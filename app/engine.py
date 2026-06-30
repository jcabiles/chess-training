"""Stockfish engine wrapper for the chess-training FastAPI app.

This module owns the lifecycle of a SINGLE Stockfish UCI process and exposes a
safe async surface for analyzing positions.

Design notes / constraints (see docs/design/specs/stockfish-analysis-board.md):

* One process only. ``chess.engine.SimpleEngine.popen_uci`` launches a long-lived
  Stockfish subprocess; we keep exactly one alive for the app's lifetime and reuse
  it across requests.

* Thread-safety + event loop. ``SimpleEngine`` is a synchronous, *not* thread-safe
  driver. Its ``analyse()`` call BLOCKS, so calling it directly on the asyncio event
  loop would stall the whole server. We therefore run the blocking call inside a
  thread-pool executor via ``loop.run_in_executor(...)``. But a thread pool is
  inherently concurrent and ``SimpleEngine`` cannot tolerate two overlapping calls
  (they would corrupt UCI protocol state). So EVERY engine access is serialized
  behind a single ``asyncio.Lock`` -> at most one analysis in flight at any moment.
  (The alternative would be the native-async ``UciProtocol``; we deliberately pick
  ONE mechanism — SimpleEngine + executor + lock — and never mix them.)

* Import-safe. Importing this module must NOT launch Stockfish or raise if the
  binary is absent. The process is created lazily on first ``start()`` / ``analyze()``
  (or explicitly by a FastAPI lifespan handler). Absence of the binary raises a
  catchable ``EngineUnavailable`` rather than crash-looping.

This module does NOT classify or normalize evals — that is ``analysis.py``'s job.
It returns the raw ``PovScore`` and the principal variation as ``chess.Move`` objects
(plus, as a convenience, SAN for those moves rendered on a copy of the position).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
from dataclasses import dataclass, field
from typing import List, Optional

import chess
import chess.engine as chess_engine

# ---------------------------------------------------------------------------
# Engine configuration constants (documented; no UI control — see spec "Engine
# config"). Depth is fixed (not a time cap) so before/after-move evals are
# searched to the same depth and cpLoss is comparable.
# ---------------------------------------------------------------------------

#: Stockfish search threads. Tuned for usable per-move latency on a single-user box.
ENGINE_THREADS: int = 2

#: Stockfish transposition-table size in MiB.
ENGINE_HASH_MB: int = 128

#: Default fixed search depth (target depth 18 per spec).
DEFAULT_DEPTH: int = 18

#: Environment variable that, if set, points at the Stockfish binary.
STOCKFISH_PATH_ENV: str = "STOCKFISH_PATH"

#: Soft time cap (seconds) passed to Stockfish for interactive (per-move) analyses.
#: Overridable via ENGINE_SOFT_TIME env var. Must be < ENGINE_HARD_TIMEOUT_S.
INTERACTIVE_SOFT_TIME_S: float = float(os.environ.get("ENGINE_SOFT_TIME", "3.0"))

#: Hard per-call asyncio watchdog (seconds). If the executor thread has not returned
#: by this deadline, the engine is poisoned and EngineUnavailable is raised.
#: Overridable via ENGINE_HARD_TIMEOUT env var. Must be > INTERACTIVE_SOFT_TIME_S.
ENGINE_HARD_TIMEOUT_S: float = float(os.environ.get("ENGINE_HARD_TIMEOUT", "8.0"))


class EngineUnavailable(RuntimeError):
    """Raised when the Stockfish binary cannot be located or launched.

    Callers (e.g. FastAPI routes / startup) should catch this and surface a
    friendly error instead of letting the server crash-loop.
    """


def _locate_binary() -> str:
    """Resolve the path to the Stockfish binary.

    Lookup order:
      1. The ``STOCKFISH_PATH`` environment variable, if set (must be an
         executable file at that path).
      2. ``shutil.which("stockfish")`` on the system ``PATH``.

    Returns:
        Absolute or PATH-resolvable path to the Stockfish executable.

    Raises:
        EngineUnavailable: if no usable binary is found.
    """
    configured = os.environ.get(STOCKFISH_PATH_ENV)
    if configured:
        if os.path.isfile(configured) and os.access(configured, os.X_OK):
            return configured
        raise EngineUnavailable(
            f"{STOCKFISH_PATH_ENV}={configured!r} is not an executable file."
        )

    found = shutil.which("stockfish")
    if found:
        return found

    raise EngineUnavailable(
        "Stockfish binary not found. Install it (e.g. `brew install stockfish`) "
        f"or set the {STOCKFISH_PATH_ENV} environment variable to its path."
    )


@dataclass
class AnalysisResult:
    """Raw analysis output for a single position.

    Intentionally minimal — no eval normalization or quality classification here
    (that belongs in ``analysis.py``).

    Attributes:
        score: The engine's ``PovScore`` straight from ``info["score"]``. The
            caller normalizes (e.g. ``.white()``) and classifies.
        pv: The principal variation as ``chess.Move`` objects (``info["pv"]``);
            empty list if the engine returned none.
        pv_san: The same PV rendered as SAN strings, produced by pushing each move
            on a COPY of the analyzed board. ``pv_san[0]`` is the best move's SAN.
        depth: The depth the engine reported reaching (``info.get("depth")``).
    """

    score: chess_engine.PovScore
    pv: List[chess.Move] = field(default_factory=list)
    pv_san: List[str] = field(default_factory=list)
    depth: Optional[int] = None


class StockfishEngine:
    """Lifecycle + serialized async access to a single Stockfish process.

    Typical usage from FastAPI:

        engine = StockfishEngine()

        # in a lifespan/startup handler:
        engine.start()                      # or rely on lazy start in analyze()

        # in a route:
        result = await engine.analyze(fen, depth=18)

        # in a lifespan/shutdown handler:
        engine.close()

    Construction is cheap and does NOT touch the binary; the subprocess is only
    spawned by ``start()`` / first ``analyze()``.
    """

    def __init__(
        self,
        *,
        threads: int = ENGINE_THREADS,
        hash_mb: int = ENGINE_HASH_MB,
    ) -> None:
        self._threads = threads
        self._hash_mb = hash_mb
        self._engine: Optional[chess_engine.SimpleEngine] = None
        # OS pid of the Stockfish subprocess, captured in start(). Used by
        # _poison() to SIGKILL the process without relying on a possibly-hung
        # engine.close() call.
        self._pid: Optional[int] = None
        # Serializes ALL engine access. SimpleEngine is not thread-safe and the
        # executor is concurrent, so only one analysis may be in flight at once.
        self._lock = asyncio.Lock()

    # -- lifecycle ----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """Whether the Stockfish subprocess has been launched."""
        return self._engine is not None

    def start(self) -> None:
        """Launch and configure the Stockfish process (idempotent).

        Locates the binary, opens ONE UCI process, and applies the documented
        ``Threads`` / ``Hash`` configuration. Safe to call multiple times; a
        no-op if already running.

        Raises:
            EngineUnavailable: if the binary is missing or fails to launch.
        """
        if self._engine is not None:
            return

        binary = _locate_binary()
        try:
            engine = chess_engine.SimpleEngine.popen_uci(binary)
        except Exception as exc:  # FileNotFoundError, EngineError, OSError, ...
            raise EngineUnavailable(
                f"Failed to launch Stockfish at {binary!r}: {exc}"
            ) from exc

        try:
            engine.configure({"Threads": self._threads, "Hash": self._hash_mb})
        except Exception as exc:
            # Don't leak the subprocess if configuration fails.
            try:
                engine.quit()
            except Exception:
                pass
            raise EngineUnavailable(
                f"Failed to configure Stockfish: {exc}"
            ) from exc

        # Capture the OS pid so _poison() can SIGKILL without blocking.
        # engine.transport is asyncio.SubprocessTransport (public attribute,
        # confirmed in python-chess SimpleEngine.__init__); get_pid() is part
        # of asyncio.SubprocessTransport's documented interface.
        pid: Optional[int] = None
        try:
            pid = engine.transport.get_pid()
        except Exception:
            # Fall back through known private attribute chains; if none work,
            # _pid stays None and _poison() degrades to fire-and-forget close().
            try:
                pid = getattr(engine, "_transport", None) and engine._transport.get_pid()
            except Exception:
                pass
            if not pid:
                try:
                    pid = engine._process.pid  # type: ignore[attr-defined]
                except Exception:
                    pid = None

        self._pid = pid
        self._engine = engine

    def close(self) -> None:
        """Cleanly shut down the Stockfish process (idempotent).

        Sends the UCI ``quit`` and releases the handle. Safe to call when the
        engine was never started. Never raises on shutdown failure.
        """
        engine, self._engine = self._engine, None
        if engine is not None:
            try:
                engine.quit()
            except Exception:
                # Best-effort shutdown; the process is being torn down anyway.
                pass

    # -- poison / restart ---------------------------------------------------

    def _poison(self, engine: Optional[chess_engine.SimpleEngine]) -> None:
        """Force-terminate *engine* and null the handle. SYNC, never awaits.

        Called from both ``_run_analyse`` exception handlers and ``restart()``.
        Must never raise. Strategy:

        1. Null ``self._engine`` and ``self._pid`` immediately so no later caller
           can reuse the poisoned handle.
        2. KILL FIRST — send SIGKILL to the subprocess pid without waiting.
           ``engine.close()``/``quit()`` call ``process.wait()`` internally which
           can block indefinitely if the Stockfish thread is hung; we must not call
           them inline.
        3. Schedule a fire-and-forget ``engine.close()`` in an executor as residual
           cleanup. We do NOT await it — if it hangs, the loop is unaffected.
        """
        if engine is None:
            return

        # Capture pid before nulling (self._pid may already differ if racing).
        pid = self._pid

        # Null handles immediately — correctness: next call gets a fresh engine.
        self._engine = None
        self._pid = None

        # KILL FIRST — instant, non-blocking.
        if pid is not None:
            try:
                os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except Exception:
                pass  # process may already be dead; ignore

        # Fire-and-forget residual cleanup — do NOT await.
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, engine.close)
        except Exception:
            pass  # best-effort; if we can't schedule, just drop it

    async def restart(self) -> None:
        """Force-restart the engine (lock-free, idempotent).

        Poisons (kills + nulls) the current engine process without acquiring the
        lock — intentional, since the lock may be held by a wedged call. Leaves
        ``self._engine = None`` so the next ``analyze*()`` lazily starts a fresh
        process. Safe to call if the engine was never started.
        """
        self._poison(self._engine)

    # -- analysis -----------------------------------------------------------

    async def _run_analyse(
        self,
        board: chess.Board,
        limit: chess_engine.Limit,
        multipv: int = 1,
    ) -> List[chess_engine.InfoDict]:
        """Internal: run engine.analyse under the lock + hard-timeout watchdog.

        Both public methods delegate here. Serializes via ``self._lock``, lazily
        starts the engine, shields the executor future from cancellation, and
        applies the hard-timeout watchdog (``ENGINE_HARD_TIMEOUT_S``).

        Returns:
            A list of ``InfoDict`` (always a list; normalized from a bare dict when
            ``multipv`` is effectively 1 and python-chess returns a dict).

        Raises:
            EngineUnavailable: on timeout, EngineError, or EngineTerminatedError.
            asyncio.CancelledError: re-raised immediately after poisoning so the
                caller's task can honor its cancellation.
        """
        async with self._lock:
            if self._engine is None:
                self.start()
            # Capture a LOCAL reference to the engine. _call must close over this
            # local — NOT self._engine — so that _poison() nulling self._engine
            # does not cause an AttributeError in the still-running executor thread.
            engine = self._engine
            assert engine is not None  # start() guarantees this or raised

            loop = asyncio.get_running_loop()

            def _call() -> List[chess_engine.InfoDict]:
                result = engine.analyse(board, limit, multipv=multipv)
                # Normalize: always return a list (multipv=None/1 may return dict).
                if isinstance(result, list):
                    return result
                return [result]

            fut = loop.run_in_executor(None, _call)

            try:
                # shield: a client-disconnect CancelledError does NOT cancel the
                #   executor future (the thread keeps running to completion).
                # wait_for: hard watchdog — raises TimeoutError after the deadline
                #   regardless of shield; the shielded fut keeps running but we
                #   poison the engine and stop waiting.
                info = await asyncio.wait_for(
                    asyncio.shield(fut), timeout=ENGINE_HARD_TIMEOUT_S
                )
            except asyncio.CancelledError:
                # Re-raise immediately — swallowing CancelledError wedges the task.
                # Poison first so the next caller gets a clean engine.
                self._poison(engine)
                raise
            except asyncio.TimeoutError:
                self._poison(engine)
                raise EngineUnavailable(
                    "Stockfish timed out; engine restarted"
                )
            except chess_engine.EngineTerminatedError as exc:
                # Process died on its own; null so the next call relaunches.
                self._engine = None
                raise EngineUnavailable(
                    f"Stockfish process terminated unexpectedly: {exc}"
                ) from exc
            except chess_engine.EngineError as exc:
                # Corrupted-but-alive handle — must poison, not just raise.
                self._poison(engine)
                raise EngineUnavailable(
                    f"Stockfish analysis failed: {exc}"
                ) from exc

        return info

    async def analyze_multi(
        self,
        fen: str,
        depth: int = DEFAULT_DEPTH,
        multipv: int = 1,
    ) -> List[AnalysisResult]:
        """Analyze a position to a fixed depth, returning up to *multipv* ranked lines.

        Uses the SAME single ``asyncio.Lock`` + ``run_in_executor`` pattern as
        :meth:`analyze` (both delegate to :meth:`_run_analyse`). When ``multipv=1``
        the list has exactly one element and its score / PV match what
        :meth:`analyze` returns.

        Args:
            fen: The position to analyze, in Forsyth-Edwards Notation.
            depth: Fixed search depth.  Defaults to ``DEFAULT_DEPTH``.
            multipv: Number of distinct lines to return (best-first).

        Returns:
            A list of :class:`AnalysisResult`, index 0 = best line, length ≤ *multipv*.
            (Fewer lines are returned when the position has fewer legal moves than
            *multipv*.)

        Raises:
            EngineUnavailable: if the binary is missing or the engine cannot run.
            ValueError: if ``fen`` is not a valid FEN.
        """
        try:
            board = chess.Board(fen)
        except ValueError as exc:
            raise ValueError(f"Invalid FEN: {fen!r} ({exc})") from exc

        # Background reviews use depth-only limit (no soft time cap).
        infos = await self._run_analyse(
            board, chess_engine.Limit(depth=depth), multipv=multipv
        )

        # --- post-processing (outside the lock; pure, no engine access) ---

        results: List[AnalysisResult] = []
        for info in infos:
            score: chess_engine.PovScore = info["score"]
            pv: List[chess.Move] = list(info.get("pv", []))

            pv_san: List[str] = []
            san_board = board.copy()
            for move in pv:
                try:
                    pv_san.append(san_board.san(move))
                    san_board.push(move)
                except (AssertionError, ValueError):
                    break

            results.append(
                AnalysisResult(
                    score=score,
                    pv=pv,
                    pv_san=pv_san,
                    depth=info.get("depth"),
                )
            )

        return results

    async def analyze(self, fen: str, depth: int = DEFAULT_DEPTH) -> AnalysisResult:
        """Analyze a position to a fixed depth.

        Lazily starts the engine if needed, then runs the BLOCKING
        ``engine.analyse(...)`` in a thread-pool executor while holding the
        module's ``asyncio.Lock`` so no two analyses overlap (SimpleEngine is not
        thread-safe). A soft time cap (``INTERACTIVE_SOFT_TIME_S``) is passed to
        Stockfish; a hard asyncio watchdog (``ENGINE_HARD_TIMEOUT_S``) guards
        against a totally hung process.

        Args:
            fen: The position to analyze, in Forsyth-Edwards Notation.
            depth: Fixed search depth (target; may not be reached if the soft time
                cap fires first). Defaults to ``DEFAULT_DEPTH``.

        Returns:
            An :class:`AnalysisResult` with the raw ``PovScore`` and the PV (as
            ``chess.Move`` objects plus SAN).

        Raises:
            EngineUnavailable: if the binary is missing or the engine cannot run.
            ValueError: if ``fen`` is not a valid FEN.
        """
        # Validate the FEN up front so we fail fast with a clear error rather than
        # deep inside the executor thread.
        try:
            board = chess.Board(fen)
        except ValueError as exc:
            raise ValueError(f"Invalid FEN: {fen!r} ({exc})") from exc

        # Interactive searches get a soft time cap so sharp positions don't stall
        # the UI; depth is still passed so Stockfish stops at depth if reached first.
        infos = await self._run_analyse(
            board,
            chess_engine.Limit(depth=depth, time=INTERACTIVE_SOFT_TIME_S),
        )
        info = infos[0]

        # --- post-processing (outside the lock; pure, no engine access) ---

        score: chess_engine.PovScore = info["score"]
        pv: List[chess.Move] = list(info.get("pv", []))

        # Render SAN by pushing each PV move on a COPY of the analyzed position
        # (never mutate `board` in a way that matters; a fresh copy is safest and
        # keeps SAN aligned with the exact position that was analyzed).
        pv_san: List[str] = []
        san_board = board.copy()
        for move in pv:
            try:
                pv_san.append(san_board.san(move))
                san_board.push(move)
            except (AssertionError, ValueError):
                # Defensive: if the PV ever contains an inconsistent move, stop
                # rather than emit garbage SAN.
                break

        depth_reached = info.get("depth")

        return AnalysisResult(
            score=score,
            pv=pv,
            pv_san=pv_san,
            depth=depth_reached,
        )
