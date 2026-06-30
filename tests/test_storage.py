"""
tests/test_storage.py — Pure unit tests for app/storage.py.

No engine, no FastAPI, no network.  Uses a temporary DB per test (tmp_path fixture).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import app.storage as storage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init(tmp_path: Path, subdir: str = "games.db") -> str:
    """Init storage with a temp DB path and return the path string."""
    db_path = str(tmp_path / subdir)
    storage.init(db_path)
    return db_path


def _game(**overrides) -> dict:
    """Return a minimal valid game fields dict."""
    base = {
        "content_hash": "abc123",
        "pgn": "[Event \"?\"]\n1. e4 e5 *",
        "imported_at": "2026-01-01T00:00:00",
        "white": "White",
        "black": "Black",
        "ply_count": 2,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Schema creation and migration idempotency
# ---------------------------------------------------------------------------

class TestSchema:
    def test_creates_from_empty(self, tmp_path):
        """init() on a brand-new path creates all tables and schema_meta."""
        db_path = _init(tmp_path)
        conn = sqlite3.connect(db_path)
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert {"schema_meta", "games", "pos_cache", "game_plies", "leaks"}.issubset(tables)

    def test_schema_meta_stamped(self, tmp_path):
        """schema_meta has exactly one row after init."""
        db_path = _init(tmp_path)
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT version FROM schema_meta").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == storage._SCHEMA_VERSION

    def test_migration_idempotent(self, tmp_path):
        """Calling init() twice on the same DB does not raise and doesn't duplicate schema_meta rows."""
        db_path = _init(tmp_path)
        storage.init(db_path)  # second call
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT version FROM schema_meta").fetchall()
        conn.close()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# insert_game dedup
# ---------------------------------------------------------------------------

class TestInsertGame:
    def test_insert_returns_id(self, tmp_path):
        _init(tmp_path)
        gid = storage.insert_game(_game())
        assert isinstance(gid, int)
        assert gid > 0

    def test_dedup_returns_same_id(self, tmp_path):
        _init(tmp_path)
        g = _game()
        id1 = storage.insert_game(g)
        id2 = storage.insert_game(g)  # same content_hash
        assert id1 == id2

    def test_dedup_does_not_change_row(self, tmp_path):
        _init(tmp_path)
        storage.insert_game(_game(white="OriginalWhite"))
        storage.insert_game(_game(white="ChangedWhite"))  # same hash, different white
        row = storage.get_game(1)
        assert row["white"] == "OriginalWhite"  # unchanged

    def test_different_hash_gets_new_id(self, tmp_path):
        _init(tmp_path)
        id1 = storage.insert_game(_game(content_hash="hash1"))
        id2 = storage.insert_game(_game(content_hash="hash2"))
        assert id1 != id2

    def test_my_color_nullable(self, tmp_path):
        _init(tmp_path)
        gid = storage.insert_game(_game(my_color=None))
        row = storage.get_game(gid)
        assert row["my_color"] is None

    def test_default_status_pending(self, tmp_path):
        _init(tmp_path)
        gid = storage.insert_game(_game())
        row = storage.get_game(gid)
        assert row["analysis_status"] == "pending"


# ---------------------------------------------------------------------------
# list_games / get_game / delete_game
# ---------------------------------------------------------------------------

class TestGameCRUD:
    def test_list_games_empty(self, tmp_path):
        _init(tmp_path)
        assert storage.list_games() == []

    def test_list_games_returns_inserted(self, tmp_path):
        _init(tmp_path)
        storage.insert_game(_game(content_hash="h1"))
        storage.insert_game(_game(content_hash="h2"))
        rows = storage.list_games()
        assert len(rows) == 2

    def test_get_game_not_found(self, tmp_path):
        _init(tmp_path)
        assert storage.get_game(999) is None

    def test_get_game_found(self, tmp_path):
        _init(tmp_path)
        gid = storage.insert_game(_game(white="Alice"))
        row = storage.get_game(gid)
        assert row["white"] == "Alice"

    def test_delete_game_returns_true(self, tmp_path):
        _init(tmp_path)
        gid = storage.insert_game(_game())
        assert storage.delete_game(gid) is True

    def test_delete_game_not_found_returns_false(self, tmp_path):
        _init(tmp_path)
        assert storage.delete_game(999) is False

    def test_delete_removes_row(self, tmp_path):
        _init(tmp_path)
        gid = storage.insert_game(_game())
        storage.delete_game(gid)
        assert storage.get_game(gid) is None


# ---------------------------------------------------------------------------
# set_status
# ---------------------------------------------------------------------------

class TestSetStatus:
    def test_set_status_valid(self, tmp_path):
        _init(tmp_path)
        gid = storage.insert_game(_game())
        for status in ("analyzing", "done", "failed", "pending"):
            storage.set_status(gid, status)
            assert storage.get_game(gid)["analysis_status"] == status

    def test_set_status_invalid_raises(self, tmp_path):
        _init(tmp_path)
        gid = storage.insert_game(_game())
        with pytest.raises(ValueError):
            storage.set_status(gid, "invalid_status")


# ---------------------------------------------------------------------------
# reset_analyzing_to_pending
# ---------------------------------------------------------------------------

class TestResetAnalyzing:
    def test_flips_analyzing_to_pending(self, tmp_path):
        _init(tmp_path)
        gid1 = storage.insert_game(_game(content_hash="h1"))
        gid2 = storage.insert_game(_game(content_hash="h2"))
        storage.set_status(gid1, "analyzing")
        storage.set_status(gid2, "done")

        count = storage.reset_analyzing_to_pending()

        assert count == 1
        assert storage.get_game(gid1)["analysis_status"] == "pending"
        assert storage.get_game(gid2)["analysis_status"] == "done"  # unchanged

    def test_no_analyzing_rows_returns_zero(self, tmp_path):
        _init(tmp_path)
        storage.insert_game(_game())
        count = storage.reset_analyzing_to_pending()
        assert count == 0


# ---------------------------------------------------------------------------
# upsert_pos_cache
# ---------------------------------------------------------------------------

class TestUpsertPosCache:
    def test_insert_and_retrieve(self, tmp_path):
        _init(tmp_path)
        storage.upsert_pos_cache(
            epd_key="rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq",
            depth=20,
            eval_cp_white=30,
            mate_white=None,
            best_uci="e7e5",
            best_san="e5",
            pv_san_json='["e5","Nf3"]',
            pv2_cp_white=10,
        )
        row = storage.get_pos_cache(
            "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq", 20
        )
        assert row is not None
        assert row["eval_cp_white"] == 30
        assert row["pv2_cp_white"] == 10

    def test_idempotent_on_same_key(self, tmp_path):
        """Re-inserting the same (epd_key, depth) → one row, values updated."""
        _init(tmp_path)
        epd = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq"
        storage.upsert_pos_cache(epd, 15, 20, None, "e7e5", "e5", "[]")
        storage.upsert_pos_cache(epd, 15, 35, None, "c7c5", "c5", '["c5"]')  # update

        # Only one row for this (epd, depth).
        conn = sqlite3.connect(str(tmp_path / "games.db"))
        rows = conn.execute(
            "SELECT * FROM pos_cache WHERE epd_key = ? AND depth = ?", (epd, 15)
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][2] == 35      # eval_cp_white updated
        assert rows[0][4] == "c7c5"  # best_uci updated

    def test_transposition_shares_one_row(self, tmp_path):
        """Two FENs differing only in move counters map to the same EPD key."""
        _init(tmp_path)
        # EPD strips the move counters — both FENs produce the same EPD.
        import chess
        fen1 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"
        fen2 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 5 10"
        epd1 = chess.Board(fen1).epd()
        epd2 = chess.Board(fen2).epd()
        assert epd1 == epd2  # confirms transposition identity

        storage.upsert_pos_cache(epd1, 18, 30, None, "e7e5", "e5", "[]")
        storage.upsert_pos_cache(epd2, 18, 30, None, "e7e5", "e5", "[]")  # same EPD

        conn = sqlite3.connect(str(tmp_path / "games.db"))
        count = conn.execute("SELECT COUNT(*) FROM pos_cache").fetchone()[0]
        conn.close()
        assert count == 1


# ---------------------------------------------------------------------------
# write_plies
# ---------------------------------------------------------------------------

class TestWritePlies:
    def test_write_and_retrieve(self, tmp_path):
        _init(tmp_path)
        gid = storage.insert_game(_game())
        plies = [
            {"ply": 1, "san": "e4", "uci": "e2e4", "is_user_move": True},
            {"ply": 2, "san": "e5", "uci": "e7e5", "is_user_move": False},
        ]
        storage.write_plies(gid, plies)
        rows = storage.get_plies(gid)
        assert len(rows) == 2
        assert rows[0]["san"] == "e4"
        assert rows[1]["san"] == "e5"

    def test_write_plies_replaces_existing(self, tmp_path):
        _init(tmp_path)
        gid = storage.insert_game(_game())
        storage.write_plies(gid, [{"ply": 1, "san": "e4", "uci": "e2e4"}])
        storage.write_plies(gid, [{"ply": 1, "san": "d4", "uci": "d2d4"}])
        rows = storage.get_plies(gid)
        assert len(rows) == 1
        assert rows[0]["san"] == "d4"


# ---------------------------------------------------------------------------
# write_leaks / cascade delete
# ---------------------------------------------------------------------------

class TestWriteLeaks:
    def _make_leak(self, game_id: int, ply: int = 5) -> storage.LeakRecord:
        return storage.LeakRecord(
            game_id=game_id,
            ply=ply,
            color="white",
            severity="blunder",
            category="hanging",
            phase="middlegame",
            win_prob_before=0.75,
            win_prob_after=0.30,
            win_prob_drop=0.45,
            lead_in_ply=3,
        )

    def test_write_and_retrieve(self, tmp_path):
        _init(tmp_path)
        gid = storage.insert_game(_game())
        storage.write_leaks(gid, [self._make_leak(gid)])
        rows = storage.get_leaks(gid)
        assert len(rows) == 1
        assert rows[0]["severity"] == "blunder"
        assert rows[0]["win_prob_drop"] == pytest.approx(0.45)
        assert rows[0]["lead_in_ply"] == 3

    def test_write_leaks_replaces_existing(self, tmp_path):
        _init(tmp_path)
        gid = storage.insert_game(_game())
        storage.write_leaks(gid, [self._make_leak(gid, ply=5)])
        storage.write_leaks(gid, [self._make_leak(gid, ply=10)])
        rows = storage.get_leaks(gid)
        assert len(rows) == 1
        assert rows[0]["ply"] == 10


class TestCascadeDelete:
    def test_delete_game_cascades_to_plies(self, tmp_path):
        _init(tmp_path)
        gid = storage.insert_game(_game())
        storage.write_plies(gid, [{"ply": 1, "san": "e4", "uci": "e2e4"}])
        storage.delete_game(gid)
        # Directly query to confirm cascade.
        conn = sqlite3.connect(str(tmp_path / "games.db"))
        count = conn.execute(
            "SELECT COUNT(*) FROM game_plies WHERE game_id = ?", (gid,)
        ).fetchone()[0]
        conn.close()
        assert count == 0

    def test_delete_game_cascades_to_leaks(self, tmp_path):
        _init(tmp_path)
        gid = storage.insert_game(_game())
        leak = storage.LeakRecord(
            game_id=gid, ply=5, color="white", severity="mistake",
            category="fork", phase="middlegame",
            win_prob_before=0.6, win_prob_after=0.4, win_prob_drop=0.2,
        )
        storage.write_leaks(gid, [leak])
        storage.delete_game(gid)
        conn = sqlite3.connect(str(tmp_path / "games.db"))
        count = conn.execute(
            "SELECT COUNT(*) FROM leaks WHERE game_id = ?", (gid,)
        ).fetchone()[0]
        conn.close()
        assert count == 0


# ---------------------------------------------------------------------------
# init() safety: missing directory
# ---------------------------------------------------------------------------

class TestInitSafety:
    def test_init_creates_missing_parent_dir(self, tmp_path):
        """init() creates parent dirs if needed — does not raise."""
        deep_path = str(tmp_path / "a" / "b" / "c" / "games.db")
        storage.init(deep_path)
        # If no exception, and the file now exists, we're good.
        assert Path(deep_path).exists()

    def test_import_does_not_open_db(self):
        """Importing the module never opens a DB (module-level _conn is None initially)."""
        # Re-import to confirm the module-level state starts as None when _conn is None.
        # We can't truly re-import in the same process, so we just verify the attr exists
        # and that calling list_games() before init() raises RuntimeError.
        storage._conn = None  # reset
        with pytest.raises(RuntimeError, match="storage.init"):
            storage.list_games()

    def test_init_with_env_override(self, tmp_path, monkeypatch):
        """GAMES_DB env var is honoured when db_path arg is None."""
        db_path = str(tmp_path / "env_override.db")
        monkeypatch.setenv("GAMES_DB", db_path)
        storage.init(None)  # should pick up env var
        assert Path(db_path).exists()


# ---------------------------------------------------------------------------
# LeakRecord dataclass
# ---------------------------------------------------------------------------

class TestLeakRecord:
    def test_defaults(self):
        lk = storage.LeakRecord(
            game_id=1, ply=10, color="black", severity="blunder",
            category="pin", phase="endgame",
            win_prob_before=0.8, win_prob_after=0.2, win_prob_drop=0.6,
        )
        assert lk.id is None
        assert lk.hung_square is None
        assert lk.lead_in_ply is None
        assert lk.explanation_json is None
