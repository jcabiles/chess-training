# Delta Spec — Per-side Accuracy % + estimated Elo for reviewed games

Contracts: `docs/ai-dlc/contracts/game-accuracy-elo.md`. Backend-led (pure aggregation)
+ a small Review-tab render. Chess.com-style "Game Review" rating, **for an already
fully-analyzed saved game**.

## Goal (one line)
When a reviewed game opens, show an estimated **Accuracy %** and **Elo** for both sides
(You / Opponent), computed on demand from the per-ply evals already stored — no new
engine work, no DB migration.

## Locked decisions (from Gate 1)
- **Accuracy = simple per-move mean** of the lichess per-move accuracy formula. Side
  accuracy = arithmetic mean over that side's scored moves.
- **Elo = single estimate** per side, labeled "est.", with a tooltip noting it's a
  single-game heuristic.
- **On-demand** at `GET /api/games/{id}/review`; rendered in `#review-bar`. **No schema
  change.**
- **Per-game only** — Profile tab untouched.
- **Labels:** `my_color` set → "You" / "Opponent" (ordered you-first); `my_color` NULL →
  "White" / "Black".
- **Only when `analysis_status == 'done'`** — else `summary: null`, render nothing.
- **Final ply excluded** (no stored successor eval); book/opening moves are NOT special-
  cased (they naturally score ~100%).

## The math (deterministic, testable)
All in a new pure module `app/accuracy.py`. Input: the ordered ply rows for one game
(each has `eval_cp_white`, `mate_white`, `fen_before`) + `my_color`.

Guards first (refuter): if `len(plies) <= 1` → both sides `None`/`None`, 0 moves. Skip
any ply with `fen_before is None` (don't `chess.Board(None)` — it raises). Plies are
assumed contiguous + ply-ordered (the review pipeline writes them so, truncating only at
the tail on a parse error — which the "final ply excluded" rule already handles).

1. Per ply `i`, White-POV win% on a **0–100 scale**:
   `wp_white_pct(i) = 100.0 * analysis.win_prob_white(eval_cp_white[i], mate_white[i])`.
   **The `100.0 *` is load-bearing** — the lichess per-move formula is calibrated on
   win% (0–100), NOT win-prob (0–1). Omit it and `exp(-0.04354 * tiny_drop) ≈ 1`, so
   every move scores ~100% with no error. (`win_prob_white(None, None)` returns 0.5 by
   contract; a 'done' ply normally has cp or mate set, so this is just a safe fallback.)
2. For the move played at ply `i` (mover = side to move in `fen_before[i]`), with a
   successor ply `i+1`:
   - mover == White → `drop = wp_white(i) - wp_white(i+1)`
   - mover == Black → `drop = wp_white(i+1) - wp_white(i)`
   - `win_pct_drop = max(0.0, drop)`
   - `move_acc = clamp(103.1668 * exp(-0.04354 * win_pct_drop) - 3.1669, 0.0, 100.0)`
3. `side_accuracy = mean(move_acc for that side's scored moves)`; a side with 0 scored
   moves → `accuracy = None`, `elo = None`.
4. **Elo (heuristic, centralized + tunable constants):**
   `elo = clamp(round(ACC_ELO_SLOPE * side_accuracy + ACC_ELO_INTERCEPT), ELO_MIN, ELO_MAX)`
   with `ACC_ELO_SLOPE = 45.0`, `ACC_ELO_INTERCEPT = -2000.0`, `ELO_MIN = 100`,
   `ELO_MAX = 2900`. (Anchors: 70%→~1150, 80%→~1600, 90%→~2050, 95%→~2275.) Documented
   in-module as a rough estimate, **not** a calibrated rating claim. `ELO_MAX` is a
   defensive cap, not an expected value — 100% accuracy maps to 2500, so the cap only
   guards pathological inputs.

Mover color comes from `fen_before[i]` (robust to non-standard start FENs) — do not
assume ply-index parity.

## In scope

### `app/accuracy.py` (NEW — pure, no engine, no DB)
- `ACC_ELO_SLOPE/INTERCEPT/ELO_MIN/ELO_MAX` constants + the lichess constants.
- `move_accuracy(win_pct_drop: float) -> float` — the clamped per-move formula.
- `accuracy_to_elo(accuracy: float) -> int` — the clamped linear mapping.
- `summarize(plies: list[<row-like>], my_color: str | None) -> dict` returning the
  `GameAccuracySummary` shape below. Accepts any row exposing `eval_cp_white`,
  `mate_white`, `fen_before` (dicts or `PlyDetail`); reuse `analysis.win_prob_white`.
  Uses `chess.Board(fen_before).turn` for mover color.

### `app/models.py`
- `GameAccuracySummary`: `{ white_accuracy: float | None, black_accuracy: float | None,
  white_elo: int | None, black_elo: int | None, white_moves: int, black_moves: int,
  my_color: str | None }`.
- `ReviewResponse.summary: GameAccuracySummary | None = None` (additive, optional).

### `app/main.py`
- In `get_game_review` (`main.py:684`): after fetching ply rows, if
  `analysis_status == 'done'` set `summary = accuracy.summarize(plies, my_color)`, else
  `None`. No engine call, no extra DB round-trip beyond what's already loaded.

### `static/review.js` + `static/index.html` + CSS
- `<div id="review-game-summary" class="review-game-summary" hidden></div>` as a **direct
  child of `#review-bar`, after the `.review-bar-header` wrapper (`index.html:134-137`)
  and before `#review-foresight` (`:138`)** — refuter: title + return button are inside
  `.review-bar-header`; foresight is its sibling, so insert between them, not inside the
  header.
- `renderGameSummary(summary)` **called inside `loadReviewData()` right after the
  existing `renderForesight(...)` call (`review.js:531`)** — refuter: `loadReviewData`
  currently only calls `renderForesight`, so without this the summary never renders.
  Hidden (`hidden` attr) when `summary` is null/absent. Renders two side cells:
  `You 87.3% · ~1480` / `Opponent 82.1% · ~1390` (or White/Black when untagged), you-
  first when tagged. `est.` carries a `title=` tooltip: single-game estimate, not an
  official rating.
- Tokens-only CSS (reuse existing review/panel classes where possible), no raw hex,
  visible `:focus`/`:focus-visible` if any control is focusable (it's display-only, so
  likely none).

## Out of scope
- Persisting accuracy/Elo (no schema change); showing it in the games-list column.
- Cross-game average accuracy / trend in the Profile tab.
- Opponent per-move quality classification, "brilliant/great/miss" categories, move-
  count breakdowns (that was the rejected "full card" direction).
- Volatility-weighted (true lichess) accuracy; excluding book moves; scoring the final
  ply; live play-mode accuracy. Re-analysis/recompute triggers (it's on-demand on open).

## Constraints
- `app/accuracy.py` is **pure** — full suite passes with no Stockfish binary (the
  `get_engine` fake seam). Reuse `analysis.win_prob_white`; don't re-derive White-POV.
- `ReviewResponse.summary` additive + optional — existing clients keep working.
- No DB schema change; `summary` only when `analysis_status == 'done'`.
- Tokens-only CSS, no raw hex; AA contrast in both themes.
- No new network calls in the frontend — `summary` rides the existing `/review` fetch.

## Verify-by
1. **Unit (pytest, no engine):** `accuracy.summarize` on a hand-built ply list →
   expected per-side accuracy + Elo; a perfect side (no win% drops) → ~100% / ~2500 Elo;
   a side with 0 moves → `None`/`None`; **a large-drop move (win% drop ≈ 50 → move_acc
   ≈ 8.5%, NOT ~98%)** — this is the regression test that catches the 0–1-vs-0–100 scale
   bug (passing 0.5 instead of 50 yields ~97.8%); mover
   color read from `fen_before` (include a Black-to-move-first FEN case); a `None`
   `fen_before` ply is skipped (no crash); `len(plies) <= 1` → both `None`; final ply
   excluded.
2. **API (pytest):** `GET /api/games/{id}/review` on a `done` game → `summary` present
   with both sides; on a `pending`/un-analyzed game → `summary: null`. Existing
   `ReviewResponse` fields unchanged.
3. **Browser:** open a reviewed game → review bar shows `You __% · ~____` /
   `Opponent __% · ~____`; an untagged game shows White/Black; tooltip present; an
   un-analyzed game shows no summary; 0 console errors.
4. `pytest` green; `ruff` clean.
