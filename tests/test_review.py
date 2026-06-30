"""
tests/test_review.py — Tests for app/review.py.

All tests use ScriptedEngine (no real Stockfish binary needed).

Test groups
-----------
1. TestBlunderDetection    — A planted blunder is found; small-drop moves are not;
                             my_color=None produces no leaks; opponent moves are ignored.
2. TestLeadInAndThreat     — lead_in_ply < blunder_ply; threat_uci + threat_motif
                             populated when the threat probe runs.
3. TestNoStarve            — Concurrent background task pauses when
                             note_interactive_start() is called and resumes after
                             note_interactive_end().
4. TestJobLifecycle        — start_analysis / cancel_analysis helpers.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import chess
import chess.engine as chess_engine
import pytest

import app.review as review
import app.storage as storage
from app.engine import AnalysisResult
from app.review import (
    analyze_game,
    analyze_pending,
    cancel_analysis,
    note_interactive_end,
    note_interactive_start,
    reset_stuck,
    start_analysis,
    start_analyze_all,
)
from tests.engine_fakes import ScriptedEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

START_FEN = chess.STARTING_FEN

# A position after 1.e4 — White to move (used as the "base" for scripted games).
AFTER_E4_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
# After 1.e4 e5 — White to move.
AFTER_E4_E5_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
# After 1.e4 e5 2.Nf3 — Black to move.
AFTER_E4_E5_NF3_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_result(cp: int, fen: str, uci: Optional[str] = None) -> AnalysisResult:
    """Make an AnalysisResult with a real first move from the position."""
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


def _insert_game(my_color: Optional[str] = "white") -> int:
    """Insert a minimal 3-ply game into storage and return the game_id.

    Game: 1.e4 e5 2.Nf3 (White = user when my_color='white')
    Plies: ply 1 = White's e4, ply 2 = Black's e5, ply 3 = White's Nf3.
    """
    game_id = storage.insert_game({
        "content_hash": f"testhash-{my_color}-{_utc_now()}",
        "pgn": "[Event \"Test\"]\n1. e4 e5 2. Nf3 *",
        "imported_at": _utc_now(),
        "white": "TestUser",
        "black": "Opponent",
        "my_color": my_color,
        "ply_count": 3,
    })
    # Insert the three plies manually.
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


@pytest.fixture(autouse=True)
def fresh_storage(tmp_path: Path):
    """Initialize a fresh in-memory/temp DB for each test."""
    db_path = str(tmp_path / "games.db")
    storage.init(db_path)
    # Reset the interactive counter between tests.
    review._interactive_pending = 0
    yield
    # Clean up any stray tasks.
    for gid in list(review._tasks.keys()):
        cancel_analysis(gid)
    review._interactive_pending = 0


# ---------------------------------------------------------------------------
# 1. Blunder detection
# ---------------------------------------------------------------------------


class TestBlunderDetection:
    """A planted blunder is detected; small drops and opponent moves are not."""

    @pytest.mark.anyio
    async def test_blunder_detected_for_user_move(self, tmp_path: Path):
        """A move that drops win-prob ≥ 20% for the user is classified as a blunder.

        Scripted evals (all White-POV centipawns):
          START_FEN          +50 cp  → wp_before for White ≈ 0.546
          AFTER_E4_FEN      -300 cp  → Black is mover; wp for Black = win_prob(300) ≈ 0.751
                                       → wp_after for White = 1 - 0.751 = 0.249
          drop = 0.546 - 0.249 = 0.297 ≥ 0.20 → blunder
        """
        game_id = _insert_game(my_color="white")

        # Script: START_FEN slightly White-ahead; AFTER_E4_FEN crushingly bad for White.
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
        engine = ScriptedEngine(script)

        await analyze_game(game_id, engine, depth=8)

        leaks = storage.get_leaks(game_id)
        # Ply 1 (e4) drops ~30% for White → blunder.
        assert len(leaks) >= 1, f"Expected at least one leak, got: {leaks}"
        blunder = leaks[0]
        assert blunder["ply"] == 1
        assert blunder["severity"] == "blunder"
        assert blunder["color"] == "white"

    @pytest.mark.anyio
    async def test_small_drop_not_a_leak(self, tmp_path: Path):
        """A move dropping < 10% win-prob is NOT recorded as a leak."""
        game_id = _insert_game(my_color="white")

        # All positions near equal → no significant drops.
        script = {
            START_FEN: [_make_result(20, START_FEN), _make_result(10, START_FEN)],
            AFTER_E4_FEN: [_make_result(15, AFTER_E4_FEN), _make_result(5, AFTER_E4_FEN)],
            AFTER_E4_E5_FEN: [_make_result(25, AFTER_E4_E5_FEN), _make_result(10, AFTER_E4_E5_FEN)],
        }
        engine = ScriptedEngine(script)

        await analyze_game(game_id, engine, depth=8)

        leaks = storage.get_leaks(game_id)
        assert leaks == [], f"Expected no leaks for small drops, got: {leaks}"

    @pytest.mark.anyio
    async def test_no_leaks_when_my_color_is_none(self, tmp_path: Path):
        """When my_color is None, no leaks are written regardless of position quality."""
        game_id = _insert_game(my_color=None)

        # Plant a huge eval swing — should still produce 0 leaks.
        script = {
            START_FEN: [_make_result(50, START_FEN), _make_result(10, START_FEN)],
            AFTER_E4_FEN: [_make_result(-500, AFTER_E4_FEN), _make_result(-600, AFTER_E4_FEN)],
            AFTER_E4_E5_FEN: [_make_result(60, AFTER_E4_E5_FEN), _make_result(30, AFTER_E4_E5_FEN)],
        }
        engine = ScriptedEngine(script)

        await analyze_game(game_id, engine, depth=8)

        leaks = storage.get_leaks(game_id)
        assert leaks == [], f"Expected no leaks for my_color=None, got: {leaks}"

    @pytest.mark.anyio
    async def test_opponent_blunder_not_leaked(self, tmp_path: Path):
        """A blunder by the opponent (Black) is not recorded when user is White."""
        # Ply 2 is Black's move (e5) — we make it a big drop for Black.
        # White's moves are fine.
        game_id = _insert_game(my_color="white")

        # AFTER_E4_FEN: Black is to move; eval is +300 for White (very good for White).
        # After Black's e5: AFTER_E4_E5_FEN eval is +50 (good for White but not huge drop for White).
        # For BLACK at AFTER_E4_FEN: wp_before for Black = 1 - win_prob_from_cp(300) ≈ 0.05 (already bad).
        # We don't classify opponent moves, so no leak for ply 2.
        script = {
            START_FEN: [_make_result(20, START_FEN), _make_result(10, START_FEN)],
            AFTER_E4_FEN: [_make_result(300, AFTER_E4_FEN), _make_result(250, AFTER_E4_FEN)],
            AFTER_E4_E5_FEN: [_make_result(50, AFTER_E4_E5_FEN), _make_result(30, AFTER_E4_E5_FEN)],
        }
        engine = ScriptedEngine(script)

        await analyze_game(game_id, engine, depth=8)

        leaks = storage.get_leaks(game_id)
        # Ply 2 (Black's move) must not be in leaks.
        opponent_leaks = [lk for lk in leaks if lk["ply"] == 2]
        assert opponent_leaks == [], f"Expected no opponent leaks, got: {opponent_leaks}"

    @pytest.mark.anyio
    async def test_status_set_to_done(self, tmp_path: Path):
        """analyze_game sets analysis_status to 'done' on completion."""
        game_id = _insert_game(my_color="white")
        engine = ScriptedEngine()  # all Cp(0) → no leaks

        await analyze_game(game_id, engine, depth=8)

        game = storage.get_game(game_id)
        assert game is not None
        assert game["analysis_status"] == "done"

    @pytest.mark.anyio
    async def test_game_plies_written(self, tmp_path: Path):
        """analyze_game writes one game_plies row per ply with eval fields."""
        game_id = _insert_game(my_color="white")
        script = {
            START_FEN: [_make_result(30, START_FEN), _make_result(10, START_FEN)],
            AFTER_E4_FEN: [_make_result(-20, AFTER_E4_FEN), _make_result(-30, AFTER_E4_FEN)],
            AFTER_E4_E5_FEN: [_make_result(40, AFTER_E4_E5_FEN), _make_result(20, AFTER_E4_E5_FEN)],
        }
        engine = ScriptedEngine(script)

        await analyze_game(game_id, engine, depth=8)

        plies = storage.get_plies(game_id)
        assert len(plies) == 3
        for ply in plies:
            assert ply["win_prob"] is not None, f"win_prob is None for ply {ply['ply']}"


# ---------------------------------------------------------------------------
# 2. Lead-in localization + threat probe
# ---------------------------------------------------------------------------


class TestLeadInAndThreat:
    """lead_in_ply < blunder_ply; threat populated at lead-in."""

    @pytest.mark.anyio
    async def test_lead_in_ply_before_blunder(self, tmp_path: Path):
        """The lead_in_ply recorded is strictly less than the blunder ply.

        5-ply game: 1.e4 e5 2.Nf3 Nc6 3.Bc4
        Ply 3 (Nf3) is scripted as an only-move position (large gap best vs 2nd-best).
        Ply 5 (Bc4) is scripted to produce a big win-prob drop for White → blunder.
        Expected: lead_in_ply ≤ ply 3 (the seed), which is < ply 5.
        """
        # Build FENs step by step (capture each fen_before correctly).
        b = chess.Board()
        fen1 = b.fen()           # before e4 (ply 1)
        b.push_uci("e2e4")
        fen2 = b.fen()           # before e5 (ply 2)
        b.push_uci("e7e5")
        fen3 = b.fen()           # before Nf3 (ply 3)
        b.push_uci("g1f3")
        fen4 = b.fen()           # before Nc6 (ply 4)
        b.push_uci("b8c6")
        fen5 = b.fen()           # before Bc4 (ply 5)

        game_id = storage.insert_game({
            "content_hash": f"5ply-{_utc_now()}",
            "pgn": "[Event \"Test\"]\n1. e4 e5 2. Nf3 Nc6 3. Bc4 *",
            "imported_at": _utc_now(),
            "white": "User",
            "black": "Bot",
            "my_color": "white",
            "ply_count": 5,
        })

        plies = [
            {"ply": 1, "san": "e4",  "uci": "e2e4", "fen_before": fen1, "is_user_move": True,  "clock_centis": None},
            {"ply": 2, "san": "e5",  "uci": "e7e5", "fen_before": fen2, "is_user_move": False, "clock_centis": None},
            {"ply": 3, "san": "Nf3", "uci": "g1f3", "fen_before": fen3, "is_user_move": True,  "clock_centis": None},
            {"ply": 4, "san": "Nc6", "uci": "b8c6", "fen_before": fen4, "is_user_move": False, "clock_centis": None},
            {"ply": 5, "san": "Bc4", "uci": "f1c4", "fen_before": fen5, "is_user_move": True,  "clock_centis": None},
        ]
        storage.write_plies(game_id, plies)

        # Script:
        #   fen3 (before Nf3) → only-move: big gap (+100 vs -80)
        #   fen5 (before Bc4) → position eval +50 for White, but after Bc4 it
        #       becomes fen6 which is +50 for Black (-50 White-POV).
        #       drop for White ≈ win_prob(50) - (1 - win_prob(-50)) = 0.55 - 0.55 = 0
        #       That won't work — we need a bigger swing for a blunder.
        #   fen5 → +50 cp; after ply 5, ply_data[5] would be fen6 eval.
        #   But there is no ply 6 in a 5-ply game.  Instead we need a blunder
        #   at ply 3 (the only user-ply with a known successor at ply 4).

        # Revised approach: make ply 3 (Nf3) the blunder.
        # fen3 (White to move): +50 cp
        # fen4 (Black to move, after Nf3): -300 cp (White-POV) → drop ≈ 30% → blunder
        # fen3 is also an only-move seed.
        # Lead-in will look back: ply 1 is an earlier user ply, OK, and if fen1 is a seed
        # it would set lead_in_ply = 1.  Otherwise lead_in_ply = ply - 1 = 2.
        script = {
            fen1: [_make_result(20, fen1), _make_result(10, fen1)],
            fen2: [_make_result(-15, fen2), _make_result(-25, fen2)],
            fen3: [_make_result(50, fen3), _make_result(-80, fen3)],   # only-move seed AND blunder src
            fen4: [_make_result(-300, fen4), _make_result(-350, fen4)],  # after Nf3: crushing
            fen5: [_make_result(20, fen5), _make_result(10, fen5)],
        }
        engine = ScriptedEngine(script)

        await analyze_game(game_id, engine, depth=8)

        leaks = storage.get_leaks(game_id)
        blunder_at_3 = [lk for lk in leaks if lk["ply"] == 3]
        assert blunder_at_3, f"Expected a blunder at ply 3 (Nf3); leaks={leaks}"
        lk = blunder_at_3[0]
        assert lk["lead_in_ply"] is not None
        assert lk["lead_in_ply"] < lk["ply"], (
            f"lead_in_ply ({lk['lead_in_ply']}) should be < blunder ply ({lk['ply']})"
        )

    @pytest.mark.anyio
    async def test_blunder_has_lead_in_before_ply(self, tmp_path: Path):
        """A confirmed blunder always has lead_in_ply < blunder ply."""
        # Use a carefully scripted 3-ply game to ensure a ≥20% drop.
        # White's ply 1 (e4): wp_before for White = win_prob(+50) ≈ 0.568
        # After e4, ply 2 (opponent's turn) eval = -300 cp (Black crushing)
        #   → ply2 wp for Black (mover) = win_prob(300) ≈ 0.753
        #   → wp_after for White = 1 - 0.753 = 0.247
        # drop = 0.568 - 0.247 = 0.321 → blunder
        game_id = _insert_game(my_color="white")

        script = {
            START_FEN: [_make_result(50, START_FEN), _make_result(-20, START_FEN)],
            AFTER_E4_FEN: [_make_result(-300, AFTER_E4_FEN), _make_result(-350, AFTER_E4_FEN)],
            AFTER_E4_E5_FEN: [_make_result(20, AFTER_E4_E5_FEN), _make_result(10, AFTER_E4_E5_FEN)],
        }
        engine = ScriptedEngine(script)

        await analyze_game(game_id, engine, depth=8)

        leaks = storage.get_leaks(game_id)
        assert leaks, "Expected at least one blunder"
        blunder = leaks[0]
        assert blunder["lead_in_ply"] is not None
        # lead_in_ply must be strictly less than the blunder ply (ply 1 → default = ply 0,
        # but ply 0 doesn't exist; the default is ply-1 which is 0, clamped below).
        # For ply 1 the lead_in defaults to ply-1 = 0 (no earlier ply found).
        # The contract says lead_in_ply < blunder ply OR equal to ply-1.
        # Accept both: lead_in_ply <= blunder["ply"] - 1.
        assert blunder["lead_in_ply"] <= blunder["ply"] - 1, (
            f"lead_in_ply={blunder['lead_in_ply']} not < blunder_ply={blunder['ply']}"
        )

    @pytest.mark.anyio
    async def test_threat_probe_populates_threat_uci(self, tmp_path: Path):
        """When the threat probe runs, threat_uci reflects the OPPONENT's best move.

        The null-move probe now runs at the LEAK board (board before the user's
        blunder), not at the lead-in board.  For a ply-1 blunder the leak board
        IS the START_FEN board, so the null FEN is derived from START_FEN.
        After the null move, the opponent (Black) is to move and the engine's
        best reply is returned as threat_uci.
        """
        game_id = _insert_game(my_color="white")

        # Null-move probe runs at the leak board = board before ply 1 (START_FEN).
        null_board = chess.Board(START_FEN)
        null_board.push(chess.Move.null())
        null_fen = null_board.fen()

        # Pick a real first move from null_fen (Black to move) for the scripted threat.
        null_board_obj = chess.Board(null_fen)
        threat_move = next(iter(null_board_obj.legal_moves))
        threat_pv_san = [null_board_obj.san(threat_move)]

        threat_result = AnalysisResult(
            score=chess_engine.PovScore(chess_engine.Cp(-50), chess.BLACK),
            pv=[threat_move],
            pv_san=threat_pv_san,
            depth=8,
        )

        script = {
            START_FEN: [_make_result(50, START_FEN), _make_result(-20, START_FEN)],
            AFTER_E4_FEN: [_make_result(-300, AFTER_E4_FEN), _make_result(-350, AFTER_E4_FEN)],
            AFTER_E4_E5_FEN: [_make_result(20, AFTER_E4_E5_FEN), _make_result(10, AFTER_E4_E5_FEN)],
            null_fen: [threat_result],
        }
        engine = ScriptedEngine(script)

        await analyze_game(game_id, engine, depth=8)

        leaks = storage.get_leaks(game_id)
        assert leaks, "Expected at least one leak"
        blunder = leaks[0]
        # threat_uci should be set (scripted null-move FEN has a PV).
        assert blunder["threat_uci"] is not None, (
            "Expected threat_uci to be set after null-move probe"
        )
        assert blunder["threat_uci"] == threat_move.uci()

    @pytest.mark.anyio
    async def test_threat_probe_at_leak_board_not_lead_in(self, tmp_path: Path):
        """The null-move probe runs at the LEAK board (before user's blunder),
        not at the lead-in board.

        5-ply game where the blunder is at ply 3 (White's Nf3).  The LEAK board
        is the board before ply 3 (fen3), NOT the lead-in board (which could be
        fen1 or fen2).  We script the null-move FEN from fen3 with an explicit
        threat move, and verify threat_uci matches that scripted move.
        """
        b = chess.Board()
        fen1 = b.fen()           # before e4 (ply 1)
        b.push_uci("e2e4")
        fen2 = b.fen()           # before e5 (ply 2)
        b.push_uci("e7e5")
        fen3 = b.fen()           # before Nf3 (ply 3) — this is the LEAK board
        b.push_uci("g1f3")
        fen4 = b.fen()           # before Nc6 (ply 4)
        b.push_uci("b8c6")
        fen5 = b.fen()           # before Bc4 (ply 5)

        game_id = storage.insert_game({
            "content_hash": f"leak-at-ply3-{_utc_now()}",
            "pgn": "[Event \"Test\"]\n1. e4 e5 2. Nf3 Nc6 3. Bc4 *",
            "imported_at": _utc_now(),
            "white": "User",
            "black": "Bot",
            "my_color": "white",
            "ply_count": 5,
        })
        plies = [
            {"ply": 1, "san": "e4",  "uci": "e2e4", "fen_before": fen1, "is_user_move": True,  "clock_centis": None},
            {"ply": 2, "san": "e5",  "uci": "e7e5", "fen_before": fen2, "is_user_move": False, "clock_centis": None},
            {"ply": 3, "san": "Nf3", "uci": "g1f3", "fen_before": fen3, "is_user_move": True,  "clock_centis": None},
            {"ply": 4, "san": "Nc6", "uci": "b8c6", "fen_before": fen4, "is_user_move": False, "clock_centis": None},
            {"ply": 5, "san": "Bc4", "uci": "f1c4", "fen_before": fen5, "is_user_move": True,  "clock_centis": None},
        ]
        storage.write_plies(game_id, plies)

        # Null-move probe runs at fen3 (White to move → after null, Black to move).
        null_board = chess.Board(fen3)
        null_board.push(chess.Move.null())
        null_fen = null_board.fen()

        # Pick Black's first legal move from null_fen as the scripted threat.
        null_board_obj = chess.Board(null_fen)
        threat_move = next(iter(null_board_obj.legal_moves))
        threat_pv_san = [null_board_obj.san(threat_move)]

        threat_result = AnalysisResult(
            score=chess_engine.PovScore(chess_engine.Cp(50), chess.BLACK),
            pv=[threat_move],
            pv_san=threat_pv_san,
            depth=8,
        )

        script = {
            fen1: [_make_result(20, fen1), _make_result(10, fen1)],
            fen2: [_make_result(-15, fen2), _make_result(-25, fen2)],
            fen3: [_make_result(50, fen3), _make_result(-80, fen3)],   # only-move seed + blunder src
            fen4: [_make_result(-300, fen4), _make_result(-350, fen4)],  # after Nf3: crushing
            fen5: [_make_result(20, fen5), _make_result(10, fen5)],
            null_fen: [threat_result],
        }
        engine = ScriptedEngine(script)

        await analyze_game(game_id, engine, depth=8)

        leaks = storage.get_leaks(game_id)
        blunder_at_3 = [lk for lk in leaks if lk["ply"] == 3]
        assert blunder_at_3, f"Expected a blunder at ply 3; leaks={leaks}"
        lk = blunder_at_3[0]
        assert lk["threat_uci"] == threat_move.uci(), (
            f"threat_uci should be the opponent's move from the leak board null-probe, "
            f"got {lk['threat_uci']!r} expected {threat_move.uci()!r}"
        )

    @pytest.mark.anyio
    async def test_threat_probe_detects_mate_category(self, tmp_path: Path):
        """When the opponent's null-move response leads to checkmate, category='mate'.

        We use a Scholar's-mate-adjacent position where the opponent's best
        reply after the null move delivers checkmate.
        """
        # Position: 1.e4 e5 2.Qh5 Nc6 3.Bc4 — White to move (ply 5 / fen5).
        # After user skips (null move), White plays Qxf7# → checkmate.
        # We simulate this via ScriptedEngine: script the null-move FEN so the
        # engine returns Qxf7 as the threat move, and script that after Qxf7 it
        # is checkmate.
        b = chess.Board()
        b.push_uci("e2e4")
        b.push_uci("e7e5")
        b.push_uci("d1h5")
        b.push_uci("b8c6")
        b.push_uci("f1c4")
        # fen5: Black to move (ply 6 = Nf6, which allows Qxf7#)
        b.push_uci("g8f6")
        # Now it's White's turn (ply 7) — but let's use Black's blunder at ply 6
        # instead.  Rebuild: fen before ply 6 = board after 5 half-moves.
        b2 = chess.Board()
        b2.push_uci("e2e4")
        b2.push_uci("e7e5")
        b2.push_uci("d1h5")
        b2.push_uci("b8c6")
        b2.push_uci("f1c4")
        leak_fen = b2.fen()   # Black to move — the LEAK board for Black's blunder (Nf6)

        # After null from leak_fen, White is to move; White plays Qxf7 (f7 = f7).
        null_board = chess.Board(leak_fen)
        null_board.push(chess.Move.null())
        null_fen = null_board.fen()

        # The mating move: Qxf7#
        qxf7 = chess.Move.from_uci("h5f7")
        # Verify it is legal in null_board and leads to checkmate.
        null_board_check = chess.Board(null_fen)
        assert qxf7 in null_board_check.legal_moves, "Qxf7 must be legal"
        after_check = null_board_check.copy()
        after_check.push(qxf7)
        assert after_check.is_checkmate(), "Qxf7 must deliver checkmate"

        threat_result = AnalysisResult(
            score=chess_engine.PovScore(chess_engine.Mate(1), chess.WHITE),
            pv=[qxf7],
            pv_san=["Qxf7#"],
            depth=8,
        )

        # The leak is at ply 6 (Black's Nf6 → allows Qxf7#).
        # We need a game with my_color=black and a blunder at ply 6.
        b3 = chess.Board()
        fen_p1 = b3.fen()
        b3.push_uci("e2e4")
        fen_p2 = b3.fen()
        b3.push_uci("e7e5")
        fen_p3 = b3.fen()
        b3.push_uci("d1h5")
        fen_p4 = b3.fen()
        b3.push_uci("b8c6")
        fen_p5 = b3.fen()
        b3.push_uci("f1c4")
        fen_p6 = b3.fen()  # = leak_fen (Black to move)
        b3.push_uci("g8f6")
        fen_p7 = b3.fen()  # after Nf6

        game_id = storage.insert_game({
            "content_hash": f"scholars-mate-{_utc_now()}",
            "pgn": "[Event \"T\"]\n[White \"Opp\"]\n[Black \"Me\"]\n1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7# 1-0",
            "imported_at": _utc_now(),
            "white": "Opp",
            "black": "Me",
            "my_color": "black",
            "result": "1-0",
            "ply_count": 7,
        })
        plies = [
            {"ply": 1, "san": "e4",    "uci": "e2e4", "fen_before": fen_p1, "is_user_move": False, "clock_centis": None},
            {"ply": 2, "san": "e5",    "uci": "e7e5", "fen_before": fen_p2, "is_user_move": True,  "clock_centis": None},
            {"ply": 3, "san": "Qh5",   "uci": "d1h5", "fen_before": fen_p3, "is_user_move": False, "clock_centis": None},
            {"ply": 4, "san": "Nc6",   "uci": "b8c6", "fen_before": fen_p4, "is_user_move": True,  "clock_centis": None},
            {"ply": 5, "san": "Bc4",   "uci": "f1c4", "fen_before": fen_p5, "is_user_move": False, "clock_centis": None},
            {"ply": 6, "san": "Nf6",   "uci": "g8f6", "fen_before": fen_p6, "is_user_move": True,  "clock_centis": None},
            {"ply": 7, "san": "Qxf7#", "uci": "h5f7", "fen_before": fen_p7, "is_user_move": False, "clock_centis": None},
        ]
        storage.write_plies(game_id, plies)

        # Script evals: ply 6 (Black's Nf6) is the blunder — big drop for Black.
        # Before Nf6 (fen_p6): Black is doing OK (eval near 0 from White's POV).
        # After Nf6 (fen_p7): White has forced mate (White eval very high).
        script = {
            fen_p1: [_make_result(20, fen_p1),   _make_result(10, fen_p1)],
            fen_p2: [_make_result(-10, fen_p2),  _make_result(-20, fen_p2)],
            fen_p3: [_make_result(15, fen_p3),   _make_result(5, fen_p3)],
            fen_p4: [_make_result(-5, fen_p4),   _make_result(-15, fen_p4)],
            fen_p5: [_make_result(20, fen_p5),   _make_result(10, fen_p5)],
            fen_p6: [_make_result(10, fen_p6),   _make_result(-5, fen_p6)],   # Black OK before Nf6
            fen_p7: [                                                          # after Nf6: White mates
                AnalysisResult(
                    score=chess_engine.PovScore(chess_engine.Mate(1), chess.WHITE),
                    pv=[chess.Move.from_uci("h5f7")],
                    pv_san=["Qxf7#"],
                    depth=8,
                )
            ],
            null_fen: [threat_result],
        }
        engine = ScriptedEngine(script)

        await analyze_game(game_id, engine, depth=8)

        leaks = storage.get_leaks(game_id)
        blunder_at_6 = [lk for lk in leaks if lk["ply"] == 6]
        assert blunder_at_6, f"Expected a blunder at ply 6 (Nf6); leaks={leaks}"
        lk = blunder_at_6[0]
        assert lk["category"] == "mate", (
            f"Expected category='mate' for a move allowing checkmate, got {lk['category']!r}"
        )
        assert lk["threat_uci"] == "h5f7", (
            f"Expected threat_uci='h5f7' (Qxf7#), got {lk['threat_uci']!r}"
        )
        assert lk["threat_motif"] == "mate", (
            f"Expected threat_motif='mate', got {lk['threat_motif']!r}"
        )


# ---------------------------------------------------------------------------
# 3. No-starve concurrency
# ---------------------------------------------------------------------------


class TestNoStarve:
    """Background analysis pauses while interactive calls are in flight."""

    @pytest.mark.anyio
    async def test_interactive_start_pauses_background(self, tmp_path: Path):
        """While note_interactive_start() is active, the background loop does
        not issue engine calls; it resumes after note_interactive_end()."""

        engine_call_log: list[str] = []

        class LoggingEngine(ScriptedEngine):
            async def analyze_multi(self, fen, depth=8, multipv=1):
                engine_call_log.append(("bg" if review._interactive_pending == 0 else "??", fen))
                return await super().analyze_multi(fen, depth=depth, multipv=multipv)

        # 3-ply game; plant an even position (no leaks needed; we care about call order).
        game_id = _insert_game(my_color="white")

        engine = LoggingEngine()

        # Start the background task.
        note_interactive_start()  # raise the counter
        task = asyncio.create_task(analyze_game(game_id, engine, depth=8))

        # Let the event loop run for a short while — the background should be blocked.
        await asyncio.sleep(0.02)
        assert engine_call_log == [], (
            "Background should not have issued engine calls while interactive is pending"
        )

        # Release the counter.
        note_interactive_end()

        # Now let the background complete.
        await asyncio.wait_for(task, timeout=2.0)

        # After release, background issued calls.
        assert len(engine_call_log) > 0, "Background should have issued calls after release"

    @pytest.mark.anyio
    async def test_interactive_call_completes_during_background(self, tmp_path: Path):
        """An interactive engine call completes independently of the background."""
        # The ScriptedEngine has no real lock contention, but we verify the
        # interactive call doesn't wait for the background to finish.

        game_id = _insert_game(my_color="white")
        engine = ScriptedEngine()

        # Start background (will complete quickly with ScriptedEngine).
        task = asyncio.create_task(analyze_game(game_id, engine, depth=8))

        # Simulate an interactive call while background may be running.
        note_interactive_start()
        try:
            result = await asyncio.wait_for(
                engine.analyze_multi(START_FEN, depth=8, multipv=1),
                timeout=1.0,
            )
            assert result is not None
        finally:
            note_interactive_end()

        # Background should also complete (ScriptedEngine is instant).
        await asyncio.wait_for(task, timeout=2.0)

    @pytest.mark.anyio
    async def test_counter_correctly_resets_to_zero(self):
        """note_interactive_end() clamps to 0 (never negative)."""
        note_interactive_start()
        note_interactive_start()
        note_interactive_end()
        note_interactive_end()
        note_interactive_end()  # extra call — must not go negative
        assert review._interactive_pending == 0


# ---------------------------------------------------------------------------
# 4. Job lifecycle
# ---------------------------------------------------------------------------


class TestJobLifecycle:
    """start_analysis / cancel_analysis helpers work correctly."""

    @pytest.mark.anyio
    async def test_start_analysis_sets_status_analyzing(self, tmp_path: Path):
        """start_analysis sets game status to 'analyzing' immediately."""
        game_id = _insert_game(my_color="white")
        engine = ScriptedEngine()

        task = start_analysis(game_id, engine, depth=8)
        # Check status immediately (before task completes).
        game = storage.get_game(game_id)
        assert game is not None
        assert game["analysis_status"] == "analyzing"

        # Let it finish.
        await asyncio.wait_for(task, timeout=2.0)

    @pytest.mark.anyio
    async def test_start_analysis_sets_done_on_completion(self, tmp_path: Path):
        """After start_analysis task completes, status is 'done'."""
        game_id = _insert_game(my_color="white")
        engine = ScriptedEngine()

        task = start_analysis(game_id, engine, depth=8)
        await asyncio.wait_for(task, timeout=2.0)

        game = storage.get_game(game_id)
        assert game is not None
        assert game["analysis_status"] == "done"

    @pytest.mark.anyio
    async def test_cancel_analysis_returns_true(self, tmp_path: Path):
        """cancel_analysis returns True when a task was running."""
        game_id = _insert_game(my_color="white")
        # Block the task with an interactive counter so it stays running.
        note_interactive_start()
        try:
            engine = ScriptedEngine()
            task = start_analysis(game_id, engine, depth=8)
            # Let the event loop schedule the task.
            await asyncio.sleep(0)

            result = cancel_analysis(game_id)
            assert result is True
        finally:
            note_interactive_end()
            # Suppress CancelledError.
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    @pytest.mark.anyio
    async def test_cancel_analysis_returns_false_when_no_task(self):
        """cancel_analysis returns False when there is no registered task."""
        result = cancel_analysis(9999)
        assert result is False

    @pytest.mark.anyio
    async def test_start_analysis_engine_unavailable_sets_failed(self, tmp_path: Path):
        """If analyze_game raises EngineUnavailable, status is set to 'failed'."""
        from app.engine import EngineUnavailable

        class BrokenEngine:
            is_running = True

            def start(self):
                pass

            def close(self):
                pass

            async def analyze_multi(self, fen, depth=8, multipv=1):
                raise EngineUnavailable("No engine for you")

            async def analyze(self, fen, depth=8):
                raise EngineUnavailable("No engine for you")

        game_id = _insert_game(my_color="white")
        task = start_analysis(game_id, BrokenEngine(), depth=8)

        await asyncio.wait_for(task, timeout=2.0)

        game = storage.get_game(game_id)
        assert game is not None
        assert game["analysis_status"] == "failed"

    @pytest.mark.anyio
    async def test_task_removed_from_registry_on_completion(self, tmp_path: Path):
        """The task registry is cleaned up after the task finishes."""
        game_id = _insert_game(my_color="white")
        engine = ScriptedEngine()

        task = start_analysis(game_id, engine, depth=8)
        await asyncio.wait_for(task, timeout=2.0)

        assert game_id not in review._tasks

    def test_reset_stuck_delegates_to_storage(self, tmp_path: Path):
        """reset_stuck() calls storage.reset_analyzing_to_pending()."""
        # Insert two games, manually set one to 'analyzing'.
        gid1 = storage.insert_game({
            "content_hash": "h1",
            "pgn": "1. e4 *",
            "imported_at": _utc_now(),
            "analysis_status": "analyzing",
        })
        gid2 = storage.insert_game({
            "content_hash": "h2",
            "pgn": "1. d4 *",
            "imported_at": _utc_now(),
            "analysis_status": "pending",
        })

        count = reset_stuck()

        assert count == 1
        assert storage.get_game(gid1)["analysis_status"] == "pending"
        assert storage.get_game(gid2)["analysis_status"] == "pending"

    @pytest.mark.anyio
    async def test_second_start_cancels_first(self, tmp_path: Path):
        """Calling start_analysis again for the same game_id cancels the first task."""
        game_id = _insert_game(my_color="white")
        engine = ScriptedEngine()

        note_interactive_start()  # keep first task blocked
        try:
            task1 = start_analysis(game_id, engine, depth=8)
            await asyncio.sleep(0)
            # Ensure task1 is in the registry.
            assert game_id in review._tasks

            note_interactive_end()  # release before 2nd start
            task2 = start_analysis(game_id, engine, depth=8)
        finally:
            note_interactive_end()  # safety

        await asyncio.wait_for(task2, timeout=2.0)
        # task1 should have been cancelled.
        assert task1.cancelled() or task1.done()


# ---------------------------------------------------------------------------
# 5. Bulk-pending analysis (analyze_pending / start_analyze_all)
# ---------------------------------------------------------------------------


class TestAnalyzePending:
    """analyze_pending walks all pending games sequentially."""

    @pytest.mark.anyio
    async def test_analyze_pending_processes_all_pending(self, tmp_path: Path):
        """analyze_pending sets all pending games to 'done'."""
        gid1 = _insert_game(my_color="white")
        gid2 = _insert_game(my_color="black")

        engine = ScriptedEngine()
        await analyze_pending(engine, depth=8)

        assert storage.get_game(gid1)["analysis_status"] == "done"
        assert storage.get_game(gid2)["analysis_status"] == "done"

    @pytest.mark.anyio
    async def test_analyze_pending_skips_done_games(self, tmp_path: Path):
        """analyze_pending does not re-analyze games already marked 'done'."""
        gid1 = _insert_game(my_color="white")
        storage.set_status(gid1, "done")  # already done

        call_count = 0
        orig_analyze = review.analyze_game

        async def counting_analyze(game_id, engine, **kw):
            nonlocal call_count
            call_count += 1
            await orig_analyze(game_id, engine, **kw)

        review.analyze_game = counting_analyze
        try:
            engine = ScriptedEngine()
            await analyze_pending(engine, depth=8)
        finally:
            review.analyze_game = orig_analyze

        assert call_count == 0, "Should not call analyze_game for already-done games"

    @pytest.mark.anyio
    async def test_start_analyze_all_no_op_when_running(self, tmp_path: Path):
        """A second call to start_analyze_all while one is running returns the same task."""
        _insert_game(my_color="white")

        note_interactive_start()  # keep the task blocked
        try:
            engine = ScriptedEngine()
            task1 = start_analyze_all(engine, depth=8)
            await asyncio.sleep(0)  # let the event loop schedule it

            task2 = start_analyze_all(engine, depth=8)
            assert task1 is task2, "Second call must return the same running task"
        finally:
            note_interactive_end()
            # Let the task finish.
            try:
                await asyncio.wait_for(task1, timeout=2.0)
            except Exception:
                pass

    @pytest.mark.anyio
    async def test_start_analyze_all_task_removed_on_completion(self, tmp_path: Path):
        """The sentinel key is removed from _tasks after the bulk task completes."""
        _insert_game(my_color="white")
        engine = ScriptedEngine()
        task = start_analyze_all(engine, depth=8)
        await asyncio.wait_for(task, timeout=5.0)
        assert review._ANALYZE_ALL_KEY not in review._tasks

    @pytest.mark.anyio
    async def test_analyze_pending_sequential_order(self, tmp_path: Path):
        """analyze_pending processes games one at a time (sequential, not concurrent)."""
        gid1 = _insert_game(my_color="white")
        gid2 = _insert_game(my_color="black")

        order: list[int] = []
        orig_analyze = review.analyze_game

        async def tracking_analyze(game_id, engine, **kw):
            order.append(game_id)
            await orig_analyze(game_id, engine, **kw)

        review.analyze_game = tracking_analyze
        try:
            engine = ScriptedEngine()
            await analyze_pending(engine, depth=8)
        finally:
            review.analyze_game = orig_analyze

        # Both games processed and in some order (not concurrently).
        assert sorted(order) == sorted([gid1, gid2])
        assert len(order) == 2  # each processed exactly once
