# Tickets — Opening Trainer (Phase 1)

Spec: `docs/ai-dlc/specs/opening-trainer.md`. `[P]` = parallelizable once deps met.
One owner per file. Hotspots: `app/main.py` (T4), `static/app.js` (T5 → T6, same
owner → sequential). All EPDs derived server-side via python-chess `board.epd()`.

---

### T1 — Acquire + commit opening data
**Do:** download the 5 lichess TSVs into `data/openings/{a,b,c,d,e}.tsv`
(`https://raw.githubusercontent.com/lichess-org/chess-openings/master/{a..e}.tsv`).
CC0 → commit them into the repo (self-contained). Document the download command in
README + a one-line CC0 note.
**Owns:** `data/openings/*.tsv`, README data section.
**Done:** 5 files present; combined row count ≈ 3,700; header is `eco<TAB>name<TAB>pgn<TAB>uci<TAB>epd`.
**Deps:** none. (User runs the download — sandbox blocks network.)

### T2 — `openings.py` loader + lookups + tests `[P]`
**Do:** `app/openings.py` — parse TSVs; build `name_by_epd` (deepest wins),
`lines`, and the `passes_through` reverse index using `chess.Board(...).epd()` after
each ply. Pure functions: `identify(base_fen, uci_moves)`, `candidates(fen, q=None)`
(dedupe-by-name keep-shortest, order by plies asc then name, cap 40 + `truncated`),
`commentary_lookup(fen)`. Import-safe + empty structures if data dir missing.
**Owns:** `app/openings.py`, `tests/test_openings.py`, `tests/fixtures/openings_sample.tsv`.
**Done:** `pytest tests/test_openings.py` passes with **no Stockfish + no full data**
(uses the fixture TSV). Covers: name detection, **transposition** (1.e4 e5 2.Nf3 vs
1.Nf3 … same name), **epd-convention assertion** (replayed `board.epd()` == TSV
`epd` for sampled rows), candidate dedupe/order/cap, commentary hit + miss,
missing-data degradation (empty, no raise).
**Deps:** none (fixture-based). Parallel with T3.

### T3 — Author bundled commentary `[P]`
**Do:** `data/commentary.json` — original authored "why" text for the main lines of
~25 mainstream openings (Italian, Ruy Lopez mains, Sicilian Open/Najdorf, French,
Caro-Kann, Scandinavian, QGD/QGA, Slav, London, KID, Nimzo, English, Catalan,
Pirc/Modern). Keyed by **EPD of the position after the move**; value `{san, text}`
(1–3 sentences, original prose — no copied copyrighted text).
**Owns:** `data/commentary.json`, `tests/test_commentary_data.py`.
**Done:** valid JSON; schema test passes (every value has non-empty `san` + `text`);
every key parses as a legal EPD; ≥ ~120 commented positions across the listed set.
**Deps:** none. Parallel with T2. (Keys verified against `board.epd()` so they match
T2's producer.)

### T4 — Opening API endpoints
**Do:** add `POST /api/opening` ({baseFen, moves} → {current, candidates, truncated})
and `POST /api/opening/commentary` ({fen} → {san,text}|null) to `main.py`, wiring
`openings.py`. Additive only; existing routes untouched. Degraded shape on missing
data (`{current:null,candidates:[],truncated:false}`; commentary `null`) — never 500.
**Owns:** `app/main.py`, `tests/test_opening_api.py`.
**Done:** `pytest tests/test_opening_api.py` passes (TestClient): identify via
baseFen+moves, candidate list shape + cap/filter, commentary hit/miss, degraded
empties. `pytest test_api.py` (existing) still green.
**Deps:** T2, T3.

### T5 — Frontend: opening panel (detection + candidates)
**Do:** `index.html` + `style.css` + `app.js` — an "Opening" panel section: live
name/ECO (from `/api/opening.current`), a **filterable** candidate list. The
`/api/opening` call is **fire-and-forget** after move/load (own try/catch, never
blocks or breaks move/eval). Renders escaped text only.
**Owns:** `static/index.html`, `static/style.css`, `static/app.js`.
**Done:** in-browser (Playwright): play 1.e4 e5 2.Nf3 Nc6 3.Bb5 → name shows;
candidate list populates + filter narrows; killing the opening endpoint still leaves
moves/eval working (isolation).
**Deps:** T4. (Owns `app.js` — must finish before T6.)

### T6 — Frontend: study walkthrough
**Do:** `app.js` (+ `index.html`/`style.css` for stepper UI) — `state.mode='study'`
with a **separate `studySnapshot`**; candidate click (play-mode only) enters study;
First/Prev/Next/Last stepper; board view-only; per-step eval (`/api/analyze`) +
commentary (`/api/opening/commentary`); "Return to my game" restores snapshot.
Transition + re-entrancy + persist guards per spec (no persist in study; flip
allowed; re-select in study doesn't re-snapshot).
**Owns:** `static/app.js`, `static/index.html`, `static/style.css`.
**Done:** in-browser (Playwright): click a candidate → board steps the line, eval +
commentary update, Return restores the exact prior game; entering study from a
mid-game line and returning is lossless; refresh mid-study returns to play cleanly.
**Deps:** T4, T5 (same owner as T5 → sequential).

### T7 — Docs `[P]`
**Do:** README — opening-trainer usage + the T1 download command + CC0 attribution
note. Update CLAUDE.md "What this is" one-liner if warranted (keep terse).
**Owns:** README opening section (coordinate w/ T1), CLAUDE.md one-liner.
**Deps:** none for drafting; finalize after T6.

### T8 — End-to-end verification
**Do:** run the spec Verify-by 1–7 against the running app with full data; fix
integration gaps.
**Owns:** no new source; verification + small fixes.
**Done:** all Verify-by steps pass; full `pytest` green; existing features unaffected.
**Deps:** T1, T4, T5, T6.

---

## DAG / waves
- **Wave 1:** T1 (data, user-run) ‖ T2 ‖ T3 ‖ T7-draft.
- **Wave 2:** T4 (needs T2+T3).
- **Wave 3:** T5 (needs T4).
- **Wave 4:** T6 (needs T4+T5; same `app.js` owner as T5 → after T5).
- **Wave 5:** T8 (needs T1+T4+T5+T6), then finalize T7.

Note: T2/T4 are fixture/TestClient-verifiable in-sandbox; T5/T6/T8 need the live
server + browser (run in user's terminal; verify via Playwright) and T1's full data.
