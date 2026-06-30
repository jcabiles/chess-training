"""
pgn.py — PGN parsing + import model (pure module).

Parses one or many games from PGN text into structured ``ParsedGame`` dataclasses.
Per-ply data (SAN, UCI, FEN-before, clock) is extracted in ``ParsedPly`` dataclasses.

Dependencies: chess, chess.pgn, stdlib only (re, hashlib, io, logging, os, dataclasses).
No engine, no FastAPI, no storage import.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
from dataclasses import dataclass, field

import chess
import chess.pgn

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# %clk comment regex  (Refuter resolution #9)
# Matches e.g. { [%clk 0:05:23.4] } inside a PGN node comment.
# Groups: (hours, minutes, seconds_with_optional_fraction)
# ---------------------------------------------------------------------------
_CLK_RE = re.compile(
    r"\[%clk\s+(\d+):(\d+):(\d+(?:\.\d+)?)\]"
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ParsedPly:
    """One half-move (ply) extracted from a parsed game.

    Attributes:
        ply:         1-based ply number (1 = White's first move).
        san:         Standard Algebraic Notation for this move (e.g. "e4").
        uci:         UCI move string (e.g. "e2e4").
        fen_before:  FEN of the board BEFORE this move is played.
        clock_centis: Remaining clock time in centiseconds, or None if absent/unparseable.
    """

    ply: int
    san: str
    uci: str
    fen_before: str
    clock_centis: int | None


@dataclass
class ParsedGame:
    """Structured import model for one PGN game.

    Attributes:
        headers:      Raw PGN header dict (e.g. {"White": "Magnus", "Event": "..."}).
        white:        White player name (from headers, empty string if absent).
        black:        Black player name (from headers, empty string if absent).
        result:       PGN result string ("1-0", "0-1", "1/2-1/2", "*").
        eco:          ECO code if available (best-effort from headers or openings module).
        opening:      Opening name if available (best-effort).
        date:         Date string from the PGN Date header.
        ply_count:    Total number of half-moves (plies) in the mainline.
        my_color:     "white", "black", or None (never guesses — see CHESS_USERNAME env).
        content_hash: SHA-1 hash over normalized movetext + key headers (for dedup).
        pgn:          The raw PGN text for this game.
        plies:        Ordered list of ParsedPly, one per mainline half-move.
    """

    headers: dict
    white: str
    black: str
    result: str
    eco: str
    opening: str
    date: str
    ply_count: int
    my_color: str | None
    content_hash: str
    pgn: str
    plies: list[ParsedPly] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_clock_centis(comment: str) -> int | None:
    """Extract centiseconds from a %clk comment string, or return None."""
    m = _CLK_RE.search(comment)
    if m is None:
        return None
    h = int(m.group(1))
    minutes = int(m.group(2))
    s = float(m.group(3))
    total_seconds = h * 3600 + minutes * 60 + s
    return int(round(total_seconds * 100))


def _infer_my_color(
    white: str,
    black: str,
    aliases: list[str],
) -> str | None:
    """Return 'white', 'black', or None.

    Matches case-insensitively against each alias. If aliases is empty or
    neither player matches, returns None (never guesses White).
    """
    if not aliases:
        return None
    white_lower = white.strip().lower()
    black_lower = black.strip().lower()
    for alias in aliases:
        a = alias.strip().lower()
        if not a:
            continue
        if a == white_lower:
            return "white"
        if a == black_lower:
            return "black"
    return None


def _load_aliases() -> list[str]:
    """Read CHESS_USERNAME env var into a list of trimmed alias strings."""
    raw = os.environ.get("CHESS_USERNAME", "")
    if not raw.strip():
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _compute_hash(uci_moves: list[str], headers: dict) -> str:
    """Compute a stable SHA-1 content hash for deduplication.

    The hash is deterministic over the mainline UCI move sequence plus the
    key PGN headers (White, Black, Date, Result, Event). Two different imports
    of the same game will produce the same hash; different games will (with
    overwhelming probability) produce different hashes.
    """
    key_headers = "|".join(
        headers.get(k, "") for k in ("White", "Black", "Date", "Result", "Event")
    )
    movetext = " ".join(uci_moves)
    payload = f"{key_headers}\n{movetext}".encode("utf-8")
    return hashlib.sha1(payload).hexdigest()


def _replay_game(game: chess.pgn.Game) -> list[ParsedPly]:
    """Walk the mainline of a python-chess Game, collecting one ParsedPly per half-move.

    Returns an ordered list; ply numbers are 1-based.
    """
    plies: list[ParsedPly] = []
    board = game.board()  # honours the FEN header if present
    ply_num = 0
    for child in game.mainline():
        ply_num += 1
        fen_before = board.fen()
        move = child.move
        san = board.san(move)
        uci = move.uci()
        clock_centis = _parse_clock_centis(child.comment or "")
        board.push(move)
        plies.append(
            ParsedPly(
                ply=ply_num,
                san=san,
                uci=uci,
                fen_before=fen_before,
                clock_centis=clock_centis,
            )
        )
    return plies


def _game_to_pgn_text(game: chess.pgn.Game) -> str:
    """Render a python-chess Game back to a PGN string."""
    buf = io.StringIO()
    exporter = chess.pgn.FileExporter(buf)
    game.accept(exporter)
    return buf.getvalue().strip()


def _parse_one(game: chess.pgn.Game, aliases: list[str]) -> ParsedGame:
    """Convert a python-chess Game into a ParsedGame."""
    headers = dict(game.headers)
    white = headers.get("White", "")
    black = headers.get("Black", "")
    result = headers.get("Result", "*")
    eco = headers.get("ECO", "")
    opening = headers.get("Opening", "")
    date = headers.get("Date", "")

    # Best-effort ECO/opening from openings module if headers lack them.
    # Import is guarded — if openings data isn't loaded the call returns None safely.
    if not (eco and opening):
        try:
            from app import openings as _openings  # optional reuse

            uci_moves_for_opening = [
                child.move.uci() for child in game.mainline()
            ]
            base_fen = game.board().fen()
            result_dict = _openings.identify(base_fen, uci_moves_for_opening)
            if result_dict:
                if not eco:
                    eco = result_dict.get("eco", "")
                if not opening:
                    opening = result_dict.get("name", "")
        except Exception:
            pass  # best-effort only; never crash

    plies = _replay_game(game)
    uci_moves = [p.uci for p in plies]
    content_hash = _compute_hash(uci_moves, headers)
    my_color = _infer_my_color(white, black, aliases)
    pgn_text = _game_to_pgn_text(game)

    return ParsedGame(
        headers=headers,
        white=white,
        black=black,
        result=result,
        eco=eco,
        opening=opening,
        date=date,
        ply_count=len(plies),
        my_color=my_color,
        content_hash=content_hash,
        pgn=pgn_text,
        plies=plies,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_games(text: str) -> list[ParsedGame]:
    """Parse PGN text containing one or more games.

    Loops ``chess.pgn.read_game`` until it returns None (standard multi-game
    PGN convention). Malformed/unparseable games are skipped with a warning;
    the rest of the batch is always processed.

    Args:
        text: Raw PGN text (may contain one or many games).

    Returns:
        A list of ``ParsedGame`` dataclasses, one per successfully parsed game.
        Empty list if the text contains no valid games.
    """
    aliases = _load_aliases()
    buf = io.StringIO(text)
    results: list[ParsedGame] = []

    while True:
        try:
            game = chess.pgn.read_game(buf)
        except Exception as exc:
            logger.warning("pgn: read_game raised an exception, aborting batch: %s", exc)
            break

        if game is None:
            break  # end of stream

        # python-chess sometimes returns a Game with no moves AND no headers
        # for genuinely malformed input — treat that as a skip rather than a crash.
        if not game.headers and not list(game.mainline()):
            logger.warning("pgn: empty/malformed game encountered, skipping")
            continue

        try:
            parsed = _parse_one(game, aliases)
        except Exception as exc:
            logger.warning("pgn: failed to parse game, skipping: %s", exc)
            continue

        results.append(parsed)

    return results
