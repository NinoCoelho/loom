"""Tests for ``loom.tools.subagent`` — fan-out tool spec, validation,
and depth/runner contract."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from loom.context import CURRENT_SESSION_ID, SUBAGENT_DEPTH
from loom.tools.subagent import (
    MAX_SUBAGENT_DEPTH,
    MAX_TASKS_PER_CALL,
    SPAWN_SUBAGENTS_TOOL_SPEC,
    SpawnSubagentsTool,
    handle_spawn_subagents,
)


def _fake_runner_factory(answers: list[str]):
    """Returns a runner that returns one canned answer per task."""
    seen: list[dict[str, Any]] = []

    async def runner(tasks, *, parent_session_id, depth):
        seen.append({"parent": parent_session_id, "depth": depth, "tasks": list(tasks)})
        return [
            {"session_id": f"child-{i}", "result": answers[i], "error": None}
            for i in range(len(tasks))
        ]

    runner.seen = seen  # type: ignore[attr-defined]
    return runner


# ── spec ────────────────────────────────────────────────────────────


class TestSpec:
    def test_name_and_required(self) -> None:
        assert SPAWN_SUBAGENTS_TOOL_SPEC.name == "spawn_subagents"
        assert SPAWN_SUBAGENTS_TOOL_SPEC.parameters["required"] == ["tasks"]

    def test_caps_in_description(self) -> None:
        assert str(MAX_TASKS_PER_CALL) in SPAWN_SUBAGENTS_TOOL_SPEC.description


# ── handler validation ──────────────────────────────────────────────


class TestHandlerValidation:
    async def test_runner_none_returns_error(self) -> None:
        text = await handle_spawn_subagents(
            {"tasks": [{"prompt": "x"}]},
            runner=None,
            parent_session_id="s1",
            depth=0,
        )
        body = json.loads(text)
        assert body["ok"] is False
        assert "runner not wired" in body["error"]

    async def test_no_session_returns_error(self) -> None:
        runner = _fake_runner_factory(["a"])
        text = await handle_spawn_subagents(
            {"tasks": [{"prompt": "x"}]},
            runner=runner,
            parent_session_id=None,
            depth=0,
        )
        body = json.loads(text)
        assert body["ok"] is False
        assert "active session" in body["error"]

    async def test_depth_cap_enforced(self) -> None:
        runner = _fake_runner_factory(["a"])
        text = await handle_spawn_subagents(
            {"tasks": [{"prompt": "x"}]},
            runner=runner,
            parent_session_id="s1",
            depth=MAX_SUBAGENT_DEPTH,
        )
        body = json.loads(text)
        assert body["ok"] is False
        assert "depth limit reached" in body["error"]

    async def test_empty_tasks(self) -> None:
        runner = _fake_runner_factory([])
        text = await handle_spawn_subagents(
            {"tasks": []},
            runner=runner,
            parent_session_id="s1",
            depth=0,
        )
        body = json.loads(text)
        assert body["ok"] is False

    async def test_too_many_tasks(self) -> None:
        runner = _fake_runner_factory(["a"] * 100)
        text = await handle_spawn_subagents(
            {"tasks": [{"prompt": str(i)} for i in range(MAX_TASKS_PER_CALL + 1)]},
            runner=runner,
            parent_session_id="s1",
            depth=0,
        )
        body = json.loads(text)
        assert body["ok"] is False
        assert "max" in body["error"]

    async def test_task_with_blank_prompt(self) -> None:
        runner = _fake_runner_factory(["a"])
        text = await handle_spawn_subagents(
            {"tasks": [{"prompt": "   "}]},
            runner=runner,
            parent_session_id="s1",
            depth=0,
        )
        body = json.loads(text)
        assert body["ok"] is False
        assert "task[0]" in body["error"]


# ── successful dispatch ─────────────────────────────────────────────


class TestHandlerSuccess:
    async def test_returns_results_in_task_order(self) -> None:
        runner = _fake_runner_factory(["alpha", "beta"])
        text = await handle_spawn_subagents(
            {
                "tasks": [
                    {"name": "first", "prompt": "1"},
                    {"name": "second", "prompt": "2"},
                ]
            },
            runner=runner,
            parent_session_id="s1",
            depth=0,
        )
        body = json.loads(text)
        assert body["ok"] is True
        assert [r["result"] for r in body["results"]] == ["alpha", "beta"]
        assert [r["name"] for r in body["results"]] == ["first", "second"]
        assert [r["session_id"] for r in body["results"]] == ["child-0", "child-1"]

    async def test_runner_receives_parent_session_and_depth(self) -> None:
        runner = _fake_runner_factory(["a"])
        await handle_spawn_subagents(
            {"tasks": [{"prompt": "x"}]},
            runner=runner,
            parent_session_id="parent-xyz",
            depth=0,
        )
        assert runner.seen[0]["parent"] == "parent-xyz"
        assert runner.seen[0]["depth"] == 0


# ── tool wrapper ────────────────────────────────────────────────────


class TestSpawnSubagentsTool:
    def test_requires_runner_or_getter(self) -> None:
        try:
            SpawnSubagentsTool()
        except ValueError as exc:
            assert "runner" in str(exc)
        else:
            raise AssertionError("expected ValueError")

    async def test_reads_contextvars_at_dispatch(self) -> None:
        runner = _fake_runner_factory(["resp"])
        tool = SpawnSubagentsTool(runner)

        token_sid = CURRENT_SESSION_ID.set("sess-A")
        token_dep = SUBAGENT_DEPTH.set(0)
        try:
            result = await tool.invoke({"tasks": [{"prompt": "ping"}]})
        finally:
            SUBAGENT_DEPTH.reset(token_dep)
            CURRENT_SESSION_ID.reset(token_sid)

        body = json.loads(result.text)
        assert body["ok"] is True
        assert runner.seen[0]["parent"] == "sess-A"
        assert runner.seen[0]["depth"] == 0

    async def test_runner_getter_late_binding(self) -> None:
        captured: dict[str, Any] = {"runner": None}

        def getter():
            return captured["runner"]

        tool = SpawnSubagentsTool(runner_getter=getter)
        token = CURRENT_SESSION_ID.set("s1")
        try:
            # First call: getter returns None → runner_not_wired error.
            text = (await tool.invoke({"tasks": [{"prompt": "x"}]})).text
            assert json.loads(text)["ok"] is False
            # Late-bind a runner; next call works.
            captured["runner"] = _fake_runner_factory(["ok"])
            text = (await tool.invoke({"tasks": [{"prompt": "x"}]})).text
            assert json.loads(text)["ok"] is True
        finally:
            CURRENT_SESSION_ID.reset(token)


# ── contextvar copy across asyncio tasks ────────────────────────────


class TestContextVarPropagation:
    async def test_subagent_depth_copies_into_gathered_tasks(self) -> None:
        """Property the runner relies on: setting SUBAGENT_DEPTH inside a
        ``create_task`` body is visible to that task only — siblings keep
        their own copy. Locks the contract so nexus's runner can rely on
        per-child depth without explicit plumbing."""
        SUBAGENT_DEPTH.set(0)

        async def child_set(value: int) -> int:
            SUBAGENT_DEPTH.set(value)
            await asyncio.sleep(0)
            return SUBAGENT_DEPTH.get()

        results = await asyncio.gather(child_set(1), child_set(2), child_set(3))
        assert sorted(results) == [1, 2, 3]
        # Parent unaffected
        assert SUBAGENT_DEPTH.get() == 0
