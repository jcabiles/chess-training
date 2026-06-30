# Chess Training — Stockfish Analysis Board

[![CI](https://github.com/jcabiles/chess-training/actions/workflows/ci.yml/badge.svg)](https://github.com/jcabiles/chess-training/actions/workflows/ci.yml)

A local, single-user web app: an interactive chess board where you move **both
colors** freely, jump to any position by FEN, and get **live Stockfish feedback**
(eval, best move + line, move-quality label) after every move.

It has grown into a small **training suite**: an **opening trainer**, an
**opening-traps trainer**, a **repertoire trainer**, and a **game-review coach**
that imports your past games and flags your recurring mistakes — with a warning a
move *before* each blunder.

All open-source, free, and local-first. No accounts. Runtime API calls stay on
your machine; the page may fetch pinned frontend assets from a CDN on first load.

![Stockfish Analysis Board — board with live evaluation, best line, opening
repertoire tree, and trap catalog](docs/screenshot.png)

## Stack
- **Backend:** FastAPI + Uvicorn, [python-chess](https://python-chess.readthedocs.io/) (engine driver + rules)
- **Engine:** [Stockfish](https://stockfishchess.org/) (installed separately)
- **Frontend:** [chessground](https://github.com/lichess-org/chessground) (board) + [chessops](https://github.com/niklasf/chessops) (client-side legality)

## Setup

1. **Install Stockfish** (the engine binary):
   ```sh
   brew install stockfish      # macOS
   # Linux: apt install stockfish  |  or download from stockfishchess.org
   ```
   Verify: `stockfish` should launch a UCI prompt (Ctrl-D to exit).

2. **Python env** (3.12+):
   ```sh
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Run:**
   ```sh
   uvicorn app.main:app --reload --port 8001
   ```
   Open <http://localhost:8001>.

   > Port **8001** is the default here because **8000** (uvicorn's own default)
   > is commonly taken by other local apps. Use any free port — e.g.
   > `--port 8123`. Run two apps at once by giving each its own port; no need
   > to stop the other.

## Configuration (optional)

All via environment variables; sensible defaults mean none are required to start.

| Variable | Default | What it does |
|----------|---------|--------------|
| `STOCKFISH_PATH` | `stockfish` on `PATH` | Path to the engine binary, if it isn't on your `PATH`. |
| `CHESS_USERNAME` | _(unset)_ | Comma-separated names/aliases you play under (lichess / chess.com). Imported games are tagged with **your** color so the review coach analyzes *your* mistakes only. If unset, set the color per-import in the Review tab; un-tagged games are skipped in the profile. |
| `GAMES_DB` | `data/games.db` | SQLite file for saved games + cached analysis (gitignored). |
| `REVIEW_BG_DEPTH` | `10` | Stockfish depth for background game-review analysis. Interactive moves use a deeper search; the background job yields to interactive requests so play stays responsive. |
| `ENGINE_SOFT_TIME` | `3.0` | Soft per-search time cap (seconds) for interactive analysis: Stockfish stops at the target depth **or** this time, whichever comes first, so a sharp position can't stall the UI. |
| `ENGINE_HARD_TIMEOUT` | `8.0` | Hard watchdog (seconds): any single engine call exceeding this auto-kills and relaunches the Stockfish process so it can never wedge. Must be greater than `ENGINE_SOFT_TIME`. |

Saved games and their analysis live in `data/games.db` (and any PGNs you drop in
`data/games/`) — both gitignored, never committed.

## Opening data

The opening trainer uses the bundled lichess-org/chess-openings TSVs under
`data/openings/`. These files are **CC0 / public domain**.

## Usage
- Drag pieces to play **either color**. Illegal moves snap back.
- Paste a **FEN** and click *Load* to jump to a position.
- After each move: eval, Stockfish's best move + line, and a quality label
  (best / good / inaccuracy / mistake / blunder).
- **Evaluate** toggle (Both / White / Black): set it to your color and Stockfish
  only analyzes **your** moves — the opponent's are shown as "not evaluated" and
  skip the engine entirely, roughly halving the work (faster, less load). *Both*
  (the default) analyzes every move as before. Your choice is remembered.
- If the engine ever feels stuck, the **Restart engine** button in the Analysis
  panel relaunches Stockfish without disturbing your game (the position and move
  list are preserved). Searches are time-bounded and a watchdog auto-restarts a
  hung engine, so the app shouldn't wedge in the first place.
- **Undo/redo** (or click any move in the **move list**) to step through your
  line; **Flip** to rotate the board.
- **Set up position** to arrange any position freely, then *Begin Game*.
- **Opening trainer:** the panel names the opening of your current line and lists
  named openings reachable from here. Click one to **study** it — step through the
  moves on the board with the eval and a note on the idea behind each move.
- **Opening Traps:** browse a curated set of beginner opening traps (filter by name
  or color). **Watch mode** steps through a trap line with eval + coaching notes;
  the branch point shows the expected mistake and what Black (or White) should do
  instead. **Practice mode** lets you play the trapping side while the opponent
  plays the standard blunder — find the punishing moves (wrong moves revert; "Reveal"
  hints, "Show refutation" gives the defense). A **Trap available** chip appears
  during play when you reach a known trap's starting position. Data lives in
  `data/traps.json` (engine-verified, original commentary; 19 traps shipped).
- **Repertoire trainer:** browse your prepared lines per color (a catalog grouped
  by opening) and **practice** them — the opponent plays prepared replies and you
  must find your single prepared move; deviations are flagged. Lines live in
  `data/repertoire.json` (SAN lines or `trapId` references, replayed and validated
  with python-chess).
- **Game Review & Coaching:** import your past games (paste/upload a **PGN** in the
  Review tab, or drop `.pgn` files in `data/games/`), saved to a local database and
  **preloaded** on restart. Replay any game step-by-step, then get two things:
  a **tendency profile** of your most common mistakes/blunders across all games
  (by motif, game phase, opening, color, plus a "hope-chess" rate and trend), and
  **foresight warnings** during replay that fire a move *before* each blunder —
  explaining the developing threat in Threat / Hanging / Plan terms (e.g. "the
  opponent is threatening mate on f7 — defend now"). The analysis is **deterministic**
  (Stockfish + python-chess: win-probability drop, null-move threat detection,
  static-exchange + motif tagging); no LLM/tokens. Set `CHESS_USERNAME` (see
  Configuration above) so it knows which color is yours, or pick the color per-import.
  Opening a reviewed game also shows an **estimated Accuracy % and Elo for both
  sides** (you and your opponent) in the review bar — Chess.com-style, derived from
  the per-move evals already computed (no extra engine work). The rating is a rough
  single-game estimate, not an official number.

## Develop / test
```sh
pytest                      # full suite — runs without a Stockfish binary
                            # (engine code is exercised via a fake-engine seam)
pytest tests/test_analysis.py   # pure logic only
```

## Project layout
```
app/        FastAPI app + single-engine wrapper; pure logic (analysis, motifs);
            SQLite storage, PGN import, the game-review pipeline, coaching/profile,
            and per-trainer logic (openings, traps, book, repertoire)
static/     frontend — one SPA with tab panels; chessground/chessops from pinned CDNs
data/       bundled opening TSVs + traps/book/repertoire JSON; your saved games
            (data/games.db, data/games/) are gitignored
tests/      unit + API tests; the whole suite runs with no Stockfish binary present
docs/       design specs, build plans, and research notes per feature
```

## Frontend asset versions
Pinned in `static/index.html`.

## Development

Built solo with an AI-assisted workflow (Claude Code + Codex). The per-feature
design specs and build plans under `docs/` (`docs/design/` for the first features,
`docs/ai-dlc/` for newer ones) are the running record of how each piece was scoped
and verified.
