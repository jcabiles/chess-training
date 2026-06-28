"""FastAPI app for the Stockfish Analysis Board.

Wires together the engine wrapper (:mod:`app.engine`), the pure classification
logic (:mod:`app.analysis`), and the request/response schemas
(:mod:`app.models`), and serves the static frontend.

Server responsibilities (see docs/ai-dlc/specs/stockfish-analysis-board.md):

* Authoritative legality check with python-chess on every move.
* Build the single shared ``Analysis`` object the same way for every endpoint.
* For ``/api/move`` run TWO depth-pinned analyses (before + after) so the
  move-quality label is comparable, classified via :func:`app.analysis.classify`.

The server is stateless per request: move history / undo lives client-side.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import chess
from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import openings, traps
from app.analysis import classify, pov_score_to_white_cp
from app.engine import AnalysisResult, EngineUnavailable, StockfishEngine
from app.models import (
    Analysis,
    AnalyzeRequest,
    AnalyzeResponse,
    CommentaryRequest,
    LoadRequest,
    LoadResponse,
    MoveRequest,
    MoveResponse,
    OpeningRequest,
    TrapsCheckRequest,
)

logger = logging.getLogger("chess_training")

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
INDEX_HTML = STATIC_DIR / "index.html"
OPENINGS_DIR = BASE_DIR / "data" / "openings"
COMMENTARY_FILE = BASE_DIR / "data" / "commentary.json"
TRAPS_FILE = BASE_DIR / "data" / "traps.json"


def _load_commentary() -> dict:
    """Load bundled opening commentary (EPD -> {san, text}); empty if absent."""
    try:
        with COMMENTARY_FILE.open(encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        logger.warning("commentary.json is not a JSON object — ignoring.")
    except FileNotFoundError:
        logger.warning("commentary.json not found — move commentary disabled.")
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("commentary.json could not be loaded: %s", exc)
    return {}


# ---------------------------------------------------------------------------
# Lifespan: own exactly one engine instance. Try to start it eagerly so a
# missing binary is reported at boot, but DON'T crash the app if it's absent —
# the frontend + FEN-validation routes still work, and engine routes return 503.
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = StockfishEngine()
    app.state.engine = engine
    try:
        engine.start()
        logger.info("Stockfish engine started.")
    except EngineUnavailable as exc:
        logger.warning("Stockfish unavailable at startup: %s", exc)

    # Opening trainer data (both degrade gracefully if files are absent).
    openings.init(str(OPENINGS_DIR))
    app.state.commentary = _load_commentary()
    traps.init(str(TRAPS_FILE))

    try:
        yield
    finally:
        engine.close()


app = FastAPI(title="Stockfish Analysis Board", lifespan=lifespan)


def get_engine(request: Request) -> StockfishEngine:
    """Dependency: the app's single engine instance (overridable in tests)."""
    return request.app.state.engine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _build_analysis(result: AnalysisResult, quality: str | None = None) -> Analysis:
    """Construct the shared ``Analysis`` object from a raw engine result.

    ``bestMoveSan`` / ``pvSan`` describe the analyzed (resulting) position;
    ``quality`` is supplied only when a prior move exists.
    """
    white = result.score.white()
    if white.is_mate():
        eval_cp: int | None = None
        mate: int | None = white.mate()
    else:
        eval_cp = int(white.score())
        mate = None

    pv_san = result.pv_san
    best_move_san = pv_san[0] if pv_san else None

    return Analysis(
        evalCp=eval_cp,
        mate=mate,
        evalWhitePov=pov_score_to_white_cp(result.score),
        bestMoveSan=best_move_san,
        pvSan=pv_san,
        quality=quality,
    )


def _engine_unavailable_response() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "detail": (
                "Stockfish engine is not available. Install it "
                "(`brew install stockfish`) or set STOCKFISH_PATH, then restart."
            )
        },
    )


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------
@app.post("/api/analyze", response_model=AnalyzeResponse)
async def analyze_position(
    req: AnalyzeRequest, engine: StockfishEngine = Depends(get_engine)
):
    """Analyze a position (no prior move → ``quality`` is null)."""
    try:
        chess.Board(req.fen)  # validate
    except ValueError:
        return JSONResponse(status_code=400, content={"detail": "Invalid FEN."})
    try:
        result = await engine.analyze(req.fen)
    except EngineUnavailable:
        return _engine_unavailable_response()
    return AnalyzeResponse(analysis=_build_analysis(result, quality=None))


@app.post("/api/load", response_model=LoadResponse)
async def load_fen(req: LoadRequest, engine: StockfishEngine = Depends(get_engine)):
    """Validate a FEN and, if valid, return its analysis (no quality label)."""
    try:
        board = chess.Board(req.fen)
    except ValueError as exc:
        return LoadResponse(valid=False, fen=None, analysis=None, error=f"Invalid FEN: {exc}")
    try:
        result = await engine.analyze(board.fen())
    except EngineUnavailable:
        return _engine_unavailable_response()
    return LoadResponse(
        valid=True,
        fen=board.fen(),
        analysis=_build_analysis(result, quality=None),
        error=None,
    )


@app.post("/api/move", response_model=MoveResponse)
async def make_move(req: MoveRequest, engine: StockfishEngine = Depends(get_engine)):
    """Validate + apply a move, then analyze before/after and label its quality.

    Runs two depth-pinned analyses (before and after the move) so the cpLoss is
    computed from comparable evals. Returns the resulting position's analysis.
    """
    # Parse the position. A bad FEN means we can't validate the move → illegal.
    try:
        board = chess.Board(req.fen)
    except ValueError:
        return MoveResponse(legal=False, fen=None, lastMoveSan=None, analysis=None)

    # Parse + legality-check the move (UCI, possibly with a promotion suffix).
    try:
        move = chess.Move.from_uci(req.move)
    except (ValueError, chess.InvalidMoveError):
        return MoveResponse(legal=False, fen=None, lastMoveSan=None, analysis=None)
    if move not in board.legal_moves:
        return MoveResponse(legal=False, fen=None, lastMoveSan=None, analysis=None)

    mover_is_white = board.turn == chess.WHITE
    last_move_san = board.san(move)  # render SAN BEFORE pushing

    fen_before = board.fen()
    board.push(move)
    fen_after = board.fen()

    try:
        before = await engine.analyze(fen_before)
        after = await engine.analyze(fen_after)
    except EngineUnavailable:
        return _engine_unavailable_response()

    quality = classify(
        pov_score_to_white_cp(before.score),
        pov_score_to_white_cp(after.score),
        mover_is_white,
    )

    return MoveResponse(
        legal=True,
        fen=fen_after,
        lastMoveSan=last_move_san,
        analysis=_build_analysis(after, quality=quality),
    )


# ---------------------------------------------------------------------------
# Opening trainer (additive; degrade gracefully when data is absent)
# ---------------------------------------------------------------------------
@app.post("/api/opening")
async def opening_info(req: OpeningRequest):
    """Live opening detection + candidate continuations for the current line.

    Server derives all EPDs from baseFen + UCI moves (the client never sends
    EPDs). Always returns a well-formed body; empty/degraded when data is absent.
    """
    current = openings.identify(req.baseFen, req.moves)
    cand = {"items": [], "truncated": False}
    try:
        board = chess.Board(req.baseFen)
        for uci in req.moves:
            board.push_uci(uci)
        cand = openings.candidates(board.fen(), q=req.q)
    except Exception:
        # A malformed line never breaks detection — just yields no candidates.
        pass
    return {
        "current": current,
        "candidates": cand["items"],
        "truncated": cand["truncated"],
    }


@app.post("/api/opening/commentary")
async def opening_commentary(req: CommentaryRequest, request: Request):
    """Bundled 'why this move' commentary for a position (EPD lookup), or null."""
    commentary = getattr(request.app.state, "commentary", {}) or {}
    return openings.commentary_lookup(req.fen, commentary)


# ---------------------------------------------------------------------------
# Opening traps (additive; degrade gracefully when data is absent)
# ---------------------------------------------------------------------------
@app.get("/api/traps")
async def list_traps():
    """Browse list of all trap summaries (id, name, color, eco, parentOpening, commonness).

    Always returns a well-formed body; empty list when data is absent.
    """
    return {"traps": traps.summaries()}


# IMPORTANT: this POST /api/traps/check route is registered BEFORE the
# GET /api/traps/{trap_id} route so FastAPI never matches the literal path
# "/api/traps/check" as trap_id="check".
@app.post("/api/traps/check")
async def check_traps(req: TrapsCheckRequest):
    """Return trap summaries whose start EPD matches the current position.

    Server derives all EPDs from baseFen + UCI moves (the client never sends
    EPDs). Always returns a well-formed body; empty/degraded when data is absent.
    Never 500.
    """
    try:
        available = traps.available(req.baseFen, req.moves)
    except Exception:
        available = []
    return {"available": available}


@app.get("/api/traps/{trap_id}")
async def get_trap(trap_id: str):
    """Return the full trap object for trap_id, or 404 if unknown."""
    trap = traps.get(trap_id)
    if trap is None:
        return JSONResponse(status_code=404, content={"detail": f"Trap {trap_id!r} not found."})
    return trap


# ---------------------------------------------------------------------------
# Static frontend (mounted last so /api/* takes precedence)
# ---------------------------------------------------------------------------
@app.get("/")
async def index():
    """Serve the board UI (404 until the frontend ticket T6 lands)."""
    if INDEX_HTML.is_file():
        return FileResponse(INDEX_HTML)
    return JSONResponse(
        status_code=404,
        content={"detail": "Frontend not built yet (static/index.html missing)."},
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
