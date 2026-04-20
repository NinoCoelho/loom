"""Tests for the session/HITL broker and its ask_user tool."""

from __future__ import annotations

import asyncio

import pytest

from loom.hitl import (
    BrokerAskUserTool,
    CURRENT_SESSION_ID,
    HitlBroker,
    HitlEvent,
    TIMEOUT_SENTINEL,
)


# ── Broker mechanics ────────────────────────────────────────────────────


class TestHitlBroker:
    async def test_ask_resolves_via_resolve(self) -> None:
        broker = HitlBroker()
        sub = broker.subscribe("s1")

        async def answer_soon() -> None:
            ev = await sub.get()
            assert ev.kind == "user_request"
            rid = ev.data["request_id"]
            broker.resolve("s1", rid, "yes")

        answerer = asyncio.create_task(answer_soon())
        result = await broker.ask("s1", "proceed?", timeout_seconds=2)
        await answerer
        assert result == "yes"

    async def test_ask_timeout_returns_sentinel(self) -> None:
        broker = HitlBroker()
        sub = broker.subscribe("s1")

        result = await broker.ask("s1", "?", timeout_seconds=1)
        assert result == TIMEOUT_SENTINEL

        events: list[HitlEvent] = []
        while not sub.empty():
            events.append(sub.get_nowait())
        kinds = [e.kind for e in events]
        assert "user_request" in kinds
        assert "user_request_cancelled" in kinds
        cancel = next(e for e in events if e.kind == "user_request_cancelled")
        assert cancel.data["reason"] == "timeout"

    async def test_yolo_short_circuits_confirm(self) -> None:
        broker = HitlBroker()
        sub = broker.subscribe("s1")
        result = await broker.ask("s1", "ok?", kind="confirm", yolo=True)
        assert result == "yes"
        ev = sub.get_nowait()
        assert ev.kind == "user_request_auto"
        assert ev.data["answer"] == "yes"

    async def test_cancel_session_clears_pending(self) -> None:
        broker = HitlBroker()
        task = asyncio.create_task(broker.ask("s1", "?", timeout_seconds=60))
        # Let ask() register the future.
        await asyncio.sleep(0)
        assert len(broker.pending("s1")) == 1
        n = broker.cancel_session("s1")
        assert n == 1
        result = await task
        assert result == TIMEOUT_SENTINEL
        assert broker.pending("s1") == []

    async def test_resolve_unknown_returns_false(self) -> None:
        broker = HitlBroker()
        assert broker.resolve("s1", "nope", "x") is False


# ── BrokerAskUserTool ───────────────────────────────────────────────────


class TestBrokerAskUserTool:
    def test_tool_spec(self) -> None:
        tool = BrokerAskUserTool(HitlBroker())
        spec = tool.tool
        assert spec.name == "ask_user"
        assert spec.parameters["required"] == ["prompt"]

    async def test_rejects_missing_session_context(self) -> None:
        tool = BrokerAskUserTool(HitlBroker())
        CURRENT_SESSION_ID.set(None)
        result = await tool.invoke({"prompt": "hi"})
        assert result.is_error is True
        assert "session" in result.text.lower()

    async def test_roundtrip_with_session_context(self) -> None:
        broker = HitlBroker()
        tool = BrokerAskUserTool(broker)
        sub = broker.subscribe("s-roundtrip")
        CURRENT_SESSION_ID.set("s-roundtrip")

        async def answer() -> None:
            ev = await sub.get()
            broker.resolve("s-roundtrip", ev.data["request_id"], "pick-me")

        answerer = asyncio.create_task(answer())
        result = await tool.invoke(
            {
                "prompt": "choose",
                "kind": "choice",
                "choices": ["a", "b"],
                "timeout_seconds": 2,
            }
        )
        await answerer
        assert result.is_error is False
        assert result.text == "pick-me"

    async def test_invalid_choice_args(self) -> None:
        tool = BrokerAskUserTool(HitlBroker())
        CURRENT_SESSION_ID.set("s1")
        r1 = await tool.invoke({"prompt": "pick", "kind": "choice"})
        assert r1.is_error is True
        r2 = await tool.invoke(
            {"prompt": "pick", "kind": "choice", "choices": [1, 2]}
        )
        assert r2.is_error is True

    async def test_yolo_getter_applied(self) -> None:
        broker = HitlBroker()
        tool = BrokerAskUserTool(broker, yolo_getter=lambda: True)
        CURRENT_SESSION_ID.set("s-yolo")
        result = await tool.invoke({"prompt": "ok?"})
        assert result.text == "yes"
