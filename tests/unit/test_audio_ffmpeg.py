import shutil
import subprocess
from pathlib import Path

import pytest

from app.adapters.audio.ffmpeg import FfmpegAudioAssembler
from app.domain.errors import ProviderError

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _sine(tmp_path: Path, name: str, freq: int) -> bytes:
    out = tmp_path / name
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=1",
         "-ac", "2", "-ar", "44100", "-c:a", "aac", str(out)],
        check=True,
    )  # fmt: skip
    return out.read_bytes()


def _probe(tmp_path: Path, data: bytes) -> dict[str, str]:
    path = tmp_path / "probe.m4a"
    path.write_bytes(data)
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=channels,sample_rate:format=duration",
         "-of", "default=noprint_wrappers=1", str(path)],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    return dict(line.split("=", 1) for line in result.stdout.split())


async def test_concat_two_chunks(tmp_path: Path) -> None:
    chunks = [_sine(tmp_path, "a.m4a", 440), _sine(tmp_path, "b.m4a", 880)]
    data, mime = await FfmpegAudioAssembler().concat(chunks, "audio/mp4")
    assert mime == "audio/mp4"
    info = _probe(tmp_path, data)
    assert info["channels"] == "1"
    assert info["sample_rate"] == "16000"
    assert float(info["duration"]) == pytest.approx(2.0, abs=0.2)


async def test_single_m4a_chunk_passes_through(tmp_path: Path) -> None:
    chunk = _sine(tmp_path, "a.m4a", 440)
    data, mime = await FfmpegAudioAssembler().concat([chunk], "audio/x-m4a")
    assert data == chunk
    assert mime == "audio/mp4"


def _wav(tmp_path: Path) -> bytes:
    wav = tmp_path / "a.wav"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=duration=1", str(wav)], check=True)
    return wav.read_bytes()


async def test_single_wav_chunk_is_transcoded(tmp_path: Path) -> None:
    data, mime = await FfmpegAudioAssembler().concat([_wav(tmp_path)], "audio/wav")
    assert mime == "audio/mp4"
    assert _probe(tmp_path, data)["sample_rate"] == "16000"


async def test_invalid_audio_is_non_retryable_error() -> None:
    with pytest.raises(ProviderError) as info:
        await FfmpegAudioAssembler().concat([b"not audio", b"also not audio"], "audio/mp4")
    assert info.value.retryable is False


async def test_empty_chunks_rejected() -> None:
    with pytest.raises(ProviderError):
        await FfmpegAudioAssembler().concat([], "audio/mp4")


async def test_missing_binary_is_provider_error() -> None:
    with pytest.raises(ProviderError):
        await FfmpegAudioAssembler(ffmpeg="/nonexistent/ffmpeg").concat([b"a", b"b"], "audio/mp4")
