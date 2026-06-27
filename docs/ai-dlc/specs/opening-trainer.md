# Spec — Opening Trainer (Phase 1)

**Status:** Reviewed (Gate 1 confirmed; refuter pass folded in)
**Slug:** `opening-trainer`
**Type:** Feature on the existing Stockfish Analysis Board app.

## Goal (one line)
Help the user learn opening repertoires: live-detect the opening of the current
line, list the named openings reachable from the current position, and let them
pick one to **step through on the board** with the move order + **why-it's-played
commentary** — all offline/free.

## Locked decisions (Gate 1)
- **Commentary:** bundled curated text (authored once, shipped as data). Offline,
  free, no API key. Covers the major mainstream openings; uncovered positions show
  "no commentary yet."
- **Continuations/name data:** bundled **lichess-org/chess-openings** TSVs (CC0).
  No Lichess Explorer API.
- **Walkthrough:** step through the chosen opening on the **main board** (snapshot
  the user's game, non-destructive — same pattern as setup mode).
- **Scope:** Phase 1 = detection + candidate list + study walkthrough. No Explorer
  popularity stats, no runtime LLM.

## Data sources (bundled, downloaded once)
- `data/openings/{a,b,c,d,e}.tsv` — from
  `https://raw.githubusercontent.com/lichess-org/chess-openings/master/{a..e}.tsv`.
  Columns: `eco`, `name`, `pgn`, `uci`, `epd`. ~3,700 rows. **License CC0** (no
  attribution required). Downloaded by the user (sandbox blocks network) — see
  Verify-by + README.
- `data/commentary.json` — **authored in this repo** (by Claude during the build),
  not downloaded. Format below.

## Matching = EPD, ONE canonical producer (transposition-safe)
All position identity uses **EPD produced by python-chess `chess.Board(...).epd()`**
— the single canonical producer, **server-side only**.
- EPD = piece-placement + side-to-move + castling + en-passant (no clocks). The
  en-passant field is subtle: python-chess emits an ep square **only when an ep
  capture is actually legal**, else `-`. The lichess TSV `epd` column was generated
  with python-chess too, so re-deriving intermediate EPDs with `board.epd()`
  matches it. **Do NOT** derive EPDs by string-stripping a FEN, and **do NOT**
  compute EPDs on the frontend (chessops uses a different ep convention → silent
  mismatches on many Sicilian/French/QGA lines).
- **The client never sends or computes EPDs.** It sends UCI moves and/or full FENs;
  the server derives every EPD via `board.epd()`.
- A test asserts a sampled TSV `epd` equals the value obtained by replaying that
  row's `uci` and calling `board.epd()` (guards the convention end-to-end).
The same position reached by different move orders maps to the same EPD → same
opening/commentary. Pure move-list prefixing is NOT used for identity.

## Backend

### New module `app/openings.py` (loaded once at startup)
On startup, parse the 5 TSVs and build three structures by replaying each row's
`uci` with python-chess and calling `board.epd()` after each half-move:
- `name_by_epd: dict[epd -> (eco, name)]` for **every position along every line**.
  The **deepest** (most plies) opening name wins when multiple rows share an EPD.
- `lines: list[{eco, name, uci, san:[...], epds:[...]}]` — one per TSV row, with the
  ordered EPDs along the line and the SAN move list (for the walkthrough).
- `passes_through: dict[epd -> set[line_id]]` — **reverse index** mapping each EPD
  to the lines whose path includes it. This makes `candidates()` an O(branches)
  dict lookup instead of an O(all-lines) scan per request.
Cost: ~3,700 lines × ~10 plies ≈ 40k board pushes at boot (<1s); memory modest.
Guard: if `data/openings/` is missing/empty, the module loads empty structures and
every opening endpoint returns a well-formed empty (below) + logs one warning —
**startup and the existing board/engine are unaffected** (import must not raise).

### Identification (longest-prefix over the played line) — server derives EPDs
`identify(base_fen, uci_moves) -> {eco, name} | null`: the server replays the line
(`chess.Board(base_fen)` + push each UCI), collecting the EPD after each ply, and
returns the **deepest** ply whose EPD is in `name_by_epd`, falling back to
shallower matches so the name doesn't flicker to null on unnamed in-between
positions. (Client sends `base_fen` + UCI list — it has these; it must NOT send
EPDs.)

### Candidates — reverse-index lookup, deterministic order
`candidates(fen) -> {items:[{eco, name, uci, san:[...]}], truncated: bool}`:
- `epd = chess.Board(fen).epd()`; look up `passes_through[epd]`.
- Include only lines where this EPD is a **non-final** node (the line extends
  beyond the current position).
- **Dedupe by `name`**, keeping the **shortest** line for each name.
- **Order deterministically:** by line length in plies **ascending** (the most
  fundamental/nearest variations first), then `name` alphabetically.
- **Cap at 40**; set `truncated: true` when more existed. The start position
  matches nearly every line → it will always be truncated; the UI gates the list
  behind a **filter box** (server may also accept an optional `q` substring filter
  applied before the cap).

### Commentary — server normalizes FEN→EPD
`commentary(fen) -> {san, text} | null`: `epd = chess.Board(fen).epd()`, then bundled
dict lookup. Client sends a full FEN; the server derives the EPD (never trust a
client EPD).

### Endpoints (additive — do NOT change existing `/api/move|analyze|load`)
- `POST /api/opening` — body `{ baseFen, moves:[uci,...] }` → `{ current: {eco,name}|null, candidates: [...], truncated: bool }`. One call after each move powers live detection **and** the candidate list. **Empty/degraded shape:** `{ current:null, candidates:[], truncated:false }` (never 500).
- `POST /api/opening/commentary` — body `{ fen }` → `{ san, text } | null`. Per walkthrough step.
- Per-step **eval** in the walkthrough reuses the existing `POST /api/analyze` (lazy, only for the step being viewed).
- The frontend's per-move `/api/opening` call is **fire-and-forget and fully
  isolated** from the move/analysis path: it runs after move application, in its own
  try/catch, and a slow/failed/empty response never delays or breaks move handling
  or the eval render.

### Data format — `data/commentary.json`
```json
{
  "<epd>": { "san": "Bb5", "text": "Pins nothing yet but pressures the knight defending e5; the Ruy Lopez aims to..." }
}
```
Keyed by EPD of the position **after** the move `san`. Authored for the main lines
of a curated set (target ~25 openings): Italian, Ruy Lopez (Morphy/Berlin/Closed),
Sicilian (Open/Najdorf), French, Caro-Kann, Scandinavian, Queen's Gambit
(Declined/Accepted), Slav, London, King's Indian, Nimzo-Indian, English, Catalan,
Pirc/Modern. Each commented move: 1–3 sentences on the idea + what it unlocks.

## Frontend

### Opening panel (new section in the right panel)
- **Opening:** live name + ECO of the current line (from `/api/opening.current`),
  updates each move; "—" when unknown.
- **Continue into:** the candidate list (clickable). A filter text box; shows
  count + truncation hint when capped.

### Study mode (FSM addition: `state.mode = 'study'`)
Entry is **only allowed from `mode === 'play'`** (candidate clicks are inert in
setup/study — see re-entrancy). Clicking a candidate:
- **Snapshots** the current game into a **separate `studySnapshot`** variable —
  NOT the setup `playSnapshot` (they must not clobber each other). `studySnapshot`
  captures `{baseFen, moves, cursor}`.
- Loads the chosen line; the stepper indexes the line's **nodes**:
  - **step 0 = the line's initial position** (before its move 1) → no commentary.
  - **step k = after k plies** → commentary lookup uses that node's FEN.
  (Off-by-one guard: commentary is keyed by the EPD of the position *after* a move,
  so step 0 never has commentary; step k maps to the k-th move's commentary.)
- **Stepper:** First / Prev / Next / Last (optional autoplay). The board is
  **view-only**: set `movable.color=undefined`, `draggable.enabled=false`, and
  `onUserMove` early-returns for `mode==='study'`.
- Per step shows: move number + SAN, lazy Stockfish eval (`/api/analyze`, only for
  the current step), and commentary (`/api/opening/commentary`) or "no commentary
  yet."
- **Return to my game** restores `studySnapshot` exactly (mode → play, re-analyze),
  then clears `studySnapshot`.

### Mode transition guards (explicit)
- **Candidate click:** valid only when `mode==='play'`. In `study`, selecting
  another candidate **loads the new line WITHOUT re-snapshotting** (re-entrancy:
  never overwrite `studySnapshot` while already in study, or the real game is lost).
  In `setup`, the opening panel/candidate list is hidden/disabled.
- **Controls in study:** `undo`/`redo`/`reset` already gate on `mode==='play'`
  (stay inert — good). **`flip` is allowed** (orientation only; must not disturb the
  stepper). `Set up position` is disabled in study. Leaving study (Return) is the
  only path back to play.
- The existing setup transitions are unchanged and continue to use `playSnapshot`.

### Persistence
No schema change for Phase 1. Study mode is **transient and never persisted**:
`persist()` must NOT fire for `mode==='study'` (the chessground `change` handler and
any stepper board updates are guarded so a refresh mid-study returns cleanly to the
restored play session, never a half-study state). Existing play/setup persistence
is untouched.

## Constraints
- Additive only: the existing `/api/move|analyze|load`, the engine wrapper, the
  analysis logic, and the play/setup modes must keep working unchanged (their
  tests must still pass).
- Offline/free: no network at runtime; no API keys. TSVs are local files.
- Graceful degradation if opening data files are absent.
- EPD-based identity everywhere (transpositions).
- Frontend stays vanilla JS + chessground/chessops; commentary text is escaped
  (no HTML injection) when rendered.
- Commentary is original text authored in-repo (no copied copyrighted prose).

## Out of scope (Phase 1)
Lichess Explorer popularity/win-loss stats; runtime LLM commentary; Wikibooks
fetch; spaced-repetition/training drills; saving favorite repertoires; per-move
commentary for every ECO line (curated subset only); a second preview board.

## Verify-by (end-to-end)
1. User downloads the 5 TSVs into `data/openings/` (documented command).
2. `pytest` — new `tests/test_openings.py` passes (name detection incl. a
   transposition case; candidate lookup; commentary lookup; missing-data
   degradation) **without** a Stockfish binary.
3. Boot server; play `1.e4 e5 2.Nf3 Nc6 3.Bb5` → panel shows a non-null Ruy
   Lopez name + ECO (assert against the **actual TSV row** for that EPD, not a
   hardcoded string); reach the same position via `1.Nf3 Nc6 2.e4 e5 3.Bb5`
   (transposition) → **identical** name/ECO.
4. Candidate list shows named variations reachable from the current position;
   typing in the filter narrows it.
5. Click a candidate → board steps through its line; Prev/Next works; eval +
   commentary show for commented moves; "Return to my game" restores the exact
   prior line and re-analyzes.
6. Existing tests (`test_analysis.py`, `test_api.py`) still pass.
7. With `data/openings/` removed, the app still boots and plays; opening panel
   shows "—" / empty, no crash.
