# RFC 0001 — `HttpCallTool` pre-request hook

- **Status**: Draft
- **Author**: —
- **Target release**: 0.3
- **Depends on**: none
- **Blocks**: RFC 0002 (can land independently but becomes the canonical example of an applier-fed tool)

## Summary

Add an optional `pre_request_hook` parameter to `loom.tools.http.HttpCallTool` so consumers can inject URL rewriting, header resolution, or credential application **before** the HTTP request is sent, without forking the tool.

## Motivation

Today, `HttpCallTool` takes a raw URL and optional headers. Consumers who need to:

- Resolve a logical target name (e.g. `prod-oic-us-east`) to a real URL
- Fetch and attach credentials from a vault
- Perform OAuth2 token exchange per-request
- Rewrite URLs for a proxy or local dev override

…have to fork the tool. Helyx does this today — it has a ~270 LOC `HttpCallHandler` that duplicates loom's transport code just to add a credential-resolution step on top.

A pre-request hook keeps the generic tool in loom and lets each consumer layer its own resolution logic without copying.

## Non-goals

- Not introducing full middleware chains.
- Not bundling credential resolution into this RFC. Appliers (RFC 0002) plug into this hook but are a separate concern.
- Not changing response handling — shape of `ToolResult` stays the same.

## Design

### API change

```python
from typing import Awaitable, Callable

PreRequestHook = Callable[[dict], Awaitable[dict]]
# Input: {method, url, headers, body} (same keys as invoke() args)
# Output: modified copy of the same shape. Must return a fresh dict.

class HttpCallTool(ToolHandler):
    def __init__(
        self,
        base_headers: dict | None = None,
        timeout: float = 30.0,
        max_response_bytes: int = 10240,
        pre_request_hook: PreRequestHook | None = None,  # NEW
    ) -> None:
        ...
```

### Semantics

- If `pre_request_hook` is `None` (default), behavior is identical to today.
- If set, the hook runs after argument parsing and before `httpx.AsyncClient` dispatches the request.
- The hook receives a dict containing `{method, url, headers, body}` — the effective request about to be sent.
- The hook returns a dict of the same shape. Any field it returns replaces the corresponding value; fields it omits are preserved.
- The hook is async. Errors raised inside it propagate out of `invoke()` as a `ToolResult(text="HTTP error: <hook error>")` — same error envelope as transport errors.

### Example: credential injection

```python
async def add_bearer(req: dict) -> dict:
    req["headers"] = {**req["headers"], "Authorization": f"Bearer {token}"}
    return req

tool = HttpCallTool(pre_request_hook=add_bearer)
```

### Example: target→URL resolution (helyx shape)

```python
async def resolve_target(req: dict) -> dict:
    # req["url"] looks like "target://prod-oic-us-east/ic/api/integration/v1"
    target, path = parse_target_url(req["url"])
    base = targets.get(target).base_url
    headers = await cred_resolver.headers_for(target)
    return {**req, "url": base + path, "headers": {**req["headers"], **headers}}
```

## Backward compatibility

Additive only. Consumers who don't set `pre_request_hook` see no behavior change.

Nexus does not use `HttpCallTool` today — verified. Any future consumer using the current signature is unaffected.

## Alternatives considered

1. **Middleware chain** (list of hooks) — richer but overdesigned for the one use case on the table. Easy to upgrade to later if needed.
2. **Subclass-and-override** — works but requires consumers to inherit; hook-based composition keeps `HttpCallTool` usable as-is.
3. **Pre- and post-request hooks** — post-hooks are useful (redaction, metrics) but belong in a separate RFC. Keep this one small.

## Test plan

- Unit test: default behavior unchanged (existing tests still pass).
- Unit test: hook mutates URL, assert request goes to mutated URL.
- Unit test: hook adds a header, assert it appears on the wire.
- Unit test: hook raises, assert `ToolResult` error envelope carries the message.
- Unit test: hook returns a fresh dict without mutating the input (contract check).

## Open questions

- Should the hook be able to **cancel** a request (e.g. policy check fails)? Current proposal: raise an exception. Alternative: a sentinel return value. Leaning toward raising — fewer surface area, same effect.
- Should `base_headers` be merged *before* or *after* the hook? Proposal: before (hook sees the final header set and can override).
