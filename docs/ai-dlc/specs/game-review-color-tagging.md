# Delta Spec — Game-Review Color Tagging + Bulk Analyze

## Problem (why)
The game-review **profiler only counts games tagged with your color** (it coaches *your*
mistakes), and leak-detection is **skipped entirely when `my_color` is NULL**. Games imported
without `CHESS_USERNAME` (or a per-import color) get `my_color = NULL` → analyzed but **0 leaks**
→ empty profile. There is currently **no way to set color on already-imported games** and **no
bulk analyze**, so a user who imported many games before setting `CHESS_USERNAME` is stuck with
an empty "holistic" view. (Observed: 120 games imported, 119 NULL-color, 0 leaks.)

## Goal (one line)
Let the user **tag color on existing games** (per-game + bulk-by-username) and **(re)analyze in
bulk**, so the cross-game profiler actually populates — without re-importing.

## Locked decisions
- Changing a game's color **invalidates its leaks** (they were computed under the old/none
  color) → setting color resets `analysis_status` to `pending` so re-analysis recomputes leaks.
- **Bulk analyze is sequential** (one game at a time through the single engine lock; reuses the
  existing no-starve yield). No 119 concurrent tasks.
- Re-analysis is **cheap**: `pos_cache` (EPD+depth) already holds most positions; only the
  per-leak null-move probes re-run.
- Deterministic, templates-only — **no new LLM/tokens, no new deps.**

## In scope
### Backend
- `app/storage.py`: `set_my_color(game_id, color)` (validates white|black|None; resets that
  game's `analysis_status='pending'`); `retag_colors_by_aliases(aliases) -> int` (bulk: set
  `my_color` by matching White/Black against aliases, reset affected to `pending`, return count);
  `list_games` already exposes counts the UI needs.
- `app/review.py`: `analyze_pending(engine, **kw)` — a single background task that walks all
  `pending` games **sequentially** (status→analyzing→done each), yielding to interactive moves;
  tracked so it can be cancelled; safe if already running (no-op/extend).
- `app/main.py` routes (literal before `{id}`):
  - `PATCH /api/games/{id}` body `{my_color: "white"|"black"|null}` → `set_my_color`.
  - `POST /api/games/retag-color` body `{username: "a,b"}` → bulk retag, return `{updated}`.
  - `POST /api/games/analyze-all` (Depends engine) → start `analyze_pending`, return `{pending}`.
  - `GET /api/profile` already exists; extend its payload (or `GET /api/games`) so the UI can
    show "tagged X / Y, analyzed Z" coverage.
- `app/models.py`: request/response models for the above (additive).

### Frontend (`static/review.js` + `.css`, `static/index.html` if needed)
- Per-game **color control** (white / black / — unknown) on each library row → `PATCH`.
- A **"Set my color from username"** input + button (→ `retag-color`) — the one-click fix for a
  bulk of untagged games.
- An **"Analyze all"** button (→ `analyze-all`) with simple progress (poll counts).
- A **coverage hint** ("47 of 120 games tagged · 12 analyzed") so the empty-profile cause is
  obvious; nudge when many games are untagged.
- After retag / analyze-all, refresh the library + profile.

## Out of scope
- Auto-fetching games from lichess/chess.com APIs (still manual import).
- The deferred **Trainer** (spaced-repetition re-solve) — separate epic.
- Changing existing routes' shapes, the localStorage session shape, or analysis.py purity.

## Constraints
- Reuse the import-time color-matching logic (alias vs White/Black) for `retag-color`.
- Bulk analyze must not starve interactive `/api/move` (reuse `note_interactive_*`).
- All engine-touching code testable via the `get_engine` fake; full suite stays green.
- Conventional Commits; verified (pytest + browser) + reviewed before commit.

## Verify-by
1. `pytest` green incl. new tests: `set_my_color` resets status; `retag_colors_by_aliases`
   tags by name + resets; `PATCH`/`retag-color`/`analyze-all` routes; bulk analyze sequential.
2. Browser: import games without color → profile empty + coverage hint shows; set color from
   username → games tagged; Analyze all → statuses go done and **leaks appear**; profile
   populates (top leaks / phase / opening / color). 0 console errors.
3. Re-run on the real 120-game DB: profile shows real cross-game tendencies.
