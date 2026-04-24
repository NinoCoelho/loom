from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from loom.errors import LLMTransportError, MalformedOutputError
from loom.llm._convert import convert_tools_openai
from loom.llm.base import LLMProvider
from loom.media import encode_to_data_url, infer_media_type
from loom.types import (
    ChatMessage,
    ChatResponse,
    ContentDeltaEvent,
    ContentPart,
    FilePart,
    ImagePart,
    Role,
    StopEvent,
    StopReason,
    StreamEvent,
    TextPart,
    ToolCall,
    ToolCallDeltaEvent,
    ToolSpec,
    Usage,
    UsageEvent,
    VideoPart,
)

logger = logging.getLogger(__name__)

_STOP_REASON_MAP: dict[str, StopReason] = {
    "stop": StopReason.STOP,
    "tool_calls": StopReason.TOOL_USE,
    "length": StopReason.LENGTH,
    "content_filter": StopReason.CONTENT_FILTER,
}


def _map_stop_reason(raw: str | None) -> StopReason:
    if raw is None:
        return StopReason.UNKNOWN
    return _STOP_REASON_MAP.get(raw, StopReason.UNKNOWN)


class OpenAICompatibleProvider(LLMProvider):
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        default_model: str = "gpt-4o",
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model = default_model
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout),
            headers=self._build_headers(),
        )

    def _build_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _convert_content_part(self, part: ContentPart) -> dict[str, Any]:
        if isinstance(part, TextPart):
            return {"type": "text", "text": part.text}
        if isinstance(part, ImagePart):
            mt = part.media_type or infer_media_type(part.source)
            url = encode_to_data_url(part.source, mt)
            return {"type": "image_url", "image_url": {"url": url}}
        if isinstance(part, VideoPart):
            mt = part.media_type or infer_media_type(part.source)
            url = encode_to_data_url(part.source, mt)
            return {"type": "image_url", "image_url": {"url": url}}
        if isinstance(part, FilePart):
            mt = part.media_type or infer_media_type(part.source)
            url = encode_to_data_url(part.source, mt)
            return {"type": "image_url", "image_url": {"url": url}}
        return {"type": "text", "text": str(part)}

    def _convert_message(self, msg: ChatMessage) -> dict[str, Any]:
        d: dict[str, Any] = {"role": msg.role.value}
        if msg.content is not None:
            if isinstance(msg.content, str):
                d["content"] = msg.content
            else:
                d["content"] = [self._convert_content_part(p) for p in msg.content]
        if msg.tool_calls is not None:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": tc.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        if msg.tool_call_id is not None:
            d["tool_call_id"] = msg.tool_call_id
        if msg.name is not None:
            d["name"] = msg.name
        return d

    def _convert_tools(self, tools: list[ToolSpec]) -> list[dict[str, Any]]:
        return convert_tools_openai(tools)

    def _build_payload(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None,
        model: str | None,
        stream: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": [self._convert_message(m) for m in messages],
            "stream": stream,
        }
        if tools:
            payload["tools"] = self._convert_tools(tools)
        return payload

    def _parse_response(self, raw: dict[str, Any]) -> ChatResponse:
        try:
            choice = raw["choices"][0]
            msg = choice["message"]
            finish = choice.get("finish_reason")
        except (KeyError, IndexError) as exc:
            raise MalformedOutputError(f"Missing expected fields in response: {exc}") from exc

        tool_calls: list[ToolCall] | None = None
        if raw_tcs := msg.get("tool_calls"):
            tool_calls = [
                ToolCall(
                    id=tc["id"],
                    name=tc["function"]["name"],
                    arguments=tc["function"]["arguments"],
                )
                for tc in raw_tcs
            ]

        usage_raw = raw.get("usage", {})
        usage = Usage(
            input_tokens=usage_raw.get("prompt_tokens", 0),
            output_tokens=usage_raw.get("completion_tokens", 0),
        )

        return ChatResponse(
            message=ChatMessage(
                role=Role.ASSISTANT,
                content=msg.get("content"),
                tool_calls=tool_calls,
            ),
            usage=usage,
            stop_reason=_map_stop_reason(finish),
            model=raw.get("model", self.default_model),
        )

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        model: str | None = None,
    ) -> ChatResponse:
        payload = self._build_payload(messages, tools, model)
        try:
            resp = await self._client.post("chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise LLMTransportError(f"HTTP request failed: {exc}") from exc

        if resp.status_code != 200:
            raise LLMTransportError(
                f"HTTP {resp.status_code}: {resp.text}",
                status_code=resp.status_code,
                body=resp.text,
            )

        try:
            raw = resp.json()
        except json.JSONDecodeError as exc:
            raise MalformedOutputError(f"Invalid JSON in response: {exc}") from exc

        return self._parse_response(raw)

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        payload = self._build_payload(messages, tools, model, stream=True)
        try:
            async with self._client.stream("POST", "chat/completions", json=payload) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise LLMTransportError(
                        f"HTTP {resp.status_code}: {body.decode()}",
                        status_code=resp.status_code,
                        body=body.decode(),
                    )

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    choices = chunk.get("choices", [])
                    if not choices:
                        if raw_usage := chunk.get("usage"):
                            yield UsageEvent(
                                usage=Usage(
                                    input_tokens=raw_usage.get("prompt_tokens", 0),
                                    output_tokens=raw_usage.get("completion_tokens", 0),
                                )
                            )
                        continue

                    choice = choices[0]
                    delta = choice.get("delta", {})
                    finish = choice.get("finish_reason")

                    if delta.get("content") is not None:
                        yield ContentDeltaEvent(delta=delta["content"])

                    if raw_tcs := delta.get("tool_calls"):
                        for tc in raw_tcs:
                            fn = tc.get("function", {})
                            yield ToolCallDeltaEvent(
                                index=tc["index"],
                                id=tc.get("id"),
                                name=fn.get("name"),
                                arguments_delta=fn.get("arguments"),
                            )

                    if finish:
                        yield StopEvent(stop_reason=_map_stop_reason(finish))

                    if raw_usage := chunk.get("usage"):
                        yield UsageEvent(
                            usage=Usage(
                                input_tokens=raw_usage.get("prompt_tokens", 0),
                                output_tokens=raw_usage.get("completion_tokens", 0),
                            )
                        )
        except LLMTransportError:
            raise
        except httpx.HTTPError as exc:
            raise LLMTransportError(f"Streaming HTTP error: {exc}") from exc

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> OpenAICompatibleProvider:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
