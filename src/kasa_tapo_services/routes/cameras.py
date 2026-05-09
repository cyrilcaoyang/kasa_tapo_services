"""FastAPI router for camera devices (Tapo C-series via ONVIF + pytapo)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import FileResponse

from kasa_tapo_services.config import device_credentials
from kasa_tapo_services.models import (
    CameraDetails,
    ComponentStatus,
    ControlAck,
    EquipmentStatus,
    HealthResponse,
    LensEntry,
    PresetEntry,
    PresetGotoRequest,
    PresetSaveRequest,
    PrivacyRequest,
    ProbeResponse,
    PtzContinuousRequest,
    PtzDirection,
    PtzNudgeRequest,
    RecordingCancelRequest,
    RecordingCancelResponse,
    RecordingStartRequest,
    RecordingStartResponse,
    RecordingStopRequest,
    RecordingStopResponse,
    SnapshotRequest,
    SnapshotResponse,
    StreamingRequest,
)
from kasa_tapo_services.tapo.bootstrap_go2rtc import _stream_name
from kasa_tapo_services.tapo.media import (
    RecordingHandle,
    list_camera_media,
    resolve_media_path,
)

from .registry import CameraClients, DeviceRegistry, get_registry

logger = logging.getLogger(__name__)


# Mapping from a UI direction button to a (pan, tilt) ONVIF velocity vector.
# Values are in [-1, 1]; the diagonal entries normalise so the magnitude is
# the same as cardinal moves (otherwise diagonals would feel ~1.4× faster).
_DIAG = 1.0 / 1.41421356
_DIRECTION_VECTORS: dict[PtzDirection, tuple[float, float]] = {
    "up": (0.0, 1.0),
    "down": (0.0, -1.0),
    "left": (-1.0, 0.0),
    "right": (1.0, 0.0),
    "up_left": (-_DIAG, _DIAG),
    "up_right": (_DIAG, _DIAG),
    "down_left": (-_DIAG, -_DIAG),
    "down_right": (_DIAG, -_DIAG),
    "stop": (0.0, 0.0),
}


def _camera_param(camera_id: str) -> str:
    """Reusable Path() with a regex matching the device-id convention."""

    return camera_id


CameraIdParam = Annotated[
    str,
    Path(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"),
]


def build_camera_router() -> APIRouter:
    router = APIRouter(prefix="/cameras", tags=["cameras"])

    @router.get(
        "/{camera_id}/",
        response_model=ProbeResponse,
        summary="Camera identity probe",
    )
    async def probe(
        camera_id: CameraIdParam,
        registry: DeviceRegistry = Depends(get_registry),
    ) -> ProbeResponse:
        bundle = registry.camera(camera_id)
        return ProbeResponse(
            equipment_id=bundle.config.id,
            equipment_name=bundle.config.name,
        )

    @router.get(
        "/{camera_id}/health",
        response_model=HealthResponse,
        summary="Camera service liveness",
    )
    async def health(
        camera_id: CameraIdParam,
        registry: DeviceRegistry = Depends(get_registry),
    ) -> HealthResponse:
        # Even a known-but-unreachable camera returns ``healthy`` here -
        # liveness is "the gateway process can answer", not "the device
        # is online" (that's what /status is for).
        registry.camera(camera_id)
        return HealthResponse()

    @router.get(
        "/{camera_id}/status",
        response_model=EquipmentStatus,
        summary="Full status envelope (STATUS_SPEC v1.0)",
    )
    async def status(
        camera_id: CameraIdParam,
        registry: DeviceRegistry = Depends(get_registry),
    ) -> EquipmentStatus:
        bundle = registry.camera(camera_id)
        return await _build_status(bundle, registry)

    @router.post(
        "/{camera_id}/control/ptz",
        response_model=ControlAck,
        summary="Move the PTZ head",
    )
    async def ptz(
        camera_id: CameraIdParam,
        body: PtzNudgeRequest | PtzContinuousRequest,
        registry: DeviceRegistry = Depends(get_registry),
    ) -> ControlAck:
        bundle = registry.camera(camera_id)
        if bundle.onvif is None:
            raise HTTPException(status_code=503, detail="ONVIF not configured for this camera")
        try:
            if isinstance(body, PtzNudgeRequest):
                pan, tilt = _DIRECTION_VECTORS[body.direction]
                if body.direction == "stop":
                    await bundle.onvif.stop()
                    return ControlAck(message="stopped")
                await bundle.onvif.nudge(
                    pan=pan * body.speed,
                    tilt=tilt * body.speed,
                    zoom=0.0,
                    duration_ms=body.duration_ms,
                )
                return ControlAck(message=f"nudged {body.direction}")
            else:
                if body.pan == body.tilt == body.zoom == 0.0:
                    await bundle.onvif.stop()
                    return ControlAck(message="stopped")
                await bundle.onvif.continuous_move(
                    pan=body.pan,
                    tilt=body.tilt,
                    zoom=body.zoom,
                    duration_ms=body.duration_ms,
                )
                return ControlAck(message="moving")
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("PTZ failed for %s", camera_id)
            raise HTTPException(status_code=502, detail=f"PTZ failed: {exc}") from exc

    @router.post(
        "/{camera_id}/control/preset/save",
        response_model=ControlAck,
        summary="Save the current view as a named preset",
    )
    async def save_preset(
        camera_id: CameraIdParam,
        body: PresetSaveRequest,
        registry: DeviceRegistry = Depends(get_registry),
    ) -> ControlAck:
        bundle = registry.camera(camera_id)
        if bundle.onvif is None:
            raise HTTPException(status_code=503, detail="ONVIF not configured for this camera")
        try:
            preset_id = await bundle.onvif.save_preset(body.name)
        except Exception as exc:
            logger.exception("preset save failed for %s", camera_id)
            raise HTTPException(status_code=502, detail=f"Save failed: {exc}") from exc
        return ControlAck(state={"preset_id": preset_id, "name": body.name})

    @router.post(
        "/{camera_id}/control/preset/goto",
        response_model=ControlAck,
        summary="Move to a saved preset",
    )
    async def goto_preset(
        camera_id: CameraIdParam,
        body: PresetGotoRequest,
        registry: DeviceRegistry = Depends(get_registry),
    ) -> ControlAck:
        bundle = registry.camera(camera_id)
        if bundle.onvif is None:
            raise HTTPException(status_code=503, detail="ONVIF not configured for this camera")
        try:
            await bundle.onvif.goto_preset(body.preset_id)
        except Exception as exc:
            logger.exception("preset goto failed for %s", camera_id)
            raise HTTPException(status_code=502, detail=f"Goto failed: {exc}") from exc
        return ControlAck(state={"preset_id": body.preset_id})

    @router.delete(
        "/{camera_id}/control/preset/{preset_id}",
        response_model=ControlAck,
        summary="Delete a saved preset",
    )
    async def delete_preset(
        camera_id: CameraIdParam,
        preset_id: str,
        registry: DeviceRegistry = Depends(get_registry),
    ) -> ControlAck:
        bundle = registry.camera(camera_id)
        if bundle.onvif is None:
            raise HTTPException(status_code=503, detail="ONVIF not configured for this camera")
        try:
            await bundle.onvif.delete_preset(preset_id)
        except Exception as exc:
            logger.exception("preset delete failed for %s", camera_id)
            raise HTTPException(status_code=502, detail=f"Delete failed: {exc}") from exc
        return ControlAck(state={"preset_id": preset_id})

    @router.post(
        "/{camera_id}/control/privacy",
        response_model=ControlAck,
        summary="Toggle the camera's privacy (lens-cover) mode",
    )
    async def privacy(
        camera_id: CameraIdParam,
        body: PrivacyRequest,
        registry: DeviceRegistry = Depends(get_registry),
    ) -> ControlAck:
        bundle = registry.camera(camera_id)
        if bundle.tapo is None:
            raise HTTPException(status_code=503, detail="pytapo not configured for this camera")
        try:
            await bundle.tapo.set_privacy_mode(body.enabled)
        except Exception as exc:
            logger.exception("privacy toggle failed for %s", camera_id)
            raise HTTPException(status_code=502, detail=f"Privacy toggle failed: {exc}") from exc
        return ControlAck(state={"privacy_mode": body.enabled})

    @router.post(
        "/{camera_id}/control/snapshot",
        response_model=SnapshotResponse,
        summary="Capture a single JPEG frame from a lens to disk",
    )
    async def snapshot(
        camera_id: CameraIdParam,
        body: SnapshotRequest | None = None,
        registry: DeviceRegistry = Depends(get_registry),
    ) -> SnapshotResponse:
        bundle = registry.camera(camera_id)
        cfg = bundle.config
        creds = device_credentials(cfg.id)
        if not creds.has_basic:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Camera {cfg.id!r} has no RTSP credentials; "
                    f"set {cfg.id.upper()}_USER / {cfg.id.upper()}_PASS"
                ),
            )
        try:
            path, lens_id, _label = await registry.media.take_snapshot(
                camera=cfg,
                creds=creds,
                lens_id=(body.lens if body else None),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        return SnapshotResponse(
            path=str(path),
            url=f"/cameras/{cfg.id}/media/snapshots/{lens_id}/{path.name}",
            taken_at=datetime.now(timezone.utc),
            lens=lens_id,
            bytes=path.stat().st_size if path.exists() else None,
        )

    @router.post(
        "/{camera_id}/control/recording/start",
        response_model=RecordingStartResponse,
        summary="Start recording the lens to a server-side MP4 (stream copy)",
    )
    async def recording_start(
        camera_id: CameraIdParam,
        body: RecordingStartRequest | None = None,
        registry: DeviceRegistry = Depends(get_registry),
    ) -> RecordingStartResponse:
        bundle = registry.camera(camera_id)
        cfg = bundle.config
        body = body or RecordingStartRequest()

        # Refuse a second concurrent recording on the same lens; users
        # who want parallel wide+tele can issue two calls with explicit
        # `lens` values and they'll land in different dict slots.
        target_lens = body.lens or (cfg.lenses[0].id if cfg.lenses else None)
        for h in bundle.recordings.values():
            if h.lens_id == target_lens and h.is_running:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"camera {cfg.id} lens {h.lens_id} is already recording "
                        f"as {h.recording_id}"
                    ),
                )

        creds = device_credentials(cfg.id)
        if not creds.has_basic:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Camera {cfg.id!r} has no RTSP credentials; "
                    f"set {cfg.id.upper()}_USER / {cfg.id.upper()}_PASS"
                ),
            )
        try:
            handle = await registry.media.start_recording(
                camera=cfg,
                creds=creds,
                lens_id=body.lens,
                max_duration_s=body.max_duration_s,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        bundle.recordings[handle.recording_id] = handle
        return RecordingStartResponse(
            recording_id=handle.recording_id,
            path=str(handle.target_path),
            url=f"/cameras/{cfg.id}/media/recordings/{handle.lens_id}/{handle.target_path.name}",
            lens=handle.lens_id,
            started_at=handle.started_at,
            max_duration_s=handle.max_duration_s,
        )

    def _resolve_recording(
        bundle: CameraClients, recording_id: str | None
    ) -> RecordingHandle:
        if recording_id is not None:
            handle = bundle.recordings.get(recording_id)
            if handle is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"camera {bundle.config.id} has no recording {recording_id}",
                )
            return handle
        running = [h for h in bundle.recordings.values() if h.is_running]
        if not running:
            raise HTTPException(
                status_code=409,
                detail=f"camera {bundle.config.id} has no active recording",
            )
        if len(running) > 1:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"camera {bundle.config.id} has {len(running)} active recordings; "
                    "specify recording_id"
                ),
            )
        return running[0]

    @router.post(
        "/{camera_id}/control/recording/stop",
        response_model=RecordingStopResponse,
        summary="Stop a recording and finalise the MP4",
    )
    async def recording_stop(
        camera_id: CameraIdParam,
        body: RecordingStopRequest | None = None,
        registry: DeviceRegistry = Depends(get_registry),
    ) -> RecordingStopResponse:
        bundle = registry.camera(camera_id)
        handle = _resolve_recording(bundle, body.recording_id if body else None)
        try:
            final_path = await registry.media.stop_recording(handle)
        except Exception as exc:
            logger.exception("recording stop failed for %s", handle.recording_id)
            raise HTTPException(status_code=502, detail=f"Stop failed: {exc}") from exc

        stopped_at = datetime.now(timezone.utc)
        bundle.recordings.pop(handle.recording_id, None)
        return RecordingStopResponse(
            recording_id=handle.recording_id,
            path=str(final_path),
            url=f"/cameras/{bundle.config.id}/media/recordings/{handle.lens_id}/{final_path.name}",
            started_at=handle.started_at,
            stopped_at=stopped_at,
            duration_ms=int((stopped_at - handle.started_at).total_seconds() * 1000),
            bytes=final_path.stat().st_size if final_path.exists() else None,
            finalized=final_path.suffix == ".mp4",
        )

    @router.post(
        "/{camera_id}/control/recording/cancel",
        response_model=RecordingCancelResponse,
        summary="Abort a recording and delete the partial file",
    )
    async def recording_cancel(
        camera_id: CameraIdParam,
        body: RecordingCancelRequest | None = None,
        registry: DeviceRegistry = Depends(get_registry),
    ) -> RecordingCancelResponse:
        bundle = registry.camera(camera_id)
        handle = _resolve_recording(bundle, body.recording_id if body else None)
        try:
            deleted = await registry.media.cancel_recording(handle)
        except Exception as exc:
            logger.exception("recording cancel failed for %s", handle.recording_id)
            raise HTTPException(status_code=502, detail=f"Cancel failed: {exc}") from exc
        bundle.recordings.pop(handle.recording_id, None)
        return RecordingCancelResponse(
            recording_id=handle.recording_id,
            deleted_path=str(deleted) if deleted else None,
        )

    @router.get(
        "/{camera_id}/media",
        summary="List captured snapshots and recordings for a camera",
    )
    async def media_list(
        camera_id: CameraIdParam,
        registry: DeviceRegistry = Depends(get_registry),
    ) -> dict:
        bundle = registry.camera(camera_id)
        return list_camera_media(bundle.config)

    @router.get(
        "/{camera_id}/media/{kind}/{lens}/{name}",
        summary="Download a saved snapshot or recording file",
        response_class=FileResponse,
    )
    async def media_download(
        camera_id: CameraIdParam,
        kind: str,
        lens: str,
        name: str,
        registry: DeviceRegistry = Depends(get_registry),
    ) -> FileResponse:
        bundle = registry.camera(camera_id)
        try:
            target = resolve_media_path(bundle.config, kind=kind, lens=lens, name=name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"file not found: {name}")
        return FileResponse(target, filename=target.name)

    @router.post(
        "/{camera_id}/control/streaming",
        response_model=ControlAck,
        summary="Show or hide the camera's MSE feed in the dashboard",
    )
    async def streaming(
        camera_id: CameraIdParam,
        body: StreamingRequest,
        registry: DeviceRegistry = Depends(get_registry),
    ) -> ControlAck:
        # We deliberately do NOT mutate go2rtc here. The streams are
        # rendered into ``go2rtc.yaml`` by ``kasa-tapo-bootstrap-go2rtc``
        # at startup with full credentials, and go2rtc only pulls from
        # the RTSP source when a consumer is connected. Flipping the
        # in-memory ``streaming_enabled`` flag is enough: when False,
        # ``_build_status`` omits each lens's ``mse_url``, the frontend
        # MsePlayer renders "Streaming disabled" instead of opening a
        # WebSocket, and go2rtc drops back to its idle no-consumer state.
        bundle = registry.camera(camera_id)
        bundle.streaming_enabled = body.enabled
        return ControlAck(state={"streaming_enabled": body.enabled})

    return router


# ---------------------------------------------------------------------
# /status envelope assembly.
# ---------------------------------------------------------------------


async def _build_status(bundle: CameraClients, registry: DeviceRegistry) -> EquipmentStatus:
    """Compose a full v1.0 envelope for one camera.

    We poll ONVIF (for presets), pytapo (for privacy mode), and go2rtc
    (for source health) in parallel; if any of them fail we degrade
    gracefully and surface the failure on the corresponding component.
    """

    import asyncio

    cfg = bundle.config

    # Lens entries from devices.yaml + the live MSE URL each lens would
    # be at if it's currently published by go2rtc.
    lenses: list[LensEntry] = []
    components: dict[str, ComponentStatus] = {}

    onvif_reachable = False
    tapo_reachable = False
    go2rtc_reachable = False
    presets: list[PresetEntry] = []
    privacy_mode = False

    onvif_task = asyncio.create_task(_probe_onvif(bundle))
    tapo_task = asyncio.create_task(_probe_tapo(bundle))
    go2rtc_task = asyncio.create_task(registry.go2rtc.is_reachable())

    onvif_reachable, presets = await onvif_task
    tapo_reachable, privacy_mode = await tapo_task
    go2rtc_reachable = await go2rtc_task

    # ``streaming_enabled`` is an explicit per-camera flag controlled by
    # the dashboard toggle (see /control/streaming). It is NOT computed
    # from "is a consumer currently connected" - that produced a chicken-
    # and-egg deadlock where the player refused to open the WebSocket
    # because it thought streaming was off, which kept the consumer
    # count at zero, which kept the flag false.
    streaming_enabled = bundle.streaming_enabled

    # Index in-flight recordings by lens so we can decorate each
    # LensEntry below. A given lens has at most one active recording
    # (start_recording rejects duplicates) so the dict is single-valued.
    active_recordings = {
        h.lens_id: h for h in bundle.recordings.values() if h.is_running
    }

    # Per-lens stream health (informational only; we do not gate the
    # equipment_status on it).
    for lens in cfg.lenses or []:
        stream = _stream_name(cfg, lens.id)
        state = "unknown"
        connected = False
        if go2rtc_reachable:
            try:
                state = await registry.go2rtc.stream_state(stream)
                connected = state == "connected"
            except Exception as exc:
                state = f"error: {exc}"
        rec = active_recordings.get(lens.id)
        lenses.append(
            LensEntry(
                id=lens.id,
                label=lens.label,
                rtsp_path=lens.rtsp_path,
                # The browser hits go2rtc through Caddy under /streams/api/ws.
                # Hide the URL when streaming is disabled so the MsePlayer
                # renders the disabled state instead of opening a socket.
                mse_url=(
                    f"/streams/api/ws?src={stream}" if streaming_enabled else None
                ),
                stream_connected=connected,
                recording_active=rec is not None,
                recording_started_at=rec.started_at if rec else None,
            )
        )
        components[f"lens_{lens.id}"] = ComponentStatus(
            connected=connected,
            state=state,
            message=lens.label,
        )

    # Health roll-up driven entirely by which control planes responded.
    # We treat live-stream consumer state as informational - the camera
    # is "ready" as long as ONVIF + go2rtc are both up and the user has
    # not turned streaming off; consumer count is incidental.
    last_error_message: str | None = None
    if onvif_reachable and go2rtc_reachable and streaming_enabled:
        equipment_state = "ready"
    elif onvif_reachable and not streaming_enabled:
        equipment_state = "ready"
        last_error_message = "Streaming disabled by user"
    elif onvif_reachable and not go2rtc_reachable:
        equipment_state = "degraded"
        last_error_message = "ONVIF up but go2rtc unreachable; live video disabled"
    elif tapo_reachable:
        equipment_state = "degraded"
        last_error_message = "Tapo API up but ONVIF unreachable; PTZ disabled"
    else:
        equipment_state = "error"
        last_error_message = "Neither ONVIF nor Tapo API responded"

    details = CameraDetails(
        lenses=lenses,
        presets=presets,
        privacy_mode=privacy_mode,
        streaming_enabled=streaming_enabled,
        onvif_reachable=onvif_reachable,
        tapo_reachable=tapo_reachable,
        go2rtc_reachable=go2rtc_reachable,
    ).model_dump(mode="json")

    allowed: list[str] = []
    if onvif_reachable:
        allowed += ["ptz", "preset/save", "preset/goto", "preset/{id}"]
    if tapo_reachable:
        allowed += ["privacy"]
    if go2rtc_reachable:
        allowed += ["streaming"]
    # Snapshot / recording only require RTSP credentials, which we
    # don't probe per-request - if the gateway has them configured at
    # boot time they're guaranteed to still be present here. We always
    # advertise these so the UI can show the buttons even when ONVIF
    # is briefly down.
    allowed += ["snapshot", "recording/start", "recording/stop", "recording/cancel"]

    return EquipmentStatus(
        equipment_id=cfg.id,
        equipment_name=cfg.name,
        equipment_kind="camera",
        host=cfg.host,
        equipment_status=equipment_state,
        message=last_error_message,
        device_time=datetime.now(timezone.utc),
        components=components,
        details=details,
        allowed_actions=allowed,
    )


async def _probe_onvif(bundle: CameraClients) -> tuple[bool, list[PresetEntry]]:
    if bundle.onvif is None:
        return False, []
    if not await bundle.onvif.is_reachable():
        return False, []
    try:
        presets = await bundle.onvif.list_presets()
    except Exception:
        presets = []
    return True, presets


async def _probe_tapo(bundle: CameraClients) -> tuple[bool, bool]:
    if bundle.tapo is None:
        return False, False
    privacy = await bundle.tapo.privacy_mode()
    if privacy is None:
        return False, False
    return True, bool(privacy)


__all__ = ["build_camera_router"]
