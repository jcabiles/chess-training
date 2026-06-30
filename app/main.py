"""FastAPI app for the Stockfish Analysis Board.

Wires together the engine wrapper (:mod:`app.engine`), the pure classification
logic (:mod:`app.analysis`), and the request/response schemas
(:mod:`app.models`), and serves the static frontend.

Server responsibilities (see docs/design/specs/stockfish-analysis-board.md):

* Authoritative legality check with python-chess on every move.
* Build the single shared ``Analysis`` object the same way for every endpoint.
* For ``/api/move`` run TWO depth-pinned analyses (before + after) so the
  move-quality label is comparable, classified via :func:`app.analysis.classify`.

The server is stateless per request: move history / undo lives client-side.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import chess
from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import (
    book,
    coaching,
    openings,
    pgn,
    profile,
    repertoire,
    review,
    storage,
    traps,
)
from app.analysis import classify, pov_score_to_white_cp
from app.engine import AnalysisResult, EngineUnavailable, StockfishEngine
from app.models import (
    Analysis,
    AnalyzeAllResponse,
    AnalyzeRequest,
    AnalyzeResponse,
    AnalyzeStatusResponse,
    CoverageDict,
    EngineRestartResponse,
    EngineStatusResponse,
    GameDetail,
    GameSummary,
    ImportRequest,
    ImportResponse,
    LoadRequest,
    LoadResponse,
    MoveRequest,
    MoveResponse,
    NarratedLeak,
    OpeningRequest,
    PlyDetail,
    ProfileResponse,
    RetagRequest,
    RetagResponse,
    ReviewResponse,
    SetColorRequest,
    TrapsCheckRequest,
)

logger = logging.getLogger("chess_training")

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
INDEX_HTML = STATIC_DIR / "index.html"
OPENINGS_DIR = BASE_DIR / "data" / "openings"
TRAPS_FILE = BASE_DIR / "data" / "traps.json"
BOOK_FILE = BASE_DIR / "data" / "book.json"
REPERTOIRE_FILE = BASE_DIR / "data" / "repertoire.json"
GAMES_DIR = BASE_DIR / "data" / "games"


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

    # Opening name detection + traps + repertoire (all degrade gracefully if absent).
    openings.init(str(OPENINGS_DIR))
    traps.init(str(TRAPS_FILE))
    repertoire.init(str(REPERTOIRE_FILE))  # needs traps loaded first (trapId leaves)

    # Opening-book fast-path: derive a set of repertoire positions from the lichess
    # lines (scoped by data/book.json) + trap mainlines + curated repertoire lines.
    # Guarded so a malformed line can never crash startup — the API tests run this real
    # lifespan via TestClient. Repertoire lines are full-from-start lines that must
    # bypass the book.json firstMoves filter, so they ride the trap_ucis slot.
    try:
        book.init(
            str(BOOK_FILE),
            lines=openings.iter_lines(),
            trap_ucis=list(traps.iter_mainline_ucis()) + list(repertoire.iter_lines()),
        )
    except Exception as exc:  # pragma: no cover - defensive; book degrades to empty
        logger.warning("Opening book unavailable (continuing without it): %s", exc)

    # Game-review storage: init DB, scan data/games/ for PGN files, reset stuck jobs.
    storage.init()
    _import_games_folder()
    try:
        review.reset_stuck()
    except Exception as exc:
        logger.warning("review: reset_stuck failed at startup: %s", exc)

    try:
        yield
    finally:
        engine.close()


def _import_games_folder() -> None:
    """Scan data/games/ for *.pgn files and import them into storage (deduped).

    Wraps each file in try/except so a bad PGN file never crashes startup.
    """
    if not GAMES_DIR.is_dir():
        return
    now = datetime.now(timezone.utc).isoformat()
    for pgn_path in GAMES_DIR.glob("*.pgn"):
        try:
            text = pgn_path.read_text(encoding="utf-8", errors="replace")
            parsed_games = pgn.parse_games(text)
            for g in parsed_games:
                try:
                    gid = storage.insert_game({
                        "content_hash": g.content_hash,
                        "pgn": g.pgn,
                        "headers_json": None,
                        "white": g.white,
                        "black": g.black,
                        "result": g.result,
                        "eco": g.eco,
                        "opening": g.opening,
                        "date": g.date,
                        "my_color": g.my_color,
                        "source": str(pgn_path.name),
                        "ply_count": g.ply_count,
                        "imported_at": now,
                    })
                    # Write per-ply rows if not already present (backfills dedup re-imports).
                    if not storage.get_plies(gid):
                        storage.write_plies(gid, [
                            {"ply": p.ply, "san": p.san, "uci": p.uci,
                             "fen_before": p.fen_before, "clock_centis": p.clock_centis}
                            for p in g.plies
                        ])
                except Exception as exc:
                    logger.warning("startup import: failed to insert game from %s: %s", pgn_path.name, exc)
        except Exception as exc:
            logger.warning("startup import: failed to read/parse %s: %s", pgn_path.name, exc)


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
    best_move_uci = result.pv[0].uci() if result.pv else None

    return Analysis(
        evalCp=eval_cp,
        mate=mate,
        evalWhitePov=pov_score_to_white_cp(result.score),
        bestMoveSan=best_move_san,
        bestMoveUci=best_move_uci,
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

    # Opening-book fast-path: when the client opts in (play mode) and the move stays
    # in book, return instantly WITHOUT touching the engine. Legality is already
    # checked above, so this only ever short-circuits a valid move.
    if req.useBook and book.is_book_move(fen_before, req.move):
        opening = openings.name_for_fen(fen_after)
        return MoveResponse(
            legal=True,
            fen=fen_after,
            lastMoveSan=last_move_san,
            analysis=None,
            book=True,
            openingName=opening["name"] if opening else None,
            openingEco=opening["eco"] if opening else None,
        )

    review.note_interactive_start()
    try:
        before = await engine.analyze(fen_before)
        after = await engine.analyze(fen_after)
    except EngineUnavailable:
        return _engine_unavailable_response()
    finally:
        review.note_interactive_end()

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
# Engine control routes
# ---------------------------------------------------------------------------
@app.get("/api/engine/status", response_model=EngineStatusResponse)
async def engine_status(engine: StockfishEngine = Depends(get_engine)):
    """Return the current status of the Stockfish engine."""
    return EngineStatusResponse(running=engine.is_running)


@app.post("/api/engine/restart", response_model=EngineRestartResponse)
async def restart_engine(engine: StockfishEngine = Depends(get_engine)):
    """Force-restart the Stockfish engine.

    Terminates the current engine subprocess (if any) and schedules a fresh
    start for the next analysis request. Does not require the engine to be
    healthy; safe to call when wedged. Always returns status 200 with restarted=True.
    """
    await engine.restart()
    return EngineRestartResponse(restarted=True, running=engine.is_running)


# ---------------------------------------------------------------------------
# Opening trainer (additive; degrade gracefully when data is absent)
# ---------------------------------------------------------------------------
@app.post("/api/opening")
async def opening_info(req: OpeningRequest):
    """Live opening detection (name + ECO) for the current line.

    Server derives all EPDs from baseFen + UCI moves (the client never sends
    EPDs). Always returns a well-formed body; ``current`` is null when no named
    opening matches or when data is absent.
    """
    return {"current": openings.identify(req.baseFen, req.moves)}


# ---------------------------------------------------------------------------
# Repertoire trainer (additive; degrade gracefully when data is absent)
# ---------------------------------------------------------------------------
@app.get("/api/repertoire")
async def get_repertoire():
    """Return the curated repertoire: per-color move trees + grouped catalog.

    Always returns a well-formed body; empty (roots present, no children, empty
    catalog) when data is absent. Never 500.
    """
    return {"tree": repertoire.tree()}


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
# Game library + review routes (additive; degrade gracefully when storage absent)
#
# IMPORTANT: literal paths (e.g. /api/games/import) are registered BEFORE
# parameterised paths (/api/games/{game_id}) so FastAPI never matches the
# literal segment as a game_id value.  This mirrors the traps precedent above.
# ---------------------------------------------------------------------------

def _game_summary(row: dict) -> GameSummary:
    """Build a GameSummary from a storage row dict."""
    return GameSummary(
        id=row["id"],
        white=row.get("white"),
        black=row.get("black"),
        result=row.get("result"),
        eco=row.get("eco"),
        opening=row.get("opening"),
        date=row.get("date"),
        my_color=row.get("my_color"),
        ply_count=row.get("ply_count"),
        analysis_status=row.get("analysis_status", "pending"),
        imported_at=row["imported_at"],
    )


# IMPORTANT: all literal /api/games/<word> paths are registered BEFORE
# /api/games/{game_id} so FastAPI never interprets the literal segment as an
# integer game_id.  Order: import → retag-color → analyze-all → {game_id}/*.

@app.post("/api/games/import", response_model=ImportResponse)
async def import_games(req: ImportRequest):
    """Import one or more PGN games from pasted text.

    Parses the PGN, inserts each game (deduped by content_hash), and applies
    the optional ``my_color`` override to every game in the batch.
    Returns imported/duplicate counts and summaries of all games in the batch.
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        parsed_games = pgn.parse_games(req.pgn)
    except Exception as exc:
        return JSONResponse(status_code=400, content={"detail": f"PGN parse error: {exc}"})

    imported = 0
    duplicates = 0
    summaries: list[GameSummary] = []

    for g in parsed_games:
        # Apply per-request my_color override (Refuter #8).
        my_color = req.my_color if req.my_color is not None else g.my_color
        fields = {
            "content_hash": g.content_hash,
            "pgn": g.pgn,
            "headers_json": None,
            "white": g.white,
            "black": g.black,
            "result": g.result,
            "eco": g.eco,
            "opening": g.opening,
            "date": g.date,
            "my_color": my_color,
            "source": "import",
            "ply_count": g.ply_count,
            "imported_at": now,
        }
        try:
            # Check whether the game already exists before inserting.
            from app.storage import _get_conn  # noqa: PLC0415
            conn = _get_conn()
            existing = conn.execute(
                "SELECT id FROM games WHERE content_hash = ?", (g.content_hash,)
            ).fetchone()
            game_id = storage.insert_game(fields)
            if existing is None:
                imported += 1
            else:
                duplicates += 1
            # Write per-ply rows if not already present (backfills dedup re-imports).
            if not storage.get_plies(game_id):
                storage.write_plies(game_id, [
                    {"ply": p.ply, "san": p.san, "uci": p.uci,
                     "fen_before": p.fen_before, "clock_centis": p.clock_centis}
                    for p in g.plies
                ])
            row = storage.get_game(game_id)
            if row:
                summaries.append(_game_summary(row))
        except Exception as exc:
            logger.warning("import: failed to insert game: %s", exc)

    return ImportResponse(imported=imported, duplicates=duplicates, games=summaries)


@app.get("/api/games", response_model=list[GameSummary])
async def list_games():
    """Return all saved games, most recently imported first."""
    try:
        rows = storage.list_games()
    except Exception:
        return []
    return [_game_summary(r) for r in rows]


# IMPORTANT: these two POST literal routes are registered BEFORE
# /api/games/{game_id} so they are never shadowed by the parameterised route.

@app.post("/api/games/retag-color", response_model=RetagResponse)
async def retag_color(req: RetagRequest):
    """Bulk-tag my_color on games whose White/Black name matches any alias.

    Accepts a comma-separated list of usernames in ``username``.  Matching is
    case-insensitive and trimmed (same logic as import-time inference).  Each
    matching game has its ``analysis_status`` reset to 'pending' so that the
    next bulk-analyze pass recomputes leaks under the correct color.

    Returns the number of updated games and fresh coverage counts.
    """
    aliases = [a.strip() for a in req.username.split(",") if a.strip()]
    try:
        updated = storage.retag_colors_by_aliases(aliases)
        cov = storage.coverage()
    except Exception as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return RetagResponse(
        updated=updated,
        coverage=CoverageDict(**cov),
    )


@app.post("/api/games/analyze-all", response_model=AnalyzeAllResponse)
async def analyze_all_games(engine: StockfishEngine = Depends(get_engine)):
    """Start a single background task that analyzes all 'pending' games sequentially.

    If a bulk-analyze task is already running, returns immediately (no-op).
    Returns the count of pending games at the time of the call.
    """
    try:
        pending_count = storage.coverage().get("pending", 0)
    except Exception:
        pending_count = 0

    review.start_analyze_all(engine, depth=review.BACKGROUND_DEPTH)
    return AnalyzeAllResponse(pending=pending_count)


@app.get("/api/games/{game_id}", response_model=GameDetail)
async def get_game(game_id: int):
    """Return a single saved game with per-ply data, or 404 if not found."""
    try:
        row = storage.get_game(game_id)
    except Exception:
        row = None
    if row is None:
        return JSONResponse(status_code=404, content={"detail": f"Game {game_id} not found."})

    try:
        ply_rows = storage.get_plies(game_id)
    except Exception:
        ply_rows = []

    plies = [
        PlyDetail(
            ply=p["ply"],
            san=p.get("san"),
            uci=p.get("uci"),
            fen_before=p.get("fen_before"),
            eval_cp_white=p.get("eval_cp_white"),
            mate_white=p.get("mate_white"),
            win_prob=p.get("win_prob"),
            is_user_move=bool(p.get("is_user_move", 0)),
            clock_centis=p.get("clock_centis"),
        )
        for p in ply_rows
    ]

    return GameDetail(
        id=row["id"],
        white=row.get("white"),
        black=row.get("black"),
        result=row.get("result"),
        eco=row.get("eco"),
        opening=row.get("opening"),
        date=row.get("date"),
        my_color=row.get("my_color"),
        ply_count=row.get("ply_count"),
        analysis_status=row.get("analysis_status", "pending"),
        imported_at=row["imported_at"],
        pgn=row.get("pgn", ""),
        plies=plies,
    )


@app.patch("/api/games/{game_id}", response_model=GameSummary)
async def patch_game(game_id: int, req: SetColorRequest):
    """Set or clear ``my_color`` on a single game.

    Resetting the color invalidates previously computed leaks, so
    ``analysis_status`` is automatically reset to 'pending'.  Returns the
    updated game summary, or 404 if the game does not exist.
    """
    try:
        changed = storage.set_my_color(game_id, req.my_color)
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    if not changed:
        return JSONResponse(status_code=404, content={"detail": f"Game {game_id} not found."})
    row = storage.get_game(game_id)
    if row is None:
        return JSONResponse(status_code=404, content={"detail": f"Game {game_id} not found."})
    return _game_summary(row)


@app.post("/api/games/{game_id}/analyze", response_model=AnalyzeStatusResponse)
async def analyze_game_route(
    game_id: int, engine: StockfishEngine = Depends(get_engine)
):
    """Start background analysis for a saved game.

    Returns an accepted response. The job status can be polled via
    GET /api/games/{game_id}/status. Engine failures are handled inside the
    background task (status moves to 'failed') rather than at route level.
    """
    try:
        row = storage.get_game(game_id)
    except Exception:
        row = None
    if row is None:
        return JSONResponse(status_code=404, content={"detail": f"Game {game_id} not found."})

    review.start_analysis(game_id, engine, depth=review.BACKGROUND_DEPTH)

    # Re-fetch status after starting (it should be 'analyzing' now).
    try:
        row = storage.get_game(game_id)
        status = row.get("analysis_status", "analyzing") if row else "analyzing"
    except Exception:
        status = "analyzing"

    return AnalyzeStatusResponse(game_id=game_id, analysis_status=status)


@app.get("/api/games/{game_id}/status", response_model=AnalyzeStatusResponse)
async def get_game_status(game_id: int):
    """Return the analysis status for a game."""
    try:
        row = storage.get_game(game_id)
    except Exception:
        row = None
    if row is None:
        return JSONResponse(status_code=404, content={"detail": f"Game {game_id} not found."})
    return AnalyzeStatusResponse(
        game_id=game_id,
        analysis_status=row.get("analysis_status", "pending"),
    )


@app.get("/api/games/{game_id}/review", response_model=ReviewResponse)
async def get_game_review(game_id: int):
    """Return narrated leaks + per-ply evals for the foresight UI."""
    try:
        row = storage.get_game(game_id)
    except Exception:
        row = None
    if row is None:
        return JSONResponse(status_code=404, content={"detail": f"Game {game_id} not found."})

    status = row.get("analysis_status", "pending")

    try:
        leak_rows = storage.get_leaks(game_id)
    except Exception:
        leak_rows = []

    try:
        ply_rows = storage.get_plies(game_id)
    except Exception:
        ply_rows = []

    narrator = coaching.get_narrator()
    narrated: list[NarratedLeak] = []
    for lk in leak_rows:
        try:
            narration = narrator.narrate_leak(lk)
        except Exception:
            narration = {"threat": None, "hanging": None, "plan": None, "summary": ""}
        narrated.append(NarratedLeak(
            id=lk.get("id") if isinstance(lk, dict) else getattr(lk, "id", None),
            ply=lk["ply"] if isinstance(lk, dict) else lk.ply,
            lead_in_ply=lk.get("lead_in_ply") if isinstance(lk, dict) else lk.lead_in_ply,
            severity=lk["severity"] if isinstance(lk, dict) else lk.severity,
            category=lk["category"] if isinstance(lk, dict) else lk.category,
            phase=lk["phase"] if isinstance(lk, dict) else lk.phase,
            win_prob_before=lk["win_prob_before"] if isinstance(lk, dict) else lk.win_prob_before,
            win_prob_after=lk["win_prob_after"] if isinstance(lk, dict) else lk.win_prob_after,
            win_prob_drop=lk["win_prob_drop"] if isinstance(lk, dict) else lk.win_prob_drop,
            best_san=lk.get("best_san") if isinstance(lk, dict) else lk.best_san,
            best_uci=lk.get("best_uci") if isinstance(lk, dict) else lk.best_uci,
            threat_uci=lk.get("threat_uci") if isinstance(lk, dict) else lk.threat_uci,
            threat_motif=lk.get("threat_motif") if isinstance(lk, dict) else lk.threat_motif,
            hung_square=lk.get("hung_square") if isinstance(lk, dict) else lk.hung_square,
            narration=narration,
        ))

    plies = [
        PlyDetail(
            ply=p["ply"],
            san=p.get("san"),
            uci=p.get("uci"),
            fen_before=p.get("fen_before"),
            eval_cp_white=p.get("eval_cp_white"),
            mate_white=p.get("mate_white"),
            win_prob=p.get("win_prob"),
            is_user_move=bool(p.get("is_user_move", 0)),
            clock_centis=p.get("clock_centis"),
        )
        for p in ply_rows
    ]

    return ReviewResponse(game_id=game_id, analysis_status=status, leaks=narrated, plies=plies)


@app.delete("/api/games/{game_id}")
async def delete_game(game_id: int):
    """Cancel any running analysis and delete the game + its plies/leaks."""
    review.cancel_analysis(game_id)
    try:
        deleted = storage.delete_game(game_id)
    except Exception as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    if not deleted:
        return JSONResponse(status_code=404, content={"detail": f"Game {game_id} not found."})
    return {"deleted": game_id}


# ---------------------------------------------------------------------------
# Profile (additive)
# ---------------------------------------------------------------------------

@app.get("/api/profile", response_model=ProfileResponse)
async def get_profile():
    """Return the cross-game tendency profile for the user."""
    try:
        data = profile.build_profile()
    except RuntimeError:
        # Storage not initialised (edge case in tests or first boot before init).
        return ProfileResponse(
            games_analyzed=0,
            games_total=0,
            games_tagged=0,
            top_leaks=[],
            by_phase={"opening": 0, "middlegame": 0, "endgame": 0},
            by_opening=[],
            by_color={"white": 0, "black": 0},
            hope_chess_rate=0.0,
            trend=[],
        )
    # Merge coverage counts into the profile payload.
    try:
        cov = storage.coverage()
        data["games_total"] = cov.get("total", 0)
        data["games_tagged"] = cov.get("tagged", 0)
    except Exception:
        data.setdefault("games_total", 0)
        data.setdefault("games_tagged", 0)
    return ProfileResponse(**data)


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
