"""Per-kind FastAPI routers + the shared device registry."""

from .cameras import build_camera_router
from .plugs import build_plug_router
from .registry import DeviceRegistry, get_registry

__all__ = [
    "DeviceRegistry",
    "build_camera_router",
    "build_plug_router",
    "get_registry",
]
