"""
openings.py — Opening Trainer backend logic (pure, no engine, no network).

Loads the lichess-org/chess-openings TSVs at startup and exposes:
  - load(data_dir=None) -> OpeningsIndex
  - identify(base_fen, uci_moves) -> dict | None
  - candidates(fen, q=None, cap=40) -> dict
  - commentary_lookup(fen, commentary) -> dict | None

EPD identity is produced exclusively by chess.Board(...).epd() (python-chess).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import chess

logger = logging.getLogger(__name__)

# The real data/ dir ships exactly these five files from lichess-org/chess-openings.
# load() scans all *.tsv in the directory so the test fixture also works.
_TSV_NAMES = ("a.tsv", "b.tsv", "c.tsv", "d.tsv", "e.tsv")

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class OpeningsIndex:
    """All opening data derived from the TSVs.

    Attributes:
        name_by_epd:     epd -> (eco, name). Deepest line wins on collision.
        lines:           List of line dicts with keys eco, name, uci, san, epds.
        passes_through:  epd -> set of line indices whose path includes that epd.
    """

    name_by_epd: dict[str, tuple[str, str]] = field(default_factory=dict)
    lines: list[dict] = field(default_factory=list)
    passes_through: dict[str, set[int]] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.lines


# Module-level singleton —initialised to an empty index so imports never raise.
_index: OpeningsIndex = OpeningsIndex()


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _replay_uci(uci_str: str) -> tuple[list[str], list[str], str]:
    """Replay a space-separated UCI string from the starting position.

    Returns:
        (san_list, epd_list, initial_epd) where:
          - san_list and epd_list are parallel to the UCI moves (after each ply).
          - initial_epd is the EPD of the board BEFORE any moves (the position the
            line "starts from"), used to make candidates() work from any mid-game FEN.

    Raises:
        ValueError if any move is illegal.
    """
    board = chess.Board()
    initial_epd = board.epd()
    moves = uci_str.split()
    san_list: list[str] = []
    epd_list: list[str] = []
    for uci in moves:
        move = chess.Move.from_uci(uci)
        san_list.append(board.san(move))
        board.push(move)
        epd_list.append(board.epd())
    return san_list, epd_list, initial_epd


def _parse_tsv(path: Path) -> list[dict]:
    """Parse one TSV file, skipping the header row.

    Handles both 3-column (eco, name, pgn) and 5-column (eco, name, pgn, uci, epd)
    formats. For 3-column, converts PGN notation to UCI.

    Returns a list of raw row dicts with keys: eco, name, pgn, uci, epd.
    Rows with parse errors are skipped with a warning.
    """
    rows: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("openings: cannot read %s: %s", path, exc)
        return rows

    lines = text.splitlines()
    if not lines:
        return rows

    # First line is the header — skip it.
    for lineno, line in enumerate(lines[1:], start=2):
        parts = line.split("\t")
        if len(parts) < 3:
            logger.debug("openings: %s line %d: too few columns, skipping", path.name, lineno)
            continue

        eco = parts[0]
        name = parts[1]
        pgn = parts[2]

        # If 5-column format, use uci + epd directly.
        if len(parts) >= 5:
            uci = parts[3].strip()
            epd = parts[4].strip()
            if not uci:
                continue
            rows.append({"eco": eco, "name": name, "pgn": pgn, "uci": uci, "epd": epd})
        else:
            # 3-column format (lichess): parse PGN to UCI.
            try:
                board = chess.Board()
                uci_moves: list[str] = []
                for part in pgn.strip().split():
                    # Skip move numbers (e.g. "1.", "2.")
                    if part[-1] in ".":
                        continue
                    # Parse SAN move and convert to UCI
                    move = board.push_san(part)
                    uci_moves.append(move.uci())
                if uci_moves:
                    uci = " ".join(uci_moves)
                    rows.append({"eco": eco, "name": name, "pgn": pgn, "uci": uci, "epd": None})
            except Exception as exc:
                logger.debug("openings: %s line %d (PGN '%s'): %s, skipping", path.name, lineno, pgn, exc)
                continue

    return rows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load(data_dir: Optional[str] = None) -> OpeningsIndex:
    """Parse the 5 TSVs from *data_dir* and populate the module-level index.

    Args:
        data_dir: Path to the directory containing a.tsv … e.tsv.  Defaults to
            the ``OPENINGS_DATA_DIR`` environment variable, then ``data/openings/``
            relative to the project root.

    Returns:
        The populated (or empty) OpeningsIndex.

    This function is import-safe: if the directory is missing or empty, it
    logs one warning and returns an empty index without raising.
    """
    global _index

    if data_dir is None:
        data_dir = os.environ.get("OPENINGS_DATA_DIR", "data/openings")

    dir_path = Path(data_dir)
    if not dir_path.is_dir():
        logger.warning(
            "openings: data directory '%s' not found — opening features disabled",
            dir_path,
        )
        _index = OpeningsIndex()
        return _index

    # Collect rows from all TSVs in the directory, sorted for determinism.
    # In production only a.tsv … e.tsv exist; scanning all *.tsv also lets
    # unit tests point load() at a fixture directory with a differently-named file.
    tsv_files = sorted(dir_path.glob("*.tsv"))
    raw_rows: list[dict] = []
    for tsv_path in tsv_files:
        raw_rows.extend(_parse_tsv(tsv_path))

    if not raw_rows:
        logger.warning(
            "openings: no TSV data found in '%s' — opening features disabled",
            dir_path,
        )
        _index = OpeningsIndex()
        return _index

    # Build the three structures.
    name_by_epd: dict[str, tuple[str, str]] = {}
    lines: list[dict] = []
    passes_through: dict[str, set[int]] = {}

    for row in raw_rows:
        try:
            san, epds, initial_epd = _replay_uci(row["uci"])
        except Exception as exc:
            logger.debug("openings: skipping row '%s': %s", row["name"], exc)
            continue

        line_id = len(lines)
        line = {
            "eco": row["eco"],
            "name": row["name"],
            "uci": row["uci"],
            "san": san,
            "epds": epds,
        }
        lines.append(line)

        # passes_through: the initial position PLUS every intermediate/final EPD.
        # We also store initial_epd on the line so candidates() can use it as a
        # non-final node (the line passes through the starting position before ply 1).
        passes_through.setdefault(initial_epd, set()).add(line_id)
        for epd in epds:
            passes_through.setdefault(epd, set()).add(line_id)

        # Store initial_epd on each line for the non-final check in candidates().
        line["initial_epd"] = initial_epd

    # Second pass to resolve deepest-wins for name_by_epd correctly.
    # We need to track the depth at which each EPD was last stored.
    name_by_epd = {}
    depth_by_epd: dict[str, int] = {}
    for line in lines:
        for depth, epd in enumerate(line["epds"], start=1):
            if depth > depth_by_epd.get(epd, 0):
                depth_by_epd[epd] = depth
                name_by_epd[epd] = (line["eco"], line["name"])

    _index = OpeningsIndex(
        name_by_epd=name_by_epd,
        lines=lines,
        passes_through=passes_through,
    )
    logger.info("openings: loaded %d lines from '%s'", len(lines), dir_path)
    return _index


def init(data_dir: Optional[str] = None) -> None:
    """Convenience wrapper — call load() at app startup."""
    load(data_dir)


def identify(base_fen: str, uci_moves: list[str]) -> Optional[dict]:
    """Return the deepest named opening on the played line, or None.

    The server replays *base_fen* + *uci_moves* itself and checks each
    intermediate EPD against ``name_by_epd``.  The deepest match wins so
    the name doesn't flicker to null on unnamed in-between positions.

    Args:
        base_fen:  Starting FEN (e.g. the standard starting position).
        uci_moves: List of UCI move strings, e.g. ["e2e4", "e7e5"].

    Returns:
        ``{"eco": ..., "name": ...}`` or ``None`` if no match found.
    """
    if _index.empty:
        return None

    try:
        board = chess.Board(base_fen)
    except Exception:
        return None

    best: Optional[dict] = None
    for uci in uci_moves:
        try:
            board.push_uci(uci)
        except Exception:
            break
        epd = board.epd()
        match = _index.name_by_epd.get(epd)
        if match is not None:
            best = {"eco": match[0], "name": match[1]}

    return best


def candidates(
    fen: str,
    q: Optional[str] = None,
    cap: int = 40,
) -> dict:
    """Return named opening lines reachable from *fen* as continuations.

    Args:
        fen: Full FEN of the current board position.
        q:   Optional case-insensitive substring filter on opening name.
        cap: Maximum number of items to return (default 40).

    Returns:
        ``{"items": [...], "truncated": bool}`` where each item has keys
        ``eco``, ``name``, ``uci``, ``san``.

    Deduplication: when multiple lines share the same name, only the shortest
    (fewest plies) is kept.  Items are ordered by line length ascending, then
    name alphabetically.
    """
    if _index.empty:
        return {"items": [], "truncated": False}

    try:
        board = chess.Board(fen)
        pos_epd = board.epd()
    except Exception:
        return {"items": [], "truncated": False}

    candidate_ids = _index.passes_through.get(pos_epd, set())

    # Filter to non-final nodes only (line extends beyond current position).
    # A line qualifies if pos_epd is:
    #   (a) the line's initial position (before any moves), OR
    #   (b) an intermediate EPD (not the final EPD) in the line's epds list.
    valid: list[dict] = []
    for line_id in candidate_ids:
        line = _index.lines[line_id]
        epds = line["epds"]
        initial_epd = line.get("initial_epd", "")
        if pos_epd == initial_epd:
            # Querying from the position the line starts — entire line is a continuation.
            valid.append(line)
        elif pos_epd in epds[:-1]:
            # Querying from a mid-line position — line extends further.
            valid.append(line)

    # Optional name filter.
    if q:
        q_lower = q.lower()
        valid = [ln for ln in valid if q_lower in ln["name"].lower()]

    # Dedupe by name — keep shortest line per name.
    by_name: dict[str, dict] = {}
    for ln in valid:
        n_plies = len(ln["epds"])
        existing = by_name.get(ln["name"])
        if existing is None or n_plies < len(existing["epds"]):
            by_name[ln["name"]] = ln

    # Sort: ascending plies, then name alpha.
    deduped = sorted(by_name.values(), key=lambda ln: (len(ln["epds"]), ln["name"]))

    truncated = len(deduped) > cap
    items = deduped[:cap]

    return {
        "items": [
            {"eco": ln["eco"], "name": ln["name"], "uci": ln["uci"], "san": ln["san"]}
            for ln in items
        ],
        "truncated": truncated,
    }


def commentary_lookup(fen: str, commentary: dict) -> Optional[dict]:
    """Look up commentary for a position by converting *fen* to an EPD.

    The commentary dict is caller-provided (injectable for testing) so this
    module never loads the file itself.

    Args:
        fen:         Full FEN of the position after the move.
        commentary:  Dict mapping EPD strings to ``{"san": ..., "text": ...}``.

    Returns:
        The matching ``{"san": ..., "text": ...}`` entry or ``None``.
    """
    try:
        epd = chess.Board(fen).epd()
    except Exception:
        return None
    return commentary.get(epd)
