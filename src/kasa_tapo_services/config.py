"""Gateway configuration: ``devices.yaml`` + ``.env`` loader.

``devices.yaml`` declares every device this gateway hosts. Real credentials
live in ``.env`` (gitignored), keyed by device id; the indirection lets the
device file be committed to the dashboard repo without leaking secrets.

Path resolution order:

1. ``KASA_TAPO_DEVICES_PATH`` env var.
2. ``./devices.yaml`` next to the current working directory.
3. ``./devices.yaml.example`` as a last-resort fallback (so the gateway
   boots in a recognisable empty state during development - it logs a
   warning when this happens).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError, model_validator

logger = logging.getLogger(__name__)

DeviceKind = Literal["camera", "smart_plug", "power_strip"]


class LensConfig(BaseModel):
    id: str
    label: str
    rtsp_path: str


class MediaConfig(BaseModel):
    """Where snapshots / recordings for a single camera land on disk.

    Both fields accept a leading ``~`` (expanded against the gateway
    process's home directory) and are auto-created on first use. If
    omitted the gateway uses defaults under ``KASA_TAPO_MEDIA_ROOT``
    (env, default ``~/kasa-tapo-media``):

    * snapshots → ``<root>/snapshots/<camera_id>/``
    * recordings → ``<root>/recordings/<camera_id>/``

    A per-camera override here lets the operator point recordings at an
    external drive / NAS mount independently of where snapshots live.
    """

    snapshots_dir: str | None = None
    recordings_dir: str | None = None


class OutletConfig(BaseModel):
    """One physical outlet on a multi-outlet Kasa device (e.g. HS300)."""

    index: int = Field(ge=0, le=31)
    label: str | None = None


class DeviceConfig(BaseModel):
    """One device entry in ``devices.yaml``.

    All of the optional fields are kind-specific - the gateway validates
    the relevant ones in :meth:`_check_kind_fields` so a typo in the YAML
    fails the gateway at startup rather than at first poll.
    """

    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    kind: DeviceKind
    host: str
    enabled: bool = True

    # Camera-specific
    onvif_port: int = 2020
    rtsp_port: int = 554
    lenses: list[LensConfig] | None = None
    max_presets: int | None = Field(default=None, ge=1, le=64)
    media: MediaConfig | None = None

    # Power-strip-specific
    outlets: list[OutletConfig] | None = None

    @model_validator(mode="after")
    def _check_kind_fields(self) -> DeviceConfig:
        if self.kind == "camera":
            if not self.lenses:
                raise ValueError(f"Device {self.id!r}: kind=camera requires at least one lens")
            ids = [lens.id for lens in self.lenses]
            if len(ids) != len(set(ids)):
                raise ValueError(f"Device {self.id!r}: lens ids must be unique")
        elif self.kind == "power_strip":
            if not self.outlets:
                raise ValueError(f"Device {self.id!r}: kind=power_strip requires an outlets list")
            indices = [outlet.index for outlet in self.outlets]
            if len(indices) != len(set(indices)):
                raise ValueError(f"Device {self.id!r}: outlet indices must be unique")
        return self


class GatewayConfig(BaseModel):
    devices: list[DeviceConfig] = Field(default_factory=list)

    def by_id(self, device_id: str) -> DeviceConfig | None:
        for d in self.devices:
            if d.id == device_id:
                return d
        return None

    def cameras(self) -> list[DeviceConfig]:
        return [d for d in self.devices if d.kind == "camera"]

    def plugs(self) -> list[DeviceConfig]:
        return [d for d in self.devices if d.kind in ("smart_plug", "power_strip")]


def _resolve_devices_path(explicit: str | os.PathLike | None) -> Path:
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("KASA_TAPO_DEVICES_PATH")
    if env:
        return Path(env)
    cwd_yaml = Path.cwd() / "devices.yaml"
    if cwd_yaml.exists():
        return cwd_yaml
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / "devices.yaml"
        if candidate.exists():
            return candidate
        candidate = ancestor / "devices.yaml.example"
        if candidate.exists():
            logger.warning(
                "kasa-tapo-services: devices.yaml not found, falling back to "
                "devices.yaml.example at %s. Copy it and edit before running "
                "in anger.",
                candidate,
            )
            return candidate
    raise FileNotFoundError(
        "Could not locate devices.yaml. Set KASA_TAPO_DEVICES_PATH or place "
        "devices.yaml in the working directory."
    )


def load_config(path: str | os.PathLike | None = None) -> GatewayConfig:
    """Read ``devices.yaml`` and validate it.

    Also calls :func:`load_dotenv` so :func:`device_credentials` can read
    per-device ``USER`` / ``PASS`` from the process environment.
    """

    resolved = _resolve_devices_path(path)
    if not resolved.exists():
        raise FileNotFoundError(f"devices.yaml not found at {resolved}")

    with resolved.open("r") as f:
        data = yaml.safe_load(f) or {}

    try:
        cfg = GatewayConfig.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"Invalid devices.yaml at {resolved}: {exc}") from exc

    # Honour a .env file next to devices.yaml. We don't override real env
    # vars set by systemd's `EnvironmentFile=` directive.
    dotenv_path = resolved.parent / ".env"
    if dotenv_path.exists():
        load_dotenv(dotenv_path, override=False)
    else:
        load_dotenv(override=False)
    return cfg


class DeviceCredentials(BaseModel):
    """Credentials resolved out of the environment for one device.

    For Tapo cameras both ``user``/``pass`` (Camera Account, used for RTSP
    + pytapo calls) and ``onvif_user``/``onvif_pass`` (ONVIF Account, used
    for PTZ) may be set. If the ONVIF pair is missing the gateway falls
    back to the Camera Account.
    """

    user: str | None = None
    password: str | None = None
    onvif_user: str | None = None
    onvif_password: str | None = None

    @property
    def has_basic(self) -> bool:
        return bool(self.user and self.password)

    @property
    def effective_onvif_user(self) -> str | None:
        return self.onvif_user or self.user

    @property
    def effective_onvif_password(self) -> str | None:
        return self.onvif_password or self.password


def device_credentials(device_id: str) -> DeviceCredentials:
    """Return the credentials env-vars for a device id.

    Variable name pattern (uppercased device id):

    * ``<ID>_USER`` / ``<ID>_PASS`` - Tapo Camera Account, RTSP, Kasa cloud
    * ``<ID>_ONVIF_USER`` / ``<ID>_ONVIF_PASS`` - separate ONVIF account
      (optional - falls back to the basic pair).
    """

    key = device_id.upper()
    return DeviceCredentials(
        user=os.environ.get(f"{key}_USER"),
        password=os.environ.get(f"{key}_PASS"),
        onvif_user=os.environ.get(f"{key}_ONVIF_USER"),
        onvif_password=os.environ.get(f"{key}_ONVIF_PASS"),
    )


__all__ = [
    "DeviceConfig",
    "DeviceCredentials",
    "DeviceKind",
    "GatewayConfig",
    "LensConfig",
    "MediaConfig",
    "OutletConfig",
    "device_credentials",
    "load_config",
]
