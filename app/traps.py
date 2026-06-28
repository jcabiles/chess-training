"""
traps.py — Opening Traps backend logic (pure, no engine, no network).

Loads data/traps.json at startup and exposes:
  - load(path=None) -> None
  - init(path=None) -> None  (convenience wrapper for lifespan wiring)
  - traps_by_id: dict[str, dict]
  - summaries() -> list[dict]
  - start_epd_index: dict[str, list[str]]
  - get(trap_id) -> dict | None
  - available(base_fen, uci_moves) -> list[dict]

EPD identity is produced exclusively by chess.Board(...).epd() (python-chess).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import chess

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singletons — initialised to empty so imports never raise.
# ---------------------------------------------------------------------------

traps_by_id: dict[str, dict] = {}
start_epd_index: dict[str, list[str]] = {}

# Ordered summaries cache — rebuilt on each load().
_summaries: list[dict] = []

# Required trap-level fields.
_TRAP_REQUIRED = {"id", "name", "color", "eco", "parentOpening", "commonness",
                  "startFen", "leadInSan", "variations"}

# Valid values.
_VALID_COLORS = {"white", "black"}

# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _replay_san_list(san_list: list[str], from_board: Optional[chess.Board] = None) -> chess.Board:
    """Replay a list of SAN moves from *from_board* (or starting position).

    Returns the board after all moves.
    Raises ValueError on any illegal move.
    """
    board = from_board if from_board is not None else chess.Board()
    for san in san_list:
        board.push_san(san)
    return board


def _trap_start_epd(lead_in_san: list[str]) -> str:
    """Derive the EPD after replaying *lead_in_san* from the starting position.

    Raises ValueError if any SAN move is illegal.
    """
    board = _replay_san_list(lead_in_san)
    return board.epd()


def _validate_trap(trap: dict) -> Optional[str]:
    """Return an error message if *trap* is malformed, else None.

    Validates:
    - All required fields present.
    - `color` is "white" or "black".
    - `commonness` is an int in 1–5.
    - `variations` is a non-empty list.
    - `leadInSan` can be replayed from the starting position without error.
    """
    missing = _TRAP_REQUIRED - set(trap.keys())
    if missing:
        return f"missing required fields: {missing}"

    if trap["color"] not in _VALID_COLORS:
        return f"invalid color: {trap['color']!r}"

    commonness = trap["commonness"]
    if not isinstance(commonness, int) or not (1 <= commonness <= 5):
        return f"commonness must be int 1–5, got {commonness!r}"

    variations = trap.get("variations")
    if not variations or not isinstance(variations, list):
        return "variations must be a non-empty list"

    # Attempt to replay leadInSan — this validates the lead-in move sequence.
    lead_in = trap.get("leadInSan", [])
    if not isinstance(lead_in, list):
        return "leadInSan must be a list"
    try:
        _trap_start_epd(lead_in)
    except Exception as exc:
        return f"leadInSan replay failed: {exc}"

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load(path: Optional[str] = None) -> None:
    """Parse *path* (a JSON file) and populate the module-level singletons.

    Args:
        path: Path to traps.json. Defaults to the ``TRAPS_FILE`` environment
            variable, then ``data/traps.json`` relative to the working directory.

    This function is import-safe: if the file is missing, unreadable, or
    invalid JSON, it logs one warning and resets to empty structures without
    raising.  Individual malformed traps are dropped with a warning; the rest
    still load.
    """
    global traps_by_id, start_epd_index, _summaries

    if path is None:
        path = os.environ.get("TRAPS_FILE", "data/traps.json")

    file_path = Path(path)

    # Reset to empty first so a failed reload leaves a clean state.
    traps_by_id = {}
    start_epd_index = {}
    _summaries = []

    if not file_path.exists():
        logger.warning(
            "traps: file '%s' not found — traps features disabled",
            file_path,
        )
        return

    try:
        import json
        raw = file_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as exc:
        logger.warning(
            "traps: cannot read/parse '%s': %s — traps features disabled",
            file_path,
            exc,
        )
        return

    if not isinstance(data, dict) or "traps" not in data:
        logger.warning(
            "traps: '%s' has no top-level 'traps' key — traps features disabled",
            file_path,
        )
        return

    raw_traps = data["traps"]
    if not isinstance(raw_traps, list):
        logger.warning(
            "traps: 'traps' key in '%s' is not a list — traps features disabled",
            file_path,
        )
        return

    new_traps_by_id: dict[str, dict] = {}
    new_epd_index: dict[str, list[str]] = {}
    new_summaries: list[dict] = []

    for trap in raw_traps:
        if not isinstance(trap, dict):
            logger.warning("traps: skipping non-dict entry")
            continue

        trap_id = trap.get("id", "<unknown>")
        error = _validate_trap(trap)
        if error:
            logger.warning("traps: dropping trap %r: %s", trap_id, error)
            continue

        # Derive start EPD from leadInSan (not startFen — see spec).
        try:
            epd = _trap_start_epd(trap["leadInSan"])
        except Exception as exc:
            logger.warning(
                "traps: dropping trap %r: leadInSan replay error: %s",
                trap_id,
                exc,
            )
            continue

        new_traps_by_id[trap["id"]] = trap
        new_epd_index.setdefault(epd, []).append(trap["id"])
        new_summaries.append({
            "id": trap["id"],
            "name": trap["name"],
            "color": trap["color"],
            "eco": trap["eco"],
            "parentOpening": trap["parentOpening"],
            "commonness": trap["commonness"],
        })

    # Sort summaries: commonness DESC, then name ASC.
    new_summaries.sort(key=lambda s: (-s["commonness"], s["name"]))

    traps_by_id = new_traps_by_id
    start_epd_index = new_epd_index
    _summaries = new_summaries

    logger.info("traps: loaded %d traps from '%s'", len(traps_by_id), file_path)


def init(path: Optional[str] = None) -> None:
    """Convenience wrapper — call load() at app startup."""
    load(path)


def summaries() -> list[dict]:
    """Return the ordered summary list (commonness DESC, then name ASC).

    Each entry has keys: id, name, color, eco, parentOpening, commonness.
    Returns an empty list when no data is loaded.
    """
    return list(_summaries)


def get(trap_id: str) -> Optional[dict]:
    """Return the full trap object for *trap_id*, or None if unknown."""
    return traps_by_id.get(trap_id)


def available(base_fen: str, uci_moves: list[str]) -> list[dict]:
    """Return summaries of traps whose start EPD matches the position after
    replaying *base_fen* + *uci_moves*.

    Args:
        base_fen:  Starting FEN (e.g. the standard starting position FEN).
        uci_moves: List of UCI move strings, e.g. ["e2e4", "e7e5", "g1f3"].

    Returns:
        A (possibly empty) list of summary dicts in the same order as
        ``summaries()``.  Never raises — returns [] on any error.
    """
    if not traps_by_id:
        return []

    try:
        board = chess.Board(base_fen)
    except Exception:
        return []

    for uci in uci_moves:
        try:
            board.push_uci(uci)
        except Exception:
            return []

    current_epd = board.epd()
    trap_ids = start_epd_index.get(current_epd, [])
    if not trap_ids:
        return []

    # Return summaries in the canonical order (commonness desc, name asc).
    id_set = set(trap_ids)
    return [s for s in _summaries if s["id"] in id_set]
