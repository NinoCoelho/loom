from __future__ import annotations

import inspect
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any

from loom.llm.base import LLMProvider
from loom.llm.registry import ProviderRegistry
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
    ContentDeltaEvent,
    DoneEvent,
    ErrorEvent,
    LimitReachedEvent,
    Role,
    StopEvent,
    StopReason,
    StreamEvent,
    ToolCall,
    ToolCallDeltaEvent,
    ToolExecResultEvent,
    ToolExecStartEvent,
    ToolSpec,
    UsageEvent,
)

if TYPE_CHECKING:
    from loom.store.graphrag import GraphRAGEngine

logger = logging.getLogger(__name__)

_DEFAULT_AFFIRMATIVES = frozenset(
    {
        "yes",
        "y",
        "ok",
        "okay",
        "sure",
        "correct",
        "right",
        "yeah",
        "yep",
        "go ahead",
        "proceed",
        "continue",
        "please",
        "do it",
    }
)
_DEFAULT_NEGATIVES = frozenset(
    {
        "no",
        "n",
        "nope",
        "cancel",
        "stop",
        "don't",
        "dont",
        "negative",
    }
)

if TYPE_CHECKING:
    from loom.home import AgentHome
    from loom.permissions import AgentPermissions
    from loom.store.memory import MemoryStore
    from loom.tools.base import ToolHandler


class AgentTurn:
    __slots__ = (
        "reply",
        "iterations",
        "skills_touched",
        "messages",
        "input_tokens",
        "output_tokens",
        "tool_calls",
        "model",
    )

    def __init__(
        self,
        reply: str,
        iterations: int = 0,
        skills_touched: list[str] | None = None,
        messages: list[ChatMessage] | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        tool_calls: int = 0,
        model: str | None = None,
    ) -> None:
        self.reply = reply
        self.iterations = iterations
        self.skills_touched = skills_touched or []
        self.messages = messages or []
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.tool_calls = tool_calls
        self.model = model


class AgentConfig:
    def __init__(
        self,
        max_iterations: int = 32,
        model: str | None = None,
        system_preamble: str = "",
        on_before_turn: Callable[[list[ChatMessage]], list[ChatMessage]] | None = None,
        on_after_turn: Callable[[AgentTurn], None] | None = None,
        on_tool_result: Callable[[ToolCall, str], None] | None = None,
        extra_tools: list[ToolHandler] | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        choose_model: Callable[[list[ChatMessage]], str | None] | None = None,
        limit_message_builder: Callable[[int], str] | None = None,
        affirmatives: frozenset[str] | set[str] | None = None,
        negatives: frozenset[str] | set[str] | None = None,
        serialize_event: Callable[[StreamEvent], Any] | None = None,
        before_llm_call: Callable[
            [list[ChatMessage]], list[ChatMessage] | Awaitable[list[ChatMessage]]
        ]
        | None = None,
    ) -> None:
        self.max_iterations = max_iterations
        self.model = model
        self.system_preamble = system_preamble
        self.on_before_turn = on_before_turn
        self.on_after_turn = on_after_turn
        self.on_tool_result = on_tool_result
        self.extra_tools = extra_tools or []
        # #4: fine-grained trace hook called as on_event(kind, payload)
        # for every notable step (llm_call, tool_exec, error, ...).
        self.on_event = on_event
        # #5: router hook — pick a model per turn from the messages.
        self.choose_model = choose_model
        # #7: override the reply emitted when max_iterations is hit.
        self.limit_message_builder = limit_message_builder
        # #8: extend or replace the yes/no vocab.
        self.affirmatives = (
            frozenset(affirmatives) if affirmatives is not None else _DEFAULT_AFFIRMATIVES
        )
        self.negatives = frozenset(negatives) if negatives is not None else _DEFAULT_NEGATIVES
        # #9: wire a custom serializer for stream events (e.g. to emit
        # dict-based SSE instead of Pydantic instances).
        self.serialize_event = serialize_event
        # #11: rewrite the message list at the top of every loop iteration.
        self.before_llm_call = before_llm_call

class Agent:
    def __init__(
        self,
        provider: LLMProvider | None = None,
        provider_registry: ProviderRegistry | None = None,
        tool_registry: ToolRegistry | None = None,
        skill_registry: SkillRegistry | None = None,
        config: AgentConfig | None = None,
        agent_home: AgentHome | None = None,
        permissions: AgentPermissions | None = None,
        memory_store: MemoryStore | None = None,
        graphrag: GraphRAGEngine | None = None,
    ) -> None:
        self._provider = provider
        self._provider_registry = provider_registry
        self._tools = tool_registry or ToolRegistry()
        self._skills = skill_registry
        self._config = config or AgentConfig()
        self._pending_question: str | None = None
        self._home = agent_home
        self._permissions = permissions
        self._memory = memory_store
        self._graphrag = graphrag

    @property
    def home(self) -> AgentHome | None:
        return self._home

    @property
    def permissions(self) -> AgentPermissions | None:
        return self._permissions

    @property
    def memory(self) -> MemoryStore | None:
        return self._memory

    def _resolve_provider(self, model_id: str | None = None) -> tuple[LLMProvider, str]:
        if self._provider_registry and model_id:
            provider, upstream = self._provider_registry.resolve(model_id)
            return provider, upstream
        if self._provider_registry and self._provider_registry.default_model:
            provider, upstream = self._provider_registry.resolve(
                self._provider_registry.default_model
            )
            return provider, upstream
        if self._provider:
            return self._provider, model_id or ""
        raise RuntimeError("No LLM provider configured")

    def _build_tools(self) -> list[ToolSpec]:
        return self._tools.specs()

    def _build_system_prompt(self, context: dict[str, Any] | None = None) -> str:
        builder = PromptBuilder()

        if self._home:
            sections = load_identity_sections(self._home, self._permissions)
            for s in sections:
                builder.add(s)

            if self._memory:
                recent = self._memory.recent(limit=5, budget=1500)
                mem_section = load_memory_preview(recent)
                if mem_section:
                    builder.add(mem_section)
        elif self._config.system_preamble:
            from loom.prompt import PromptSection

            builder.add(
                PromptSection(name="preamble", content=self._config.system_preamble, priority=10)
            )

        if self._skills:
            desc_section = load_skills_section(self._skills.descriptions())
            if desc_section:
                builder.add(desc_section)

        ctx_section = load_context_section(context)
        if ctx_section:
            builder.add(ctx_section)

        pend_section = load_pending_section(self._pending_question)
        if pend_section:
            builder.add(pend_section)

        return builder.build()

    def _extract_pending_question(self, reply: str) -> str | None:
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

    async def _graphrag_enrich(
        self, messages: list[ChatMessage]
    ) -> list[ChatMessage]:
        if self._graphrag is None:
            return messages
        user_text = ""
        for msg in reversed(messages):
            if msg.role == Role.USER and msg.content:
                user_text = msg.content
                break
        if not user_text:
            return messages
        try:
            results = await self._graphrag.retrieve(user_text)
            context = self._graphrag.format_context(results)
        except Exception:
            logger.warning("graphrag retrieve/format failed", exc_info=True)
            return messages
        if not context:
            return messages
        enriched = list(messages)
        for i, msg in enumerate(enriched):
            if msg.role == Role.SYSTEM:
                enriched[i] = ChatMessage(
                    role=Role.SYSTEM,
                    content=(msg.content or "") + "\n\n" + context,
                )
                break
        else:
            enriched.insert(0, ChatMessage(role=Role.SYSTEM, content=context))
        return enriched

    def _annotate_short_reply(self, user_text: str) -> str | None:
        stripped = user_text.strip().lower()
        if stripped in self._config.affirmatives and self._pending_question:
            return f'{user_text} (affirmative answer to: "{self._pending_question}")'
        if stripped in self._config.negatives and self._pending_question:
            return f'{user_text} (negative answer to: "{self._pending_question}")'
        return None

    def _emit(self, kind: str, payload: dict[str, Any] | None = None) -> None:
        """Fire the optional on_event trace hook. Swallow handler errors
        so tracing never breaks the turn."""
        if self._config.on_event is None:
            return
        try:
            self._config.on_event(kind, payload or {})
        except Exception:
            pass

    async def _call_llm(
        self,
        provider: LLMProvider,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        model: str,
    ) -> ChatResponse:
        return await with_retry(lambda: provider.chat(messages, tools=tools, model=model))

    async def _call_llm_stream(
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

    async def _dispatch_tool(self, tc: ToolCall) -> str:
        text, _ = await self._dispatch_tool_result(tc)
        return text

    async def _dispatch_tool_result(self, tc: ToolCall) -> tuple[str, bool]:
        try:
            args = json.loads(tc.arguments) if tc.arguments else {}
        except json.JSONDecodeError:
            args = {}
        result = await self._tools.dispatch(tc.name, args)
        if self._config.on_tool_result:
            self._config.on_tool_result(tc, result.to_text())
        return result.to_text(), bool(getattr(result, "is_error", False))

    async def run_turn(
        self,
        messages: list[ChatMessage],
        context: dict[str, Any] | None = None,
        model_id: str | None = None,
    ) -> AgentTurn:
        if self._config.on_before_turn:
            messages = self._config.on_before_turn(messages)

        if messages and messages[-1].role == Role.USER and messages[-1].content:
            annotated = self._annotate_short_reply(messages[-1].content)
            if annotated:
                messages[-1] = ChatMessage(role=Role.USER, content=annotated)

        system_prompt = self._build_system_prompt(context)
        all_messages = [ChatMessage(role=Role.SYSTEM, content=system_prompt)] + messages

        # Precedence: explicit per-call arg > choose_model hook > config default.
        if model_id is None and self._config.choose_model is not None:
            try:
                model_id = self._config.choose_model(messages)
            except Exception:
                model_id = None
        if model_id is None:
            model_id = self._config.model
        provider, upstream_model = self._resolve_provider(model_id)
        model_name = upstream_model or model_id or ""
        self._emit("turn_start", {"model": model_name, "num_messages": len(messages)})
        tools = self._build_tools()

        skills_touched: list[str] = []
        total_input = 0
        total_output = 0
        total_tool_calls = 0

        if self._graphrag is not None:
            try:
                all_messages = await self._graphrag_enrich(all_messages)
            except Exception:
                logger.warning("graphrag enrichment failed", exc_info=True)

        for iteration in range(self._config.max_iterations):
            if self._config.before_llm_call is not None:
                try:
                    result = self._config.before_llm_call(all_messages)
                    if inspect.isawaitable(result):
                        result = await result
                    if result is not None:
                        all_messages = result
                except Exception as exc:
                    turn = AgentTurn(
                        reply=f"[before_llm_call hook error: {exc}]",
                        iterations=iteration,
                        skills_touched=skills_touched,
                        messages=all_messages,
                        input_tokens=total_input,
                        output_tokens=total_output,
                        tool_calls=total_tool_calls,
                        model=model_name,
                    )
                    if self._config.on_after_turn:
                        self._config.on_after_turn(turn)
                    return turn

            response: ChatResponse = await self._call_llm(
                provider, all_messages, tools, upstream_model
            )

            total_input += response.usage.input_tokens
            total_output += response.usage.output_tokens

            if (
                response.stop_reason not in (StopReason.TOOL_USE,)
                or not response.message.tool_calls
            ):
                reply = response.message.content or ""
                self._pending_question = self._extract_pending_question(reply)

                if response.model:
                    model_name = response.model

                turn = AgentTurn(
                    reply=reply,
                    iterations=iteration + 1,
                    skills_touched=skills_touched,
                    messages=all_messages,
                    input_tokens=total_input,
                    output_tokens=total_output,
                    tool_calls=total_tool_calls,
                    model=model_name,
                )
                if self._config.on_after_turn:
                    self._config.on_after_turn(turn)
                return turn

            all_messages.append(response.message)

            for tc in response.message.tool_calls:
                total_tool_calls += 1
                if tc.name == "activate_skill" and self._skills:
                    args = json.loads(tc.arguments) if tc.arguments else {}
                    skill_name = args.get("name", "")
                    skill = self._skills.get(skill_name)
                    if skill:
                        result_text = skill.body
                        skills_touched.append(skill_name)
                    else:
                        result_text = f"Skill not found: {skill_name}"
                else:
                    result_text = await self._dispatch_tool(tc)

                all_messages.append(
                    ChatMessage(
                        role=Role.TOOL,
                        content=result_text,
                        tool_call_id=tc.id,
                        name=tc.name,
                    )
                )

        limit_reply = (
            self._config.limit_message_builder(self._config.max_iterations)
            if self._config.limit_message_builder
            else "[iteration limit reached]"
        )
        turn = AgentTurn(
            reply=limit_reply,
            iterations=self._config.max_iterations,
            skills_touched=skills_touched,
            messages=all_messages,
            input_tokens=total_input,
            output_tokens=total_output,
            tool_calls=total_tool_calls,
            model=model_name,
        )
        if self._config.on_after_turn:
            self._config.on_after_turn(turn)
        return turn

    async def run_turn_stream(
        self,
        messages: list[ChatMessage],
        context: dict[str, Any] | None = None,
        model_id: str | None = None,
    ) -> AsyncIterator[Any]:
        if self._config.on_before_turn:
            messages = self._config.on_before_turn(messages)

        if messages and messages[-1].role == Role.USER and messages[-1].content:
            annotated = self._annotate_short_reply(messages[-1].content)
            if annotated:
                messages[-1] = ChatMessage(role=Role.USER, content=annotated)

        system_prompt = self._build_system_prompt(context)
        all_messages = [ChatMessage(role=Role.SYSTEM, content=system_prompt)] + messages

        if model_id is None and self._config.choose_model is not None:
            try:
                model_id = self._config.choose_model(messages)
            except Exception:
                model_id = None
        if model_id is None:
            model_id = self._config.model
        provider, upstream_model = self._resolve_provider(model_id)
        model_name = upstream_model or model_id or ""
        tools = self._build_tools()
        self._emit("stream_start", {"model": model_name, "num_messages": len(messages)})

        # #9 — apply custom serializer to every outbound event.
        serialize = self._config.serialize_event

        def _wrap(ev: Any) -> Any:
            return serialize(ev) if serialize is not None else ev

        skills_touched: list[str] = []
        total_input = 0
        total_output = 0
        total_tool_calls = 0

        if self._graphrag is not None:
            try:
                all_messages = await self._graphrag_enrich(all_messages)
            except Exception:
                logger.warning("graphrag enrichment failed", exc_info=True)

        for iteration in range(self._config.max_iterations):
            if self._config.before_llm_call is not None:
                try:
                    result = self._config.before_llm_call(all_messages)
                    if inspect.isawaitable(result):
                        result = await result
                    if result is not None:
                        all_messages = result
                except Exception as exc:
                    err_ev = ErrorEvent(
                        message=str(exc),
                        reason="hook_error",
                    )
                    yield _wrap(err_ev)
                    yield _wrap(DoneEvent(context={"model": model_name, "iterations": iteration}))
                    return

            content_parts: list[str] = []
            tool_call_parts: dict[int, dict[str, Any]] = {}
            stop_reason: StopReason = StopReason.UNKNOWN
            has_forwarded = False

            try:
                # #2 — with_retry wraps stream *creation*; if creation
                # fails after N attempts we surface an ErrorEvent rather
                # than propagate (caller may still be iterating).
                stream: AsyncIterator[StreamEvent] = await self._call_llm_stream(
                    provider, all_messages, tools, upstream_model
                )
            except Exception as exc:
                from loom.errors import classify_api_error

                cls = classify_api_error(exc)
                err_ev = ErrorEvent(
                    message=str(exc),
                    reason=cls.reason.value if hasattr(cls.reason, "value") else str(cls.reason),
                    status_code=cls.status_code,
                    retryable=cls.retryable,
                )
                self._emit("stream_error", {"phase": "create", "message": str(exc)})
                yield _wrap(err_ev)
                yield _wrap(DoneEvent(context={"model": model_name, "iterations": iteration}))
                return

            try:
                async for event in stream:
                    if isinstance(event, ContentDeltaEvent):
                        if not has_forwarded:
                            has_forwarded = True
                        content_parts.append(event.delta)
                        yield _wrap(ContentDeltaEvent(delta=event.delta))
                    elif isinstance(event, ToolCallDeltaEvent):
                        idx = event.index
                        if idx not in tool_call_parts:
                            tool_call_parts[idx] = {
                                "id": event.id,
                                "name": event.name,
                                "arguments": "",
                            }
                        if event.id:
                            tool_call_parts[idx]["id"] = event.id
                        if event.name:
                            tool_call_parts[idx]["name"] = event.name
                        if event.arguments_delta:
                            tool_call_parts[idx]["arguments"] += event.arguments_delta
                        yield _wrap(event)
                    elif isinstance(event, UsageEvent):
                        total_input += event.usage.input_tokens
                        total_output += event.usage.output_tokens
                    elif isinstance(event, StopEvent):
                        stop_reason = event.stop_reason
            except Exception as exc:
                # Mid-stream failure: can't retry without replaying
                # partial assistant output. Surface as ErrorEvent + Done
                # so the caller can tear down cleanly.
                from loom.errors import classify_api_error

                cls = classify_api_error(exc)
                err_ev = ErrorEvent(
                    message=str(exc),
                    reason=cls.reason.value if hasattr(cls.reason, "value") else str(cls.reason),
                    status_code=cls.status_code,
                    retryable=cls.retryable,
                )
                self._emit(
                    "stream_error",
                    {"phase": "iterate", "forwarded": has_forwarded, "message": str(exc)},
                )
                yield _wrap(err_ev)
                yield _wrap(
                    DoneEvent(
                        context={
                            "model": model_name,
                            "iterations": iteration,
                            "partial": has_forwarded,
                        }
                    )
                )
                return

            if stop_reason not in (StopReason.TOOL_USE,) or not tool_call_parts:
                reply = "".join(content_parts)
                self._pending_question = self._extract_pending_question(reply)

                yield _wrap(ContentDeltaEvent(delta=""))

                turn = AgentTurn(
                    reply=reply,
                    iterations=iteration + 1,
                    skills_touched=skills_touched,
                    messages=all_messages,
                    input_tokens=total_input,
                    output_tokens=total_output,
                    tool_calls=total_tool_calls,
                    model=model_name,
                )
                if self._config.on_after_turn:
                    self._config.on_after_turn(turn)
                final_assistant = ChatMessage(role=Role.ASSISTANT, content=reply)
                yield _wrap(
                    DoneEvent(
                        context={
                            "model": model_name,
                            "iterations": iteration + 1,
                            "input_tokens": total_input,
                            "output_tokens": total_output,
                            "tool_calls": total_tool_calls,
                            "messages": [m.model_dump() for m in all_messages + [final_assistant]],
                        }
                    )
                )
                return

            assembled_tcs: list[ToolCall] = []
            for idx in sorted(tool_call_parts.keys()):
                parts = tool_call_parts[idx]
                tc = ToolCall(
                    id=parts["id"] or f"tc_{idx}",
                    name=parts["name"] or "",
                    arguments=parts["arguments"],
                )
                assembled_tcs.append(tc)

            all_messages.append(
                ChatMessage(
                    role=Role.ASSISTANT,
                    content="".join(content_parts) or None,
                    tool_calls=assembled_tcs,
                )
            )

            for tc in assembled_tcs:
                total_tool_calls += 1
                yield _wrap(
                    ToolExecStartEvent(tool_call_id=tc.id, name=tc.name, arguments=tc.arguments)
                )
                is_error = False
                if tc.name == "activate_skill" and self._skills:
                    args = json.loads(tc.arguments) if tc.arguments else {}
                    skill_name = args.get("name", "")
                    skill = self._skills.get(skill_name)
                    if skill:
                        result_text = skill.body
                        skills_touched.append(skill_name)
                    else:
                        result_text = f"Skill not found: {skill_name}"
                        is_error = True
                else:
                    result_text, is_error = await self._dispatch_tool_result(tc)

                yield _wrap(
                    ToolExecResultEvent(
                        tool_call_id=tc.id,
                        name=tc.name,
                        text=result_text,
                        is_error=is_error,
                    )
                )

                all_messages.append(
                    ChatMessage(
                        role=Role.TOOL,
                        content=result_text,
                        tool_call_id=tc.id,
                        name=tc.name,
                    )
                )

        limit_reply = (
            self._config.limit_message_builder(self._config.max_iterations)
            if self._config.limit_message_builder
            else "[iteration limit reached]"
        )
        yield _wrap(ContentDeltaEvent(delta=limit_reply))
        final_assistant = ChatMessage(role=Role.ASSISTANT, content=limit_reply)
        yield _wrap(LimitReachedEvent(iterations=self._config.max_iterations))
        yield _wrap(
            DoneEvent(
                context={
                    "model": model_name,
                    "iterations": self._config.max_iterations,
                    "input_tokens": total_input,
                    "output_tokens": total_output,
                    "tool_calls": total_tool_calls,
                    "messages": [m.model_dump() for m in all_messages + [final_assistant]],
                    "limit_reached": True,
                }
            )
        )
