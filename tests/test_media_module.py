"""Unit tests for kasa_tapo_services.tapo.media helpers.

These do NOT spawn ffmpeg - they cover only the pure-Python helpers
(path resolution, redaction, traversal protection, filename format).
End-to-end recording/snapshot behaviour is exercised via the route
tests in ``test_camera_media_routes.py`` against a stubbed manager.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from kasa_tapo_services.config import (
    DeviceConfig,
    LensConfig,
    MediaConfig,
)
from kasa_tapo_services.tapo import media as media_mod


def _camera(media: MediaConfig | None = None) -> DeviceConfig:
    return DeviceConfig(
        id="cam_test",
        name="Test",
        kind="camera",
        host="192.168.1.1",
        lenses=[
            LensConfig(id="wide", label="Wide", rtsp_path="stream1"),
            LensConfig(id="tele", label="Tele", rtsp_path="stream6"),
        ],
        media=media,
    )


# ---------------------------------------------------------------------
# Path resolution.
# ---------------------------------------------------------------------


def test_default_media_root_falls_back_to_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KASA_TAPO_MEDIA_ROOT", raising=False)
    cam = _camera()
    snap = media_mod._camera_snapshots_dir(cam)
    rec = media_mod._camera_recordings_dir(cam)
    home = Path.home()
    assert str(snap).startswith(str(home))
    assert str(rec).startswith(str(home))
    assert snap.parts[-2:] == ("snapshots", "cam_test")
    assert rec.parts[-2:] == ("recordings", "cam_test")


def test_env_override_for_default_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KASA_TAPO_MEDIA_ROOT", str(tmp_path / "custom"))
    cam = _camera()
    assert str(media_mod._camera_snapshots_dir(cam)).startswith(str(tmp_path / "custom"))


def test_per_camera_override_wins_over_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KASA_TAPO_MEDIA_ROOT", str(tmp_path / "ignored"))
    cam = _camera(
        MediaConfig(
            snapshots_dir=str(tmp_path / "pics"),
            recordings_dir=str(tmp_path / "vids"),
        )
    )
    assert media_mod._camera_snapshots_dir(cam) == (tmp_path / "pics").resolve()
    assert media_mod._camera_recordings_dir(cam) == (tmp_path / "vids").resolve()


def test_tilde_expansion_in_per_camera_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    cam = _camera(
        MediaConfig(
            snapshots_dir="~/some/dir",
            recordings_dir="~/other/dir",
        )
    )
    snap = media_mod._camera_snapshots_dir(cam)
    rec = media_mod._camera_recordings_dir(cam)
    home = str(Path.home())
    assert str(snap).startswith(home)
    assert str(rec).startswith(home)


# ---------------------------------------------------------------------
# Filename / URL helpers.
# ---------------------------------------------------------------------


def test_ts_filename_uses_dashes_not_colons() -> None:
    when = datetime(2026, 5, 8, 21, 43, 0, tzinfo=timezone.utc)
    out = media_mod._ts_filename(".jpg", when=when)
    assert out == "2026-05-08T21-43-00Z.jpg"
    assert ":" not in out


def test_redact_url_hides_credentials() -> None:
    url = "rtsp://alice:s3cret@192.168.1.1:554/stream1"
    redacted = media_mod._redact_url(url)
    assert "alice" not in redacted
    assert "s3cret" not in redacted
    assert "192.168.1.1:554/stream1" in redacted


def test_redact_url_passes_through_when_no_credentials() -> None:
    url = "rtsp://192.168.1.1:554/stream1"
    assert media_mod._redact_url(url) == url


# ---------------------------------------------------------------------
# Lens resolution.
# ---------------------------------------------------------------------


def test_resolve_lens_default_is_first() -> None:
    cam = _camera()
    lens, label, rtsp_path = media_mod._resolve_lens(cam, None)
    assert lens == "wide"
    assert rtsp_path == "stream1"


def test_resolve_lens_by_id() -> None:
    cam = _camera()
    lens, _label, rtsp_path = media_mod._resolve_lens(cam, "tele")
    assert lens == "tele"
    assert rtsp_path == "stream6"


def test_resolve_lens_unknown_raises() -> None:
    cam = _camera()
    with pytest.raises(ValueError, match="no lens id"):
        media_mod._resolve_lens(cam, "ghost")


def test_resolve_lens_no_lenses_raises() -> None:
    # DeviceConfig validates `kind=camera requires at least one lens`,
    # so we test the resolver against a synthetic config that bypasses
    # that. Pydantic's `model_construct` skips the validator.
    cam = DeviceConfig.model_construct(  # type: ignore[arg-type]
        id="cam_empty",
        name="Empty",
        kind="camera",
        host="192.168.1.1",
        lenses=None,
    )
    with pytest.raises(ValueError, match="no lenses configured"):
        media_mod._resolve_lens(cam, None)


# ---------------------------------------------------------------------
# Path traversal protection.
# ---------------------------------------------------------------------


def test_resolve_media_path_inside_root(tmp_path: Path) -> None:
    cam = _camera(
        MediaConfig(
            snapshots_dir=str(tmp_path / "snaps"),
            recordings_dir=str(tmp_path / "vids"),
        )
    )
    (tmp_path / "snaps" / "wide").mkdir(parents=True)
    target = tmp_path / "snaps" / "wide" / "ok.jpg"
    target.write_bytes(b"x")
    out = media_mod.resolve_media_path(cam, kind="snapshots", lens="wide", name="ok.jpg")
    assert out == target.resolve()


def test_resolve_media_path_blocks_traversal(tmp_path: Path) -> None:
    cam = _camera(
        MediaConfig(
            snapshots_dir=str(tmp_path / "snaps"),
            recordings_dir=str(tmp_path / "vids"),
        )
    )
    (tmp_path / "snaps").mkdir()
    with pytest.raises(ValueError, match="traversal"):
        media_mod.resolve_media_path(
            cam, kind="snapshots", lens="..", name="evil.jpg"
        )


def test_resolve_media_path_blocks_partial_files(tmp_path: Path) -> None:
    cam = _camera(
        MediaConfig(
            snapshots_dir=str(tmp_path / "snaps"),
            recordings_dir=str(tmp_path / "vids"),
        )
    )
    (tmp_path / "vids" / "wide").mkdir(parents=True)
    (tmp_path / "vids" / "wide" / "live.mp4.partial").write_bytes(b"x")
    with pytest.raises(ValueError, match="partial"):
        media_mod.resolve_media_path(
            cam, kind="recordings", lens="wide", name="live.mp4.partial"
        )


def test_resolve_media_path_unknown_kind(tmp_path: Path) -> None:
    cam = _camera(
        MediaConfig(
            snapshots_dir=str(tmp_path / "snaps"),
            recordings_dir=str(tmp_path / "vids"),
        )
    )
    with pytest.raises(ValueError, match="unknown media kind"):
        media_mod.resolve_media_path(cam, kind="garbage", lens="wide", name="x.jpg")


# ---------------------------------------------------------------------
# Listing.
# ---------------------------------------------------------------------


def test_list_camera_media_skips_partial(tmp_path: Path) -> None:
    cam = _camera(
        MediaConfig(
            snapshots_dir=str(tmp_path / "snaps"),
            recordings_dir=str(tmp_path / "vids"),
        )
    )
    snap_dir = tmp_path / "snaps" / "wide"
    snap_dir.mkdir(parents=True)
    (snap_dir / "real.jpg").write_bytes(b"x")
    rec_dir = tmp_path / "vids" / "wide"
    rec_dir.mkdir(parents=True)
    (rec_dir / "done.mp4").write_bytes(b"x")
    (rec_dir / "live.mp4.partial").write_bytes(b"x")

    out = media_mod.list_camera_media(cam)
    snap_names = [f["name"] for f in out["snapshots"]]
    rec_names = [f["name"] for f in out["recordings"]]
    assert snap_names == ["real.jpg"]
    assert rec_names == ["done.mp4"]


def test_list_camera_media_handles_missing_dirs(tmp_path: Path) -> None:
    cam = _camera(
        MediaConfig(
            snapshots_dir=str(tmp_path / "missing-snaps"),
            recordings_dir=str(tmp_path / "missing-vids"),
        )
    )
    out = media_mod.list_camera_media(cam)
    assert out == {"snapshots": [], "recordings": []}
