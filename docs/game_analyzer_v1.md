# Game Analyzer V1

The game analyzer is a post-game consumer of the existing Champions battle/search stack.
It is deliberately not a second simulator.

## Reused foundation

V1 reuses:

- vgc.battle_state_replay to rebuild the exact player-view state at a saved decision;
- vgc.actions for the legal doubles action space and exact Showdown wire messages;
- vgc.search.search_joint_orders for the same engineered shallow search used by the
  playing agent;
- the existing Champions data, damage, speed, belief, and mechanics code transitively
  used by search.

This keeps simulator and analyzer mechanics in one place.

## Input contract

V1 consumes a vgc-decision-replay-v1 JSON bundle produced by the existing
DecisionReplayRecorder. This is stronger evidence than a spectator HTML replay because
it preserves the actual player-side message stream, request cutoffs, legal actions, and
information boundary at each decision.

A future ingestion layer can convert human/public replays into analyzable states, but it
must not silently invent private information that was unavailable to the player.

## Output semantics

For each ordinary move decision V1 rebuilds the battle at that exact cutoff, re-runs
search_joint_orders, matches the saved choice by its exact wire message, and reports:

- played action and rank;
- engine-preferred action;
- engineered search-score gap;
- up to N alternatives;
- worst modeled opponent response when available;
- tactical evidence already present in the evaluator breakdown, including estimated
  guaranteed/likely KOs, lethal-exposure penalties, and relevant Speed benchmarks;
- a conservative evidence band: none, candidate, moderate, or high.

Search score is not win probability. The evidence band measures disagreement with the
current engineered evaluator/search. V1 therefore says "engine disagreement" or
"likely tactical mistake," not "objective blunder."

Opponent Stat Points/nature are often hidden in ordinary Bo1 play. A "guaranteed KO"
therefore means guaranteed against the analyzer's current hidden-spread estimate, not
against every legal spread. Reports label those facts model-based.

Team preview and forced-switch grading are intentionally skipped in this first slice.
Forfeits, missing choices, and Showdown default choices are also skipped rather than
misgraded as unavailable legal moves.

## CLI

    .venv/bin/python offline/analyze_game.py path/to/decision-replay.json
    .venv/bin/python offline/analyze_game.py path/to/decision-replay.json --json

The first form prints a readable turn-by-turn report; --json emits
vgc-game-analysis-v1 for later UI/report tooling.

## Verification

The integration suite now creates an authentic player-view bundle from a deterministic
local Showdown M-C battle, runs that bundle through the analyzer, and requires every
graded played action to match a reconstructed legal candidate. This verifies the
record -> cutoff replay -> legal action -> search -> report path without committing a
fabricated "real user" replay.

The repository still does not contain a real human M-C player-view decision bundle.
When one is available, it should become the first human golden fixture after removing
account-identifying metadata if necessary.

## Next slices

1. Add redundant-Protect and missed-survival tactical explanations where the existing
   evaluator evidence is strong enough to support them.
2. Capture and retain one representative human M-C player-view bundle as a golden
   fixture.
3. Public/human replay ingestion with explicit known-vs-true-state handling.
4. Only after those are trustworthy: calibrated multi-turn decision-loss estimates.
