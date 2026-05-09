"""FastAPI router for Kasa plug devices (HS103 single + HS300 strip)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path

from kasa_tapo_services.kasa.plug_client import PlugState
from kasa_tapo_services.models import (
    ComponentStatus,
    ControlAck,
    EquipmentStatus,
    HealthResponse,
    MetricValue,
    PlugSwitchRequest,
    ProbeResponse,
)

from .registry import DeviceRegistry, PlugClients, get_registry

logger = logging.getLogger(__name__)

PlugIdParam = Annotated[
    str,
    Path(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"),
]


def build_plug_router() -> APIRouter:
    router = APIRouter(prefix="/plugs", tags=["plugs"])

    @router.get(
        "/{plug_id}/",
        response_model=ProbeResponse,
        summary="Plug identity probe",
    )
    async def probe(
        plug_id: PlugIdParam,
        registry: DeviceRegistry = Depends(get_registry),
    ) -> ProbeResponse:
        bundle = registry.plug(plug_id)
        return ProbeResponse(equipment_id=bundle.config.id, equipment_name=bundle.config.name)

    @router.get(
        "/{plug_id}/health",
        response_model=HealthResponse,
        summary="Plug service liveness",
    )
    async def health(
        plug_id: PlugIdParam,
        registry: DeviceRegistry = Depends(get_registry),
    ) -> HealthResponse:
        registry.plug(plug_id)
        return HealthResponse()

    @router.get(
        "/{plug_id}/status",
        response_model=EquipmentStatus,
        summary="Full status envelope (STATUS_SPEC v1.0)",
    )
    async def status(
        plug_id: PlugIdParam,
        registry: DeviceRegistry = Depends(get_registry),
    ) -> EquipmentStatus:
        bundle = registry.plug(plug_id)
        return await _build_status(bundle)

    @router.post(
        "/{plug_id}/control/on",
        response_model=ControlAck,
        summary="Turn plug or outlet on",
    )
    async def turn_on(
        plug_id: PlugIdParam,
        body: PlugSwitchRequest | None = None,
        registry: DeviceRegistry = Depends(get_registry),
    ) -> ControlAck:
        return await _switch(registry.plug(plug_id), "on", body)

    @router.post(
        "/{plug_id}/control/off",
        response_model=ControlAck,
        summary="Turn plug or outlet off",
    )
    async def turn_off(
        plug_id: PlugIdParam,
        body: PlugSwitchRequest | None = None,
        registry: DeviceRegistry = Depends(get_registry),
    ) -> ControlAck:
        return await _switch(registry.plug(plug_id), "off", body)

    @router.post(
        "/{plug_id}/control/toggle",
        response_model=ControlAck,
        summary="Toggle plug or outlet",
    )
    async def toggle(
        plug_id: PlugIdParam,
        body: PlugSwitchRequest | None = None,
        registry: DeviceRegistry = Depends(get_registry),
    ) -> ControlAck:
        return await _switch(registry.plug(plug_id), "toggle", body)

    return router


# ---------------------------------------------------------------------
# /status envelope + control helper.
# ---------------------------------------------------------------------


async def _build_status(bundle: PlugClients) -> EquipmentStatus:
    cfg = bundle.config

    state: PlugState | None = None
    error_message: str | None = None
    try:
        state = await bundle.kasa.state()
    except Exception as exc:
        logger.warning("kasa state failed for %s: %s", cfg.id, exc)
        error_message = str(exc)

    if state is None:
        return EquipmentStatus(
            equipment_id=cfg.id,
            equipment_name=cfg.name,
            equipment_kind=cfg.kind,
            host=cfg.host,
            equipment_status="error",
            message=error_message or "Kasa device unreachable",
            device_time=datetime.now(timezone.utc),
        )

    components: dict[str, ComponentStatus] = {}
    metrics: dict[str, MetricValue] = {}
    details: dict = {"is_strip": state.is_strip, "alias": state.alias}

    if state.is_strip and bundle.config.outlets:
        # Decorate each outlet with its registry-side label so the
        # dashboard can render meaningful titles per outlet.
        labels_by_index = {o.index: o.label for o in bundle.config.outlets}
        for outlet in state.outlets:
            label = labels_by_index.get(outlet.index) or outlet.label or f"Outlet {outlet.index}"
            components[f"outlet_{outlet.index}"] = ComponentStatus(
                connected=True,
                state="on" if outlet.is_on else "off",
                message=label,
            )
            if outlet.power_w is not None:
                metrics[f"power_outlet_{outlet.index}"] = MetricValue(
                    value=round(outlet.power_w, 2), unit="W"
                )
        details["outlets"] = [
            {
                "index": outlet.index,
                "label": labels_by_index.get(outlet.index) or outlet.label,
                "is_on": outlet.is_on,
                "power_w": outlet.power_w,
            }
            for outlet in state.outlets
        ]
    else:
        components["plug"] = ComponentStatus(
            connected=True,
            state="on" if state.is_on else "off",
        )

    if state.rssi is not None:
        metrics["rssi"] = MetricValue(value=state.rssi, unit="dBm")
    if state.model:
        details["model"] = state.model

    return EquipmentStatus(
        equipment_id=cfg.id,
        equipment_name=cfg.name,
        equipment_kind=cfg.kind,
        host=cfg.host,
        equipment_status="ready",
        device_time=datetime.now(timezone.utc),
        components=components,
        metrics=metrics,
        details=details,
        allowed_actions=["on", "off", "toggle"],
    )


async def _switch(
    bundle: PlugClients,
    action: str,
    body: PlugSwitchRequest | None,
) -> ControlAck:
    outlet = body.outlet if body is not None else None
    try:
        if action == "on":
            await bundle.kasa.turn_on(outlet)
            new_state = True
        elif action == "off":
            await bundle.kasa.turn_off(outlet)
            new_state = False
        elif action == "toggle":
            new_state = await bundle.kasa.toggle(outlet)
        else:  # pragma: no cover - guarded by caller
            raise HTTPException(status_code=400, detail=f"Unknown action: {action}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("plug %s %s failed", bundle.config.id, action)
        raise HTTPException(status_code=502, detail=f"{action} failed: {exc}") from exc
    return ControlAck(state={"outlet": outlet, "is_on": new_state})


__all__ = ["build_plug_router"]
