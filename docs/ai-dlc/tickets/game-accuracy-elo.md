# Tickets — Per-side Accuracy % + estimated Elo for reviewed games

Spec: `docs/ai-dlc/specs/game-accuracy-elo.md`. Contracts: `docs/ai-dlc/contracts/game-accuracy-elo.md`.

**Shared contract (all tickets agree):**
- New pure module `app/accuracy.py` — no engine, no DB; reuses `analysis.win_prob_white`.
- Summary shape: `{ white_accuracy, black_accuracy: float|None, white_elo, black_elo: int|None,
  white_moves, black_moves: int, my_color: str|None }`.
- Win% is on a **0–100** scale into the lichess formula (`100.0 * win_prob_white(...)`).
- `summary` only when `analysis_status == 'done'`, else `null`; additive/optional on `ReviewResponse`.
- Frontend: `#review-game-summary` is a direct child of `#review-bar`, after `.review-bar-header`,
  before `#review-foresight`; rendered by `renderGameSummary`, called inside `loadReviewData`.

| # | Ticket | Owned files | Done-condition | Deps |
|---|--------|-------------|----------------|------|
| T1 | Create pure `app/accuracy.py`: constants (lichess `103.1668`/`0.04354`/`3.1669`; `ACC_ELO_SLOPE=45.0`, `ACC_ELO_INTERCEPT=-2000.0`, `ELO_MIN=100`, `ELO_MAX=2900`); `move_accuracy(win_pct_drop)`, `accuracy_to_elo(acc)`, `summarize(plies, my_color)`. Guards: `len(plies)<=1`→all None; skip `fen_before is None`; final ply excluded; mover from `chess.Board(fen_before).turn`; **explicit `100.0 *` on win%**. | `app/accuracy.py` (new) | Importable with no Stockfish binary; `summarize` returns the agreed dict; pure (no engine/DB import). | — |
| T2 | Add `GameAccuracySummary` model; add `summary: GameAccuracySummary \| None = None` to `ReviewResponse`. | `app/models.py` | Models import; field optional/additive; existing `ReviewResponse` use still constructs. | — |
| T3 | In `get_game_review` (`app/main.py` ~684): when `analysis_status=='done'`, set `summary = accuracy.summarize(plies, row["my_color"])` (reuse already-fetched ply rows + `row`); else `None`. No engine call, no new DB query. | `app/main.py` | `/api/games/{id}/review` returns `summary` on done games, `null` otherwise; no extra engine/DB work. | T1, T2 |
| T4 | Add `<div id="review-game-summary" class="review-game-summary" hidden></div>` as direct child of `#review-bar`, after `.review-bar-header`, before `#review-foresight`. Tokens-only CSS for the two side cells (accuracy + `est.` Elo), AA contrast both themes, no raw hex. | `static/index.html`, `static/style.css` | Element present in correct spot; CSS uses tokens only (`grep -E '#[0-9a-fA-F]{3,6}'` → none new). | — |
| T5 | `review.js`: `renderGameSummary(summary)` — hide div when null; else fill two cells (`You __% · ~____` / `Opponent __% · ~____`, you-first when tagged; White/Black when `my_color` null); `est.` carries a single-game-estimate `title=` tooltip. Call it inside `loadReviewData()` right after `renderForesight(...)`. | `static/review.js` | Summary renders from the `/review` response; null → hidden; no new network call; 0 console errors. | T4 |
| T6 | Unit tests for `app/accuracy.py` on hand-built ply lists: perfect side → ~100%/~2500; **large drop (win%≈50 → move_acc≈8.5%, not ~98%)** [scale-bug regression]; 0-move side → None/None; `None` `fen_before` skipped (no crash); `len<=1` → None; Black-to-move-first FEN; final ply excluded. | `tests/test_accuracy.py` (new) | `pytest tests/test_accuracy.py` green; no Stockfish binary needed. | T1 |
| T7 | API test: `GET /api/games/{id}/review` on a `done` game → `summary` with both sides; on a `pending`/un-analyzed game → `summary: null`; existing `ReviewResponse` fields unchanged. | `tests/test_api.py` | `pytest` green incl. new tests. | T3 |
| T8 | Verify end-to-end: `pytest` + `ruff check` clean; browser — open a reviewed game shows `You __% · ~____` / `Opponent __% · ~____`, untagged shows White/Black, tooltip present, un-analyzed shows no summary, 0 console errors. Update README usage bullet. | `README.md` + verification | See spec Verify-by. | T1–T7 |

## Orchestration plan
- **Wave 1 (parallel, disjoint files):** T1 (sonnet — correctness-critical math), T2 (haiku —
  mechanical model add), T4 (haiku — display-only HTML+CSS).
- **Wave 2 (parallel):** T3 (sonnet — route wiring), T5 (sonnet — review.js render).
- **Wave 3 (parallel, maker≠checker — different agents than T1/T3):** T6 (sonnet — accuracy unit
  tests), T7 (haiku — API test).
- **Wave 4:** T8 verify (me: pytest + ruff + browser + README).
