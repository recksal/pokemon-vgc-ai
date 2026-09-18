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
- a conservative evidence band: none, candidate, moderate, or high.

Search score is not win probability. The evidence band measures disagreement with the
current engineered evaluator/search. V1 therefore says "engine disagreement" or
"likely tactical mistake," not "objective blunder."

Team preview and forced-switch grading are intentionally skipped in this first slice.

## CLI

    .venv/bin/python offline/analyze_game.py path/to/decision-replay.json
    .venv/bin/python offline/analyze_game.py path/to/decision-replay.json --json

The first form prints a readable turn-by-turn report; --json emits
vgc-game-analysis-v1 for later UI/report tooling.

## Next slices

1. Golden end-to-end fixture from a real M-C decision bundle.
2. Tactical evidence extractors for guaranteed KOs, survival, speed order, and redundant
   Protects so reports explain why a line is preferred rather than only exposing score.
3. Public/human replay ingestion with explicit known-vs-true-state handling.
4. Only after those are trustworthy: calibrated multi-turn decision-loss estimates.
