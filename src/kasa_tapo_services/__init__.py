"""kasa-tapo-services: STATUS_SPEC v1.0 gateway for Kasa plugs and Tapo cameras.

The package owns:

* The vendored copy of the lab equipment status envelope (``models``).
* A small ``devices.yaml`` + ``.env`` config loader (``config``).
* Per-device clients - one per backend protocol:
    - ``kasa.plug_client`` for HS103/HS300 (python-kasa)
    - ``tapo.tapo_client`` for Tapo proprietary API (privacy / day-night)
    - ``tapo.onvif_client`` for ONVIF PTZ + presets
    - ``tapo.go2rtc_client`` for go2rtc source health
* The FastAPI app (``main``) which mounts one router per device kind.

The dashboard (``ac-organic-lab``) registers each device as a normal
equipment entry with ``adapter: http``. Devices are reachable to the dashboard
only via this gateway - they never touch the tailnet directly.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
