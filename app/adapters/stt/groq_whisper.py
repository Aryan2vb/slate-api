"""Groq-hosted Whisper. A no-diarization fallback: every segment is attributed to speaker 0."""

from __future__ import annotations

import httpx

from app.adapters.http_errors import raise_for_status, transport_error
from app.domain.errors import ProviderError
from app.domain.models import Transcript, TranscriptSegment
from app.ports.providers import TranscriptionOptions

GROQ_TRANSCRIPTIONS_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
# Whisper's `prompt` biases spelling of names and jargon; it is limited to ~224 tokens, so keep it short.
MAX_PROMPT_CHARS = 600

_EXTENSIONS = {
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
}


class GroqWhisperSpeechToText:
    name = "groq-whisper"

    def __init__(
        self, api_key: str, *, model: str = "whisper-large-v3-turbo", client: httpx.AsyncClient | None = None
    ) -> None:
        if not api_key:
            raise ValueError("GROQ_API_KEY is required for the groq STT provider")
        self._api_key = api_key
        self._model = model
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))

    async def transcribe(self, audio: bytes, mime_type: str, options: TranscriptionOptions) -> Transcript:
        if not audio:
            raise ProviderError(self.name, "empty audio", retryable=False)
        if len(audio) > MAX_UPLOAD_BYTES:
            raise ProviderError(self.name, f"audio is {len(audio)} bytes; limit is {MAX_UPLOAD_BYTES}", retryable=False)
        data = {"model": self._model, "response_format": "verbose_json", "timestamp_granularities[]": "segment"}
        if options.language:
            data["language"] = options.language
        if options.keyterms:
            data["prompt"] = ", ".join(options.keyterms)[:MAX_PROMPT_CHARS]
        filename = f"audio.{_EXTENSIONS.get(mime_type.split(';')[0].strip().lower(), 'bin')}"
        try:
            response = await self._client.post(
                GROQ_TRANSCRIPTIONS_URL,
                data=data,
                files={"file": (filename, audio, mime_type)},
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        except httpx.HTTPError as e:
            raise transport_error(self.name, e) from e
        raise_for_status(self.name, response)
        try:
            body = response.json()
            segments = [
                TranscriptSegment(
                    speaker=0, start_ms=round(float(s["start"]) * 1000), end_ms=round(float(s["end"]) * 1000), text=text
                )
                for s in body.get("segments") or []
                if (text := (s.get("text") or "").strip())
            ]
            duration = body.get("duration")
            return Transcript(
                segments=segments,
                language=body.get("language"),
                duration_ms=round(float(duration) * 1000) if duration is not None else None,
                provider=self.name,
            )
        except (ValueError, KeyError, TypeError) as e:
            raise ProviderError(self.name, f"unexpected response shape: {e}", retryable=False) from e
