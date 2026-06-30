# Delta Spec — Test-suite speedup (cache data loads, skip engine spawn in tests)

## Problem (why)
The full suite takes ~289s. Profiling (`pytest --durations=25`) shows the slowest 25 are
all **`setup`**, ~3.4s each, in the API test files. Cause: each API test's `client`
fixture does `with TestClient(app)`, which runs the FastAPI **lifespan on every test**,
which unconditionally rebuilds module-global data:

| lifespan step | per-call cost | note |
|---|---|---|
| `openings.load(data/openings)` | ~1.9s | re-parses + replays all TSV lines |
| `book.load(...)` | ~1.3s | rebuilds book EPD set from all opening + trap + rep lines |
| `engine.start()` | ~0.21s | spawns a real Stockfish per test (tests use fakes) |
| `traps.load` | ~0.006s | fine |

~86 API tests × ~3.4s ≈ the entire runtime. The 390 pure tests are nearly free. The
loaded data is **identical every call** — rebuilding it per test is pure waste.

## Goal (one line)
Make `openings.load` and `book.load` **idempotent** (skip the rebuild when the inputs
are unchanged) and make the test lifespan **skip the real Stockfish spawn**, cutting the
suite from ~289s to tens of seconds — with **zero behavior change** and the full suite
still green (degraded-mode tests included).

## Locked decisions
- **Cache by INPUT, never a global "loaded" flag.** Degraded tests call
  `openings.load("/nonexistent...")` / `traps.load("/nonexistent...")` and expect an
  empty result; the opening/traps fixtures call `load(FIXTURE_DIR)` / `load(PRODUCTION)`.
  The cache key MUST include the data source so a different source always rebuilds.
- **Don't cache empty results** — so a missing dir followed by a real load rebuilds.
- **Engine: skip only the lifespan autostart via a flag (preserve real-engine tests).**
  A first cut pointed `STOCKFISH_PATH` at a bogus path — but that ALSO disabled the 3
  real-Stockfish integration tests in `test_engine_multi.py` (they read `STOCKFISH_PATH`
  to start their own engine), and would do the same in CI. Instead: `lifespan` skips
  `engine.start()` when `CHESS_SKIP_ENGINE_AUTOSTART` is set (a guarded, prod-off opt-out
  in main.py); a new `tests/conftest.py` sets that flag. `STOCKFISH_PATH` is left
  untouched, so the direct-engine tests still run with the real binary. `close()` is
  documented safe on a never-started engine (engine.py:238).
- **Don't bother caching `traps.load`** (6ms) or `repertoire` — negligible.

## In scope

### `app/openings.py` — `load()` memoization
- Add a module global `_loaded_dir: str | None = None`.
- At the top of `load()`, after resolving `data_dir`: compute `key = str(Path(data_dir).resolve())`.
  If `_loaded_dir == key and not _index.empty`: `return _index` (cache hit).
- On a successful non-empty build: set `_loaded_dir = key`.
- On missing dir / no rows (empty index): set `_loaded_dir = None` (don't cache empties).

### `app/book.py` — `load()` memoization
- Materialize inputs once at the top: `lines_list = [tuple(l) for l in lines]`,
  `traps_list = [tuple(t) for t in trap_ucis]` (use these for both the key AND the build
  so a generator isn't consumed twice).
- Build a cheap signature: `sig = (resolved_config_path, len(lines_list), len(traps_list),
  hash(tuple(lines_list)), hash(tuple(traps_list)))` stored in a module global
  `_cache_sig`. (Store the hash tuple, not the full lines — keep memory small.)
- If `sig == _cache_sig and not _index.empty`: `return _index`.
- On empty/disabled config: `_index = BookIndex(); _cache_sig = None; return`.
- On a successful build: `_cache_sig = sig`. The build loop must iterate `lines_list` /
  `traps_list` (the materialized tuples), not the original iterables.

### `app/main.py` (lifespan) + `tests/conftest.py` (NEW)
- `main.py`: add `import os`; in `lifespan`, wrap `engine.start()` so it's skipped when
  `os.environ.get("CHESS_SKIP_ENGINE_AUTOSTART")` is set (logs that it skipped). Default
  off → production unchanged.
- `tests/conftest.py`: a **session-scoped autouse** fixture that sets
  `CHESS_SKIP_ENGINE_AUTOSTART=1` for the session (restoring the prior value on
  teardown). API tests inject fake engines via `dependency_overrides`; the unstarted
  lifespan engine is never used. The 3 real-engine tests start their own engine and are
  unaffected (`STOCKFISH_PATH` untouched).

## Out of scope
- Session/module-scoping the `client` fixtures, or rewriting any test fixture.
- Touching `app/main.py`, `app/engine.py`, `app/traps.py`, `app/repertoire.py`.
- Editing existing test files (keep the diff to new conftest + the two loaders) — avoids
  conflicts with the in-flight accuracy PR (#7).
- Any change to production behavior (the caches only skip provably-redundant rebuilds;
  prod loads once at startup anyway, so it's harmless there).

## Constraints
- **Full suite stays green**, especially the degraded-mode tests
  (`test_opening_api::test_opening_degraded_when_data_absent`,
  `test_traps_api::test_*_degraded_when_no_data`, `test_repertoire_api::test_repertoire_empty_degrades`)
  and the unit book/openings tests that load fixtures with differing data.
- `openings`/`book` stay pure + import-safe (no engine import).
- Caches keyed on inputs → a different source ALWAYS rebuilds (no stale data).

## Verify-by
1. `pytest -q` → still **475 passed** (no skips/failures); degraded tests pass.
2. `pytest --durations=15` → the ~3.4s `setup` entries are gone; total wall-clock is a
   small fraction of 289s (target: well under ~60s).
3. `ruff check` clean.
4. Spot-check determinism: run the suite twice — same pass count (no cache-order flakiness).
