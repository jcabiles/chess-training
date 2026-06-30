"""
coaching.py — Template-based leak narrator (pure; zero LLM, no network).

Converts a LeakRecord (or a leak dict from storage.get_leaks) into
DecodeChess-style bucketed foresight text:

    {"threat": str | None, "hanging": str | None, "plan": str | None, "summary": str}

and a human tendency-cluster name for the profile dashboard:

    name_cluster(category, stats) -> str

Factory
-------
get_narrator() reads env COACH_NARRATOR (default 'template') → TemplateNarrator.
When COACH_NARRATOR='claude', also returns TemplateNarrator for now (the seam is
left in place so a future ClaudeNarrator can be dropped in without touching any
other module).

TODO(claude-narrator): when COACH_NARRATOR='claude', instantiate ClaudeNarrator
instead of TemplateNarrator.  ClaudeNarrator should use ANTHROPIC_API_KEY (not
OAuth/Max — see spec §Token/LLM strategy), tiny cached prompts, batched per game.
Do NOT add an `anthropic` import to this module until that work is ready.

Foresight frame
---------------
All text is written from a *foresight* perspective — what is about to happen if
the player fails to act — NOT at-the-blunder scolding.  This mirrors the
DecodeChess "Threat / Hanging / Plan" bucket philosophy and the pedagogical
guidance in research §4.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

from app import storage

# ---------------------------------------------------------------------------
# Narrator protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Narrator(Protocol):
    """Interface every narrator implementation must satisfy."""

    def narrate_leak(self, leak: "storage.LeakRecord | dict") -> dict:
        """Turn a leak into DecodeChess-style bucketed foresight text.

        Returns a dict with keys:
            threat  (str | None) — the developing opponent threat
            hanging (str | None) — piece(s) at risk if left undefended
            plan    (str | None) — the recommended defensive/offensive plan
            summary (str)       — one-sentence overall foresight summary
        """
        ...

    def name_cluster(self, category: str, stats: dict) -> str:
        """Return a human-readable tendency-cluster name.

        Args:
            category: leak category string (e.g. 'hanging', 'knight_fork').
            stats: dict with at least 'count' and optionally 'phase', 'opening',
                   'color'.  Used to personalise the name where possible.

        Returns:
            A human name such as "Missing knight forks when the king is uncastled".
        """
        ...


# ---------------------------------------------------------------------------
# Normaliser — accept LeakRecord or dict
# ---------------------------------------------------------------------------


def _to_dict(leak: "storage.LeakRecord | dict") -> dict:
    """Normalise a LeakRecord or a raw dict into a plain dict."""
    if isinstance(leak, storage.LeakRecord):
        return {
            "id": leak.id,
            "game_id": leak.game_id,
            "ply": leak.ply,
            "color": leak.color,
            "severity": leak.severity,
            "category": leak.category,
            "motif_json": leak.motif_json,
            "phase": leak.phase,
            "win_prob_before": leak.win_prob_before,
            "win_prob_after": leak.win_prob_after,
            "win_prob_drop": leak.win_prob_drop,
            "hung_square": leak.hung_square,
            "threat_uci": leak.threat_uci,
            "threat_motif": leak.threat_motif,
            "best_uci": leak.best_uci,
            "best_san": leak.best_san,
            "lead_in_ply": leak.lead_in_ply,
            "tags_json": leak.tags_json,
            "explanation_json": leak.explanation_json,
        }
    return dict(leak)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pct(prob: float | None) -> str:
    """Format a win-probability [0,1] as a percentage string."""
    if prob is None:
        return "?"
    return f"{round(prob * 100)}%"


def _severity_label(severity: str) -> str:
    return "blunder" if severity == "blunder" else "mistake"


# ---------------------------------------------------------------------------
# TemplateNarrator
# ---------------------------------------------------------------------------


class TemplateNarrator:
    """Pure f-string template narrator.

    All text is foresight-framed — what the opponent is preparing — not
    at-the-blunder scolding.  Covers every motif type from motifs.py plus
    a generic fallback.
    """

    # ------------------------------------------------------------------
    # narrate_leak
    # ------------------------------------------------------------------

    def narrate_leak(self, leak: "storage.LeakRecord | dict") -> dict:  # noqa: C901
        """Return DecodeChess-style bucketed foresight text for a leak."""
        d = _to_dict(leak)

        category = (d.get("category") or "").strip()
        severity = d.get("severity", "mistake")
        hung = d.get("hung_square")
        threat_motif = (d.get("threat_motif") or "").strip()
        threat_uci = d.get("threat_uci")
        best_san = d.get("best_san")
        win_drop = d.get("win_prob_drop", 0.0)
        phase = (d.get("phase") or "middlegame").strip()

        threat_text: str | None = None
        hanging_text: str | None = None
        plan_text: str | None = None

        # ---- Hanging / LPDO ------------------------------------------------
        if category == "hanging":
            hanging_text = self._hanging_threat(hung, threat_uci)
            threat_text = self._generic_threat(threat_motif, threat_uci)
            plan_text = self._defend_or_move(hung, best_san)

        # ---- Knight fork ---------------------------------------------------
        elif category == "knight_fork":
            threat_text = self._knight_fork_threat(threat_uci)
            hanging_text = self._fork_hanging(hung)
            plan_text = self._fork_plan(best_san)

        # ---- Generic fork --------------------------------------------------
        elif category == "fork":
            threat_text = self._fork_threat(threat_uci)
            hanging_text = self._fork_hanging(hung)
            plan_text = self._fork_plan(best_san)

        # ---- Pin ------------------------------------------------------------
        elif category == "pin":
            threat_text = self._pin_threat(hung, threat_uci)
            hanging_text = (
                f"Your piece on {hung} is becoming pinned — it cannot move "
                f"without exposing a more valuable piece."
                if hung else
                "A pin is developing — one of your pieces is about to lose its mobility."
            )
            plan_text = self._pin_plan(best_san)

        # ---- Skewer --------------------------------------------------------
        elif category == "skewer":
            threat_text = self._skewer_threat(threat_uci)
            hanging_text = (
                f"Your piece on {hung} is in the line of a skewer — "
                f"after it moves, the piece behind it will be lost."
                if hung else
                "A skewer is developing — your high-value piece must move, "
                "leaving the piece behind it vulnerable."
            )
            plan_text = self._skewer_plan(best_san)

        # ---- Discovered attack ---------------------------------------------
        elif category == "discovered":
            threat_text = self._discovered_threat(threat_uci)
            hanging_text = (
                f"Piece on {hung} will be exposed when the opponent unblocks "
                f"a hidden slider."
                if hung else
                "A discovered attack is being set up — moving one opponent piece "
                "will unleash a hidden attacker."
            )
            plan_text = self._discovered_plan(best_san)

        # ---- Back-rank mate threat -----------------------------------------
        elif category == "back_rank":
            threat_text = (
                "Opponent is preparing a back-rank mating attack — "
                "your king has no escape square on the back rank."
            )
            hanging_text = (
                "Your back rank is weak: king locked in by own pawns with "
                "no luft (escape square)."
            )
            plan_text = self._back_rank_plan(best_san)

        # ---- Missed threat (hope-chess) ------------------------------------
        elif category == "missed_threat":
            threat_text = self._missed_threat_text(threat_motif, threat_uci)
            hanging_text = (
                f"Your piece on {hung} is under attack and will be lost "
                f"if you play normally."
                if hung else
                "A strong opponent threat is being ignored — stop and ask: "
                "'What does my opponent's move threaten?'"
            )
            plan_text = (
                f"Consider {best_san} to neutralise the threat first."
                if best_san else
                "Prioritise defensive moves — check for captures, checks, and threats (CCT)."
            )

        # ---- Checkmate threat ----------------------------------------------
        elif category == "mate":
            threat_san = None
            if threat_uci and len(threat_uci) >= 4:
                # Fall back to UCI notation — we don't have the board here to compute SAN.
                threat_san = threat_uci
            threat_text = (
                f"The opponent is threatening checkmate ({threat_san}). "
                f"You must stop the mate now (defend the mating square or give the king luft)."
                if threat_san else
                "The opponent is threatening checkmate. "
                "You must stop the mate now (defend the mating square or give the king luft)."
            )
            hanging_text = (
                "Your king will have no escape — the mating square is undefended."
            )
            plan_text = (
                f"Stop the checkmate: consider {best_san} to parry the mating threat."
                if best_san else
                "Stop the checkmate immediately — block, capture the attacker, or move the king."
            )

        # ---- Generic fallback ----------------------------------------------
        else:
            threat_text = self._generic_threat(threat_motif, threat_uci)
            hanging_text = (
                f"Your piece on {hung} may be at risk."
                if hung else
                None
            )
            plan_text = (
                f"Consider {best_san} to improve your position."
                if best_san else
                "Look for the strongest defensive resource before playing your intended move."
            )

        # ---- Summary -------------------------------------------------------
        drop_pct = round((win_drop or 0.0) * 100)
        phase_label = {"opening": "the opening", "middlegame": "the middlegame",
                       "endgame": "the endgame"}.get(phase, "the game")

        if threat_text:
            summary = (
                f"Danger ahead in {phase_label}: {threat_text} "
                f"(win chance drops ~{drop_pct}% if missed)."
            )
        elif hanging_text:
            summary = (
                f"In {phase_label} a {_severity_label(severity)} looms — "
                f"{hanging_text} "
                f"(win chance drops ~{drop_pct}% if missed)."
            )
        else:
            summary = (
                f"A {_severity_label(severity)} is developing in {phase_label} "
                f"(win chance drops ~{drop_pct}% if missed)."
            )

        return {
            "threat": threat_text,
            "hanging": hanging_text,
            "plan": plan_text,
            "summary": summary,
        }

    # ------------------------------------------------------------------
    # name_cluster
    # ------------------------------------------------------------------

    def name_cluster(self, category: str, stats: dict) -> str:
        """Return a human tendency-cluster name for the profile dashboard."""
        phase = stats.get("phase", "")
        opening = stats.get("opening", "")
        color = stats.get("color", "")
        count = stats.get("count", 0)

        phase_suffix = f" in the {phase}" if phase else ""
        opening_suffix = f" in the {opening}" if opening else ""
        color_suffix = f" as {color.capitalize()}" if color else ""

        names: dict[str, str] = {
            "hanging": (
                f"Leaving pieces en prise (LPDO){phase_suffix}"
            ),
            "knight_fork": (
                f"Missing knight forks{phase_suffix}{opening_suffix}"
            ),
            "fork": (
                f"Overlooking fork threats{phase_suffix}"
            ),
            "pin": (
                f"Ignoring pin threats{phase_suffix}{color_suffix}"
            ),
            "skewer": (
                f"Missing skewer setups{phase_suffix}"
            ),
            "discovered": (
                f"Overlooked discovered attacks{phase_suffix}"
            ),
            "back_rank": (
                f"Weak back-rank king safety{color_suffix}"
            ),
            "missed_threat": (
                f"Hope chess — playing without checking threats{phase_suffix}"
            ),
            "mate": (
                f"Missing forced checkmate threats{phase_suffix}"
            ),
        }

        base = names.get(
            category,
            f"Recurring {category.replace('_', ' ')} errors{phase_suffix}",
        )

        if count and count >= 5:
            return f"{base} ({count}× so far)"
        return base

    # ------------------------------------------------------------------
    # Private helpers — threat / hanging / plan text per motif
    # ------------------------------------------------------------------

    def _hanging_threat(self, hung: str | None, threat_uci: str | None) -> str | None:
        if hung:
            return (
                f"Opponent is eyeing your piece on {hung} — "
                f"it is undefended and can be taken for free."
            )
        if threat_uci:
            return (
                "Opponent is about to capture an undefended piece."
            )
        return None

    def _generic_threat(self, threat_motif: str, threat_uci: str | None) -> str | None:
        if threat_motif:
            motif_labels = {
                "hanging": "win material",
                "fork": "create a fork",
                "knight_fork": "deliver a knight fork",
                "pin": "set up a pin",
                "skewer": "execute a skewer",
                "discovered": "unleash a discovered attack",
                "back_rank": "threaten a back-rank mate",
            }
            action = motif_labels.get(threat_motif, f"play {threat_motif}")
            return f"Opponent is about to {action}."
        if threat_uci:
            return "A strong opponent threat is brewing — look carefully before moving."
        return None

    def _defend_or_move(self, hung: str | None, best_san: str | None) -> str | None:
        if hung and best_san:
            return (
                f"Defend or move the piece on {hung}. "
                f"Engine recommends {best_san}."
            )
        if hung:
            return f"Defend or relocate the piece on {hung} before it is taken."
        if best_san:
            return f"Consider {best_san} to address the material threat."
        return "Find a defensive resource — capture, check, or move the attacked piece."

    def _knight_fork_threat(self, threat_uci: str | None) -> str | None:
        dest = threat_uci[2:4] if threat_uci and len(threat_uci) >= 4 else None
        if dest:
            return (
                f"Opponent knight is maneuvering toward {dest} — "
                f"a fork of your king and/or major pieces is being prepared."
            )
        return (
            "Opponent is setting up a knight fork — "
            "watch for the knight reaching a square that attacks two of your pieces."
        )

    def _fork_threat(self, threat_uci: str | None) -> str | None:
        dest = threat_uci[2:4] if threat_uci and len(threat_uci) >= 4 else None
        if dest:
            return (
                f"Opponent is about to fork two of your pieces from {dest}."
            )
        return "A fork is being set up — one opponent move may attack two of your pieces at once."

    def _fork_hanging(self, hung: str | None) -> str | None:
        if hung:
            return f"Your piece on {hung} is one of the fork targets and cannot escape."
        return "Two of your pieces are in the firing line of an upcoming fork."

    def _fork_plan(self, best_san: str | None) -> str | None:
        if best_san:
            return f"Break up the fork threat with {best_san}."
        return "Move one of the targeted pieces, or block the forking square."

    def _pin_threat(self, hung: str | None, threat_uci: str | None) -> str | None:
        if hung:
            return (
                f"Opponent is about to pin your piece on {hung} against a more valuable piece — "
                f"once pinned it cannot legally move."
            )
        return "A pin is being set up — your piece will be nailed in place."

    def _pin_plan(self, best_san: str | None) -> str | None:
        if best_san:
            return f"Unpin or pre-empt with {best_san}."
        return "Break the pin by interposing, capturing the pinner, or moving the shielded piece."

    def _skewer_threat(self, threat_uci: str | None) -> str | None:
        if threat_uci:
            return (
                "Opponent slider is lining up a skewer — your high-value piece must "
                "move, and the piece behind it will be lost."
            )
        return "A skewer is developing — moving away will expose the piece behind you."

    def _skewer_plan(self, best_san: str | None) -> str | None:
        if best_san:
            return f"Step the vulnerable piece off the diagonal/file with {best_san}."
        return "Get the high-value piece off the skewer line before the attacker strikes."

    def _discovered_threat(self, threat_uci: str | None) -> str | None:
        return (
            "A discovered attack is being prepared — when the front piece moves, "
            "a hidden slider will attack your piece(s)."
        )

    def _discovered_plan(self, best_san: str | None) -> str | None:
        if best_san:
            return f"Neutralise the hidden threat with {best_san}."
        return "Get your vulnerable pieces off the discovered-attack ray."

    def _back_rank_plan(self, best_san: str | None) -> str | None:
        if best_san:
            return f"Create luft (a king escape square) with {best_san}."
        return (
            "Push a pawn in front of your king to create an escape square (luft), "
            "or activate a rook on the back rank."
        )

    def _missed_threat_text(self, threat_motif: str, threat_uci: str | None) -> str | None:
        if threat_motif == "hanging":
            return "Your opponent is about to win material — you have an undefended piece."
        if threat_motif in ("fork", "knight_fork"):
            return "Your opponent is setting up a fork — do not play 'hope chess'."
        if threat_motif == "back_rank":
            return "A back-rank checkmate threat is forming — check your back rank."
        return (
            "Your opponent's last move contains a concrete threat. "
            "Ask: 'What does this move threaten?' before responding."
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_narrator() -> Narrator:
    """Return the configured Narrator implementation.

    Reads env COACH_NARRATOR (default 'template').
    'claude' → returns TemplateNarrator for now (TODO seam — see module docstring).
    Any unrecognised value falls back to TemplateNarrator.
    """
    mode = os.environ.get("COACH_NARRATOR", "template").strip().lower()

    if mode == "claude":
        # TODO(claude-narrator): swap TemplateNarrator for ClaudeNarrator once
        # that class is implemented.  Do NOT add an `anthropic` import here
        # until then.
        return TemplateNarrator()

    # Default: 'template' (and any unrecognised value)
    return TemplateNarrator()
