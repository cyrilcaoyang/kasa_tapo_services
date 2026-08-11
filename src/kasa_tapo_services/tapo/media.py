"""Server-side media capture (snapshots + recordings) via ffmpeg.

The gateway already has the camera's RTSP credentials in process memory
(loaded from ``.env``), so it's the natural place to spawn ffmpeg
subprocesses that pull from RTSP and write to the configured media
directories. We deliberately do NOT route this through go2rtc:

* go2rtc's ``api/frame.jpeg`` endpoint requires the source to already be
  publishing fMP4 fragments, which only happens after a consumer
  connects - making it racy for one-shot snapshots.
* go2rtc's recording feature would couple our recording lifecycle to
  go2rtc's process lifecycle, which we don't want (a go2rtc restart
  would silently drop in-progress recordings).

A direct ffmpeg subprocess is simpler and isolates failures.

File layout (per :class:`kasa_tapo_services.config.MediaConfig`):

    <snapshots_dir>/<lens_id>/<ISO ts>.jpg
    <recordings_dir>/<lens_id>/<ISO ts>.mp4   (rename of .mp4.partial on stop)

Timestamps use ``YYYY-MM-DDTHH-MM-SSZ`` format - colons are
filesystem-unfriendly on most OSes, so we substitute ``-``. Camera
identity is encoded in the directory path, not the filename, so
sibling files on disk are easy to sort by name = sort by time.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from kasa_tapo_services.config import DeviceConfig, DeviceCredentials

logger = logging.getLogger(__name__)


# Default media root used when neither devices.yaml nor the env override
# provides a path. We expand against the gateway process's HOME.
_DEFAULT_MEDIA_ROOT = Path("~/kasa-tapo-media").expanduser()


def _media_root() -> Path:
    """Resolve the default media root (``$KASA_TAPO_MEDIA_ROOT`` or fallback)."""

    env = os.environ.get("KASA_TAPO_MEDIA_ROOT")
    return Path(env).expanduser() if env else _DEFAULT_MEDIA_ROOT


def _expand(path: str | os.PathLike[str]) -> Path:
    """Expand ``~`` and resolve to an absolute path without requiring it to exist."""

    return Path(os.path.expanduser(str(path))).resolve()


def _camera_snapshots_dir(camera: DeviceConfig) -> Path:
    if camera.media and camera.media.snapshots_dir:
        return _expand(camera.media.snapshots_dir)
    return _media_root() / "snapshots" / camera.id


def _camera_recordings_dir(camera: DeviceConfig) -> Path:
    if camera.media and camera.media.recordings_dir:
        return _expand(camera.media.recordings_dir)
    return _media_root() / "recordings" / camera.id


def _camera_rolling_dir(camera: DeviceConfig) -> Path:
    """Dedicated directory for rolling-recorder segments (separate from manual recordings)."""
    return _media_root() / "rolling" / camera.id


def _ts_filename(suffix: str, *, when: datetime | None = None) -> str:
    """Build a filesystem-safe ISO 8601 UTC timestamp filename."""

    when = when or datetime.now(tz=timezone.utc)
    # 2026-05-08T21:43:00Z -> 2026-05-08T21-43-00Z (drop microseconds; safe on win/mac/linux).
    iso = when.strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"{iso}{suffix}"


def _rtsp_url(camera: DeviceConfig, lens_path: str, creds: DeviceCredentials) -> str:
    """Build an authenticated RTSP URL for ``ffmpeg`` to pull from.

    Unlike :func:`bootstrap_go2rtc._rtsp_url` (which leaves
    ``${VAR}`` placeholders for go2rtc to substitute), this returns
    the resolved URL with the actual credentials inlined. The URL is
    NEVER logged - log calls always use the redacted form via
    :func:`_redact_url`.

    Credentials are percent-encoded, same as in
    :func:`bootstrap_go2rtc._rtsp_url`: Tapo accounts are email
    addresses (``@``) and generated passwords routinely contain ``#``,
    ``%``, ``:`` and ``/``. Inlined raw, ffmpeg parses userinfo up to
    the *first* ``@`` and truncates at ``#``, yielding the misleading
    ``Port missing in uri``.
    """

    if not creds.has_basic:
        raise RuntimeError(
            f"Camera {camera.id!r} has no credentials in env; "
            f"set {camera.id.upper()}_USER and {camera.id.upper()}_PASS"
        )
    user = quote(creds.user or "", safe="")
    password = quote(creds.password or "", safe="")
    return f"rtsp://{user}:{password}@{camera.host}:{camera.rtsp_port}/{lens_path}"


def _redact_url(url: str) -> str:
    """Replace user:pass in an RTSP URL with ``***`` for safe logging."""

    if "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" not in rest:
        return url
    _, host_path = rest.rsplit("@", 1)
    return f"{scheme}://***:***@{host_path}"


def _resolve_lens(camera: DeviceConfig, lens_id: str | None) -> tuple[str, str, str]:
    """Pick a lens, returning ``(lens_id, lens_label, rtsp_path)``.

    Raises ``ValueError`` if the camera has no lenses or the requested
    id doesn't match one. With ``lens_id=None`` we fall back to the
    camera's first declared lens.
    """

    if not camera.lenses:
        raise ValueError(f"Camera {camera.id!r} has no lenses configured")
    if lens_id is None:
        first = camera.lenses[0]
        return first.id, first.label, first.rtsp_path
    for lens in camera.lenses:
        if lens.id == lens_id:
            return lens.id, lens.label, lens.rtsp_path
    raise ValueError(
        f"Camera {camera.id!r} has no lens id={lens_id!r}; "
        f"available: {[lens.id for lens in camera.lenses]}"
    )


@dataclass
class RecordingHandle:
    """One in-flight ffmpeg recording.

    Lives in :class:`kasa_tapo_services.routes.registry.CameraClients` so
    that POSTs to ``/recording/stop`` and ``/recording/cancel`` can find
    the running subprocess on a subsequent request. The ``recording_id``
    is a short uuid prefix - long enough to be unique, short enough to
    fit in URLs.
    """

    recording_id: str
    camera_id: str
    lens_id: str
    started_at: datetime
    target_path: Path  # final destination after rename (.mp4)
    partial_path: Path  # what ffmpeg writes to (.mp4.partial)
    process: asyncio.subprocess.Process
    max_duration_s: int | None = None
    watchdog: asyncio.Task | None = field(default=None, repr=False)

    @property
    def is_running(self) -> bool:
        return self.process.returncode is None


class CameraMediaManager:
    """One per gateway-instance: knows how to capture stills and clips.

    Single-camera helpers take a :class:`DeviceConfig` rather than
    holding a reference, so the manager is stateless except for the
    in-flight recording table. That's stored *outside* the manager (on
    the per-camera :class:`CameraClients` bundle) so a manager can be
    swapped out without losing the bookkeeping.
    """

    FFMPEG_BINARY = "ffmpeg"  # resolved against $PATH at call time

    def __init__(self, ffmpeg_binary: str | None = None) -> None:
        # Allows tests to inject a fake binary; defaults to the system
        # ``ffmpeg`` discovered via ``which``.
        self._ffmpeg = ffmpeg_binary or shutil.which(self.FFMPEG_BINARY) or self.FFMPEG_BINARY

    @property
    def ffmpeg(self) -> str:
        return self._ffmpeg

    # -----------------------------------------------------------------
    # Snapshot.
    # -----------------------------------------------------------------

    async def take_snapshot(
        self,
        *,
        camera: DeviceConfig,
        creds: DeviceCredentials,
        lens_id: str | None = None,
        timeout_s: float = 8.0,
    ) -> tuple[Path, str, str]:
        """Capture one JPEG frame from ``lens_id`` (or the first lens).

        Returns ``(path, lens_id, lens_label)``. Raises ``RuntimeError``
        if ffmpeg fails or doesn't produce a non-empty file within
        ``timeout_s`` seconds.
        """

        lens, label, rtsp_path = _resolve_lens(camera, lens_id)
        snapshots_dir = _camera_snapshots_dir(camera) / lens
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        target = snapshots_dir / _ts_filename(".jpg")

        url = _rtsp_url(camera, rtsp_path, creds)
        argv = [
            self._ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-y",
            "-i", url,
            "-frames:v", "1",
            "-q:v", "2",
            str(target),
        ]

        logger.info(
            "snapshot start camera=%s lens=%s url=%s -> %s",
            camera.id, lens, _redact_url(url), target,
        )

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            target.unlink(missing_ok=True)
            raise RuntimeError(
                f"ffmpeg timed out after {timeout_s}s while capturing {camera.id}/{lens}"
            ) from None

        if proc.returncode != 0 or not target.exists() or target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            raise RuntimeError(
                f"ffmpeg failed for {camera.id}/{lens} (rc={proc.returncode}): "
                f"{stderr.decode(errors='replace').strip()[:400]}"
            )

        logger.info(
            "snapshot ok camera=%s lens=%s bytes=%d -> %s",
            camera.id, lens, target.stat().st_size, target,
        )
        return target, lens, label

    # -----------------------------------------------------------------
    # Recording.
    # -----------------------------------------------------------------

    async def start_recording(
        self,
        *,
        camera: DeviceConfig,
        creds: DeviceCredentials,
        lens_id: str | None = None,
        max_duration_s: int | None = 3600,
        include_audio: bool = False,
        rolling: bool = False,
    ) -> RecordingHandle:
        """Start a long-running stream-copy ffmpeg subprocess.

        ffmpeg pulls H.264 from RTSP and remuxes it into an MP4 with
        ``faststart`` (moves the moov atom to the head on close so
        partial files are still playable). Stop the process with
        :meth:`stop_recording` for a clean finalize, or
        :meth:`cancel_recording` to abandon the partial.
        """

        lens, _label, rtsp_path = _resolve_lens(camera, lens_id)
        base_dir = _camera_rolling_dir(camera) if rolling else _camera_recordings_dir(camera)
        recordings_dir = base_dir / lens
        recordings_dir.mkdir(parents=True, exist_ok=True)

        started_at = datetime.now(tz=timezone.utc)
        recording_id = uuid.uuid4().hex[:12]
        # Keep the recording-id appended to the filename so two
        # back-to-back recordings within the same second don't collide.
        partial = recordings_dir / _ts_filename(f"_{recording_id}.mp4.partial", when=started_at)
        final = partial.with_suffix("")  # strip .partial -> ...mp4
        # ``with_suffix("")`` on ".mp4.partial" only strips the trailing
        # ".partial", leaving ".mp4". That's exactly what we want.

        url = _rtsp_url(camera, rtsp_path, creds)
        audio_args: list[str]
        if include_audio:
            # Tapo cameras stream PCMA (G.711 a-law) which MP4 won't mux natively;
            # transcode to AAC at 64 kbps (speech-quality, minimal overhead).
            audio_args = ["-c:a", "aac", "-b:a", "64k"]
        else:
            audio_args = ["-an"]
        argv = [
            self._ffmpeg,
            "-hide_banner",
            "-loglevel", "warning",
            "-rtsp_transport", "tcp",
            "-i", url,
            "-c:v", "copy",
            *audio_args,
            "-movflags", "+faststart",
            "-f", "mp4",
            str(partial),
        ]

        logger.info(
            "recording start camera=%s lens=%s id=%s url=%s -> %s",
            camera.id, lens, recording_id, _redact_url(url), partial,
        )

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        handle = RecordingHandle(
            recording_id=recording_id,
            camera_id=camera.id,
            lens_id=lens,
            started_at=started_at,
            target_path=final,
            partial_path=partial,
            process=proc,
            max_duration_s=max_duration_s,
        )
        if max_duration_s:
            handle.watchdog = asyncio.create_task(
                self._watchdog(handle, max_duration_s),
                name=f"recording-watchdog-{recording_id}",
            )
        return handle

    async def _watchdog(self, handle: RecordingHandle, max_duration_s: int) -> None:
        """Stop a recording after ``max_duration_s`` if it's still running."""

        try:
            await asyncio.sleep(max_duration_s)
        except asyncio.CancelledError:
            return
        if handle.is_running:
            logger.warning(
                "recording watchdog firing camera=%s id=%s after %ds",
                handle.camera_id, handle.recording_id, max_duration_s,
            )
            try:
                await self.stop_recording(handle)
            except Exception:
                logger.exception("watchdog stop failed for %s", handle.recording_id)

    async def stop_recording(
        self,
        handle: RecordingHandle,
        *,
        timeout_s: float = 10.0,
    ) -> Path:
        """Send SIGINT to ffmpeg, wait for clean exit, then rename .partial -> .mp4.

        Returns the path to the final mp4. Raises ``RuntimeError`` if
        ffmpeg fails to exit cleanly within ``timeout_s`` seconds.
        """

        if handle.watchdog and not handle.watchdog.done():
            handle.watchdog.cancel()

        if not handle.is_running:
            return self._finalize_partial(handle)

        try:
            handle.process.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass

        try:
            await asyncio.wait_for(handle.process.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            logger.warning(
                "recording stop timeout id=%s; sending SIGKILL",
                handle.recording_id,
            )
            handle.process.kill()
            await handle.process.wait()
            # SIGKILL'd files often have no moov atom, so the rename may
            # land us with an unplayable .mp4. We still rename so the
            # operator can see *something* on disk; ffmpeg can usually
            # repair these with `ffmpeg -i broken.mp4 -c copy fixed.mp4`.

        return self._finalize_partial(handle)

    def _finalize_partial(self, handle: RecordingHandle) -> Path:
        if handle.partial_path.exists():
            try:
                # Atomic on POSIX; on Windows atomic only if target doesn't exist.
                handle.partial_path.rename(handle.target_path)
                logger.info(
                    "recording finalized id=%s -> %s (%d bytes)",
                    handle.recording_id,
                    handle.target_path,
                    handle.target_path.stat().st_size if handle.target_path.exists() else 0,
                )
            except OSError:
                logger.exception(
                    "rename %s -> %s failed", handle.partial_path, handle.target_path,
                )
                # Best effort: leave the .partial in place so nothing's lost.
                return handle.partial_path
        return handle.target_path

    async def cancel_recording(
        self,
        handle: RecordingHandle,
        *,
        timeout_s: float = 5.0,
    ) -> Path | None:
        """Kill ffmpeg and delete the partial file. Returns the deleted path or None."""

        if handle.watchdog and not handle.watchdog.done():
            handle.watchdog.cancel()

        if handle.is_running:
            try:
                handle.process.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(handle.process.wait(), timeout=timeout_s)
            except asyncio.TimeoutError:
                logger.warning(
                    "recording cancel: ffmpeg id=%s did not exit after kill",
                    handle.recording_id,
                )

        deleted: Path | None = None
        if handle.partial_path.exists():
            try:
                handle.partial_path.unlink()
                deleted = handle.partial_path
                logger.info(
                    "recording canceled id=%s; deleted partial %s",
                    handle.recording_id, handle.partial_path,
                )
            except OSError:
                logger.exception("failed to delete partial %s", handle.partial_path)
        return deleted


def list_camera_media(camera: DeviceConfig) -> dict[str, list[dict]]:
    """Enumerate snapshots and recordings on disk for the minimal media list page.

    Walks the configured snapshots/recordings directories (per-lens
    subfolders) and returns a flat-ish dict keyed by ``"snapshots"`` /
    ``"recordings"``. Each entry is a small ``dict`` with name, size,
    mtime and a relative ``url`` path the gateway serves. Returns
    empty lists if the directories don't exist yet.
    """

    base_snap = _camera_snapshots_dir(camera)
    base_rec = _camera_recordings_dir(camera)

    def collect(root: Path, kind: str) -> list[dict]:
        out: list[dict] = []
        if not root.exists():
            return out
        for lens_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            lens = lens_dir.name
            for entry in sorted(lens_dir.iterdir(), reverse=True):
                if entry.is_dir():
                    continue
                if entry.suffix == ".partial":
                    continue
                stat = entry.stat()
                out.append(
                    {
                        "name": entry.name,
                        "lens": lens,
                        "kind": kind,
                        "bytes": stat.st_size,
                        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                        "url": f"/cameras/{camera.id}/media/{kind}/{lens}/{entry.name}",
                        "abs_path": str(entry),
                    }
                )
        return out

    return {
        "snapshots": collect(base_snap, "snapshots"),
        "recordings": collect(base_rec, "recordings"),
    }


def prune_rolling_recordings(camera: DeviceConfig, lens_id: str, max_segments: int) -> int:
    """Delete the oldest rolling segments for ``lens_id`` until at most ``max_segments`` remain.

    Returns the number of files deleted.  Safe to call even if the directory
    doesn't exist yet (returns 0).
    """

    rolling_dir = _camera_rolling_dir(camera) / lens_id
    if not rolling_dir.exists():
        return 0

    files = sorted(
        [f for f in rolling_dir.iterdir() if f.is_file() and f.suffix == ".mp4"],
        key=lambda p: p.stat().st_mtime,
    )
    deleted = 0
    while len(files) > max_segments:
        oldest = files.pop(0)
        try:
            oldest.unlink()
            logger.info("rolling prune: deleted %s", oldest)
            deleted += 1
        except OSError:
            logger.exception("rolling prune: failed to delete %s", oldest)
    return deleted


def count_rolling_recordings(camera: DeviceConfig, lens_id: str) -> int:
    """Return the number of completed rolling segments on disk for ``lens_id``."""

    rolling_dir = _camera_rolling_dir(camera) / lens_id
    if not rolling_dir.exists():
        return 0
    return sum(1 for f in rolling_dir.iterdir() if f.is_file() and f.suffix == ".mp4")


def resolve_media_path(
    camera: DeviceConfig,
    *,
    kind: str,
    lens: str,
    name: str,
) -> Path:
    """Translate a ``/media/<kind>/<lens>/<name>`` URL into an on-disk path.

    Validates that the resolved path is contained inside the camera's
    configured snapshots/recordings directory - prevents path traversal
    via ``..`` segments in the URL.
    """

    if kind == "snapshots":
        base = _camera_snapshots_dir(camera)
    elif kind == "recordings":
        base = _camera_recordings_dir(camera)
    else:
        raise ValueError(f"unknown media kind {kind!r}")

    target = (base / lens / name).resolve()
    base_resolved = base.resolve()
    if base_resolved not in target.parents:
        raise ValueError("path traversal detected")
    if target.suffix == ".partial":
        # Don't expose in-progress recordings; clients can poll status
        # for ``recording_active`` instead.
        raise ValueError("partial files are not exposed")
    return target


__all__ = [
    "CameraMediaManager",
    "count_rolling_recordings",
    "prune_rolling_recordings",
    "RecordingHandle",
    "list_camera_media",
    "resolve_media_path",
]
