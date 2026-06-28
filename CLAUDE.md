# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A local, single-user **Stockfish chess analysis web app**: FastAPI + python-chess
backend driving Stockfish, chessground/chessops frontend. Play both colors, load
FENs, undo/redo/flip, live eval + move-quality labels, localStorage session
persistence, a setup/pregame position editor, an **opening trainer** (live
opening detection + candidate openings + a study walkthrough with commentary,
backed by the bundled lichess openings TSVs + `data/commentary.json`), and an
**opening traps trainer** (browsable trap catalog with watch and practice modes,
backed by `data/traps.json`).

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

- Server is **stateless per request**; move history lives client-side.
- `app/engine.py`: one Stockfish process, all access serialized behind a single
  `asyncio.Lock` (SimpleEngine isn't thread-safe). Import-safe if the binary is
  absent (`EngineUnavailable`).
- `app/analysis.py` is **pure** — unit-testable without a Stockfish binary.

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
