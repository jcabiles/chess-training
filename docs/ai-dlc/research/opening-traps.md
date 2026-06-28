# Research notes — Opening Traps

Source: one cursory research agent (shortlist) + two Sonnet deep-research agents
(Batch A = open games `1.e4 e5`; Batch B = `1.d4` + semi-open defenses). This is
**raw research input** for the build phase — every line MUST be engine-verified
before it ships (the research agents self-flagged several mate claims as uncertain).

## Trap inventory (14 candidates, split into the two research batches)

| # | Trap | Color | Opening | ECO | Common (1–5) | Spring point / notes |
|---|------|-------|---------|-----|--------------|----------------------|
| A1 | Legal's Trap (Legal Mate) | White | Philidor/Italian | C41 | 3 | After `4.Nc3`; `5.Nxe5` pseudo-sac → `Bxd1?? 6.Bxf7+ Ke7 7.Nd5#`. Mate **confirmed**. |
| A2 | Scholar's Mate | White | Open Game | C20 | 5 | `2.Bc4 … 3.Qh5`; bait `…Nf6?? 4.Qxf7#`. **Teach the defense too** (`…Qe7!`/`…g6!`). Objectively bad for White if declined. |
| A3 | Blackburne Shilling Gambit | Black | Italian | C50 | 4 | `3…Nd4?!`; bait `4.Nxe5?? Qg5!` → smothered mate. ⚠ `Nf3#` claim **needs verification**. |
| A4 | Fried Liver Attack | White | Two Knights | C57 | 4 | `6.Nxf7!` sac; sound attacking comp, not a one-move mate. Refuted earlier by `5…Na5!`. |
| A5 | Fishing Pole Trap | Black | Ruy Lopez Berlin | C65 | 3 | `4…Ng4`; bait `5.h3?? h5!` then `6.hxg4?? hxg4`. Involves an en-passant line — verify legality. |
| A6 | Noah's Ark Trap | Black | Ruy Lopez | C60s | 2 | Pawns `a6-b5-c4` trap the `b3` bishop after White grabs pawns. Lower commonness. |
| A7 | **Stafford Gambit** (flagship) | Black | Petrov | C42 | 3 | `3…Nc6?!`. Multiple White tries: `4.Nxc6??` (king hunt), `4.Nf3 Nxe4` (…Ng4!/…Nxf2!! ideas), `4.Nc4!` (correct refutation). ⚠ Some mate claims **need verification**. |
| B1 | Elephant Trap | Black | QGD | D06/D08 | 3 | `…Nbd7` then `cxd5 exd5 6.Nxd5?? Nxd5! 7.Bxd8 Bb4+` wins a piece. |
| B2 | Lasker Trap | Black | Albin Counter-Gambit | D08 | 2 | `…dxe3` then `…exf2+` and the `…fxg1=N+!` **underpromotion**. Cute but Albin is uncommon at beginner level. |
| B3 | Englund Gambit Trap | Black | Englund | A40 | 2 | `1.d4 e5 2.dxe5 Nc6 3.Nf3 Qe7 4.Bf4 Qb4+`; the `…Qxb2 / …Bb4 / …Qxc3` mating idea (`…Qa1#`). Agent produced a CORRECTED final line. |
| B4 | London System Trap | White | London | D00 | 3 | `…Qb6?!` then `…Qxb2?? 5.Rb1`/`Na4`-type net traps the queen (eval ~+3.0). Confirmed sound. |
| B5 | Scandinavian Trap | Black | Scandinavian | B01 | 3 | Cursory pass was muddled → **rebuilt cleanly** by the deep agent. |
| B6 | Caro-Kann Fantasy Variation | White | Caro-Kann | B12 | 2 | `3.f3!?` lines; `…Bg4?` punished by `Bxf7+`/queen tactics. Lower commonness. |
| B7 | French — Tarrasch Greek Gift | (substituted) | French | C11 | 2–3 | Original "French Burn" was muddled; deep agent **substituted a clean Greek Gift** (`Bxh7+` sac) as the clearer beginner-common French trap. |

Color balance of the 14: **5 White / 9 Black** (cursory pass miscounted as 8/6).

## Structural findings that shape the data model
- Every trap is a **lead-in → branch point → two outcomes**:
  - **Bait** (opponent's natural-looking losing move) → **punishment** (forcing win/mate).
  - **Refutation** (correct move) → a **short sound continuation** so the trapper isn't lost.
- The trapper's key moves are often **objectively dubious** (e.g. Stafford `−0.6`, Fishing Pole `+0.3` for the opponent). The honest eval + a "why it scores in practice" note must both be shown.
- Several traps have **multiple opponent tries** (Stafford especially) → the model needs optional named variations per trap.

## Engine-verification mandate (NON-NEGOTIABLE for build)
The deep agents explicitly flagged uncertain mates: **BSG `Nf3#`**, **Stafford `Qa3#`**,
and parts of the **Englund** line. Therefore the build MUST, before shipping any trap:
1. Replay every line with python-chess — assert all moves legal in order.
2. Assert claimed checkmates are `board.is_checkmate()`; claimed material wins reach the stated material delta.
3. Stockfish sanity-check the eval framing (dubious-but-practical claims).
Any line that can't be made correct is **fixed or dropped** — we never ship wrong chess.
Target shipping subset: the engine-verified, genuinely beginner-common traps
(expect ~10–12 of the 14 to survive; the 2/5-commonness ones are first to cut).

## Raw deep-research outputs
- Batch A (open games): captured in the build conversation; lines as above.
- Batch B (`1.d4`/semi-open): persisted tool-result JSON in the session transcript
  dir (`…/tool-results/`); contains the corrected Englund + clean Scandinavian +
  Greek-Gift French lines. Re-derive + verify during build rather than trusting prose.
