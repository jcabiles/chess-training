# Tickets — Engine Reliability

Spec: `docs/ai-dlc/specs/engine-reliability.md`. Diagnosis: `docs/ai-dlc/contracts/engine-reliability.md`.

**Orchestration:** T1 is the async-correctness crux — implement it FIRST with a single dedicated
agent and review before building on it. After T1, the backend track (T2→T3) and frontend track
(T5→T4) touch disjoint files and may run as **two parallel agents** (≤2, per ORCHESTRATION).
T6 (verify) last.

| # | Ticket | Owned files | Done-condition | Deps |
|---|--------|-------------|----------------|------|
| T1 | Cancellation-safe + watchdog engine core: factor `_run_analyse` (lock → executor → `await asyncio.wait_for(asyncio.shield(fut), HARD)`; capture local `engine`; **separate** `except CancelledError: _poison; raise` and `except TimeoutError: _poison; raise EngineUnavailable`; `EngineError`→poison). Sync `_poison` (null handle+pid, **SIGKILL-first** via pid captured in `start()`, fire-and-forget `close()` never inline). `restart()` (lock-free poison). Soft cap `Limit(depth=18, time=ENGINE_SOFT_TIME)` on `analyze()`; `analyze_multi` depth-only. New env consts `ENGINE_SOFT_TIME`/`ENGINE_HARD_TIMEOUT`. | `app/engine.py` | New unit tests (T3) green; `analyze()`/`analyze_multi()` behavior unchanged on the happy path; pid access verified against installed python-chess. | — |
| T2 | `POST /api/engine/restart` → `await engine.restart()` → `{restarted, running}`; optional `GET /api/engine/status` → `{running}`. Additive response model(s). | `app/main.py`, `app/models.py` | Route returns 200; works without acquiring the engine lock; `/api/move` still 200 before+after; 503 shape unchanged. | T1 |
| T3 | Test fakes + engine tests: add `restart()` no-op to `FakeEngine` (+`is_running`) and `ScriptedEngine`; unit tests driving real `StockfishEngine` with an injected blocking/sleeping/raising fake `_engine` for **cancellation**, **hard-timeout**, **EngineError** → all poison + relaunch; soft-cap `Limit.time` asserted; restart-route API test. | `tests/test_api.py`, `tests/engine_fakes.py`, `tests/test_engine.py` (new ok) | `pytest -q` green incl. new tests; no real Stockfish binary needed. | T1, T2 |
| T5 | "Restart engine" control markup in the Analysis panel (near `#analysis-status`), always visible, with id for wiring; tokens-only styles (incl. disabled/spinner state). | `static/index.html`, `static/style.css` | Button renders in the Analysis panel; no raw hex (`grep -nE '#[0-9a-fA-F]{3,6}'` shows none new). | — |
| T4 | Frontend logic in `refreshAnalysis()`: **single in-flight + coalesce-to-latest** (record latest cursor/fen while busy; fire one follow-up if it changed) + monotonic **stale-response token**; wire the restart button → `POST /api/engine/restart` → toast + re-run analysis for current position (disable+spinner while in flight); on 503/engine error show inline "Engine stopped responding — use Restart engine" and STOP (no retry storm). No `AbortController`. | `static/app.js` | Rapid move-list clicks → at most one in-flight, settles on latest; restart preserves game (moves/cursor); error path shows hint, no retry loop. | T5, T2 |
| T6 | Verify end-to-end. | — | See Verify-by below; pytest green, ruff clean, 0 console errors. | T1–T5 |

## T6 — Verify-by (from spec)
1. **Unit:** cancellation mid-`analyze()` → poisoned + next call relaunches; hard-timeout →
   `EngineUnavailable` + recover; `EngineError` → poison; `analyze()` uses `Limit(time≈3.0)`.
2. **API:** `POST /api/engine/restart` ok (no lock needed); `/api/move` works before+after; 503 shape intact.
3. **Browser:** rapid move-list clicking → one in-flight, coalesces to latest (no pile-up);
   "Restart engine" button restarts + game preserved; induced engine error shows inline restart
   hint, no retry storm; 0 console errors.
4. `pytest` green; `ruff` clean.

## Notes
- Keep the single SimpleEngine + executor + lock mechanism (no UciProtocol).
- `_poison` must never block/await (so `restart()` is instant even when wedged).
- Background review job inherits the protection; threat-probe timeouts are silently skipped
  (intended), main-loop timeouts fail the job (intended).
