# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A local, single-user **Stockfish chess training app**: FastAPI + python-chess
backend driving Stockfish, chessground/chessops frontend (one SPA, tab panels).
Play both colors, load FENs, undo/redo/flip, a clickable **move list**, live eval
+ move-quality labels, localStorage session persistence, and a setup/pregame
position editor. Plus four trainers:
- **opening trainer** (live opening detection + candidate openings + a study
  walkthrough with commentary; bundled lichess openings TSVs + `data/commentary.json`),
- **opening traps trainer** (browsable catalog, watch + practice modes; `data/traps.json`),
- **repertoire trainer** (per-color prepared lines: browse catalog + practice with
  deviation-checking; `data/repertoire.json`), and
- **game-review coach** (import/persist PGN, replay, a cross-game mistake/blunder
  **profiler**, and **pre-blunder foresight** during replay — a deterministic
  Stockfish + python-chess pipeline with template narration, no LLM; backed by
  SQLite `data/games.db` + a `data/games/` drop-folder). Modules: `storage`, `pgn`,
  `motifs`, `review`, `coaching`, `profile`.

## Commands

```sh
# One-time setup
brew install stockfish                 # engine binary (or set STOCKFISH_PATH)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # if truststore error: add --cert "$(python3 -m certifi)"

# Run (8001 because 8000 is often taken by other local apps)
uvicorn app.main:app --reload --port 8001     # → http://localhost:8001

# Test
pytest                                  # full suite
pytest tests/test_analysis.py           # pure logic — no Stockfish binary needed
pytest tests/test_api.py::test_move_legal_labels_quality   # a single test
```

## Constraints (non-obvious)

- Server is **stateless per request** for play + the opening/traps/repertoire
  trainers (move history lives client-side) — **except game review**, which persists
  to SQLite (`app/storage.py`, `data/games.db`) and runs a **background analysis
  task** (`app/review.py`). User game data (`data/games.db`, `data/games/`) is
  **gitignored**; never commit it.
- `app/engine.py`: one Stockfish process, all access serialized behind a single
  `asyncio.Lock` (SimpleEngine isn't thread-safe). Import-safe if the binary is
  absent (`EngineUnavailable`). `analyze_multi(fen, depth, multipv)` shares the same
  lock; the review job funnels through it and yields to interactive `/api/move`
  (via `review.note_interactive_start/end`).
- `app/analysis.py` (plus `motifs.py`, `pgn.py`, `coaching.py`, `profile.py`) are
  **pure** — unit-testable without a Stockfish binary; the full suite runs with no
  engine via the `get_engine` fake seam.
- Reuse `analysis.pov_score_to_white_cp` / `classify` — all evals are White-POV
  before classification; don't re-derive the mover-sign rule.

## Commit policy (IMPORTANT)

Commit only after a change is **implemented, verified, and reviewed** — never WIP.

1. **Implemented** — complete, not partial.
2. **Verified** — evidence seen, not assumed:
   - Python/API: `pytest` passes.
   - Frontend/UI: exercised in the browser (Playwright or manual).
3. **Reviewed** — diff read for correctness; no debug artifacts left
   (`console.log`, `window.__dbg`, screenshots, `.playwright-mcp/`).

**Format:** Conventional Commits subject (`feat:`, `fix:`, `refactor:`, `docs:`,
`test:`, `chore:`); imperative, ≤ 50 chars; body explains *why* when non-obvious.
End every message with:
`Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`

One logical change per commit. Never `git push` unless asked. If verification
fails or was skipped — say so and don't commit.

## Environment notes

The Claude Code Bash **sandbox** may block network installs, socket `bind`, and
writes to `.git/`. Verify routes in-process via FastAPI `TestClient` when a live
server is unavailable.
