# Tickets — Game Review & Coaching

9 tickets, 5 phases. File ownership is disjoint within each phase (see spec's ownership
table) → safe parallel agents. Backend-heavy epic; the frontend hotspots (`app.js`,
`index.html`, `movelist.js`) are a single owner (T8). Refuter amendments are baked in.

```
Phase 1 — Foundation (parallel ×4)        Phase 2 (∥×2)     Phase 3   Phase 4   Phase 5
 ┌ T1 storage + schema ┐                   ┌ T5 review ┐
 │ T2 pgn parse        ├──────────────────►│  pipeline ├──► T7 API ─► T8 front ─► T9 integrate
 │ T3 analysis+motifs  │                   │ T6 coach/ │     +wiring   +replay     verify
 └ T4 engine multipv ──┘                   └   profile ┘                +UI        review/commit
```

Engine work (T4) blocks the pipeline (T5). T7 runs after T1/T5/T6 freeze public APIs.
T8 (frontend) needs T7's routes. T9 integrates.

---

## Phase 1 — Foundation (parallel ×4)

### T1 — SQLite storage layer + schema
**Owns:** `app/storage.py` (new); `.gitignore`; `data/games/.gitkeep` (new).
**Does:** `sqlite3` DAL (stdlib — no pip dep). Tables per spec: `games` (incl. nullable
`my_color`, `analysis_status` enum `pending|analyzing|done|failed`, `content_hash UNIQUE`),
`pos_cache` (**EPD+depth** key, White-POV evals, `pv2_cp_white`), `game_plies` (incl.
`clock_centis` nullable, White-POV evals), `leaks`, `schema_meta`. A `LeakRecord` dataclass
(shared shape for T5 writer / T6 readers). Tiny migration runner keyed on `schema_meta`.
Import-safe `init(db_path)` (env `GAMES_DB`, default `data/games.db`); never crash on a
missing/locked DB. CRUD helpers (insert/dedup game, upsert pos_cache, write plies/leaks,
list/get/delete game, set status, **reset-analyzing-to-pending** startup helper). Add
`data/games.db` + `data/games/` to `.gitignore`.
**Acceptance:** schema creates from empty; dedup by `content_hash` is a no-op on re-insert;
pos_cache transposition test (two FENs differing only in move counters → one row); delete
cascades to plies+leaks; status reset helper flips `analyzing`→`pending`.
**Done-condition:** `pytest tests/test_storage.py` green; pure (no engine, no FastAPI).
**Deps:** none.

### T2 — PGN parsing + import model
**Owns:** `app/pgn.py` (new) + tests.
**Does:** `chess.pgn.read_game` loop over multi-game text/files → `ParsedGame` (headers,
SAN+UCI+`fen_before` per ply, result, ECO/opening best-effort, `ply_count`). Regex `%clk`
→ `clock_centis` per ply. `content_hash` (stable over moves+key headers) for dedup.
`my_color` inference: match `CHESS_USERNAME` (comma-separated aliases, env) vs White/Black;
**no match/unset → `None`** (never guess White). Pure (python-chess only).
**Acceptance:** parses a multi-game PGN; `%clk` extracted to centiseconds; identical PGN →
identical `content_hash`; `my_color` None when username absent; malformed game skipped, not
crashing.
**Done-condition:** `pytest tests/test_pgn.py` green (fixture PGNs incl. one with `%clk`,
one multi-game, one malformed); pure.
**Deps:** none.

### T3 — Analysis math + tactical motif detection
**Owns:** `app/analysis.py` (additions only); `app/motifs.py` (new) + tests.
**Does:** *analysis.py (pure):* `win_prob_from_cp(cp)` (Lichess model), `game_phase(board)`
→ opening|middlegame|endgame, `leak_severity(win_prob_drop)` → none|mistake|blunder (filters
out inaccuracy/good/best; **level-tuned named constants** for ~800-1100; documented to
intentionally differ from cp-loss `classify`). *motifs.py (pure, python-chess only):*
attacker/defender counts, a correct **SEE** (static exchange evaluation) helper, and motif
detectors `hanging`, `fork` (+`knight_fork`), `pin`, `skewer`, `discovered`, `back_rank`
(lichess-puzzler `cook.py` style). All deterministic, no engine.
**Acceptance:** win% monotonic + symmetric around 0; phase boundaries sane; SEE correct on
known capture sequences (winning/losing/equal); each motif detector flags a crafted FEN and
rejects a near-miss.
**Done-condition:** `pytest tests/test_analysis.py tests/test_motifs.py` green; both modules
import without a Stockfish binary.
**Deps:** none.

### T4 — Engine multipv + scriptable test engine
**Owns:** `app/engine.py` (multipv); a scriptable test engine helper in `tests/`.
**Does:** extend the locked path — add `multipv: int = 1` to `analyze()` (or a sibling
`analyze_multi`) passing `multipv=` to `engine.analyse`, returning ranked lines incl. the
2nd-best score, **still under the one `asyncio.Lock` + executor** (no second process, import-
safe, `EngineUnavailable` preserved). Backward-compatible default (`multipv=1` == today).
Ship `ScriptedEngine` (FEN→scripted `AnalysisResult`, multipv + mate support) for
`get_engine` override — **without** changing the existing `FakeEngine` signature.
**Acceptance:** `multipv=1` matches current single-line behavior (existing tests green);
`multipv=2` returns two ranked lines with a measurable 2nd-best cp; ScriptedEngine returns
distinct evals per FEN incl. a mate.
**Done-condition:** `pytest` green incl. existing 152; a new test drives multipv via the
override seam with no real binary.
**Deps:** none.

---

## Phase 2 — Pipeline + coaching/profile (parallel ×2)

### T5 — Review pipeline + background analysis job
**Owns:** `app/review.py` (new) + tests (using T4's ScriptedEngine).
**Does:** for a game, walk every ply through T4's locked multipv `analyze`; upsert
`pos_cache` (EPD, White-POV) + `game_plies` (incl. win%); classify leaks via
`leak_severity` on **Win%-drop**, **user-color only** (skip if `my_color` None),
mistakes+blunders only; **lead-in localization** (sharpest Win% swing + only-move from
2nd-best gap) → `leaks.lead_in_ply`; at each leak run the **null-move threat probe**
(`board.push(Move.null())`, **guarded by `is_check`**, threat read from the returned PV) +
`motifs` scan → fill threat/motif/hung-square/best. Run as a **background asyncio task**:
task registry keyed by `game_id`; **lower configurable depth** than interactive; **await the
lock only when no interactive request is pending** (counter bumped by `/api/move`); cancel on
delete; check cancellation + game-existence between plies; set `analysis_status`.
**Acceptance:** on a ScriptedEngine game with a planted blunder, `leaks` has that blunder for
the user color only, `lead_in_ply < blunder_ply`, a non-null threat+motif; inaccuracies/good
moves absent; opponent-color leaks absent; an interactive analyze issued mid-run returns
within a bounded wait (asserted).
**Done-condition:** `pytest tests/test_review.py` green (no real binary).
**Deps:** T1, T2, T3, T4.

### T6 — Coaching (template narrator) + profile aggregation
**Owns:** `app/coaching.py` (new), `app/profile.py` (new) + tests.
**Does:** *coaching.py (pure):* `Narrator` protocol + `TemplateNarrator` → leak record →
bucketed foresight text (Threat / Hanging / Plan) + a tendency-cluster name; seam +
`COACH_NARRATOR=template|claude` env (default `template`; **no `anthropic` dep**).
*profile.py:* aggregate SQL over `leaks` (`WHERE my_color IS NOT NULL`) → top categories by
count, by phase/opening/color, the **hope-chess rate** (% games with an allowed-threat leak),
trend buckets by import date; opening cross-link via `openings`/`book`.
**Acceptance:** template narration non-empty + correct for each motif/category; profile
returns ranked categories + habit stat; NULL-color games excluded from counts.
**Done-condition:** `pytest tests/test_coaching.py tests/test_profile.py` green; pure
(coaching) / storage-only (profile), no engine.
**Deps:** T1 (schema/`LeakRecord`), T3 (category names). Parallel with T5.

---

## Phase 3 — API + wiring (serial)

### T7 — API routes, models, lifespan wiring
**Owns:** `app/main.py` (routes + lifespan), `app/models.py` (additions) + API tests.
**Does:** new Pydantic response models; routes (literal before `{id}`): `POST
/api/games/import` (paste text; `my_color` override field), `GET /api/games`, `GET
/api/games/{id}`, `POST /api/games/{id}/analyze`, `GET /api/games/{id}/status`, `GET
/api/games/{id}/review`, `DELETE /api/games/{id}` (cancels job), `GET /api/profile`. Engine
routes keep the 503-on-`EngineUnavailable` pattern. Lifespan: `storage.init()` → folder-scan
`data/games/` (deduped import) → **reset `analyzing`→`pending`** → init job registry (auto-
analyze off by default; trigger via API). Wire the interactive-pending counter into
`/api/move`.
**Acceptance:** import persists + dedups (re-import no-op); list/get/delete work; analyze
starts a job, status polls `pending→analyzing→done`; review returns leaks+plies; profile
returns aggregates; **all existing routes unchanged**; 503 on missing engine.
**Done-condition:** `pytest tests/test_games_api.py` green (ScriptedEngine via `get_engine`
override) + existing 152 green; route order verified.
**Deps:** T1–T6.

---

## Phase 4 — Frontend (serial; single owner of the hotspots)

### T8 — Review tab, replay mode, import + dashboard + foresight
**Owns:** `static/index.html`, `static/app.js`, `static/movelist.js`, `static/review.js`
(new), `static/review.css` (new).
**Does (fixed sub-order):** (1) **Tab shell + library + import** — add `#tab-review` +
`data-tab="review"`, add `'review'` to the panel array (app.js:1837); `review.js` library
list + paste/upload import (calls T7 routes). (2) **Review mode + replay** — load a saved
game's `{baseFen, moves}` into the existing board as a new transient `review` mode (NOT
persisted; follows trap/rep snapshot precedent); **broaden** `movelist.js render()`
(movelist.js:63) + `goto()` (app.js:465) + the board-sync path to accept `review` mode and
emit a `review:ply` event; a **"Return to my game"** button restores the play snapshot
(precedent app.js:1891/1905). (3) **Profile dashboard** — render `GET /api/profile` (top
leaks, hope-chess stat, phase/opening/color, trend). (4) **Foresight cards** — on `review:ply`
show the lead-in warning (Threat/Hanging/Plan buckets) before each leak; at the leak ply tie
back to the earlier warning (de-emphasize the at-blunder scold). Reuse `panel.js`,
`format.js`, injected-`api` pattern; keep modules one-directional.
**Acceptance:** Review tab lists saved games; import works; opening a game enters review mode
+ steps via movelist/←→; foresight card fires on the lead-in ply before the blunder; "Return
to my game" restores the prior session; dashboard shows the profile; existing tabs/modes
unaffected; localStorage shape unchanged.
**Done-condition:** Playwright (trusted mouse) — import a PGN → it appears in the library →
open → step to the lead-in ply (foresight shows) → return restores play; dashboard renders;
**0 console errors**.
**Deps:** T7.

---

## Phase 5 — Integration (serial)

### T9 — Integrate, verify, review, commit
**Owns:** cross-cutting glue (small edits as needed) + the merge.
**Does:** full end-to-end Playwright (import → analyze with **real Stockfish** → replay with
foresight → profile); `pytest` (existing 152 + all new); manual blunder-game sanity (real
engine) confirming a foresight warning lands a move before a real blunder; **verify the
threat narration perspective** — `review.py` threat_motif/hung_square must describe the
OPPONENT's threat against the USER's pieces, not the user's own opportunities (review.py:477-
478 detect from the post-threat side-to-move = the user; confirm/correct the color+perspective
against a real foresight card); independent
**review (maker ≠ checker)** via `cavecrew-reviewer`; remove debug artifacts
(`.playwright-mcp/`, screenshots, `console.log`); confirm `data/games.db` is gitignored and
not committed; **commit** per policy (logical grouping; implemented+verified+reviewed only).
**Acceptance:** every spec Verify-by step passes; review clean; tests green; no user-data
files committed.
**Done-condition:** full spec Verify-by green; `git` commits made (not pushed).
**Deps:** T1–T8.

---

## Parallelization summary
- **Phase 1:** 4 agents (T1 storage, T2 pgn, T3 analysis+motifs, T4 engine) — disjoint files.
  T3 is the heaviest (SEE + motifs); budget it.
- **Phase 2:** 2 agents (T5 pipeline, T6 coaching+profile) — disjoint; both build on T1's
  `LeakRecord` + schema.
- **Phase 3:** T7 alone (sole owner of main.py/models.py) — runs after Phase-1/2 public APIs.
- **Phase 4:** T8 alone (sole owner of app.js/index.html/movelist.js) — frontend can't
  parallelize; done in the fixed sub-order above.
- **Phase 5:** T9 integrator (serial).
- **Safety property:** `main.py`, `app.js`, `index.html`, `engine.py`, `analysis.py` are each
  touched by exactly one ticket.

## Token discipline
Zero LLM tokens shipped this epic (templates only). For BUILD orchestration: Phase 1 can run
as up to 4 parallel agents in isolated worktrees; keep each agent tightly scoped to its owned
files + tests. Default to the smallest model that passes the ticket's tests.
