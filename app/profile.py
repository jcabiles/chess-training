"""
profile.py — Cross-game tendency profiler.

Aggregates leak records across all analysed games (WHERE my_color IS NOT NULL
so leaks from games with unknown player color are NEVER counted — Refuter
resolution #8).

Public API
----------
build_profile() -> dict

Return shape:
    {
        "games_analyzed": int,
        "top_leaks": [{"category": str, "count": int, "coach": str}, ...],
        "by_phase": {"opening": int, "middlegame": int, "endgame": int},
        "by_opening": [{"opening": str, "count": int}, ...],   # top 5 openings by leak count
        "by_color": {"white": int, "black": int},
        "hope_chess_rate": float,   # [0, 1] fraction of analysed games with a missed-threat leak
        "trend": [{"bucket": str, "count": int}, ...],         # weekly buckets
    }

Notes
-----
- Read-only SQL queries run against the storage module's connection via
  storage._get_conn().  profile.py does NOT edit storage.py.
- The `coach` field on each top-leak entry is the human tendency-cluster name
  from coaching.TemplateNarrator, so the dashboard can display it without an
  extra round-trip.
- When the DB is uninitialised (storage not yet called) build_profile() raises
  RuntimeError (same as storage._get_conn()).
"""

from __future__ import annotations

from app import coaching, storage

# ---------------------------------------------------------------------------
# Internal SQL helpers (read-only; never edit storage.py)
# ---------------------------------------------------------------------------


def _qualified_game_ids() -> list[int]:
    """Return game IDs where my_color IS NOT NULL and status='done'."""
    conn = storage._get_conn()
    rows = conn.execute(
        "SELECT id FROM games WHERE my_color IS NOT NULL AND analysis_status = 'done'"
    ).fetchall()
    return [r["id"] for r in rows]


def _all_qualified_game_ids() -> list[int]:
    """Game IDs that have been analysed (status='done') with a known color."""
    return _qualified_game_ids()


def _games_with_leak_category(game_ids: list[int], category: str) -> int:
    """Count distinct games (from *game_ids*) that have at least one leak of *category*."""
    if not game_ids:
        return 0
    conn = storage._get_conn()
    placeholders = ",".join("?" * len(game_ids))
    row = conn.execute(
        f"SELECT COUNT(DISTINCT game_id) FROM leaks "
        f"WHERE game_id IN ({placeholders}) AND category = ?",
        game_ids + [category],
    ).fetchone()
    return row[0] if row else 0


def _top_leaks_by_category(game_ids: list[int], limit: int = 10) -> list[dict]:
    """Return leak category counts across *game_ids*, most frequent first."""
    if not game_ids:
        return []
    conn = storage._get_conn()
    placeholders = ",".join("?" * len(game_ids))
    rows = conn.execute(
        f"SELECT category, COUNT(*) AS cnt FROM leaks "
        f"WHERE game_id IN ({placeholders}) "
        f"GROUP BY category ORDER BY cnt DESC LIMIT ?",
        game_ids + [limit],
    ).fetchall()
    return [{"category": r["category"], "count": r["cnt"]} for r in rows]


def _by_phase(game_ids: list[int]) -> dict:
    if not game_ids:
        return {"opening": 0, "middlegame": 0, "endgame": 0}
    conn = storage._get_conn()
    placeholders = ",".join("?" * len(game_ids))
    rows = conn.execute(
        f"SELECT phase, COUNT(*) AS cnt FROM leaks "
        f"WHERE game_id IN ({placeholders}) "
        f"GROUP BY phase",
        game_ids,
    ).fetchall()
    result = {"opening": 0, "middlegame": 0, "endgame": 0}
    for r in rows:
        phase = r["phase"]
        if phase in result:
            result[phase] = r["cnt"]
    return result


def _by_color(game_ids: list[int]) -> dict:
    if not game_ids:
        return {"white": 0, "black": 0}
    conn = storage._get_conn()
    placeholders = ",".join("?" * len(game_ids))
    rows = conn.execute(
        f"SELECT color, COUNT(*) AS cnt FROM leaks "
        f"WHERE game_id IN ({placeholders}) "
        f"GROUP BY color",
        game_ids,
    ).fetchall()
    result: dict[str, int] = {"white": 0, "black": 0}
    for r in rows:
        color = (r["color"] or "").lower()
        if color in result:
            result[color] = r["cnt"]
    return result


def _by_opening(game_ids: list[int], limit: int = 5) -> list[dict]:
    """Return top openings by leak count (NULL opening → 'Unknown')."""
    if not game_ids:
        return []
    conn = storage._get_conn()
    placeholders = ",".join("?" * len(game_ids))
    rows = conn.execute(
        f"""
        SELECT COALESCE(g.opening, 'Unknown') AS opening, COUNT(l.id) AS cnt
        FROM leaks l
        JOIN games g ON l.game_id = g.id
        WHERE l.game_id IN ({placeholders})
        GROUP BY g.opening
        ORDER BY cnt DESC
        LIMIT ?
        """,
        game_ids + [limit],
    ).fetchall()
    return [{"opening": r["opening"], "count": r["cnt"]} for r in rows]


def _hope_chess_rate(game_ids: list[int]) -> float:
    """Fraction of *game_ids* that contain at least one missed-threat or allowed-threat leak.

    'missed_threat' is the canonical category used by the pipeline.  If a game
    has any leak with category='missed_threat', it counts as a hope-chess game.
    """
    if not game_ids:
        return 0.0
    n_games = len(game_ids)
    conn = storage._get_conn()
    placeholders = ",".join("?" * n_games)
    row = conn.execute(
        f"SELECT COUNT(DISTINCT game_id) FROM leaks "
        f"WHERE game_id IN ({placeholders}) AND category = 'missed_threat'",
        game_ids,
    ).fetchone()
    n_with_threat = row[0] if row else 0
    return n_with_threat / n_games


def _trend(game_ids: list[int]) -> list[dict]:
    """Weekly leak-count buckets ordered by date (oldest first).

    Bucket key: ISO week string, e.g. '2026-W01'.
    """
    if not game_ids:
        return []
    conn = storage._get_conn()
    placeholders = ",".join("?" * len(game_ids))
    # SQLite's strftime('%Y-W%W', ...) gives the ISO week.
    rows = conn.execute(
        f"""
        SELECT strftime('%Y-W%W', g.imported_at) AS bucket, COUNT(l.id) AS cnt
        FROM leaks l
        JOIN games g ON l.game_id = g.id
        WHERE l.game_id IN ({placeholders})
        GROUP BY bucket
        ORDER BY bucket ASC
        """,
        game_ids,
    ).fetchall()
    return [{"bucket": r["bucket"], "count": r["cnt"]} for r in rows]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_profile() -> dict:
    """Aggregate leaks across analysed games into a tendency profile.

    Filters to games WHERE my_color IS NOT NULL (Refuter #8 — never count
    opponent blunders as the user's own).

    Raises RuntimeError if storage has not been initialised.

    Returns:
        dict with keys: games_analyzed, top_leaks, by_phase, by_opening,
        by_color, hope_chess_rate, trend.
    """
    game_ids = _qualified_game_ids()
    n = len(game_ids)

    narrator = coaching.get_narrator()

    raw_top = _top_leaks_by_category(game_ids)
    top_leaks = []
    for entry in raw_top:
        category = entry["category"]
        count = entry["count"]
        coach = narrator.name_cluster(category, {"count": count})
        top_leaks.append({"category": category, "count": count, "coach": coach})

    return {
        "games_analyzed": n,
        "top_leaks": top_leaks,
        "by_phase": _by_phase(game_ids),
        "by_opening": _by_opening(game_ids),
        "by_color": _by_color(game_ids),
        "hope_chess_rate": _hope_chess_rate(game_ids),
        "trend": _trend(game_ids),
    }
