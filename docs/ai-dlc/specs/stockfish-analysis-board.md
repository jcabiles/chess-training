# Spec — Stockfish Analysis Board

**Status:** Reviewed (Gate 1 confirmed; refuter pass folded in)
**Slug:** `stockfish-analysis-board`
**Type:** Greenfield app (new repo `chess-training`)

## Goal (one line)
Local single-user web app: an interactive chess board where you move both colors freely, jump to any position by FEN, and get live Stockfish feedback (eval, best move + line, move-quality label) after every move.

## Stack
- **Backend:** FastAPI + Uvicorn (Python, async).
- **Engine driver:** `python-chess` (`chess`, `chess.engine` UCI wrapper).
- **Engine:** Stockfish binary (installed separately: `brew install stockfish`).
- **Frontend:** chessground (Lichess board, GPL) for rendering + **chessops** (MIT) for client-side legality (chessground does NOT know chess rules — it needs a precomputed legal-`dests` map and a move validator). Vanilla JS/HTML/CSS, pinned vendored assets, no build step.
- **Python:** target **3.12+** (tested baseline). 3.14 is present locally but `uvloop`/`httptools` (pulled by `uvicorn[standard]`) may lack 3.14 wheels → use plain `uvicorn` (no `[standard]`) to avoid source-build failures. `python-chess` is pure-Python (fine on any 3.12+).

## Behavior (in scope)
1. **Board:** starts at standard opening. User drags pieces to make **legal moves for both colors**. **Legality is enforced on BOTH sides:** the client uses chessops to compute legal destination squares (highlight + instant snap-back on illegal drops, no round-trip lag); the **server re-validates every move with python-chess** as the authority before applying it (rejects desync / tampering). **Promotion:** when a pawn reaches the last rank, the client prompts for the promotion piece and sends a UCI move with the suffix (e.g. `e7e8q`); server validates it.
2. **FEN load:** text input + "Load" button. Valid FEN → board jumps to that position and play continues from there. Invalid FEN → inline error, board unchanged.
3. **Auto-analysis:** every applied move (either color) and every FEN load triggers a Stockfish analysis of the resulting position.
4. **Feedback panel shows:**
   - **Eval score** of current position, always displayed from **White's POV** (`PovScore.white()`), e.g. `+0.80`, `-1.2`, `M3`. (One consistent convention everywhere; the per-mover view used for classification is derived from this — see below.)
   - **Best move + principal variation** (Stockfish top line) for the **current/resulting** position, in SAN.
   - **Move-quality label** for the move just played: best / good / inaccuracy / mistake / blunder, derived from centipawn loss (see classification section). No label on FEN load or the initial position (no prior move).
5. **Undo / takeback:** step back one move (and redo forward) through the played line. Undo re-analyzes the resulting position; quality label reflects that position's last move (or none).
6. **Flip board:** rotate orientation (Black at bottom / White at bottom). Pure client-side.
7. **Engine config:** **fixed DEPTH** (target depth 18) — NOT a time cap. Depth (not time) is pinned so the before- and after-move evals are comparable (cpLoss is meaningless if the two positions are searched to different depths). Also configure Stockfish `Threads` and `Hash` (e.g. Threads=2, Hash=128) for usable per-move latency. No UI control.

## Move-quality classification (centipawn-loss thresholds)
All evals are taken as **White-POV centipawns** `E(pos)` (mate mapped to cp — see below). cpLoss is the eval the mover *gave up*, sign-corrected by who moved:
- White moved: `cpLoss = E(before) − E(after)`
- Black moved: `cpLoss = E(after) − E(before)`
Then **clamp `cpLoss = max(0, cpLoss)`** (engine noise / a move that matches the engine's pick can yield a small negative; floor at 0).

> Sign rationale (refuter fix): after a move the side-to-move flips, so the raw engine eval of the after-position is from the opponent's POV. Using a single White-POV convention for both evals and flipping by mover color avoids the double-negation bug.

Default buckets (tunable constants in `analysis.py`, integer cp):
- `0–10` → **best**
- `11–50` → **good**
- `51–100` → **inaccuracy**
- `101–250` → **mistake**
- `>250` → **blunder**

**Mate mapping (with distance):** map a mate score to White-POV cp as `sign * (MATE_CP − N)` where `MATE_CP = 10000` and `N` = moves-to-mate (so M1 > M8 in magnitude — losing a *faster* mate, or converting a mate into a slower one, is penalized). This makes "had forced mate, threw it away" and "walked into a mate" naturally land in **blunder** via the same cpLoss formula, while M5→M3 (still winning, just faster) yields a small cpLoss. Guard against absurd overflow by capping cpLoss at a sane max for display.

## Files / interfaces to touch (proposed layout)
```
chess-training/
  README.md                 # setup: brew install stockfish, venv, run
  requirements.txt          # fastapi, uvicorn (NO [standard] — avoid uvloop on 3.14), chess, pydantic
  app/
    main.py                 # FastAPI app, routes, static mount
    engine.py               # Stockfish lifecycle + analyze(position) -> result
    analysis.py             # eval normalization + quality classification (pure, testable)
    models.py               # pydantic request/response schemas
  static/
    index.html
    app.js                  # chessground wiring, fetch calls, panel render
    style.css
    vendor/                 # chessground (render) + chessops (legality/dests, promotion) — pinned
  tests/
    test_analysis.py        # classification + eval normalization (pure unit, no engine)
    test_api.py             # routes with engine mocked/skipped if no binary
```

### API (server validates; one shared `analysis` shape everywhere)
- `POST /api/move` — body `{ fen (position BEFORE the move), move (uci) }`. Server validates legality with python-chess; if illegal → `{ legal:false }`. If legal: applies move, then runs **two depth-pinned analyses — the before-position and the after-position** (both needed to compute `quality`), and returns `{ legal:true, fen (after), lastMoveSan, analysis }`. The two-eval cost (~2× single-analysis latency per move) is accepted; depth-18 with Threads/Hash configured should keep it sub-second to ~1–2s.
- `POST /api/analyze` — body `{ fen }` → `{ analysis }` for that position (used on initial load / FEN load — no prior move, so `quality` is null).
- `POST /api/load` — body `{ fen }` → validate FEN → `{ valid:true, fen, analysis }` or `{ valid:false, error }`.
- Move history / undo state held **client-side** as a **list of UCI moves from the start position** (replayable → recovers any FEN, the last move, and SAN). Server stays stateless per request. Undo = client drops the last move, replays, and calls `/api/move` for the new last move (to get its `quality`) or `/api/analyze` if no moves remain.

**Shared `analysis` object (identical across all endpoints):**
`{ evalCp:int|null, mate:int|null, evalWhitePov:int (cp, mate-mapped), bestMoveSan:str, pvSan:[str], quality:("best"|"good"|"inaccuracy"|"mistake"|"blunder")|null }`
- `evalCp` / `mate`: raw display fields (one is null). `evalWhitePov`: the normalized cp value used by classification. `quality`: null unless a prior move exists. `bestMoveSan` / `pvSan` describe the **resulting** position.

## Constraints
- **Engine lifecycle:** one Stockfish process managed safely (startup on app start or first use; clean shutdown). Guard against the binary being missing → clear error at startup and a friendly API error, not a crash loop. Configure `Threads`/`Hash` on startup.
- **No blocking the event loop + thread-safety:** `SimpleEngine` is **not** thread-safe, and a thread-pool executor is inherently concurrent → serialize ALL engine access behind a single **`asyncio.Lock`** (one in-flight analysis at a time) while running the blocking `analyse()` call via `run_in_executor`. (Alternative: native async `chess.engine.popen_uci` / `UciProtocol` — pick ONE mechanism, don't mix.) This prevents concurrent requests from corrupting UCI state.
- **SAN rendering:** the engine returns `Move` objects (UCI); render SAN by pushing each PV move on a `board` copy of the **correct** position (resulting position for move-quality; loaded position for `/api/analyze`).
- **Open-source/free only**; runs fully local; no external network calls at runtime.
- **No secrets / no API keys**.
- Pure logic (`analysis.py`) must be unit-testable **without** a Stockfish binary present.
- Concurrency: single-user assumption is fine; serialize engine access (one process) so concurrent requests don't corrupt UCI state.

## Out of scope (this build)
Top-3 MultiPV; adjustable depth/time UI; copy/export FEN+PGN; full-game PGN review; play-vs-engine opponent; drag-piece position editor (illegal-position setup); saving / accounts / persistence; opening book / tablebase; mobile-optimized UI; multi-user/session state on server.

## Verify-by (end-to-end)
1. `brew install stockfish`; `pip install -r requirements.txt`; start server (`uvicorn app.main:app`).
2. Open `http://localhost:8000` → board renders at opening position.
3. Play **1.e4** (White) → eval + best move/line + quality label appear.
4. Play a Black move → same feedback updates, from correct perspective.
5. Play a deliberate blunder (e.g. hang the queen) → label = **blunder**, best move shown.
6. Attempt an illegal move → snaps back, no server change. Promote a pawn → prompt appears, chosen piece applied.
7. Paste a known FEN + Load → board jumps, analysis shows (no quality label).
8. **Undo** → board steps back, re-analyzes; label reflects the new last move.
9. **Flip** → orientation rotates.
10. `pytest` → `test_analysis.py` passes with **no Stockfish binary required** (pure classification/normalization, incl. POV sign cases for White and Black moves, mate mapping, and negative-cpLoss clamp).
