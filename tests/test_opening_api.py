"""API tests for the opening-trainer endpoints (no Stockfish needed).

The opening index is repopulated from the test fixture TSV after the app's
lifespan runs (production data isn't present in CI/sandbox), so these tests are
independent of the downloaded data set. Commentary uses the real bundled file.
"""

from __future__ import annotations

import json
from pathlib import Path

import chess
import pytest
from fastapi.testclient import TestClient

from app import openings
from app.main import app

FIXTURE_DIR = str(Path(__file__).parent / "fixtures")
COMMENTARY_FILE = Path(__file__).parent.parent / "data" / "commentary.json"

START = chess.STARTING_FEN


@pytest.fixture
def client():
    with TestClient(app) as c:
        # Repopulate the module index from the fixture (lifespan may have loaded
        # an empty index when production data/openings/ is absent).
        openings.load(FIXTURE_DIR)
        yield c


def test_opening_identify_ruy_lopez(client):
    # 1.e4 e5 2.Nf3 Nc6 3.Bb5 — Ruy Lopez (C60) is in the fixture.
    r = client.post("/api/opening", json={
        "baseFen": START,
        "moves": ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["current"] is not None
    assert "Ruy Lopez" in body["current"]["name"]
    assert body["current"]["eco"] == "C60"


def test_opening_identify_transposition(client):
    # Same position via a different move order → identical detection.
    direct = client.post("/api/opening", json={
        "baseFen": START, "moves": ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5"],
    }).json()["current"]
    transposed = client.post("/api/opening", json={
        "baseFen": START, "moves": ["g1f3", "b8c6", "e2e4", "e7e5", "f1b5"],
    }).json()["current"]
    assert direct == transposed
    assert direct is not None


def test_opening_candidates_shape_and_filter(client):
    r = client.post("/api/opening", json={"baseFen": START, "moves": ["e2e4"]})
    body = r.json()
    assert isinstance(body["candidates"], list)
    assert isinstance(body["truncated"], bool)
    for item in body["candidates"]:
        assert {"eco", "name", "uci", "san"} <= item.keys()

    # Name filter narrows the list.
    filtered = client.post("/api/opening", json={
        "baseFen": START, "moves": ["e2e4"], "q": "ruy",
    }).json()
    assert all("ruy" in i["name"].lower() for i in filtered["candidates"])


def test_commentary_hit_and_miss(client):
    # Pick a real EPD from the bundled commentary file and round-trip it.
    data = json.loads(COMMENTARY_FILE.read_text())
    sample_epd = next(iter(data))
    fen = sample_epd + " 0 1"  # EPD + clocks = a valid FEN for the same position
    hit = client.post("/api/opening/commentary", json={"fen": fen}).json()
    assert hit is not None
    assert hit["text"] and hit["san"]

    # A position with no commentary → null.
    miss = client.post("/api/opening/commentary", json={"fen": START}).json()
    assert miss is None


def test_opening_degraded_when_data_absent(client):
    # Point the index at a nonexistent dir → empty, well-formed response (no 500).
    openings.load("/nonexistent-openings-dir")
    r = client.post("/api/opening", json={
        "baseFen": START, "moves": ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body == {"current": None, "candidates": [], "truncated": False}


def test_opening_handles_malformed_line(client):
    # An illegal/garbage move must not 500 — detection just stops there.
    r = client.post("/api/opening", json={
        "baseFen": START, "moves": ["e2e4", "zzzz"],
    })
    assert r.status_code == 200
    assert "candidates" in r.json()
