"""Deterministic STT for local development without vendor keys (STT_PROVIDER=fake)."""

from app.domain.errors import ProviderError
from app.domain.models import Transcript, TranscriptSegment
from app.ports.providers import TranscriptionOptions


class StubSpeechToText:
    name = "stub-stt"

    async def transcribe(self, audio: bytes, mime_type: str, options: TranscriptionOptions) -> Transcript:
        if not audio:
            raise ProviderError(self.name, "empty audio", retryable=False)
        return Transcript(
            provider=self.name,
            language=options.language or "en",
            duration_ms=12_000,
            segments=[
                TranscriptSegment(speaker=0, start_ms=0, end_ms=4_000, text="Hi everyone, I'm Alex. Let's start."),
                TranscriptSegment(
                    speaker=1 if options.diarize else 0,
                    start_ms=4_000,
                    end_ms=12_000,
                    text=f"This is a stub transcript of {len(audio)} bytes of {mime_type} audio.",
                ),
            ],
        )
