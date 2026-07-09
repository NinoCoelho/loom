from __future__ import annotations

import inspect
import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any

from loom.llm.base import LLMProvider
from loom.llm.registry import ProviderRegistry
from loom.loop._turn import TurnState
from loom.loop._types import AgentConfig, AgentTurn
from loom.loop.compaction import CompactionRequest, classify_zone
from loom.overflow import OverflowCheck
from loom.overflow import check_overflow as _check_overflow
from loom.prompt import (
    PromptBuilder,
    load_context_section,
    load_identity_sections,
    load_memory_preview,
    load_pending_section,
    load_skills_section,
)
from loom.retry import with_retry
from loom.skills.registry import SkillRegistry
from loom.tools.registry import ToolRegistry
from loom.types import (
    ChatMessage,
    ChatResponse,
    Role,
    StreamEvent,
    ToolCall,
    ToolSpec,
)

if TYPE_CHECKING:
    from loom.home import AgentHome
    from loom.permissions import AgentPermissions
    from loom.store.graphrag import GraphRAGEngine
    from loom.store.memory import MemoryStore

logger = logging.getLogger(__name__)


def annotate_short_reply(
    user_text: str,
    pending_question: str | None,
    affirmatives: frozenset[str],
    negatives: frozenset[str],
) -> str | None:
    stripped = user_text.strip().lower()
    if stripped in affirmatives and pending_question:
        return f'{user_text} (affirmative answer to: "{pending_question}")'
    if stripped in negatives and pending_question:
        return f'{user_text} (negative answer to: "{pending_question}")'
    return None


def extract_pending_question(reply: str) -> str | None:
    last_q = reply.rfind("?")
    if last_q == -1:
        return None
    start = max(0, last_q - 200)
    segment = reply[start : last_q + 1]
    first_nl = segment.find("\n")
    if first_nl >= 0:
        segment = segment[first_nl + 1 :]
    if len(segment) > 500:
        segment = segment[-500:]
    return segment.strip()


def build_system_prompt(
    home: AgentHome | None,
    permissions: AgentPermissions | None,
    memory: MemoryStore | None,
    skills: SkillRegistry | None,
    config: AgentConfig,
    pending_question: str | None,
    context: dict[str, Any] | None = None,
) -> str:
    builder = PromptBuilder()

    if home:
        sections = load_identity_sections(home, permissions)
        for s in sections:
            builder.add(s)

        if memory:
            recent = memory.recent(limit=5, budget=1500)
            mem_section = load_memory_preview(recent)
            if mem_section:
                builder.add(mem_section)
    elif config.system_preamble:
        from loom.prompt import PromptSection

        builder.add(
            PromptSection(name="preamble", content=config.system_preamble, priority=10)
        )

    if skills:
        desc_section = load_skills_section(skills.descriptions())
        if desc_section:
            builder.add(desc_section)

    ctx_section = load_context_section(context)
    if ctx_section:
        builder.add(ctx_section)

    pend_section = load_pending_section(pending_question)
    if pend_section:
        builder.add(pend_section)

    return builder.build()


def resolve_provider(
    provider: LLMProvider | None,
    provider_registry: ProviderRegistry | None,
    model_id: str | None = None,
) -> tuple[LLMProvider, str]:
    if provider_registry and model_id:
        p, upstream = provider_registry.resolve(model_id)
        return p, upstream
    if provider_registry and provider_registry.default_model:
        p, upstream = provider_registry.resolve(provider_registry.default_model)
        return p, upstream
    if provider:
        return provider, model_id or ""
    raise RuntimeError("No LLM provider configured")


async def graphrag_enrich(
    graphrag: GraphRAGEngine | None,
    messages: list[ChatMessage],
) -> list[ChatMessage]:
    if graphrag is None:
        return messages
    user_text = ""
    for msg in reversed(messages):
        if msg.role == Role.USER and msg.text_content:
            user_text = msg.text_content
            break
    if not user_text:
        return messages
    try:
        results = await graphrag.retrieve(user_text)
        ctx = graphrag.format_context(results)
    except Exception:
        logger.warning("graphrag retrieve/format failed", exc_info=True)
        return messages
    if not ctx:
        return messages
    enriched = list(messages)
    for i, msg in enumerate(enriched):
        if msg.role == Role.SYSTEM:
            enriched[i] = ChatMessage(
                role=Role.SYSTEM,
                content=(msg.text_content or "") + "\n\n" + ctx,
            )
            break
    else:
        enriched.insert(0, ChatMessage(role=Role.SYSTEM, content=ctx))
    return enriched


class TurnDeps:
    def __init__(
        self,
        provider: LLMProvider | None,
        provider_registry: ProviderRegistry | None,
        tools: ToolRegistry,
        skills: SkillRegistry | None,
        config: AgentConfig,
        home: AgentHome | None,
        permissions: AgentPermissions | None,
        memory: MemoryStore | None,
        graphrag: GraphRAGEngine | None,
        emit_fn: Callable,
    ) -> None:
        self.provider = provider
        self.provider_registry = provider_registry
        self.tools = tools
        self.skills = skills
        self.config = config
        self.home = home
        self.permissions = permissions
        self.memory = memory
        self.graphrag = graphrag
        self.emit_fn = emit_fn


class TurnExecutor:
    def __init__(self, deps: TurnDeps) -> None:
        self._deps = deps

    async def prepare(
        self,
        messages: list[ChatMessage],
        context: dict[str, Any] | None,
        model_id: str | None,
        state: TurnState,
    ) -> tuple[list[ChatMessage], LLMProvider, str, str, list[ToolSpec]]:
        if self._deps.config.on_before_turn:
            messages = self._deps.config.on_before_turn(messages)

        if messages and messages[-1].role == Role.USER and messages[-1].text_content:
            annotated = annotate_short_reply(
                messages[-1].text_content,
                state.pending_question,
                self._deps.config.affirmatives,
                self._deps.config.negatives,
            )
            if annotated:
                messages[-1] = ChatMessage(role=Role.USER, content=annotated)

        system_prompt = build_system_prompt(
            self._deps.home,
            self._deps.permissions,
            self._deps.memory,
            self._deps.skills,
            self._deps.config,
            state.pending_question,
            context,
        )
        all_messages = [ChatMessage(role=Role.SYSTEM, content=system_prompt)] + messages

        if model_id is None and self._deps.config.choose_model is not None:
            try:
                model_id = self._deps.config.choose_model(messages)
            except Exception:
                model_id = None
        if model_id is None:
            model_id = self._deps.config.model
        provider, upstream_model = resolve_provider(
            self._deps.provider, self._deps.provider_registry, model_id
        )
        model_name = upstream_model or model_id or ""
        tools = self._deps.tools.specs()

        if self._deps.graphrag is not None:
            try:
                all_messages = await graphrag_enrich(self._deps.graphrag, all_messages)
            except Exception:
                logger.warning("graphrag enrichment failed", exc_info=True)

        return all_messages, provider, upstream_model, model_name, tools

    async def apply_before_hook(
        self,
        all_messages: list[ChatMessage],
    ) -> tuple[list[ChatMessage] | None, Exception | None]:
        if self._deps.config.before_llm_call is None:
            return all_messages, None
        try:
            result = self._deps.config.before_llm_call(all_messages)
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                return result, None
        except Exception as exc:
            return None, exc
        return all_messages, None

    def check_overflow(
        self,
        all_messages: list[ChatMessage],
        ctx_window: int,
    ):
        if ctx_window <= 0:
            return None
        ov = _check_overflow(
            all_messages,
            context_window=ctx_window,
            output_headroom=self._deps.config.overflow_output_headroom,
            tools_overhead=self._deps.config.overflow_tools_overhead,
            estimator=self._deps.config.estimate_input_tokens,
        )
        return ov if ov.overflowed else None

    async def resolve_overflow(
        self,
        all_messages: list[ChatMessage],
        ctx_window: int,
        iteration: int,
    ) -> tuple[list[ChatMessage], OverflowCheck | None]:
        """Check overflow; if a compactor is configured, rescue the turn.

        Returns ``(messages, overflow_check_or_None)``:

        * ``(messages, None)`` — fits (or was compacted to fit); proceed.
        * ``(messages, ov)`` — still overflowing after all attempts (or no
          compactor wired). The caller should emit ``OverflowEvent`` and stop,
          exactly as it did before compaction existed.

        With ``config.compactor is None`` this is a thin wrapper around
        ``check_overflow`` and behavior is identical to pre-compaction loom.
        """
        ov = self.check_overflow(all_messages, ctx_window)
        if ov is None:
            return all_messages, None

        compactor = self._deps.config.compactor
        if compactor is None:
            return all_messages, ov

        messages = all_messages
        last_ov = ov
        max_attempts = self._deps.config.max_compaction_attempts
        overhead = self._deps.config.overflow_tools_overhead
        emit = self._deps.emit_fn

        for attempt in range(1, max_attempts + 1):
            zone = classify_zone(
                last_ov.estimated_input_tokens, ctx_window, tools_overhead=overhead
            )
            request = CompactionRequest(
                messages=messages,
                estimated_tokens=last_ov.estimated_input_tokens,
                context_window=ctx_window,
                zone=zone,
                iteration=iteration,
                attempt=attempt,
            )
            if emit:
                emit(
                    "before_compaction",
                    {
                        "attempt": attempt,
                        "iteration": iteration,
                        "estimated_tokens": last_ov.estimated_input_tokens,
                        "zone": zone,
                    },
                )
            try:
                result = await compactor(request)
            except Exception as exc:  # noqa: BLE001 — compactor is consumer code
                logger.warning(
                    "compactor attempt %d/%d raised; aborting compaction",
                    attempt,
                    max_attempts,
                    exc_info=True,
                )
                if emit:
                    emit(
                        "compaction_error",
                        {"attempt": attempt, "error": str(exc)},
                    )
                return messages, last_ov

            compacted = result.messages if result.messages is not None else messages
            tokens_after = result.tokens_after
            if emit:
                emit(
                    "after_compaction",
                    {
                        "attempt": attempt,
                        "actions": list(result.actions),
                        "tokens_before": last_ov.estimated_input_tokens,
                        "tokens_after": tokens_after,
                        "still_overflowed": result.still_overflowed,
                    },
                )
            messages = compacted
            # Authoritative re-check. A compactor may claim success (e.g. it
            # used a cheaper estimator) but the loop's own check is the gate.
            last_ov = self.check_overflow(messages, ctx_window)
            if last_ov is None:
                logger.info(
                    "compaction resolved overflow on attempt %d/%d "
                    "(tokens %d -> %d)",
                    attempt,
                    max_attempts,
                    request.estimated_tokens,
                    tokens_after,
                )
                return messages, None

        logger.warning(
            "compaction exhausted after %d attempt(s); still overflowing",
            max_attempts,
        )
        return messages, last_ov

    async def call_llm(
        self,
        provider: LLMProvider,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        model: str,
    ) -> ChatResponse:
        return await with_retry(lambda: provider.chat(messages, tools=tools, model=model))

    async def call_llm_stream(
        self,
        provider: LLMProvider,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        model: str,
    ) -> AsyncIterator[StreamEvent]:
        async def _factory():
            return provider.chat_stream(messages, tools=tools, model=model)

        stream = await with_retry(_factory)
        return stream

    async def handle_tool_call(
        self,
        tc: ToolCall,
        skills_touched: list[str],
    ) -> tuple[str, bool, list[Any] | None, list[str]]:
        is_error = False
        if tc.name == "activate_skill" and self._deps.skills:
            args = json.loads(tc.arguments) if tc.arguments else {}
            skill_name = args.get("name", "")
            skill = self._deps.skills.get(skill_name)
            if skill:
                result_text = skill.body
                skills_touched.append(skill_name)
            else:
                result_text = f"Skill not found: {skill_name}"
                is_error = True
        else:
            result_text, is_error, tool_parts = await self._dispatch_tool_result(tc)
            return result_text, is_error, tool_parts, skills_touched
        return result_text, is_error, None, skills_touched

    def build_tool_message(
        self,
        tc: ToolCall,
        result_text: str,
        content_parts: list[Any] | None,
    ) -> ChatMessage:
        if content_parts:
            from loom.types import TextPart

            tool_msg_content: str | list[Any] = [TextPart(text=result_text)] + content_parts
        else:
            tool_msg_content = result_text
        return ChatMessage(
            role=Role.TOOL,
            content=tool_msg_content,
            tool_call_id=tc.id,
            name=tc.name,
        )

    def update_stuck_state(
        self,
        tc_signature: tuple[tuple[str, str], ...],
        iter_all_errored: bool,
        has_tool_calls: bool,
        state: TurnState,
    ) -> None:
        if iter_all_errored and has_tool_calls and tc_signature == state.last_tc_signature:
            state.identical_tc_streak += 1
        elif iter_all_errored and has_tool_calls:
            state.identical_tc_streak = 1
            state.last_tc_signature = tc_signature
        else:
            state.identical_tc_streak = 0
            state.last_tc_signature = tc_signature

    def is_stuck(self, state: TurnState) -> bool:
        return state.identical_tc_streak >= TurnState.IDENTICAL_TC_LIMIT

    def build_agent_turn(
        self,
        reply: str,
        iteration: int,
        state: TurnState,
        all_messages: list[ChatMessage],
        model_name: str,
    ) -> AgentTurn:
        return AgentTurn(
            reply=reply,
            iterations=iteration,
            skills_touched=state.skills_touched,
            messages=all_messages,
            input_tokens=state.total_input,
            output_tokens=state.total_output,
            tool_calls=state.total_tool_calls,
            model=model_name,
        )

    async def _dispatch_tool_result(
        self, tc: ToolCall
    ) -> tuple[str, bool, list[Any] | None]:
        try:
            args = json.loads(tc.arguments) if tc.arguments else {}
        except json.JSONDecodeError as exc:
            return (
                f"error: tool arguments were not valid JSON ({exc}). "
                f"Got: {tc.arguments!r}. Retry with a valid JSON object "
                f"matching the tool schema.",
                True,
                None,
            )
        result = await self._deps.tools.dispatch(tc.name, args)
        if self._deps.config.on_tool_result:
            self._deps.config.on_tool_result(tc, result.to_text())
        return (
            result.to_text(),
            bool(getattr(result, "is_error", False)),
            getattr(result, "content_parts", None),
        )
