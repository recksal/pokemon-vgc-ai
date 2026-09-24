from __future__ import annotations

import asyncio

import pytest

from offline.analyzer_service import analyze_payload, authorized, normalize_request


def bundle() -> dict[str, object]:
    return {"schema": "vgc-decision-replay-v1", "decisions": []}


def test_normalize_request_accepts_wrapped_and_raw_bundles() -> None:
    assert normalize_request(bundle()) == (bundle(), 3)
    assert normalize_request({"bundle": bundle(), "top_k": 5}) == (bundle(), 5)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"bundle": []},
        {"schema": "wrong"},
        {"bundle": bundle(), "top_k": 0},
        {"bundle": bundle(), "top_k": 11},
        {"bundle": bundle(), "top_k": True},
    ],
)
def test_normalize_request_rejects_invalid_inputs(payload: object) -> None:
    with pytest.raises(ValueError):
        normalize_request(payload)


def test_authorized_uses_optional_bearer_token() -> None:
    assert authorized(None, None)
    assert authorized("Bearer secret", "secret")
    assert not authorized(None, "secret")
    assert not authorized("Bearer wrong", "secret")


def test_analyze_payload_passes_bundle_and_top_k() -> None:
    captured: dict[str, object] = {}

    async def fake_analyzer(
        replay: dict[str, object],
        *,
        top_k: int,
    ) -> dict[str, object]:
        captured.update({"bundle": replay, "top_k": top_k})
        return {"schema": "vgc-game-analysis-v1"}

    report = asyncio.run(
        analyze_payload({"bundle": bundle(), "top_k": 4}, analyzer=fake_analyzer)
    )
    assert report["schema"] == "vgc-game-analysis-v1"
    assert captured == {"bundle": bundle(), "top_k": 4}
