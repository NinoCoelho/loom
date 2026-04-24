# RFC 0004 — SSE event shape: convergence toward a richer default

- **Status**: Partially implemented — updated to reflect current state
- **Author**: —
- **Target release**: v0.4
- **Depends on**: cross-project alignment (loom, helyx, nexus)
- **Breaking risk**: LOW — all changes are additive

## Summary

Loom's `StreamEvent` taxonomy has grown significantly since this RFC was first drafted. Most of the events helyx needs now exist in loom, and the `serialize_event` hook lets consumers customise SSE output. However, two gaps remain:

1. **The `/chat/stream` SSE route is a skeleton** — it strips all event fields and only emits `{"type": "<name>"}`, making the richer events invisible to HTTP consumers.
2. **No `session_id` propagation** — events don't carry session context.

This RFC is updated to reflect what's been implemented, what's still missing, and what's obsolete.

---

## Current loom event taxonomy (v0.3)

| Event type | Fields | Status |
|---|---|---|
| `content_delta` | `delta: str` | ✅ Stable |
| `tool_call_delta` | `index, id?, name?, arguments_delta?` | ✅ Stable |
| `usage` | `usage: {input_tokens, output_tokens, cache_read_tokens, cache_write_tokens}` | ✅ Stable |
| `stop` | `stop_reason` | ✅ Stable |
| `tool_exec_start` | `tool_call_id, name, arguments` | ✅ Since v0.3 — replaces the old "piecewise delta only" gap |
| `tool_exec_result` | `tool_call_id, name, text, is_error` | ✅ Since v0.3 — replaces the old "no tool result in stream" gap |
| `limit_reached` | `iterations` | ✅ Since v0.3 |
| `error` | `message, reason?, status_code?, retryable` | ✅ Since v0.3 |
| `done` | `context: dict` (carries `model, iterations, input_tokens, output_tokens, tool_calls, messages, limit_reached?`) | ✅ Since v0.3 |

Additionally, `AgentConfig.serialize_event` lets consumers wire a custom serializer to transform loom's Pydantic events into any shape before they leave the agent loop.

---

## Gap analysis: loom events → what consumers actually need

### Helyx

Helyx's `Chat.tsx` handles these SSE event types:

| Helyx event | Loom equivalent | Gap? |
|---|---|---|
| `{ type: "session", session_id }` | **No equivalent** | ❌ Loom never emits a session anchor event |
| `{ type: "delta", text }` | `ContentDeltaEvent(delta=...)` | ⚠️ Field name mismatch: loom uses `delta`, helyx reads `text` |
| `{ type: "tool_call", name }` | `ToolExecStartEvent(name=...)` | ⚠️ Field name mismatch: loom uses `tool_exec_start` |
| `{ type: "done", session_id, iterations, activated_skills }` | `DoneEvent(context={...})` | ⚠️ `session_id` not in event; `activated_skills` not in context |
| `{ type: "error", error }` | `ErrorEvent(message=...)` | ⚠️ Field name mismatch: loom uses `message`, helyx reads `error` |

Note: Helyx's own agent loop (`helyx/agent`) translates loom events into helyx's shape before they reach the SSE stream. The gaps above exist in that translation layer.

### Nexus

Nexus's `_loom_bridge.py` consumes these loom types directly:

| Loom type | How nexus uses it | Gap? |
|---|---|---|
| `ContentDeltaEvent` | `ev.get("text")` — expects a `text` field | ⚠️ Nexus reads `text` but loom's field is `delta` |
| `ToolCallDeltaEvent` | Assembles tool calls from index-keyed deltas | ✅ Works — nexus handles the piecewise assembly |
| `UsageEvent` | Translates to `lt.Usage` | ✅ Works |
| `StopEvent` | Maps `stop_reason` to loom's `StopReason` | ✅ Works |

Note: Nexus wraps its own providers to satisfy loom's `LLMProvider` interface — it translates *into* loom events, not *from* them. The `ContentDeltaEvent` field mismatch is handled by the Nexus provider adapter, not by loom.

---

## What's been implemented (was proposed in original RFC)

The original RFC proposed "Direction A — additive enrichment." Most of it has landed:

| Original proposal | Current status |
|---|---|
| New `ToolCallStartedEvent` (full spec) | ✅ `ToolExecStartEvent` — emitted before each tool dispatch |
| New `ToolResultEvent` | ✅ `ToolExecResultEvent` — emitted after each tool execution |
| New `TurnCompleteEvent` with `session_id, stop_reason, usage` | ⚠️ `DoneEvent` serves this role but uses a freeform `context` dict instead of typed fields |
| `ContentDeltaEvent` gains optional `session_id`, `turn_id` | ❌ Not implemented |
| `serialize_event` hook for custom SSE shapes | ✅ `AgentConfig.serialize_event` — reduces need for translation layers |

Events added that weren't in the original RFC:
- `ErrorEvent` — structured error with `reason`, `status_code`, `retryable`
- `LimitReachedEvent` — emitted when iteration cap is hit
- `DoneEvent` — terminal marker with `context` dict

---

## What's still missing

### 1. The SSE route is a skeleton

`/chat/stream` in `routes/chat.py` yields `{"type": event.type}` — it strips all event fields. No consumer can use `tool_exec_start`, `tool_exec_result`, `done.context`, or any other enriched payload over SSE.

**What needs to happen:** The route should serialise the full event (or a curated subset) as SSE `data:` lines. The `serialize_event` hook could serve this role if wired through the server.

### 2. No `session_id` in the event stream

The session ID lives in the route handler but is never injected into events. Every downstream consumer (helyx, any future web UI) needs it to correlate events with the active session.

**What needs to happen:** Either:
- (a) Inject `session_id` into every event via the `serialize_event` hook at the server layer, or
- (b) Add an optional `session_id: str | None` field to `DoneEvent` and emit a `{ type: "session", session_id }` anchor at the start of the stream.

### 3. `DoneEvent` uses a freeform dict

`DoneEvent.context` is `dict`. Consumers can't rely on specific keys existing. This is fragile for cross-project use.

**What needs to happen:** Promote the common keys (`model`, `iterations`, `input_tokens`, `output_tokens`, `tool_calls`) to typed fields on `DoneEvent`, keeping `context` as an escape hatch for extra metadata.

---

## What's obsolete in this RFC

1. **"Tool call emitted piecewise via ToolCallDeltaEvent"** — `ToolExecStartEvent` now provides a full-spec event before the per-token deltas. The piecewise-assembly concern is resolved for consumers that listen to `tool_exec_start`.

2. **"~80 LOC translation layer"** — The `serialize_event` hook in `AgentConfig` lets consumers wire custom serialisation directly into the agent loop, significantly reducing translation code. Helyx still has a translation layer because the SSE route doesn't emit full events yet.

3. **"Direction B — versioned shape"** and **"Direction C — full convergence"** — Direction A (additive enrichment) has been the de facto approach and is working. No need for versioned endpoints or breaking changes.

4. **The "Questions for the group" and "Decision gate" sections** — Most questions are resolved by the current additive approach. The remaining questions are concrete implementation tasks, not design decisions.

---

## Remaining action items

- [ ] **SSE route**: Serialise full event payloads in `/chat/stream` (not just `{"type": ...}`).
- [ ] **Session anchor**: Emit a `session` event at the start of every stream, or inject `session_id` into `DoneEvent`.
- [ ] **`DoneEvent` typed fields**: Promote common context keys to typed Pydantic fields.
- [ ] **Field naming audit**: Document the `delta` vs `text`, `message` vs `error`, `tool_exec_start` vs `tool_call` mismatches between loom and helyx. Decide if loom adds aliases or if the translation layer stays.
- [ ] Update this RFC status to "implemented" once the SSE route ships.
