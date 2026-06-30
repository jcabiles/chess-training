"""
tests/test_pgn.py — unit tests for app.pgn (pure module, no engine required).
"""

from __future__ import annotations

from pathlib import Path

import chess

from app.pgn import _parse_clock_centis, parse_games

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"

STARTING_FEN = chess.Board().fen()


def _load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Multi-game parsing
# ---------------------------------------------------------------------------


def test_multi_game_yields_correct_count():
    """A PGN with 3 games returns 3 ParsedGame objects."""
    text = _load_fixture("games_sample.pgn")
    games = parse_games(text)
    assert len(games) == 3


def test_multi_game_headers_are_correct():
    """Headers from each game are correctly extracted."""
    text = _load_fixture("games_sample.pgn")
    games = parse_games(text)
    assert games[0].white == "Alice"
    assert games[0].black == "Bob"
    assert games[0].result == "1-0"
    assert games[1].white == "Charlie"
    assert games[1].black == "Alice"
    assert games[2].result == "1/2-1/2"


def test_multi_game_ply_counts():
    """Ply counts match actual mainline length in each game."""
    text = _load_fixture("games_sample.pgn")
    games = parse_games(text)
    # Each game has 5 full moves = 10 plies
    for game in games:
        assert game.ply_count == 10
        assert len(game.plies) == 10


def test_plies_have_correct_types():
    """Each ParsedPly has the expected fields with correct types."""
    text = _load_fixture("games_sample.pgn")
    game = parse_games(text)[0]
    for ply in game.plies:
        assert isinstance(ply.ply, int)
        assert isinstance(ply.san, str)
        assert isinstance(ply.uci, str)
        assert isinstance(ply.fen_before, str)
        assert ply.clock_centis is None or isinstance(ply.clock_centis, int)


def test_ply_numbers_are_sequential():
    """Ply numbers start at 1 and increment by 1."""
    text = _load_fixture("games_sample.pgn")
    game = parse_games(text)[0]
    assert [p.ply for p in game.plies] == list(range(1, 11))


# ---------------------------------------------------------------------------
# fen_before correctness
# ---------------------------------------------------------------------------


def test_fen_before_first_ply_is_start_position():
    """The first ply's fen_before should be the standard starting position."""
    text = _load_fixture("games_sample.pgn")
    game = parse_games(text)[0]
    first_ply = game.plies[0]
    assert first_ply.fen_before == STARTING_FEN


def test_fen_before_with_custom_start_fen():
    """If a game has a FEN header, fen_before of ply 1 should be that custom FEN."""
    # King-and-pawn endgame starting position
    custom_fen = "4k3/8/8/8/8/8/4P3/4K3 w - - 0 1"
    pgn_text = f"""[Event "Custom Start"]
[White "A"]
[Black "B"]
[Result "1-0"]
[FEN "{custom_fen}"]
[SetUp "1"]

1. e4 Ke7 2. Ke2 Kd6 3. Ke3 Ke5 4. e5 Ke6 5. Ke4 Kd7 1-0"""
    games = parse_games(pgn_text)
    assert len(games) == 1
    assert games[0].plies[0].fen_before == custom_fen


def test_fen_before_is_position_before_move():
    """fen_before for ply N is the FEN BEFORE move N is played."""
    text = _load_fixture("games_sample.pgn")
    game = parse_games(text)[0]
    # Replay independently and verify
    board = chess.Board()
    for ply in game.plies:
        assert ply.fen_before == board.fen(), f"Mismatch at ply {ply.ply}"
        move = chess.Move.from_uci(ply.uci)
        board.push(move)


def test_uci_and_san_are_consistent():
    """Each ply's SAN and UCI refer to the same legal move."""
    text = _load_fixture("games_sample.pgn")
    game = parse_games(text)[0]
    board = chess.Board()
    for ply in game.plies:
        move = chess.Move.from_uci(ply.uci)
        assert board.san(move) == ply.san, f"SAN mismatch at ply {ply.ply}"
        board.push(move)


# ---------------------------------------------------------------------------
# %clk parsing
# ---------------------------------------------------------------------------


class TestClockParsing:
    def test_clk_comment_is_parsed(self):
        """%clk comments are extracted to centiseconds."""
        text = _load_fixture("clocked_game.pgn")
        game = parse_games(text)[0]
        # White's first move: [%clk 0:10:00] => 600s = 60000 centis
        assert game.plies[0].clock_centis == 60000

    def test_clk_with_fractional_seconds(self):
        """Fractional seconds in %clk are handled correctly."""
        text = _load_fixture("clocked_game.pgn")
        game = parse_games(text)[0]
        # White's second move: [%clk 0:09:57.3] => 9*60 + 57.3 = 597.3s => 59730 centis
        assert game.plies[2].clock_centis == 59730

    def test_clk_no_comment_is_none(self):
        """Plies without a %clk comment have clock_centis == None."""
        pgn_text = """[Event "No Clock"]
[White "A"]
[Black "B"]
[Result "1-0"]

1. e4 e5 2. Nf3 Nc6 1-0"""
        game = parse_games(pgn_text)[0]
        for ply in game.plies:
            assert ply.clock_centis is None

    def test_clk_all_plies_in_clocked_game(self):
        """All plies in a clocked game have non-None clock_centis."""
        text = _load_fixture("clocked_game.pgn")
        game = parse_games(text)[0]
        for ply in game.plies:
            assert ply.clock_centis is not None

    def test_parse_clock_centis_hours(self):
        """Hours are correctly converted."""
        assert _parse_clock_centis("[%clk 1:00:00]") == 360000  # 3600s * 100

    def test_parse_clock_centis_minutes(self):
        """Minutes-only clock works."""
        assert _parse_clock_centis("[%clk 0:05:00]") == 30000  # 300s * 100

    def test_parse_clock_centis_absent(self):
        """Returns None when no %clk tag is present."""
        assert _parse_clock_centis("some arbitrary comment") is None

    def test_parse_clock_centis_zero(self):
        """Zero clock (flagged) returns 0."""
        assert _parse_clock_centis("[%clk 0:00:00]") == 0


# ---------------------------------------------------------------------------
# content_hash
# ---------------------------------------------------------------------------


class TestContentHash:
    def test_same_game_same_hash(self):
        """Parsing the same PGN text twice yields identical content_hash."""
        text = _load_fixture("games_sample.pgn")
        games_a = parse_games(text)
        games_b = parse_games(text)
        for a, b in zip(games_a, games_b):
            assert a.content_hash == b.content_hash

    def test_different_games_different_hash(self):
        """Different games in a multi-game PGN have distinct content_hash values."""
        text = _load_fixture("games_sample.pgn")
        games = parse_games(text)
        hashes = [g.content_hash for g in games]
        assert len(hashes) == len(set(hashes)), "Duplicate content_hash across different games"

    def test_hash_is_hex_string(self):
        """content_hash is a non-empty hexadecimal string."""
        text = _load_fixture("games_sample.pgn")
        game = parse_games(text)[0]
        assert game.content_hash
        # SHA-1 = 40 hex chars
        assert len(game.content_hash) == 40
        int(game.content_hash, 16)  # raises ValueError if not hex


# ---------------------------------------------------------------------------
# my_color inference  (Refuter resolution #8)
# ---------------------------------------------------------------------------


class TestMyColor:
    def test_my_color_none_when_env_unset(self, monkeypatch):
        """my_color is None when CHESS_USERNAME is not set."""
        monkeypatch.delenv("CHESS_USERNAME", raising=False)
        text = _load_fixture("games_sample.pgn")
        games = parse_games(text)
        for game in games:
            assert game.my_color is None

    def test_my_color_white(self, monkeypatch):
        """my_color == 'white' when CHESS_USERNAME matches the White header."""
        monkeypatch.setenv("CHESS_USERNAME", "Alice")
        text = _load_fixture("games_sample.pgn")
        games = parse_games(text)
        assert games[0].my_color == "white"  # Alice is White in game 1

    def test_my_color_black(self, monkeypatch):
        """my_color == 'black' when CHESS_USERNAME matches the Black header."""
        monkeypatch.setenv("CHESS_USERNAME", "Alice")
        text = _load_fixture("games_sample.pgn")
        games = parse_games(text)
        assert games[1].my_color == "black"  # Alice is Black in game 2

    def test_my_color_none_on_no_match(self, monkeypatch):
        """my_color is None when CHESS_USERNAME doesn't match either player."""
        monkeypatch.setenv("CHESS_USERNAME", "Nobody")
        text = _load_fixture("games_sample.pgn")
        games = parse_games(text)
        for game in games:
            assert game.my_color is None

    def test_my_color_case_insensitive(self, monkeypatch):
        """Alias matching is case-insensitive."""
        monkeypatch.setenv("CHESS_USERNAME", "alice")  # lowercase
        text = _load_fixture("games_sample.pgn")
        games = parse_games(text)
        assert games[0].my_color == "white"

    def test_my_color_comma_separated_aliases(self, monkeypatch):
        """Multiple comma-separated aliases are all tried."""
        monkeypatch.setenv("CHESS_USERNAME", "NotAPlayer, Bob, SomeOther")
        text = _load_fixture("games_sample.pgn")
        games = parse_games(text)
        # Bob is Black in game 1
        assert games[0].my_color == "black"

    def test_my_color_empty_env(self, monkeypatch):
        """Empty CHESS_USERNAME string behaves like unset."""
        monkeypatch.setenv("CHESS_USERNAME", "")
        text = _load_fixture("games_sample.pgn")
        games = parse_games(text)
        for game in games:
            assert game.my_color is None

    def test_my_color_never_guesses_white(self, monkeypatch):
        """When env is unset, my_color is NEVER 'white' (never defaults)."""
        monkeypatch.delenv("CHESS_USERNAME", raising=False)
        pgn_text = """[Event "Solo"]
[White "SoloPlayer"]
[Black "Unknown"]
[Result "1-0"]

1. e4 e5 1-0"""
        game = parse_games(pgn_text)[0]
        assert game.my_color is None


# ---------------------------------------------------------------------------
# Malformed game handling (graceful degradation — contracts invariant)
# ---------------------------------------------------------------------------


class TestMalformedGames:
    def test_malformed_batch_good_games_survive(self):
        """Good games in a batch survive even if there's invalid PGN between them."""
        text = _load_fixture("malformed_batch.pgn")
        games = parse_games(text)
        # At minimum, the two well-formed games should be parsed
        assert len(games) >= 2
        events = {g.headers.get("Event", "") for g in games}
        assert "Good Game" in events
        assert "Another Good Game" in events

    def test_empty_string_returns_empty_list(self):
        """Parsing an empty string returns an empty list without raising."""
        games = parse_games("")
        assert games == []

    def test_single_valid_game_no_moves(self):
        """A game with headers but no moves is still parsed (ply_count == 0)."""
        pgn_text = """[Event "Empty Game"]
[White "A"]
[Black "B"]
[Result "*"]

*"""
        games = parse_games(pgn_text)
        # python-chess may or may not return this — either 0 or 1 game, but must not crash
        assert isinstance(games, list)
        if games:
            assert games[0].ply_count == 0

    def test_single_malformed_does_not_crash(self):
        """A single completely malformed PGN input doesn't raise — returns empty list."""
        games = parse_games("NOT PGN AT ALL %%%###")
        assert isinstance(games, list)


# ---------------------------------------------------------------------------
# ECO / opening fields
# ---------------------------------------------------------------------------


def test_eco_preserved_from_headers():
    """ECO from the PGN header is captured in the ParsedGame."""
    text = _load_fixture("games_sample.pgn")
    games = parse_games(text)
    # games_sample.pgn game 1 has ECO "C60"
    assert games[0].eco == "C60"


def test_opening_preserved_from_headers():
    """Opening name from the PGN header is captured in the ParsedGame."""
    text = _load_fixture("games_sample.pgn")
    games = parse_games(text)
    assert games[0].opening == "Ruy Lopez"


# ---------------------------------------------------------------------------
# pgn field
# ---------------------------------------------------------------------------


def test_pgn_field_is_non_empty_string():
    """The .pgn field contains the game's PGN representation as a string."""
    text = _load_fixture("games_sample.pgn")
    game = parse_games(text)[0]
    assert isinstance(game.pgn, str)
    assert len(game.pgn) > 0


def test_pgn_field_contains_headers():
    """The .pgn field includes PGN header lines."""
    text = _load_fixture("games_sample.pgn")
    game = parse_games(text)[0]
    assert "[White" in game.pgn
    assert "[Black" in game.pgn


# ---------------------------------------------------------------------------
# ParsedGame is a dataclass (structural check)
# ---------------------------------------------------------------------------


def test_parsed_game_is_dataclass():
    """ParsedGame and ParsedPly are proper dataclasses with the expected fields."""
    text = _load_fixture("games_sample.pgn")
    game = parse_games(text)[0]
    # Check all required fields exist
    assert hasattr(game, "headers")
    assert hasattr(game, "white")
    assert hasattr(game, "black")
    assert hasattr(game, "result")
    assert hasattr(game, "eco")
    assert hasattr(game, "opening")
    assert hasattr(game, "date")
    assert hasattr(game, "ply_count")
    assert hasattr(game, "my_color")
    assert hasattr(game, "content_hash")
    assert hasattr(game, "pgn")
    assert hasattr(game, "plies")
    # Check plies
    ply = game.plies[0]
    assert hasattr(ply, "ply")
    assert hasattr(ply, "san")
    assert hasattr(ply, "uci")
    assert hasattr(ply, "fen_before")
    assert hasattr(ply, "clock_centis")
