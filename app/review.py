"""
review.py — Engine-orchestrating analysis pipeline + background job.

This module is the *impure conductor*: it calls the engine (via the same single
asyncio.Lock the rest of the app uses) but is fully testable with ScriptedEngine
(no real Stockfish binary needed in tests).

Public surface
--------------
analyze_game(game_id, engine, *, depth, progress_cb) -> None
    Walk every ply through the engine, fill pos_cache + game_plies, classify
    leaks (Win%-drop, user-color only, mistakes+blunders only), run the
    null-move threat probe + motifs scan at each leak, write everything to DB.

start_analysis(game_id, engine, **kw) -> asyncio.Task
    Create and track a background task; set status → 'analyzing'.

cancel_analysis(game_id) -> bool
    Cancel the tracked task if present.

note_interactive_start() / note_interactive_end()
    Called by /api/move (T7) to signal that an interactive engine call is
    pending.  The background loop pauses before issuing the next engine call
    while the counter > 0.

reset_stuck()
    Delegate to storage.reset_analyzing_to_pending() (called at startup by T7).

No-starve design (Refuter #5)
------------------------------
The background loop does `await asyncio.sleep(0)` between every ply so
interactive requests can be scheduled by the event loop.  Additionally a
module-level counter (_interactive_pending) is incremented by
note_interactive_start() and decremented by note_interactive_end().  Before
issuing each engine call the loop polls until _interactive_pending == 0 (with a
brief asyncio.sleep(0.05) between polls).

Bounded worst-case: the background pass uses BACKGROUND_DEPTH (configurable via
REVIEW_BG_DEPTH env, default 10) which is lower than the interactive default
(DEFAULT_DEPTH = 18).  At depth 10 each engine call takes ~50-200 ms.  An
interactive request that calls note_interactive_start() will prevent the
background from issuing a *new* engine call; the interactive call then acquires
the same underlying engine lock and runs within one background-ply window.
Worst case = one already-in-flight background search (≤ ~200 ms) + the
interactive search itself.

Job lifecycle (Refuter #6)
---------------------------
- task registry: dict[game_id, asyncio.Task]
- start_analysis: creates task, sets status → 'analyzing', registers in registry
- task body: wraps analyze_game; on completion removes from registry, sets
  'done'; on EngineUnavailable sets 'failed' (logged, no crash); on
  CancelledError propagates (allows clean cancellation).
- cancel_analysis: cancels the tracked task (T7 DELETE route calls this).
- reset_stuck: flips any 'analyzing' rows → 'pending' (surviving restarts).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Callable, Optional

import chess

from app import motifs, storage
from app.analysis import (
    game_phase,
    leak_severity,
    pov_score_to_white_cp,
    win_prob_white,
)
from app.storage import LeakRecord

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Default depth for background analysis (lower than interactive to reduce
#: worst-case latency for interactive /api/move calls).
BACKGROUND_DEPTH: int = int(os.environ.get("REVIEW_BG_DEPTH", "10"))

#: How long (seconds) to sleep between polls when waiting for the interactive
#: counter to drop to zero.
_INTERACTIVE_POLL_INTERVAL: float = 0.05

#: Maximum plies to look back when searching for the lead-in start.
_LEAD_IN_LOOKBACK: int = 3

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# Task registry: game_id → running asyncio.Task
_tasks: dict[int, asyncio.Task] = {}  # type: ignore[type-arg]

# Interactive-pending counter: note_interactive_start increments, note_interactive_end
# decrements.  The background loop waits while this is > 0.
_interactive_pending: int = 0


# ---------------------------------------------------------------------------
# Interactive-pending counter (called by T7's /api/move)
# ---------------------------------------------------------------------------


def note_interactive_start() -> None:
    """Signal that an interactive engine call is about to start.

    Must be paired with a call to note_interactive_end() in a try/finally block.
    Increments the counter so the background loop yields.
    """
    global _interactive_pending
    _interactive_pending += 1


def note_interactive_end() -> None:
    """Signal that an interactive engine call has completed.

    Decrements the counter; clamped to 0 to guard against double-calls.
    """
    global _interactive_pending
    _interactive_pending = max(0, _interactive_pending - 1)


# ---------------------------------------------------------------------------
# Startup helper
# ---------------------------------------------------------------------------


def reset_stuck() -> int:
    """Flip any 'analyzing' rows → 'pending' (called at startup by T7).

    Returns the number of rows updated.
    """
    return storage.reset_analyzing_to_pending()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _wp_for_mover(cp_white: Optional[int], mate_white: Optional[int], turn: chess.Color) -> float:
    """Return win probability from the side-to-move's perspective."""
    wp_white = win_prob_white(cp_white, mate_white)
    return wp_white if turn == chess.WHITE else (1.0 - wp_white)


def _extract_cache_fields(
    results: list,  # list[AnalysisResult]
    board: chess.Board,
) -> dict[str, Any]:
    """Extract pos_cache fields from analyze_multi results (White-POV normalized)."""
    best = results[0]
    score_cp = pov_score_to_white_cp(best.score)

    # Determine if the score is a mate.
    white_score = best.score.white()
    if white_score.is_mate():
        eval_cp_white = None
        mate_white = white_score.mate()
    else:
        eval_cp_white = score_cp
        mate_white = None

    best_uci = best.pv[0].uci() if best.pv else None
    best_san = best.pv_san[0] if best.pv_san else None
    pv_san_json = json.dumps(best.pv_san) if best.pv_san else None

    # Second-best: centipawn score (White POV), if available.
    pv2_cp_white: Optional[int] = None
    if len(results) >= 2:
        second = results[1]
        second_white = second.score.white()
        if not second_white.is_mate():
            pv2 = second_white.score()
            if pv2 is not None:
                pv2_cp_white = int(pv2)
        else:
            # Mate in second line: map to a large cp value.
            pv2_cp_white = pov_score_to_white_cp(second.score)

    return {
        "eval_cp_white": eval_cp_white,
        "mate_white": mate_white,
        "best_uci": best_uci,
        "best_san": best_san,
        "pv_san_json": pv_san_json,
        "pv2_cp_white": pv2_cp_white,
    }


def _is_only_move(pv2_cp_white: Optional[int], eval_cp_white: Optional[int], turn: chess.Color) -> bool:
    """Return True when there is effectively only one good move (small gap to 2nd-best).

    An 'only-move' position is one where the gap between best and 2nd-best is
    large from the mover's perspective — meaning any other move would be bad.
    We use this to identify seed positions for the lead-in look-back.
    """
    if pv2_cp_white is None or eval_cp_white is None:
        return False
    # Gap from mover's perspective.
    if turn == chess.WHITE:
        gap = eval_cp_white - pv2_cp_white
    else:
        gap = pv2_cp_white - eval_cp_white
    # A small or negative gap means both moves are roughly equal — NOT an only-move.
    # A large positive gap means the 2nd-best is much worse → only-move situation.
    return gap > 100  # centipawns threshold


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------


async def analyze_game(
    game_id: int,
    engine: Any,
    *,
    depth: int = BACKGROUND_DEPTH,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> None:
    """Analyze a game ply-by-ply, writing pos_cache, game_plies, and leaks.

    Args:
        game_id: The game row id in storage.
        engine:  An engine instance (StockfishEngine or ScriptedEngine).
        depth:   Search depth for each position.
        progress_cb: Optional callback(current_ply, total_plies) called after
                 each ply is processed.

    Raises:
        EngineUnavailable: propagated as-is (caller sets 'failed' status).
        CancelledError:    propagated so the task can be cleanly cancelled.
    """
    # --- Load game + plies from storage ---
    game = storage.get_game(game_id)
    if game is None:
        logger.warning("review: game %d not found in storage", game_id)
        return

    pgn_plies = storage.get_plies(game_id)
    if not pgn_plies:
        logger.warning("review: game %d has no plies stored", game_id)
        storage.set_status(game_id, "done")
        return

    my_color_str: Optional[str] = game.get("my_color")  # 'white', 'black', or None
    my_color: Optional[chess.Color] = None
    if my_color_str == "white":
        my_color = chess.WHITE
    elif my_color_str == "black":
        my_color = chess.BLACK

    # --- Replay the game to build the board at each ply ---
    # We use the stored fen_before from the first ply to reconstruct the start position.
    # If the first ply has a FEN, use it; otherwise use the starting position.
    start_fen = pgn_plies[0]["fen_before"] if pgn_plies else chess.STARTING_FEN
    board = chess.Board(start_fen)

    total_plies = len(pgn_plies)

    # We need two passes:
    # Pass 1: analyze every position, fill pos_cache + game_plies data.
    # Pass 2: classify leaks + run threat probes.
    # To avoid replaying twice we store intermediate data in memory.

    # ply_data[i] = dict with fields for game_plies row + cached eval info
    ply_data: list[dict[str, Any]] = []
    # fen_list[i] = FEN before ply i (for null-move probe later)
    board_list: list[chess.Board] = []

    # --- Pass 1: analyze each position ---
    for i, ply_row in enumerate(pgn_plies):
        # Yield between plies so interactive requests can be scheduled.
        await asyncio.sleep(0)

        # Wait if an interactive request is pending (no-starve mechanism).
        while _interactive_pending > 0:
            await asyncio.sleep(_INTERACTIVE_POLL_INTERVAL)

        ply_num = ply_row["ply"]
        fen_before = ply_row.get("fen_before") or board.fen()
        uci_str = ply_row.get("uci")
        san_str = ply_row.get("san")

        # Snapshot the board before the move for motif detection later.
        board_snapshot = board.copy()
        board_list.append(board_snapshot)

        # Determine is_user_move
        is_user_move = (my_color is not None) and (board.turn == my_color)

        # EPD key (transposition-aware: move counters stripped).
        epd_key = board.epd()

        # Check pos_cache first.
        cached = storage.get_pos_cache(epd_key, depth)
        if cached is not None:
            eval_cp_white = cached["eval_cp_white"]
            mate_white = cached["mate_white"]
            best_uci = cached["best_uci"]
            best_san = cached["best_san"]
            pv2_cp_white = cached["pv2_cp_white"]
        else:
            # Cache miss: call the engine.
            results = await engine.analyze_multi(fen_before, depth, multipv=2)
            cf = _extract_cache_fields(results, board)
            eval_cp_white = cf["eval_cp_white"]
            mate_white = cf["mate_white"]
            best_uci = cf["best_uci"]
            best_san = cf["best_san"]
            pv2_cp_white = cf["pv2_cp_white"]

            storage.upsert_pos_cache(
                epd_key=epd_key,
                depth=depth,
                eval_cp_white=eval_cp_white,
                mate_white=mate_white,
                best_uci=best_uci,
                best_san=best_san,
                pv_san_json=cf["pv_san_json"],
                pv2_cp_white=pv2_cp_white,
            )

        # Win probability for the side to move (before this ply's move).
        turn_before = board.turn
        wp_before = _wp_for_mover(eval_cp_white, mate_white, turn_before)

        ply_data.append({
            "ply": ply_num,
            "san": san_str,
            "uci": uci_str,
            "fen_before": fen_before,
            "eval_cp_white": eval_cp_white,
            "mate_white": mate_white,
            "win_prob": wp_before,
            "is_user_move": is_user_move,
            "clock_centis": ply_row.get("clock_centis"),
            # Extra fields for leak classification (not written to game_plies).
            "_turn_before": turn_before,
            "_wp_before": wp_before,
            "_best_uci": best_uci,
            "_best_san": best_san,
            "_pv2_cp_white": pv2_cp_white,
            "_epd_key": epd_key,
            "_board_idx": i,
        })

        # Advance the board.
        if uci_str:
            try:
                move = chess.Move.from_uci(uci_str)
                board.push(move)
            except (ValueError, chess.IllegalMoveError, chess.AmbiguousMoveError):
                logger.warning(
                    "review: game %d ply %d: invalid UCI %r, stopping replay",
                    game_id, ply_num, uci_str,
                )
                break

        if progress_cb is not None:
            progress_cb(i + 1, total_plies)

    # --- After the full game is analyzed, get the post-game eval for win-prob-after ---
    # For each ply we need the eval of the position AFTER the move (= ply_data[i+1]'s
    # eval_before), so we can compute win-prob-drop from the mover's POV.

    # --- Pass 2: classify leaks (user-color only) ---
    leaks: list[LeakRecord] = []

    for i, pd in enumerate(ply_data):
        if not pd["is_user_move"]:
            continue  # Only classify user moves

        # Win-prob BEFORE the move (mover's POV = already computed).
        wp_before = pd["_wp_before"]

        # Win-prob AFTER: use the next ply's eval, flipped for the opponent's turn.
        if i + 1 < len(ply_data):
            next_pd = ply_data[i + 1]
            # After the user's move, it's the opponent's turn.
            # next_pd["win_prob"] is for the opponent (side to move after our move).
            # We want the user's win-prob after their move = 1 - opponent's wp.
            wp_after = 1.0 - next_pd["win_prob"]
        else:
            # Last ply: no next position; use same eval (no drop).
            wp_after = wp_before

        drop = max(0.0, wp_before - wp_after)
        sev = leak_severity(drop)

        if sev not in ("mistake", "blunder"):
            continue  # Skip inaccuracies, good, and best moves.

        # Determine game phase.
        board_at_ply = board_list[pd["_board_idx"]]
        phase = game_phase(board_at_ply)
        ply_num = pd["ply"]

        # --- Lead-in localization ---
        # Look back up to _LEAD_IN_LOOKBACK plies for the earliest still-OK
        # position that already contained the seed (an only-move or standing threat).
        lead_in_ply = ply_num - 1  # default: one ply before the blunder

        for lookback in range(1, _LEAD_IN_LOOKBACK + 1):
            check_idx = i - lookback
            if check_idx < 0:
                break
            check_pd = ply_data[check_idx]

            # Is it still an OK position for the user?
            # OK = the user's win-prob before that ply was not already lost.
            if check_pd["_wp_before"] < 0.35:
                break  # Already bad for user — don't look further back.

            # Does the position contain the seed?
            # Seed = an only-move situation (large gap between best and 2nd-best).
            is_seed = _is_only_move(
                check_pd["_pv2_cp_white"],
                check_pd["eval_cp_white"],
                check_pd["_turn_before"],
            )

            if is_seed:
                lead_in_ply = check_pd["ply"]
                # Keep looking back for an earlier seed.

        # --- Threat probe (null-move) at the LEAK position ---
        # We probe at the board position just before the user's blunder move
        # (the "leak board"), where the USER is to move.  After a null move,
        # it becomes the OPPONENT's turn — so the engine's best reply is the
        # opponent's threat.  lead_in_ply is kept only for display timing;
        # it does NOT govern which board is probed here.
        leak_board = board_list[pd["_board_idx"]].copy()

        threat_uci: Optional[str] = None
        threat_motif: Optional[str] = None
        hung_square: Optional[str] = None
        motif_json: Optional[str] = None
        tags: list[str] = []

        # Only do null-move probe when NOT in check (can't push null in check).
        if not leak_board.is_check():
            await asyncio.sleep(0)
            while _interactive_pending > 0:
                await asyncio.sleep(_INTERACTIVE_POLL_INTERVAL)

            try:
                # Push a null move: now it is the OPPONENT's turn.
                null_board = leak_board.copy()
                null_board.push(chess.Move.null())
                null_fen = null_board.fen()

                threat_results = await engine.analyze_multi(null_fen, depth, multipv=1)
                if threat_results and threat_results[0].pv:
                    threat_move = threat_results[0].pv[0]
                    threat_uci = threat_move.uci()

                    # Apply the opponent's threat move to get the after-threat board.
                    after_board = null_board.copy()
                    try:
                        after_board.push(threat_move)
                    except Exception:
                        after_board = null_board.copy()

                    # Check for checkmate first (highest priority).
                    if after_board.is_checkmate():
                        threat_motif = "mate"
                        motif_json = json.dumps([{"type": "mate", "uci": threat_uci}])
                        tags = ["mate"]
                    else:
                        # Detect motifs from the OPPONENT's perspective.
                        # null_board.turn is the OPPONENT (they are about to move).
                        # detect_motifs(null_board) reports threats the opponent
                        # (side to move in null_board) can execute.
                        threat_motifs = motifs.detect_motifs(null_board)
                        # hung pieces: user's pieces left hanging after the threat move.
                        # after_board.turn is the USER, so we check the USER's color.
                        hung = motifs.hanging_pieces(after_board, my_color)

                        if threat_motifs:
                            primary = threat_motifs[0]
                            threat_motif = primary["type"]
                            motif_json = json.dumps(threat_motifs)
                            tags = [m["type"] for m in threat_motifs]
                            if hung:
                                hung_square = chess.square_name(hung[0]["square"])
                        elif hung:
                            threat_motif = "hanging"
                            hung_square = chess.square_name(hung[0]["square"])
                            motif_json = json.dumps(hung)
                            tags = ["hanging"]
            except Exception as exc:
                logger.warning(
                    "review: game %d ply %d: threat probe failed: %s",
                    game_id, ply_num, exc,
                )

        # Determine primary category.
        if threat_motif == "mate":
            category = "mate"
        elif threat_motif:
            category = threat_motif
        elif hung_square:
            category = "hanging"
        else:
            category = "missed_threat"

        color_str = "white" if my_color == chess.WHITE else "black"

        leak = LeakRecord(
            game_id=game_id,
            ply=ply_num,
            color=color_str,
            severity=sev,
            category=category,
            phase=phase,
            win_prob_before=wp_before,
            win_prob_after=wp_after,
            win_prob_drop=drop,
            motif_json=motif_json,
            hung_square=hung_square,
            threat_uci=threat_uci,
            threat_motif=threat_motif,
            best_uci=pd["_best_uci"],
            best_san=pd["_best_san"],
            lead_in_ply=lead_in_ply,
            tags_json=json.dumps(tags) if tags else None,
        )
        leaks.append(leak)

    # --- Write game_plies ---
    plies_to_write = [
        {
            "ply": pd["ply"],
            "san": pd["san"],
            "uci": pd["uci"],
            "fen_before": pd["fen_before"],
            "eval_cp_white": pd["eval_cp_white"],
            "mate_white": pd["mate_white"],
            "win_prob": pd["win_prob"],
            "is_user_move": pd["is_user_move"],
            "clock_centis": pd["clock_centis"],
        }
        for pd in ply_data
    ]
    storage.write_plies(game_id, plies_to_write)

    # --- Write leaks ---
    storage.write_leaks(game_id, leaks)

    # --- Set status done ---
    storage.set_status(game_id, "done")
    logger.info("review: game %d analysis complete (%d leaks)", game_id, len(leaks))


# ---------------------------------------------------------------------------
# Background job lifecycle
# ---------------------------------------------------------------------------


def start_analysis(
    game_id: int,
    engine: Any,
    **kw: Any,
) -> "asyncio.Task[None]":
    """Create and track a background analysis task.

    Sets the game status to 'analyzing' immediately, then starts the task.
    The task removes itself from the registry on completion and sets 'done'
    or 'failed' as appropriate.

    Args:
        game_id: The game to analyze.
        engine:  Engine instance (StockfishEngine or ScriptedEngine).
        **kw:    Forwarded to analyze_game (e.g. depth=).

    Returns:
        The running asyncio.Task.
    """
    from app.engine import EngineUnavailable

    # Cancel any existing task for this game.
    cancel_analysis(game_id)

    # Mark as analyzing immediately (before the task body runs).
    try:
        storage.set_status(game_id, "analyzing")
    except Exception as exc:
        logger.warning("review: could not set status for game %d: %s", game_id, exc)

    async def _body() -> None:
        try:
            await analyze_game(game_id, engine, **kw)
        except asyncio.CancelledError:
            logger.info("review: game %d analysis cancelled", game_id)
            raise
        except EngineUnavailable as exc:
            logger.error("review: game %d engine unavailable: %s", game_id, exc)
            try:
                storage.set_status(game_id, "failed")
            except Exception:
                pass
        except Exception as exc:
            logger.error("review: game %d unexpected error: %s", game_id, exc, exc_info=True)
            try:
                storage.set_status(game_id, "failed")
            except Exception:
                pass
        finally:
            if _tasks.get(game_id) is task:
                _tasks.pop(game_id, None)

    task: "asyncio.Task[None]" = asyncio.create_task(_body(), name=f"review-{game_id}")
    _tasks[game_id] = task
    return task


def cancel_analysis(game_id: int) -> bool:
    """Cancel the tracked background task for a game.

    Args:
        game_id: The game whose analysis task to cancel.

    Returns:
        True if a running task was cancelled, False if none was registered.
    """
    task = _tasks.get(game_id)
    if task is None:
        return False
    if not task.done():
        task.cancel()
    _tasks.pop(game_id, None)
    return True
