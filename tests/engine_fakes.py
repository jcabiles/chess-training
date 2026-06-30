"""Scriptable test engine for the chess-training pipeline tests.

``ScriptedEngine`` satisfies the same async interface as ``StockfishEngine``
(``analyze``, ``analyze_multi``, ``is_running``, ``start``, ``close``) and can
be injected via ``app.dependency_overrides[get_engine]``.

It is constructed from a *script* — a mapping of FEN strings to pre-canned
``AnalysisResult`` values — so downstream tests (T5, T7, …) can plant a known
blunder or mate without touching a real Stockfish binary.

Supported script shapes
-----------------------
- **Single result** (``AnalysisResult``) — returned by both ``analyze()`` and
  as the first (and only) element of ``analyze_multi()``.
- **List of results** (``list[AnalysisResult]``) — used by ``analyze_multi()``
  ranked best-first; ``analyze()`` returns the first element.
- **Callable** (``Callable[[str], AnalysisResult | list[AnalysisResult]]``) —
  called with the FEN; the return value is treated as above.

Unscripted FENs fall back to a default: ``Cp(0)``, empty PV, depth 1.

Example
-------
::

    from chess.engine import Cp, Mate, PovScore
    import chess

    scripted = {
        "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1": AnalysisResult(
            score=PovScore(Mate(1), chess.WHITE),
            pv=[],
            pv_san=[],
            depth=1,
        ),
    }
    engine = ScriptedEngine(scripted)

    # Inject via FastAPI dependency override:
    app.dependency_overrides[get_engine] = lambda: engine
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Union

import chess
import chess.engine as chess_engine

from app.engine import DEFAULT_DEPTH, AnalysisResult

# Type alias for a single scripted value (before list-normalisation).
_ScriptValue = Union[
    AnalysisResult,
    List[AnalysisResult],
    Callable[[str], Union[AnalysisResult, List[AnalysisResult]]],
]

# The script mapping type: FEN → result or callable.
Script = Dict[str, _ScriptValue]


def _default_result() -> AnalysisResult:
    """Fallback for FENs that are not in the script."""
    return AnalysisResult(
        score=chess_engine.PovScore(chess_engine.Cp(0), chess.WHITE),
        pv=[],
        pv_san=[],
        depth=1,
    )


def _resolve(script_value: _ScriptValue, fen: str) -> List[AnalysisResult]:
    """Normalise a script entry to a list of AnalysisResult (best-first)."""
    if callable(script_value):
        script_value = script_value(fen)

    if isinstance(script_value, AnalysisResult):
        return [script_value]

    # Already a list.
    return list(script_value)


class ScriptedEngine:
    """A scriptable drop-in for ``StockfishEngine``.

    Accepts a ``script`` mapping FEN strings to pre-canned ``AnalysisResult``
    values (or lists for multipv, or callables).  FENs not in the script fall
    back to ``Cp(0)``.

    The engine requires no Stockfish binary and performs no I/O.

    Args:
        script: Mapping of FEN → scripted result.  May be empty (all FENs use
            the default fallback).
        default: Optional explicit default result for unscripted FENs.  When
            omitted, ``Cp(0)`` with an empty PV and depth 1 is used.
    """

    def __init__(
        self,
        script: Optional[Script] = None,
        *,
        default: Optional[AnalysisResult] = None,
    ) -> None:
        self._script: Script = script or {}
        self._default = default  # None → use _default_result() each time

    # -- lifecycle (no-ops; no subprocess) ----------------------------------

    @property
    def is_running(self) -> bool:
        return True

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def restart(self) -> None:
        """No-op restart: ScriptedEngine has no subprocess to kill."""

    # -- analysis -----------------------------------------------------------

    async def analyze(
        self,
        fen: str,
        depth: int = DEFAULT_DEPTH,
    ) -> AnalysisResult:
        """Return the first (best) scripted line for *fen*, or the default."""
        lines = await self.analyze_multi(fen, depth=depth, multipv=1)
        return lines[0]

    async def analyze_multi(
        self,
        fen: str,
        depth: int = DEFAULT_DEPTH,
        multipv: int = 1,
    ) -> List[AnalysisResult]:
        """Return up to *multipv* scripted lines for *fen*, best-first.

        If the script has fewer lines than *multipv*, all available lines are
        returned.  If the FEN is not scripted, a single default result is
        returned regardless of *multipv*.
        """
        script_value = self._script.get(fen)
        if script_value is None:
            fallback = self._default if self._default is not None else _default_result()
            return [fallback]

        lines = _resolve(script_value, fen)
        return lines[:multipv] if multipv < len(lines) else lines
