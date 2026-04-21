# RFC 0004 — SSE event shape: convergence toward a richer default

- **Status**: Discussion (not implementation-ready)
- **Author**: —
- **Target release**: TBD (v0.4 at earliest)
- **Depends on**: cross-project alignment (loom, helyx, nexus)
- **Breaking risk**: HIGH — affects every loom streaming consumer

## Summary

Today's `loom.types.StreamEvent` subclasses emit a minimal shape: `{type: <event_name>}` with a handful of per-event fields. Consumers like helyx need richer metadata (session IDs, tool names carried through the stream, delta kinds, run completion markers) and end up translating loom events into their own shape.

This RFC proposes a conversation — **not yet a concrete design** — about converging on a richer event shape so consumers can feed loom's SSE directly to their UI without a translation layer.

Because Nexus reads loom's event types directly, any breaking change requires explicit cross-project alignment before code moves.

## Motivation

### Helyx

`ui/src/components/Chat.tsx` consumes events shaped like:

```ts
type ChatEvent =
  | { type: "session", session_id: string }
  | { type: "delta", text: string }
  | { type: "tool_call", id: string, name: string, args_json: string }
  | { type: "tool_result", id: string, text: string, ok: boolean }
  | { type: "done", session_id: string, stop_reason: string };
```

Loom's current events carry *some* of this but not all — there's no consolidated `done` event with `session_id`, `tool_call` is emitted piecewise via `ToolCallDeltaEvent` (token-at-a-time), and `tool_result` isn't part of the stream in a single event.

Helyx emits its own shape in `server/app.py`, translating loom's events under the hood. ~80 LOC that would be unnecessary if loom's event shape were richer.

### Nexus

Reads `loom.ContentDeltaEvent` and `loom.ToolCallDeltaEvent` directly in `_loom_bridge.py`. Any rename/reshape breaks nexus.

### Future consumers

Without convergence, every new consumer either (a) translates loom's events to their own, or (b) inherits loom's minimal shape and builds a UI around it. (a) means duplicated translation code; (b) means a thinner UX than the ecosystem can support.

## Constraints

1. **No silent breaking changes.** Nexus is in production-ish use. If we rename `delta` to `content_delta`, Nexus breaks.
2. **Additive is safe.** Adding optional fields to existing events and adding new event subclasses doesn't break anyone.
3. **Consumers should be able to opt in** to richer events via a flag or an alternate endpoint, so migration is per-consumer.

## Possible directions

### Direction A — Additive enrichment
Add optional fields to existing events and define new events alongside the current ones.

- `ContentDeltaEvent` gains optional `session_id`, `turn_id`.
- New `ToolCallStartedEvent` (full spec, not a delta) emitted once per tool call before the per-token deltas.
- New `ToolResultEvent` (full shape) emitted after tool execution; distinct from `ToolExecResultEvent` (which might be the same thing rebranded — TBD).
- New `TurnCompleteEvent` with `{session_id, stop_reason, usage}`.

**Pros**: Zero breaking changes. Consumers opt in by listening to the new events.
**Cons**: Event taxonomy grows; some redundancy with existing deltas.

### Direction B — Versioned shape
Let consumers request a shape version via an accept header or query param: `/chat/stream?event_schema=v2`.

**Pros**: Clean slate at v2 without breaking v1.
**Cons**: Two code paths in loom; maintenance cost.

### Direction C — Full convergence (major version)
Propose a v1.0 loom event shape and migrate all consumers to it. Deprecate the current shape with a transition window.

**Pros**: One canonical shape long-term.
**Cons**: Breaking change; requires coordinated migration across helyx, nexus, any future consumers.

## Questions for the group

1. Is there appetite to standardize, or is the current "each consumer translates" model acceptable long-term?
2. If we standardize, A / B / C — which pattern fits the project's stability goals (loom is alpha; breaking changes are cheap *now*, expensive after v1.0)?
3. What's the minimum set of fields an event consumer needs to render a streaming chat UI correctly? Helyx's shape is one answer. Nexus's implicit shape (from `_loom_bridge.py`) is another. Are they reconcilable into one canonical shape?
4. Should `done` / `turn_complete` carry `usage` (token counts), or should usage stay on its own `UsageEvent`?
5. Should session IDs be part of every event (simple, redundant) or only anchor events (`session_started`, `turn_complete`) (clean, but consumers need state)?

## Decision gate

This RFC blocks on:

- Input from a nexus maintainer on acceptable migration cost.
- A concrete audit of helyx's `Chat.tsx` event consumption versus loom's current event types — to produce a gap list.
- A decision on versioning vs full convergence.

Once those three are in hand, this RFC gets rewritten as a concrete implementation proposal (or abandoned if consensus is "translation layer per consumer is fine").

## Action items

- [ ] Share this RFC with nexus maintainers for reaction.
- [ ] Produce the helyx ↔ loom gap list (what helyx's UI needs that loom doesn't emit).
- [ ] Produce the nexus ↔ loom contact surface (what events nexus reads and how it transforms them).
- [ ] Decide on direction A / B / C.
- [ ] Rewrite this RFC as an implementation proposal.
