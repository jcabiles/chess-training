# Contracts — Per-side Accuracy % + estimated Elo for reviewed games

Goal: after a saved game's background review runs, show an **estimated Accuracy % and
Elo for both sides** (You / Opponent), Chess.com-style. Pure aggregation over data the
review pipeline already stores — **no new Stockfish work, no schema change** (computed
on demand at the review endpoint).

## What already exists (reuse, don't rebuild)

### Per-ply data is already persisted, for BOTH colors
- `game_plies` table (`storage.py:83`) stores, per ply: `eval_cp_white` (White-POV cp,
  NULL on mate), `mate_white` (White-POV mate-in-N, NULL otherwise), `win_prob` (REAL,
  **side-to-move POV**), `is_user_move` (1 iff `board.turn == my_color`), `san`, `uci`,
  `fen_before`.
- Pass 1 of `analyze_game` (`review.py:280`) writes `eval_cp_white` + `win_prob` for
  **every ply regardless of color** — so both sides' positions are already evaluated.
  (Pass-2 leak classification is user-color-only, `review.py:379`, but we don't need
  leaks for accuracy.)
- `win_prob` is stored mover-POV (fiddly to sign). **Prefer `eval_cp_white` (unambiguous
  White-POV)** + `analysis.win_prob_white` for the accuracy math.

### Pure helpers (engine-free, in `app/analysis.py`)
- `win_prob_from_cp(cp) -> float` (`analysis.py:143`) — lichess logistic
  `1/(1+exp(-0.00368208*cp))`, clamped [0,1]. NOTE: `100 * win_prob_from_cp(cp_white)`
  == lichess "win%" for White exactly.
- `win_prob_white(cp_white, mate_white) -> float` (`analysis.py:169`) — handles the
  mate cases (→0.0/1.0), delegates to `win_prob_from_cp`. **Use this per ply.**
- `classify`, `bucket`, `leak_severity` exist but are not needed for accuracy.
- There is **no ACPL and no accuracy formula** in the codebase yet — both are new.

### API surface (`app/main.py` + `app/models.py`)
- `GET /api/games/{id}/review` → `ReviewResponse` (`models.py:302`) = `{game_id,
  analysis_status, leaks[], plies[]}`. The route (`main.py:684`) already pulls the ply
  rows + leaks. **This is the attach point** — add an optional `summary` object.
- `PlyDetail` (`models.py:235`): `ply, san, uci, fen_before, eval_cp_white, mate_white,
  win_prob, is_user_move, clock_centis`.
- Other endpoints (`GET /api/games`, `/{id}`, `/status`, `analyze`, `analyze-all`,
  `retag-color`, `/api/profile`) are unaffected.

### Frontend Review tab (`static/`)
- `openGame()` (`review.js:489`) loads `GameDetail`, then `loadReviewData()` fetches
  `ReviewResponse`. Replay UI shows `#review-bar` (`index.html:133`) containing
  `#review-game-title` (`:135`) and `#review-foresight` (`:138`, rendered by
  `renderForesight`, `review.js:541`).
- **Insertion point:** a new `#review-game-summary` div inside `#review-bar`, between
  title and foresight; rendered by a new `renderGameSummary(summary)` after review data
  loads.

### Color tagging — how "me" is known
- `games.my_color` TEXT (`storage.py:61`) ∈ `'white'|'black'|NULL`. Inferred at import
  from `CHESS_USERNAME` (`pgn._infer_my_color`, `pgn.py:109`); settable via
  `PATCH /api/games/{id}` and bulk `retag-color`. The review row carries it; `is_user_move`
  derives from it.

## Contracts to respect
- `app/` stays import-safe + unit-testable **without a Stockfish binary** — the new
  accuracy code must be a pure function (no engine), like `analysis.py`/`motifs.py`.
- Reuse `analysis.win_prob_white` / the White-POV convention — **don't re-derive the
  mover-sign rule** (CLAUDE.md).
- No schema change (on-demand compute). `_SCHEMA_VERSION` stays 1; no migration branch
  exists yet anyway (`storage.py:159` is a stub).
- Tokens-only CSS for the new summary UI; no raw hex; `:focus-visible` + AA contrast.
- `ReviewResponse.summary` is **additive + optional** — existing consumers unaffected.

## Invisible-contract warnings (from the contract scan)
1. **No cp-loss / accuracy stored** — must reconstruct from consecutive `eval_cp_white`.
2. **`win_prob` column is mover-POV**, not White — avoid it; use `eval_cp_white` +
   `win_prob_white` for clean White-POV math, then flip per mover.
3. **No successor eval for the final ply** — the last move can't be scored (no stored
   post-position eval); exclude it (documented, negligible).
4. **`win_prob`/`eval_cp_white` are NULL until analysis is `done`** — only compute the
   summary when `analysis_status == 'done'`; otherwise return `summary: null`.
5. **Opponent leaks don't exist** (user-only) — irrelevant here; accuracy reads
   `game_plies` directly, so both sides are covered without touching `leaks`.

## Integration points (summary)
- New pure `app/accuracy.py`: `summarize(ply_rows, my_color) -> summary dict` using
  `analysis.win_prob_white`. No engine, no DB.
- `app/models.py`: new `GameAccuracySummary`; `ReviewResponse.summary: ... | None`.
- `app/main.py` `get_game_review` (`main.py:684`): compute summary from the ply rows it
  already fetches when status is `done`.
- `static/review.js` + `index.html` + CSS: `renderGameSummary` into `#review-bar`.
