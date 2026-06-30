# Delta Spec — Game Review & Coaching

## Goal (one line)
Turn the app into a **personal weakness coach driven by your own games**: import + persist
PGN, replay games step-by-step, and surface (a) a **cross-game tendency profile** of your
recurring mistakes/blunders and (b) **pre-blunder foresight warnings** during replay that
explain the *developing* threat a move or more before it costs you — all powered by a
**deterministic** Stockfish + python-chess pipeline (zero LLM tokens to start).

## Locked decisions (from interview)
- **Scope = Profiler + Foresight.** The **Trainer** (re-solve-your-blunders + spaced
  repetition) is **deferred to a follow-on epic** (its design notes live in research §4).
- **Templates now, LLM optional later.** Ship deterministic + Python-template narration
  (zero tokens, zero ToS risk, offline). A pluggable narrator seam allows a future Claude
  layer; **no `anthropic` dependency this epic.** OAuth/Max is forbidden by ToS (research §2).
- **Storage = SQLite** (`sqlite3` stdlib — no new pip dep). The app's first persistence.
- **Import = in-app paste/upload + startup folder scan** of `data/games/`, deduped; **PGN
  clock tags (`%clk`) preserved** (unlocks time-trouble detection).
- **Only mistakes + blunders surface** (not inaccuracies/good/best) and **only for the
  user's own color.** Win%-drop thresholds **tuned for ~800-1100** play.

## The product shape
A **shared backend foundation** (import → persist → deterministic per-ply analysis →
structured **leak records**), with **two heads** on top:
- **Profiler** — a Review-tab dashboard: top leaks by frequency / phase / opening / color,
  the "hope-chess" habit stat, and trend-over-time. (Research §3 top-5 = the ranking.)
- **Foresight** — stepping through a saved game in a new `review` mode, warning cards fire on
  the **lead-in plies** before each leak ("opponent is setting up Nc7 forking K+R; your f5
  bishop's only defender is pinned — defend now"), in DecodeChess-style buckets
  (Threat / Hanging / Plan). De-emphasize the at-the-blunder scold.

## In scope

### Backend
- **`app/storage.py`** — SQLite DAL + schema + tiny migration runner; import-safe `init()` in
  the lifespan; degrades gracefully. Tables (final shape may refine in T1):
  - `games(id, content_hash UNIQUE, pgn, headers_json, white, black, result, eco, opening,
    date, my_color, source, ply_count, imported_at, analysis_status)`.
  - `pos_cache(epd_key, depth, eval_cp_white, mate_white, best_uci, best_san, pv_san_json,
    pv2_cp_white)` — keyed by **EPD** (position+side+castling+ep, move-counters stripped, so
    transpositions actually hit) + depth; evals stored **White-POV-normalized at write time**
    (`pov_score_to_white_cp`) so every reader is POV-safe; multipv≥2 fills `pv2_cp_white`.
  - `game_plies(game_id, ply, san, uci, fen_before, eval_cp_white, mate_white, win_prob,
    is_user_move, clock_centis)` — `clock_centis` from a `%clk` regex (nullable).
  - `leaks(id, game_id, ply, color, severity, category, motif_json, phase, win_prob_before,
    win_prob_after, win_prob_drop, hung_square, threat_uci, threat_motif, best_uci, best_san,
    lead_in_ply, tags_json, explanation_json)`.
  - `schema_meta(version)`.
- **`app/pgn.py`** — parse PGN text (one or many games) via `chess.pgn.read_game`; extract
  headers, SAN/UCI move list, FENs, **preserve `%clk` clock comments**; compute
  `content_hash` for dedup; infer `my_color` by matching `CHESS_USERNAME` aliases against
  White/Black (fallback: unknown → user picks, default White). Pure (python-chess only).
- **`app/analysis.py` additions (pure):** `win_prob_from_cp(cp)`, `game_phase(board)` →
  opening|middlegame|endgame, `leak_severity(win_prob_drop)` → none|mistake|blunder (filters
  out inaccuracy/good/best; level-tuned thresholds as named constants).
- **`app/motifs.py` (pure):** rule-based tactical detection ported from lichess-puzzler
  `cook.py` style — `hanging`, `fork` (knight-fork subtag), `pin`, `skewer`, `discovered`,
  `back_rank`, plus a **SEE** helper + attacker/defender counts. python-chess only,
  unit-testable, no engine.
- **`app/engine.py` multipv extension** — today `analyze(fen, depth)` returns a single line
  with no multipv (engine.py:216); the only-move / sharpest-swing lead-in logic needs the
  2nd-best score. Add `multipv` to the **same locked + executor** path (ranked lines + 2nd-best
  cp). Ship a **scriptable test engine** (FEN→result, multipv, mate) for the `get_engine` fake
  — the existing `FakeEngine` returns a constant `Cp(20)` (test_api.py:21) and cannot exercise
  the pipeline; do not break its signature (152 tests depend on it).
- **`app/review.py`** — the engine-orchestrating pipeline (impure conductor; testable via the
  scriptable fake): walk every ply through the **single locked** `engine.analyze`, fill
  `pos_cache` + `game_plies`; classify leaks (Win%-drop, user-color only, mistakes+blunders);
  **localize the lead-in ply** (sharpest swing + only-move) ; run the **null-move threat
  probe** (guarded by `is_check`) + `motifs` scan at each leak → write `leaks`. Runs as a
  **background asyncio task** with status in `games.analysis_status`, **yielding between
  plies so interactive `/api/move` is never starved**.
- **`app/coaching.py` (pure):** `Narrator` protocol + default `TemplateNarrator` → turns a
  leak record into the bucketed foresight text + a tendency-cluster name. Seam for a future
  `ClaudeNarrator` (env `COACH_NARRATOR=template|claude`, default `template`).
- **`app/profile.py`** — aggregate SQL over `leaks` → tendency profile (top categories by
  count, by phase/opening/color, hope-chess rate = % games with an allowed-threat leak,
  trend buckets by import date). Reads storage; no engine.
- **`app/profile`/opening cross-link** — tag each game's opening via `openings`/`book`; the
  profile can point a recurring opening leak at the existing traps/repertoire trainers.
- **New routes in `app/main.py`** (+ new response models in `app/models.py`):
  `POST /api/games/import` · `GET /api/games` · `GET /api/games/{id}` ·
  `POST /api/games/{id}/analyze` · `GET /api/games/{id}/status` ·
  `GET /api/games/{id}/review` · `DELETE /api/games/{id}` · `GET /api/profile`.
  Literal paths before `{id}`. Engine routes return 503 on `EngineUnavailable` as today.
- **Lifespan wiring:** `storage.init()` → folder-scan `data/games/` import (deduped) →
  optionally enqueue analysis of un-analyzed games (off by default; trigger via API/UI).

### Frontend
- **New "Review" tab** (`#tab-review`, `data-tab="review"`) in the SPA — added alongside the
  existing tabs without disturbing them.
- **`static/review.js` + `static/review.css`** (injected-`api` module): game **library list**
  + import (paste/upload) UI; **profile dashboard** (top leaks, habit stat, trends); load a
  game → enters `review` mode; render **foresight cards** keyed to the current ply.
- **`review` client mode** in `app.js`: load a saved game's `{baseFen, moves}` into the
  existing board, step via the existing `movelist.js`/undo/redo/`goToPly`; expose a
  **ply-change hook** so `review.js` shows the right foresight card; **transient** (not
  persisted — follows the trap/rep snapshot precedent; restores the play session on exit).
- Reuse `movelist.js`, board sync, `format.js`, `panel.js`.

## Out of scope (explicit)
- **Trainer / spaced-repetition / re-solve-your-blunders** (next epic — research §4).
- **Any LLM call / `anthropic` dep / OAuth** (templates only; seam left for later).
- **Auto-fetch games from lichess/chess.com APIs** (manual import only this epic).
- **Eval graph / sparkline** (data is produced here → unblocked, but not built — backlog #3).
- **Variation-tree replay** (linear `moves[]` only; backlog #4).
- **Positional/strategic "plan" judgement** beyond the deterministic motif/threat layer.
- **Changing any existing route, wire-format field, or the localStorage session shape.**
- **Light theme, deep a11y, command palette** (separate backlog items).

## Architecture — seam contract (so tickets parallelize on disjoint files)
- **`app/main.py` is edited by exactly ONE ticket (T6).** `static/app.js` + `static/index.html`
  by exactly ONE ticket (T7). Everything else is NEW files owned by one ticket each.
- **Leak-record shape is defined in T1** (a dataclass in `storage.py` + DB schema) so T4
  (writer) and T5 (coaching/profile readers) build against it in parallel.
- **The narrator is a seam** (`coaching.Narrator`) — `review.py` produces structured leak
  records; narration is applied by `coaching.TemplateNarrator`; the API serves narrated text.
- **Background analysis** funnels through `StockfishEngine.analyze()` (the one lock) and
  `await asyncio.sleep(0)`-yields between plies; status polled via `GET /api/games/{id}/status`.

## Invariants preserved (see `contracts/game-review-coaching.md`)
Stateless existing routes + frozen wire-format fields + frozen localStorage shape;
`analysis.py` purity (new math is pure); `engine.py` import-safety + single lock + `EngineUnavailable`
degradation; `get_engine` test seam (all engine code testable with a fake, no binary needed);
route-order rule; one-directional frontend modules (injected `api`); client never sends EPDs;
`review` mode transient like trap/rep.

## Token / LLM strategy
Zero tokens this epic. Deterministic detectors + `TemplateNarrator` produce all coaching
text. The `Narrator` seam + `COACH_NARRATOR` env keep a future Haiku layer a drop-in (tiny,
cached, batched per game) without reworking the pipeline.

## Constraints (chess-app)
- **`sqlite3` only** (stdlib) — no new `requirements.txt` entry. DB file (`data/games.db`,
  override `GAMES_DB`) is **user data → `.gitignore` it**; `data/games/` folder is for
  user-dropped PGNs (gitignore its contents, keep a `.gitkeep`).
- Reuse `pov_score_to_white_cp`/`classify`; keep new `analysis.py` math **pure** + White-POV.
- Background job must **not starve** interactive analysis (yield between plies; the lock
  already serializes — interactive `/api/move` just queues normally).
- All engine-touching code testable via the `get_engine` fake (no Stockfish in CI).
- Commit policy: implemented + verified (pytest + Playwright) + reviewed (maker ≠ checker);
  no debug artifacts (`.playwright-mcp/`, screenshots, `console.log`).

## Verify-by (end-to-end)
1. `pytest` green — existing 152 tests unchanged + new pure tests (storage, pgn, analysis
   math, motifs, coaching, profile) + API tests (games/review/profile via the `get_engine`
   fake). No Stockfish binary required.
2. **Import:** `POST /api/games/import` with a multi-game PGN (incl. `%clk`) persists +
   dedups; re-import is a no-op; dropping a `.pgn` in `data/games/` then restarting imports it
   once. Persists across restart (preloaded).
3. **Analyze:** trigger analysis on a game with a **known blunder** (FakeEngine scripted in
   tests; real Stockfish in a manual pass) → `leaks` contains the blunder for the user's
   color only, with a `lead_in_ply` earlier than the blunder ply, a `threat`/`motif`, and a
   non-empty template explanation. Inaccuracies/good moves are NOT in `leaks`.
4. **Profiler (Playwright):** Review tab shows the library; the profile lists top leak
   categories with counts + the hope-chess habit stat + opening/color breakdown.
5. **Foresight (Playwright):** open a game → `review` mode loads it on the board; stepping to
   a lead-in ply shows the foresight card (threat/hanging/plan buckets) *before* the blunder;
   reaching the blunder ply ties back to the earlier warning; exiting Review restores the
   prior play session unchanged; 0 console errors.
6. Existing flows unaffected: play a move, undo/redo, traps, repertoire, FEN load all work;
   localStorage key/shape unchanged.
7. Independent review (maker ≠ checker) clean before commit.

## File-ownership table (the parallel-safety contract)
Each shared/existing file is edited by **exactly one** ticket. Everything else is a NEW file
owned by one ticket.

| File | Owner | Note |
|---|---|---|
| `app/storage.py` (new), `.gitignore`, `data/games/.gitkeep` | **T1** | adds `data/games.db`, `data/games/` to gitignore |
| `app/pgn.py` (new) | **T2** | owns its `ParsedGame` DTO |
| `app/analysis.py` (additions), `app/motifs.py` (new) | **T3** | only ticket touching analysis.py |
| `app/engine.py` (multipv), scriptable test engine | **T4** | only ticket touching engine.py |
| `app/review.py` (new) | **T5** | |
| `app/coaching.py` (new), `app/profile.py` (new) | **T6** | |
| `app/main.py` (routes + lifespan), `app/models.py` (additions) | **T7** | only ticket touching main.py/models.py; runs after T1/T5/T6 freeze their public APIs |
| `static/index.html`, `static/app.js`, `static/movelist.js`, `static/review.js`+`.css` | **T8** | only ticket touching app.js/index.html/movelist.js — frontend hotspots |
| (merge + cross-cutting glue) | **T9** | integrator |

## Refuter resolutions (binding — fold into the named ticket)
1. **(blocker) Engine has no multipv** — `analyze(fen,depth)` is single-line (engine.py:216).
   **T4** adds multipv to the locked path + 2nd-best cp; review.py's only-move/lead-in logic
   depends on it. → schema `pos_cache.pv2_cp_white`.
2. **(blocker) FakeEngine can't test the pipeline** — it returns constant `Cp(20)`
   (test_api.py:21), single-line, never mate. **T4** ships a `ScriptedEngine` (FEN→result,
   multipv, mate) injected via `app.dependency_overrides[get_engine]`; **does not** change the
   existing `FakeEngine` signature (152 tests). T5/T7 tests use the scriptable fake.
3. **(blocker) Replay reuse is gated to play-mode** — `movelist.js:63` and `app.js:465`
   early-return when `state.mode !== 'play'`; the only `position:change`-style hook lives in
   the play-only `syncBoard` (app.js:220). **Decision: reuse, don't duplicate** — **T8** owns
   `movelist.js` too and **broadens** `render()`/`goto()`/the board-sync path to accept the new
   `review` mode, emitting a `review:ply` event for `review.js`. (Chosen over a separate
   review-only replay engine to keep one notation/keyboard UX.)
4. **(blocker) Review tab can't switch** — the tab handler is play-only (app.js:1828) with a
   hardcoded panel array `['analysis','opening','traps','repertoire']` (app.js:1837). **T8**
   adds `'review'` to that array. **UX decision:** the **Review tab is a normal panel tab**
   (library + dashboard, available in play mode); **opening a game enters `review` mode** (like
   trap/rep) with a **"Return to my game"** button (precedent app.js:1891/1905) that restores
   the play snapshot — the user does NOT return via the (frozen) tab bar.
5. **(major) Background-job latency, not "never starved"** — the lock frees between
   `analyze()` calls (engine.py:245 wraps one search), but each background ply holds it for a
   full depth search, so an interactive `/api/move` can wait ~one ply (~1-2s). **Resolution
   (T5):** run the background pass at a **lower, configurable depth** than interactive AND have
   the loop **await the lock only when no interactive request is pending** (a counter the
   `/api/move` route bumps); document the bounded worst-case; add an async test proving bounded
   interactive wait mid-run. Drop the "never starved" wording.
6. **(major) Job lifecycle** — **T1 schema**: `analysis_status` enum =
   `pending|analyzing|done|failed`. **T5 job**: keep a task registry keyed by `game_id`;
   `DELETE` cancels the task; the loop checks cancellation + game-existence between plies.
   **T7 lifespan**: on startup reset any `analyzing` rows → `pending` (no task survived restart).
7. **(major) `pos_cache` key** — key on **EPD** (move-counters stripped) so transpositions
   hit; store evals **White-POV-normalized** at write (`pov_score_to_white_cp`). Pure test:
   two FENs differing only in move counters share one cache row. (T1 schema + T5 writer.)
8. **(major) `my_color`** — new env `CHESS_USERNAME` (comma-separated aliases). If unset/no
   match → `my_color = NULL`; the **profiler EXCLUDES NULL-color games** (never default to
   White — that counts the opponent's blunders as yours). **Per-game override** in the import
   API/UI. (T2 infer, T1 nullable column, T6 `WHERE my_color IS NOT NULL`, T7 override field,
   README env note.)
9. **(major) `%clk` parsing** — python-chess does NOT structure `%clk`; **T2** regexes
   `\[%clk\s+(\d+):(\d+):(\d+(?:\.\d+)?)\]` → `game_plies.clock_centis`. Time-trouble
   *detection* in the profiler is **best-effort/optional** this epic — the data is captured
   regardless; don't block the epic on it.
10. **(minor) Two move-quality systems** — review uses **Win%-drop** (level-tuned, win-prob
    aware); play mode keeps **cp-loss** `classify()`. They may disagree **by design**; state
    it so it isn't read as a bug. (Spec note + T3.)
11. **(minor) T8 is large** — done by one agent (frontend hotspots can't parallelize) in a
    fixed sub-order: tab shell + library + import → review mode + replay hook → dashboard →
    foresight cards. `motifs.py`+SEE (T3) is likewise budgeted as a substantial standalone.
