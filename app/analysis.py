"""Pure move-quality classification and eval normalization.

This module contains ONLY pure functions: no engine, no I/O, no network. It is
importable and fully unit-testable without a Stockfish binary present (the
pure-Python ``chess`` package is the only dependency, used for its
``chess.engine.PovScore`` type).

POV sign rationale
------------------
All evaluations are normalized to a single **White-POV centipawn** convention,
``E(pos)``. After a move the side-to-move flips, so the raw engine eval of the
after-position is reported from the *opponent's* POV. To avoid the resulting
double-negation bug, we (1) always store evals in White-POV cp, and (2) flip the
centipawn-loss formula by the mover's color:

    White moved: cpLoss = E(before) - E(after)
    Black moved: cpLoss = E(after) - E(before)

Mate scores are mapped onto the same White-POV cp axis as
``sign * (MATE_CP - N)`` where ``N`` is moves-to-mate, so a faster mate (M1) has
a larger magnitude than a slower one (M8). This makes "had a forced mate, threw
it away" and "walked into a mate" land naturally in the **blunder** bucket via
the same cpLoss formula.
"""

from __future__ import annotations

import chess
import chess.engine

# Magnitude assigned to a mate-in-zero. A mate in N is mapped to
# sign * (MATE_CP - N), so faster mates have larger magnitude (M1 > M8).
MATE_CP: int = 10000

# Centipawn-loss bucket thresholds (tunable). Each constant is the INCLUSIVE
# upper bound (in integer cp) of its bucket:
#   0   .. BEST_MAX        -> "best"
#   ..  .. GOOD_MAX        -> "good"
#   ..  .. INACCURACY_MAX  -> "inaccuracy"
#   ..  .. MISTAKE_MAX     -> "mistake"
#   >   MISTAKE_MAX        -> "blunder"
BEST_MAX: int = 10
GOOD_MAX: int = 50
INACCURACY_MAX: int = 100
MISTAKE_MAX: int = 250

# Sane ceiling for cpLoss so mate-axis arithmetic can't produce absurd display
# values (e.g. converting one mate distance into another never exceeds this).
MAX_CP_LOSS: int = 2 * MATE_CP


def pov_score_to_white_cp(score: chess.engine.PovScore) -> int:
    """Convert a ``chess.engine.PovScore`` to White-POV centipawns.

    Args:
        score: A python-chess ``PovScore`` (as returned in an engine analysis
            ``info["score"]``). Wraps either a ``Cp`` or a ``Mate`` value.

    Returns:
        The evaluation as an integer in White-POV centipawns. A real centipawn
        score is returned as-is; a mate in ``N`` is mapped to
        ``sign * (MATE_CP - N)`` (positive = White mates, negative = Black
        mates), so a faster mate has a larger magnitude.
    """
    white = score.white()
    if white.is_mate():
        # white.mate() is signed: positive => White delivers mate, negative =>
        # Black delivers mate. N is the (positive) distance to mate.
        mate_in = white.mate()
        sign = 1 if mate_in > 0 else -1
        n = abs(mate_in)
        return sign * (MATE_CP - n)
    # Not a mate: score() returns the centipawn value (never None here).
    cp = white.score()
    assert cp is not None  # not-a-mate implies a concrete cp value
    return int(cp)


def bucket(cp_loss: int) -> str:
    """Map a (non-negative) centipawn loss to a quality label.

    Args:
        cp_loss: Centipawn loss, expected to already be clamped to ``>= 0``.

    Returns:
        One of ``"best"``, ``"good"``, ``"inaccuracy"``, ``"mistake"``,
        ``"blunder"``.
    """
    if cp_loss <= BEST_MAX:
        return "best"
    if cp_loss <= GOOD_MAX:
        return "good"
    if cp_loss <= INACCURACY_MAX:
        return "inaccuracy"
    if cp_loss <= MISTAKE_MAX:
        return "mistake"
    return "blunder"


def classify(white_cp_before: int, white_cp_after: int, mover_is_white: bool) -> str:
    """Classify a move's quality from before/after White-POV evals.

    cpLoss is the eval the mover gave up, sign-corrected by who moved:

        White moved: cpLoss = before - after
        Black moved: cpLoss = after - before

    The result is clamped to ``>= 0`` (engine noise or an engine-matching move
    can yield a small negative) and capped at ``MAX_CP_LOSS`` for display
    safety, then bucketed.

    Args:
        white_cp_before: White-POV centipawn eval of the position BEFORE the
            move (mate-mapped via :func:`pov_score_to_white_cp`).
        white_cp_after: White-POV centipawn eval of the position AFTER the move.
        mover_is_white: Whether the side that just moved was White.

    Returns:
        The quality label for the move (see :func:`bucket`).
    """
    if mover_is_white:
        cp_loss = white_cp_before - white_cp_after
    else:
        cp_loss = white_cp_after - white_cp_before
    cp_loss = max(0, cp_loss)
    cp_loss = min(cp_loss, MAX_CP_LOSS)
    return bucket(cp_loss)
