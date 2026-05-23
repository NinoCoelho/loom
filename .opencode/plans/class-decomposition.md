# Refactoring Plan: Class Decomposition

> **Status**: Planning — not yet approved for implementation.
> Each refactoring is independent and can be done in any order unless noted.

---

## Refactoring 1: Agent → Agent + TurnExecutor + TurnState

**File**: `src/loom/loop.py` (1004 lines)
**Risk**: High (core hot path, 12 consumers)
**Principle**: Extract the duplicated turn logic into a shared `TurnExecutor` and make per-turn state explicit.

### 1.1 Create `TurnState` dataclass

**New file**: `src/loom/loop/_turn.py`

```
@dataclass
class TurnState:
    pending_question: str | None
    skills_touched: list[str]
    last_tc_signature: str | None
    identical_tc_streak: int
    total_input: int
    total_output: int
    total_tool_calls: int
```

This replaces the mutable `self._pending_question` on `Agent` and the local variables re-declared in both `run_turn` and `run_turn_stream`:
- `skills_touched` (loop.py:534, 709)
- `_last_tc_signature` (loop.py:538, 718)
- `_identical_tc_streak` (loop.py:539, 719)
- `_IDENTICAL_TC_LIMIT = 3` (loop.py:542, 722) → class constant on `TurnState`
- `total_input/output/tool_calls` (loop.py:535-537, 710-712)

### 1.2 Create `TurnExecutor` class

**New file**: `src/loom/loop/_executor.py`

Holds all the logic that is currently duplicated between `run_turn` and `run_turn_stream`:

| Method | Extracted from | Lines (blocking / streaming) |
|--------|---------------|------------------------------|
| `prepare(messages, context, model_id) -> (all_messages, system_msg, provider, model, tools, ctx_window)` | `_prepare_turn` | 365-403 (shared) |
| `check_overflow(messages, ctx_window) -> OverflowCheck | None` | overflow guard block | 566-596 / 742-784 |
| `apply_hook(messages, tools) -> list | None` | before-hook block | 547-561 / 727-736 |
| `handle_tool_calls(response, state, all_messages) -> None` | tool dispatch + stuck-loop detection | 636-671 / 924-981 |
| `build_final_turn(reply, state, messages) -> AgentTurn` | final assembly | 605-627 / 868-901 |
| `build_stuck_reply(state) -> AgentTurn` | stuck-loop abort | 645-671 / 949-981 |
| `build_limit_reply(iteration, max) -> AgentTurn` | limit reached | 673-690 / 983-1004 |

The executor receives references to `Agent`'s dependencies (provider, tools, skills, config, etc.) via constructor injection, NOT by holding a reference to `Agent` itself.

### 1.3 Refactor `run_turn` and `run_turn_stream`

Both methods become thin wrappers:

```python
def run_turn(self, messages, context=None, model_id=None) -> AgentTurn:
    state = TurnState(pending_question=self._pending_question, ...)
    executor = TurnExecutor(self._deps)
    for event in executor.execute(messages, context, model_id, state):
        if isinstance(event, AgentTurn):
            self._pending_question = state.pending_question
            return event
    ...

async def run_turn_stream(self, messages, context=None, model_id=None) -> AsyncIterator:
    state = TurnState(pending_question=self._pending_question, ...)
    executor = TurnExecutor(self._deps)
    async for event in executor.execute_stream(messages, context, model_id, state):
        yield _wrap(event)
        if isinstance(event, (DoneEvent, LimitReachedEvent)):
            self._pending_question = state.pending_question
```

### 1.4 Update `Agent` to remove extracted methods

After extraction, `Agent` retains:
- `__init__`, properties (`home`, `permissions`, `memory`)
- `_build_system_prompt` (stays — it's prompt assembly, not turn execution)
- `_resolve_provider` (stays — simple lookup)
- `_graphrag_enrich` (stays — side concern)
- `_emit` (stays — callback wrapper)

Methods moved to `TurnExecutor`: `_prepare_turn`, `_apply_before_llm_hook`, `_call_llm`, `_call_llm_stream`, `_dispatch_tool`, `_dispatch_tool_result`, `_handle_tool_call`, `_build_tool_message`, `_annotate_short_reply`, `_extract_pending_question`.

### 1.5 Update consumers

| File | Change |
|------|--------|
| `src/loom/__init__.py` | Add `TurnState`, `TurnExecutor` to imports + `__all__` (or keep them internal) |
| `tests/test_loop*.py` | Update private method tests to target `TurnExecutor` instead |
| `tests/test_overflow.py` | May need to construct `TurnState` instead of calling `Agent.run_turn` directly |
| `tests/test_sse_events.py` | No change (tests public `run_turn_stream` API) |
| `src/loom/heartbeat/scheduler.py` | No change (uses public `Agent` API) |

### 1.6 Verification

- [ ] `run_turn` and `run_turn_stream` each under 80 lines
- [ ] No duplicated logic between the two methods
- [ ] `TurnState` is created per-call, making `Agent` reentrant
- [ ] All existing tests pass unchanged (public API preserved)
- [ ] `_pending_question` no longer on `self`

---

## Refactoring 2: MemoryStore → MemoryStore (facade) + 4 focused classes

**File**: `src/loom/store/memory/_core.py` (817 lines)
**Risk**: Medium (5 consumers + test suite)
**Principle**: Eliminate the 11 `if self._vault_backend is not None` branches via Strategy pattern. Separate schema, storage, and search.

### 2.1 Create `MemorySchema`

**New file**: `src/loom/store/memory/_schema.py`

Extract from `_core.py`:

| Method | Original lines | Responsibility |
|--------|---------------|----------------|
| `_init_fts5` | 236-265 | FTS5 virtual table creation |
| `_table_exists` | 267-272 | Schema check helper |
| `_migrate_content_to_fts5` | 274-283 | Data migration |
| `_migrate_salience_columns` | 285-287 | Column migration |
| `_migrate_vault_path_column` | 289-290 | Column migration |
| `memory_meta` DDL from `__init__` | 205-216 | Table creation |
| `memory_vectors` DDL from `__init__` | 218-223 | Table creation |

`MemorySchema` is called once during `__init__` and never again. It's a one-shot migrator.

### 2.2 Create `StorageBackend` protocol + two implementations

**New file**: `src/loom/store/memory/_backend.py`

```python
class StorageBackend(Protocol):
    async def write(self, key: str, content: str, metadata: dict, tags: list[str]) -> MemoryEntry: ...
    async def read(self, key: str) -> MemoryEntry: ...
    async def delete(self, key: str) -> str | None: ...
    async def search(self, query: str, limit: int) -> list[SearchHit]: ...
    async def list_entries(self, prefix: str, limit: int, offset: int) -> list[MemoryEntry]: ...
    async def recent(self, limit: int) -> list[MemoryEntry]: ...
    async def update_frontmatter(self, key: str, updates: dict) -> None: ...
```

Two implementations:

**`FileStorageBackend`** — extracted from `_core.py`:
- `_key_path` (294-297)
- `_write_file` (299-375)
- `_read_file` (377-396)
- All the file-based branches in `write`/`read`/`delete`/`search`/`list_entries`/`recent`

**`VaultStorageBackend`** — existing `_vault_backend.py` renamed to conform to the protocol:
- Already has `write`, `read`, `delete`, `search`, `list_entries`, `recent`
- Needs `update_frontmatter` (currently named differently)
- Currently imported as `VaultMemoryBackend`

This eliminates ALL 11 `if self._vault_backend is not None` branches. `MemoryStore` just calls `self._backend.write(...)`.

### 2.3 Create `MemorySearchEngine`

**New file**: `src/loom/store/memory/_search.py`

Extract from `_core.py`:

| Method | Original lines | Responsibility |
|--------|---------------|----------------|
| `recall` | 636-685 | Hybrid retrieval orchestrator |
| `_bm25_candidates` | 687-702 | FTS5/LIKE candidate pool |
| `_rerank` | 704-770 | Weighted scoring (BM25 + salience + recency + vector) |
| `_salience` | 772-778 | Salience formula |
| `_recency` | 780-790 | Recency decay formula |

`MemorySearchEngine` receives the SQLite `db` connection, `has_fts5` flag, and optional `EmbeddingProvider` via constructor.

### 2.4 Slim down `MemoryStore` to a facade

After extraction, `MemoryStore` becomes:

```python
class MemoryStore(SqliteResource):
    def __init__(self, dir, index_db, embedder=None, vault=None, ...):
        self._schema = MemorySchema(db)
        self._backend = VaultStorageBackend(...) if vault else FileStorageBackend(...)
        self._search = MemorySearchEngine(db, has_fts5, embedder)

    def write(self, key, content, **kw):
        entry = self._backend.write(key, content, **kw)
        # vector indexing + GraphRAG hook stay here (cross-cutting)

    def read(self, key):
        return self._backend.read(key)

    def recall(self, query, **kw):
        return self._search.recall(query, **kw)
    ...
```

### 2.5 Update consumers

| File | Change |
|------|--------|
| `src/loom/store/memory/__init__.py` | Update imports if internal modules moved |
| `src/loom/store/__init__.py` | No change (re-exports from `__init__.py`) |
| `src/loom/tools/memory.py` | No change (uses `MemoryStore` public API) |
| `tests/test_memory_store.py` | May need to test `FileStorageBackend` and `MemorySearchEngine` individually; existing integration tests unchanged |

### 2.6 Verification

- [ ] Zero `if self._vault_backend is not None` branches remain in `MemoryStore`
- [ ] `MemoryStore` under 200 lines (facade only)
- [ ] `MemorySearchEngine` independently testable
- [ ] All existing tests pass
- [ ] `_vault_backend.py` renamed to `_backend.py` with protocol conformance

---

## Refactoring 3: AgentRuntime → AgentRuntime + AgentRecord + AgentFactory

**File**: `src/loom/runtime.py` (161 lines)
**Risk**: Low (3 consumers, small file)
**Principle**: Replace 6 parallel dicts with a single typed container.

### 3.1 Create `AgentRecord` dataclass

**New file**: `src/loom/runtime/_record.py`

```python
@dataclass
class AgentRecord:
    agent: Agent
    home: AgentHome
    config: AgentConfig
    permissions: AgentPermissions
    session_store: SessionStore
    memory_store: MemoryStore
```

### 3.2 Create `AgentFactory`

**New file**: `src/loom/runtime/_factory.py`

Extract `create_agent` body (lines 57-116) into `AgentFactory.create(name, ...) -> AgentRecord`. This is a pure function that creates all the objects and returns a complete record. No dict mutation.

### 3.3 Slim down `AgentRuntime`

```python
class AgentRuntime:
    def __init__(self, loom_home=None):
        self._home = loom_home or Path.home() / ".loom"
        self._records: dict[str, AgentRecord] = {}
        self._provider_registry: ProviderRegistry | None = None

    def create_agent(self, name, ...):
        record = AgentFactory.create(name, ...)
        self._records[name] = record

    def remove_agent(self, name):
        record = self._records.pop(name, None)
        if record:
            record.session_store.close()
            record.memory_store.close()

    def get_agent(self, name): return self._records[name].agent
    def get_session_store(self, name): return self._records[name].session_store
    # etc.
```

### 3.4 Update consumers

| File | Change |
|------|--------|
| `tests/test_runtime.py` | Minor: `create_agent`/`remove_agent` API unchanged |
| `tests/test_tools.py` | No change (uses `AgentRuntime` public API) |

### 3.5 Verification

- [ ] Only 1 dict (`_records`) instead of 6
- [ ] All consumers work without changes
- [ ] `create_agent`/`remove_agent` are atomic operations on a single dict

---

## Refactoring 4: GraphRAGEngine → GraphRAGEngine (facade) + Indexer + Extractor + Retriever

**File**: `src/loom/store/graphrag/_engine.py` (511 lines)
**Risk**: Medium (4 consumers)
**Principle**: Separate the pipeline stages.

### 4.1 Create `GraphRAGIndexer`

**New file**: `src/loom/store/graphrag/_indexer.py`

| Method | Original lines | Responsibility |
|--------|---------------|----------------|
| `chunk_text` | 151-157 | Delegate to `chunk_markdown` |
| `index_source` | 159-199 | Chunk + embed + store + trigger extraction |
| `index_vault` | 201-208 | Iterate vault files, call `index_source` |
| `remove_source` | 210-219 | Delete chunks + entities for a source |
| `_chunk_ids_for_source` | 487-491 | Helper |
| `_get_chunk` | 493-505 | Helper |
| Chunk DDL from `__init__` | 105-117 | Schema |

Owns: `VectorStore`, chunk SQLite db. Depends on `EntityGraph` (passed in).

### 4.2 Create `GraphRAGExtractor`

**New file**: `src/loom/store/graphrag/_extractor.py`

| Method | Original lines | Responsibility |
|--------|---------------|----------------|
| `_extract_entities` | 221-256 | Call LLM per chunk, with gleaning |
| `_store_extraction` | 258-305 | Write parsed results to EntityGraph |

Owns: `EntityGraph` reference + LLM provider. Called by `Indexer` after chunking.

### 4.3 Create `GraphRAGRetriever`

**New file**: `src/loom/store/graphrag/_retriever.py`

| Method | Original lines | Responsibility |
|--------|---------------|----------------|
| `retrieve` | 307-315 | Thin wrapper |
| `retrieve_enriched` | 317-456 | Hybrid vector + graph expansion |

Read-only. Owns: references to `VectorStore`, `EntityGraph`, chunk db (for reading only).

### 4.4 Keep `format_context` and `export_graph` on `GraphRAGEngine`

These are formatting/output concerns that are small (30-60 lines each) and depend on the retrieval result types. They stay on the facade.

### 4.5 Slim down `GraphRAGEngine` to facade

```python
class GraphRAGEngine(SqliteResource):
    def __init__(self, db_dir, config, embedder, llm=None):
        self._indexer = GraphRAGIndexer(db_dir, config, embedder, entity_graph)
        self._extractor = GraphRAGExtractor(entity_graph, llm, config) if llm else None
        self._retriever = GraphRAGRetriever(vector_store, entity_graph, chunk_db)

    def index_source(self, path, content):
        chunks = self._indexer.index_source(path, content)
        if self._extractor:
            self._extractor.extract(chunks)

    def retrieve(self, query, **kw): return self._retriever.retrieve(query, **kw)
    def format_context(self, results, **kw): ...  # stays
    def export_graph(self, **kw): ...  # stays
```

### 4.6 Update consumers

| File | Change |
|------|--------|
| `src/loom/store/graphrag/__init__.py` | Re-export new sub-modules if needed |
| `src/loom/store/memory/_core.py` | No change (only uses `GraphRAGEngine` public API) |
| `tests/test_graphrag.py` | Add tests for Indexer/Extractor/Retriever individually; existing integration tests unchanged |

### 4.7 Verification

- [ ] `GraphRAGEngine` under 150 lines
- [ ] Indexer, Extractor, Retriever independently testable
- [ ] Public API (`index_source`, `retrieve`, `format_context`) unchanged

---

## Refactoring 5: EntityGraph → EntityRepository + TripleRepository + GraphQueries

**File**: `src/loom/store/graph.py` (480 lines)
**Risk**: Low (3 consumers: `GraphRAGEngine`, `__init__.py` re-exports, tests)
**Principle**: Separate write operations from read-only graph traversal.

### 5.1 Create `EntityRepository`

**New file**: `src/loom/store/graph/_entities.py`

| Method | Original lines | Type |
|--------|---------------|------|
| `resolve_entity` | 113-143 | Write (find-or-create) |
| `get_entity` | 145-152 | Read |
| `find_entity` | 154-163 | Read |
| `set_entity_description` | 475-480 | Write |
| Entity-related DDL from `_SCHEMA` | 42-65 | Schema |

### 5.2 Create `TripleRepository`

**New file**: `src/loom/store/graph/_triples.py`

| Method | Original lines | Type |
|--------|---------------|------|
| `add_triple` | 165-189 | Write |
| `add_mention` | 191-196 | Write |
| `remove_for_chunks` | 247-283 | Write (cascading delete) |
| `remove_for_source` | 285-286 | Write (delegates) |
| Triple/mention DDL from `_SCHEMA` | 66-75 | Schema |

### 5.3 Create `GraphQueries`

**New file**: `src/loom/store/graph/_queries.py`

All read-only methods (13 methods):

| Method | Original lines |
|--------|---------------|
| `entities_for_chunk` | 198-207 |
| `chunks_for_entity` | 209-214 |
| `neighbors` | 216-245 |
| `count_entities` | 288-290 |
| `count_triples` | 292-294 |
| `list_entities` | 296-322 |
| `get_entity_triples` | 324-341 |
| `subgraph` | 343-406 |
| `connected_components` | 408-435 |
| `entity_degree` | 437-442 |
| `entity_counts_by_type` | 444-448 |
| `list_all_entities` | 450-456 |
| `list_all_triples` | 458-473 |

### 5.4 Keep `EntityGraph` as facade

`EntityGraph` composes the three sub-objects and delegates:

```python
class EntityGraph(SqliteResource):
    def __init__(self, db_path):
        ...
        self._entities = EntityRepository(self._db)
        self._triples = TripleRepository(self._db)
        self._queries = GraphQueries(self._db)

    # Delegate all public methods to sub-objects
    def resolve_entity(self, *a, **kw): return self._entities.resolve_entity(*a, **kw)
    def neighbors(self, *a, **kw): return self._queries.neighbors(*a, **kw)
    # etc.
```

This preserves the public API completely.

### 5.5 Verification

- [ ] `EntityGraph` is a pure delegation facade (no logic)
- [ ] Each sub-module under 120 lines
- [ ] All existing tests pass unchanged

---

## Refactoring 6: SshSessionTool → SshSessionTool + SshConnectionPool + TmuxManager

**File**: `src/loom/tools/ssh_session.py` (531 lines)
**Risk**: Low (only test consumers, runtime-registered)
**Principle**: Separate connection lifecycle from tmux session management from tool dispatch.

### 6.1 Create `SshConnectionPool`

**New file**: `src/loom/tools/ssh/_pool.py`

| Extract | Original lines | Responsibility |
|---------|---------------|----------------|
| `_ScopeState` dataclass | 97-100 | Connection state per scope |
| `_ensure_connection` | 224-268 | Open/reuse SSH connections |
| `_run_remote` | 270-280 | One-shot command over connection |
| `aclose` | 499-509 | Close all connections |

Owns: `self._scopes`, `self._lock`, credential resolver.

### 6.2 Create `TmuxManager`

**New file**: `src/loom/tools/ssh/_tmux.py`

| Extract | Original lines | Responsibility |
|---------|---------------|----------------|
| `_SessionState` dataclass | 89-94 | Session state |
| `_action_open` | 284-323 | Create tmux session |
| `_action_send` | 325-431 | Send command + poll output |
| `_action_read` | 433-461 | Capture pane buffer |
| `_action_close` | 463-479 | Kill tmux session |
| `_action_list` | 481-495 | List sessions |
| Done-marker constants | | Session completion detection |

Receives `SshConnectionPool` via constructor. Each `_action_*` calls `pool._run_remote(scope, command)`.

### 6.3 Slim down `SshSessionTool`

```python
class SshSessionTool(ToolHandler):
    def __init__(self, resolver, **kw):
        self._pool = SshConnectionPool(resolver, **kw)
        self._tmux = TmuxManager(self._pool, **kw)

    async def invoke(self, args):
        action, scope = args["action"], args["host"]
        await self._pool.ensure_connection(scope)
        return self._tmux.dispatch(action, scope, args)

    async def aclose(self):
        await self._pool.aclose()
```

### 6.4 Also extract shared `_classify_error`

**New file**: `src/loom/tools/ssh/_classify.py`

Currently duplicated in:
- `tools/ssh.py:48-78`
- `tools/ssh_session.py:61-86`

Both files import from the shared location.

### 6.5 Verification

- [ ] `SshSessionTool` under 60 lines (pure dispatch)
- [ ] `SshConnectionPool` manages only connections
- [ ] `TmuxManager` manages only tmux sessions
- [ ] `_classify_error` exists in one place
- [ ] All SSH tests pass

---

## Refactoring 7: SkillManager → SkillManager (disk) + SkillToolHandler (tool dispatch)

**File**: `src/loom/skills/manager.py` (238 lines)
**Risk**: Low (3 consumers)
**Principle**: Separate disk CRUD from tool invocation routing.

### 7.1 Keep `SkillManager` as disk-only CRUD

Methods that stay:
- `_create` (73-104)
- `_edit` (106-132)
- `_patch` (134-167)
- `_delete` (169-182)
- `_write_file` (184-212)
- `_remove_file` (214-238)
- `_skill_dir`, `_resolve`, `_scan_content`, `_build_skill_md` (helpers)

Remove: `invoke` (28-45).

### 7.2 Create `SkillToolHandler`

**New file**: `src/loom/skills/tool.py`

```python
class SkillToolHandler(ToolHandler):
    """Tool handler that routes manage_skill actions to SkillManager."""
    def __init__(self, manager: SkillManager):
        self._manager = manager

    async def invoke(self, args: dict) -> ToolResult:
        action = args.get("action")
        name = args.get("name")
        if action not in VALID_ACTIONS:
            return ToolResult(text=f"Unknown action: {action}", is_error=True)
        result = getattr(self._manager, f"_{action}")(name, args)
        is_error = isinstance(result, str) and result.startswith("error:")
        return ToolResult(text=result, is_error=is_error)
```

This is the same pattern as `HeartbeatToolHandler` in `heartbeat/tool.py`.

### 7.3 Fix encapsulation violation

Replace `self._registry._skills_dir` (manager.py:48) with:
- Add a public `skills_dir` property to `SkillRegistry`, or
- Pass the skills dir to `SkillManager.__init__` directly

### 7.4 Verification

- [ ] `SkillManager` has no `invoke` method
- [ ] `SkillToolHandler` is a proper `ToolHandler`
- [ ] No access to `SkillRegistry._skills_dir` from `SkillManager`

---

## Refactoring 8: HeartbeatManager → HeartbeatManager (disk) + HeartbeatToolHandler (already exists, strengthen)

**File**: `src/loom/heartbeat/manager.py` (171 lines)
**Risk**: Low (3 consumers)
**Principle**: Same split as SkillManager.

### 8.1 Remove `invoke` from `HeartbeatManager`

`HeartbeatToolHandler` already exists in `heartbeat/tool.py`. Remove the `invoke` method from `HeartbeatManager` and ensure all dispatch goes through `HeartbeatToolHandler`.

### 8.2 Fix N+1 query in `_list`

Replace the loop at line 163:
```python
# Before
[r for r in self._store.list_runs() if run.heartbeat_id == r.id]
# After: single query
self._store.list_runs_for_heartbeat(r.id)
```

Add `list_runs_for_heartbeat(heartbeat_id)` to `HeartbeatStore`.

### 8.3 Verification

- [ ] `HeartbeatManager` has no `invoke` method
- [ ] `HeartbeatToolHandler` is the sole entry point for tool dispatch
- [ ] `_list` makes 1 + N queries instead of N * M

---

## Implementation Order

Dependencies between refactorings are minimal. Recommended order by risk/payoff:

| Phase | Refactoring | Risk | Impact | Estimated effort |
|-------|------------|------|--------|-----------------|
| 1 | **#3 AgentRuntime** | Low | High (eliminates sync bugs) | Small |
| 2 | **#7 SkillManager** | Low | Medium | Small |
| 3 | **#8 HeartbeatManager** | Low | Medium | Small |
| 4 | **#6 SshSessionTool** | Low | Medium | Medium |
| 5 | **#5 EntityGraph** | Low | Medium | Medium |
| 6 | **#4 GraphRAGEngine** | Medium | High | Medium |
| 7 | **#2 MemoryStore** | Medium | High | Large |
| 8 | **#1 Agent** | High | Highest | Large |

Phases 1-3 are low-risk warmups. Phase 8 is the highest payoff but highest risk — do it last when patterns are established.

---

## General Principles for All Refactorings

1. **Preserve public API** — all existing consumers work unchanged
2. **One PR per refactoring** — easy to review and roll back
3. **Run full test suite** after each PR
4. **Add tests for new sub-classes** before extracting them
5. **Use facade pattern** — original class delegates to new sub-classes
6. **No behavioral changes** — this is pure restructuring
