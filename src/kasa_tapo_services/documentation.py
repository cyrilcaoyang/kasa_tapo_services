"""Agent documentation served from resources shipped inside the package.

Same shape as the other lab device services (sense-every-zone,
torry-pines-shaker-server): a Markdown agent guide, a Markdown API
reference, and a plain-text ``/llms.txt`` index, so an agent — or the
dashboard's API reference page — can discover how to drive this gateway
without reading the repo.

These routes are gateway-level, not per-device: they describe the whole
camera + plug surface, and they answer without touching any hardware.
"""

from __future__ import annotations

from importlib.resources import files

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter(tags=["documentation"])


class MarkdownResponse(PlainTextResponse):
    media_type = "text/markdown"


def _document(name: str) -> str:
    return files("kasa_tapo_services").joinpath("docs", name).read_text(encoding="utf-8")


@router.get("/agent-docs", response_class=MarkdownResponse, summary="Agent guide (Markdown)")
async def agent_docs() -> str:
    return _document("AGENT_GUIDE.md")


@router.get(
    "/agent-docs/api-reference",
    response_class=MarkdownResponse,
    summary="API reference (Markdown)",
)
async def api_reference() -> str:
    return _document("API_REFERENCE.md")


@router.get("/llms.txt", response_class=PlainTextResponse, summary="Discovery index for agents")
async def llms_txt() -> str:
    # Document-relative links so the index works at the gateway root and
    # under any reverse-proxy or mount prefix.
    return (
        "# Camera & Plug Gateway (STATUS_SPEC v1.0 gateway for Tapo cameras and Kasa plugs)\n\n"
        "## Documentation\n\n"
        "- [Agent guide](agent-docs): per-device addressing, health vs activity, "
        "per-model actions, privacy/streaming, capture, plug power control.\n"
        "- [API reference](agent-docs/api-reference): every route with bodies and refusal codes.\n"
        "- [OpenAPI](openapi.json): request/response schemas.\n\n"
        "## Live status\n\n"
        "Enumerate devices with `GET /devices`, then read each device's own envelope at "
        "`/cameras/{camera_id}/status` or `/plugs/{plug_id}/status`; the gateway's own "
        "`GET /status` describes this process only. Read allowed_actions before acting.\n"
    )
