# Tickets — Stockfish Analysis Board

Spec: `docs/ai-dlc/specs/stockfish-analysis-board.md`. Build order respects deps; `[P]` = parallelizable once deps met. One owner per file.

---

### T1 — Project scaffold + deps + setup docs
**Do:** create repo structure (`app/`, `static/`, `tests/`), `requirements.txt` (fastapi, uvicorn [no `[standard]`], chess, pydantic, pytest), `README.md` with setup (`brew install stockfish`, venv, `pip install`, `uvicorn app.main:app`), `.gitignore`.
**Owns:** `requirements.txt`, `README.md`, `.gitignore`, `app/__init__.py`, empty package dirs.
**Done:** `pip install -r requirements.txt` succeeds on Python 3.12+; `python -c "import fastapi, chess"` works.
**Deps:** none.

### T2 — Pure analysis logic + unit tests `[P]`
**Do:** `analysis.py` — White-POV eval normalization, mate→cp mapping `sign*(10000−N)`, cpLoss with mover-color sign flip, `max(0,·)` clamp, threshold→label buckets. Pure functions, no engine import.
**Owns:** `app/analysis.py`, `tests/test_analysis.py`.
**Done:** `pytest tests/test_analysis.py` passes with **no Stockfish present** — covers White-move & Black-move sign cases, mate mapping (M1>M8), negative-cpLoss clamp, each bucket boundary.
**Deps:** T1. Parallel with T3/T4/T7.

### T3 — Stockfish engine wrapper `[P]`
**Do:** `engine.py` — locate/launch one Stockfish process, configure `Threads`/`Hash`, depth-18 `analyse(fen)` returning raw `{score(PovScore), pv(Move[])}`; serialize via `asyncio.Lock` + `run_in_executor`; clean shutdown; friendly error if binary missing.
**Owns:** `app/engine.py`.
**Done:** with Stockfish installed, a small script calls `analyze(startpos)` and gets a score + PV; with binary absent, raises a clear handled error (no crash loop).
**Deps:** T1. Parallel with T2/T4/T7.

### T4 — Pydantic models (shared schema) `[P]`
**Do:** `models.py` — request/response schemas; the single shared `analysis` object (`evalCp, mate, evalWhitePov, bestMoveSan, pvSan, quality`) used by all three endpoints; move/load/analyze request bodies.
**Owns:** `app/models.py`.
**Done:** `python -c "from app.models import *"` imports; schemas instantiate with example data.
**Deps:** T1. Parallel with T2/T3/T7. (Defines the contract T6 codes against.)

### T5 — FastAPI app + routes + API tests
**Do:** `main.py` — app, static mount, `/api/move` (validate w/ python-chess, two depth-pinned evals → quality), `/api/analyze`, `/api/load` (FEN validation); wire engine+analysis+models; SAN rendering on correct board copy.
**Owns:** `app/main.py`, `tests/test_api.py`.
**Done:** `pytest tests/test_api.py` passes (engine mocked/skipped if no binary); `/api/load` rejects bad FEN; `/api/move` rejects illegal move; with engine present, `/api/move` returns a `quality` label.
**Deps:** T2, T3, T4.

### T6 — Frontend board + interactions
**Do:** `index.html`, `app.js`, `style.css` — chessground render; chessops legal-`dests` + snap-back; promotion prompt → UCI suffix; both-colors play; FEN input+Load; client move-list history → undo/redo; flip; feedback panel (eval, best move/PV, quality label); fetch wiring to the T4 contract.
**Owns:** `static/index.html`, `static/app.js`, `static/style.css`.
**Done:** served at `localhost:8000`, board renders; legal moves only; promotion works; FEN load jumps; undo steps back; flip rotates; panel updates after each move.
**Deps:** T5 (live API). Can start against T4 contract in parallel.

### T7 — Vendor frontend assets `[P]`
**Do:** pin/vendor chessground + chessops (+ piece sprites/CSS) into `static/vendor/`; document versions.
**Owns:** `static/vendor/`.
**Done:** assets load locally with no CDN/network at runtime; versions recorded in README.
**Deps:** T1. Parallel with T2/T3/T4. (T6 consumes these.)

### T8 — End-to-end verification
**Do:** run the spec's full Verify-by checklist against the running app; fix integration gaps; finalize README run/verify steps.
**Owns:** README verify section (shared edit — coordinate); no new source files.
**Done:** all 10 Verify-by steps pass on a clean machine (post `brew install stockfish`).
**Deps:** T5, T6, T7.

---

## DAG / waves
- **Wave 1:** T1.
- **Wave 2 (parallel):** T2, T3, T4, T7.
- **Wave 3:** T5 (needs T2+T3+T4).
- **Wave 4:** T6 (needs T5; may pre-build against T4).
- **Wave 5:** T8 (needs T5+T6+T7).

Hotspot files (single owner, no concurrent edits): `app/main.py` (T5), `static/app.js` (T6). README touched by T1 and T8 — sequence, don't parallelize.
