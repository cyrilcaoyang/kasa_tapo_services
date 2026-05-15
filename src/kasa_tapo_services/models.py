"""Lab equipment status spec v1.0 envelope (vendored) + control bodies.

The first half of this file is a verbatim mirror of
``ac-organic-lab/skills/src/lab_skills/models.py`` (the STATUS_SPEC v1.0
contract). It is kept identical so the dashboard's ``HttpStatusAdapter``
parses our responses without any per-device translation. The
``EquipmentKind`` literal is extended here with the three kinds this
gateway emits (``camera``, ``smart_plug``, ``power_strip``) - the
dashboard's ``lab_skills`` mirror has the same additions.

The second half is the gateway-only request/response bodies: PTZ moves,
preset save/goto, privacy/streaming toggles, plug on/off.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

PROTOCOL_VERSION = "1.0"


EquipmentKind = Literal[
    "solid_doser",
    "liquid_handler",
    "press",
    "fume_hood",
    "robot_arm",
    "environmental_sensor",
    "hplc",
    "plate_reader",
    "plate_sealer",
    "plate_stacker",
    # Extensions emitted by this gateway. Mirrored in
    # ac-organic-lab/skills/src/lab_skills/models.py.
    "camera",
    "smart_plug",
    "power_strip",
    "other",
]


EquipmentState = Literal[
    "ready",
    "busy",
    "requires_init",
    "degraded",
    "dry_run",
    "error",
    "e_stop",
    "unknown",
]

ErrorSeverity = Literal["info", "warning", "error", "critical"]


class ComponentStatus(BaseModel):
    connected: bool
    state: str
    message: str | None = None
    last_event_at: datetime | None = None


class MetricValue(BaseModel):
    value: float | int | str | bool
    unit: str | None = None
    timestamp: datetime | None = None


class ErrorInfo(BaseModel):
    code: str | None = None
    message: str
    severity: ErrorSeverity
    timestamp: datetime


class EquipmentStatus(BaseModel):
    """The unified ``GET /status`` envelope every equipment must return."""

    protocol_version: str = PROTOCOL_VERSION

    equipment_id: str
    equipment_name: str
    equipment_kind: EquipmentKind
    equipment_version: str | None = None
    host: str | None = None

    equipment_status: EquipmentState
    message: str | None = None
    required_actions: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=list)

    device_time: datetime
    uptime_seconds: float | None = None

    components: dict[str, ComponentStatus] = Field(default_factory=dict)
    metrics: dict[str, MetricValue] = Field(default_factory=dict)
    last_error: ErrorInfo | None = None

    details: dict[str, Any] = Field(default_factory=dict)


class ProbeResponse(BaseModel):
    equipment_id: str
    equipment_name: str
    protocol_version: str = PROTOCOL_VERSION


class HealthResponse(BaseModel):
    status: Literal["healthy"] = "healthy"


# ---------------------------------------------------------------------
# Camera-specific request bodies and details schemas.
# ---------------------------------------------------------------------


PtzDirection = Literal["up", "down", "left", "right", "up_left", "up_right", "down_left", "down_right", "stop"]


class PtzNudgeRequest(BaseModel):
    """Discrete PTZ nudge (mousedown→mouseup pattern from the UI)."""

    direction: PtzDirection
    speed: float = Field(default=0.5, ge=0.0, le=1.0)
    duration_ms: int = Field(default=400, ge=0, le=5000)


class PtzContinuousRequest(BaseModel):
    """Direct continuous PTZ vector. ``stop`` is implied by all-zero values."""

    pan: float = Field(default=0.0, ge=-1.0, le=1.0)
    tilt: float = Field(default=0.0, ge=-1.0, le=1.0)
    zoom: float = Field(default=0.0, ge=-1.0, le=1.0)
    duration_ms: int | None = Field(default=None, ge=0, le=10_000)


class PresetSaveRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class PresetGotoRequest(BaseModel):
    preset_id: str


class PrivacyRequest(BaseModel):
    enabled: bool


class StreamingRequest(BaseModel):
    enabled: bool


class PresetEntry(BaseModel):
    id: str
    name: str


class LensEntry(BaseModel):
    id: str
    label: str
    rtsp_path: str
    mse_url: str | None = None
    stream_connected: bool | None = None
    recording_active: bool = False
    recording_started_at: datetime | None = None
    rolling_active: bool = False
    rolling_started_at: datetime | None = None
    rolling_segment_count: int = 0


class SnapshotRequest(BaseModel):
    """Take a still photo from one lens of a camera."""

    lens: str | None = Field(
        default=None,
        description="Lens id to capture. Omit to use the camera's first lens.",
    )


class SnapshotResponse(BaseModel):
    path: str
    url: str
    taken_at: datetime
    lens: str
    width: int | None = None
    height: int | None = None
    bytes: int | None = None


class RecordingStartRequest(BaseModel):
    """Start a server-side MP4 recording from one lens."""

    lens: str | None = Field(default=None, description="Lens id; defaults to the first lens.")
    max_duration_s: int | None = Field(
        default=3600,
        ge=1,
        le=86_400,
        description="Hard cap so a forgotten recording cannot fill the disk.",
    )


class RecordingStartResponse(BaseModel):
    recording_id: str
    path: str
    url: str
    lens: str
    started_at: datetime
    max_duration_s: int | None = None


class RecordingStopRequest(BaseModel):
    """Stop a recording and finalise the MP4."""

    recording_id: str | None = Field(
        default=None,
        description="Specific recording to stop; defaults to the camera's only active one.",
    )


class RecordingStopResponse(BaseModel):
    recording_id: str
    path: str
    url: str
    started_at: datetime
    stopped_at: datetime
    duration_ms: int
    bytes: int | None = None
    finalized: bool = True


class RecordingCancelRequest(BaseModel):
    recording_id: str | None = None


class RecordingCancelResponse(BaseModel):
    recording_id: str
    canceled: bool = True
    deleted_path: str | None = None


class RollingStartRequest(BaseModel):
    """Start a rolling background recording on one lens."""

    lens: str | None = Field(default=None, description="Lens id; defaults to the camera's first lens.")
    segment_duration_s: int = Field(
        default=1800,
        ge=60,
        le=7200,
        description="Length of each rolling segment in seconds (default 30 min).",
    )
    max_segments: int = Field(
        default=96,
        ge=1,
        le=1000,
        description="Maximum number of completed segments to keep on disk; oldest deleted first.",
    )
    include_audio: bool = Field(
        default=False,
        description="Transcode PCMA microphone audio to AAC and include it in the MP4.",
    )


class RollingStopResponse(BaseModel):
    ok: bool = True
    message: str | None = None
    segments_recorded: int = 0


class CameraDetails(BaseModel):
    """Shape of ``EquipmentStatus.details`` for ``kind: camera``.

    Documented as a Pydantic model so the gateway and the dashboard can both
    rely on it; the model is serialised into the free-form ``details`` dict
    on the way out so the spec envelope stays uniform.
    """

    lenses: list[LensEntry] = Field(default_factory=list)
    presets: list[PresetEntry] = Field(default_factory=list)
    privacy_mode: bool = False
    streaming_enabled: bool = True
    onvif_reachable: bool = False
    tapo_reachable: bool = False
    go2rtc_reachable: bool = False


# ---------------------------------------------------------------------
# Plug-specific request bodies.
# ---------------------------------------------------------------------


class PlugSwitchRequest(BaseModel):
    """Turn an outlet (or the whole strip) on/off/toggle.

    ``outlet`` is the zero-indexed outlet for HS300 (and any other multi-outlet
    Kasa device). Omit it for HS103 single-plug devices, or to act on the
    whole strip at once on multi-outlet devices.
    """

    outlet: int | None = Field(default=None, ge=0, le=31)


class ControlAck(BaseModel):
    """Generic body returned by every POST/DELETE control endpoint.

    Lets the dashboard fire-and-forget without depending on per-action
    response shapes. The ``state`` is a snapshot taken immediately after
    the action - clients should still re-poll ``/status`` for ground truth.
    """

    ok: bool = True
    message: str | None = None
    state: dict[str, Any] | None = None


__all__ = [
    "PROTOCOL_VERSION",
    "CameraDetails",
    "ComponentStatus",
    "ControlAck",
    "EquipmentKind",
    "EquipmentState",
    "EquipmentStatus",
    "ErrorInfo",
    "ErrorSeverity",
    "HealthResponse",
    "LensEntry",
    "MetricValue",
    "PlugSwitchRequest",
    "PresetEntry",
    "PresetGotoRequest",
    "PresetSaveRequest",
    "PrivacyRequest",
    "ProbeResponse",
    "PtzContinuousRequest",
    "PtzDirection",
    "PtzNudgeRequest",
    "RecordingCancelRequest",
    "RecordingCancelResponse",
    "RecordingStartRequest",
    "RecordingStartResponse",
    "RecordingStopRequest",
    "RecordingStopResponse",
    "RollingStartRequest",
    "RollingStopResponse",
    "SnapshotRequest",
    "SnapshotResponse",
    "StreamingRequest",
]
