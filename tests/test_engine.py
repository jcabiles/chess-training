"""Unit tests for StockfishEngine — no real Stockfish binary required.

Each test injects a fake ``SimpleEngine``-shaped object directly into
``eng._engine`` and sets ``eng._pid = None`` so ``_poison()`` skips ``os.kill``
(never pass a real or None pid to os.kill).

Coverage:
- Cancellation safety: task.cancel() → CancelledError; engine poisoned (_engine None).
- Hard-timeout watchdog: ENGINE_HARD_TIMEOUT_S fires → EngineUnavailable; engine poisoned.
- EngineError propagation: analyse raises chess.engine.EngineError → EngineUnavailable; poisoned.
- Soft time cap: analyze() passes Limit(time=INTERACTIVE_SOFT_TIME_S); analyze_multi() passes time=None.
- restart() idempotence: poisons regardless of state; safe to call twice.
"""

from __future__ import annotations

import threading
import time
from typing import List

import chess
import chess.engine as chess_engine
import pytest

import app.engine as engine_module
from app.engine import (
    INTERACTIVE_SOFT_TIME_S,
    EngineUnavailable,
    StockfishEngine,
)

START_FEN = chess.STARTING_FEN


# ---------------------------------------------------------------------------
# Helpers: minimal InfoDict + fake SimpleEngine shapes
# ---------------------------------------------------------------------------

def _make_info(cp: int = 20) -> chess_engine.InfoDict:
    """Return a minimal InfoDict with a White-POV centipawn score."""
    info: chess_engine.InfoDict = {}  # type: ignore[assignment]
    info["score"] = chess_engine.PovScore(chess_engine.Cp(cp), chess.WHITE)
    board = chess.Board(START_FEN)
    pv = list(board.legal_moves)[:1]
    info["pv"] = pv
    info["depth"] = 1
    return info


class _ImmediateEngine:
    """Fake SimpleEngine whose analyse() returns immediately with a valid InfoDict."""

    def __init__(self, cp: int = 20):
        self._cp = cp
        self._calls: List[chess_engine.Limit] = []

    def analyse(
        self,
        board: chess.Board,
        limit: chess_engine.Limit,
        *,
        multipv: int = 1,
    ) -> chess_engine.InfoDict:
        self._calls.append(limit)
        return _make_info(self._cp)

    def close(self) -> None:
        pass


class _BlockingEngine:
    """Fake SimpleEngine whose analyse() blocks on a threading.Event indefinitely."""

    def __init__(self) -> None:
        self._unblock = threading.Event()

    def analyse(
        self,
        board: chess.Board,
        limit: chess_engine.Limit,
        *,
        multipv: int = 1,
    ) -> chess_engine.InfoDict:
        # Block until unblocked (set by teardown) so the worker thread can exit.
        self._unblock.wait()
        return _make_info()

    def close(self) -> None:
        self._unblock.set()


class _SlowEngine:
    """Fake SimpleEngine that sleeps longer than the hard timeout, then returns."""

    def __init__(self, sleep_s: float = 0.5) -> None:
        self._sleep_s = sleep_s

    def analyse(
        self,
        board: chess.Board,
        limit: chess_engine.Limit,
        *,
        multipv: int = 1,
    ) -> chess_engine.InfoDict:
        time.sleep(self._sleep_s)
        return _make_info()

    def close(self) -> None:
        pass


class _ErrorEngine:
    """Fake SimpleEngine whose analyse() raises chess.engine.EngineError."""

    def analyse(
        self,
        board: chess.Board,
        limit: chess_engine.Limit,
        *,
        multipv: int = 1,
    ) -> chess_engine.InfoDict:
        raise chess_engine.EngineError("simulated engine error")

    def close(self) -> None:
        pass


def _inject(eng: StockfishEngine, fake_engine: object) -> None:
    """Inject fake_engine into eng, bypassing start(). Set _pid=None so _poison skips os.kill."""
    eng._engine = fake_engine  # type: ignore[assignment]
    eng._pid = None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCancellationSafety:
    """Task cancellation poisons the engine and re-raises CancelledError."""

    @pytest.mark.anyio
    async def test_cancel_raises_cancelled_error_and_poisons(self):
        import asyncio

        eng = StockfishEngine()
        fake = _BlockingEngine()
        _inject(eng, fake)

        task = asyncio.create_task(eng.analyze(START_FEN))

        # Yield to the event loop briefly so the task enters _run_analyse and
        # acquires the lock, then cancel it.
        await asyncio.sleep(0.05)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        # Engine must be poisoned: handle nulled, is_running False.
        assert eng._engine is None
        assert eng.is_running is False

        # Unblock the worker thread so it can exit cleanly (no thread leak).
        fake._unblock.set()


class TestHardTimeoutWatchdog:
    """When the executor thread exceeds ENGINE_HARD_TIMEOUT_S, raise EngineUnavailable."""

    @pytest.mark.anyio
    async def test_timeout_raises_engine_unavailable_and_poisons(self, monkeypatch):
        # Patch the module-level constant to a tiny value so the test is fast.
        monkeypatch.setattr(engine_module, "ENGINE_HARD_TIMEOUT_S", 0.1)

        eng = StockfishEngine()
        fake = _SlowEngine(sleep_s=0.5)  # slower than the patched hard timeout
        _inject(eng, fake)

        with pytest.raises(EngineUnavailable, match="timed out"):
            await eng.analyze(START_FEN)

        # Engine must be poisoned after the timeout.
        assert eng._engine is None
        assert eng.is_running is False


class TestEngineErrorPropagation:
    """chess.engine.EngineError → EngineUnavailable; engine poisoned."""

    @pytest.mark.anyio
    async def test_engine_error_raises_unavailable_and_poisons(self):
        eng = StockfishEngine()
        fake = _ErrorEngine()
        _inject(eng, fake)

        with pytest.raises(EngineUnavailable, match="analysis failed"):
            await eng.analyze(START_FEN)

        assert eng._engine is None
        assert eng.is_running is False


class TestSoftCapAndMultiPVLimit:
    """analyze() passes time=INTERACTIVE_SOFT_TIME_S; analyze_multi() passes time=None."""

    @pytest.mark.anyio
    async def test_analyze_passes_soft_time_cap(self):
        eng = StockfishEngine()
        fake = _ImmediateEngine()
        _inject(eng, fake)

        await eng.analyze(START_FEN)

        assert len(fake._calls) == 1
        limit = fake._calls[0]
        assert limit.time == INTERACTIVE_SOFT_TIME_S, (
            f"analyze() should pass time={INTERACTIVE_SOFT_TIME_S}, got {limit.time}"
        )

    @pytest.mark.anyio
    async def test_analyze_multi_passes_no_time_cap(self):
        eng = StockfishEngine()
        fake = _ImmediateEngine()
        _inject(eng, fake)

        await eng.analyze_multi(START_FEN)

        assert len(fake._calls) == 1
        limit = fake._calls[0]
        # analyze_multi uses depth-only limit — time should be None.
        assert limit.time is None, (
            f"analyze_multi() should not pass a time cap, got {limit.time}"
        )


class TestRestartIdempotence:
    """restart() poisons the engine handle; safe to call multiple times."""

    @pytest.mark.anyio
    async def test_restart_with_running_engine_poisons(self):
        eng = StockfishEngine()
        fake = _ImmediateEngine()
        _inject(eng, fake)

        assert eng.is_running is True

        await eng.restart()

        assert eng._engine is None
        assert eng.is_running is False

    @pytest.mark.anyio
    async def test_restart_when_already_stopped_is_safe(self):
        """Calling restart() on a never-started / already-poisoned engine is a no-op."""
        eng = StockfishEngine()
        # _engine is None from construction — _poison(None) should be a no-op.

        await eng.restart()  # must not raise

        assert eng._engine is None
        assert eng.is_running is False

    @pytest.mark.anyio
    async def test_restart_twice_is_idempotent(self):
        eng = StockfishEngine()
        fake = _ImmediateEngine()
        _inject(eng, fake)

        await eng.restart()
        await eng.restart()  # second call on already-None engine; must not raise

        assert eng._engine is None
        assert eng.is_running is False
