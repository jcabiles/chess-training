"""API tests for the opening-traps endpoints (no Stockfish needed, no live server).

Uses the production data/traps.json (which includes the Stafford Gambit), reloaded
from the fixture path in the client fixture so tests are independent of lifespan
ordering. Mirrors the style of test_opening_api.py.
"""

from __future__ import annotations

from pathlib import Path

import chess
import pytest
from fastapi.testclient import TestClient

from app import traps
from app.main import app

PRODUCTION_TRAPS = str(Path(__file__).parent.parent / "data" / "traps.json")
START = chess.STARTING_FEN

# UCI for: 1.e4 e5 2.Nf3 Nf6 3.Nxe5 Nc6  (Stafford Gambit lead-in)
STAFFORD_MOVES = ["e2e4", "e7e5", "g1f3", "g8f6", "f3e5", "b8c6"]


@pytest.fixture
def client():
    with TestClient(app) as c:
        # Reload from the production traps file so tests are independent of the
        # lifespan execution context (CI may not have run lifespan at all).
        traps.load(PRODUCTION_TRAPS)
        yield c


# ---------------------------------------------------------------------------
# GET /api/traps — browse list
# ---------------------------------------------------------------------------


def test_list_traps_returns_non_empty(client):
    """GET /api/traps returns a non-empty list of summaries from production data."""
    r = client.get("/api/traps")
    assert r.status_code == 200
    body = r.json()
    assert "traps" in body
    assert isinstance(body["traps"], list)
    assert len(body["traps"]) > 0


def test_list_traps_summary_shape(client):
    """Each entry in GET /api/traps has the required summary keys."""
    r = client.get("/api/traps")
    body = r.json()
    required = {"id", "name", "color", "eco", "parentOpening", "commonness"}
    for item in body["traps"]:
        assert required <= set(item.keys()), (
            f"Summary missing keys: {required - set(item.keys())}"
        )


def test_list_traps_degraded_when_no_data(client):
    """GET /api/traps returns {traps:[]} when no data is loaded (never 500)."""
    traps.load("/nonexistent-traps-for-api-test.json")
    r = client.get("/api/traps")
    assert r.status_code == 200
    assert r.json() == {"traps": []}


# ---------------------------------------------------------------------------
# POST /api/traps/check — live position matching
# ---------------------------------------------------------------------------


def test_check_traps_stafford_gambit(client):
    """POST /api/traps/check returns the Stafford Gambit after its lead-in moves."""
    r = client.post("/api/traps/check", json={
        "baseFen": START,
        "moves": STAFFORD_MOVES,
    })
    assert r.status_code == 200
    body = r.json()
    assert "available" in body
    ids = [item["id"] for item in body["available"]]
    assert "stafford-gambit" in ids, (
        f"Expected 'stafford-gambit' in available traps after lead-in moves, got: {ids}"
    )


def test_check_traps_empty_from_start_position(client):
    """POST /api/traps/check returns {available:[]} from the starting position."""
    r = client.post("/api/traps/check", json={
        "baseFen": START,
        "moves": [],
    })
    assert r.status_code == 200
    assert r.json() == {"available": []}


def test_check_traps_available_summary_shape(client):
    """Each entry in POST /api/traps/check has the required summary keys."""
    r = client.post("/api/traps/check", json={
        "baseFen": START,
        "moves": STAFFORD_MOVES,
    })
    body = r.json()
    required = {"id", "name", "color", "eco", "parentOpening", "commonness"}
    for item in body["available"]:
        assert required <= set(item.keys()), (
            f"Available item missing keys: {required - set(item.keys())}"
        )


def test_check_traps_degraded_when_no_data(client):
    """POST /api/traps/check returns {available:[]} when no data is loaded (never 500)."""
    traps.load("/nonexistent-traps-for-api-test.json")
    r = client.post("/api/traps/check", json={
        "baseFen": START,
        "moves": STAFFORD_MOVES,
    })
    assert r.status_code == 200
    assert r.json() == {"available": []}


# ---------------------------------------------------------------------------
# GET /api/traps/{trap_id} — full trap object
# ---------------------------------------------------------------------------


def test_get_trap_returns_full_object(client):
    """GET /api/traps/{id} returns the full trap object for a real id."""
    r = client.get("/api/traps/stafford-gambit")
    assert r.status_code == 200
    body = r.json()
    required = {"id", "name", "color", "eco", "parentOpening", "commonness",
                "startFen", "leadInSan", "variations"}
    assert required <= set(body.keys()), (
        f"Full trap missing keys: {required - set(body.keys())}"
    )
    assert body["id"] == "stafford-gambit"


def test_get_trap_404_for_unknown_id(client):
    """GET /api/traps/{id} returns 404 for an unknown trap id."""
    r = client.get("/api/traps/nonexistent")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Route-order test: POST /api/traps/check must NOT be shadowed by
# GET /api/traps/{trap_id} (literal path registered before the wildcard)
# ---------------------------------------------------------------------------


def test_route_order_check_not_shadowed_by_trap_id(client):
    """POST /api/traps/check returns the 'available' shape, NOT a trap object.

    This proves the literal POST /api/traps/check route is matched before the
    wildcard GET /api/traps/{trap_id} route — FastAPI is not treating 'check'
    as a trap_id.
    """
    r = client.post("/api/traps/check", json={
        "baseFen": START,
        "moves": STAFFORD_MOVES,
    })
    assert r.status_code == 200
    body = r.json()
    # Must have the "available" key (traps/check shape), NOT "id"/"variations" (trap shape)
    assert "available" in body, (
        f"Expected 'available' key (check route), got keys: {list(body.keys())}"
    )
    assert "id" not in body, (
        "Got a trap object instead of the check response — route ordering is wrong"
    )


def test_route_order_real_trap_id_still_resolves(client):
    """GET /api/traps/<real-id> still returns the trap correctly after check route is registered."""
    r = client.get("/api/traps/stafford-gambit")
    assert r.status_code == 200
    body = r.json()
    assert body.get("id") == "stafford-gambit"
    assert "variations" in body
