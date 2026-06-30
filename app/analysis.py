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

import math as _math  # used by win_prob_from_cp; stdlib only, not re-exported

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


# ---------------------------------------------------------------------------
# Win-probability math (T3 additions — pure, no engine)
# ---------------------------------------------------------------------------
#
# Design note (Refuter resolution #10): the review pipeline classifies leaks by
# *win-probability drop* (see ``leak_severity`` below), while play-mode uses
# *centipawn loss* (``classify`` above). The two systems may disagree on
# borderline moves by design — win-% is more sensitive to practical danger at
# the ~800-1100 level, while cp-loss is better for opening-prep feedback. Both
# remain in the module; neither replaces the other.

def win_prob_from_cp(cp: int) -> float:
    """Convert a centipawn advantage to a win probability (Lichess model).

    Returns the probability in ``[0.0, 1.0]`` for the side whose advantage is
    ``cp`` centipawns (i.e., from *that side's* point of view).  A value of 0.5
    means the position is dead even; 1.0 means a guaranteed win.

    Formula (Lichess winning-chances model)::

        p = 1 / (1 + exp(-0.00368208 * cp))

    which is equivalent to ``(50 + 50 * (2 / (1 + exp(...)) - 1)) / 100``.
    Result is clamped to ``[0.0, 1.0]`` to guard against floating-point edge
    cases.

    Args:
        cp: Centipawn advantage, mover's POV (positive = the side to evaluate
            is better off).

    Returns:
        Win probability in ``[0.0, 1.0]``.
    """
    p = 1.0 / (1.0 + _math.exp(-0.00368208 * cp))
    return max(0.0, min(1.0, p))


def win_prob_white(cp_white: int | None, mate_white: int | None) -> float:
    """Return White's win probability from White-POV eval fields.

    Args:
        cp_white: White-POV centipawn eval, or ``None`` if the score is a mate.
        mate_white: Signed moves-to-mate from White's POV (positive = White
            mates, negative = Black mates), or ``None`` if the score is a
            centipawn value.

    Returns:
        Win probability for White in ``[0.0, 1.0]``.  A forced mate for White
        (``mate_white > 0``) returns ~1.0; a forced mate for Black
        (``mate_white < 0``) returns ~0.0.
    """
    if mate_white is not None:
        # Treat any forced mate as a near-certain result.
        return 1.0 if mate_white > 0 else 0.0
    if cp_white is None:
        return 0.5  # Unknown eval — treat as even.
    return win_prob_from_cp(cp_white)


def game_phase(board: chess.Board) -> str:
    """Estimate the game phase from the current board position.

    Heuristic: count total non-pawn material remaining (both sides combined)
    using unit weights (N/B = 3, R = 5, Q = 9).  Pawns are excluded because
    they remain on the board deep into the endgame.  Thresholds are calibrated
    so that typical openings (all pieces present) read as ``'opening'``,
    fully-developed middlegames register as ``'middlegame'``, and positions
    with few major pieces left register as ``'endgame'``.

    Piece-value weights (both sides combined, max = 2*(2*3+2*3+2*5+9) = 78):

    * >= 56 → opening
    * >= 24 → middlegame
    * <  24 → endgame

    Args:
        board: A ``chess.Board`` representing the position to evaluate.

    Returns:
        ``'opening'``, ``'middlegame'``, or ``'endgame'``.
    """
    # Count total non-pawn material using unit weights for each piece type.
    # Pawns are excluded because they remain on the board deep into the endgame.
    _piece_weights = {
        chess.KNIGHT: 3,
        chess.BISHOP: 3,
        chess.ROOK: 5,
        chess.QUEEN: 9,
    }
    total = 0
    for piece_type, weight in _piece_weights.items():
        total += len(board.pieces(piece_type, chess.WHITE)) * weight
        total += len(board.pieces(piece_type, chess.BLACK)) * weight

    if total >= 56:
        return "opening"
    if total >= 24:
        return "middlegame"
    return "endgame"


# Win-probability-drop thresholds for leak classification, tuned for ~800-1100
# strength (positions are often double-edged; a 10% WP swing is meaningful).
# These constants operate on a *different axis* from the cp-loss ``classify()``
# thresholds above — see the design note at the top of this section.
MISTAKE_WP_DROP: float = 0.10   # ≥10% win-prob drop → mistake
BLUNDER_WP_DROP: float = 0.20   # ≥20% win-prob drop → blunder


def leak_severity(win_prob_drop: float) -> str:
    """Map a win-probability drop to a leak severity label.

    Operates on the *win-probability-drop* axis (not centipawn loss), tuned for
    ~800-1100 strength play.  Only mistakes and blunders are surfaced; anything
    below ``MISTAKE_WP_DROP`` returns ``'none'`` (filtering out inaccuracies,
    good moves, and best moves).

    Note: this function intentionally uses a different axis (win% drop) from
    the cp-loss ``classify()`` function. The two systems may disagree on
    borderline moves by design (Refuter resolution #10).

    Args:
        win_prob_drop: The decrease in win probability for the moving side
            (always non-negative; pass ``max(0, before - after)`` if needed).

    Returns:
        ``'none'``, ``'mistake'``, or ``'blunder'``.
    """
    if win_prob_drop >= BLUNDER_WP_DROP:
        return "blunder"
    if win_prob_drop >= MISTAKE_WP_DROP:
        return "mistake"
    return "none"
