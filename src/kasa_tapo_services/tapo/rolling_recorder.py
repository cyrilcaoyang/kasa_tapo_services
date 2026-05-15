"""Continuous rolling recorder for a single camera lens.

Maintains a background asyncio loop that:

1. Starts an ffmpeg recording segment of ``segment_duration_s`` seconds.
2. Sleeps for ``segment_duration_s`` (the full segment length).
3. Sends SIGINT to ffmpeg, waits for a clean exit, and renames the
   ``.mp4.partial`` file to ``.mp4``.
4. Prunes the oldest completed segments so at most ``max_segments`` remain.
5. Immediately starts the next segment.

The recorder writes into the dedicated rolling directory
(``$KASA_TAPO_MEDIA_ROOT/rolling/<camera_id>/<lens_id>/``) and is kept
completely separate from the manual-recording table on ``CameraClients``
so that starting a manual recording on the same camera does not conflict.

Usage::

    recorder = RollingRecorder(
        camera=bundle.config,
        creds=creds,
        media=registry.media,
        lens_id="wide",
        segment_duration_s=1800,
        max_segments=96,
        include_audio=False,
    )
    recorder.start()   # spawns the loop task; returns immediately
    ...
    await recorder.stop()  # graceful: finalises the current segment
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from kasa_tapo_services.config import DeviceConfig, DeviceCredentials
from kasa_tapo_services.tapo.media import (
    CameraMediaManager,
    RecordingHandle,
    count_rolling_recordings,
    prune_rolling_recordings,
    _resolve_lens,  # internal helper – same module, same package
)

logger = logging.getLogger(__name__)


class RollingRecorder:
    """Runs a continuous segmented recording loop for one camera lens.

    ``start()`` is non-blocking: it spawns an asyncio Task and returns
    immediately. ``stop()`` is a coroutine that cancels the task and
    finalises the in-flight segment before returning.
    """

    def __init__(
        self,
        *,
        camera: DeviceConfig,
        creds: DeviceCredentials,
        media: CameraMediaManager,
        lens_id: str | None = None,
        segment_duration_s: int = 1800,
        max_segments: int = 96,
        include_audio: bool = False,
    ) -> None:
        self._camera = camera
        self._creds = creds
        self._media = media
        self._lens_id = lens_id
        self._segment_duration_s = segment_duration_s
        self._max_segments = max_segments
        self._include_audio = include_audio

        self._task: asyncio.Task[None] | None = None
        self._current_handle: RecordingHandle | None = None
        self._started_at: datetime | None = None
        self._segments_recorded: int = 0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def started_at(self) -> datetime | None:
        return self._started_at

    @property
    def segments_recorded(self) -> int:
        return self._segments_recorded

    @property
    def current_handle(self) -> RecordingHandle | None:
        return self._current_handle

    @property
    def lens_id(self) -> str | None:
        return self._lens_id

    def start(self) -> None:
        """Spawn the recording loop as a background asyncio Task."""
        if self.is_running:
            logger.warning(
                "rolling: start() called on already-running recorder for %s", self._camera.id
            )
            return
        self._started_at = datetime.now(tz=timezone.utc)
        self._segments_recorded = 0
        self._task = asyncio.create_task(
            self._loop(),
            name=f"rolling-{self._camera.id}-{self._lens_id or 'default'}",
        )
        logger.info(
            "rolling: started camera=%s lens=%s segment_s=%d max_segments=%d audio=%s",
            self._camera.id,
            self._lens_id or "(first)",
            self._segment_duration_s,
            self._max_segments,
            self._include_audio,
        )

    async def stop(self) -> None:
        """Stop the loop and finalise the current segment.

        Safe to call when not running (no-op).
        """
        if not self.is_running and self._current_handle is None:
            return

        # Snapshot the current handle before we cancel the task (the task
        # sets self._current_handle = None before each stop_recording call,
        # so we might miss it if we read it after cancellation).
        handle = self._current_handle

        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        # Finalise whatever segment was in-flight when we cancelled.
        # The task may or may not have managed to stop ffmpeg itself, so
        # we check is_running and only act if the process is still alive.
        if handle is None:
            handle = self._current_handle  # re-read in case task updated it
        self._current_handle = None

        if handle is not None and handle.is_running:
            logger.info("rolling: stop() finalising in-flight segment %s", handle.recording_id)
            try:
                await self._media.stop_recording(handle)
            except Exception:
                logger.exception(
                    "rolling: stop_recording failed during stop() for %s", handle.recording_id
                )

        self._task = None
        self._started_at = None
        logger.info("rolling: stopped camera=%s lens=%s", self._camera.id, self._lens_id or "(first)")

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        """Perpetual segment loop. Exits only on CancelledError."""
        while True:
            handle: RecordingHandle | None = None
            try:
                handle = await self._media.start_recording(
                    camera=self._camera,
                    creds=self._creds,
                    lens_id=self._lens_id,
                    max_duration_s=None,  # we own the timing; no watchdog needed
                    include_audio=self._include_audio,
                    rolling=True,
                )
                self._current_handle = handle

                logger.info(
                    "rolling: segment started camera=%s id=%s -> %s",
                    self._camera.id,
                    handle.recording_id,
                    handle.partial_path,
                )

                # Sleep for the full segment duration; cancellation wakes us up.
                await asyncio.sleep(self._segment_duration_s)

                # Clear current handle before the (possibly slow) stop call
                # so that /status doesn't report a stale handle after stop.
                self._current_handle = None
                handle_to_stop = handle
                handle = None

                try:
                    final = await self._media.stop_recording(handle_to_stop)
                    logger.info(
                        "rolling: segment finalised camera=%s -> %s (%d bytes)",
                        self._camera.id,
                        final,
                        final.stat().st_size if final.exists() else 0,
                    )
                except Exception:
                    logger.exception(
                        "rolling: stop_recording failed for segment %s", handle_to_stop.recording_id
                    )

                self._segments_recorded += 1
                self._prune()

            except asyncio.CancelledError:
                # Task is being stopped. Try to finalise the current segment.
                h = handle or self._current_handle
                self._current_handle = None
                if h is not None and h.is_running:
                    logger.info(
                        "rolling: CancelledError – finalising segment %s", h.recording_id
                    )
                    try:
                        await self._media.stop_recording(h)
                    except Exception:
                        logger.exception("rolling: stop_recording in cancel handler failed")
                raise

            except Exception:
                # Non-fatal error (e.g. transient RTSP disconnect). Wait a bit
                # then retry so we don't spin-loop on a persistent failure.
                logger.exception(
                    "rolling: loop error for camera=%s; retrying in 15 s", self._camera.id
                )
                if handle is not None and handle.is_running:
                    try:
                        await self._media.cancel_recording(handle)
                    except Exception:
                        pass
                self._current_handle = None
                try:
                    await asyncio.sleep(15)
                except asyncio.CancelledError:
                    raise

    def _prune(self) -> None:
        try:
            resolved_lens, _, _ = _resolve_lens(self._camera, self._lens_id)
        except ValueError:
            return
        deleted = prune_rolling_recordings(self._camera, resolved_lens, self._max_segments)
        if deleted:
            logger.info(
                "rolling: pruned %d old segment(s) for camera=%s lens=%s (kept %d)",
                deleted,
                self._camera.id,
                resolved_lens,
                self._max_segments,
            )

    def on_disk_count(self) -> int:
        """Number of completed rolling segments currently on disk."""
        try:
            resolved_lens, _, _ = _resolve_lens(self._camera, self._lens_id)
        except ValueError:
            return 0
        return count_rolling_recordings(self._camera, resolved_lens)


__all__ = ["RollingRecorder"]
