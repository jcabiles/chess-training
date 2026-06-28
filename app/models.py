"""Pydantic v2 request/response schemas for the Stockfish analysis board.

Pure data definitions only — no logic, no engine/`chess` imports. The single
shared :class:`Analysis` object is returned identically by all three endpoints
(`/api/move`, `/api/analyze`, `/api/load`); see
``docs/ai-dlc/specs/stockfish-analysis-board.md`` (API section).
"""

from typing import Literal

from pydantic import BaseModel, Field

# Move-quality buckets derived from centipawn loss (see spec classification).
Quality = Literal["best", "good", "inaccuracy", "mistake", "blunder"]


class Analysis(BaseModel):
    """The single shared analysis shape returned by every endpoint.

    ``evalCp`` and ``mate`` are the raw display fields (exactly one is set; the
    other is ``None``). ``evalWhitePov`` is the normalized White-POV centipawn
    value used by move-quality classification (mate scores are mapped to cp).
    ``bestMoveSan`` / ``pvSan`` describe the *resulting* position. ``quality``
    is ``None`` unless a prior move exists (e.g. on FEN load / initial position).
    """

    evalCp: int | None = Field(
        description="Raw eval in centipawns from White's POV; None when the "
        "position is a forced mate."
    )
    mate: int | None = Field(
        description="Moves-to-mate (signed, White's POV); None when the eval is "
        "a centipawn score."
    )
    evalWhitePov: int = Field(
        description="Normalized White-POV centipawns used for classification; "
        "mate scores are mate-mapped to cp."
    )
    bestMoveSan: str | None = Field(
        description="Engine's best move for the resulting position, in SAN; None "
        "if unavailable (e.g. terminal position)."
    )
    pvSan: list[str] = Field(
        default_factory=list,
        description="Principal variation (engine's top line) for the resulting "
        "position, in SAN.",
    )
    quality: Quality | None = Field(
        default=None,
        description="Move-quality label for the move just played; None when there "
        "is no prior move.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "evalCp": 80,
                    "mate": None,
                    "evalWhitePov": 80,
                    "bestMoveSan": "Nf3",
                    "pvSan": ["Nf3", "Nc6", "Bb5"],
                    "quality": "good",
                }
            ]
        }
    }


# --- Request models ---------------------------------------------------------


class MoveRequest(BaseModel):
    """Body for ``POST /api/move`` — the move to apply against ``fen``."""

    fen: str = Field(description="Position BEFORE the move, in FEN.")
    move: str = Field(
        description="Move in UCI; may include a promotion suffix, e.g. 'e7e8q'."
    )


class AnalyzeRequest(BaseModel):
    """Body for ``POST /api/analyze`` — a position to analyze (no prior move)."""

    fen: str = Field(description="Position to analyze, in FEN.")


class LoadRequest(BaseModel):
    """Body for ``POST /api/load`` — a candidate FEN to validate and load."""

    fen: str = Field(description="Candidate position to validate and load, in FEN.")


class OpeningRequest(BaseModel):
    """Body for ``POST /api/opening`` — the current line (server derives EPDs)."""

    baseFen: str = Field(description="Starting FEN of the line (usually the standard start).")
    moves: list[str] = Field(
        default_factory=list, description="UCI moves applied from baseFen, in order."
    )
    q: str | None = Field(
        default=None, description="Optional case-insensitive name filter for candidates."
    )


class CommentaryRequest(BaseModel):
    """Body for ``POST /api/opening/commentary`` — a position to look up."""

    fen: str = Field(description="Position (after a move) to fetch commentary for, in FEN.")


class TrapsCheckRequest(BaseModel):
    """Body for ``POST /api/traps/check`` — the current line (server derives EPD)."""

    baseFen: str = Field(description="Starting FEN of the line (usually the standard start).")
    moves: list[str] = Field(
        default_factory=list, description="UCI moves applied from baseFen, in order."
    )


# --- Response models --------------------------------------------------------


class MoveResponse(BaseModel):
    """Response for ``POST /api/move``.

    On an illegal move, ``legal`` is False and every other field is ``None``.
    On a legal move, ``fen`` is the position after the move and ``analysis``
    describes that resulting position.
    """

    legal: bool = Field(description="Whether the submitted move was legal.")
    fen: str | None = Field(
        default=None, description="Position AFTER the move, in FEN; None if illegal."
    )
    lastMoveSan: str | None = Field(
        default=None, description="The applied move in SAN; None if illegal."
    )
    analysis: Analysis | None = Field(
        default=None,
        description="Analysis of the resulting position; None if illegal.",
    )


class AnalyzeResponse(BaseModel):
    """Response for ``POST /api/analyze`` — analysis of the given position."""

    analysis: Analysis = Field(
        description="Analysis of the position; quality is None (no prior move)."
    )


class LoadResponse(BaseModel):
    """Response for ``POST /api/load``.

    On a valid FEN, ``valid`` is True with ``fen`` and ``analysis`` set and
    ``error`` None. On an invalid FEN, ``valid`` is False with ``error`` set
    and ``fen`` / ``analysis`` None.
    """

    valid: bool = Field(description="Whether the submitted FEN was valid.")
    fen: str | None = Field(
        default=None, description="Loaded position in FEN; None if invalid."
    )
    analysis: Analysis | None = Field(
        default=None,
        description="Analysis of the loaded position; None if invalid.",
    )
    error: str | None = Field(
        default=None, description="Validation error message; None if valid."
    )
