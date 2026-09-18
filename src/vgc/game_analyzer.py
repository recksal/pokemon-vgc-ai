"""V1 post-game decision analysis built on the existing Champions search engine.

The analyzer consumes a vgc-decision-replay-v1 bundle, rebuilds the exact player-view
state at each saved decision cutoff, and re-scores legal actions with the same search
stack used by the simulator/bot.

Important: the numeric score is an ENGINE SCORE, not a win probability. V1 reports
engine-preferred alternatives and conservative evidence; it does not claim objective
Stockfish-style blunders.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields

from poke_env.battle.double_battle import DoubleBattle
from poke_env.battle.field import Field

from vgc.actions import choice_wire_message, describe_order
from vgc.battle_state_replay import DECISION_REPLAY_SCHEMA, replay_battle_at_cutoff
from vgc.models import PolicyConfig
from vgc.search import search_joint_orders


@dataclass(frozen=True)
class TacticalEvidence:
    kind: str
    confidence: str
    text: str
    actor: str | None = None
    target: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CandidateScore:
    order: str
    wire: str
    score: float
    searched: bool
    worst_response: str | None = None
    exchange_value: float | None = None
    evidence: tuple[TacticalEvidence, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class DecisionFinding:
    decision_sequence: int
    turn: int
    phase: str
    chosen_order: str | None
    chosen_wire: str | None
    chosen_rank: int | None
    chosen_score: float | None
    best_order: str | None
    best_wire: str | None
    best_score: float | None
    score_gap: float | None
    confidence: str
    finding: str
    explanations: tuple[TacticalEvidence, ...]
    alternatives: tuple[CandidateScore, ...]

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["explanations"] = [entry.as_dict() for entry in self.explanations]
        payload["alternatives"] = [entry.as_dict() for entry in self.alternatives]
        return payload


def _config_from_bundle(bundle: dict[str, object]) -> PolicyConfig:
    raw = bundle.get("policy_config")
    if not isinstance(raw, dict):
        return PolicyConfig()
    allowed = {entry.name for entry in fields(PolicyConfig)}
    values = {key: value for key, value in raw.items() if key in allowed}
    return PolicyConfig(**values)


def _confidence(*, rank: int, gap: float, searched: bool) -> str:
    """Conservative disagreement band; never interpreted as win probability."""

    if rank == 1 or gap <= 1e-9:
        return "none"
    if searched and rank >= 3 and gap >= 75.0:
        return "high"
    if searched and gap >= 30.0:
        return "moderate"
    return "candidate"


def _species_label(mon: object | None, fallback: str) -> str:
    species = getattr(mon, "species", None)
    return str(species) if species else fallback


def _move_id(single: object | None) -> str | None:
    target = getattr(single, "order", None)
    move_id = getattr(target, "id", None)
    return str(move_id) if move_id else None


def _candidate_tactical_evidence(
    battle: DoubleBattle,
    entry: object,
) -> tuple[TacticalEvidence, ...]:
    """Expose tactical facts already used by the evaluator for this candidate.

    The evaluator estimates hidden opponent spreads, so damage and Speed facts are
    labelled model-based rather than certain. This avoids turning a "guaranteed KO"
    against one estimated spread into a claim that every legal hidden spread is KOed.
    """

    breakdown = getattr(entry, "breakdown", None) or {}
    order = getattr(entry, "order", None)
    singles = (
        getattr(order, "first_order", None),
        getattr(order, "second_order", None),
    )
    our_active = list(getattr(battle, "active_pokemon", None) or [])
    opp_active = list(getattr(battle, "opponent_active_pokemon", None) or [])
    trick_room = Field.TRICK_ROOM in (getattr(battle, "fields", None) or {})

    evidence: list[TacticalEvidence] = []
    for slot, single in enumerate(singles):
        info = breakdown.get(f"slot{slot}") or {}
        if not isinstance(info, dict):
            continue
        actor = str(info.get("actor_species") or "") or _species_label(
            our_active[slot] if slot < len(our_active) else None,
            f"our slot {slot + 1}",
        )
        move_id = _move_id(single) or "this action"

        guaranteed = {int(value) for value in info.get("guaranteed_ko_slots") or []}
        likely = {int(value) for value in info.get("ko_slots") or []} - guaranteed
        target_speeds = info.get("target_speeds") or {}
        actor_speed = info.get("actor_speed")
        actor_priority = int(info.get("actor_priority") or 0)

        for target_slot in sorted(guaranteed):
            target = _species_label(
                opp_active[target_slot] if target_slot < len(opp_active) else None,
                f"opponent slot {target_slot + 1}",
            )
            evidence.append(
                TacticalEvidence(
                    kind="guaranteed_ko",
                    confidence="model",
                    actor=actor,
                    target=target,
                    text=(
                        f"{actor}'s {move_id} is a guaranteed KO on {target} against "
                        "the analyzer's current hidden-spread estimate."
                    ),
                )
            )
        for target_slot in sorted(likely):
            target = _species_label(
                opp_active[target_slot] if target_slot < len(opp_active) else None,
                f"opponent slot {target_slot + 1}",
            )
            evidence.append(
                TacticalEvidence(
                    kind="likely_ko",
                    confidence="model",
                    actor=actor,
                    target=target,
                    text=(
                        f"{actor}'s {move_id} reaches the expected-damage KO threshold "
                        f"on {target} under the current hidden-spread estimate."
                    ),
                )
            )

        if float(info.get("expected_death_cost") or 0.0) > 0.0:
            evidence.append(
                TacticalEvidence(
                    kind="lethal_exposure",
                    confidence="model",
                    actor=actor,
                    text=(
                        f"This line leaves {actor} exposed to a modeled lethal threat "
                        "without first removing that threat."
                    ),
                )
            )

        # Speed numbers are useful benchmark evidence only when this action is also
        # threatening a KO. Move priority can override Speed, so phrase this explicitly
        # as the equal-priority relationship rather than "acts first" unconditionally.
        if actor_speed is not None:
            for target_slot in sorted(guaranteed | likely):
                raw_target_speed = (
                    target_speeds.get(target_slot)
                    if isinstance(target_speeds, dict)
                    else None
                )
                if raw_target_speed is None and isinstance(target_speeds, dict):
                    raw_target_speed = target_speeds.get(str(target_slot))
                if raw_target_speed is None:
                    continue
                target = _species_label(
                    opp_active[target_slot] if target_slot < len(opp_active) else None,
                    f"opponent slot {target_slot + 1}",
                )
                actor_speed_value = float(actor_speed)
                target_speed_value = float(raw_target_speed)
                if trick_room:
                    favored = actor if actor_speed_value <= target_speed_value else target
                    condition = "under Trick Room, within equal priority"
                else:
                    favored = actor if actor_speed_value >= target_speed_value else target
                    condition = "in normal order, within equal priority"
                evidence.append(
                    TacticalEvidence(
                        kind="speed_benchmark",
                        confidence="model",
                        actor=actor,
                        target=target,
                        text=(
                            f"Estimated effective Speed benchmark: {actor} "
                            f"{actor_speed_value:.1f} vs {target} {target_speed_value:.1f}; "
                            f"{condition} this favors {favored}. "
                            f"{move_id} itself is priority {actor_priority:+d}."
                        ),
                    )
                )
    return tuple(evidence)


def _evidence_key(entry: TacticalEvidence) -> tuple[str, str | None, str | None]:
    return entry.kind, entry.actor, entry.target


def _difference_evidence(
    best: CandidateScore,
    chosen: CandidateScore,
    *,
    limit: int = 4,
) -> tuple[TacticalEvidence, ...]:
    """Explain facts that distinguish the preferred line from the played one."""

    chosen_keys = {_evidence_key(entry) for entry in chosen.evidence}
    best_keys = {_evidence_key(entry) for entry in best.evidence}
    explanations: list[TacticalEvidence] = []

    for entry in best.evidence:
        if _evidence_key(entry) in chosen_keys:
            continue
        if entry.kind not in {"guaranteed_ko", "likely_ko", "speed_benchmark"}:
            continue
        explanations.append(
            TacticalEvidence(
                kind=entry.kind,
                confidence=entry.confidence,
                actor=entry.actor,
                target=entry.target,
                text=f"Preferred line: {entry.text}",
            )
        )
        if len(explanations) >= limit:
            return tuple(explanations)

    for entry in chosen.evidence:
        if _evidence_key(entry) in best_keys:
            continue
        if entry.kind != "lethal_exposure":
            continue
        explanations.append(
            TacticalEvidence(
                kind=entry.kind,
                confidence=entry.confidence,
                actor=entry.actor,
                target=entry.target,
                text=f"Played line: {entry.text}",
            )
        )
        if len(explanations) >= limit:
            break
    return tuple(explanations)


def compare_candidates(
    *,
    decision_sequence: int,
    turn: int,
    phase: str,
    chosen_order: str | None,
    chosen_wire: str | None,
    candidates: list[CandidateScore],
    top_k: int = 3,
) -> DecisionFinding:
    """Compare a played wire action with a ranked candidate list."""

    if not candidates:
        return DecisionFinding(
            decision_sequence=decision_sequence,
            turn=turn,
            phase=phase,
            chosen_order=chosen_order,
            chosen_wire=chosen_wire,
            chosen_rank=None,
            chosen_score=None,
            best_order=None,
            best_wire=None,
            best_score=None,
            score_gap=None,
            confidence="unavailable",
            finding="No analyzable candidates were produced for this decision.",
            explanations=(),
            alternatives=(),
        )

    best = candidates[0]
    chosen_index = next(
        (index for index, entry in enumerate(candidates) if entry.wire == chosen_wire),
        None,
    )
    if chosen_index is None:
        return DecisionFinding(
            decision_sequence=decision_sequence,
            turn=turn,
            phase=phase,
            chosen_order=chosen_order,
            chosen_wire=chosen_wire,
            chosen_rank=None,
            chosen_score=None,
            best_order=best.order,
            best_wire=best.wire,
            best_score=best.score,
            score_gap=None,
            confidence="unavailable",
            finding="The saved choice could not be matched to the rebuilt legal action set.",
            explanations=(),
            alternatives=tuple(candidates[:top_k]),
        )

    chosen = candidates[chosen_index]
    rank = chosen_index + 1
    gap = max(0.0, best.score - chosen.score)
    band = _confidence(rank=rank, gap=gap, searched=chosen.searched)
    if rank == 1:
        finding = "Played action matches the engine's top-ranked line."
    elif band == "high":
        finding = (
            "Strong engine disagreement: inspect the preferred line as a likely "
            "tactical mistake."
        )
    elif band == "moderate":
        finding = (
            "Meaningful engine disagreement: the preferred line deserves replay review."
        )
    else:
        finding = (
            "Alternative candidate found, but V1 evidence is not strong enough to call "
            "this a mistake."
        )

    explanations = () if rank == 1 else _difference_evidence(best, chosen)
    alternatives = tuple(
        entry for entry in candidates if entry.wire != chosen_wire
    )[:top_k]
    return DecisionFinding(
        decision_sequence=decision_sequence,
        turn=turn,
        phase=phase,
        chosen_order=chosen_order,
        chosen_wire=chosen_wire,
        chosen_rank=rank,
        chosen_score=chosen.score,
        best_order=best.order,
        best_wire=best.wire,
        best_score=best.score,
        score_gap=gap,
        confidence=band,
        finding=finding,
        explanations=explanations,
        alternatives=alternatives,
    )


async def analyze_decision_bundle(
    bundle: dict[str, object],
    *,
    top_k: int = 3,
) -> dict[str, object]:
    """Analyze every ordinary move decision in a saved player-view replay bundle."""

    if bundle.get("schema") != DECISION_REPLAY_SCHEMA:
        raise ValueError(f"expected {DECISION_REPLAY_SCHEMA!r}")

    decisions = bundle.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("bundle is missing decisions")

    config = _config_from_bundle(bundle)
    findings: list[DecisionFinding] = []
    skipped: list[dict[str, object]] = []

    for index, raw in enumerate(decisions):
        if not isinstance(raw, dict):
            skipped.append({"decision_sequence": index, "reason": "malformed decision"})
            continue
        phase = str(raw.get("phase") or "")
        turn = int(raw.get("turn") or 0)
        if phase != "move":
            skipped.append(
                {
                    "decision_sequence": index,
                    "turn": turn,
                    "phase": phase,
                    "reason": "V1 scores ordinary move decisions only",
                }
            )
            continue

        battle = await replay_battle_at_cutoff(bundle, index)
        if not isinstance(battle, DoubleBattle):
            skipped.append(
                {
                    "decision_sequence": index,
                    "turn": turn,
                    "phase": phase,
                    "reason": "reconstructed state is not a doubles battle",
                }
            )
            continue

        scored = search_joint_orders(battle, config)
        candidates = [
            CandidateScore(
                order=describe_order(entry.order),
                wire=choice_wire_message(entry.order),
                score=float(entry.score),
                searched=bool(entry.breakdown.get("searched", False)),
                worst_response=(
                    str(entry.breakdown["worst_response"])
                    if entry.breakdown.get("worst_response") is not None
                    else None
                ),
                exchange_value=(
                    float(entry.breakdown["exchange_value"])
                    if entry.breakdown.get("exchange_value") is not None
                    else None
                ),
                evidence=_candidate_tactical_evidence(battle, entry),
            )
            for entry in scored
        ]
        findings.append(
            compare_candidates(
                decision_sequence=index,
                turn=turn,
                phase=phase,
                chosen_order=(
                    str(raw["chosen_order"]) if raw.get("chosen_order") is not None else None
                ),
                chosen_wire=(
                    str(raw["chosen_order_wire"])
                    if raw.get("chosen_order_wire") is not None
                    else None
                ),
                candidates=candidates,
                top_k=top_k,
            )
        )

    meaningful = [
        finding for finding in findings if finding.confidence in {"moderate", "high"}
    ]
    return {
        "schema": "vgc-game-analysis-v1",
        "battle_tag": bundle.get("battle_tag"),
        "format": bundle.get("format"),
        "player_side": bundle.get("player_side"),
        "player_username": bundle.get("player_username"),
        "score_semantics": (
            "Engine score deltas from the existing engineered search; not win probability."
        ),
        "evidence_semantics": (
            "Opponent damage and Speed evidence uses the analyzer's current hidden-spread "
            "estimate unless the underlying battle data makes it public."
        ),
        "decisions_analyzed": len(findings),
        "meaningful_findings": len(meaningful),
        "findings": [finding.as_dict() for finding in findings],
        "skipped": skipped,
    }


def render_text_report(report: dict[str, object]) -> str:
    """Render a compact, human-readable V1 report."""

    lines = [
        "POKEMON CHAMPIONS GAME ANALYSIS — V1",
        f"Battle: {report.get('battle_tag') or 'unknown'}",
        f"Format: {report.get('format') or 'unknown'}",
        (
            f"Analyzed decisions: {report.get('decisions_analyzed', 0)} | "
            f"Meaningful findings: {report.get('meaningful_findings', 0)}"
        ),
        "",
        "Note: score gaps are engineered search-score deltas, not win probability.",
        (
            "Damage/Speed evidence involving hidden opponent spreads is model-based, "
            "not guaranteed across every legal spread."
        ),
    ]
    for finding in report.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        lines.extend(
            [
                "",
                f"TURN {finding.get('turn')} — {str(finding.get('confidence')).upper()}",
                (
                    f"Played: "
                    f"{finding.get('chosen_order') or finding.get('chosen_wire') or 'unknown'}"
                ),
                str(finding.get("finding") or ""),
            ]
        )
        if finding.get("best_order") is not None:
            lines.append(f"Engine preference: {finding['best_order']}")
        if finding.get("chosen_rank") is not None:
            lines.append(
                f"Rank: {finding['chosen_rank']} | score gap: "
                f"{float(finding.get('score_gap') or 0.0):.1f}"
            )
        explanations = finding.get("explanations") or []
        if explanations:
            lines.append("Evidence:")
            for item in explanations:
                if isinstance(item, dict):
                    lines.append(
                        f"  - [{str(item.get('confidence') or 'unknown').upper()}] "
                        f"{item.get('text')}"
                    )
        alternatives = finding.get("alternatives") or []
        if alternatives:
            lines.append("Top alternatives:")
            for alt in alternatives:
                if isinstance(alt, dict):
                    suffix = (
                        f" | worst response: {alt['worst_response']}"
                        if alt.get("worst_response")
                        else ""
                    )
                    lines.append(
                        f"  - {alt.get('order')} "
                        f"({float(alt.get('score') or 0.0):.1f}){suffix}"
                    )
    return "\n".join(lines)
