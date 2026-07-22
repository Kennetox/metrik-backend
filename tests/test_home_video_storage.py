import asyncio
from io import BytesIO
from pathlib import Path
import subprocess

from fastapi import UploadFile
import pytest

from services import storage


def _metadata(**overrides) -> storage.VideoMetadata:
    values = {
        "duration_seconds": 30,
        "video_codec": "h264",
        "audio_codec": "aac",
        "width": 720,
        "height": 1280,
        "fps": 30.0,
        "bitrate": 1_200_000,
        "pixel_format": "yuv420p",
    }
    values.update(overrides)
    return storage.VideoMetadata(**values)


def test_web_ready_home_video_requires_lightweight_h264_aac_profile():
    assert storage._is_web_ready_home_video(_metadata()) is True
    assert storage._is_web_ready_home_video(_metadata(width=1080, height=1920)) is False
    assert storage._is_web_ready_home_video(_metadata(video_codec="hevc")) is False
    assert storage._is_web_ready_home_video(_metadata(bitrate=2_000_000)) is False
    assert storage._is_web_ready_home_video(_metadata(pixel_format="yuv420p10le")) is False


def test_home_video_rejects_input_above_practical_limit(monkeypatch):
    monkeypatch.setattr(storage, "MAX_HOME_VIDEO_SIZE", 3)
    monkeypatch.setattr(storage.shutil, "which", lambda _binary: "/usr/bin/mock")
    upload = UploadFile(filename="reel.mp4", file=BytesIO(b"1234"))

    with pytest.raises(ValueError, match="supera los .* permitidos"):
        asyncio.run(storage.save_home_video(upload, tenant_id=1))


def test_optimized_home_video_uses_fast_remux(monkeypatch, tmp_path):
    commands: list[list[str]] = []

    monkeypatch.setenv("PRODUCT_VIDEO_UPLOAD_DIR", str(tmp_path / "videos"))
    monkeypatch.setattr(storage.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(storage, "_probe_video_metadata", lambda *_args: _metadata())

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        Path(command[-1]).write_bytes(b"optimized-video")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(storage, "_run_video_command", fake_run)
    upload = UploadFile(filename="reel.mp4", file=BytesIO(b"source-video"))

    result = asyncio.run(storage.save_home_video(upload, tenant_id=7))

    assert len(commands) == 1
    assert commands[0][commands[0].index("-c") + 1] == "copy"
    assert result.duration_seconds == 30
    assert result.size_bytes == len(b"optimized-video")
    assert (tmp_path / "videos" / "7" / result.filename).read_bytes() == b"optimized-video"


def test_unoptimized_home_video_uses_fast_transcode(monkeypatch, tmp_path):
    commands: list[list[str]] = []

    monkeypatch.setenv("PRODUCT_VIDEO_UPLOAD_DIR", str(tmp_path / "videos"))
    monkeypatch.setattr(storage.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(
        storage,
        "_probe_video_metadata",
        lambda *_args: _metadata(width=1080, height=1920, bitrate=5_000_000),
    )

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        Path(command[-1]).write_bytes(b"transcoded-video")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(storage, "_run_video_command", fake_run)
    upload = UploadFile(filename="reel.mov", file=BytesIO(b"source-video"))

    result = asyncio.run(storage.save_home_video(upload, tenant_id=7))

    assert len(commands) == 1
    assert commands[0][commands[0].index("-c:v") + 1] == "libx264"
    assert commands[0][commands[0].index("-preset") + 1] == "veryfast"
    assert result.size_bytes == len(b"transcoded-video")
