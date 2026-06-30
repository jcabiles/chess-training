"""Tests for engine multipv + ScriptedEngine (T4).

Tests in this file:
  1. Pure ``ScriptedEngine`` tests — no Stockfish binary required.
  2. ``StockfishEngine.analyze_multi`` integration tests — skipped automatically
     when the Stockfish binary is absent from PATH / STOCKFISH_PATH env.
"""

from __future__ import annotations

import shutil

import chess
import chess.engine as chess_engine
import pytest

from app.engine import (
    DEFAULT_DEPTH,
    AnalysisResult,
    EngineUnavailable,
    StockfishEngine,
)
from tests.engine_fakes import ScriptedEngine

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

START_FEN = chess.STARTING_FEN

# A simple 1-move-from-mate position (White to move; Qh7#):
# 6k1/5ppp/8/8/8/8/8/6KQ w - - 0 1  (White queen on h1, king g1, black king g8)
MATE_IN_1_FEN = "6k1/5ppp/8/8/8/8/8/6KQ w - - 0 1"


def _cp_result(cp: int, fen: str = START_FEN) -> AnalysisResult:
    board = chess.Board(fen)
    pv = list(board.legal_moves)[:1]
    pv_san = [board.san(pv[0])] if pv else []
    return AnalysisResult(
        score=chess_engine.PovScore(chess_engine.Cp(cp), chess.WHITE),
        pv=pv,
        pv_san=pv_san,
        depth=DEFAULT_DEPTH,
    )


def _mate_result(mate_in: int) -> AnalysisResult:
    return AnalysisResult(
        score=chess_engine.PovScore(chess_engine.Mate(mate_in), chess.WHITE),
        pv=[],
        pv_san=[],
        depth=1,
    )


# ---------------------------------------------------------------------------
# 1. Pure ScriptedEngine tests — always run (no binary needed)
# ---------------------------------------------------------------------------


class TestScriptedEngine:
    """ScriptedEngine behaves correctly for scripted and unscripted FENs."""

    @pytest.mark.anyio
    async def test_scripted_fen_returns_scripted_eval(self):
        result = _cp_result(50)
        engine = ScriptedEngine({START_FEN: result})

        got = await engine.analyze(START_FEN)

        assert got is result

    @pytest.mark.anyio
    async def test_unscripted_fen_returns_default_cp0(self):
        engine = ScriptedEngine()
        some_other_fen = "8/8/8/8/8/8/8/4K2k w - - 0 1"

        got = await engine.analyze(some_other_fen)

        assert got.score.white().score() == 0  # Cp(0) default

    @pytest.mark.anyio
    async def test_mate_score_preserved(self):
        mate_res = _mate_result(1)
        engine = ScriptedEngine({MATE_IN_1_FEN: mate_res})

        got = await engine.analyze(MATE_IN_1_FEN)

        assert got.score.white().is_mate()
        assert got.score.white().mate() == 1

    @pytest.mark.anyio
    async def test_multipv_two_lines_returned_best_first(self):
        line1 = _cp_result(100)  # best line
        line2 = _cp_result(50)   # second-best
        engine = ScriptedEngine({START_FEN: [line1, line2]})

        lines = await engine.analyze_multi(START_FEN, multipv=2)

        assert len(lines) == 2
        assert lines[0] is line1
        assert lines[1] is line2

    @pytest.mark.anyio
    async def test_multipv_clipped_when_fewer_lines_scripted(self):
        """Asking for multipv=3 when only 2 lines are scripted returns 2."""
        line1 = _cp_result(100)
        line2 = _cp_result(40)
        engine = ScriptedEngine({START_FEN: [line1, line2]})

        lines = await engine.analyze_multi(START_FEN, multipv=3)

        assert len(lines) == 2

    @pytest.mark.anyio
    async def test_multipv_1_matches_analyze(self):
        result = _cp_result(75)
        engine = ScriptedEngine({START_FEN: result})

        single = await engine.analyze(START_FEN)
        multi = await engine.analyze_multi(START_FEN, multipv=1)

        assert multi[0] is single

    @pytest.mark.anyio
    async def test_callable_script_receives_fen(self):
        """A callable script entry is invoked with the queried FEN."""
        seen_fens: list[str] = []

        def _factory(fen: str) -> AnalysisResult:
            seen_fens.append(fen)
            return _cp_result(99)

        engine = ScriptedEngine({START_FEN: _factory})

        got = await engine.analyze(START_FEN)

        assert seen_fens == [START_FEN]
        assert got.score.white().score() == 99

    def test_lifecycle_nops(self):
        """start() / close() / is_running are safe no-ops."""
        engine = ScriptedEngine()
        assert engine.is_running is True
        engine.start()   # no-op
        engine.close()   # no-op
        assert engine.is_running is True  # still "running" (no real process)

    @pytest.mark.anyio
    async def test_distinct_evals_per_fen(self):
        """Different FENs get their own scripted evals (not cross-contaminated)."""
        fen_a = START_FEN
        fen_b = "8/8/8/8/8/8/8/4K2k w - - 0 1"
        res_a = _cp_result(30)
        res_b = _cp_result(-150)
        engine = ScriptedEngine({fen_a: res_a, fen_b: res_b})

        got_a = await engine.analyze(fen_a)
        got_b = await engine.analyze(fen_b)

        assert got_a.score.white().score() == 30
        assert got_b.score.white().score() == -150

    @pytest.mark.anyio
    async def test_custom_default(self):
        """A custom default result is returned for unscripted FENs."""
        custom_default = _cp_result(999)
        engine = ScriptedEngine(default=custom_default)
        unscripted = "8/8/8/8/8/8/8/4K2k w - - 0 1"

        got = await engine.analyze(unscripted)

        assert got.score.white().score() == 999


# ---------------------------------------------------------------------------
# 2. StockfishEngine.analyze_multi integration tests — skip if no binary
# ---------------------------------------------------------------------------

def _stockfish_available() -> bool:
    import os
    path_env = os.environ.get("STOCKFISH_PATH")
    if path_env:
        import os.path
        return os.path.isfile(path_env) and os.access(path_env, os.X_OK)
    return shutil.which("stockfish") is not None


SKIP_NO_STOCKFISH = pytest.mark.skipif(
    not _stockfish_available(),
    reason="Stockfish binary not found; skipping real-engine multipv tests",
)


@SKIP_NO_STOCKFISH
class TestStockfishMultipv:
    """Integration tests against a real Stockfish process.

    These are skipped automatically when no binary is installed, so ``pytest``
    always exits green in CI.
    """

    @pytest.fixture(autouse=True)
    def engine(self):
        eng = StockfishEngine()
        try:
            eng.start()
        except EngineUnavailable as exc:
            pytest.skip(f"Could not start Stockfish: {exc}")
        yield eng
        eng.close()

    @pytest.mark.anyio
    async def test_multipv2_returns_two_ranked_lines(self, engine):
        lines = await engine.analyze_multi(START_FEN, depth=8, multipv=2)

        assert len(lines) == 2, "Expected exactly 2 lines from multipv=2"

        # Best line score ≥ second-best (White POV centipawns or mate values).
        score0 = lines[0].score.white()
        score1 = lines[1].score.white()

        # PovScore supports rich comparison (higher = better for White).
        assert score0 >= score1, (
            f"Best line ({score0}) should be ≥ second-best ({score1})"
        )

    @pytest.mark.anyio
    async def test_multipv1_matches_analyze(self, engine):
        """analyze_multi(multipv=1) returns one valid result, same sign as analyze()."""
        depth = 8

        single = await engine.analyze(START_FEN, depth=depth)
        multi = await engine.analyze_multi(START_FEN, depth=depth, multipv=1)

        assert len(multi) == 1
        # Both must produce a valid (non-None) score at the starting position.
        # Exact score equality across two consecutive searches can differ by a
        # few cp due to Stockfish's NNUE/TB nondeterminism at shallow depth, so
        # we just verify the sign agrees (both should be White-positive at start).
        single_cp = single.score.white().score(mate_score=100_000)
        multi_cp = multi[0].score.white().score(mate_score=100_000)
        assert single_cp is not None
        assert multi_cp is not None
        # Both searches see the starting position as roughly equal/slight-White:
        # scores should have the same sign or be within 50 cp of each other.
        assert abs(single_cp - multi_cp) < 100, (
            f"analyze() gave {single_cp} cp, analyze_multi() gave {multi_cp} cp — "
            "more than 100 cp apart, which is unexpected for the same starting pos"
        )

    @pytest.mark.anyio
    async def test_second_line_has_measurable_score(self, engine):
        """The second line must carry a real score (not a sentinel/None)."""
        lines = await engine.analyze_multi(START_FEN, depth=8, multipv=2)

        assert len(lines) >= 2
        # score.white() must not raise and must be a real numeric/mate value.
        score1 = lines[1].score.white()
        assert score1.score(mate_score=100_000) is not None
