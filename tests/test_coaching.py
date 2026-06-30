"""
tests/test_coaching.py — Unit tests for app/coaching.py.

All tests are pure (no engine, no DB, no network, no anthropic import).
"""

from __future__ import annotations

import importlib

from app import coaching, storage
from app.coaching import TemplateNarrator, get_narrator

# ---------------------------------------------------------------------------
# Helper: build a minimal LeakRecord for a given category.
# ---------------------------------------------------------------------------

def _leak(
    category: str,
    *,
    hung_square: str | None = "e4",
    threat_motif: str | None = None,
    threat_uci: str | None = None,
    best_san: str | None = "Nf3",
    phase: str = "middlegame",
    severity: str = "blunder",
    win_prob_before: float = 0.65,
    win_prob_after: float = 0.35,
) -> storage.LeakRecord:
    return storage.LeakRecord(
        game_id=1,
        ply=20,
        color="white",
        severity=severity,
        category=category,
        phase=phase,
        win_prob_before=win_prob_before,
        win_prob_after=win_prob_after,
        win_prob_drop=win_prob_before - win_prob_after,
        hung_square=hung_square,
        threat_uci=threat_uci,
        threat_motif=threat_motif,
        best_uci="g1f3",
        best_san=best_san,
    )


def _leak_dict(category: str, **kwargs) -> dict:
    """Return a plain dict (as storage.get_leaks returns)."""
    rec = _leak(category, **kwargs)
    return {
        "id": 1,
        "game_id": rec.game_id,
        "ply": rec.ply,
        "color": rec.color,
        "severity": rec.severity,
        "category": rec.category,
        "motif_json": rec.motif_json,
        "phase": rec.phase,
        "win_prob_before": rec.win_prob_before,
        "win_prob_after": rec.win_prob_after,
        "win_prob_drop": rec.win_prob_drop,
        "hung_square": rec.hung_square,
        "threat_uci": rec.threat_uci,
        "threat_motif": rec.threat_motif,
        "best_uci": rec.best_uci,
        "best_san": rec.best_san,
        "lead_in_ply": rec.lead_in_ply,
        "tags_json": rec.tags_json,
        "explanation_json": rec.explanation_json,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MOTIF_CATEGORIES = [
    "hanging",
    "knight_fork",
    "fork",
    "pin",
    "skewer",
    "discovered",
    "back_rank",
    "missed_threat",
    "mate",
]


# ---------------------------------------------------------------------------
# TemplateNarrator — narrate_leak
# ---------------------------------------------------------------------------

class TestNarrateLeakBuckets:
    """Each motif category produces non-empty, sensible bucket text."""

    def _assert_sensible(self, result: dict, category: str) -> None:
        assert isinstance(result, dict), f"narrate_leak must return a dict for {category}"
        assert set(result.keys()) == {"threat", "hanging", "plan", "summary"}
        # summary is always non-empty
        assert result["summary"], f"summary must be non-empty for {category}"
        assert len(result["summary"]) > 10, f"summary too short for {category}"
        # At least one of threat/hanging/plan should be non-empty for all motif categories
        has_content = any(v for v in (result["threat"], result["hanging"], result["plan"]))
        assert has_content, f"At least one bucket must be non-empty for {category}"

    def test_hanging(self):
        narrator = TemplateNarrator()
        result = narrator.narrate_leak(_leak("hanging", hung_square="d5"))
        self._assert_sensible(result, "hanging")
        # Hanging text should mention the square
        assert "d5" in (result["threat"] or "") or "d5" in (result["hanging"] or "")

    def test_knight_fork(self):
        narrator = TemplateNarrator()
        result = narrator.narrate_leak(_leak("knight_fork", threat_uci="d5e7"))
        self._assert_sensible(result, "knight_fork")
        assert result["threat"] is not None
        assert "fork" in result["threat"].lower() or "knight" in result["threat"].lower()

    def test_fork(self):
        narrator = TemplateNarrator()
        result = narrator.narrate_leak(_leak("fork", threat_uci="d5f6"))
        self._assert_sensible(result, "fork")
        assert result["threat"] is not None

    def test_pin(self):
        narrator = TemplateNarrator()
        result = narrator.narrate_leak(_leak("pin", hung_square="f6"))
        self._assert_sensible(result, "pin")
        assert result["hanging"] is not None
        assert "pin" in result["hanging"].lower()

    def test_skewer(self):
        narrator = TemplateNarrator()
        result = narrator.narrate_leak(_leak("skewer", hung_square="e8"))
        self._assert_sensible(result, "skewer")
        assert result["hanging"] is not None
        assert "skewer" in result["hanging"].lower() or "skewer" in (result["threat"] or "").lower()

    def test_discovered(self):
        narrator = TemplateNarrator()
        result = narrator.narrate_leak(_leak("discovered", threat_uci="b2h8"))
        self._assert_sensible(result, "discovered")
        assert result["threat"] is not None
        assert "discover" in result["threat"].lower()

    def test_back_rank(self):
        narrator = TemplateNarrator()
        result = narrator.narrate_leak(_leak("back_rank", hung_square=None, threat_uci=None))
        self._assert_sensible(result, "back_rank")
        assert result["threat"] is not None
        assert "back" in result["threat"].lower() or "rank" in result["threat"].lower()

    def test_missed_threat(self):
        narrator = TemplateNarrator()
        result = narrator.narrate_leak(
            _leak("missed_threat", threat_motif="fork", hung_square="g7")
        )
        self._assert_sensible(result, "missed_threat")
        assert result["threat"] is not None

    def test_mate(self):
        narrator = TemplateNarrator()
        result = narrator.narrate_leak(
            _leak("mate", threat_motif="mate", threat_uci="h5f7", hung_square=None)
        )
        self._assert_sensible(result, "mate")
        assert result["threat"] is not None, "mate category must have threat text"
        assert "checkmate" in result["threat"].lower() or "mate" in result["threat"].lower(), (
            f"mate threat text should mention checkmate/mate, got: {result['threat']!r}"
        )
        # threat_uci (h5f7) should appear in the threat text as the move notation.
        assert "h5f7" in result["threat"], (
            f"threat text should include the mating move UCI, got: {result['threat']!r}"
        )

    def test_mate_without_threat_uci(self):
        narrator = TemplateNarrator()
        result = narrator.narrate_leak(
            _leak("mate", threat_motif="mate", threat_uci=None, hung_square=None)
        )
        self._assert_sensible(result, "mate (no threat_uci)")
        assert result["threat"] is not None
        assert "checkmate" in result["threat"].lower() or "mate" in result["threat"].lower()

    def test_fallback_unknown_category(self):
        narrator = TemplateNarrator()
        result = narrator.narrate_leak(_leak("something_new", threat_motif=None))
        self._assert_sensible(result, "something_new (fallback)")

    def test_all_motif_categories_produce_summary(self):
        narrator = TemplateNarrator()
        for cat in MOTIF_CATEGORIES:
            result = narrator.narrate_leak(_leak(cat))
            assert result["summary"], f"summary empty for {cat}"

    def test_win_prob_drop_reflected_in_summary(self):
        narrator = TemplateNarrator()
        result = narrator.narrate_leak(_leak("hanging", win_prob_before=0.70, win_prob_after=0.40))
        # 30% drop should appear in the summary
        assert "30%" in result["summary"]

    def test_accepts_dict_input(self):
        """narrate_leak should accept a plain dict (as storage.get_leaks returns)."""
        narrator = TemplateNarrator()
        d = _leak_dict("hanging", hung_square="c6")
        result = narrator.narrate_leak(d)
        assert result["summary"]


# ---------------------------------------------------------------------------
# TemplateNarrator — name_cluster
# ---------------------------------------------------------------------------

class TestNameCluster:
    def test_hanging(self):
        narrator = TemplateNarrator()
        name = narrator.name_cluster("hanging", {"count": 3})
        assert "piece" in name.lower() or "lpdo" in name.lower() or "en prise" in name.lower()

    def test_knight_fork(self):
        narrator = TemplateNarrator()
        name = narrator.name_cluster("knight_fork", {"count": 7, "phase": "middlegame"})
        assert "knight" in name.lower()
        assert "fork" in name.lower()
        # High count should appear
        assert "7" in name

    def test_pin(self):
        narrator = TemplateNarrator()
        name = narrator.name_cluster("pin", {"count": 2})
        assert "pin" in name.lower()

    def test_back_rank(self):
        narrator = TemplateNarrator()
        name = narrator.name_cluster("back_rank", {"count": 1})
        assert "back" in name.lower() or "rank" in name.lower()

    def test_missed_threat(self):
        narrator = TemplateNarrator()
        name = narrator.name_cluster("missed_threat", {"count": 4})
        assert "hope" in name.lower() or "threat" in name.lower()

    def test_mate(self):
        narrator = TemplateNarrator()
        name = narrator.name_cluster("mate", {"count": 2})
        assert "mate" in name.lower() or "checkmate" in name.lower()

    def test_unknown_category_fallback(self):
        narrator = TemplateNarrator()
        name = narrator.name_cluster("some_new_motif", {"count": 1})
        assert "some new motif" in name.lower() or name  # non-empty at minimum

    def test_all_known_categories_non_empty(self):
        narrator = TemplateNarrator()
        for cat in MOTIF_CATEGORIES:
            name = narrator.name_cluster(cat, {"count": 2})
            assert name, f"name_cluster returned empty string for {cat}"

    def test_phase_suffix_included(self):
        narrator = TemplateNarrator()
        name = narrator.name_cluster("fork", {"count": 1, "phase": "endgame"})
        assert "endgame" in name.lower()


# ---------------------------------------------------------------------------
# get_narrator factory
# ---------------------------------------------------------------------------

class TestGetNarrator:
    def test_default_returns_template_narrator(self, monkeypatch):
        monkeypatch.delenv("COACH_NARRATOR", raising=False)
        narrator = get_narrator()
        assert isinstance(narrator, TemplateNarrator)

    def test_template_env_returns_template_narrator(self, monkeypatch):
        monkeypatch.setenv("COACH_NARRATOR", "template")
        narrator = get_narrator()
        assert isinstance(narrator, TemplateNarrator)

    def test_claude_env_returns_template_narrator(self, monkeypatch):
        """COACH_NARRATOR=claude must return TemplateNarrator until Claude seam is implemented."""
        monkeypatch.setenv("COACH_NARRATOR", "claude")
        narrator = get_narrator()
        assert isinstance(narrator, TemplateNarrator)

    def test_unknown_env_falls_back_to_template(self, monkeypatch):
        monkeypatch.setenv("COACH_NARRATOR", "chatgpt")
        narrator = get_narrator()
        assert isinstance(narrator, TemplateNarrator)

    def test_narrator_satisfies_protocol(self, monkeypatch):
        monkeypatch.delenv("COACH_NARRATOR", raising=False)
        narrator = get_narrator()
        assert isinstance(narrator, coaching.Narrator)

    def test_no_anthropic_import(self):
        """coaching.py must never import the anthropic package."""
        import sys
        # Reload to ensure we see the real import tree.
        if "app.coaching" in sys.modules:
            importlib.reload(sys.modules["app.coaching"])
        assert "anthropic" not in sys.modules, (
            "anthropic should NOT be imported by coaching.py"
        )
