"""External-provider ports. Each has one responsibility; vendors are adapters behind them (Strategy pattern)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel

from app.domain.models import AuthUser, Transcript


class AuthVerifier(Protocol):
    """Turns a bearer token into a user. Raises AuthenticationError on any failure."""

    async def verify(self, token: str) -> AuthUser: ...


class SignedUpload(BaseModel):
    url: str
    path: str
    expires_in_s: int


class ObjectStorage(Protocol):
    async def create_upload_url(self, path: str, *, overwrite: bool = False) -> SignedUpload:
        """Signed URL for one direct client upload. `overwrite` lets a retry replace a partial earlier upload."""
        ...

    async def download(self, path: str) -> bytes: ...
    async def delete(self, paths: Sequence[str]) -> None: ...


class AudioAssembler(Protocol):
    """Joins ordered audio chunks into one file the STT provider accepts (e.g. via ffmpeg)."""

    async def concat(self, chunks: Sequence[bytes], mime_type: str) -> tuple[bytes, str]:
        """Returns (audio_bytes, mime_type_of_result)."""
        ...


class TranscriptionOptions(BaseModel):
    diarize: bool = True
    keyterms: list[str] = []
    language: str | None = None


class SpeechToText(Protocol):
    name: str

    async def transcribe(self, audio: bytes, mime_type: str, options: TranscriptionOptions) -> Transcript:
        """Raises ProviderError(retryable=...) on failure."""
        ...


class LanguageModel(Protocol):
    """Structured generation only: every call returns a validated instance of `schema`."""

    name: str

    async def generate[T: BaseModel](self, *, system: str, prompt: str, schema: type[T], max_tokens: int = 8000) -> T:
        """Raises ProviderError(retryable=...) on failure or when output never validates."""
        ...


class RateLimiter(Protocol):
    """Counts actions per key in a sliding window. Raises RateLimitedError when `limit` is already reached."""

    async def hit(self, key: str, *, limit: int, window_s: int) -> None: ...
