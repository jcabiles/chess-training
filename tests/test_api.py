"""API tests for the Stockfish Analysis Board.

Engine-dependent routes use a FAKE engine injected via FastAPI dependency
overrides, so the whole suite runs with no Stockfish binary present. Routes that
reject input before any engine call (bad FEN, illegal move) are tested directly.
"""

from __future__ import annotations

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

START_FEN = chess.STARTING_FEN


class FakeEngine:
    """Minimal stand-in for StockfishEngine.

    Returns a fixed, even evaluation regardless of position. That is enough to
    exercise the route wiring + response shapes; the *correctness* of the
    classification math is covered by tests/test_analysis.py.

    ``analyze_call_count`` is incremented on every call to ``analyze`` so tests
    can assert the engine was or was not consulted without touching any other
    part of the fake's contract.
    """

    def __init__(self, cp: int = 20):
        self._cp = cp
        self.analyze_call_count: int = 0

    @property
    def is_running(self) -> bool:
        return True

    async def restart(self) -> None:
        """No-op restart for test use."""

    async def analyze(self, fen: str, depth: int = 18) -> AnalysisResult:
        self.analyze_call_count += 1
        board = chess.Board(fen)
        score = chess_engine.PovScore(chess_engine.Cp(self._cp), chess.WHITE)
        # Render a plausible best-move SAN from the position's first legal move.
        pv = list(board.legal_moves)[:1]
        pv_san = [board.san(pv[0])] if pv else []
        return AnalysisResult(score=score, pv=pv, pv_san=pv_san, depth=depth)


@pytest.fixture
def client():
    fake = FakeEngine()
    app.dependency_overrides[get_engine] = lambda: fake
    with TestClient(app) as c:
        c.fake_engine = fake  # type: ignore[attr-defined]  # exposed for call-count assertions
        yield c
    app.dependency_overrides.clear()


# --- routes that don't need an engine --------------------------------------

def test_load_invalid_fen(client):
    r = client.post("/api/load", json={"fen": "this is not a fen"})
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False
    assert body["analysis"] is None
    assert body["error"]


def test_move_illegal(client):
    # e2e5 is not a legal opening move.
    r = client.post("/api/move", json={"fen": START_FEN, "move": "e2e5"})
    assert r.status_code == 200
    assert r.json() == {
        "legal": False, "fen": None, "lastMoveSan": None, "analysis": None,
        "book": False, "openingName": None, "openingEco": None,
    }


def test_move_bad_uci(client):
    r = client.post("/api/move", json={"fen": START_FEN, "move": "zzzz"})
    assert r.status_code == 200
    assert r.json()["legal"] is False


def test_move_bad_fen(client):
    r = client.post("/api/move", json={"fen": "garbage", "move": "e2e4"})
    assert r.status_code == 200
    assert r.json()["legal"] is False


# --- routes that use the (fake) engine -------------------------------------

def test_analyze_ok(client):
    r = client.post("/api/analyze", json={"fen": START_FEN})
    assert r.status_code == 200
    analysis = r.json()["analysis"]
    assert analysis["evalWhitePov"] == 20
    assert analysis["evalCp"] == 20
    assert analysis["mate"] is None
    assert analysis["quality"] is None  # no prior move
    assert analysis["bestMoveSan"]


def test_load_valid(client):
    r = client.post("/api/load", json={"fen": START_FEN})
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True
    assert body["fen"] == chess.Board(START_FEN).fen()
    assert body["analysis"]["quality"] is None


def test_move_legal_labels_quality(client):
    r = client.post("/api/move", json={"fen": START_FEN, "move": "e2e4"})
    assert r.status_code == 200
    body = r.json()
    assert body["legal"] is True
    assert body["lastMoveSan"] == "e4"
    # FEN advanced + black to move.
    assert body["fen"] != START_FEN
    assert " b " in body["fen"]
    # Even eval before/after via the fake → zero cpLoss → "best".
    assert body["analysis"]["quality"] in {
        "best", "good", "inaccuracy", "mistake", "blunder"
    }
    assert body["analysis"]["quality"] == "best"


def test_move_promotion_uci_accepted(client):
    # White pawn on e7, black king far away; e7e8q is the only sensible move set.
    fen = "8/4P3/8/8/8/8/k7/4K3 w - - 0 1"
    r = client.post("/api/move", json={"fen": fen, "move": "e7e8q"})
    assert r.status_code == 200
    body = r.json()
    assert body["legal"] is True
    assert body["lastMoveSan"].startswith("e8=Q")


# --- engine control routes -------------------------------------------------

def test_engine_status_ok(client):
    """GET /api/engine/status returns 200 with running:true from FakeEngine."""
    r = client.get("/api/engine/status")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is True


def test_engine_restart_ok(client):
    """POST /api/engine/restart returns 200 with restarted:true."""
    r = client.post("/api/engine/restart")
    assert r.status_code == 200
    body = r.json()
    assert body["restarted"] is True
    assert isinstance(body["running"], bool)


def test_engine_restart_running_reflects_fake(client):
    """running field after restart matches FakeEngine.is_running (True)."""
    r = client.post("/api/engine/restart")
    assert r.status_code == 200
    assert r.json()["running"] is True


def test_move_still_works_after_restart(client):
    """Existing happy-path move route is unaffected by the new engine routes."""
    # Confirm restart doesn't disturb subsequent move analysis.
    client.post("/api/engine/restart")
    r = client.post("/api/move", json={"fen": START_FEN, "move": "e2e4"})
    assert r.status_code == 200
    body = r.json()
    assert body["legal"] is True
    assert body["lastMoveSan"] == "e4"


# --- analyze=false flag ----------------------------------------------------

# Use an endgame FEN (pawn promotion position) that is guaranteed to be outside
# any opening book, so the book fast-path never fires and the new `analyze`
# flag is the sole reason the engine is skipped.
_ENDGAME_FEN = "8/4P3/8/8/8/8/k7/4K3 w - - 0 1"
_ENDGAME_MOVE = "e7e8q"  # legal pawn promotion


def test_move_analyze_false_skips_engine(client):
    """POST /api/move with analyze:false must return legal move data but null
    analysis, and must NOT call the engine at all.
    """
    calls_before = client.fake_engine.analyze_call_count
    r = client.post(
        "/api/move",
        json={"fen": _ENDGAME_FEN, "move": _ENDGAME_MOVE, "analyze": False},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["legal"] is True
    assert body["analysis"] is None
    assert body["fen"] is not None
    assert body["lastMoveSan"] is not None
    # Engine must not have been called at all during this request.
    assert client.fake_engine.analyze_call_count == calls_before


def test_move_analyze_omitted_still_analyzes(client):
    """When `analyze` is omitted the default (True) applies: analysis is present
    and quality is labelled — identical to the existing happy-path behaviour.
    """
    r = client.post(
        "/api/move",
        json={"fen": _ENDGAME_FEN, "move": _ENDGAME_MOVE},
        # `analyze` intentionally absent → server default True
    )
    assert r.status_code == 200
    body = r.json()
    assert body["legal"] is True
    assert body["fen"] is not None
    assert body["lastMoveSan"] is not None
    assert body["analysis"] is not None
    assert body["analysis"]["quality"] in {
        "best", "good", "inaccuracy", "mistake", "blunder"
    }
    # Engine must have been called (before + after = 2 calls for this request).
    assert client.fake_engine.analyze_call_count >= 2


# ---------------------------------------------------------------------------
# GET /api/games/{id}/review — summary field tests
# ---------------------------------------------------------------------------
#
# These tests exercise the optional ``summary`` object added to the review
# endpoint.  They require a live storage DB + the review pipeline, so they
# bring in a fresh-storage fixture and use ScriptedEngine to run the analysis
# pipeline synchronously (no background task).  The pattern mirrors
# tests/test_games_api.py::TestReview exactly.
# ---------------------------------------------------------------------------

_REVIEW_START_FEN = chess.STARTING_FEN
# After 1.e4
_REVIEW_AFTER_E4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
# After 1.e4 e5
_REVIEW_AFTER_E4_E5 = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
# After 1.e4 e5 2.Nf3
_REVIEW_AFTER_NF3 = "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"


def _review_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def review_storage(tmp_path: Path, monkeypatch):
    """Isolated temp DB for review-summary tests; resets review module state."""
    db_path = str(tmp_path / "review_summary_test.db")
    monkeypatch.setenv("GAMES_DB", db_path)
    storage.init(db_path)
    review_module._interactive_pending = 0
    yield
    for gid in list(review_module._tasks.keys()):
        review_module.cancel_analysis(gid)
    review_module._interactive_pending = 0


@pytest.fixture()
def review_client(review_storage):
    """TestClient with a neutral ScriptedEngine; storage pointed at temp DB."""
    engine = ScriptedEngine()  # Cp(0) everywhere — no eval swings needed for structure tests
    app.dependency_overrides[get_engine] = lambda: engine
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _insert_done_game_with_evals(my_color: Optional[str] = "white") -> int:
    """Insert a 4-ply game with non-null eval_cp_white + fen_before, set status done.

    Returns the new game_id.  The four plies span three mover-pairs so that
    accuracy.summarize() can score at least one move per side.
    """
    game_id = storage.insert_game({
        "content_hash": f"summary-test-{_review_utc_now()}",
        "pgn": '[Event "Test"]\n[White "Alice"]\n[Black "Bob"]\n[Result "*"]\n\n1. e4 e5 2. Nf3 *\n',
        "imported_at": _review_utc_now(),
        "white": "Alice",
        "black": "Bob",
        "my_color": my_color,
        "ply_count": 4,
    })
    # Four plies: eval_cp_white and fen_before are both non-null so that
    # accuracy.summarize() can compute per-side win-% drops.
    plies = [
        {
            "ply": 1,
            "san": "e4",
            "uci": "e2e4",
            "fen_before": _REVIEW_START_FEN,
            "eval_cp_white": 20,     # slight white edge before e4
            "mate_white": None,
            "win_prob": None,
            "is_user_move": True,
            "clock_centis": None,
        },
        {
            "ply": 2,
            "san": "e5",
            "uci": "e7e5",
            "fen_before": _REVIEW_AFTER_E4,
            "eval_cp_white": -10,    # black equalises
            "mate_white": None,
            "win_prob": None,
            "is_user_move": False,
            "clock_centis": None,
        },
        {
            "ply": 3,
            "san": "Nf3",
            "uci": "g1f3",
            "fen_before": _REVIEW_AFTER_E4_E5,
            "eval_cp_white": 30,     # white edges ahead
            "mate_white": None,
            "win_prob": None,
            "is_user_move": True,
            "clock_centis": None,
        },
        {
            "ply": 4,
            "san": "Nc6",
            "uci": "b8c6",
            "fen_before": _REVIEW_AFTER_NF3,
            "eval_cp_white": 10,     # black develops
            "mate_white": None,
            "win_prob": None,
            "is_user_move": False,
            "clock_centis": None,
        },
    ]
    storage.write_plies(game_id, plies)
    storage.set_status(game_id, "done")
    return game_id


def _insert_pending_game() -> int:
    """Insert a minimal pending game (no evals, analysis_status='pending')."""
    game_id = storage.insert_game({
        "content_hash": f"pending-test-{_review_utc_now()}",
        "pgn": '[Event "Test"]\n[White "X"]\n[Black "Y"]\n[Result "*"]\n\n1. d4 *\n',
        "imported_at": _review_utc_now(),
        "white": "X",
        "black": "Y",
        "my_color": None,
        "ply_count": 1,
    })
    storage.write_plies(game_id, [
        {
            "ply": 1,
            "san": "d4",
            "uci": "d2d4",
            "fen_before": _REVIEW_START_FEN,
            "eval_cp_white": None,
            "mate_white": None,
            "win_prob": None,
            "is_user_move": False,
            "clock_centis": None,
        }
    ])
    # analysis_status stays 'pending' (the default)
    return game_id


def test_review_summary_present_on_done_game(review_client, review_storage):
    """GET /api/games/{id}/review returns a non-null summary for a done game.

    The summary must contain all seven documented keys.  Accuracy values are
    float-or-null; move counts are ints.  Exact numeric values are NOT asserted
    here — those are the domain of the unit tests in test_analysis.py.
    """
    game_id = _insert_done_game_with_evals(my_color="white")

    r = review_client.get(f"/api/games/{game_id}/review")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["analysis_status"] == "done"
    summary = body["summary"]
    assert summary is not None, "Expected a non-null summary for a done game"

    # All seven keys must be present.
    expected_keys = {
        "white_accuracy", "black_accuracy",
        "white_elo", "black_elo",
        "white_moves", "black_moves",
        "my_color",
    }
    assert expected_keys == set(summary.keys()), (
        f"summary keys mismatch: got {set(summary.keys())}"
    )

    # Move counts must be ints.
    assert isinstance(summary["white_moves"], int)
    assert isinstance(summary["black_moves"], int)

    # Accuracy and Elo are float/int or null — never a wrong type.
    for acc_key in ("white_accuracy", "black_accuracy"):
        val = summary[acc_key]
        assert val is None or isinstance(val, (int, float)), (
            f"{acc_key} must be float or null, got {type(val)}"
        )
    for elo_key in ("white_elo", "black_elo"):
        val = summary[elo_key]
        assert val is None or isinstance(val, int), (
            f"{elo_key} must be int or null, got {type(val)}"
        )

    # my_color is passed through from the game row.
    assert summary["my_color"] == "white"

    # With 4 plies and all fen_before set, at least one side must have scored moves.
    assert summary["white_moves"] + summary["black_moves"] >= 1


def test_review_summary_null_when_not_done(review_client, review_storage):
    """GET /api/games/{id}/review returns summary=null for a pending game.

    The route must return 200 for a pending game (not 404/422); only the
    summary field changes based on analysis_status.
    """
    game_id = _insert_pending_game()

    r = review_client.get(f"/api/games/{game_id}/review")
    assert r.status_code == 200, (
        f"Expected 200 for a pending game, got {r.status_code}: {r.text}"
    )
    body = r.json()

    assert body["analysis_status"] == "pending"
    assert body["summary"] is None, (
        f"Expected summary=null for a pending game, got: {body['summary']}"
    )


def test_review_existing_fields_unchanged(review_client, review_storage):
    """GET /api/games/{id}/review still returns all pre-existing top-level keys.

    Guards against the summary addition accidentally dropping game_id,
    analysis_status, leaks, or plies from the response shape.
    """
    game_id = _insert_done_game_with_evals(my_color="white")

    r = review_client.get(f"/api/games/{game_id}/review")
    assert r.status_code == 200, r.text
    body = r.json()

    # Pre-existing required keys — must all still be present.
    assert "game_id" in body, "Missing field: game_id"
    assert "analysis_status" in body, "Missing field: analysis_status"
    assert "leaks" in body, "Missing field: leaks"
    assert "plies" in body, "Missing field: plies"

    # Types must be sensible.
    assert body["game_id"] == game_id
    assert isinstance(body["leaks"], list)
    assert isinstance(body["plies"], list)

    # Plies must reflect the 4 rows we wrote.
    assert len(body["plies"]) == 4
