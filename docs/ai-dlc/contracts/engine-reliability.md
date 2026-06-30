# Contracts / Diagnosis — Engine Reliability (slows down → hangs, unrecoverable by refresh)

Investigation 2026-06-29 (read-only). App: FastAPI + ONE `SimpleEngine` Stockfish process
behind a single `asyncio.Lock`; blocking `engine.analyse()` runs via
`loop.run_in_executor(None, ...)` inside the lock. Symptom: progressive slowdown, then total
unresponsiveness; **browser hard-refresh does NOT recover** (only a server restart does); no
traceback (a HANG, not an exception).

## Ranked root causes

1. **[HIGH] Cancellation race releases the lock with the engine thread still running.**
   `engine.py:334–356` — `async with self._lock: ... await loop.run_in_executor(None, _run)`.
   On client disconnect/refresh, uvicorn cancels the handler → `CancelledError` at the `await`
   → `async with` calls `Lock.release()` **unconditionally** while the executor thread is still
   inside `engine.analyse()`. The next request acquires the lock and calls `engine.analyse()` on
   the SAME (non-thread-safe) `SimpleEngine` concurrently → UCI protocol corruption → the process
   hangs forever (thread blocked on a UCI read; all later requests queue behind it). Refresh =
   another cancellation = worse. **This is why refresh can't recover it.** (Confirmed by an async
   repro: lock released while thread still running, next waiter acquired it pre-completion.)

2. **[HIGH] No in-flight guard on the frontend** (`app.js` `refreshAnalysis()` ~321–342, called
   from `undo`/`redo`/`goto`/move-list click/mode-exits). No `AbortController`, no debounce, no
   "one at a time" guard. Fast navigation queues many `/api/move` (2× depth-18) or `/api/analyze`
   (1× depth-18) calls → the "slows down" phase, and more cancellations → more race triggers.

3. **[HIGH] No time cap on the search.** `engine.py:345` `Limit(depth=18)` → `time=None` → no
   timeout. One sharp position can hold the engine 5–10+ s; `/api/move` runs it twice, for every
   move regardless of side.

4. **[MED] `EngineError` does not null the engine.** `engine.py:349–350` (and `270–271`) raise
   `EngineUnavailable` but keep `self._engine`; a corrupted-but-alive handle is reused next call.
   Only `EngineTerminatedError` (`351–353`, `272–273`) self-heals by nulling.

5. **[LOW/MED] `/api/analyze` + `/api/load` + rep-reply don't call `note_interactive_start/end`**
   (only `/api/move` does, `main.py:303`). The background review job's no-starve yield
   (`review.py:282–286`, polls `_interactive_pending`) therefore won't pause for those, adding
   contention. Background job is depth-10 (`REVIEW_BG_DEPTH`), one `analyze_multi` per ply through
   the same lock — compounds slowdown while running, but is not itself the hang.

## Recovery gaps (what's missing)
- No cancellation shield / the lock can be released while the engine thread runs (cause #1).
- No per-call timeout (`asyncio.wait_for` / `Limit(time=...)`).
- No frontend abort of stale analyses.
- No engine health/reset endpoint — a wedged engine needs a full server restart.
- `EngineError` keeps a poisoned handle.
- No watchdog for a stuck executor thread / lock held too long.

## Key integration points (for any fix)
- `app/engine.py` `analyze` / `analyze_multi` — the lock + executor pattern (cause #1, #3, #4).
- `app/main.py` `/api/move` (`260–323`), `/api/analyze` (`225–238`), `/api/load` (`241–257`),
  `_engine_unavailable_response` (503). Lifespan startup `~118` (`reset_stuck`).
- `app/review.py` background task + `note_interactive_start/end` + `reset_stuck`.
- `static/app.js` `refreshAnalysis()` and its callers (undo/redo/goto/reset/mode-exits).
- Game state is **client-side** (localStorage `:session:v1`) → restarting the engine process
  does NOT touch the game; an engine reset is inherently game-preserving.
- Constraint: ONE engine mechanism only (SimpleEngine + executor + lock) — do NOT mix in
  native-async UciProtocol (engine.py docstring).
