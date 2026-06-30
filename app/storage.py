"""
storage.py — SQLite data-access layer for the game-review & coaching feature.

House pattern (mirrors app/traps.py and app/repertoire.py):
  - Module-level singleton (_conn) — initialised to None so imports never raise.
  - init(db_path) at app lifespan: resolves path, creates dirs, runs migrations.
  - NEVER crashes on a missing/locked DB — logs one warning, leaves state empty.
  - Importing this module never opens a DB connection.

EPD cache key convention:
  Callers supply the EPD string (move-counters stripped, see chess.Board.epd()).
  All eval columns are stored White-POV-normalized at write time
  (pov_score_to_white_cp was applied before calling upsert_pos_cache), so every
  reader is automatically POV-safe without any sign-flip logic.

Analysis status enum: 'pending' | 'analyzing' | 'done' | 'failed'.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state — import-safe (no DB opened at import time).
# ---------------------------------------------------------------------------

_conn: Optional[sqlite3.Connection] = None

# Schema version — increment when adding new columns/tables.
_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Schema definition
# ---------------------------------------------------------------------------

_SCHEMA_DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    version  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS games (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash    TEXT UNIQUE NOT NULL,
    pgn             TEXT NOT NULL,
    headers_json    TEXT,
    white           TEXT,
    black           TEXT,
    result          TEXT,
    eco             TEXT,
    opening         TEXT,
    date            TEXT,
    my_color        TEXT,           -- NULL when CHESS_USERNAME unset/no match
    source          TEXT,
    ply_count       INTEGER,
    imported_at     TEXT NOT NULL,
    analysis_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(analysis_status IN ('pending','analyzing','done','failed'))
);

CREATE TABLE IF NOT EXISTS pos_cache (
    -- Keyed by EPD (move-counters stripped) + depth so transpositions share one row.
    -- All eval columns are White-POV-normalized at write time.
    epd_key         TEXT NOT NULL,
    depth           INTEGER NOT NULL,
    eval_cp_white   INTEGER,        -- centipawns, White POV (NULL when mate)
    mate_white      INTEGER,        -- mate-in-N, White POV (NULL when not mate)
    best_uci        TEXT,
    best_san        TEXT,
    pv_san_json     TEXT,           -- JSON array of SAN strings
    pv2_cp_white    INTEGER,        -- 2nd-best line score, White POV (NULL when not available)
    PRIMARY KEY (epd_key, depth)
);

CREATE TABLE IF NOT EXISTS game_plies (
    game_id         INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    ply             INTEGER NOT NULL,
    san             TEXT,
    uci             TEXT,
    fen_before      TEXT,
    eval_cp_white   INTEGER,        -- White POV (NULL when mate or unanalysed)
    mate_white      INTEGER,        -- White POV (NULL when not mate)
    win_prob        REAL,           -- win probability [0,1] for the side to move
    is_user_move    INTEGER NOT NULL DEFAULT 0,
    clock_centis    INTEGER,        -- from %clk tag, NULL when absent
    PRIMARY KEY (game_id, ply)
);

CREATE TABLE IF NOT EXISTS leaks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id         INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    ply             INTEGER NOT NULL,
    color           TEXT NOT NULL,
    severity        TEXT NOT NULL,  -- 'mistake' | 'blunder'
    category        TEXT NOT NULL,
    motif_json      TEXT,           -- JSON object from motifs module
    phase           TEXT NOT NULL,  -- 'opening' | 'middlegame' | 'endgame'
    win_prob_before REAL NOT NULL,
    win_prob_after  REAL NOT NULL,
    win_prob_drop   REAL NOT NULL,
    hung_square     TEXT,
    threat_uci      TEXT,
    threat_motif    TEXT,
    best_uci        TEXT,
    best_san        TEXT,
    lead_in_ply     INTEGER,        -- ply where the foresight warning should fire
    tags_json       TEXT,           -- JSON array of tag strings
    explanation_json TEXT           -- JSON object for template narrator
);
"""


# ---------------------------------------------------------------------------
# LeakRecord dataclass — shared shape for T5 (writer) and T6 (readers).
# ---------------------------------------------------------------------------

@dataclass
class LeakRecord:
    """Mirrors the leaks table columns exactly.

    Fields that are optional in the DB default to None here.  game_id/id are
    set by the DB on insert; pass id=None, game_id=0 when building before insert.
    """
    game_id: int
    ply: int
    color: str
    severity: str               # 'mistake' | 'blunder'
    category: str
    phase: str                  # 'opening' | 'middlegame' | 'endgame'
    win_prob_before: float
    win_prob_after: float
    win_prob_drop: float

    # optional / nullable
    id: Optional[int] = None
    motif_json: Optional[str] = None        # JSON string
    hung_square: Optional[str] = None
    threat_uci: Optional[str] = None
    threat_motif: Optional[str] = None
    best_uci: Optional[str] = None
    best_san: Optional[str] = None
    lead_in_ply: Optional[int] = None
    tags_json: Optional[str] = None         # JSON string
    explanation_json: Optional[str] = None  # JSON string


# ---------------------------------------------------------------------------
# Migration runner
# ---------------------------------------------------------------------------

def _run_migrations(conn: sqlite3.Connection) -> None:
    """Create tables if missing and stamp the schema_meta version.

    Idempotent: running twice on an already-migrated DB is a no-op.
    """
    conn.executescript(_SCHEMA_DDL)
    row = conn.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_meta (version) VALUES (?)", (_SCHEMA_VERSION,))
        conn.commit()
        logger.debug("storage: schema created at version %d", _SCHEMA_VERSION)
    else:
        current = row[0]
        if current < _SCHEMA_VERSION:
            # Future migrations go here; for now just update the stamp.
            conn.execute("UPDATE schema_meta SET version = ?", (_SCHEMA_VERSION,))
            conn.commit()
            logger.info("storage: schema migrated from v%d to v%d", current, _SCHEMA_VERSION)
        else:
            logger.debug("storage: schema already at version %d", current)


# ---------------------------------------------------------------------------
# Public init
# ---------------------------------------------------------------------------

def init(db_path: Optional[str] = None) -> None:
    """Open (or create) the SQLite DB and run migrations.

    Path resolution: arg → env GAMES_DB → default 'data/games.db'.
    Creates parent directories as needed.
    Import-safe: never raises — on any error logs one warning and leaves _conn=None.
    Calling init() multiple times is safe (re-uses existing connection when already open).
    """
    global _conn

    if db_path is None:
        db_path = os.environ.get("GAMES_DB", "data/games.db")

    path = Path(db_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        _run_migrations(conn)
        _conn = conn
        logger.info("storage: opened DB at '%s'", path)
    except Exception as exc:
        logger.warning("storage: could not open/init DB at '%s': %s — storage disabled", path, exc)
        _conn = None


def _get_conn() -> sqlite3.Connection:
    """Return the active connection, raising RuntimeError when storage is disabled."""
    if _conn is None:
        raise RuntimeError("storage.init() has not been called or failed to open the DB")
    return _conn


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------

_ANALYSIS_STATUSES = frozenset({"pending", "analyzing", "done", "failed"})


def insert_game(fields: dict[str, Any]) -> int:
    """Insert a game row, deduplicating by content_hash.

    If a row with the same content_hash already exists, returns the existing id
    without touching the row (no-op re-insert).

    Required key: 'content_hash', 'pgn', 'imported_at'.
    All other columns are optional (default to NULL / 'pending').

    Returns the game id (existing or newly created).
    """
    conn = _get_conn()
    content_hash = fields["content_hash"]

    # Dedup check.
    existing = conn.execute(
        "SELECT id FROM games WHERE content_hash = ?", (content_hash,)
    ).fetchone()
    if existing is not None:
        return existing["id"]

    status = fields.get("analysis_status", "pending")
    if status not in _ANALYSIS_STATUSES:
        raise ValueError(f"invalid analysis_status: {status!r}")

    conn.execute(
        """
        INSERT INTO games
            (content_hash, pgn, headers_json, white, black, result, eco, opening,
             date, my_color, source, ply_count, imported_at, analysis_status)
        VALUES
            (:content_hash, :pgn, :headers_json, :white, :black, :result, :eco,
             :opening, :date, :my_color, :source, :ply_count, :imported_at,
             :analysis_status)
        """,
        {
            "content_hash": content_hash,
            "pgn": fields["pgn"],
            "headers_json": fields.get("headers_json"),
            "white": fields.get("white"),
            "black": fields.get("black"),
            "result": fields.get("result"),
            "eco": fields.get("eco"),
            "opening": fields.get("opening"),
            "date": fields.get("date"),
            "my_color": fields.get("my_color"),
            "source": fields.get("source"),
            "ply_count": fields.get("ply_count"),
            "imported_at": fields["imported_at"],
            "analysis_status": status,
        },
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM games WHERE content_hash = ?", (content_hash,)
    ).fetchone()
    return row["id"]


def list_games() -> list[dict]:
    """Return all games as dicts, most recently imported first."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM games ORDER BY imported_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_game(game_id: int) -> Optional[dict]:
    """Return a single game row as a dict, or None if not found."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    return dict(row) if row else None


def delete_game(game_id: int) -> bool:
    """Delete a game and cascade to game_plies + leaks.

    Returns True if a row was deleted, False if the id was not found.
    """
    conn = _get_conn()
    cur = conn.execute("DELETE FROM games WHERE id = ?", (game_id,))
    conn.commit()
    return cur.rowcount > 0


def set_status(game_id: int, status: str) -> None:
    """Update analysis_status for a game.  Raises ValueError on an invalid status."""
    if status not in _ANALYSIS_STATUSES:
        raise ValueError(f"invalid analysis_status: {status!r}")
    conn = _get_conn()
    conn.execute(
        "UPDATE games SET analysis_status = ? WHERE id = ?", (status, game_id)
    )
    conn.commit()


def reset_analyzing_to_pending() -> int:
    """Flip all 'analyzing' rows → 'pending' (startup recovery after a crash/restart).

    Returns the number of rows updated.
    """
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE games SET analysis_status = 'pending' WHERE analysis_status = 'analyzing'"
    )
    conn.commit()
    if cur.rowcount:
        logger.info("storage: reset %d 'analyzing' game(s) to 'pending'", cur.rowcount)
    return cur.rowcount


# ---------------------------------------------------------------------------
# pos_cache helpers
# ---------------------------------------------------------------------------

def upsert_pos_cache(
    epd_key: str,
    depth: int,
    eval_cp_white: Optional[int],
    mate_white: Optional[int],
    best_uci: Optional[str],
    best_san: Optional[str],
    pv_san_json: Optional[str],
    pv2_cp_white: Optional[int] = None,
) -> None:
    """Insert or update a pos_cache row.

    Idempotent on (epd_key, depth): re-inserting the same key updates the values.
    All eval values must already be White-POV-normalized by the caller.
    """
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO pos_cache
            (epd_key, depth, eval_cp_white, mate_white, best_uci, best_san,
             pv_san_json, pv2_cp_white)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(epd_key, depth) DO UPDATE SET
            eval_cp_white = excluded.eval_cp_white,
            mate_white    = excluded.mate_white,
            best_uci      = excluded.best_uci,
            best_san      = excluded.best_san,
            pv_san_json   = excluded.pv_san_json,
            pv2_cp_white  = excluded.pv2_cp_white
        """,
        (epd_key, depth, eval_cp_white, mate_white, best_uci, best_san,
         pv_san_json, pv2_cp_white),
    )
    conn.commit()


def get_pos_cache(epd_key: str, depth: int) -> Optional[dict]:
    """Return a pos_cache row as a dict, or None if not found."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM pos_cache WHERE epd_key = ? AND depth = ?", (epd_key, depth)
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# game_plies helpers
# ---------------------------------------------------------------------------

def write_plies(game_id: int, plies: list[dict[str, Any]]) -> None:
    """Bulk-write game_plies for a game (replaces any existing plies for the game).

    Each dict in *plies* must have a 'ply' key and may include any subset of the
    other game_plies columns (omitted columns default to NULL).
    """
    conn = _get_conn()
    conn.execute("DELETE FROM game_plies WHERE game_id = ?", (game_id,))
    conn.executemany(
        """
        INSERT INTO game_plies
            (game_id, ply, san, uci, fen_before, eval_cp_white, mate_white,
             win_prob, is_user_move, clock_centis)
        VALUES
            (:game_id, :ply, :san, :uci, :fen_before, :eval_cp_white, :mate_white,
             :win_prob, :is_user_move, :clock_centis)
        """,
        [
            {
                "game_id": game_id,
                "ply": p["ply"],
                "san": p.get("san"),
                "uci": p.get("uci"),
                "fen_before": p.get("fen_before"),
                "eval_cp_white": p.get("eval_cp_white"),
                "mate_white": p.get("mate_white"),
                "win_prob": p.get("win_prob"),
                "is_user_move": int(p.get("is_user_move", False)),
                "clock_centis": p.get("clock_centis"),
            }
            for p in plies
        ],
    )
    conn.commit()


def get_plies(game_id: int) -> list[dict]:
    """Return game_plies for a game ordered by ply."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM game_plies WHERE game_id = ? ORDER BY ply", (game_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# leaks helpers
# ---------------------------------------------------------------------------

def write_leaks(game_id: int, leaks: list[LeakRecord]) -> None:
    """Bulk-write leaks for a game (replaces any existing leaks for the game)."""
    conn = _get_conn()
    conn.execute("DELETE FROM leaks WHERE game_id = ?", (game_id,))
    conn.executemany(
        """
        INSERT INTO leaks
            (game_id, ply, color, severity, category, motif_json, phase,
             win_prob_before, win_prob_after, win_prob_drop,
             hung_square, threat_uci, threat_motif,
             best_uci, best_san, lead_in_ply, tags_json, explanation_json)
        VALUES
            (:game_id, :ply, :color, :severity, :category, :motif_json, :phase,
             :win_prob_before, :win_prob_after, :win_prob_drop,
             :hung_square, :threat_uci, :threat_motif,
             :best_uci, :best_san, :lead_in_ply, :tags_json, :explanation_json)
        """,
        [
            {
                "game_id": game_id,
                "ply": lk.ply,
                "color": lk.color,
                "severity": lk.severity,
                "category": lk.category,
                "motif_json": lk.motif_json,
                "phase": lk.phase,
                "win_prob_before": lk.win_prob_before,
                "win_prob_after": lk.win_prob_after,
                "win_prob_drop": lk.win_prob_drop,
                "hung_square": lk.hung_square,
                "threat_uci": lk.threat_uci,
                "threat_motif": lk.threat_motif,
                "best_uci": lk.best_uci,
                "best_san": lk.best_san,
                "lead_in_ply": lk.lead_in_ply,
                "tags_json": lk.tags_json,
                "explanation_json": lk.explanation_json,
            }
            for lk in leaks
        ],
    )
    conn.commit()


def get_leaks(game_id: int) -> list[dict]:
    """Return leaks for a game ordered by ply."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM leaks WHERE game_id = ? ORDER BY ply", (game_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Color-tagging helpers
# ---------------------------------------------------------------------------

_VALID_COLORS = frozenset({"white", "black"})


def set_my_color(game_id: int, color: Optional[str]) -> bool:
    """Set my_color for a game and reset its analysis_status to 'pending'.

    The reset is required because leaks are computed per-color: changing the
    color invalidates any previously written leaks.

    Args:
        game_id: The game to update.
        color:   'white', 'black', or None (clears the color tag).

    Returns:
        True if a row was found and updated, False if game_id does not exist.

    Raises:
        ValueError: if color is not 'white', 'black', or None.
    """
    if color is not None and color not in _VALID_COLORS:
        raise ValueError(f"invalid color: {color!r}; must be 'white', 'black', or None")
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE games SET my_color = ?, analysis_status = 'pending' WHERE id = ?",
        (color, game_id),
    )
    conn.commit()
    return cur.rowcount > 0


def retag_colors_by_aliases(aliases: list[str]) -> int:
    """Bulk-set my_color on games whose White/Black names match any alias.

    Implements the same case-insensitive trimmed matching as
    pgn._infer_my_color.  For each matching game, my_color is updated and
    analysis_status is reset to 'pending' (invalidates stale leaks).
    Games that do not match any alias are left untouched.

    Args:
        aliases: List of username strings to match against (e.g. from the
                 CHESS_USERNAME env var, split on commas).

    Returns:
        The number of game rows that were actually updated.
    """
    if not aliases:
        return 0

    # Normalise aliases once.
    normalised = [a.strip().lower() for a in aliases if a.strip()]
    if not normalised:
        return 0

    conn = _get_conn()
    rows = conn.execute("SELECT id, white, black FROM games").fetchall()

    updated = 0
    for row in rows:
        game_id = row[0]
        white = (row[1] or "").strip().lower()
        black = (row[2] or "").strip().lower()

        my_color: Optional[str] = None
        for alias in normalised:
            if alias == white:
                my_color = "white"
                break
            if alias == black:
                my_color = "black"
                break

        if my_color is not None:
            conn.execute(
                "UPDATE games SET my_color = ?, analysis_status = 'pending' WHERE id = ?",
                (my_color, game_id),
            )
            updated += 1

    if updated:
        conn.commit()
    return updated


def coverage() -> dict:
    """Return a dict summarising how many games are tagged / analyzed.

    Keys:
        total:    Total number of game rows.
        tagged:   Games with my_color IS NOT NULL.
        analyzed: Games with analysis_status = 'done'.
        pending:  Games with analysis_status = 'pending'.
    """
    conn = _get_conn()
    row = conn.execute(
        """
        SELECT
            COUNT(*)                                          AS total,
            SUM(CASE WHEN my_color IS NOT NULL THEN 1 ELSE 0 END) AS tagged,
            SUM(CASE WHEN analysis_status = 'done'    THEN 1 ELSE 0 END) AS analyzed,
            SUM(CASE WHEN analysis_status = 'pending' THEN 1 ELSE 0 END) AS pending
        FROM games
        """
    ).fetchone()
    return {
        "total":    row[0] or 0,
        "tagged":   row[1] or 0,
        "analyzed": row[2] or 0,
        "pending":  row[3] or 0,
    }
