from __future__ import annotations

from vgc.game_analyzer import (
    CandidateScore,
    TacticalEvidence,
    compare_candidates,
    render_text_report,
)


def _candidate(
    order: str,
    wire: str,
    score: float,
    *,
    searched: bool = True,
    evidence: tuple[TacticalEvidence, ...] = (),
) -> CandidateScore:
    return CandidateScore(
        order=order,
        wire=wire,
        score=score,
        searched=searched,
        evidence=evidence,
    )


def test_compare_candidates_marks_top_choice_as_no_issue() -> None:
    candidates = [
        _candidate("psychic@1 / eruption@2", "/choose move 1 +1, move 1 +2", 150.0),
        _candidate("protect / eruption@2", "/choose move 4, move 1 +2", 112.0),
    ]
    finding = compare_candidates(
        decision_sequence=1,
        turn=2,
        phase="move",
        chosen_order=candidates[0].order,
        chosen_wire=candidates[0].wire,
        candidates=candidates,
    )
    assert finding.chosen_rank == 1
    assert finding.score_gap == 0.0
    assert finding.confidence == "none"
    assert finding.explanations == ()


def test_compare_candidates_flags_large_searched_disagreement() -> None:
    candidates = [
        _candidate("psychic@1 / eruption@2", "/choose move 1 +1, move 1 +2", 180.0),
        _candidate("protect / eruption@2", "/choose move 4, move 1 +2", 132.0),
        _candidate("helpinghand / eruption@2", "/choose move 2, move 1 +2", 90.0),
    ]
    finding = compare_candidates(
        decision_sequence=2,
        turn=3,
        phase="move",
        chosen_order=candidates[2].order,
        chosen_wire=candidates[2].wire,
        candidates=candidates,
    )
    assert finding.chosen_rank == 3
    assert finding.score_gap == 90.0
    assert finding.confidence == "high"
    assert finding.best_order == candidates[0].order


def test_difference_evidence_surfaces_missed_ko_and_lethal_exposure() -> None:
    guaranteed = TacticalEvidence(
        kind="guaranteed_ko",
        confidence="model",
        actor="farigiraf",
        target="sneasler",
        text=(
            "farigiraf's psychic is a guaranteed KO on sneasler against "
            "the analyzer's current hidden-spread estimate."
        ),
    )
    speed = TacticalEvidence(
        kind="speed_benchmark",
        confidence="model",
        actor="farigiraf",
        target="sneasler",
        text=(
            "Estimated effective Speed benchmark: farigiraf 80.0 vs sneasler 189.0; "
            "under Trick Room, within equal priority this favors farigiraf."
        ),
    )
    exposure = TacticalEvidence(
        kind="lethal_exposure",
        confidence="model",
        actor="typhlosionhisui",
        text=(
            "This line leaves typhlosionhisui exposed to a modeled lethal threat "
            "without first removing that threat."
        ),
    )
    candidates = [
        _candidate(
            "psychic@1 / shadowball@2",
            "/choose best",
            180.0,
            evidence=(guaranteed, speed),
        ),
        _candidate(
            "helpinghand / shadowball@2",
            "/choose played",
            105.0,
            evidence=(exposure,),
        ),
    ]
    finding = compare_candidates(
        decision_sequence=2,
        turn=3,
        phase="move",
        chosen_order=candidates[1].order,
        chosen_wire=candidates[1].wire,
        candidates=candidates,
    )
    assert finding.confidence == "moderate"
    texts = [item.text for item in finding.explanations]
    assert any(text.startswith("Preferred line:") and "guaranteed KO" in text for text in texts)
    assert any(text.startswith("Played line:") and "lethal threat" in text for text in texts)


def test_shared_tactical_fact_is_not_presented_as_difference() -> None:
    shared = TacticalEvidence(
        kind="guaranteed_ko",
        confidence="model",
        actor="mawile",
        target="salamence",
        text="same tactical fact",
    )
    candidates = [
        _candidate("best", "/choose best", 140.0, evidence=(shared,)),
        _candidate("played", "/choose played", 100.0, evidence=(shared,)),
    ]
    finding = compare_candidates(
        decision_sequence=0,
        turn=1,
        phase="move",
        chosen_order="played",
        chosen_wire="/choose played",
        candidates=candidates,
    )
    assert finding.explanations == ()


def test_unsearched_tail_is_never_called_high_confidence() -> None:
    candidates = [
        _candidate("best", "/choose best", 100.0),
        _candidate("tail", "/choose tail", 0.0, searched=False),
    ]
    finding = compare_candidates(
        decision_sequence=0,
        turn=1,
        phase="move",
        chosen_order="tail",
        chosen_wire="/choose tail",
        candidates=candidates,
    )
    assert finding.confidence == "candidate"


def test_missing_wire_match_is_reported_as_unavailable() -> None:
    finding = compare_candidates(
        decision_sequence=0,
        turn=1,
        phase="move",
        chosen_order="unknown",
        chosen_wire="/choose missing",
        candidates=[_candidate("best", "/choose best", 100.0)],
    )
    assert finding.confidence == "unavailable"
    assert finding.chosen_rank is None


def test_text_report_states_score_and_hidden_spread_semantics() -> None:
    report = {
        "battle_tag": "battle-test",
        "format": "gen9championsvgc2026regmc",
        "decisions_analyzed": 1,
        "meaningful_findings": 0,
        "findings": [],
    }
    rendered = render_text_report(report)
    assert "not win probability" in rendered
    assert "hidden opponent spreads is model-based" in rendered


def test_text_report_renders_tactical_evidence() -> None:
    report = {
        "battle_tag": "battle-test",
        "format": "gen9championsvgc2026regmc",
        "decisions_analyzed": 1,
        "meaningful_findings": 1,
        "findings": [
            {
                "turn": 2,
                "confidence": "moderate",
                "chosen_order": "helpinghand / eruption",
                "finding": "Meaningful engine disagreement.",
                "best_order": "psychic@1 / eruption",
                "chosen_rank": 2,
                "score_gap": 45.0,
                "explanations": [
                    {
                        "confidence": "model",
                        "text": "Preferred line: Farigiraf's Psychic is a guaranteed KO.",
                    }
                ],
                "alternatives": [],
            }
        ],
    }
    rendered = render_text_report(report)
    assert "Evidence:" in rendered
    assert "[MODEL]" in rendered
    assert "guaranteed KO" in rendered
