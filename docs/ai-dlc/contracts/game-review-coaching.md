# Contracts — Game Review & Coaching

Read-only architecture map (contract-mapper pass) defining the seams this epic must
respect, and the **new surfaces** it introduces (the app's first stateful + first
background-job code).

## Invariants this epic MUST NOT break

- **Server is stateless per request (today).** All game history lives client-side in
  `state.moves` + localStorage (`chess-training:session:v1`). This epic adds the FIRST
  server-side persistence — it must be **additive**: a NEW store, NEW routes. Do **not**
  change the existing `/api/move|analyze|load|opening|traps|repertoire` request/response
  shapes, and do **not** touch the localStorage session shape (`{baseFen, moves, cursor,
  orientation}` / setup variant). `restore()` tolerance must stay intact.
- **Single Stockfish process behind one `asyncio.Lock`.** `SimpleEngine` is not
  thread-safe. Full-game analysis = N **sequential** `engine.analyze()` calls through the
  same lock. There is NO engine parallelism. Batch/background analysis must funnel through
  `StockfishEngine.analyze()` — never open a second engine, never bypass the lock.
- **`app/analysis.py` is pure** (imports only `chess`/`chess.engine` types; no I/O, no
  engine wrapper). New pure math (Win%, phase, leak classification) may be ADDED here and
  stays unit-testable with no Stockfish. Reuse `pov_score_to_white_cp` + `classify` —
  never re-derive the White-POV / mover-sign rule.
- **`app/engine.py` is import-safe.** Importing must never launch Stockfish. New code may
  not change this. `EngineUnavailable` → friendly degradation (no 500s).
- **`get_engine` dependency-override is the test seam.** The whole suite (152 tests) runs
  with NO Stockfish binary via `app.dependency_overrides[get_engine] = lambda: FakeEngine`.
  All new engine-touching code must be testable through this seam (inject a fake).
- **Route registration order.** Literal POST paths register BEFORE `{param}` GETs
  (`/api/traps/check` before `/api/traps/{trap_id}`). New `/api/games/...` routes follow the
  same rule (e.g. `/api/games/import` before `/api/games/{id}`).
- **Data loaders are import-safe + degrade gracefully.** Module-level singletons, `init()`
  in the lifespan, never crash startup on bad/missing data, routes return well-formed empty
  bodies. New storage + folder-scan import must follow this pattern.
- **Frontend modules are one-directional.** Peripheral modules (`panel.js`, `movelist.js`,
  `feedback.js`, …) never import `app.js`; they receive deps via an injected `api` object.
  `app.js` is `type="module"` — module-private vars, no globals. A new `review.js` follows
  the same injected-`api` contract.
- **Client SAN is display-only; server is the authority** on legality/eval/identity. Client
  sends `{baseFen, moves}` (or a FEN), never an EPD. Keep it that way for game replay.
- **Transient modes are not persisted.** `trap-watch/trap-practice/rep-practice` early-return
  in `persist()` and restore via a snapshot (`playSnapshot`). A new `review` mode follows
  this precedent — it must NOT clobber or persist over the play session.

## New surfaces this epic introduces (no prior art in the repo)

1. **Persistent storage** — first write-to-disk in `app/`. Proposed: SQLite (`sqlite3` is
   stdlib; no new pip dep). User data, NOT bundled → `.gitignore` the DB file.
2. **A background job** — first long-running async task: batch per-ply analysis through the
   single engine lock, with status polling. New concurrency surface to design carefully
   (must not starve interactive `/api/move` calls — see spec).
3. **A new SPA tab + a new client mode** (`review`) reusing the existing board + `movelist.js`.
4. **A pluggable narrator seam** — template narration now; optional Claude later. No
   `anthropic` dependency added this epic.

## Reuse map (don't rebuild)

- `app/analysis.py`: `pov_score_to_white_cp`, `classify`, label set (best/good/inaccuracy/
  mistake/blunder), `MATE_CP`. ADD Win%/phase/leak-severity here (pure).
- `app/engine.py`: `StockfishEngine.analyze(fen, depth)` → `AnalysisResult{score, pv,
  pv_san, depth}`. Add a null-move/threat probe via the SAME locked path.
- `app/openings.py` / `app/book.py` / `app/traps.py` / `app/repertoire.py`: ECO/opening
  name lookup + trap-line matching → reuse for per-game opening tagging and "you blundered
  in the Italian → drill this trap/line" cross-links.
- Frontend `movelist.js` (clickable replay), `panel.js`, `format.js`, the board sync +
  `goToPly`/`undo`/`redo`/`flip` in `app.js`.

## Adjacent backlog items this epic subsumes/unlocks
- Backlog **#5 "per-move quality on loaded/restored games"** — delivered for saved games by
  the per-ply analysis cache.
- Backlog **#3 "eval graph"** — the per-ply eval history produced here is exactly its data
  source (graph itself left out of scope, but unblocked).
