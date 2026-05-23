"""Agent loop implementation.

Orchestrates the turn-based agent cycle: building the system prompt,
calling the LLM (with or without streaming), dispatching tool calls,
and accumulating messages until the model stops or the iteration limit
is reached. Supports GraphRAG enrichment, per-turn hooks, and
custom event serialisation.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from loom.llm.base import LLMProvider
from loom.llm.registry import ProviderRegistry
from loom.skills.registry import SkillRegistry
from loom.tools.registry import ToolRegistry
from loom.types import (
    ChatMessage,
    ContentDeltaEvent,
    DoneEvent,
    ErrorEvent,
    LimitReachedEvent,
    OverflowEvent,
    Role,
    StopReason,
    StreamEvent,
    ToolCall,
    ToolExecResultEvent,
    ToolExecStartEvent,
)

from loom.loop._executor import (
    TurnDeps,
    TurnExecutor,
    annotate_short_reply,
    build_system_prompt,
    extract_pending_question,
    graphrag_enrich,
    resolve_provider,
)
from loom.loop._turn import TurnState
from loom.loop._types import AgentConfig, AgentTurn

if TYPE_CHECKING:
    from loom.home import AgentHome
    from loom.permissions import AgentPermissions
    from loom.store.graphrag import GraphRAGEngine
    from loom.store.memory import MemoryStore

logger = logging.getLogger(__name__)


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
        return resolve_provider(self._provider, self._provider_registry, model_id)

    def _build_tools(self) -> list[Any]:
        return self._tools.specs()

    def _build_system_prompt(self, context: dict[str, Any] | None = None) -> str:
        return build_system_prompt(
            self._home,
            self._permissions,
            self._memory,
            self._skills,
            self._config,
            self._pending_question,
            context,
        )

    def _extract_pending_question(self, reply: str) -> str | None:
        return extract_pending_question(reply)

    def _annotate_short_reply(self, user_text: str) -> str | None:
        return annotate_short_reply(
            user_text,
            self._pending_question,
            self._config.affirmatives,
            self._config.negatives,
        )

    async def _graphrag_enrich(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        return await graphrag_enrich(self._graphrag, messages)

    def _emit(self, kind: str, payload: dict[str, Any] | None = None) -> None:
        if self._config.on_event is None:
            return
        try:
            self._config.on_event(kind, payload or {})
        except Exception:
            pass

    def _make_deps(self) -> TurnDeps:
        return TurnDeps(
            provider=self._provider,
            provider_registry=self._provider_registry,
            tools=self._tools,
            skills=self._skills,
            config=self._config,
            home=self._home,
            permissions=self._permissions,
            memory=self._memory,
            graphrag=self._graphrag,
            emit_fn=self._emit,
        )

    async def run_turn(
        self,
        messages: list[ChatMessage],
        context: dict[str, Any] | None = None,
        model_id: str | None = None,
    ) -> AgentTurn:
        state = TurnState(pending_question=self._pending_question)
        executor = TurnExecutor(self._make_deps())

        all_messages, provider, upstream_model, model_name, tools = (
            await executor.prepare(messages, context, model_id, state)
        )
        self._emit("turn_start", {"model": model_name, "num_messages": len(messages)})
        ctx_window = self._config.resolve_context_window(model_name)

        for iteration in range(self._config.max_iterations):
            hooked, hook_exc = await executor.apply_before_hook(all_messages)
            if hooked is None:
                turn = executor.build_agent_turn(
                    f"[before_llm_call hook error: {hook_exc}]",
                    iteration, state, all_messages, model_name,
                )
                if self._config.on_after_turn:
                    self._config.on_after_turn(turn)
                self._pending_question = state.pending_question
                return turn
            all_messages = hooked

            ov = executor.check_overflow(all_messages, ctx_window)
            if ov is not None:
                self._emit("context_overflow", {
                    "iteration": iteration,
                    "estimated_input_tokens": ov.estimated_input_tokens,
                    "context_window": ov.context_window,
                    "headroom": ov.headroom,
                })
                turn = executor.build_agent_turn(
                    ov.detail or "context overflow",
                    iteration, state, all_messages, model_name,
                )
                if self._config.on_after_turn:
                    self._config.on_after_turn(turn)
                self._pending_question = state.pending_question
                return turn

            response: ChatResponse = await executor.call_llm(
                provider, all_messages, tools, upstream_model
            )
            state.total_input += response.usage.input_tokens
            state.total_output += response.usage.output_tokens

            if (
                response.stop_reason not in (StopReason.TOOL_USE,)
                or not response.message.tool_calls
            ):
                reply = response.message.text_content or ""
                state.pending_question = extract_pending_question(reply)
                if response.model:
                    model_name = response.model
                turn = executor.build_agent_turn(
                    reply, iteration + 1, state, all_messages, model_name,
                )
                if self._config.on_after_turn:
                    self._config.on_after_turn(turn)
                self._pending_question = state.pending_question
                return turn

            all_messages.append(response.message)
            tc_signature = tuple(
                (tc.name, tc.arguments) for tc in response.message.tool_calls
            )

            iter_all_errored = True
            for tc in response.message.tool_calls:
                state.total_tool_calls += 1
                result_text, is_error, tool_content_parts, state.skills_touched = (
                    await executor.handle_tool_call(tc, state.skills_touched)
                )
                if not is_error:
                    iter_all_errored = False
                all_messages.append(
                    executor.build_tool_message(tc, result_text, tool_content_parts)
                )

            executor.update_stuck_state(
                tc_signature, iter_all_errored, bool(response.message.tool_calls), state
            )
            if executor.is_stuck(state):
                stuck_reply = (
                    "[stopped: model repeated the same failing tool call "
                    f"{state.identical_tc_streak} times in a row]"
                )
                turn = executor.build_agent_turn(
                    stuck_reply, iteration + 1, state, all_messages, model_name,
                )
                if self._config.on_after_turn:
                    self._config.on_after_turn(turn)
                self._pending_question = state.pending_question
                return turn

        limit_reply = (
            self._config.limit_message_builder(self._config.max_iterations)
            if self._config.limit_message_builder
            else "[iteration limit reached]"
        )
        turn = executor.build_agent_turn(
            limit_reply, self._config.max_iterations, state, all_messages, model_name,
        )
        if self._config.on_after_turn:
            self._config.on_after_turn(turn)
        self._pending_question = state.pending_question
        return turn

    async def run_turn_stream(
        self,
        messages: list[ChatMessage],
        context: dict[str, Any] | None = None,
        model_id: str | None = None,
    ) -> AsyncIterator[Any]:
        state = TurnState(pending_question=self._pending_question)
        executor = TurnExecutor(self._make_deps())

        all_messages, provider, upstream_model, model_name, tools = (
            await executor.prepare(messages, context, model_id, state)
        )
        self._emit("stream_start", {"model": model_name, "num_messages": len(messages)})

        serialize = self._config.serialize_event

        def _wrap(ev: Any) -> Any:
            return serialize(ev) if serialize is not None else ev

        ctx_window = self._config.resolve_context_window(model_name)

        for iteration in range(self._config.max_iterations):
            hooked, hook_exc = await executor.apply_before_hook(all_messages)
            if hooked is None:
                yield _wrap(ErrorEvent(message=str(hook_exc), reason="hook_error"))
                yield _wrap(DoneEvent(model=model_name, iterations=iteration))
                self._pending_question = state.pending_question
                return
            all_messages = hooked

            ov = executor.check_overflow(all_messages, ctx_window)
            if ov is not None:
                self._emit("context_overflow", {
                    "iteration": iteration,
                    "estimated_input_tokens": ov.estimated_input_tokens,
                    "context_window": ov.context_window,
                    "headroom": ov.headroom,
                })
                yield _wrap(OverflowEvent(
                    message=ov.detail or "context overflow",
                    estimated_input_tokens=ov.estimated_input_tokens,
                    context_window=ov.context_window,
                    headroom=ov.headroom,
                    iteration=iteration,
                ))
                yield _wrap(DoneEvent(
                    model=model_name,
                    iterations=iteration,
                    input_tokens=state.total_input,
                    output_tokens=state.total_output,
                    tool_calls=state.total_tool_calls,
                    stop_reason=StopReason.STOP,
                    skills_touched=state.skills_touched,
                    context={
                        "messages": [m.model_dump() for m in all_messages],
                        "context_overflow": True,
                    },
                ))
                self._pending_question = state.pending_question
                return

            content_parts: list[str] = []
            tool_call_parts: dict[int, dict[str, Any]] = {}
            stop_reason: StopReason = StopReason.UNKNOWN
            has_forwarded = False

            try:
                stream: AsyncIterator[StreamEvent] = await executor.call_llm_stream(
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
                yield _wrap(DoneEvent(model=model_name, iterations=iteration))
                self._pending_question = state.pending_question
                return

            try:
                async for event in stream:
                    if isinstance(event, ContentDeltaEvent):
                        if not has_forwarded:
                            has_forwarded = True
                        content_parts.append(event.delta)
                        yield _wrap(ContentDeltaEvent(delta=event.delta))
                    elif isinstance(event, StreamEvent):
                        if event.type == "tool_call_delta":
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
                            if hasattr(event, "arguments_delta") and event.arguments_delta:
                                tool_call_parts[idx]["arguments"] += event.arguments_delta
                            yield _wrap(event)
                        elif event.type == "usage":
                            state.total_input += event.usage.input_tokens
                            state.total_output += event.usage.output_tokens
                        elif event.type == "stop":
                            stop_reason = event.stop_reason
            except Exception as exc:
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
                yield _wrap(DoneEvent(
                    model=model_name,
                    iterations=iteration,
                    context={"partial": has_forwarded},
                ))
                self._pending_question = state.pending_question
                return

            if stop_reason not in (StopReason.TOOL_USE,) or not tool_call_parts:
                reply = "".join(content_parts)
                state.pending_question = extract_pending_question(reply)

                yield _wrap(ContentDeltaEvent(delta=""))

                turn = executor.build_agent_turn(
                    reply, iteration + 1, state, all_messages, model_name,
                )
                if self._config.on_after_turn:
                    self._config.on_after_turn(turn)
                final_assistant = ChatMessage(role=Role.ASSISTANT, content=reply)
                yield _wrap(DoneEvent(
                    model=model_name,
                    iterations=iteration + 1,
                    input_tokens=state.total_input,
                    output_tokens=state.total_output,
                    tool_calls=state.total_tool_calls,
                    stop_reason=stop_reason,
                    skills_touched=state.skills_touched,
                    context={
                        "messages": [m.model_dump() for m in all_messages + [final_assistant]],
                    },
                ))
                self._pending_question = state.pending_question
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

            tc_signature = tuple((tc.name, tc.arguments) for tc in assembled_tcs)

            iter_all_errored = True
            for tc in assembled_tcs:
                state.total_tool_calls += 1
                yield _wrap(
                    ToolExecStartEvent(tool_call_id=tc.id, name=tc.name, arguments=tc.arguments)
                )
                result_text, is_error, stream_tool_parts, state.skills_touched = (
                    await executor.handle_tool_call(tc, state.skills_touched)
                )
                if not is_error:
                    iter_all_errored = False

                yield _wrap(ToolExecResultEvent(
                    tool_call_id=tc.id,
                    name=tc.name,
                    text=result_text,
                    is_error=is_error,
                ))

                all_messages.append(
                    executor.build_tool_message(tc, result_text, stream_tool_parts)
                )

            executor.update_stuck_state(
                tc_signature, iter_all_errored, bool(assembled_tcs), state
            )
            if executor.is_stuck(state):
                stuck_reply = (
                    "[stopped: model repeated the same failing tool call "
                    f"{state.identical_tc_streak} times in a row]"
                )
                yield _wrap(ContentDeltaEvent(delta=stuck_reply))
                final_assistant = ChatMessage(role=Role.ASSISTANT, content=stuck_reply)
                yield _wrap(DoneEvent(
                    model=model_name,
                    iterations=iteration + 1,
                    input_tokens=state.total_input,
                    output_tokens=state.total_output,
                    tool_calls=state.total_tool_calls,
                    stop_reason=StopReason.STOP,
                    skills_touched=state.skills_touched,
                    context={
                        "messages": [
                            m.model_dump() for m in all_messages + [final_assistant]
                        ],
                        "stuck_loop": True,
                    },
                ))
                self._pending_question = state.pending_question
                return

        limit_reply = (
            self._config.limit_message_builder(self._config.max_iterations)
            if self._config.limit_message_builder
            else "[iteration limit reached]"
        )
        yield _wrap(ContentDeltaEvent(delta=limit_reply))
        final_assistant = ChatMessage(role=Role.ASSISTANT, content=limit_reply)
        yield _wrap(LimitReachedEvent(iterations=self._config.max_iterations))
        yield _wrap(DoneEvent(
            model=model_name,
            iterations=self._config.max_iterations,
            input_tokens=state.total_input,
            output_tokens=state.total_output,
            tool_calls=state.total_tool_calls,
            skills_touched=state.skills_touched,
            context={
                "messages": [m.model_dump() for m in all_messages + [final_assistant]],
                "limit_reached": True,
            },
        ))
        self._pending_question = state.pending_question
