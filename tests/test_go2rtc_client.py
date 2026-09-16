"""Tests for the go2rtc loopback management client."""

from __future__ import annotations

import httpx
import respx

from kasa_tapo_services.tapo.go2rtc_client import Go2RtcClient


@respx.mock
async def test_add_stream_uses_source_as_src_and_stream_id_as_name() -> None:
    source = "ffmpeg:http://192.0.2.10/video#video=h264"
    route = respx.put(
        "http://relay.test/api/streams",
        params={"src": source, "name": "instrument_main"},
    ).mock(return_value=httpx.Response(200))
    client = Go2RtcClient("http://relay.test")

    try:
        await client.add_stream("instrument_main", source)
    finally:
        await client.close()

    assert route.called
