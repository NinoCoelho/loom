from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from loom.errors import LLMError, LLMTransportError, MalformedOutputError
from loom.llm.base import LLMProvider
from loom.types import (
    ChatMessage,
    ChatResponse,
    ContentDeltaEvent,
    Role,
    StopEvent,
    StopReason,
    StreamEvent,
    ToolCall,
    ToolCallDeltaEvent,
    ToolSpec,
    Usage,
    UsageEvent,
)

_ANTHROPIC_STOP_MAP: dict[str, StopReason] = {
    "end_turn": StopReason.STOP,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.LENGTH,
    "stop_sequence": StopReason.STOP,
}


def _map_anthropic_stop(raw: str | None) -> StopReason:
    if raw is None:
        return StopReason.UNKNOWN
    return _ANTHROPIC_STOP_MAP.get(raw, StopReason.UNKNOWN)


class AnthropicProvider(LLMProvider):
    def __init__(
        self,
        api_key: str,
        default_model: str = "claude-sonnet-4-20250514",
        timeout: float = 120.0,
    ) -> None:
        try:
            import anthropic
        except ImportError:
            raise ImportError(
                "The 'anthropic' package is required for AnthropicProvider. "
                "Install it with: pip install anthropic"
            )
        self._anthropic = anthropic
        self.default_model = default_model
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            timeout=timeout,
        )

    def _extract_system(self, messages: list[ChatMessage]) -> str | None:
        parts: list[str] = []
        for msg in messages:
            if msg.role == Role.SYSTEM and msg.content:
                parts.append(msg.content)
        return "\n\n".join(parts) if parts else None

    def _convert_messages(self, messages: list[ChatMessage]) -> list[dict]:
        anthropic_msgs: list[dict] = []
        tool_result_buffer: list[dict] = []

        def flush_tool_buffer() -> None:
            nonlocal tool_result_buffer
            if tool_result_buffer:
                anthropic_msgs.append({"role": "user", "content": tool_result_buffer})
                tool_result_buffer = []

        for msg in messages:
            if msg.role == Role.SYSTEM:
                flush_tool_buffer()
                continue

            if msg.role == Role.TOOL:
                tool_result_buffer.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": msg.tool_call_id or "",
                        "content": msg.content or "",
                    }
                )
                continue

            flush_tool_buffer()

            if msg.role == Role.ASSISTANT:
                content: list[dict] = []
                if msg.content:
                    content.append({"type": "text", "text": msg.content})
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        try:
                            inp = json.loads(tc.arguments) if tc.arguments else {}
                        except json.JSONDecodeError:
                            inp = {}
                        content.append(
                            {
                                "type": "tool_use",
                                "id": tc.id,
                                "name": tc.name,
                                "input": inp,
                            }
                        )
                anthropic_msgs.append({"role": "assistant", "content": content or ""})
            elif msg.role == Role.USER:
                anthropic_msgs.append({"role": "user", "content": msg.content or ""})

        flush_tool_buffer()
        return anthropic_msgs

    def _convert_tools(self, tools: list[ToolSpec]) -> list[dict]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters,
            }
            for t in tools
        ]

    def _parse_response(self, response: Any) -> ChatResponse:
        content_text: str | None = None
        tool_calls: list[ToolCall] = []

        for block in response.content:
            if block.type == "text":
                content_text = block.text
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=json.dumps(block.input),
                    )
                )

        return ChatResponse(
            message=ChatMessage(
                role=Role.ASSISTANT,
                content=content_text,
                tool_calls=tool_calls or None,
            ),
            usage=Usage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            ),
            stop_reason=_map_anthropic_stop(response.stop_reason),
            model=response.model,
        )

    def _build_kwargs(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None,
        model: str | None,
    ) -> dict:
        system = self._extract_system(messages)
        anthropic_msgs = self._convert_messages(messages)
        kwargs: dict = {
            "model": model or self.default_model,
            "max_tokens": 4096,
            "messages": anthropic_msgs,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = self._convert_tools(tools)
        return kwargs

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        model: str | None = None,
    ) -> ChatResponse:
        kwargs = self._build_kwargs(messages, tools, model)
        try:
            response = await self._client.messages.create(**kwargs)
        except self._anthropic.APIConnectionError as exc:
            raise LLMTransportError(f"Connection error: {exc}") from exc
        except self._anthropic.APIStatusError as exc:
            raise LLMTransportError(
                f"HTTP {exc.status_code}: {exc.message}",
                status_code=exc.status_code,
                body=str(exc.body),
            ) from exc
        except self._anthropic.BadRequestError as exc:
            raise MalformedOutputError(f"Bad request: {exc}") from exc
        except Exception as exc:
            raise LLMError(f"Unexpected error: {exc}") from exc
        return self._parse_response(response)

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        kwargs = self._build_kwargs(messages, tools, model)
        try:
            async with self._client.messages.stream(**kwargs) as stream:
                tool_index = 0
                input_tokens = 0
                async for event in stream:
                    if event.type == "message_start":
                        input_tokens = event.message.usage.input_tokens

                    elif event.type == "content_block_start":
                        cb = event.content_block
                        if cb.type == "tool_use":
                            yield ToolCallDeltaEvent(
                                index=tool_index,
                                id=cb.id,
                                name=cb.name,
                            )
                            tool_index += 1

                    elif event.type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            yield ContentDeltaEvent(delta=delta.text)
                        elif delta.type == "input_json_delta":
                            yield ToolCallDeltaEvent(
                                index=tool_index - 1,
                                arguments_delta=delta.partial_json,
                            )

                    elif event.type == "message_delta":
                        usage = event.usage
                        if usage:
                            yield UsageEvent(
                                usage=Usage(
                                    input_tokens=input_tokens,
                                    output_tokens=usage.output_tokens,
                                )
                            )
                        stop = event.delta.stop_reason if event.delta else None
                        if stop:
                            yield StopEvent(stop_reason=_map_anthropic_stop(stop))

        except self._anthropic.APIConnectionError as exc:
            raise LLMTransportError(f"Connection error: {exc}") from exc
        except self._anthropic.APIStatusError as exc:
            raise LLMTransportError(
                f"HTTP {exc.status_code}: {exc.message}",
                status_code=exc.status_code,
                body=str(exc.body),
            ) from exc
        except LLMTransportError:
            raise
        except Exception as exc:
            raise LLMError(f"Unexpected streaming error: {exc}") from exc

    async def close(self) -> None:
        await self._client.close()

    async def __aenter__(self) -> AnthropicProvider:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
