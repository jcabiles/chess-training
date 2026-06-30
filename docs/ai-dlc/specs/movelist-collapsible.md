# Delta Spec — Collapsible + Height-Capped Move List (Analysis tab)

## Problem (why)
The Analysis tab is a **single scrolling flex column** (`.panel > [role=tabpanel]`,
`overflow-y:auto`), ordered: Evaluation → Last move → Best move/line → **Moves**. `#move-list`
has **no height cap** (`static/movelist.css:3`) so it grows with the game. To see recent moves
you scroll down, which pushes the **Evaluation number / Best move** off the top of the panel —
you can't see the eval and the latest moves at once. (The eval *bar* beside the board stays
visible; the eval *number* + best-move text are what scroll away.)

## Goal (one line)
Keep the eval visible regardless of game length: **cap the move list's height with an internal
scrollbar**, and let the user **collapse/expand** the list from its "Moves" header (state
remembered across reloads).

## Locked decisions
- **Both** shapes: (a) `#move-list` gets `max-height` + `overflow-y:auto` (~10–12 plies, then
  scrolls inside itself); (b) the "Moves" header becomes a collapse toggle.
- **Default = expanded.** Collapsing is opt-in.
- **Persist** collapsed state — but in a **separate UI-prefs key** `chess-training:ui:v1`
  (shape `{ moveListCollapsed: boolean }`), **NOT** the game-session object. Rationale:
  `persist()` (`app.js:108`) is mode-specific and early-returns in review/trap/rep modes, so a
  flag there would be dropped in those modes; a standalone key is mode-independent and additive.
- **Active move stays visible**: `render()` already calls
  `active.scrollIntoView({block:'nearest'})` (`movelist.js:112`) — once `#move-list` is the
  scroll container, `block:'nearest'` scrolls the **innermost** scrollable ancestor (the capped
  list), not the outer tabpanel. That's the win. **Do NOT change the `{block:'nearest'}`
  argument** (refuter [med]) — `center`/`start` or adding `scroll-padding` would scroll the outer
  panel. When collapsed (`#move-list` is `display:none`), `scrollIntoView` is a harmless no-op
  (refuter [low]) — no guard needed.
- Deterministic, frontend-only — **no backend, no new deps, no LLM.**

## In scope
### Shared contract (selectors all three files agree on)
- Header element id: **`#movelist-toggle`** — a `<button>` (keyboard + `aria-expanded` for free)
  replacing the plain `<div class="eval-label">Moves</div>`, holding the "Moves" text + a chevron.
- Collapsed class: **`.movelist-block.collapsed`** on the existing `.movelist-block` wrapper
  (`index.html:189`) — mirrors the repertoire collapse pattern (`.collapsed { display:none }`,
  `style.css:825,860`).
- UI-prefs storage key: **`chess-training:ui:v1`**.

### `static/index.html` (the `.movelist-block`, ~line 189–192)
- Replace `<div class="eval-label">Moves</div>` with a header **button**:
  `<button id="movelist-toggle" class="movelist-toggle eval-label" aria-expanded="true"
  aria-controls="move-list">Moves <span class="movelist-chevron" aria-hidden="true">…</span></button>`
  (chevron via CSS/inline SVG or a unicode glyph consistent with the app's iconography).

### `static/movelist.css`
- `#move-list`: add `max-height` (token-derived, ~`18rem` / enough for ~10–12 plies) +
  `overflow-y:auto`. Keep existing top border/padding.
- `.movelist-block.collapsed #move-list { display:none; }` (and hide the top border gap when
  collapsed so it reads tidy).
- `.movelist-toggle`: full-width flex header (text + chevron pushed right). Because `.eval-label`
  (`style.css:459`) sets `margin-bottom` and assumes a block element, and a `<button>` carries UA
  styles, **enumerate the full reset** (refuter [high]): `display:flex; width:100%;
  align-items:center; justify-content:space-between; background:transparent; border:none;
  padding:0; font:inherit; text-align:left; cursor:pointer;`. Keep `:hover` and
  `:focus-visible` per existing convention (`outline:2px solid var(--accent)`).
- `.movelist-chevron`: rotates on collapsed state, transition uses `--motion-fast`/`--ease-out`;
  wrap the transition in `@media (prefers-reduced-motion: reduce)` → no transition.
- **Tokens only** — no raw hex; reuse `--space-*`, `--accent`, `--border-subtle`,
  `--surface-raised`, motion tokens.

### `static/movelist.js`
- In `initMovelist(api)`: locate `#movelist-toggle`; on click **and** keyboard activation
  (native `<button>` covers Enter/Space) → toggle `.collapsed` on `.movelist-block`, sync
  `aria-expanded`, and write the flag to `chess-training:ui:v1`.
- **Writes must read-merge-write** (refuter [low]): parse the existing `chess-training:ui:v1`
  object, set `moveListCollapsed`, write the merged object back — never overwrite with a
  single-key object (so a future second UI pref in the same key isn't clobbered). Use small
  `readUiPrefs()` / `writeUiPref(key, val)` helpers.
- On init: read `chess-training:ui:v1`; if `moveListCollapsed` truthy, apply collapsed class +
  `aria-expanded="false"` before/at first paint. Wrap all storage in try/catch (best-effort,
  matching `persist()`).
- Self-contained in the module (no `app.js` state coupling); the prefs key is read/written only
  here.

## Out of scope
- The **Review tab**'s own layout/move list rendering (only the Analysis-tab `.movelist-block`).
- The **eval bar**, board column, other panels/tabs.
- Changing the `:session:v1` session shape, `persist()`/`restore()`, or `movelist.js`
  SAN/quality rendering logic.
- Animating the collapse height (just show/hide, like the existing `.collapsed` pattern).

## Constraints
- **Tokens-only CSS**, no raw hex (`grep -nE '#[0-9a-fA-F]{3,6}' static/movelist.css` → none new).
- A11y: visible `:focus-visible`, `aria-expanded` reflects state, `aria-controls` points at
  `#move-list`, AA contrast, honors `prefers-reduced-motion`.
- Must not break the move list render across **play + review** modes (`movelist.js:64`) nor the
  active-move scroll-into-view.
- Pure frontend; `pytest` stays green (no backend touched). Verified in-browser before commit.

## Verify-by
1. Load a long game (or play ~20+ plies) in the Analysis tab → eval number + best move stay on
   screen; the move list scrolls **inside itself**; the active move is visible after navigation.
2. Click the "Moves" header → list collapses, chevron rotates, `aria-expanded="false"`; click
   again → expands. Keyboard: Tab to the header, Enter/Space toggles.
3. Collapse, **reload the page** → still collapsed (read from `chess-training:ui:v1`). Game-state
   persistence (moves/cursor) unaffected.
4. `prefers-reduced-motion` on → no chevron transition. 0 console errors. `pytest` green.
