# Chess Training — Stockfish Analysis Board

A local, single-user web app: an interactive chess board where you move **both
colors** freely, jump to any position by FEN, and get **live Stockfish feedback**
(eval, best move + line, move-quality label) after every move.

All open-source, free, and local-first. No accounts. Runtime API calls stay on
your machine; the page may fetch pinned frontend assets from a CDN on first load.

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

## Opening data

The opening trainer uses the bundled lichess-org/chess-openings TSVs under
`data/openings/`. These files are **CC0 / public domain**.

## Usage
- Drag pieces to play **either color**. Illegal moves snap back.
- Paste a **FEN** and click *Load* to jump to a position.
- After each move: eval, Stockfish's best move + line, and a quality label
  (best / good / inaccuracy / mistake / blunder).
- **Undo/redo** to step through your line; **Flip** to rotate the board.
- **Set up position** to arrange any position freely, then *Begin Game*.
- **Opening trainer:** the panel names the opening of your current line and lists
  named openings reachable from here. Click one to **study** it — step through the
  moves on the board with the eval and a note on the idea behind each move.

## Develop / test
```sh
pytest                      # all tests
pytest tests/test_analysis.py   # pure logic, no Stockfish needed
```

## Project layout
```
app/        FastAPI app, engine wrapper, analysis logic, models
static/     frontend files; chessground/chessops loaded from pinned CDN URLs
tests/      unit + API tests
docs/ai-dlc/  spec + ticket plan for this build
```

## Frontend asset versions
Pinned in `static/index.html`.
