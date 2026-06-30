"""Per-side chess Accuracy % and estimated Elo from stored per-ply evals.

Pure module — no Stockfish engine, no database, no FastAPI. Importable and
fully unit-testable without a Stockfish binary present (python-chess is the
only non-stdlib dependency, used solely for ``chess.Board.turn``).

Accuracy formula
----------------
Uses the Lichess per-move accuracy model, calibrated on a **0–100 win%
scale** (not 0–1 win-prob). The ``100.0 *`` multiplier in :func:`summarize`
is load-bearing: omit it and ``exp(-K * tiny_drop) ≈ 1`` so every move
silently scores ~100%.

Win-probability calls are delegated to ``analysis.win_prob_white``, which
returns 0.5 when both ``eval_cp_white`` and ``mate_white`` are ``None`` (an
already-analyzed "done" ply normally has one of the two set; the 0.5 fallback
is a safe guard for incomplete rows).

Elo estimate
------------
:func:`accuracy_to_elo` is a rough single-game heuristic — **not** a
calibrated rating claim. Anchors: 70 % → ~1150, 80 % → ~1600, 90 % → ~2050,
95 % → ~2275. ``ELO_MAX`` (2900) is a defensive cap, not an expected value —
100 % accuracy maps to ~2500, so the cap only guards pathological inputs.
"""

from __future__ import annotations

import math
from statistics import mean
from typing import Any

import chess

from app.analysis import win_prob_white

# ---------------------------------------------------------------------------
# Lichess per-move accuracy constants
# ---------------------------------------------------------------------------

LICHESS_A: float = 103.1668
LICHESS_K: float = 0.04354
LICHESS_B: float = 3.1669

# ---------------------------------------------------------------------------
# Elo heuristic constants (tunable)
# ---------------------------------------------------------------------------

ACC_ELO_SLOPE: float = 45.0
ACC_ELO_INTERCEPT: float = -2000.0
ELO_MIN: int = 100
ELO_MAX: int = 2900


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _field(row: Any, key: str) -> Any:
    """Return ``row[key]`` (dict) or ``getattr(row, key, None)`` (object).

    Accepts both dict rows and ORM-style objects that expose fields as
    attributes (e.g. ``PlyDetail`` named-tuples / dataclasses).
    """
    try:
        return row[key]
    except (TypeError, KeyError):
        return getattr(row, key, None)


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp ``value`` to the closed interval ``[lo, hi]``."""
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def move_accuracy(win_pct_drop: float) -> float:
    """Compute per-move accuracy from the win-% drop on a 0–100 scale.

    Uses the Lichess per-move accuracy model::

        acc = LICHESS_A * exp(-LICHESS_K * max(0.0, win_pct_drop)) - LICHESS_B

    clamped to ``[0.0, 100.0]``.

    Args:
        win_pct_drop: Win-percentage drop for the moving side, expressed on a
            **0–100 scale** (not 0–1). A negative drop (the move improved the
            position) is treated as 0 before the exponent.

    Returns:
        Per-move accuracy in ``[0.0, 100.0]``.
    """
    drop = max(0.0, win_pct_drop)
    acc = LICHESS_A * math.exp(-LICHESS_K * drop) - LICHESS_B
    return _clamp(acc, 0.0, 100.0)


def accuracy_to_elo(accuracy: float) -> int:
    """Estimate Elo from a side's average accuracy % (rough single-game heuristic).

    Linear mapping::

        elo = clamp(round(ACC_ELO_SLOPE * accuracy + ACC_ELO_INTERCEPT),
                    ELO_MIN, ELO_MAX)

    ``ELO_MAX`` (2900) is a defensive cap — 100 % accuracy maps to ~2500, so
    the cap only guards pathological inputs.

    Args:
        accuracy: Average accuracy in ``[0.0, 100.0]`` for one side.

    Returns:
        Estimated Elo as an integer in ``[ELO_MIN, ELO_MAX]``.
    """
    raw = round(ACC_ELO_SLOPE * accuracy + ACC_ELO_INTERCEPT)
    return int(_clamp(raw, ELO_MIN, ELO_MAX))


def summarize(plies: list[Any] | None, my_color: str | None) -> dict:
    """Compute per-side Accuracy % and estimated Elo from stored per-ply evals.

    Args:
        plies: Ordered list of ply rows for one game, each exposing
            ``eval_cp_white`` (White-POV centipawns or ``None``),
            ``mate_white`` (signed moves-to-mate or ``None``), and
            ``fen_before`` (FEN string before the move, or ``None``). Rows may
            be dicts **or** objects (``_field`` handles both).
        my_color: ``'white'`` or ``'black'`` when the user's side is known,
            ``None`` otherwise. Passed through unchanged into the result.

    Returns:
        A dict with keys::

            {
                "white_accuracy": float | None,
                "black_accuracy": float | None,
                "white_elo":      int   | None,
                "black_elo":      int   | None,
                "white_moves":    int,
                "black_moves":    int,
                "my_color":       str | None,
            }

        Sides with no scored moves (e.g. a one-ply game or all FENs missing)
        return ``None`` accuracy and ``None`` elo with 0 move count.

    Notes:
        - The final ply is excluded — it has no successor eval to compare against.
        - A ply with ``fen_before is None`` is skipped (``chess.Board(None)``
          raises, so we guard before calling it).
        - When both ``eval_cp_white`` and ``mate_white`` are ``None`` on a ply,
          ``win_prob_white`` returns 0.5 (position treated as even); this is a
          safe fallback for incomplete rows in an otherwise "done" analysis.
        - The ``100.0 *`` applied to ``win_prob_white``'s output is load-bearing:
          the Lichess formula is calibrated on win% (0–100), not win-prob (0–1).
    """
    # Guard: too few plies to compute any move accuracy.
    if plies is None or len(plies) <= 1:
        return {
            "white_accuracy": None,
            "black_accuracy": None,
            "white_elo": None,
            "black_elo": None,
            "white_moves": 0,
            "black_moves": 0,
            "my_color": my_color,
        }

    white_accs: list[float] = []
    black_accs: list[float] = []

    # Iterate over plies[0..n-2]; the final ply (index n-1) has no successor.
    for i in range(len(plies) - 1):
        row = plies[i]
        nxt = plies[i + 1]

        fen = _field(row, "fen_before")
        if fen is None:
            continue  # chess.Board(None) raises; skip this ply

        mover = chess.Board(fen).turn  # chess.WHITE (True) or chess.BLACK (False)

        # Win% on a 0–100 scale — the ×100 is load-bearing (see module docstring).
        wp_i = 100.0 * win_prob_white(
            _field(row, "eval_cp_white"),
            _field(row, "mate_white"),
        )
        wp_next = 100.0 * win_prob_white(
            _field(nxt, "eval_cp_white"),
            _field(nxt, "mate_white"),
        )

        # Drop is always from the mover's perspective.
        if mover == chess.WHITE:
            drop = wp_i - wp_next
        else:
            drop = wp_next - wp_i

        acc = move_accuracy(drop)

        if mover == chess.WHITE:
            white_accs.append(acc)
        else:
            black_accs.append(acc)

    white_accuracy = round(mean(white_accs), 1) if white_accs else None
    black_accuracy = round(mean(black_accs), 1) if black_accs else None
    white_elo = accuracy_to_elo(white_accuracy) if white_accuracy is not None else None
    black_elo = accuracy_to_elo(black_accuracy) if black_accuracy is not None else None

    return {
        "white_accuracy": white_accuracy,
        "black_accuracy": black_accuracy,
        "white_elo": white_elo,
        "black_elo": black_elo,
        "white_moves": len(white_accs),
        "black_moves": len(black_accs),
        "my_color": my_color,
    }
