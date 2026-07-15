"""Unit tests for PTZ-limit detection in ``OnvifCameraClient.nudge``.

The client's ONVIF calls are replaced with fakes so no connection happens;
only the before/after position comparison logic is under test. Timing
constants are patched so the tests don't sleep for real.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from kasa_tapo_services.tapo import onvif_client as mod
from kasa_tapo_services.tapo.onvif_client import OnvifCameraClient, PtzNudgeOutcome


@pytest.fixture(autouse=True)
def _fast_settle(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mod, "_POSITION_SETTLE_S", 0.0)


def _client(positions: list[tuple[float, float] | None]) -> OnvifCameraClient:
    """A client whose ONVIF surface is faked; ``get_position`` pops from
    ``positions`` (before-read first, then after-read)."""

    client = OnvifCameraClient("203.0.113.1", 2020, "u", "p")
    client.continuous_move = AsyncMock()  # type: ignore[method-assign]
    client.stop = AsyncMock()  # type: ignore[method-assign]
    client.get_position = AsyncMock(side_effect=positions)  # type: ignore[method-assign]
    return client


async def test_nudge_moved_is_not_limit() -> None:
    client = _client([(0.30, -0.50), (0.24, -0.50)])
    outcome = await client.nudge(pan=-0.5, tilt=0.0, zoom=0.0, duration_ms=200)
    assert outcome == PtzNudgeOutcome(limited_axes=(), detected=True)
    assert not outcome.limit_hit


async def test_nudge_pinned_position_reports_limit() -> None:
    client = _client([(1.0, -0.50), (1.0, -0.50)])
    outcome = await client.nudge(pan=0.5, tilt=0.0, zoom=0.0, duration_ms=200)
    assert outcome.detected
    assert outcome.limited_axes == ("pan",)
    assert outcome.limit_hit


async def test_diagonal_nudge_reports_only_pinned_axis() -> None:
    # Pan glides along while tilt is pinned at the limit.
    client = _client([(0.30, -1.0), (0.24, -1.0)])
    outcome = await client.nudge(pan=-0.5, tilt=-0.5, zoom=0.0, duration_ms=200)
    assert outcome.limited_axes == ("tilt",)


async def test_no_position_support_degrades_gracefully() -> None:
    client = _client([None])
    outcome = await client.nudge(pan=0.5, tilt=0.0, zoom=0.0, duration_ms=200)
    assert outcome == PtzNudgeOutcome()
    assert not outcome.detected and not outcome.limit_hit


async def test_after_read_failure_degrades_gracefully() -> None:
    client = _client([(0.30, -0.50), None])
    outcome = await client.nudge(pan=0.5, tilt=0.0, zoom=0.0, duration_ms=200)
    assert outcome == PtzNudgeOutcome()


async def test_too_small_command_skips_detection() -> None:
    # 0.1 speed * 100 ms = 10 < _MIN_DETECTABLE_CMD: a pinned-looking
    # position must NOT be reported as a limit.
    client = _client([])
    outcome = await client.nudge(pan=0.1, tilt=0.0, zoom=0.0, duration_ms=100)
    assert outcome == PtzNudgeOutcome()
    client.get_position.assert_not_awaited()  # type: ignore[attr-defined]


async def test_stop_failure_skips_detection() -> None:
    # If Stop failed the head may still be moving; a position comparison
    # would be meaningless, so detection is abandoned.
    client = _client([(0.30, -0.50), (0.30, -0.50)])
    client.stop = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    outcome = await client.nudge(pan=0.5, tilt=0.0, zoom=0.0, duration_ms=100)
    assert outcome == PtzNudgeOutcome()
