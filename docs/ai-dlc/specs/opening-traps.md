# Spec — Opening Traps (Phase 1)

**Status:** Reviewed (Gate 1 confirmed; refuter pass folded in — 3 blockers, 5 major, 3 minor)
**Slug:** `opening-traps`
**Type:** Feature on the existing Stockfish Analysis Board app, building on the
opening trainer (`docs/ai-dlc/specs/opening-trainer.md`).

## Goal (one line)
Let the user learn **opening traps** — deep, multi-move lines where you deliberately
play engine-dubious moves to bait a novice into a losing reply — by browsing a
**Traps section**, drilling each trap in **watch or practice mode** on the main
board with trap-aware commentary, and getting a **live "trap available" nudge**
when a real game reaches a trap's starting position. All offline/free.

## Locked decisions (Gate 1)
- **Two modes per trap:** *watch* (passive step-through) + *practice* (interactive —
  app plays the opponent's expected reply, you must find the punishment), toggled.
- **Eval stays on, trap-aware:** keep the eval bar; on the trapper's deliberately
  dubious moves, show the real eval **plus** a note on why it scores in practice.
  Do **not** flash the existing "Mistake/Blunder" quality label without that context.
- **Three-part lines:** lead-in → trap + punishment (if opponent takes the bait) →
  a short sound continuation (if the opponent refutes correctly).
- **Placement:** a new **Traps section** to browse/drill, **plus** a live suggestion
  when the live game hits a trap's start position.
- **Offline/free:** new bundled `data/traps.json`; no network/LLM at runtime.

## Data source — `data/traps.json` (authored in-repo, engine-verified)
Not downloaded — authored during the build from the research notes
(`docs/ai-dlc/research/opening-traps.md`) and **engine-verified** before shipping.

### Format
```json
{
  "traps": [
    {
      "id": "stafford-gambit-greedy",
      "name": "Stafford Gambit",
      "color": "black",                 // side that springs the trap
      "eco": "C42",
      "parentOpening": "Petrov Defense",
      "commonness": 3,                  // 1–5, how often a beginner reaches it
      "engineNote": "Objectively ~+0.6 for White with 4.Nc4!, but the practical traps score brilliantly below 2000.",
      "startFen": "<FEN at the drill start>",
      "leadInSan": ["e4","e5","Nf3","Nf6","Nxe5","Nc6"],   // moves from initial pos → startFen (context)
      "variations": [
        {
          "label": "4.Nxc6?? (greedy capture)",
          "mainLine": [
            {"san":"Nxc6","uci":"e5c6","side":"victim","bait":true,
              "note":"Natural-looking: grab the free knight.",
              "refutation":{"san":"Nc4","uci":"e5c4",
                "note":"Correct — retreat the knight; declines the whole gambit.",
                "declineSan":["Nc4","Nxe4","Nc3"],
                "assessment":"White is slightly better (~+0.5)."}},
            {"san":"dxc6","uci":"d7c6","side":"trapper"},
            {"san":"e5","uci":"e4e5","side":"victim"},
            {"san":"Ne4","uci":"f6e4","side":"trapper","dubious":false,
              "note":"The knight isn't kicked — it attacks."}
            /* … to mate or decisive material; result on the final ply … */
          ]
        }
      ]
    }
  ]
}
```
Per-ply fields: `san` + `uci` (required), `side` (`"trapper"`|`"victim"`),
optional `dubious` (bool, trapper's engine-poor-but-practical move), `bait` (bool,
the victim's expected mistake), `forced` (bool), `result`
(`"checkmate"`|`"wins-piece"`|`"wins-queen"`|… on the last ply), `note` (teaching
prose). The `bait` ply also carries `refutation: {san, uci, note, declineSan[],
assessment}` — the correct move + the short sound continuation.

**Bait cardinality (refuter #8):** each variation has **exactly one** `bait` ply —
the single branch point where the victim chooses fall-in vs refute. A trap with
more than one distinct "wrong reply worth teaching" is modeled as **separate
`variations`** (or separate trap entries), not multiple baits in one line. This
keeps practice-mode branching trivial (one decision point per drill).

**`startFen` is authoring-only (refuter #5):** runtime matching uses the EPD from
replaying `leadInSan` — `startFen` is a human-readable reference, NOT used for
matching. `test_traps_data.py` asserts `chess.Board(startFen).epd()` equals the EPD
after replaying `leadInSan` (both directions) so a wrong `startFen` can't pass silently.

**`declineSan[]` anchor (refuter #6):** the decline continuation is played **from the
position after `refutation.uci` is applied at the bait ply** — i.e. push the main
line up to (not including) the bait ply, then push `refutation.uci`, then each
`declineSan` move. `test_traps_data.py` replays it exactly this way to assert legality.

**UCI encoding for special moves (refuter #8):** `uci` must be python-chess-legal —
- **En-passant** (e.g. Fishing Pole): the **target** square, `g4f3` (not `g4g3`);
  legal only when the immediately preceding ply is the matching two-square pawn push,
  so ply ordering must preserve the ep right.
- **Promotion / underpromotion** (e.g. Lasker `fxg1=N`): include the piece suffix,
  `f2g1n` (knight), `e7e8q`, etc.
`test_traps_data.py` catches any malformed special move by replaying with python-chess.

Rationale: a **flat per-ply array with branch data on the single bait ply** models
"trap + punishment + short decline note" without a full game tree. Traps with
multiple opponent tries (Stafford) use multiple `variations`. Commentary lives
**inline** per ply (a trap context needs different prose than the opening trainer),
so `data/commentary.json` is untouched.

## Matching = EPD, server-derived (same discipline as the opening trainer)
Trap **start** positions are matched by **EPD from python-chess
`chess.Board(...).epd()`**, server-side only — identical convention to the opening
trainer so transpositions into a trap are detected. The client sends `baseFen` +
UCI moves (or a FEN); the server derives EPDs. The client never computes EPDs.

## Backend

### New module `app/traps.py` (loaded once at startup)
Mirrors `app/openings.py`: module-level singletons + an `init(path)` wrapper, no
state on `app.state` (refuter #11). Parse + validate `data/traps.json`; build:
- `traps_by_id: dict[id -> trap]`.
- `summaries: list[{id,name,color,eco,parentOpening,commonness}]` (browse list),
  deterministically ordered (commonness desc, then name asc).
- `start_epd_index: dict[epd -> [id]]` — reverse index from each trap's **start
  position EPD** (derived by replaying `leadInSan`) to trap ids, for live detection.
**Import-safe:** missing/invalid/empty file → empty structures + one warning;
startup, the engine, the board, and the opening trainer are unaffected (no raise).
A schema/consistency check at load drops malformed traps with a logged warning
rather than failing the whole app.

**Lifespan wiring (refuter #11):** in `app/main.py`, add
`TRAPS_FILE = BASE_DIR / "data" / "traps.json"` and call `traps.init(str(TRAPS_FILE))`
in the `lifespan` context manager, right after `openings.init(...)`. Same
load-once-at-boot pattern; degrades to empty on absence.

### Endpoints (additive — do NOT change existing routes)
**Route ordering (refuter #2):** the literal-path route is named `check` (not
`available`) and is registered **before** the `{id}` route so FastAPI never matches
`/api/traps/check` as `{id}="check"`.
- `GET /api/traps` → `{ traps: [summary,...] }` (browse list; empty list if no data).
- `POST /api/traps/check` — body `{ baseFen, moves:[uci,...] }` →
  `{ available: [summary,...] }`: replay server-side, derive the current EPD, return
  traps whose start EPD matches. Empty/degraded shape `{ available: [] }` (never 500).
  **Registered before `/api/traps/{id}`.**
- `GET /api/traps/{id}` → the full trap object, or `404` if unknown.
- Per-step **eval** reuses the existing `POST /api/analyze` (lazy, current step only).
- The live `/api/traps/check` call is **fire-and-forget and isolated** from the
  move/analysis path (own try/catch; a slow/failed/empty response never delays or
  breaks move handling or eval render) — same rule as the opening trainer's
  `/api/opening` call.

## Frontend

### Traps section (new block in the right panel)
- A **browse list** of trap summaries (name · ECO · color-to-move · commonness),
  with a filter box (by name/opening) and a color filter (White/Black/all).
- Clicking a trap enters **trap study** in **watch** mode (see below).

### New HTML elements (refuter #10 — pin IDs so the build is unambiguous)
All additive to `static/index.html`; hidden via the existing `[hidden]{display:none}`
rule. Required IDs:
- `#traps-section` — container; `#traps-list` (the browse list), `#traps-name-filter`
  (text), `#traps-color-filter` (select White/Black/all).
- `#trap-bar` — the trap study bar (analogous to the opening trainer's `.study-bar`),
  containing: `#trap-title`, `#trap-mode-toggle` (Watch ⇄ Practice),
  `#trap-stepper` (First/Prev/Next/Last, watch only), `#trap-move` (SAN+eval),
  `#trap-note` (commentary / bait refutation + assessment), `#trap-reveal`
  (practice hint), `#trap-show-refutation` (practice), `#trap-feedback`
  (correct/try-again line), `#trap-return` (Return to my game).
- `#trap-chip` — the dismissible live "Trap available" nudge in play mode.

### Two new FSM states (refuter #1 — practice is NOT an extension of read-only study)
The current FSM is `play`/`setup`/`study`, where `study` is strictly view-only
(`movable.color=undefined`, `onUserMove` early-returns). Trap drilling adds **two**
states so the read-only guard isn't punctured ad hoc:
- **`mode === 'trap-watch'`** — view-only, exactly like `study`: First/Prev/Next/Last
  over the variation's plies; `movable.color=undefined`, `draggable.enabled=false`,
  `onUserMove` early-returns. Each step shows SAN, lazy eval (`/api/analyze`), and the
  ply `note`; the bait ply also surfaces `refutation.note` + `assessment`.
- **`mode === 'trap-practice'`** — interactive; its own board config and input path
  (below). The existing `study`/`setup`/`play` handlers are untouched.

Entry (from `mode==='play'` only) **snapshots the game into `studySnapshot`**
(non-destructive; same re-entrancy rule — picking another trap while already in a
trap loads it **without** re-snapshotting). **Return to my game** (`#trap-return`)
restores `studySnapshot` exactly (mode → play, re-analyze), then clears it.

### Practice-mode input contract (refuter #3 — `onTrapMove`)
In `trap-practice`, chessground is configured for the trapper only:
`movable.color = trap.color`, `draggable.enabled = true`, and `movable.dests`
contains **only** the single legal trapper move for the current ply (computed from
`mainLine[ply].uci` via chessops) — so the user physically cannot move the victim's
pieces or play out of turn. On drop, `onTrapMove(orig, dest, promo?)`:
1. Build the attempted UCI (`orig+dest`, plus promotion suffix from the promotion
   picker if the move is a promotion).
2. **Correct** (`=== mainLine[ply].uci`): apply it; then the app **auto-plays the
   victim reply** (`mainLine[ply+1]`, including the `bait` move) after a short beat;
   advance `ply += 2`; show positive `#trap-feedback`.
3. **Wrong:** snap the piece back with `ground.set({fen: currentFen})` (same takeback
   pattern as the existing illegal-move path), show "not quite — try again" in
   `#trap-feedback`; do **not** advance.
4. **Promotion plies** (e.g. Lasker `f2g1n`): reuse the existing `askPromotion()`
   picker; if the chosen piece's suffix ≠ the expected UCI suffix, treat as **wrong**
   (snap back, try again) — the picker does not auto-reopen.
5. At the **bait ply** the auto-played victim move is the `bait`, so the user then
   finds the punishment. `#trap-show-refutation` swaps the view to the decline line
   (`refutation` + `declineSan`) + `assessment` as a read-only preview; it does not
   change the drill position.
6. At a `result` ply, show a completion state; Reveal (`#trap-reveal`) plays the next
   expected trapper move for a stuck user.

**Watch ⇄ practice toggle (refuter #9):** switching to practice **always resets to
the variation's step 0 (`startFen`)** — never mid-line — so the played/unplayed ply
boundary is unambiguous.

### Eval display in trap mode (trap-aware) (refuter #4)
The eval bar stays visible in both trap states; the **move-quality label is never
shown** in trap mode:
- **Watch:** per-step eval comes from `POST /api/analyze`, which returns `quality:null`
  (no prior move supplied) — so the label is already absent. Render eval + ply `note`.
- **Practice:** moves are validated for legality via the existing `POST /api/move`,
  but the client **discards `analysis.quality`** from that response and renders only
  the eval bar + the ply `note` (so a real "Blunder!" label from the engine never
  contradicts the lesson on a `dubious` move). On `dubious` plies, show the eval
  together with the ply `note` and the trap's `engineNote`.

### Live trap suggestion (in play mode only) (refuter #7)
A `refreshTrapsAvailable()` fire-and-forget call to `POST /api/traps/check`, **fired
only in `mode==='play'`** (never in trap-watch/trap-practice/study/setup). It is
guarded by its own cancellation token (analogous to `studyEvalToken`) so a stale
response from a superseded position can't render, and it runs **after** the existing
`refreshOpening()` call resolves (sequential, not a second parallel burst per move).
If the current position is a trap start, show the dismissible `#trap-chip`
("Trap available: Stafford Gambit — drill it"); clicking enters trap-watch for that
trap (snapshotting the game); dismissing is sticky for that position. The chip never
blocks or delays play.

## Constraints
- **Additive only:** existing routes (`/api/move|analyze|load|opening*`), the engine
  wrapper, analysis logic, and play/setup/opening-study modes keep working unchanged
  (their tests still pass).
- **Offline/free:** no runtime network or API keys; `traps.json` is a local file.
- **Graceful degradation** if `data/traps.json` is absent/invalid (empty Traps
  section, no live chips, no crash).
- **EPD-based identity** (transpositions into traps detected), server-derived only.
- Vanilla JS + chessground/chessops on the frontend; all trap text is **escaped**
  on render (no HTML injection).
- **Original prose** authored in-repo (no copied copyrighted analysis); trap/opening
  names are standard chess terminology (fine to use).
- **Every shipped line is engine-verified** (see Verify-by) — no wrong chess.

## Out of scope (Phase 1)
Cross-session progress tracking / spaced-repetition drilling; a "human-like"
opponent engine beyond the scripted bait/reply; exhaustive coverage of every trap
variation; a trap-authoring UI; importing traps from external APIs; a second board;
difficulty grading beyond the `commonness` field.

## Verify-by (end-to-end)
1. **Data integrity (no Stockfish needed):** `tests/test_traps_data.py` replays every
   variation with python-chess and asserts:
   - all `mainLine` moves legal in order (catches bad ep/underpromotion UCI);
   - **exactly one** `bait` ply per variation;
   - `chess.Board(startFen).epd()` == the EPD after replaying `leadInSan` (both
     directions, so a wrong `startFen` can't pass);
   - `refutation.uci` is legal at the bait ply, and the `declineSan[]` continuation is
     legal **when replayed from the position after `refutation.uci`** (main line up to
     the bait ply → `refutation.uci` → each `declineSan`);
   - every `result:"checkmate"` ply satisfies `board.is_checkmate()`; material-win
     results reach the claimed material delta.
   **Any line that fails is fixed or dropped before merge** (the research agents
   flagged BSG `Nf3#` / Stafford `Qa3#` / Englund as needing exactly this check).
2. **Module degradation:** `tests/test_traps.py` — summaries ordering, `start_epd_index`
   lookup incl. a **transposition** into a trap, unknown-id `404`, and missing/invalid
   `traps.json` → empty structures, app still boots.
3. **API:** `GET /api/traps` lists the shipped traps; `GET /api/traps/{id}` returns a
   full trap; `POST /api/traps/check` returns the Stafford trap when the line
   `1.e4 e5 2.Nf3 Nf6 3.Nxe5 Nc6` is played, and `[]` from the start position; and
   `GET /api/traps/check` is NOT shadowed by `/api/traps/{id}` (route-order test).
4. **Browser — watch (`trap-watch`):** open a trap → step through; eval + notes render;
   the bait ply shows the refutation + assessment; "Return to my game" restores the
   prior line and re-analyzes.
5. **Browser — practice (`trap-practice`):** the board offers only the trapper's move;
   playing it auto-replies with the victim/bait move; a wrong move is rejected + taken
   back; a wrong promotion piece is rejected; Reveal works; the trap reaches its
   `result`; "show refutation" previews the decline line. Dubious moves show eval
   **without** any quality label (trap note shown instead). Toggling watch→practice
   resets to step 0.
6. **Browser — live nudge:** in play mode, reaching a trap's start position shows the
   `#trap-chip`; clicking it drills that trap; dismissing it is sticky for that
   position; the chip never appears in trap/study/setup modes.
7. **Regression:** `pytest` green for `test_analysis.py`, `test_api.py`,
   `test_openings.py`, `test_opening_api.py`, `test_commentary_data.py`; existing
   play/setup/opening-study modes unchanged in the browser.
8. With `data/traps.json` removed, the app boots and plays; Traps section empty, no
   chips, no crash.
