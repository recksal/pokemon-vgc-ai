from __future__ import annotations

from vgc.game_analyzer import CandidateScore, compare_candidates, render_text_report


def _candidate(
    order: str,
    wire: str,
    score: float,
    *,
    searched: bool = True,
) -> CandidateScore:
    return CandidateScore(
        order=order,
        wire=wire,
        score=score,
        searched=searched,
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


def test_text_report_states_score_semantics() -> None:
    report = {
        "battle_tag": "battle-test",
        "format": "gen9championsvgc2026regmc",
        "decisions_analyzed": 1,
        "meaningful_findings": 0,
        "findings": [],
    }
    rendered = render_text_report(report)
    assert "not win probability" in rendered
