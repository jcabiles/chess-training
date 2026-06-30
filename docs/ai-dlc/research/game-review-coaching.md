# Research — Game Review & Coaching

Synthesis of three research passes (deterministic pipeline + OAuth; beginner pitfalls;
training pedagogy) that shaped this epic. Kept as reference for implementation.

## 1. The "why" is ~95% deterministic — Claude only narrates

Everything except the final sentence is deterministic Python on Stockfish + python-chess:

1. **Per-ply eval** — `engine.analyse(board, Limit(depth≈18-20), multipv=2-3)`; store cp +
   **Win%** + PV1/PV2.
2. **Win% (Lichess model)** — `Win% = 50 + 50*(2/(1+exp(-0.00368208*cp)) - 1)`. Classify
   leaks on **Win%-drop**, not raw cp (200cp at +50 is nothing; at 0.0 it's fatal). cp
   thresholds as a secondary guard.
3. **Critical-moment localization** — find the sharpest Win% swing; back up to the last
   still-fine position; the move out of THAT position is the real mistake (often 1+ plies
   before the visible blunder). "Only-move" positions (PV1 ≫ PV2) flag fragility.
4. **Null-move threat probe** — `board.push(chess.Move.null())` then `analyse`; the
   opponent's best reply IS the standing threat. **Guards:** never push a null move while
   `board.is_check()`; null clears en-passant; re-derive the threat from the returned PV,
   not a board diff.
5. **Hanging / SEE scan** — `board.attackers(color, sq)` + defenders; Static Exchange
   Evaluation (~30-line helper, no python-chess builtin) decides if a capture wins material.
   Pins via `board.is_pinned` / `board.pin`.
6. **Motif tagging** — port the rule-based (no-ML) logic of lichess-puzzler `tagger/cook.py`:
   fork, pin, skewer, discovered attack, double check (`len(board.checkers())>1`), back-rank,
   hanging piece, sacrifice. AGPL → inspiration/private use, mind redistribution.
7. **Emit structured JSON per leak** → **template (or later Haiku) narrates** it.

Sources: [Lichess accuracy](https://lichess.org/page/accuracy) · [Null Move /
Threat Move](https://www.chessprogramming.org/Threat_Move) · [SEE](https://www.chessprogramming.org/Static_Exchange_Evaluation)
· [lichess-puzzler tagger](https://github.com/ornicar/lichess-puzzler/tree/master/tagger)
· [python-chess core](https://python-chess.readthedocs.io/en/latest/core.html)

## 2. OAuth/Max is OFF THE TABLE (ToS)

Anthropic's **Feb 2026 terms** (enforced **Apr 4 2026**) forbid using Free/Pro/Max
subscription OAuth tokens "in any other product, tool, or service." Subscription OAuth is
restricted to Anthropic's own products (Claude.ai, Claude Code). So piping a Max sub into
this FastAPI app via OAuth is not permitted. Compliant options if/when an LLM layer is added:
**`ANTHROPIC_API_KEY`** (clean; cents/game given tiny cached prompts), or shelling out to the
first-party `claude` CLI (gray-area; unblessed). **Decision: templates now, optional
pluggable Claude layer later.** Sources: [Claude Code auth](https://code.claude.com/docs/en/authentication)
· [The Register](https://www.theregister.com/2026/02/20/anthropic_clarifies_ban_third_party_claude_access/).

## 3. Sub-1100 leak taxonomy (detectable) — what to detect & rank

Almost every blunder is a "Seed of Tactical Destruction" the player failed to see (Heisman).
**Top 5 leaks by (frequency × cost × detectability)** — the Profiler surfaces these first:

1. **Hanging pieces / undefended (LPDO)** — #1 by far. Detect: own piece attacked &
   under-defended after the move (attackers/defenders + SEE<0) + eval cliff. Fully det.
2. **Missing the opponent's threat ("hope chess")** — the decision-habit root cause. Best
   expressed as a **cross-game statistic** ("you allow a threat in X% of games"), not a
   per-move tag. This is the headline "tendency."
3. **Forks (esp. knight) / pins / skewers / discovered / back-rank mate** — decide most
   sub-2000 games. Detect via fork-square scan, `is_pinned`, ray-casts, Stockfish `Mate(n)`.
4. **King safety: not castling / king in centre with queens on** — detect king on home
   square past ~move 12-15 + open lines toward it. Fully det.
5. **Endgame conversion** (passive king, opposition, promotion, failing to convert) — lowest-
   improving skill by rating. Detect via material signature + eval-trajectory over the phase.

Cross-game **habit** signatures (the "you tend to…" gold): repeated-piece-moves in opening,
queen out early, undeveloped pieces at move N, automatic recaptures, time-trouble blunder
clusters (needs PGN `%clk`). Sources: [Heisman Seeds](http://coachjaychess.blogspot.com/2015/01/seeds-of-tactical-destruction.html)
· [chess.com 10 mistakes](https://www.chess.com/article/view/the-10-most-common-mistakes-among-chess-beginners)
· [Heisman thought process](https://www.danheisman.com/thought-process-principles.html).

## 4. Pedagogy — what actually fixes a sub-1100 player

Consensus: **rating below ~1100 is lost to hanging pieces + one-move tactics + not asking
"what does his move threaten?"** Highest-leverage practice = a **pre-move blunder-check
(CCT: Checks/Captures/Threats) habit**, fed by tactics on the player's OWN recurring
mistakes. Patterns worth stealing (this epic builds the first two heads; Trainer deferred):

- **Personalized weakness profile w/ trend tracking** (Chess.com Insights / Aimchess
  dimensions) → the **Profiler**.
- **Explain the conflict in concept buckets, not just eval** (DecodeChess: Threats / Plans /
  Functionality; Dr. Wolf flags *before* you commit) → the **Foresight** warnings.
- *(Deferred to next epic — Trainer)* Re-solve YOUR blunders with a forced threat-check
  (Lichess "learn from mistakes" + Aimchess Retry-Mistakes) + spaced repetition.

**Critical design caveat for the deferred Trainer:** naive SR of the *identical* position
teaches board recall, not transfer (the de la Maza trap) — resurface the **motif in varied
positions**. Sources: [Heisman finding safe moves](https://www.danheisman.com/finding-safe-moves.html)
· [Aimchess](https://aimchess.com/) · [DecodeChess](https://decodechess.com/natural-language-chess-analysis/)
· [Woodpecker](https://forwardchess.com/blog/what-is-the-woodpecker-method/)
· [transfer caveat](https://www.chess.com/forum/view/general/tactics-transfering-into-real-games).
