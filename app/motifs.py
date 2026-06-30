"""Pure tactical motif detection (python-chess only, no engine, no I/O).

All functions are deterministic and unit-testable without a Stockfish binary.
Style is modelled after the lichess-puzzler ``tagger/cook.py`` approach:
rule-based, board-introspection only, no search.

SEE limitation
--------------
The ``see()`` implementation uses the Least-Valuable-Attacker (LVA) recapture
loop.  It does **not** model x-ray attackers (e.g. a rook that becomes
available behind another rook only after the first one captures).
``board.attackers_mask`` is called with the real board occupancy; pieces
logically removed via the ``occupied`` tracking mask are filtered out by an
``& occupied`` intersection, but pieces *behind* removed pieces are not
revealed.  This is a standard approximation; it is accurate for the vast
majority of practical captures and is sufficient for the motif-detection use
case here.
"""

from __future__ import annotations

import chess

# ---------------------------------------------------------------------------
# Piece values (centipawns, standard values)
# ---------------------------------------------------------------------------

PIECE_VALUES: dict[int, int] = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 20000,
}


# ---------------------------------------------------------------------------
# SEE — Static Exchange Evaluation
# ---------------------------------------------------------------------------


def see(board: chess.Board, move: chess.Move) -> int:
    """Static Exchange Evaluation for *move*.

    Returns the net material (in centipawns) won by the moving side from the
    full capture sequence on *move*'s target square, using Least-Valuable-
    Attacker (LVA) recapture resolution and negamax backward pass.

    * Positive → the sequence is winning for the mover (good capture).
    * Negative → the sequence is losing (the capture costs more than it gains).
    * Zero     → an equal trade.

    If *move* is not a capture (and not en passant), returns 0.

    Limitation: x-ray attackers are NOT modelled — see module docstring.

    Args:
        board: The current position.  Not mutated.
        move: The move to evaluate.

    Returns:
        Net centipawn gain for the initial capturer.
    """
    target = move.to_square
    frm = move.from_square

    victim = board.piece_at(target)
    if victim is None:
        # En passant: the captured pawn is not on the target square.
        if board.is_en_passant(move):
            return PIECE_VALUES[chess.PAWN]
        return 0

    mover_piece = board.piece_at(frm)
    if mover_piece is None:
        return 0

    # pile[i] = material value gained by the i-th capturer
    # (the value of the piece sitting on the target square when they capture).
    pile: list[int] = [PIECE_VALUES[victim.piece_type]]

    # Remove the initial attacker from its source AND remove the victim from
    # the target, so that (a) the attacker is not seen as its own recapturer
    # and (b) any piece behind the victim along a sliding ray can be found.
    occupied: chess.Bitboard = (
        board.occupied ^ chess.BB_SQUARES[frm] ^ chess.BB_SQUARES[target]
    )

    # Value of the piece now conceptually "on" the target square.
    last_piece_val: int = PIECE_VALUES[mover_piece.piece_type]
    stm: chess.Color = not board.turn  # opponent recaptures first

    while True:
        best_val: int | None = None
        best_sq: chess.Square | None = None
        # Least-valuable-attacker scan.
        for piece_type in (
            chess.PAWN,
            chess.KNIGHT,
            chess.BISHOP,
            chess.ROOK,
            chess.QUEEN,
            chess.KING,
        ):
            # board.attackers_mask uses real occupancy but we filter by our
            # running `occupied` mask to exclude already-removed pieces.
            attacker_mask = (
                board.attackers_mask(stm, target)
                & board.pieces_mask(piece_type, stm)
                & occupied
            )
            if attacker_mask:
                best_sq = chess.lsb(attacker_mask)
                best_val = PIECE_VALUES[piece_type]
                break

        if best_val is None or best_sq is None:
            break  # no more recaptures

        # Append what this recapturer gains (the piece currently on the square).
        pile.append(last_piece_val)
        # Remove the recapturer from occupied (it "moves" to the target).
        occupied ^= chess.BB_SQUARES[best_sq]
        last_piece_val = best_val
        stm = not stm

    # Negamax backward pass: each side (from last back to first after the
    # initial capture) may choose NOT to capture if it would be unprofitable.
    n = len(pile)
    if n == 1:
        return pile[0]  # no recaptures: straightforward gain

    result = pile[-1]
    for i in range(n - 2, 0, -1):
        # Side i can capture (net = pile[i] - result) or stop (net = 0).
        result = max(0, pile[i] - result)

    # The initial capturer committed to the capture; net = their gain - opponent's best.
    return pile[0] - result


# ---------------------------------------------------------------------------
# Attacker / defender counts
# ---------------------------------------------------------------------------


def attackers_count(board: chess.Board, color: chess.Color, square: chess.Square) -> int:
    """Return the number of pieces of *color* attacking *square*.

    Args:
        board: Current position.
        color: The attacking side.
        square: The square being attacked.

    Returns:
        Integer count of attackers.
    """
    return len(board.attackers(color, square))


def defenders_count(board: chess.Board, color: chess.Color, square: chess.Square) -> int:
    """Return the number of pieces of *color* defending *square*.

    "Defending" is defined as attacking the square (the conventional SEE
    definition — how many of your pieces can recapture if a piece on this
    square is taken).

    Args:
        board: Current position.
        color: The defending side (the piece owner's color).
        square: The square being defended.

    Returns:
        Integer count of defenders.
    """
    return len(board.attackers(color, square))


# ---------------------------------------------------------------------------
# Hanging pieces
# ---------------------------------------------------------------------------


def hanging_pieces(board: chess.Board, color: chess.Color) -> list[dict]:
    """Return *color*'s pieces that are hanging (attacked and SEE-losing).

    A piece is hanging if it is attacked by the opponent **and** the SEE on
    that square from the opponent's perspective is positive (i.e. the opponent
    gains material by capturing).

    Args:
        board: Current position.
        color: The side whose hanging pieces we want to find.

    Returns:
        List of dicts, each with:

        * ``square`` (int): Square index of the hanging piece.
        * ``piece`` (str): Piece symbol (e.g. ``'N'`` for a White knight).
        * ``see`` (int): SEE result from the **owner's** POV (negative = losing).

        All values are JSON-serializable.
    """
    opponent = not color
    result: list[dict] = []

    for square in chess.SQUARES:
        piece = board.piece_at(square)
        if piece is None or piece.color != color:
            continue
        if not board.is_attacked_by(opponent, square):
            continue
        # Find the best capture for the opponent (least-valuable attacker).
        best_see: int | None = None
        for atk_sq in board.attackers(opponent, square):
            capture_move = chess.Move(atk_sq, square)
            s = see(board, capture_move)
            if best_see is None or s > best_see:
                best_see = s
        if best_see is not None and best_see > 0:
            # Opponent gains material → piece is hanging.
            result.append(
                {
                    "square": square,
                    "piece": piece.symbol(),
                    "see": -best_see,  # owner's POV: negative = losing
                }
            )

    return result


# ---------------------------------------------------------------------------
# Motif detectors
# ---------------------------------------------------------------------------


def detect_motifs(
    board: chess.Board,
    move: chess.Move | None = None,
) -> list[dict]:
    """Detect tactical motifs present in *board* (after *move*, if given).

    Each returned dict is JSON-serializable and has at minimum:

    * ``type`` (str): One of ``'hanging'``, ``'fork'``, ``'knight_fork'``,
      ``'pin'``, ``'skewer'``, ``'discovered'``, ``'back_rank'``.
    * ``by`` (str | None): The square (algebraic) of the piece creating the
      motif, or ``None`` if not applicable.
    * ``targets`` (list[str]): Squares involved as targets (algebraic names).
    * ``detail`` (str): Human-readable one-liner.

    Motifs are detected from **side-to-move**'s perspective: hanging/fork/
    skewer/discovered/back_rank identify threats the side-to-move can exploit;
    pin identifies opponent pieces that are pinned.

    When *move* is supplied the board should be the position **before** the
    move; motifs are detected on the position resulting from the move.

    Args:
        board: The position to analyse.  If *move* is given, a copy is used
            internally (the original is not mutated).
        move: Optional move that just occurred.

    Returns:
        List of motif dicts (may be empty).
    """
    if move is not None:
        board = board.copy()
        board.push(move)

    motifs: list[dict] = []
    side = board.turn       # side to move — exploiting threats
    opponent = not side

    # -----------------------------------------------------------------------
    # 1. Hanging — opponent pieces that are SEE-losing
    # -----------------------------------------------------------------------
    for h in hanging_pieces(board, opponent):
        sq = h["square"]
        motifs.append(
            {
                "type": "hanging",
                "by": None,
                "targets": [chess.square_name(sq)],
                "detail": (
                    f"{h['piece']} on {chess.square_name(sq)} is hanging "
                    f"(SEE={h['see']})"
                ),
            }
        )

    # -----------------------------------------------------------------------
    # 2. Forks — one piece attacks two or more valuable opponent pieces
    # -----------------------------------------------------------------------
    # Targets worth forking: king, queen, rook (high-value), or any piece
    # where our attacker has a SEE > 0 advantage.
    _high_value = {chess.KING, chess.QUEEN, chess.ROOK}

    for sq in chess.SQUARES:
        piece = board.piece_at(sq)
        if piece is None or piece.color != side:
            continue
        attacks_bb = board.attacks(sq)

        valuable_targets: list[chess.Square] = []
        see_winning_targets: list[chess.Square] = []

        for target_sq in chess.SquareSet(attacks_bb):
            tp = board.piece_at(target_sq)
            if tp is None or tp.color != opponent:
                continue
            if tp.piece_type in _high_value:
                valuable_targets.append(target_sq)
            elif see(board, chess.Move(sq, target_sq)) > 0:
                see_winning_targets.append(target_sq)

        all_targets = valuable_targets + [
            t for t in see_winning_targets if t not in valuable_targets
        ]
        if len(all_targets) >= 2:
            targets_str = [chess.square_name(t) for t in all_targets]
            motif_type = (
                "knight_fork" if piece.piece_type == chess.KNIGHT else "fork"
            )
            motifs.append(
                {
                    "type": motif_type,
                    "by": chess.square_name(sq),
                    "targets": targets_str,
                    "detail": (
                        f"{'Knight' if piece.piece_type == chess.KNIGHT else piece.symbol()}"
                        f" on {chess.square_name(sq)} forks "
                        f"{', '.join(targets_str)}"
                    ),
                }
            )

    # -----------------------------------------------------------------------
    # 3. Pins — opponent pieces pinned against their king (or a more valuable piece)
    # -----------------------------------------------------------------------
    for sq in chess.SQUARES:
        piece = board.piece_at(sq)
        if piece is None or piece.color != opponent:  # only opponent's pieces can be pinned
            continue
        if board.is_pinned(opponent, sq):  # pinned against opponent's own king
            # Find the pinner: a sliding piece of *side* that lies on the pin ray.
            pin_ray = board.pin(opponent, sq)
            pinner_sq: chess.Square | None = None
            for psq in chess.SquareSet(pin_ray):
                pp = board.piece_at(psq)
                if pp is not None and pp.color == side and psq != sq:
                    pinner_sq = psq
                    break
            motifs.append(
                {
                    "type": "pin",
                    "by": chess.square_name(pinner_sq) if pinner_sq is not None else None,
                    "targets": [chess.square_name(sq)],
                    "detail": (
                        f"{piece.symbol()} on {chess.square_name(sq)} is pinned"
                        + (
                            f" by {chess.square_name(pinner_sq)}"
                            if pinner_sq is not None
                            else ""
                        )
                    ),
                }
            )

    # -----------------------------------------------------------------------
    # 4. Skewers — attack a high-value piece; a lesser piece hides behind it
    # -----------------------------------------------------------------------
    # A skewer requires a sliding piece attacking a high-value opponent piece,
    # with a lesser opponent piece on the same ray behind it.
    _skewer_high = {chess.KING, chess.QUEEN}

    for sq in chess.SQUARES:
        piece = board.piece_at(sq)
        if piece is None or piece.color != side:
            continue
        if piece.piece_type not in (chess.BISHOP, chess.ROOK, chess.QUEEN):
            continue  # only sliders can skewer

        for front_sq in chess.SquareSet(board.attacks(sq)):
            front_piece = board.piece_at(front_sq)
            if front_piece is None or front_piece.color != opponent:
                continue
            if front_piece.piece_type not in _skewer_high:
                continue  # front piece must be high-value

            # Look for a lesser opponent piece on the same ray past front_sq.
            for behind_sq in chess.SQUARES:
                if behind_sq == sq or behind_sq == front_sq:
                    continue
                behind_piece = board.piece_at(behind_sq)
                if behind_piece is None or behind_piece.color != opponent:
                    continue
                if PIECE_VALUES.get(behind_piece.piece_type, 0) >= PIECE_VALUES.get(
                    front_piece.piece_type, 0
                ):
                    continue  # not a skewer if behind piece is equal or more valuable

                # Must be collinear on the same ray: sq→front_sq→behind_sq.
                # Also: no pieces between front_sq and behind_sq (so the skewer
                # is real — after the front piece moves, the attacker hits behind).
                if (
                    _are_collinear(sq, front_sq, behind_sq)
                    and _same_direction(sq, front_sq, behind_sq)
                    and not (chess.between(front_sq, behind_sq) & board.occupied)
                ):
                    motifs.append(
                        {
                            "type": "skewer",
                            "by": chess.square_name(sq),
                            "targets": [
                                chess.square_name(front_sq),
                                chess.square_name(behind_sq),
                            ],
                            "detail": (
                                f"{piece.symbol()} on {chess.square_name(sq)} skewers "
                                f"{front_piece.symbol()} on {chess.square_name(front_sq)}, "
                                f"winning {behind_piece.symbol()} on {chess.square_name(behind_sq)}"
                            ),
                        }
                    )

    # -----------------------------------------------------------------------
    # 5. Discovered attacks — moving a blocking piece reveals a sliding attack
    # -----------------------------------------------------------------------
    # Static heuristic: for each of *side*'s pieces that blocks a sliding
    # piece of the same side from attacking the opponent king or queen, flag it.
    _discovered_valuable = {chess.KING, chess.QUEEN, chess.ROOK}

    seen_discovered: set[tuple[chess.Square, chess.Square]] = set()

    for slider_sq in chess.SQUARES:
        slider = board.piece_at(slider_sq)
        if slider is None or slider.color != side:
            continue
        if slider.piece_type not in (chess.BISHOP, chess.ROOK, chess.QUEEN):
            continue

        for blocker_sq in chess.SquareSet(board.attacks(slider_sq)):
            blocker = board.piece_at(blocker_sq)
            if blocker is None or blocker.color != side:
                continue  # must be own piece blocking

            # Check what the slider would attack if the blocker moved away.
            # Build a temporary occupied mask without the blocker.
            occ_without = board.occupied ^ chess.BB_SQUARES[blocker_sq]
            # Recalculate slider attacks with the blocker removed.
            # Use the *_MASKS tables (which exclude edge squares) as the
            # correct occupancy mask for each attack-table lookup.
            if slider.piece_type == chess.ROOK:
                new_attacks = chess.SquareSet(
                    chess.BB_RANK_ATTACKS[slider_sq].get(
                        occ_without & chess.BB_RANK_MASKS[slider_sq], 0
                    )
                    | chess.BB_FILE_ATTACKS[slider_sq].get(
                        occ_without & chess.BB_FILE_MASKS[slider_sq], 0
                    )
                )
            elif slider.piece_type == chess.BISHOP:
                # BB_DIAG_ATTACKS + BB_DIAG_MASKS covers both diagonals.
                new_attacks = chess.SquareSet(
                    chess.BB_DIAG_ATTACKS[slider_sq].get(
                        occ_without & chess.BB_DIAG_MASKS[slider_sq], 0
                    )
                )
            else:  # queen = rook + bishop
                new_attacks = chess.SquareSet(
                    chess.BB_RANK_ATTACKS[slider_sq].get(
                        occ_without & chess.BB_RANK_MASKS[slider_sq], 0
                    )
                    | chess.BB_FILE_ATTACKS[slider_sq].get(
                        occ_without & chess.BB_FILE_MASKS[slider_sq], 0
                    )
                    | chess.BB_DIAG_ATTACKS[slider_sq].get(
                        occ_without & chess.BB_DIAG_MASKS[slider_sq], 0
                    )
                )

            # Filter to squares previously blocked (not attacked before blocker move).
            old_attacks = board.attacks(slider_sq)
            newly_exposed = new_attacks - old_attacks

            for exposed_sq in newly_exposed:
                ep = board.piece_at(exposed_sq)
                if ep is None or ep.color != opponent:
                    continue
                if ep.piece_type not in _discovered_valuable:
                    continue
                key = (slider_sq, exposed_sq)
                if key in seen_discovered:
                    continue
                seen_discovered.add(key)
                motifs.append(
                    {
                        "type": "discovered",
                        "by": chess.square_name(slider_sq),
                        "targets": [chess.square_name(exposed_sq)],
                        "detail": (
                            f"Moving {blocker.symbol()} from "
                            f"{chess.square_name(blocker_sq)} reveals "
                            f"{slider.symbol()} attack on "
                            f"{chess.square_name(exposed_sq)}"
                        ),
                    }
                )

    # -----------------------------------------------------------------------
    # 6. Back-rank threats
    # -----------------------------------------------------------------------
    # The opponent's king is on its back rank, its escape forward is blocked by
    # own pawns/pieces, and *side* has a rook or queen that can reach the back
    # rank in one move (giving check or delivering mate).
    #
    # Detection heuristic:
    #   (a) King is on its back rank.
    #   (b) King has no forward escape: all squares one rank ahead of the king
    #       (the 2nd/7th rank side) are occupied by the king's own pieces
    #       OR covered by the attacking side.  This captures the "pawns on
    #       the 7th-rank wall" pattern without requiring a strictly zero escape
    #       count (diagonal f8/h8 escapes often exist but are dominated by the
    #       rook once it lands on the 8th rank).
    #   (c) *side* has a rook or queen that can reach ANY square on the
    #       opponent's back rank in one legal-ish move (attacks the back rank).
    back_rank_bb = chess.BB_RANK_1 if opponent == chess.WHITE else chess.BB_RANK_8

    king_sq_bb = board.pieces(chess.KING, opponent)
    if king_sq_bb & back_rank_bb:
        king_sq = chess.lsb(int(king_sq_bb))

        # (b) Are all squares directly in front of the king (on shield rank)
        #     blocked or covered?  We look at the three squares on the shield
        #     rank adjacent to the king's file.
        king_file = chess.square_file(king_sq)
        shield_files = [f for f in (king_file - 1, king_file, king_file + 1) if 0 <= f <= 7]
        shield_rank = 1 if opponent == chess.WHITE else 6  # 0-indexed rank
        shield_squares = [chess.square(f, shield_rank) for f in shield_files]

        forward_blocked = all(
            (board.piece_at(ssq) is not None and board.piece_at(ssq).color == opponent)  # type: ignore[union-attr]
            or board.is_attacked_by(side, ssq)
            for ssq in shield_squares
        )

        # (c) *side* has a rook or queen that can reach the back rank.
        has_back_rank_attacker = any(
            board.attacks_mask(bsq) & back_rank_bb
            for bsq in chess.SquareSet(
                board.pieces(chess.ROOK, side) | board.pieces(chess.QUEEN, side)
            )
        )

        if forward_blocked and has_back_rank_attacker:
            motifs.append(
                {
                    "type": "back_rank",
                    "by": None,
                    "targets": [chess.square_name(king_sq)],
                    "detail": (
                        f"Back-rank threat: opponent king on "
                        f"{chess.square_name(king_sq)} is trapped on its back rank"
                    ),
                }
            )

    return motifs


# ---------------------------------------------------------------------------
# Geometry helpers (internal)
# ---------------------------------------------------------------------------


def _are_collinear(a: chess.Square, b: chess.Square, c: chess.Square) -> bool:
    """Return True if squares *a*, *b*, *c* lie on the same rank, file, or diagonal."""
    ar, af = chess.square_rank(a), chess.square_file(a)
    br, bf = chess.square_rank(b), chess.square_file(b)
    cr, cf = chess.square_rank(c), chess.square_file(c)
    if ar == br == cr:
        return True  # same rank
    if af == bf == cf:
        return True  # same file
    # Same diagonal: |Δrank| == |Δfile| for all pairs AND same slope.
    if abs(ar - br) == abs(af - bf) and abs(ar - cr) == abs(af - cf):
        dr1, df1 = br - ar, bf - af
        dr2, df2 = cr - ar, cf - af
        if dr1 * df2 == dr2 * df1:  # same direction vector (not anti-parallel)
            return True
    return False


def _same_direction(a: chess.Square, b: chess.Square, c: chess.Square) -> bool:
    """Return True if *c* is on the same ray FROM *a* THROUGH *b* (b is between a and c,
    or c is further along the ray past b)."""
    ar, af = chess.square_rank(a), chess.square_file(a)
    br, bf = chess.square_rank(b), chess.square_file(b)
    cr, cf = chess.square_rank(c), chess.square_file(c)
    # Direction a→b
    dr_ab = br - ar
    df_ab = bf - af
    # Direction a→c
    dr_ac = cr - ar
    df_ac = cf - af
    # c must be in the same direction and further than b.
    if dr_ab == 0 and df_ab == 0:
        return False
    # Same direction: vectors must be parallel and c must be further.
    if dr_ab != 0:
        if dr_ac == 0 or (dr_ac > 0) != (dr_ab > 0):
            return False
        t = dr_ac / dr_ab
    else:
        if df_ac == 0 or (df_ac > 0) != (df_ab > 0):
            return False
        t = df_ac / df_ab
    return t > 1.0  # c is past b along the ray
