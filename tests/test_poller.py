"""Behavioural tests for the background poller + status cache.

The other route tests use ``StubRegistry`` which does not spin up
pollers, so we cover that path here directly. The goal is to pin two
properties:

1. The cache, once populated, is what ``/status`` returns - the route
   does NOT call the live builder on every request.
2. ``request_refresh`` actually wakes the poller before the interval
   elapses, so control endpoints don't have to wait out the timer for
   the cache to reflect the new state.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from kasa_tapo_services.models import EquipmentStatus
from kasa_tapo_services.poller import DevicePoller, StatusCache


def _stub_status(value: str) -> EquipmentStatus:
    """Cheap envelope for assertions; only the message field varies."""

    return EquipmentStatus(
        equipment_id="dev",
        equipment_name="dev",
        equipment_kind="smart_plug",
        equipment_status="ready",
        message=value,
        device_time=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_cache_round_trip() -> None:
    cache = StatusCache()
    assert cache.get("dev") is None
    cache.put("dev", _stub_status("first"))
    got = cache.get("dev")
    assert got is not None
    assert got.message == "first"
    # Age is measurable but small.
    age = cache.age_seconds("dev")
    assert age is not None and 0 <= age < 1.0


@pytest.mark.asyncio
async def test_poller_writes_cache_and_request_refresh_wakes_it() -> None:
    cache = StatusCache()
    calls: list[int] = []

    async def builder() -> EquipmentStatus:
        calls.append(1)
        return _stub_status(f"call_{len(calls)}")

    poller = DevicePoller(
        device_id="dev",
        interval_s=60.0,  # long enough that we'd never hit it in this test
        builder=builder,
        cache=cache,
    )
    poller.start()
    try:
        # Initial poll completes promptly.
        for _ in range(50):
            if cache.get("dev") is not None:
                break
            await asyncio.sleep(0.01)
        first = cache.get("dev")
        assert first is not None and first.message == "call_1"

        # Without request_refresh, no further polls happen for ages
        # (interval=60s). Verify by sleeping and checking call count.
        await asyncio.sleep(0.05)
        assert len(calls) == 1

        # Now request a refresh - the poll task should wake and rebuild.
        poller.request_refresh()
        for _ in range(50):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.01)
        assert len(calls) == 2
        latest = cache.get("dev")
        assert latest is not None and latest.message == "call_2"
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_poller_survives_builder_exception() -> None:
    """A single builder failure must not kill the poll loop; the cache
    keeps the last good envelope while we recover."""

    cache = StatusCache()
    state = {"i": 0}

    async def builder() -> EquipmentStatus:
        state["i"] += 1
        if state["i"] == 2:
            raise RuntimeError("transient device hiccup")
        return _stub_status(f"ok_{state['i']}")

    poller = DevicePoller(
        device_id="dev",
        interval_s=60.0,
        builder=builder,
        cache=cache,
    )
    poller.start()
    try:
        # First poll succeeds.
        for _ in range(50):
            if cache.get("dev") is not None:
                break
            await asyncio.sleep(0.01)
        assert cache.get("dev").message == "ok_1"  # type: ignore[union-attr]

        # Second poll raises - cache should keep ok_1.
        poller.request_refresh()
        for _ in range(50):
            if state["i"] >= 2:
                break
            await asyncio.sleep(0.01)
        assert cache.get("dev").message == "ok_1"  # type: ignore[union-attr]

        # Third poll succeeds.
        poller.request_refresh()
        for _ in range(50):
            if state["i"] >= 3:
                break
            await asyncio.sleep(0.01)
        assert cache.get("dev").message == "ok_3"  # type: ignore[union-attr]
    finally:
        await poller.stop()


def test_plug_status_route_served_from_cache(client: TestClient, stub_registry) -> None:
    """Once the cache has an envelope, /status returns it without hitting
    the underlying KasaPlugClient again. This is the headline win - the
    HS300 emeter sweep happens in the background, not on every poll."""

    bundle = stub_registry.plug("plug_hotplate_strip")
    # Pre-seat the cache with a recognisable envelope. The real poller
    # would write this once per interval.
    cached = _stub_status("from-cache")
    cached.equipment_kind = "power_strip"
    stub_registry.status_cache.put("plug_hotplate_strip", cached)

    r = client.get("/plugs/plug_hotplate_strip/status")
    assert r.status_code == 200
    assert r.json()["message"] == "from-cache"
    # The cache hit path must not have called kasa.state() at all.
    bundle.kasa.state.assert_not_called()


def test_plug_status_route_falls_back_to_live_when_cache_empty(client: TestClient, stub_registry) -> None:
    """No poller running and no cache entry - the route still answers by
    building live. This keeps the cold-start window safe."""

    bundle = stub_registry.plug("plug_hotplate_strip")
    assert stub_registry.status_cache.get("plug_hotplate_strip") is None

    r = client.get("/plugs/plug_hotplate_strip/status")
    assert r.status_code == 200
    # Live build path must have called the underlying client.
    bundle.kasa.state.assert_awaited()
