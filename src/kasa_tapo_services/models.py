"""STATUS_SPEC contract types (imported) + this gateway's control bodies.

The contract half of this module used to be a hand-copied mirror of
``ac-organic-lab/skills/src/lab_skills/models.py``. It is now imported
from **``sdl-lab-contract``**, the shared package that is the single
source for these types (ac-organic-lab ARCHITECTURE.md LG5). The
re-exports below keep ``from kasa_tapo_services.models import ...``
working for the rest of this package, so the swap is invisible to
``routes/``, ``poller.py`` and ``main.py``.

The three kinds this gateway emits (``camera``, ``smart_plug``,
``power_strip``) are part of the shared ``EquipmentKind`` enum, so there
is nothing left to extend locally.

**This gateway stays on ``protocol_version: "1.0"``**, and importing
v1.2 types does not change that. It exposes ``/control/*`` but implements
no claim protocol, and STATUS_SPEC §9 is explicit that partial control
without claims stays v1.0 — the read-only exemption that lets a
monitoring-only device declare a higher version does not apply to a
device that *has* actions to serialize. ``PROTOCOL_VERSION`` below is the
contract's v1.0 default, which is exactly what we want to report.

The second half of this file is the gateway-only request/response
bodies: PTZ moves, preset save/goto, privacy/streaming toggles, snapshot
and recording control, plug on/off.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sdl_lab_contract import (
    PROTOCOL_VERSION,
    SPEC_VERSION,
    Activity,
    ComponentStatus,
    EquipmentKind,
    EquipmentState,
    EquipmentStatus,
    ErrorInfo,
    ErrorSeverity,
    HealthResponse,
    MetricValue,
    ProbeResponse,
)


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
    # Re-exported from sdl-lab-contract so importers of this module do not
    # need to know whether a type is contract-owned or gateway-owned.
    "PROTOCOL_VERSION",
    "SPEC_VERSION",
    "Activity",
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
