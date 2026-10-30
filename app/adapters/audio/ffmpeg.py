"""Joins recorded chunks into one mono 16 kHz AAC file with ffmpeg (no shell; temp files always cleaned up)."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Sequence
from pathlib import Path

from app.domain.errors import ProviderError

_OUTPUT_MIME = "audio/mp4"
_PASSTHROUGH = {"audio/mp4", "audio/m4a", "audio/x-m4a"}
_EXTENSIONS = {
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
}


class FfmpegAudioAssembler:
    """Implements AudioAssembler."""

    def __init__(self, ffmpeg: str = "ffmpeg", timeout_s: float = 600) -> None:
        self._ffmpeg = ffmpeg
        self._timeout = timeout_s

    async def concat(self, chunks: Sequence[bytes], mime_type: str) -> tuple[bytes, str]:
        if not chunks:
            raise ProviderError("ffmpeg", "no audio chunks to assemble", retryable=False)
        mime = mime_type.split(";")[0].strip().lower()
        if len(chunks) == 1 and mime in _PASSTHROUGH:
            return chunks[0], _OUTPUT_MIME

        ext = _EXTENSIONS.get(mime, ".bin")
        with tempfile.TemporaryDirectory(prefix="slate-audio-") as tmp:
            root = Path(tmp)
            inputs: list[str] = []
            for i, data in enumerate(chunks):
                path = root / f"chunk-{i:05d}{ext}"
                path.write_bytes(data)
                inputs.append(f"file '{path.name}'")
            listing = root / "inputs.txt"
            listing.write_text("\n".join(inputs) + "\n")
            output = root / "out.m4a"
            # Re-encode rather than stream-copy: chunks from a phone can differ in timestamps and codec params.
            args = [
                self._ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-f", "concat", "-safe", "0", "-i", str(listing),
                "-vn", "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "32k",
                "-movflags", "+faststart", str(output),
            ]  # fmt: skip
            await self._run(args)
            return output.read_bytes(), _OUTPUT_MIME

    async def _run(self, args: list[str]) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
        except OSError as e:
            raise ProviderError("ffmpeg", f"could not start ffmpeg: {e.strerror}", retryable=False) from e
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except TimeoutError as e:
            proc.kill()
            await proc.wait()
            raise ProviderError("ffmpeg", "timed out assembling audio", retryable=False) from e
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace").strip().splitlines()[-1:] or ["unknown error"]
            raise ProviderError("ffmpeg", f"exit {proc.returncode}: {detail[0][:300]}", retryable=False)
