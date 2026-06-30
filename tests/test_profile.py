"""
tests/test_profile.py — Unit tests for app/profile.py.

Seeds a temp DB (storage.init(tmp_path)), inserts games + leaks via storage
CRUD helpers, then asserts build_profile() returns correct aggregates.

No engine, no network, no Stockfish.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import app.storage as storage
from app.profile import build_profile
from app.storage import LeakRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_db(tmp_path: Path) -> str:
    db_path = str(tmp_path / "test_games.db")
    storage.init(db_path)
    return db_path


def _insert_game(
    *,
    content_hash: str,
    my_color: str | None,
    opening: str | None = "Sicilian",
    analysis_status: str = "done",
) -> int:
    return storage.insert_game(
        {
            "content_hash": content_hash,
            "pgn": "[Event \"?\"]\n1. e4 e5 *",
            "imported_at": "2026-01-15T10:00:00",
            "white": "Alice",
            "black": "Bob",
            "ply_count": 2,
            "my_color": my_color,
            "opening": opening,
            "analysis_status": analysis_status,
        }
    )


def _set_done(game_id: int) -> None:
    storage.set_status(game_id, "done")


def _make_leak(
    game_id: int,
    *,
    ply: int = 20,
    color: str = "white",
    category: str = "hanging",
    phase: str = "middlegame",
    severity: str = "blunder",
) -> LeakRecord:
    return LeakRecord(
        game_id=game_id,
        ply=ply,
        color=color,
        severity=severity,
        category=category,
        phase=phase,
        win_prob_before=0.65,
        win_prob_after=0.35,
        win_prob_drop=0.30,
    )


# ---------------------------------------------------------------------------
# Seed a canonical DB for most tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def seeded_db(tmp_path):
    """
    Seed a DB with:
      - game A (my_color='white', status='done', opening='Italian'):
          2 leaks: hanging(middlegame), knight_fork(opening)
      - game B (my_color='black', status='done', opening='Sicilian'):
          1 leak: missed_threat(endgame) — counts toward hope-chess rate
      - game C (my_color='white', status='done', opening='Italian'):
          1 leak: hanging(middlegame)
      - game D (my_color=NULL, status='done', opening='French'):
          1 leak: hanging — MUST NOT appear in profile
      - game E (my_color='white', status='pending', opening='Ruy Lopez'):
          no leaks — not done, shouldn't count
    """
    _init_db(tmp_path)

    id_a = _insert_game(content_hash="a", my_color="white", opening="Italian")
    id_b = _insert_game(content_hash="b", my_color="black", opening="Sicilian")
    id_c = _insert_game(content_hash="c", my_color="white", opening="Italian")
    id_d = _insert_game(content_hash="d", my_color=None, opening="French")
    id_e = _insert_game(content_hash="e", my_color="white", opening="Ruy Lopez",
                        analysis_status="pending")

    # Write leaks for A
    storage.write_leaks(id_a, [
        _make_leak(id_a, category="hanging", phase="middlegame", color="white"),
        _make_leak(id_a, ply=10, category="knight_fork", phase="opening", color="white"),
    ])

    # Write leaks for B
    storage.write_leaks(id_b, [
        _make_leak(id_b, category="missed_threat", phase="endgame", color="black"),
    ])

    # Write leaks for C
    storage.write_leaks(id_c, [
        _make_leak(id_c, category="hanging", phase="middlegame", color="white"),
    ])

    # Write leaks for D (NULL color — must NOT appear)
    storage.write_leaks(id_d, [
        _make_leak(id_d, category="hanging", color="white"),
    ])

    # E has status='pending' — excluded

    return {
        "id_a": id_a, "id_b": id_b, "id_c": id_c,
        "id_d": id_d, "id_e": id_e,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGamesAnalyzed:
    def test_counts_only_done_and_known_color(self, seeded_db):
        """Only games with my_color IS NOT NULL AND status='done' are counted."""
        profile = build_profile()
        # A (white, done), B (black, done), C (white, done) = 3
        assert profile["games_analyzed"] == 3

    def test_null_color_excluded(self, seeded_db):
        """Game D (my_color=NULL) is never counted."""
        profile = build_profile()
        # If D were counted it would be 4.
        assert profile["games_analyzed"] == 3

    def test_pending_game_excluded(self, seeded_db):
        """Game E (status='pending') is not counted even though my_color is set."""
        profile = build_profile()
        assert profile["games_analyzed"] == 3


class TestTopLeaks:
    def test_top_leaks_ordering(self, seeded_db):
        """top_leaks is sorted descending by count."""
        profile = build_profile()
        top = profile["top_leaks"]
        assert len(top) >= 1
        counts = [e["count"] for e in top]
        assert counts == sorted(counts, reverse=True)

    def test_hanging_is_top(self, seeded_db):
        """Hanging has 2 leaks (A+C), knight_fork has 1, missed_threat has 1."""
        profile = build_profile()
        categories = [e["category"] for e in profile["top_leaks"]]
        assert categories[0] == "hanging"

    def test_null_color_leaks_not_counted(self, seeded_db):
        """Game D's hanging leak must NOT appear in top_leaks count."""
        profile = build_profile()
        # hanging: game A (1) + game C (1) = 2; NOT including game D's leak
        hanging = next(e for e in profile["top_leaks"] if e["category"] == "hanging")
        assert hanging["count"] == 2

    def test_total_leak_count_excludes_null_games(self, seeded_db):
        """Sum of all top_leak counts = 4 (2 hanging + 1 knight_fork + 1 missed_threat)."""
        profile = build_profile()
        total = sum(e["count"] for e in profile["top_leaks"])
        assert total == 4

    def test_coach_field_non_empty(self, seeded_db):
        """Each top-leak entry has a non-empty 'coach' string."""
        profile = build_profile()
        for entry in profile["top_leaks"]:
            assert entry.get("coach"), f"coach empty for {entry['category']}"


class TestByPhase:
    def test_by_phase_keys(self, seeded_db):
        profile = build_profile()
        assert set(profile["by_phase"].keys()) == {"opening", "middlegame", "endgame"}

    def test_by_phase_counts(self, seeded_db):
        """opening=1 (knight_fork, game A), middlegame=2 (hanging A+C), endgame=1 (missed_threat B)."""
        profile = build_profile()
        bp = profile["by_phase"]
        assert bp["opening"] == 1
        assert bp["middlegame"] == 2
        assert bp["endgame"] == 1

    def test_by_phase_null_game_excluded(self, seeded_db):
        """Game D's middlegame hanging must not inflate by_phase['middlegame']."""
        profile = build_profile()
        # If D were included, middlegame would be 3.
        assert profile["by_phase"]["middlegame"] == 2


class TestByColor:
    def test_by_color_keys(self, seeded_db):
        profile = build_profile()
        assert set(profile["by_color"].keys()) == {"white", "black"}

    def test_by_color_counts(self, seeded_db):
        """white leaks: 3 (A: 2, C: 1). black leaks: 1 (B)."""
        profile = build_profile()
        bc = profile["by_color"]
        assert bc["white"] == 3
        assert bc["black"] == 1


class TestHopeChessRate:
    def test_hope_chess_rate_correct(self, seeded_db):
        """1 of 3 analysed games has a missed_threat leak → rate = 1/3."""
        profile = build_profile()
        assert abs(profile["hope_chess_rate"] - 1 / 3) < 1e-9

    def test_hope_chess_rate_zero_when_no_missed_threats(self, tmp_path):
        """If no missed_threat leaks exist, rate is 0."""
        _init_db(tmp_path)
        gid = _insert_game(content_hash="x", my_color="white")
        storage.write_leaks(gid, [_make_leak(gid, category="hanging")])
        profile = build_profile()
        assert profile["hope_chess_rate"] == 0.0

    def test_hope_chess_rate_one_when_all_games_have_threat(self, tmp_path):
        """If every game has a missed_threat, rate = 1.0."""
        _init_db(tmp_path)
        gid1 = _insert_game(content_hash="p", my_color="white")
        gid2 = _insert_game(content_hash="q", my_color="black")
        storage.write_leaks(gid1, [_make_leak(gid1, category="missed_threat")])
        storage.write_leaks(gid2, [_make_leak(gid2, category="missed_threat", color="black")])
        profile = build_profile()
        assert profile["hope_chess_rate"] == 1.0

    def test_hope_chess_rate_null_color_excluded(self, seeded_db):
        """Rate is over 3 qualified games, not 4 (D with NULL color excluded)."""
        profile = build_profile()
        # Numerator is 1 (B has missed_threat), denominator is 3 — not 4.
        assert abs(profile["hope_chess_rate"] - 1 / 3) < 1e-9


class TestByOpening:
    def test_by_opening_structure(self, seeded_db):
        profile = build_profile()
        assert isinstance(profile["by_opening"], list)
        for item in profile["by_opening"]:
            assert "opening" in item
            assert "count" in item

    def test_by_opening_top_is_italian(self, seeded_db):
        """Italian has 3 leaks (A: 2, C: 1); Sicilian has 1."""
        profile = build_profile()
        top = profile["by_opening"]
        assert top[0]["opening"] == "Italian"
        assert top[0]["count"] == 3

    def test_french_null_color_excluded(self, seeded_db):
        """French opening (game D, NULL color) must not appear."""
        profile = build_profile()
        openings = [e["opening"] for e in profile["by_opening"]]
        assert "French" not in openings


class TestTrend:
    def test_trend_structure(self, seeded_db):
        profile = build_profile()
        assert isinstance(profile["trend"], list)
        for item in profile["trend"]:
            assert "bucket" in item
            assert "count" in item

    def test_trend_counts_match_total(self, seeded_db):
        profile = build_profile()
        total_trend = sum(e["count"] for e in profile["trend"])
        total_top = sum(e["count"] for e in profile["top_leaks"])
        assert total_trend == total_top


class TestEmptyDB:
    def test_empty_db_returns_zeros(self, tmp_path):
        _init_db(tmp_path)
        profile = build_profile()
        assert profile["games_analyzed"] == 0
        assert profile["top_leaks"] == []
        assert profile["by_phase"] == {"opening": 0, "middlegame": 0, "endgame": 0}
        assert profile["by_color"] == {"white": 0, "black": 0}
        assert profile["hope_chess_rate"] == 0.0
        assert profile["by_opening"] == []
        assert profile["trend"] == []

    def test_only_null_color_games_still_empty(self, tmp_path):
        """If all games have my_color=NULL, profile should look like an empty DB."""
        _init_db(tmp_path)
        gid = _insert_game(content_hash="null_only", my_color=None)
        storage.write_leaks(gid, [_make_leak(gid, category="hanging")])
        profile = build_profile()
        assert profile["games_analyzed"] == 0
        assert profile["top_leaks"] == []
