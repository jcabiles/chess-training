# Tickets — Test-suite speedup

Spec: `docs/ai-dlc/specs/test-suite-speedup.md`.

**Shared contract:** caches key on INPUT (data source / materialized lines), never a bare
"loaded" flag; empty results are NOT cached; no behavior change; degraded-mode tests must
stay green. Only 3 files touched (2 loaders + new conftest) — no edits to existing tests
or to main.py/engine.py.

| # | Ticket | Owned files | Done-condition | Deps |
|---|--------|-------------|----------------|------|
| T1 | `openings.load()` path-keyed memoization: global `_loaded_dir`; cache-hit returns `_index` when `str(Path(data_dir).resolve()) == _loaded_dir and not _index.empty`; set `_loaded_dir` on non-empty build, `None` on empty/missing. | `app/openings.py` | Repeated `load(same_dir)` rebuilds once; `load(other_dir)` rebuilds; empty dir → empty index, not cached. | — |
| T2 | `book.load()` input-signature memoization: materialize `lines`/`trap_ucis` to tuples once (use for key + build); `_cache_sig = (resolved_cfg, len, len, hash, hash)`; cache-hit returns `_index`; `None` on empty config; set on build. | `app/book.py` | Repeated `load(same inputs)` rebuilds once; different inputs rebuild; empty config → empty, not cached. | — |
| T3 | New `tests/conftest.py`: session-scoped autouse fixture sets `STOCKFISH_PATH` to a bogus non-existent path for the session (skips real Stockfish spawn via the existing `EngineUnavailable` path); restores prior value on teardown. | `tests/conftest.py` (new) | Lifespan no longer spawns Stockfish in tests; suite still green. | — |
| T4 | Verify: full suite green (475), degraded tests pass, durations show the 3.4s setups gone, ruff clean, two runs same pass count. | — | See spec Verify-by. | T1,T2,T3 |

## Orchestration
T1/T2/T3 touch disjoint files → 3 parallel surgical builders. T4 verify (me): pytest +
durations + ruff.
