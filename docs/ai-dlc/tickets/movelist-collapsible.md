# Tickets — Collapsible + Height-Capped Move List

Spec: `docs/ai-dlc/specs/movelist-collapsible.md`. Frontend-only; no backend.

**Shared contract (all tickets honor):** header id `#movelist-toggle` (a `<button>`); collapsed
class `.movelist-block.collapsed`; UI-prefs key `chess-training:ui:v1` =
`{ moveListCollapsed: boolean }`.

**Orchestration note:** tickets are tightly coupled by the shared selectors above; T1→T3 are
small and share contracts, so **default to ONE builder agent doing T1–T3 in sequence** (per
ORCHESTRATION "default to ONE agent"), then T4. Do not parallelize across these three files.

| # | Ticket | Owned files | Done-condition | Deps |
|---|--------|-------------|----------------|------|
| T1 | Replace the "Moves" `<div class="eval-label">` with a `<button id="movelist-toggle" class="movelist-toggle eval-label" aria-expanded="true" aria-controls="move-list">` holding the label text + a decorative `<span class="movelist-chevron" aria-hidden="true">` chevron. | `static/index.html` (~line 190) | Page renders; header is a focusable button labelled "Moves" with a chevron; `#move-list` unchanged. | — |
| T2 | In `movelist.css`: cap `#move-list` (`max-height` ~`18rem` + `overflow-y:auto`, keep border/padding); `.movelist-block.collapsed #move-list { display:none }`; `.movelist-toggle` full reset (`display:flex; width:100%; align-items:center; justify-content:space-between; background:transparent; border:none; padding:0; font:inherit; text-align:left; cursor:pointer`) + `:hover`/`:focus-visible` (`outline:2px solid var(--accent)`); `.movelist-chevron` rotate-on-collapsed with `--motion-fast`/`--ease-out`, wrapped in `@media (prefers-reduced-motion:reduce)` (no transition). Tokens only — no raw hex. | `static/movelist.css` | Long list scrolls inside itself; eval stays visible; collapsed class hides the list; chevron rotates; `grep -nE '#[0-9a-fA-F]{3,6}' static/movelist.css` shows no new hex. | T1 |
| T3 | In `movelist.js` `initMovelist`: wire `#movelist-toggle` click → toggle `.collapsed` on `.movelist-block` + sync `aria-expanded`; add `readUiPrefs()`/`writeUiPref()` (read-merge-write on `chess-training:ui:v1`, try/catch best-effort); apply saved collapsed state at init (before/at first paint). Do **not** change the `scrollIntoView({block:'nearest'})` call. No `app.js` coupling. | `static/movelist.js` | Click/Enter/Space toggles + persists; reload keeps collapsed state; `:session:v1` untouched. | T1 |
| T4 | Verify end-to-end (browser + tests). | — (no source edit) | See Verify-by below; all pass; 0 console errors; `pytest` green. | T1–T3 |

## T4 — Verify-by (from spec)
1. Long game in Analysis tab → eval number + best move stay on screen; move list scrolls
   **inside itself**; active move visible after navigation (undo/redo/click).
2. Click "Moves" header → collapses, chevron rotates, `aria-expanded="false"`; click → expands.
   Keyboard: Tab to header, Enter/Space toggles.
3. Collapse → **reload** → still collapsed (`chess-training:ui:v1`); moves/cursor persistence
   unaffected.
4. `prefers-reduced-motion` → no chevron transition. 0 console errors. `pytest` green
   (no backend touched, run as a guard).
