"""Tests for the snapshot / recording media routes (using stubbed ffmpeg)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def test_status_advertises_media_actions(client: TestClient) -> None:
    body = client.get("/cameras/cam_lab499_west/status").json()
    actions = body["allowed_actions"]
    assert "snapshot" in actions
    assert "recording/start" in actions
    assert "recording/stop" in actions
    assert "recording/cancel" in actions
    # Lens entries always carry recording_active (default false).
    for lens in body["details"]["lenses"]:
        assert lens["recording_active"] is False
        assert lens["recording_started_at"] is None


def test_snapshot_returns_url_and_writes_file(
    client: TestClient, tmp_path: Path, stub_registry
) -> None:
    r = client.post("/cameras/cam_lab499_west/control/snapshot", json={"lens": "wide"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lens"] == "wide"
    assert body["url"].startswith("/cameras/cam_lab499_west/media/snapshots/wide/")
    on_disk = Path(body["path"])
    assert on_disk.exists() and on_disk.stat().st_size > 0
    # Snapshot landed under the per-camera tmp dir we configured in conftest.
    assert str(on_disk).startswith(str(tmp_path))


def test_snapshot_default_lens_falls_back_to_first(
    client: TestClient, stub_registry
) -> None:
    # No body -> defaults to body=None -> stubbed manager picks lens[0].
    r = client.post("/cameras/cam_lab499_west/control/snapshot")
    assert r.status_code == 200, r.text
    assert r.json()["lens"] == "wide"


def test_snapshot_unknown_lens_400(client: TestClient) -> None:
    # Stub raises ValueError for an unknown lens; route should map to 400.
    r = client.post(
        "/cameras/cam_lab499_west/control/snapshot",
        json={"lens": "doesnotexist"},
    )
    assert r.status_code == 400


def test_recording_start_and_stop(client: TestClient, stub_registry) -> None:
    r = client.post(
        "/cameras/cam_lab499_west/control/recording/start",
        json={"lens": "wide", "max_duration_s": 60},
    )
    assert r.status_code == 200, r.text
    start = r.json()
    rec_id = start["recording_id"]
    assert start["lens"] == "wide"
    assert start["max_duration_s"] == 60

    # Status must now report the lens as recording.
    body = client.get("/cameras/cam_lab499_west/status").json()
    wide = next(lens for lens in body["details"]["lenses"] if lens["id"] == "wide")
    assert wide["recording_active"] is True
    assert wide["recording_started_at"] is not None

    # Stop, default-locating the active recording (no recording_id needed).
    r = client.post("/cameras/cam_lab499_west/control/recording/stop")
    assert r.status_code == 200, r.text
    stop = r.json()
    assert stop["recording_id"] == rec_id
    assert stop["finalized"] is True
    assert Path(stop["path"]).exists()
    assert stop["duration_ms"] >= 0

    # After stopping, status reports the lens as idle again.
    body2 = client.get("/cameras/cam_lab499_west/status").json()
    wide2 = next(lens for lens in body2["details"]["lenses"] if lens["id"] == "wide")
    assert wide2["recording_active"] is False


def test_recording_start_409_on_duplicate_lens(
    client: TestClient, stub_registry
) -> None:
    r1 = client.post(
        "/cameras/cam_lab499_west/control/recording/start",
        json={"lens": "wide"},
    )
    assert r1.status_code == 200
    r2 = client.post(
        "/cameras/cam_lab499_west/control/recording/start",
        json={"lens": "wide"},
    )
    assert r2.status_code == 409
    # Cleanup: stop the one we started so the test is hermetic.
    client.post("/cameras/cam_lab499_west/control/recording/stop")


def test_recording_cancel_deletes_partial(client: TestClient, stub_registry) -> None:
    start = client.post(
        "/cameras/cam_lab499_west/control/recording/start",
        json={"lens": "tele"},
    ).json()
    partial_path = Path(start["path"] + ".partial") if not start["path"].endswith(".partial") else Path(start["path"])
    # The stub creates the partial file at start time.
    expected_partial = Path(start["path"]).with_suffix(".mp4.partial")
    assert expected_partial.exists() or partial_path.exists()

    r = client.post("/cameras/cam_lab499_west/control/recording/cancel")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["canceled"] is True
    assert body["deleted_path"] is not None
    assert not Path(body["deleted_path"]).exists()


def test_recording_stop_without_active_409(client: TestClient) -> None:
    r = client.post("/cameras/cam_lab499_west/control/recording/stop")
    assert r.status_code == 409


def test_media_list_returns_categorised_files(
    client: TestClient, stub_registry, tmp_path: Path
) -> None:
    # Take a snapshot so there's something to list.
    client.post("/cameras/cam_lab499_west/control/snapshot", json={"lens": "wide"})
    r = client.get("/cameras/cam_lab499_west/media")
    assert r.status_code == 200
    body = r.json()
    assert "snapshots" in body
    assert "recordings" in body
    assert any(f["lens"] == "wide" for f in body["snapshots"])
    # The .partial file produced by the stub start must NOT show up
    # in the listing.
    assert all(not f["name"].endswith(".partial") for f in body["snapshots"])
    assert all(not f["name"].endswith(".partial") for f in body["recordings"])


def test_media_download_serves_file(client: TestClient, stub_registry) -> None:
    snap = client.post(
        "/cameras/cam_lab499_west/control/snapshot",
        json={"lens": "wide"},
    ).json()
    name = Path(snap["path"]).name
    r = client.get(f"/cameras/cam_lab499_west/media/snapshots/wide/{name}")
    assert r.status_code == 200
    assert r.content == b"jpg"


def test_media_download_rejects_path_traversal(client: TestClient) -> None:
    # ``..`` in the URL must not escape the camera's media root.
    # FastAPI normalises some traversal at the router level, so we
    # also exercise an explicit traversal in the lens segment.
    r = client.get("/cameras/cam_lab499_west/media/snapshots/wide/..%2Fevil.jpg")
    assert r.status_code in (400, 404)


def test_media_download_404_for_missing(client: TestClient) -> None:
    r = client.get("/cameras/cam_lab499_west/media/snapshots/wide/never_taken.jpg")
    assert r.status_code == 404


def test_media_download_unknown_kind_400(client: TestClient) -> None:
    r = client.get("/cameras/cam_lab499_west/media/garbage/wide/whatever.jpg")
    assert r.status_code == 400
