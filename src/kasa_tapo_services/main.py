"""FastAPI entrypoint for the kasa-tapo-services gateway.

Composes one camera router and one plug router and exposes a small
gateway-level surface (``GET /``, ``GET /health``, ``GET /devices``).
The gateway always allows a CORS origin matching the dashboard server
so the routes are debuggable from a browser even when the aggregator
is down (mirrors STATUS_SPEC v1.0 best practice 10).
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .routes import build_camera_router, build_plug_router
from .routes.registry import DeviceRegistry, build_registry_from_disk

logger = logging.getLogger(__name__)


def _cors_origins() -> list[str]:
    raw = os.environ.get("KASA_TAPO_CORS_ORIGINS", "http://100.64.254.6:8000,http://sdl2-server-gaia.tail6a1dd7.ts.net:8000")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _poll_interval_env(name: str) -> float | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number, ignoring", name, raw)
        return None
    if value <= 0:
        logger.warning("%s=%r must be > 0, ignoring", name, raw)
        return None
    return value


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    registry: DeviceRegistry = await build_registry_from_disk()
    app.state.registry = registry
    app.state.boot_time = datetime.now(timezone.utc)
    registry.start_pollers(
        plug_interval_s=_poll_interval_env("KASA_TAPO_PLUG_POLL_INTERVAL_S"),
        camera_interval_s=_poll_interval_env("KASA_TAPO_CAMERA_POLL_INTERVAL_S"),
    )
    logger.info(
        "kasa-tapo-services up: %d cameras, %d plugs",
        len(registry.list_cameras()),
        len(registry.list_plugs()),
    )
    try:
        yield
    finally:
        await registry.aclose()


app = FastAPI(
    title="kasa-tapo-services",
    description=(
        "Gateway service that exposes Kasa smart plugs (HS103, HS300) and "
        "Tapo cameras (C200/C210/C220/C225/C245D) as STATUS_SPEC v1.0 "
        "equipment to the AC Organic Self-driving Lab dashboard."
    ),
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

app.include_router(build_camera_router())
app.include_router(build_plug_router())


@app.get("/", tags=["meta"])
async def root() -> dict:
    """Service-level identity probe.

    Distinct from the per-device ``/cameras/{id}/`` and ``/plugs/{id}/``
    probes; this is what a human curls to find out which devices the
    gateway is hosting.
    """

    registry: DeviceRegistry = app.state.registry
    return {
        "service": "kasa-tapo-services",
        "version": __version__,
        "boot_time": app.state.boot_time.isoformat(),
        "device_count": len(registry.config.devices),
    }


@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {"status": "healthy"}


@app.get("/devices", tags=["meta"])
async def list_devices() -> dict:
    """Enumerate every device the gateway is hosting (without polling them)."""

    registry: DeviceRegistry = app.state.registry
    return {
        "cameras": [
            {
                "id": cam.config.id,
                "name": cam.config.name,
                "host": cam.config.host,
                "lenses": [lens.model_dump() for lens in (cam.config.lenses or [])],
                "tapo_configured": cam.tapo is not None,
                "onvif_configured": cam.onvif is not None,
            }
            for cam in registry.list_cameras()
        ],
        "plugs": [
            {
                "id": plug.config.id,
                "name": plug.config.name,
                "kind": plug.config.kind,
                "host": plug.config.host,
                "outlets": [outlet.model_dump() for outlet in (plug.config.outlets or [])],
            }
            for plug in registry.list_plugs()
        ],
    }


__all__ = ["app"]
