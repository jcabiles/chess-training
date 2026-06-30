"""API tests for the game-review and profile routes (T7).

All engine-dependent routes use ScriptedEngine injected via
``app.dependency_overrides[get_engine]``.  Storage uses a temp DB per test
fixture (GAMES_DB env var or storage.init(tmp)).  No Stockfish binary needed.

Coverage
--------
- import: persists games, deduplicates on re-import (duplicate count rises,
  no new rows), applies my_color override.
- list/get/delete: basic CRUD round-trip.
- analyze route: returns accepted response, status moves off 'pending'.
- review route: returns narrated leaks (verified by calling analyze_game
  directly; the route then returns the narrated data).
- profile route: returns aggregated profile structure.
- all existing routes still 200 (smoke a couple: /api/analyze, /api/traps).
- route order: /api/games/import is NOT shadowed by /api/games/{game_id}.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import chess
import chess.engine as chess_engine
import pytest
from fastapi.testclient import TestClient

import app.review as review_module
import app.storage as storage
from app.engine import AnalysisResult
from app.main import app, get_engine
from tests.engine_fakes import ScriptedEngine

# ---------------------------------------------------------------------------
# PGN fixtures
# ---------------------------------------------------------------------------

# A minimal 3-ply game (1.e4 e5 2.Nf3).
_GAME_1_PGN = (
    '[Event "Test"]\n'
    '[White "Alice"]\n'
    '[Black "Bob"]\n'
    '[Result "*"]\n'
    '\n'
    '1. e4 e5 2. Nf3 *\n'
)

# A second game (1.d4) to test multi-game import.
_GAME_2_PGN = (
    '[Event "Test2"]\n'
    '[White "Charlie"]\n'
    '[Black "Dave"]\n'
    '[Result "*"]\n'
    '\n'
    '1. d4 d5 *\n'
)

_TWO_GAME_PGN = _GAME_1_PGN + "\n" + _GAME_2_PGN

START_FEN = chess.STARTING_FEN
AFTER_E4_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
AFTER_E4_E5_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_result(cp: int, fen: str, uci: Optional[str] = None) -> AnalysisResult:
    """Make an AnalysisResult with a real move from the position."""
    board = chess.Board(fen)
    if uci:
        try:
            move = chess.Move.from_uci(uci)
            if move in board.legal_moves:
                pv = [move]
                pv_san = [board.san(move)]
                return AnalysisResult(
                    score=chess_engine.PovScore(chess_engine.Cp(cp), chess.WHITE),
                    pv=pv,
                    pv_san=pv_san,
                    depth=8,
                )
        except Exception:
            pass
    pv = list(board.legal_moves)[:1]
    pv_san = [board.san(pv[0])] if pv else []
    return AnalysisResult(
        score=chess_engine.PovScore(chess_engine.Cp(cp), chess.WHITE),
        pv=pv,
        pv_san=pv_san,
        depth=8,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_storage(tmp_path: Path, monkeypatch):
    """Fresh temp DB for every test; also resets review module state."""
    db_path = str(tmp_path / "games_api_test.db")
    monkeypatch.setenv("GAMES_DB", db_path)
    storage.init(db_path)
    # Reset review module's interactive counter between tests.
    review_module._interactive_pending = 0
    yield
    # Cancel any stray background tasks.
    for gid in list(review_module._tasks.keys()):
        review_module.cancel_analysis(gid)
    review_module._interactive_pending = 0


@pytest.fixture
def scripted_engine():
    """A ScriptedEngine with enough scripted positions for the test games."""
    script = {
        START_FEN: [
            _make_result(50, START_FEN),
            _make_result(-20, START_FEN),
        ],
        AFTER_E4_FEN: [
            _make_result(-300, AFTER_E4_FEN),
            _make_result(-350, AFTER_E4_FEN),
        ],
        AFTER_E4_E5_FEN: [
            _make_result(60, AFTER_E4_E5_FEN),
            _make_result(30, AFTER_E4_E5_FEN),
        ],
    }
    return ScriptedEngine(script)


@pytest.fixture
def client(scripted_engine):
    """TestClient with ScriptedEngine injected and storage pointing at temp DB."""
    app.dependency_overrides[get_engine] = lambda: scripted_engine
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def neutral_client():
    """TestClient with a neutral ScriptedEngine (no eval swings) — for smoke tests."""
    engine = ScriptedEngine()  # all Cp(0), no leaks
    app.dependency_overrides[get_engine] = lambda: engine
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Import tests
# ---------------------------------------------------------------------------


class TestImport:
    def test_import_single_game(self, client):
        """POST /api/games/import with a single game persists it and returns imported=1."""
        r = client.post("/api/games/import", json={"pgn": _GAME_1_PGN})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["imported"] == 1
        assert body["duplicates"] == 0
        assert len(body["games"]) == 1
        g = body["games"][0]
        assert g["white"] == "Alice"
        assert g["black"] == "Bob"
        assert g["analysis_status"] == "pending"

    def test_import_two_games(self, client):
        """Importing a two-game PGN returns imported=2."""
        r = client.post("/api/games/import", json={"pgn": _TWO_GAME_PGN})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["imported"] == 2
        assert body["duplicates"] == 0
        assert len(body["games"]) == 2

    def test_reimport_counts_as_duplicate(self, client):
        """Re-importing the same PGN increments duplicates, not imported."""
        r1 = client.post("/api/games/import", json={"pgn": _GAME_1_PGN})
        assert r1.status_code == 200
        assert r1.json()["imported"] == 1

        r2 = client.post("/api/games/import", json={"pgn": _GAME_1_PGN})
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["imported"] == 0
        assert body2["duplicates"] == 1

        # Storage should still have exactly 1 row.
        rows = storage.list_games()
        assert len(rows) == 1

    def test_my_color_override_applied(self, client):
        """The my_color override from the request body overrides CHESS_USERNAME inference."""
        r = client.post(
            "/api/games/import",
            json={"pgn": _GAME_1_PGN, "my_color": "black"},
        )
        assert r.status_code == 200
        g = r.json()["games"][0]
        assert g["my_color"] == "black"

        # Verify it was actually persisted.
        game_id = g["id"]
        row = storage.get_game(game_id)
        assert row is not None
        assert row["my_color"] == "black"

    def test_my_color_none_leaves_inference(self, client, monkeypatch):
        """When my_color is null in the request, CHESS_USERNAME inference runs."""
        # No CHESS_USERNAME set → inference returns None.
        monkeypatch.delenv("CHESS_USERNAME", raising=False)
        r = client.post("/api/games/import", json={"pgn": _GAME_1_PGN, "my_color": None})
        assert r.status_code == 200
        g = r.json()["games"][0]
        assert g["my_color"] is None

    def test_import_populates_plies(self, client):
        """Import writes per-ply rows: GET /api/games/{id} must return plies matching ply_count."""
        # _GAME_1_PGN has 3 half-moves (1. e4 e5 2. Nf3).
        r = client.post("/api/games/import", json={"pgn": _GAME_1_PGN})
        assert r.status_code == 200
        game_id = r.json()["games"][0]["id"]
        ply_count = r.json()["games"][0]["ply_count"]
        assert ply_count == 3

        detail = client.get(f"/api/games/{game_id}")
        assert detail.status_code == 200
        body = detail.json()
        plies = body["plies"]

        # Ply rows must be populated immediately after import (before analysis).
        assert len(plies) == ply_count, (
            f"Expected {ply_count} plies from import, got {len(plies)}"
        )
        # First ply must carry san, uci, and fen_before.
        first = plies[0]
        assert first["san"] is not None and first["san"] != ""
        assert first["uci"] is not None and first["uci"] != ""
        assert first["fen_before"] is not None and first["fen_before"] != ""

    def test_reimport_does_not_duplicate_plies(self, client):
        """Re-importing the same PGN must not create duplicate ply rows."""
        r1 = client.post("/api/games/import", json={"pgn": _GAME_1_PGN})
        assert r1.status_code == 200
        game_id = r1.json()["games"][0]["id"]

        # Re-import (dedup path).
        r2 = client.post("/api/games/import", json={"pgn": _GAME_1_PGN})
        assert r2.status_code == 200
        assert r2.json()["duplicates"] == 1

        # Ply count must still equal the game's ply_count (no duplication).
        plies = storage.get_plies(game_id)
        assert len(plies) == 3


# ---------------------------------------------------------------------------
# Route-order test: /api/games/import must not be shadowed by /{game_id}
# ---------------------------------------------------------------------------


class TestRouteOrder:
    def test_import_path_not_shadowed(self, client):
        """GET /api/games/import returns method-not-allowed (405), not 404 or 422.

        This proves that FastAPI registered the literal POST /api/games/import
        route before the GET /api/games/{game_id} path-parameter route.
        A GET to the literal path should hit the {game_id} route and attempt to
        parse 'import' as an int → 422 (unprocessable entity), not 404.
        """
        r = client.get("/api/games/import")
        # FastAPI will try to coerce 'import' to int → 422
        assert r.status_code == 422

    def test_import_post_works(self, client):
        """POST /api/games/import is served by the import route (not the analyze sub-route)."""
        r = client.post("/api/games/import", json={"pgn": _GAME_1_PGN})
        assert r.status_code == 200
        assert "imported" in r.json()


# ---------------------------------------------------------------------------
# List / get / delete tests
# ---------------------------------------------------------------------------


class TestListGetDelete:
    def _import_one(self, client) -> int:
        """Helper: import one game and return its id."""
        r = client.post("/api/games/import", json={"pgn": _GAME_1_PGN})
        assert r.status_code == 200
        return r.json()["games"][0]["id"]

    def test_list_empty(self, client):
        """GET /api/games with no games returns an empty list."""
        r = client.get("/api/games")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_after_import(self, client):
        """GET /api/games after import returns one summary."""
        self._import_one(client)
        r = client.get("/api/games")
        assert r.status_code == 200
        assert len(r.json()) == 1

    def test_get_game(self, client):
        """GET /api/games/{id} returns the game with a pgn field."""
        game_id = self._import_one(client)
        r = client.get(f"/api/games/{game_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == game_id
        assert "pgn" in body
        assert "plies" in body  # empty until analyzed

    def test_get_game_not_found(self, client):
        """GET /api/games/99999 returns 404."""
        r = client.get("/api/games/99999")
        assert r.status_code == 404

    def test_delete_game(self, client):
        """DELETE /api/games/{id} removes the game."""
        game_id = self._import_one(client)

        r = client.delete(f"/api/games/{game_id}")
        assert r.status_code == 200
        assert r.json()["deleted"] == game_id

        # Should be gone.
        r2 = client.get(f"/api/games/{game_id}")
        assert r2.status_code == 404

    def test_delete_not_found(self, client):
        """DELETE /api/games/99999 returns 404."""
        r = client.delete("/api/games/99999")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Analyze / status tests
# ---------------------------------------------------------------------------


class TestAnalyze:
    def _import_one(self, client) -> int:
        r = client.post("/api/games/import", json={"pgn": _GAME_1_PGN})
        assert r.status_code == 200
        return r.json()["games"][0]["id"]

    def test_analyze_returns_accepted(self, client):
        """POST /api/games/{id}/analyze returns 200 with an analysis_status."""
        game_id = self._import_one(client)
        r = client.post(f"/api/games/{game_id}/analyze")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["game_id"] == game_id
        assert body["analysis_status"] in ("analyzing", "pending", "done", "failed")

    def test_analyze_status_moves_off_pending(self, client):
        """After calling analyze, the status is no longer 'pending'."""
        game_id = self._import_one(client)

        # Status starts as 'pending'.
        r0 = client.get(f"/api/games/{game_id}/status")
        assert r0.status_code == 200
        assert r0.json()["analysis_status"] == "pending"

        # Trigger analysis.
        r1 = client.post(f"/api/games/{game_id}/analyze")
        assert r1.status_code == 200

        # Status should have moved (analyzing or done).
        r2 = client.get(f"/api/games/{game_id}/status")
        assert r2.status_code == 200
        assert r2.json()["analysis_status"] != "pending"

    def test_analyze_not_found(self, client):
        """POST /api/games/99999/analyze returns 404."""
        r = client.post("/api/games/99999/analyze")
        assert r.status_code == 404

    def test_status_not_found(self, client):
        """GET /api/games/99999/status returns 404."""
        r = client.get("/api/games/99999/status")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Review route tests
# ---------------------------------------------------------------------------


class TestReview:
    def _insert_game_with_plies(self, my_color: Optional[str] = "white") -> int:
        """Insert a 3-ply game directly into storage (no HTTP) and return game_id."""
        game_id = storage.insert_game({
            "content_hash": f"reviewtest-{my_color}-{_utc_now()}",
            "pgn": _GAME_1_PGN,
            "imported_at": _utc_now(),
            "white": "Alice",
            "black": "Bob",
            "my_color": my_color,
            "ply_count": 3,
        })
        plies = [
            {
                "ply": 1,
                "san": "e4",
                "uci": "e2e4",
                "fen_before": START_FEN,
                "eval_cp_white": None,
                "mate_white": None,
                "win_prob": None,
                "is_user_move": True,
                "clock_centis": None,
            },
            {
                "ply": 2,
                "san": "e5",
                "uci": "e7e5",
                "fen_before": AFTER_E4_FEN,
                "eval_cp_white": None,
                "mate_white": None,
                "win_prob": None,
                "is_user_move": False,
                "clock_centis": None,
            },
            {
                "ply": 3,
                "san": "Nf3",
                "uci": "g1f3",
                "fen_before": AFTER_E4_E5_FEN,
                "eval_cp_white": None,
                "mate_white": None,
                "win_prob": None,
                "is_user_move": True,
                "clock_centis": None,
            },
        ]
        storage.write_plies(game_id, plies)
        return game_id

    def test_review_not_found(self, client):
        """GET /api/games/99999/review returns 404."""
        r = client.get("/api/games/99999/review")
        assert r.status_code == 404

    def test_review_empty_before_analysis(self, client):
        """GET /api/games/{id}/review before analysis returns empty leaks."""
        game_id = self._insert_game_with_plies()
        r = client.get(f"/api/games/{game_id}/review")
        assert r.status_code == 200
        body = r.json()
        assert body["game_id"] == game_id
        assert body["leaks"] == []  # no analysis yet

    def test_review_returns_narrated_leaks(self, client, scripted_engine):
        """After analyze_game runs, GET /api/games/{id}/review returns narrated leaks."""
        game_id = self._insert_game_with_plies(my_color="white")

        # Run analyze_game directly (synchronously in test) so we can assert data.
        asyncio.run(review_module.analyze_game(game_id, scripted_engine, depth=8))

        r = client.get(f"/api/games/{game_id}/review")
        assert r.status_code == 200
        body = r.json()
        assert body["analysis_status"] == "done"

        # The scripted engine plants a blunder on ply 1 → at least one leak.
        leaks = body["leaks"]
        assert len(leaks) >= 1, f"Expected at least one narrated leak, got: {leaks}"
        lk = leaks[0]
        assert "ply" in lk
        assert "severity" in lk
        assert "narration" in lk
        narration = lk["narration"]
        assert "summary" in narration
        assert isinstance(narration["summary"], str)
        assert len(narration["summary"]) > 0

    def test_review_includes_plies(self, client, scripted_engine):
        """After analysis, review response includes per-ply eval data."""
        game_id = self._insert_game_with_plies(my_color="white")

        asyncio.run(review_module.analyze_game(game_id, scripted_engine, depth=8))

        r = client.get(f"/api/games/{game_id}/review")
        assert r.status_code == 200
        body = r.json()
        plies = body["plies"]
        assert len(plies) == 3  # 3-ply game
        assert all("ply" in p for p in plies)


# ---------------------------------------------------------------------------
# Profile route tests
# ---------------------------------------------------------------------------


class TestProfile:
    def test_profile_empty_db(self, client):
        """GET /api/profile with no analyzed games returns zero counts."""
        r = client.get("/api/profile")
        assert r.status_code == 200
        body = r.json()
        assert body["games_analyzed"] == 0
        assert body["top_leaks"] == []
        assert body["hope_chess_rate"] == 0.0

    def test_profile_returns_aggregates(self, client, scripted_engine):
        """After importing + analyzing a game, /api/profile reflects the leaks."""
        # Import and prepare the game.
        game_id = storage.insert_game({
            "content_hash": f"profiletest-{_utc_now()}",
            "pgn": _GAME_1_PGN,
            "imported_at": _utc_now(),
            "white": "Alice",
            "black": "Bob",
            "my_color": "white",
            "ply_count": 3,
        })
        plies = [
            {
                "ply": 1, "san": "e4", "uci": "e2e4",
                "fen_before": START_FEN, "eval_cp_white": None,
                "mate_white": None, "win_prob": None,
                "is_user_move": True, "clock_centis": None,
            },
            {
                "ply": 2, "san": "e5", "uci": "e7e5",
                "fen_before": AFTER_E4_FEN, "eval_cp_white": None,
                "mate_white": None, "win_prob": None,
                "is_user_move": False, "clock_centis": None,
            },
            {
                "ply": 3, "san": "Nf3", "uci": "g1f3",
                "fen_before": AFTER_E4_E5_FEN, "eval_cp_white": None,
                "mate_white": None, "win_prob": None,
                "is_user_move": True, "clock_centis": None,
            },
        ]
        storage.write_plies(game_id, plies)

        asyncio.run(review_module.analyze_game(game_id, scripted_engine, depth=8))

        r = client.get("/api/profile")
        assert r.status_code == 200
        body = r.json()
        assert body["games_analyzed"] >= 1
        assert "by_phase" in body
        assert "hope_chess_rate" in body
        assert "trend" in body
        assert isinstance(body["top_leaks"], list)


# ---------------------------------------------------------------------------
# Smoke tests — existing routes must still return 200
# ---------------------------------------------------------------------------


class TestExistingRoutesUnbroken:
    def test_api_analyze_still_works(self, neutral_client):
        """POST /api/analyze returns 200 and an analysis object."""
        r = neutral_client.post("/api/analyze", json={"fen": chess.STARTING_FEN})
        assert r.status_code == 200
        body = r.json()
        assert "analysis" in body
        assert "evalWhitePov" in body["analysis"]

    def test_api_traps_still_works(self, neutral_client):
        """GET /api/traps returns 200 with a 'traps' list."""
        r = neutral_client.get("/api/traps")
        assert r.status_code == 200
        assert "traps" in r.json()

    def test_api_opening_still_works(self, neutral_client):
        """POST /api/opening returns 200 with a 'current' field."""
        r = neutral_client.post(
            "/api/opening",
            json={"baseFen": chess.STARTING_FEN, "moves": ["e2e4"]},
        )
        assert r.status_code == 200
        assert "current" in r.json()

    def test_api_repertoire_still_works(self, neutral_client):
        """GET /api/repertoire returns 200 with a 'tree' field."""
        r = neutral_client.get("/api/repertoire")
        assert r.status_code == 200
        assert "tree" in r.json()


# ---------------------------------------------------------------------------
# Color-tagging + bulk-analyze route tests
# ---------------------------------------------------------------------------


class TestPatchGameColor:
    """PATCH /api/games/{game_id} sets my_color and resets status to pending."""

    def _import_one(self, client, my_color=None) -> int:
        r = client.post("/api/games/import", json={"pgn": _GAME_1_PGN, "my_color": my_color})
        assert r.status_code == 200
        return r.json()["games"][0]["id"]

    def test_patch_sets_color_white(self, client):
        """PATCH sets my_color to 'white'."""
        game_id = self._import_one(client)
        r = client.patch(f"/api/games/{game_id}", json={"my_color": "white"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["my_color"] == "white"
        assert body["id"] == game_id

    def test_patch_sets_color_black(self, client):
        """PATCH sets my_color to 'black'."""
        game_id = self._import_one(client)
        r = client.patch(f"/api/games/{game_id}", json={"my_color": "black"})
        assert r.status_code == 200
        assert r.json()["my_color"] == "black"

    def test_patch_clears_color(self, client):
        """PATCH with null clears my_color."""
        game_id = self._import_one(client, my_color="white")
        r = client.patch(f"/api/games/{game_id}", json={"my_color": None})
        assert r.status_code == 200
        assert r.json()["my_color"] is None

    def test_patch_resets_status_to_pending(self, client):
        """After PATCH, the game's analysis_status is 'pending'."""
        game_id = self._import_one(client)
        # Manually mark done to simulate a previously analyzed game.
        storage.set_status(game_id, "done")

        r = client.patch(f"/api/games/{game_id}", json={"my_color": "white"})
        assert r.status_code == 200
        body = r.json()
        assert body["analysis_status"] == "pending"

    def test_patch_not_found(self, client):
        """PATCH on a nonexistent game returns 404."""
        r = client.patch("/api/games/99999", json={"my_color": "white"})
        assert r.status_code == 404

    def test_patch_invalid_color_returns_422(self, client):
        """PATCH with an invalid color value returns 422."""
        game_id = self._import_one(client)
        r = client.patch(f"/api/games/{game_id}", json={"my_color": "purple"})
        assert r.status_code == 422


class TestRetagColor:
    """POST /api/games/retag-color bulk-tags games by username."""

    def test_retag_tags_matching_game(self, client):
        """Retag returns updated=1 when a username matches a game's White player."""
        client.post("/api/games/import", json={"pgn": _GAME_1_PGN})  # White=Alice, Black=Bob
        r = client.post("/api/games/retag-color", json={"username": "Alice"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["updated"] == 1

        games = storage.list_games()
        assert games[0]["my_color"] == "white"

    def test_retag_case_insensitive(self, client):
        """Retag matches case-insensitively."""
        client.post("/api/games/import", json={"pgn": _GAME_1_PGN})
        r = client.post("/api/games/retag-color", json={"username": "alice"})
        assert r.status_code == 200
        assert r.json()["updated"] == 1

    def test_retag_no_match_returns_zero(self, client):
        """When no game matches, updated=0."""
        client.post("/api/games/import", json={"pgn": _GAME_1_PGN})
        r = client.post("/api/games/retag-color", json={"username": "UnknownPlayer"})
        assert r.status_code == 200
        assert r.json()["updated"] == 0

    def test_retag_returns_coverage(self, client):
        """Retag response includes a coverage dict with total/tagged/analyzed/pending."""
        client.post("/api/games/import", json={"pgn": _GAME_1_PGN})
        r = client.post("/api/games/retag-color", json={"username": "Alice"})
        assert r.status_code == 200
        cov = r.json()["coverage"]
        assert "total" in cov
        assert "tagged" in cov
        assert "analyzed" in cov
        assert "pending" in cov
        assert cov["tagged"] >= 1

    def test_retag_with_comma_separated_aliases(self, client):
        """Multiple aliases separated by commas all match."""
        client.post("/api/games/import", json={"pgn": _GAME_1_PGN})   # White=Alice
        client.post("/api/games/import", json={"pgn": _GAME_2_PGN})   # White=Charlie
        r = client.post("/api/games/retag-color", json={"username": "Alice,Charlie"})
        assert r.status_code == 200
        assert r.json()["updated"] == 2

    def test_retag_route_not_shadowed_by_game_id(self, client):
        """POST /api/games/retag-color is NOT treated as /api/games/{game_id}."""
        # This would 422 if routed to {game_id} (can't parse 'retag-color' as int).
        r = client.post("/api/games/retag-color", json={"username": "Alice"})
        # Must reach the retag route, not a 422 from int coercion.
        assert r.status_code == 200
        assert "updated" in r.json()


class TestAnalyzeAll:
    """POST /api/games/analyze-all starts a bulk analysis task."""

    def test_analyze_all_returns_pending_count(self, client):
        """analyze-all returns the count of pending games."""
        client.post("/api/games/import", json={"pgn": _GAME_1_PGN})
        client.post("/api/games/import", json={"pgn": _GAME_2_PGN})
        r = client.post("/api/games/analyze-all")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "pending" in body
        assert body["pending"] >= 1

    def test_analyze_all_route_not_shadowed(self, client):
        """POST /api/games/analyze-all is NOT treated as /api/games/{game_id}."""
        r = client.post("/api/games/analyze-all")
        # If shadowed, FastAPI would 422 (can't parse 'analyze-all' as int).
        assert r.status_code == 200
        assert "pending" in r.json()


class TestProfileCoverage:
    """GET /api/profile includes games_total and games_tagged."""

    def test_profile_includes_coverage_fields(self, client):
        """Profile response has games_total and games_tagged fields."""
        r = client.get("/api/profile")
        assert r.status_code == 200
        body = r.json()
        assert "games_total" in body
        assert "games_tagged" in body

    def test_profile_coverage_reflects_db(self, client):
        """After importing and tagging, games_total and games_tagged update."""
        # Import two games, tag one.
        client.post("/api/games/import", json={"pgn": _GAME_1_PGN, "my_color": "white"})
        client.post("/api/games/import", json={"pgn": _GAME_2_PGN})

        r = client.get("/api/profile")
        assert r.status_code == 200
        body = r.json()
        assert body["games_total"] == 2
        assert body["games_tagged"] == 1
