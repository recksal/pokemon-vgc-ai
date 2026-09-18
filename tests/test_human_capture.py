from __future__ import annotations

from vgc.human_capture import parse_action_selection, parse_preview_selection


def test_parse_preview_selection_accepts_compact_and_spaced_input() -> None:
    assert parse_preview_selection("1234", team_size=6) == (1, 2, 3, 4)
    assert parse_preview_selection("6 2 5 1", team_size=6) == (6, 2, 5, 1)


def test_parse_preview_selection_rejects_duplicates_wrong_count_and_range() -> None:
    assert parse_preview_selection("1123", team_size=6) is None
    assert parse_preview_selection("123", team_size=6) is None
    assert parse_preview_selection("1237", team_size=6) is None


def test_parse_action_selection_uses_human_one_based_numbers() -> None:
    assert parse_action_selection("1", count=4) == 0
    assert parse_action_selection("4", count=4) == 3
    assert parse_action_selection("0", count=4) is None
    assert parse_action_selection("5", count=4) is None


def test_parse_action_selection_supports_explicit_forfeit() -> None:
    assert parse_action_selection("q", count=4) == "forfeit"
    assert parse_action_selection("forfeit", count=4) == "forfeit"
