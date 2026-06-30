# UX / Feature Backlog

Ranked list of future improvements not built yet. Seeded from the 2026 UX research
synthesis (`contracts/specs` for the UX modernization) — the items deprioritized for the
single-user local tool — plus ideas surfaced while building the overhaul + move list.

Impact = value to the user · Effort = build + verify cost. Ordered by rough impact/effort.

| # | Item | What it is | Impact | Effort | Notes / why deferred |
|---|------|-----------|--------|--------|----------------------|
| 1 | **Light / system theme** | A light mode + follow-OS / manual toggle, on top of the dark default. | M | M | Cheap now that colors are OKLCH tokens — flip a `[data-theme]` scope. Deferred: user runs dark; doubles visual test surface. |
| 2 | **Command palette (Cmd/Ctrl-K)** | Fuzzy launcher: load FEN, flip, switch mode/tab, jump to a trap/line. | M | M | Big perceived-pro upgrade for a power user; additive module + a registry of the existing actions. |
| 3 | **Eval graph** | A line chart of the eval across the whole game (click a point → jump). | M | M | **Unblocked for saved games:** game review now stores per-ply evals (`game_plies`), so a graph over a reviewed game is a small frontend add. Live-play games still need eval history captured. |
| 4 | **Move-list extras** | Variation tree (branches), eval-per-move sparkline/number, NAG glyphs. | M | H | Variation tree means replacing the flat `moves[]` with a tree model — touches persist/restore + the whole move pipeline. Big. |
| 5 | **Per-move quality on loaded/restored games** | Compute quality for history that wasn't played live this session. | L | M | **Largely delivered for saved games** by game review (per-ply quality computed + cached). Still open for live/FEN-loaded sessions that aren't imported as a game. |
| 6 | **Opening-explorer revival** | Bring back / rework the candidate-openings list (was de-emphasized by traps). | L | M | Decide repurpose vs remove first (earlier `propose-ideas` discussion). |
| 7 | **Deep assistive-tech a11y** | Screen-reader board + full keyboard board navigation (ARIA grid/treegrid, `aria-live` move announcements). | L (solo user) | H | The accessible chess board alone is a sub-project. Cheap a11y (contrast, color+label, keyboard shortcuts, `<dialog>`, reduced-motion) is already done. |
| 8 | **Vendor CDN libs offline** | Download chessground / chessops / Lucide into `static/vendor/` and switch imports to local paths. | L | L | Currently loads from `esm.sh` / jsdelivr (needs internet once). `TODO(vendor)` marker in `index.html`. |
| 9 | **Motion polish** | View-Transitions API for tab/mode switches; eval-bar ease tuning. | L | M | Motion tokens + `prefers-reduced-motion` are in place; this is the next tier of polish. |
| 10 | **P3 wide-gamut accents** | Richer accent colors on P3 displays via `@media (color-gamut: p3)`. | L | L | Pure enhancement; sRGB fallback already correct. |
| 11 | **Harden `askPromotion`** | Replace the `cloneNode` listener-reset with explicit teardown on the same node. | L | L | Not currently reachable (the `<dialog>` is modal, so no concurrent promotion), but the clone-then-detach pattern could orphan a pending Promise if that ever changes. |
| 12 | **Blunder Trainer** (game-review next epic) | Re-solve your own blunders as puzzles with a forced threat-check (CCT) + spaced repetition. | H | H | Deferred third "head" of the game-review epic (spec: `specs/game-review-coaching.md`). Caveat: resurface the **motif in varied positions**, never the identical board (de la Maza memorization trap). |
| 13 | **Game-review polish** | Self-hang (not missed-threat) narration; surface time-trouble from `%clk`; auto-fetch games from lichess/chess.com APIs. | M | M | Captured-but-unused: `game_plies.clock_centis` holds clock data; self-hang blunders currently lean on the best-move suggestion; import is manual only. |

## How to pick up an item
Run `/ai-dlc <item>` to turn one into requirements → spec → tickets, same as the overhaul.
