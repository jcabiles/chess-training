# Tickets — Opening Traps (Phase 1)

Spec: `docs/ai-dlc/specs/opening-traps.md`. Research: `docs/ai-dlc/research/opening-traps.md`.
`[P]` = parallelizable once deps met. One owner per file. Hotspots: `app/main.py`
(TT3), `static/app.js` (TT4 → TT5 → TT6, same owner → **sequential**). All EPDs
derived server-side via python-chess `board.epd()` (same convention as the opening
trainer). Build on the opening-trainer module/FSM patterns.

---

### TT1 — Author + engine-verify `data/traps.json`
**Do:** author `data/traps.json` from the research notes — the engine-verified subset
of the 14 candidate traps (expect ~10–12 to survive; cut the 2/5-commonness ones
first). Use the spec's per-ply schema (`leadInSan`, `startFen`, `variations[].mainLine`
with `side`/`dubious`/`bait`/`forced`/`result`/`note`, and `refutation` on the bait
ply). Original prose only. **Every line must pass the python-chess checks in
`test_traps_data.py`** — fix or DROP any line whose claimed mate/material/legality
fails (the research flagged BSG `Nf3#`, Stafford `Qa3#`, Englund as suspect). Encode
ep as the target square (`g4f3`) and underpromotion with the piece suffix (`f2g1n`).
**Owns:** `data/traps.json`, `tests/test_traps_data.py`.
**Done:** `pytest tests/test_traps_data.py` passes with **no Stockfish** (pure
python-chess): all `mainLine` moves legal in order; exactly one `bait` per variation;
`Board(startFen).epd()` == EPD after `leadInSan`; `refutation.uci` legal at the bait
ply and `declineSan[]` legal replayed from after `refutation.uci`; every
`result:"checkmate"` is `board.is_checkmate()`; material-win deltas hold. ≥10 traps
shipped, both colors represented.
**Deps:** none. (Stockfish eval-framing sanity for `dubious`/`engineNote` is a soft
check done during TT8 in the user's terminal; the hard gate here needs no binary.)

### TT2 — `traps.py` loader + lookups + tests `[P]`
**Do:** `app/traps.py` mirroring `app/openings.py` — module-level singletons +
`init(path)`; parse/validate `traps.json`; build `traps_by_id`, `summaries`
(ordered commonness desc then name asc), and `start_epd_index` (epd → [id] from
replaying each `leadInSan`). Malformed traps dropped with a warning; missing/invalid
file → empty structures, no raise.
**Owns:** `app/traps.py`, `tests/test_traps.py`, `tests/fixtures/traps_sample.json`.
**Done:** `pytest tests/test_traps.py` passes with **no Stockfish + no real data**
(fixture JSON). Covers: summaries ordering, `start_epd_index` lookup incl. a
**transposition** into a trap, lookup by id, malformed-trap drop, missing/invalid
file → empty + no raise.
**Deps:** none (fixture-based). Parallel with TT1.

### TT3 — Traps API endpoints
**Do:** add to `main.py` (wiring `traps.py` + `TRAPS_FILE` + `traps.init()` in
`lifespan` after `openings.init`): `GET /api/traps` → `{traps:[summary]}`;
`POST /api/traps/check` ({baseFen,moves} → {available:[summary]}) **registered BEFORE**
`GET /api/traps/{id}` (→ full trap | 404) so the literal path isn't shadowed. Add
request models to `models.py`. Additive only; existing routes untouched; degraded
shapes on missing data (`{traps:[]}` / `{available:[]}` / 404) — never 500.
**Owns:** `app/main.py`, `app/models.py`, `tests/test_traps_api.py`.
**Done:** `pytest tests/test_traps_api.py` passes (TestClient): list shape;
`/check` returns the Stafford trap after `1.e4 e5 2.Nf3 Nf6 3.Nxe5 Nc6` and `[]` from
start; `{id}` hit + 404; **route-order test** proving `/api/traps/check` isn't matched
as `{id}`. Existing `test_api.py` + `test_opening_api.py` still green.
**Deps:** TT2 (uses TT1's real data at runtime; tests can use the TT2 fixture).

### TT4 — Frontend: Traps section + live chip
**Do:** `index.html`/`style.css`/`app.js` — `#traps-section` (browse `#traps-list`
from `GET /api/traps`, `#traps-name-filter`, `#traps-color-filter`). Add
`refreshTrapsAvailable()`: fire-and-forget `POST /api/traps/check` **only in
`mode==='play'`**, token-guarded (like `studyEvalToken`), run **after**
`refreshOpening()` resolves; show/hide dismissible `#trap-chip` (sticky dismiss per
position). List-item and chip clicks call `enterTrap(id)` (implemented in TT5).
Escaped text only; isolation — a slow/failed `/check` never blocks moves/eval.
**Owns:** `static/index.html`, `static/style.css`, `static/app.js`.
**Done:** in-browser (Playwright): Traps list populates + both filters narrow it;
playing `1.e4 e5 2.Nf3 Nf6 3.Nxe5 Nc6` shows the chip; dismiss is sticky; killing
`/check` leaves moves/eval working. (`enterTrap` may no-op/log until TT5.)
**Deps:** TT3. (Owns `app.js` → before TT5.)

### TT5 — Frontend: trap-watch mode
**Do:** `app.js` (+ `#trap-bar` markup/styles) — `mode==='trap-watch'` (view-only,
like `study`): `enterTrap(id)` snapshots the game into `studySnapshot` (play-mode
only; re-entrancy: re-select doesn't re-snapshot), loads the trap via
`GET /api/traps/{id}`, First/Prev/Next/Last over the variation's plies; per-step eval
(`/api/analyze`, `quality` absent) + ply `note`; bait ply also shows
`refutation.note` + `assessment`; `#trap-return` restores the snapshot + re-analyzes.
Persist guards: no persist in trap modes; flip allowed.
**Owns:** `static/app.js`, `static/index.html`, `static/style.css`.
**Done:** in-browser (Playwright): from the list/chip → board steps the line, eval +
notes render, bait ply shows refutation+assessment, Return restores the exact prior
game; refresh mid-watch returns to play cleanly.
**Deps:** TT3, TT4 (same `app.js` owner → after TT4).

### TT6 — Frontend: trap-practice mode
**Do:** `app.js` (+ practice controls in `#trap-bar`) — `mode==='trap-practice'` with
its own board config (`movable.color=trap.color`, `movable.dests` = only the current
trapper move) and `onTrapMove(orig,dest,promo?)` per spec: correct → apply + auto-play
victim/bait reply + advance; wrong → snap back + "try again"; promotion via existing
`askPromotion()` (wrong piece = wrong); `#trap-reveal` hint; `#trap-show-refutation`
previews the decline line; legality via `/api/move` but **discard `analysis.quality`**
(no quality label in trap mode — show eval + `note` + `engineNote` on `dubious`).
Watch⇄practice toggle (`#trap-mode-toggle`) **resets to step 0 (`startFen`)**.
**Owns:** `static/app.js`, `static/index.html`, `static/style.css`.
**Done:** in-browser (Playwright): board offers only the trapper move; playing it
auto-replies; wrong move + wrong promotion rejected/taken back; Reveal advances; trap
reaches `result`; show-refutation previews decline; no "Blunder!" label on dubious
moves; toggle resets to step 0.
**Deps:** TT5 (same owner → after TT5).

### TT7 — Docs `[P]`
**Do:** README — Traps section usage (browse / watch / practice / live chip) + note
the bundled `data/traps.json` (engine-verified, original prose). Update CLAUDE.md
"What this is" one-liner to mention traps (keep terse).
**Owns:** README traps section, CLAUDE.md one-liner.
**Deps:** none for drafting; finalize after TT6.

### TT8 — End-to-end verification
**Do:** run the spec Verify-by 1–8 against the running app with full data (user's
terminal + Playwright); run the **Stockfish eval-framing sanity** on `dubious`/
`engineNote` claims from TT1 and correct any misleading note; fix integration gaps.
**Owns:** no new source; verification + small fixes.
**Done:** all Verify-by steps pass; full `pytest` green; existing play/setup/
opening-study features unaffected.
**Deps:** TT1, TT3, TT4, TT5, TT6.

---

## DAG / waves
- **Wave 1:** TT1 (author+verify data) ‖ TT2 (module) ‖ TT7-draft.
- **Wave 2:** TT3 (needs TT2).
- **Wave 3:** TT4 (needs TT3).
- **Wave 4:** TT5 (needs TT3+TT4; same `app.js` owner → after TT4).
- **Wave 5:** TT6 (needs TT5; same owner → after TT5).
- **Wave 6:** TT8 (needs TT1+TT3+TT4+TT5+TT6), then finalize TT7.

Note: TT1/TT2/TT3 are python-chess/TestClient-verifiable **in-sandbox** (no Stockfish,
no live server). TT4/TT5/TT6/TT8 need the live server + browser (run in the user's
terminal; verify via Playwright) and TT1's data. The three `app.js` tickets share one
owner and run strictly sequentially to avoid clobbering.
