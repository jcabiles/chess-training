"""Data-integrity tests for the bundled opening-trap content (data/traps.json).

Replays every trap line with python-chess (the app's canonical position
producer) and asserts the chess is correct: legal moves in order, exactly one
bait ply per variation, a legal refutation + decline continuation, claimed
checkmates that really mate, and claimed material wins that reach the stated
delta. No Stockfish binary is required.

This is Verify-by #1 from docs/ai-dlc/specs/opening-traps.md.
"""

from __future__ import annotations

import json
import os

import chess
import pytest

DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "traps.json",
)

# Allowed enum values.
_COLORS = {"white", "black"}
_SIDES = {"trapper", "victim"}
# "winning-attack" = a sacrifice for a decisive attack where the payoff is NOT a
# forced mate or a fixed material tally at the final ply (e.g. the Fried Liver).
# It can't be proven "winning" without an engine, so its correctness gate is:
# legal replay (like every line) PLUS a mechanical king-exposure signature
# (see test_winning_attack_results) — the trapper line must leave the victim's
# king in check or stripped of castling rights.
_RESULTS = {"checkmate", "wins-piece", "wins-queen", "wins-material", "winning-attack"}

# Simple piece-value table for the material-delta checks (kings excluded).
_PIECE_VALUES = {
    chess.PAWN: 1,
    chess.KNIGHT: 3,
    chess.BISHOP: 3,
    chess.ROOK: 5,
    chess.QUEEN: 9,
}

# Minimum trapper material gain (in pawn-equivalents) for each wins-* result.
# A piece-for-pawn trap (Elephant, Noah's Ark) nets +2, hence wins-material >= 2;
# a clean piece win is >= 3; a queen win is >= 8 (queen for two minors is +3..).
_RESULT_MIN_DELTA = {
    "wins-piece": 3,
    "wins-queen": 8,
    "wins-material": 2,
}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def traps_doc():
    with open(DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def traps(traps_doc):
    assert isinstance(traps_doc, dict), "top level must be an object"
    assert "traps" in traps_doc, "missing 'traps' key"
    items = traps_doc["traps"]
    assert isinstance(items, list), "'traps' must be a list"
    return items


def _trapper_color(trap) -> bool:
    """python-chess color (WHITE/BLACK) of the side that springs the trap."""
    return chess.WHITE if trap["color"] == "white" else chess.BLACK


def _material(board: chess.Board, color: bool) -> int:
    return sum(
        _PIECE_VALUES.get(p.piece_type, 0)
        for p in board.piece_map().values()
        if p.color == color
    )


def _trapper_advantage(board: chess.Board, trapper: bool) -> int:
    return _material(board, trapper) - _material(board, not trapper)


def _start_board(trap) -> chess.Board:
    return chess.Board(trap["startFen"])


def _all_variations(traps):
    """Yield (trap, variation) pairs for parametrization-style iteration."""
    for trap in traps:
        for var in trap["variations"]:
            yield trap, var


# ---------------------------------------------------------------------------
# 7. Schema validation (top-level + per-trap + per-ply)
# ---------------------------------------------------------------------------

_TRAP_REQUIRED = {
    "id",
    "name",
    "color",
    "eco",
    "parentOpening",
    "commonness",
    "startFen",
    "leadInSan",
    "variations",
}
_VARIATION_REQUIRED = {"label", "mainLine"}
_PLY_REQUIRED = {"san", "uci", "side"}
_REFUTATION_REQUIRED = {"san", "uci", "note", "declineSan", "assessment"}


def test_min_trap_count_and_both_colors(traps):
    assert len(traps) >= 10, f"need >= 10 traps, found {len(traps)}"
    colors = {t["color"] for t in traps}
    assert colors == _COLORS, f"both colors must be represented, found {colors}"


def test_unique_kebab_case_ids(traps):
    ids = [t["id"] for t in traps]
    assert len(ids) == len(set(ids)), f"trap ids must be unique: {ids}"
    for tid in ids:
        assert isinstance(tid, str) and tid, f"bad id {tid!r}"
        assert tid == tid.lower(), f"id not lowercase: {tid!r}"
        assert " " not in tid and "_" not in tid, f"id not kebab-case: {tid!r}"


def test_trap_schema(traps):
    for trap in traps:
        missing = _TRAP_REQUIRED - set(trap)
        assert not missing, f"trap {trap.get('id')!r} missing fields {missing}"
        assert trap["color"] in _COLORS, f"bad color in {trap['id']}"
        assert isinstance(trap["commonness"], int), f"commonness not int in {trap['id']}"
        assert 1 <= trap["commonness"] <= 5, f"commonness out of range in {trap['id']}"
        assert isinstance(trap["leadInSan"], list) and trap["leadInSan"], (
            f"leadInSan must be a non-empty list in {trap['id']}"
        )
        assert isinstance(trap["variations"], list) and trap["variations"], (
            f"variations must be a non-empty list in {trap['id']}"
        )
        # optional engineNote, if present, must be a non-empty string
        if "engineNote" in trap:
            assert isinstance(trap["engineNote"], str) and trap["engineNote"].strip()


def test_variation_and_ply_schema(traps):
    for trap, var in _all_variations(traps):
        ctx = f"{trap['id']}::{var.get('label')}"
        missing = _VARIATION_REQUIRED - set(var)
        assert not missing, f"variation {ctx} missing {missing}"
        assert isinstance(var["mainLine"], list) and var["mainLine"], (
            f"mainLine must be a non-empty list in {ctx}"
        )
        for i, ply in enumerate(var["mainLine"]):
            pmissing = _PLY_REQUIRED - set(ply)
            assert not pmissing, f"ply {i} in {ctx} missing {pmissing}"
            assert ply["side"] in _SIDES, f"bad side at ply {i} in {ctx}"
            assert isinstance(ply["san"], str) and ply["san"], f"bad san at {i} in {ctx}"
            assert isinstance(ply["uci"], str) and ply["uci"], f"bad uci at {i} in {ctx}"
            if "result" in ply:
                assert ply["result"] in _RESULTS, (
                    f"bad result {ply['result']!r} at ply {i} in {ctx}"
                )
            for flag in ("dubious", "bait", "forced"):
                if flag in ply:
                    assert isinstance(ply[flag], bool), f"{flag} not bool at {i} in {ctx}"


# ---------------------------------------------------------------------------
# 1. leadInSan replays exactly to startFen (both directions)
# ---------------------------------------------------------------------------


def test_lead_in_reaches_start_fen(traps):
    for trap in traps:
        board = chess.Board()
        for san in trap["leadInSan"]:
            board.push_san(san)
        derived = board.epd()
        declared = chess.Board(trap["startFen"]).epd()
        # both directions: a wrong startFen cannot pass silently
        assert derived == declared, (
            f"{trap['id']}: leadInSan -> {derived!r} != startFen -> {declared!r}"
        )
        assert declared == derived


# ---------------------------------------------------------------------------
# 2. Every mainLine ply is legal and its SAN matches the UCI move
# ---------------------------------------------------------------------------


def test_main_line_moves_legal_and_san_matches(traps):
    for trap, var in _all_variations(traps):
        ctx = f"{trap['id']}::{var['label']}"
        board = _start_board(trap)
        for i, ply in enumerate(var["mainLine"]):
            move = chess.Move.from_uci(ply["uci"])
            assert move in board.legal_moves, (
                f"illegal uci {ply['uci']!r} at ply {i} in {ctx} (fen {board.fen()})"
            )
            # SAN computed from the position must match the declared SAN
            # (normalize: strip check/mate suffixes which authors may include).
            expected = board.san(move)
            assert _norm_san(ply["san"]) == _norm_san(expected), (
                f"san mismatch at ply {i} in {ctx}: "
                f"declared {ply['san']!r} vs board.san {expected!r}"
            )
            board.push(move)


def _norm_san(san: str) -> str:
    """Strip trailing check/mate markers and the promotion '=' so author SAN
    like 'fxg1=N+' compares equal to python-chess 'fxg1=N+' robustly."""
    return san.replace("+", "").replace("#", "").replace("!", "").replace("?", "")


# ---------------------------------------------------------------------------
# 3. Exactly one bait ply per variation; refutation present iff bait
# ---------------------------------------------------------------------------


def test_exactly_one_bait_with_refutation(traps):
    for trap, var in _all_variations(traps):
        ctx = f"{trap['id']}::{var['label']}"
        bait_plies = [p for p in var["mainLine"] if p.get("bait") is True]
        assert len(bait_plies) == 1, (
            f"{ctx}: expected exactly one bait ply, found {len(bait_plies)}"
        )
        for p in var["mainLine"]:
            has_ref = "refutation" in p
            is_bait = p.get("bait") is True
            assert has_ref == is_bait, (
                f"{ctx}: refutation must be present iff bait (san {p['san']!r})"
            )
        ref = bait_plies[0]["refutation"]
        missing = _REFUTATION_REQUIRED - set(ref)
        assert not missing, f"{ctx}: refutation missing {missing}"
        assert isinstance(ref["declineSan"], list) and ref["declineSan"], (
            f"{ctx}: declineSan must be a non-empty list"
        )


# ---------------------------------------------------------------------------
# 4. refutation.uci legal at the bait ply; declineSan continuation legal
#    when replayed from the position AFTER refutation.uci.
# ---------------------------------------------------------------------------


def test_refutation_and_decline_legal(traps):
    for trap, var in _all_variations(traps):
        ctx = f"{trap['id']}::{var['label']}"
        board = _start_board(trap)
        # replay up to (not including) the bait ply
        for ply in var["mainLine"]:
            if ply.get("bait") is True:
                ref = ply["refutation"]
                ref_move = chess.Move.from_uci(ref["uci"])
                assert ref_move in board.legal_moves, (
                    f"{ctx}: refutation uci {ref['uci']!r} illegal "
                    f"(fen {board.fen()})"
                )
                # SAN of the refutation must match too
                assert _norm_san(ref["san"]) == _norm_san(board.san(ref_move)), (
                    f"{ctx}: refutation san mismatch "
                    f"{ref['san']!r} vs {board.san(ref_move)!r}"
                )
                board.push(ref_move)
                # now replay the decline continuation as SAN
                for j, dsan in enumerate(ref["declineSan"]):
                    try:
                        dmove = board.parse_san(dsan)
                    except (chess.IllegalMoveError, chess.InvalidMoveError, ValueError) as exc:
                        pytest.fail(
                            f"{ctx}: declineSan[{j}]={dsan!r} illegal "
                            f"(fen {board.fen()}): {exc}"
                        )
                    board.push(dmove)
                break
            board.push(chess.Move.from_uci(ply["uci"]))
        else:  # pragma: no cover - guarded by test #3
            pytest.fail(f"{ctx}: no bait ply found")


# ---------------------------------------------------------------------------
# 5. result == 'checkmate' really mates after the ply is pushed.
# ---------------------------------------------------------------------------


def test_checkmate_results_are_mate(traps):
    for trap, var in _all_variations(traps):
        ctx = f"{trap['id']}::{var['label']}"
        board = _start_board(trap)
        for i, ply in enumerate(var["mainLine"]):
            board.push(chess.Move.from_uci(ply["uci"]))
            if ply.get("result") == "checkmate":
                assert board.is_checkmate(), (
                    f"{ctx}: ply {i} ({ply['san']!r}) claims checkmate "
                    f"but board is not mate (fen {board.fen()})"
                )


# ---------------------------------------------------------------------------
# 6. wins-* results reach the claimed trapper material advantage.
# ---------------------------------------------------------------------------


def test_material_win_results_reach_delta(traps):
    for trap, var in _all_variations(traps):
        ctx = f"{trap['id']}::{var['label']}"
        trapper = _trapper_color(trap)
        start = _start_board(trap)
        base_adv = _trapper_advantage(start, trapper)

        board = _start_board(trap)
        for i, ply in enumerate(var["mainLine"]):
            board.push(chess.Move.from_uci(ply["uci"]))
            result = ply.get("result")
            if result in _RESULT_MIN_DELTA:
                gained = _trapper_advantage(board, trapper) - base_adv
                need = _RESULT_MIN_DELTA[result]
                assert gained >= need, (
                    f"{ctx}: ply {i} ({ply['san']!r}) claims {result} but trapper "
                    f"material gain is only {gained} (need >= {need}); "
                    f"fen {board.fen()}"
                )


# ---------------------------------------------------------------------------
# 6b. result == 'winning-attack' leaves the victim's king exposed after the ply
#     (in check, or having lost all castling rights). Positional sacs can't be
#     proven "winning" mechanically, so this is the rigor we CAN assert.
# ---------------------------------------------------------------------------


def test_winning_attack_results_expose_king(traps):
    for trap, var in _all_variations(traps):
        ctx = f"{trap['id']}::{var['label']}"
        victim = not _trapper_color(trap)
        board = _start_board(trap)
        for i, ply in enumerate(var["mainLine"]):
            board.push(chess.Move.from_uci(ply["uci"]))
            if ply.get("result") == "winning-attack":
                exposed = board.is_check() or not (
                    board.has_kingside_castling_rights(victim)
                    or board.has_queenside_castling_rights(victim)
                )
                assert exposed, (
                    f"{ctx}: ply {i} ({ply['san']!r}) claims winning-attack but the "
                    f"victim king is neither in check nor stripped of castling "
                    f"(fen {board.fen()})"
                )


# ---------------------------------------------------------------------------
# Consistency: the side of each ply must alternate with the side to move,
# and the bait ply must always be the victim's move.
# ---------------------------------------------------------------------------


def test_ply_side_matches_turn_and_bait_is_victim(traps):
    for trap, var in _all_variations(traps):
        ctx = f"{trap['id']}::{var['label']}"
        trapper = _trapper_color(trap)
        board = _start_board(trap)
        for i, ply in enumerate(var["mainLine"]):
            mover_is_trapper = board.turn == trapper
            declared_is_trapper = ply["side"] == "trapper"
            assert mover_is_trapper == declared_is_trapper, (
                f"{ctx}: ply {i} ({ply['san']!r}) side={ply['side']!r} "
                f"contradicts whose turn it is"
            )
            if ply.get("bait") is True:
                assert ply["side"] == "victim", (
                    f"{ctx}: bait ply {ply['san']!r} must be the victim's move"
                )
            board.push(chess.Move.from_uci(ply["uci"]))
