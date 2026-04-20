from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

from loom.errors import LLMTransportError
from loom.llm.base import LLMProvider
from loom.llm.registry import ProviderRegistry
from loom.retry import with_retry
from loom.skills.registry import SkillRegistry
from loom.tools.registry import ToolRegistry
from loom.types import (
    ChatMessage,
    ChatResponse,
    ContentDeltaEvent,
    Role,
    StopReason,
    StreamEvent,
    ToolCall,
    ToolSpec,
    Usage,
)


class AgentTurn:
    __slots__ = ("reply", "iterations", "skills_touched", "messages",
                 "input_tokens", "output_tokens", "tool_calls", "model")

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
    ) -> None:
        self.max_iterations = max_iterations
        self.model = model
        self.system_preamble = system_preamble
        self.on_before_turn = on_before_turn
        self.on_after_turn = on_after_turn
        self.on_tool_result = on_tool_result


class Agent:
    def __init__(
        self,
        provider: LLMProvider | None = None,
        provider_registry: ProviderRegistry | None = None,
        tool_registry: ToolRegistry | None = None,
        skill_registry: SkillRegistry | None = None,
        config: AgentConfig | None = None,
    ) -> None:
        self._provider = provider
        self._provider_registry = provider_registry
        self._tools = tool_registry or ToolRegistry()
        self._skills = skill_registry
        self._config = config or AgentConfig()
        self._pending_question: str | None = None

    def _resolve_provider(self, model_id: str | None = None) -> tuple[LLMProvider, str]:
        if self._provider_registry and model_id:
            provider, upstream = self._provider_registry.resolve(model_id)
            return provider, upstream
        if self._provider_registry and self._provider_registry.default_model:
            provider, upstream = self._provider_registry.resolve(self._provider_registry.default_model)
            return provider, upstream
        if self._provider:
            return self._provider, model_id or ""
        raise RuntimeError("No LLM provider configured")

    def _build_tools(self) -> list[ToolSpec]:
        return self._tools.specs()

    def _build_system_prompt(self, context: dict[str, Any] | None = None) -> str:
        parts: list[str] = []
        if self._config.system_preamble:
            parts.append(self._config.system_preamble)
        if context:
            ctx_lines = [f"{k}: {v}" for k, v in context.items()]
            parts.append("Context:\n" + "\n".join(ctx_lines))
        if self._skills:
            descs = self._skills.descriptions()
            if descs:
                lines = [f"- {name} -- {desc}" for name, desc in descs]
                parts.append("Available skills:\n" + "\n".join(lines))
        if self._pending_question:
            parts.append(f"Pending question from last turn: {self._pending_question}")
        return "\n\n".join(parts)

    def _extract_pending_question(self, reply: str) -> str | None:
        last_q = reply.rfind("?")
        if last_q == -1:
            return None
        start = max(0, last_q - 200)
        segment = reply[start:last_q + 1]
        first_nl = segment.find("\n")
        if first_nl >= 0:
            segment = segment[first_nl + 1:]
        if len(segment) > 500:
            segment = segment[-500:]
        return segment.strip()

    def _annotate_short_reply(self, user_text: str) -> str | None:
        stripped = user_text.strip().lower()
        affirmatives = {"yes", "y", "ok", "okay", "sure", "correct", "right", "yeah", "yep", "go ahead", "proceed", "continue", "please", "do it"}
        negatives = {"no", "n", "nope", "cancel", "stop", "don't", "dont", "negative"}
        if stripped in affirmatives and self._pending_question:
            return f"{user_text} (affirmative answer to: \"{self._pending_question}\")"
        if stripped in negatives and self._pending_question:
            return f"{user_text} (negative answer to: \"{self._pending_question}\")"
        return None

    async def _call_llm(
        self,
        provider: LLMProvider,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        model: str,
    ) -> ChatResponse:
        return await with_retry(
            lambda: provider.chat(messages, tools=tools, model=model)
        )

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
        try:
            args = json.loads(tc.arguments) if tc.arguments else {}
        except json.JSONDecodeError:
            args = {}
        result = await self._tools.dispatch(tc.name, args)
        if self._config.on_tool_result:
            self._config.on_tool_result(tc, result.to_text())
        return result.to_text()

    async def run_turn(
        self,
        messages: list[ChatMessage],
        context: dict[str, Any] | None = None,
    ) -> AgentTurn:
        if self._config.on_before_turn:
            messages = self._config.on_before_turn(messages)

        if messages and messages[-1].role == Role.USER and messages[-1].content:
            annotated = self._annotate_short_reply(messages[-1].content)
            if annotated:
                messages[-1] = ChatMessage(role=Role.USER, content=annotated)

        system_prompt = self._build_system_prompt(context)
        all_messages = [ChatMessage(role=Role.SYSTEM, content=system_prompt)] + messages

        model_id = self._config.model
        provider, upstream_model = self._resolve_provider(model_id)
        model_name = upstream_model or model_id or ""
        tools = self._build_tools()

        skills_touched: list[str] = []
        total_input = 0
        total_output = 0
        total_tool_calls = 0

        for iteration in range(self._config.max_iterations):
            response: ChatResponse = await self._call_llm(
                provider, all_messages, tools, upstream_model
            )

            total_input += response.usage.input_tokens
            total_output += response.usage.output_tokens

            if response.stop_reason not in (StopReason.TOOL_USE,) or not response.message.tool_calls:
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

        turn = AgentTurn(
            reply="[iteration limit reached]",
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
    ) -> AsyncIterator[StreamEvent]:
        if self._config.on_before_turn:
            messages = self._config.on_before_turn(messages)

        if messages and messages[-1].role == Role.USER and messages[-1].content:
            annotated = self._annotate_short_reply(messages[-1].content)
            if annotated:
                messages[-1] = ChatMessage(role=Role.USER, content=annotated)

        system_prompt = self._build_system_prompt(context)
        all_messages = [ChatMessage(role=Role.SYSTEM, content=system_prompt)] + messages

        model_id = self._config.model
        provider, upstream_model = self._resolve_provider(model_id)
        model_name = upstream_model or model_id or ""
        tools = self._build_tools()

        skills_touched: list[str] = []
        total_input = 0
        total_output = 0
        total_tool_calls = 0

        for iteration in range(self._config.max_iterations):
            stream: AsyncIterator[StreamEvent] = await self._call_llm_stream(
                provider, all_messages, tools, upstream_model
            )

            content_parts: list[str] = []
            tool_call_parts: dict[int, dict[str, Any]] = {}
            stop_reason: StopReason = StopReason.UNKNOWN
            stream_error: str | None = None
            has_forwarded = False

            async for event in stream:
                if isinstance(event, ContentDeltaEvent):
                    if not has_forwarded:
                        has_forwarded = True
                    content_parts.append(event.delta)
                    yield ContentDeltaEvent(delta=event.delta)
                elif isinstance(event, StreamEvent):
                    if event.type == "tool_call_delta":
                        idx = event.index
                        if idx not in tool_call_parts:
                            tool_call_parts[idx] = {"id": event.id, "name": event.name, "arguments": ""}
                        if event.id:
                            tool_call_parts[idx]["id"] = event.id
                        if event.name:
                            tool_call_parts[idx]["name"] = event.name
                        if hasattr(event, "arguments_delta") and event.arguments_delta:
                            tool_call_parts[idx]["arguments"] += event.arguments_delta
                        yield event
                    elif event.type == "usage":
                        total_input += event.usage.input_tokens
                        total_output += event.usage.output_tokens
                    elif event.type == "stop":
                        stop_reason = event.stop_reason
                        if hasattr(event, "model") and event.model:
                            model_name = event.model

            if stop_reason not in (StopReason.TOOL_USE,) or not tool_call_parts:
                reply = "".join(content_parts)
                self._pending_question = self._extract_pending_question(reply)

                yield ContentDeltaEvent(delta="")

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

            all_messages.append(ChatMessage(
                role=Role.ASSISTANT,
                content="".join(content_parts) or None,
                tool_calls=assembled_tcs,
            ))

            for tc in assembled_tcs:
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

                all_messages.append(ChatMessage(
                    role=Role.TOOL,
                    content=result_text,
                    tool_call_id=tc.id,
                    name=tc.name,
                ))

        yield ContentDeltaEvent(delta="[iteration limit reached]")
