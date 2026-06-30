# Delta Spec — Engine Reliability (stop the slow-then-hang; add recovery)

Diagnosis: `docs/ai-dlc/contracts/engine-reliability.md`. This spec fixes the root cause and
adds recovery. No new deps; one engine mechanism (SimpleEngine + executor + single lock) kept.

## Problem (why)
The single Stockfish process wedges: a client refresh/disconnect cancels an in-flight request,
which releases the `asyncio.Lock` **while the executor thread is still running `engine.analyse()`**;
the next request then calls `analyse()` concurrently on the non-thread-safe `SimpleEngine` →
UCI corruption → permanent hang (unrecoverable by refresh). Compounded by: no time cap on
depth-18 (slow), no frontend brake (pile-ups), `EngineError` keeps a poisoned handle, and no
restart path.

## Goal (one line)
The app must not permanently wedge: bound + cancellation-proof every engine call, auto-recover a
wedged engine, give the user a one-click "Restart engine" that preserves the game, and stop the
frontend from queueing analyses.

## Locked decisions
- **Soft time cap ~3s** on interactive search + **hard per-call watchdog ~8s** → on breach,
  poison+restart and surface an error (no hang). Eval comparability may degrade slightly on rare
  sharp positions — accepted.
- **Auto-restart on wedge** + an **always-visible manual "Restart engine"** control in the
  Analysis panel.
- Restart/teardown must be **forceful, non-blocking, and lock-free** (it has to work when the
  lock is held by a wedged call).
- Frontend: **coalesce to latest** (single in-flight), not abort — avoids triggering more
  cancellations.

## In scope

### `app/engine.py` — the core fix
- **Constants** (env-overridable, matching existing style):
  `INTERACTIVE_SOFT_TIME_S = float(os.environ.get("ENGINE_SOFT_TIME", "3.0"))`,
  `ENGINE_HARD_TIMEOUT_S = float(os.environ.get("ENGINE_HARD_TIMEOUT", "8.0"))`
  (must be > soft cap). Keep `DEFAULT_DEPTH=18`.
- **At `start()`**: capture and store the OS pid of the subprocess (e.g.
  `self._pid = engine.transport.get_pid()` — guard for absence) so a wedged engine can be killed
  without depending on its (possibly hung) internal loop.
- **Factor a private `async def _run_analyse(self, board, limit, multipv)`** used by both public
  methods. It MUST:
  1. `async with self._lock:` lazily `start()`; **capture `engine = self._engine` as a LOCAL**
     (refuter [low] #7 — `_call` must close over this local, NEVER `self._engine.analyse(...)`,
     which would `AttributeError` after `_poison` nulls the handle); get the loop.
  2. `fut = loop.run_in_executor(None, _call)` where `_call` runs `engine.analyse(board, limit, multipv=...)` (closes over the local `engine`).
  3. `info = await asyncio.wait_for(asyncio.shield(fut), timeout=ENGINE_HARD_TIMEOUT_S)` —
     `shield` so a request-cancellation does NOT cancel the running future; `wait_for` is the
     hard watchdog. (Py 3.12: on timeout, `fut` keeps running and `wait_for` raises
     `TimeoutError`; on outer cancel, `CancelledError` is raised here while `fut` is protected.)
  4. **Two SEPARATE except clauses — do NOT collapse into one** (refuter [high] #3; `CancelledError`
     is `BaseException`, must be re-raised, never converted):
     - `except asyncio.CancelledError:` `self._poison(engine)` then **`raise`** (re-raise to honor
       cancellation — swallowing it wedges the task forever).
     - `except asyncio.TimeoutError:` `self._poison(engine)` then
       `raise EngineUnavailable("engine timed out; restarted")`.
     Poison nulls the handle, so after the lock releases the next call relaunches a clean engine.
  5. **`except EngineTerminatedError`**: `self._engine=None` (already) — process died, self-heals.
  6. **`except EngineError`**: `self._poison(engine)` then raise `EngineUnavailable` (NEW: was
     not nulling before — root cause #4).
- **`_poison(self, engine)` — a SYNC method, never `await`s** (refuter [med] #4, so `restart()`
  stays instant). It: (1) sets `self._engine=None` + `self._pid=None` immediately (correctness:
  a later call can't reuse the poisoned handle); (2) **forcibly terminates the old process,
  KILL-FIRST** (refuter [high] #1 — `engine.close()`/`quit()` can BLOCK on `process.wait()` if
  the worker thread is hung, so it must NOT be called inline first): if `self._pid` is known,
  `os.kill(pid, signal.SIGKILL)` inline (instant); then optionally schedule `engine.close()`
  fire-and-forget via `loop.run_in_executor(None, engine.close)` **without awaiting** (residual
  cleanup that can't stall the loop). Never raises. Guard `signal.SIGKILL` for non-POSIX (app is
  macOS/local; `getattr(signal, "SIGKILL", signal.SIGTERM)`).
- **pid capture (refuter [high] #2):** `get_pid()` access is not a guaranteed-stable public API.
  The builder MUST verify the real attribute chain against the **installed python-chess version**
  (likely `engine.transport.get_pid()`; fall back to `getattr` chains like
  `engine._transport`/`engine._process.pid`) and store it in `start()`. If no pid is obtainable,
  fall back to a detached `engine.close()` in an executor (never inline). A missing pid must not
  raise — it just degrades the backstop.
- **`async def restart(self)`**: call `self._poison(self._engine)` (sync, force-terminate,
  **lock-free** — do NOT `await self._lock`, a wedge holds it) leaving `self._engine=None` so the
  next analysis lazily starts fresh. Idempotent; safe if never started; reads/writes of
  `self._engine`/`self._pid` are all on the single event-loop thread (the executor `_call` only
  reads its LOCAL `engine`), so no data race (refuter [med] #4).
- **`analyze()`** → calls `_run_analyse(board, Limit(depth=DEFAULT_DEPTH, time=INTERACTIVE_SOFT_TIME_S))`
  (single line). **`analyze_multi()`** → `_run_analyse(board, Limit(depth=depth), multipv=multipv)`
  (background; depth-only, no soft cap; still under hard watchdog + poison). Post-processing
  (PovScore→AnalysisResult, SAN) unchanged.

### `app/main.py`
- **`POST /api/engine/restart`** → `await engine.restart()`; return `{ "restarted": true,
  "running": engine.is_running }`. Does not touch any game/DB state.
- (Optional) **`GET /api/engine/status`** → `{ "running": engine.is_running }` for the button.
- `_engine_unavailable_response` (503) unchanged; routes already convert `EngineUnavailable`.
  Verify `/api/move`'s `note_interactive_end()` still runs in `finally` (it does).

### `app/models.py`
- Additive response model(s) for restart/status.

### `tests/` fakes (required — refuter [med] #6)
- The new `/api/engine/restart` route calls `engine.restart()` and reads `engine.is_running`.
  Both test fakes must gain a `restart()` no-op: `FakeEngine` (`tests/test_api.py`) needs
  `restart()` **and** an `is_running` property; `ScriptedEngine` (`tests/engine_fakes.py`) has
  `is_running` but needs `restart()`. Without these the route test raises `AttributeError`.
- Cancellation/timeout/EngineError unit tests drive the REAL `StockfishEngine` with a fake
  injected `_engine` (no binary): an object whose `.analyse(board, limit, multipv=...)` blocks
  (asyncio event), sleeps past the hard timeout, or raises `EngineError`; plus `.close()` and a
  `.transport.get_pid()` (or chosen pid path) so `_poison` runs. Assert poison + relaunch.

### `app/review.py` — background behavior (refuter [med] #5; intentional, documented)
- No code change required, but note the **asymmetry**: the main per-ply `analyze_multi`
  (`review.py:~313`) lets `EngineUnavailable` propagate → the job is marked `failed` (correct).
  The null-move threat probe (`review.py:~461`) sits inside a broad `except Exception`
  (`~500`) → a watchdog timeout there **silently skips that one leak with a warning and
  continues** (acceptable: a single un-probed ply, not a job failure). This is intended.

### `static/app.js` (+ `static/index.html`, `static/style.css`)
- **Single-in-flight + coalesce** in `refreshAnalysis()`: if an analysis is in flight, record the
  latest requested `cursor`/fen instead of firing; when the in-flight one resolves, if the latest
  differs, fire exactly one follow-up. A monotonically-increasing **request token** guards stale
  responses (apply only the newest). No `AbortController` (avoid cancellation churn).
- **"Restart engine" button** in the Analysis panel (near `#analysis-status`), always visible:
  on click → `POST /api/engine/restart`, show a toast/status, disable+spinner while in flight,
  then re-run `refreshAnalysis()` for the current position. Game is untouched (client-side).
- **On 503 / engine error**: show a clear inline status ("Engine stopped responding — use Restart
  engine") and STOP (no auto-retry loop). Tokens-only CSS for the button.

## Out of scope
- The "my color" analyze-only-my-moves toggle (separate feature).
- Switching to native-async `UciProtocol`, or a multi-process engine pool.
- Changing `:session:v1`/game persistence, the review DB, or background-job depth behavior
  (it inherits the same protection automatically).
- A separate always-on watchdog task — the per-call `wait_for` IS the watchdog.

## Constraints
- Keep the single SimpleEngine + executor + lock mechanism (engine.py docstring contract).
- Teardown/restart path must be non-blocking and must not require the lock (it runs when wedged).
- Full suite stays green via the `get_engine` fake seam; new engine-internal tests inject a fake
  blocking `_engine` (no real binary).
- Tokens-only CSS, no raw hex. Conventional Commits; verified (pytest + browser) before commit.

## Verify-by
1. **Cancellation safety (unit):** with a fake `_engine` whose `analyse` blocks, start an
   `analyze()` task, cancel it mid-flight → engine is poisoned (`is_running` False), a subsequent
   `analyze()` relaunches and succeeds, and no overlap occurred (the lock is free).
2. **Hard watchdog (unit):** fake `analyse` sleeps > `ENGINE_HARD_TIMEOUT_S` → call raises
   `EngineUnavailable`, engine poisoned, next call works. **Soft cap:** `analyze()` passes a
   `Limit` with `time≈3.0`.
3. **EngineError (unit):** fake `analyse` raises `EngineError` → `EngineUnavailable` + engine
   nulled (poisoned).
4. **Restart (API):** `POST /api/engine/restart` returns ok and does not require the lock;
   `/api/move` works before and after; 503 shape preserved on `EngineUnavailable`.
5. **Frontend (browser):** rapid move-list clicking issues at most one in-flight analysis and
   settles on the latest position (no pile-up); the "Restart engine" button restarts and the game
   (moves/cursor) is preserved; an induced engine error shows the inline restart hint, no retry
   storm. 0 console errors.
6. `pytest` green; `ruff` clean.
