"""Deepgram Nova-3 pre-recorded transcription with diarization and keyterm prompting."""

from __future__ import annotations

from typing import Any

import httpx

from app.adapters.http_errors import raise_for_status, transport_error
from app.domain.errors import ProviderError
from app.domain.models import Transcript, TranscriptSegment
from app.ports.providers import TranscriptionOptions

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
# Deepgram caps keyterms at 500 tokens per request and recommends 20-50 terms; 50 stays well inside both.
MAX_KEYTERMS = 50
MAX_KEYTERM_CHARS = 100


def normalize_keyterms(terms: list[str]) -> list[str]:
    """Trim, drop empties and over-long entries, dedupe case-insensitively keeping first-seen order, cap the count."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in terms:
        term = " ".join(raw.split())
        key = term.casefold()
        if not term or len(term) > MAX_KEYTERM_CHARS or key in seen:
            continue
        seen.add(key)
        out.append(term)
        if len(out) == MAX_KEYTERMS:
            break
    return out


def _ms(seconds: Any) -> int:
    return round(float(seconds) * 1000)


def _segments_from_utterances(utterances: list[dict[str, Any]]) -> list[TranscriptSegment]:
    return [
        TranscriptSegment(speaker=int(u.get("speaker") or 0), start_ms=_ms(u["start"]), end_ms=_ms(u["end"]), text=text)
        for u in utterances
        if (text := (u.get("transcript") or "").strip())
    ]


def _segments_from_words(words: list[dict[str, Any]]) -> list[TranscriptSegment]:
    """Fallback when utterances are absent: merge consecutive words from the same speaker into one segment."""
    segments: list[TranscriptSegment] = []
    current: dict[str, Any] | None = None
    for w in words:
        speaker = int(w.get("speaker") or 0)
        token = w.get("punctuated_word") or w.get("word") or ""
        if current and current["speaker"] == speaker:
            current["end"] = w["end"]
            current["tokens"].append(token)
            continue
        if current:
            segments.append(_flush(current))
        current = {"speaker": speaker, "start": w["start"], "end": w["end"], "tokens": [token]}
    if current:
        segments.append(_flush(current))
    return [s for s in segments if s.text]


def _flush(c: dict[str, Any]) -> TranscriptSegment:
    return TranscriptSegment(
        speaker=c["speaker"], start_ms=_ms(c["start"]), end_ms=_ms(c["end"]), text=" ".join(c["tokens"]).strip()
    )


def parse_response(body: dict[str, Any], provider: str) -> Transcript:
    results = body.get("results") or {}
    utterances = results.get("utterances")
    if utterances:
        segments = _segments_from_utterances(utterances)
    else:
        channels = results.get("channels") or []
        alternatives = (channels[0].get("alternatives") or []) if channels else []
        segments = _segments_from_words(alternatives[0].get("words") or []) if alternatives else []
    metadata = body.get("metadata") or {}
    duration = metadata.get("duration")
    language = None
    if channels := results.get("channels"):
        language = channels[0].get("detected_language")
    return Transcript(
        segments=segments,
        language=language,
        duration_ms=_ms(duration) if duration is not None else None,
        provider=provider,
    )


class DeepgramSpeechToText:
    name = "deepgram"

    def __init__(self, api_key: str, *, model: str = "nova-3", client: httpx.AsyncClient | None = None) -> None:
        if not api_key:
            raise ValueError("DEEPGRAM_API_KEY is required for the deepgram STT provider")
        self._api_key = api_key
        self._model = model
        # Up to an hour of audio: upload plus processing can take minutes.
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))

    def _params(self, options: TranscriptionOptions) -> httpx.QueryParams:
        params = httpx.QueryParams(
            {
                "model": self._model,
                "smart_format": "true",
                "punctuate": "true",
                "utterances": "true",
                "diarize": "true" if options.diarize else "false",
            }
        )
        if options.language:
            params = params.set("language", options.language)
        for term in normalize_keyterms(options.keyterms):
            params = params.add("keyterm", term)
        return params

    async def transcribe(self, audio: bytes, mime_type: str, options: TranscriptionOptions) -> Transcript:
        if not audio:
            raise ProviderError(self.name, "empty audio", retryable=False)
        try:
            response = await self._client.post(
                DEEPGRAM_URL,
                params=self._params(options),
                content=audio,
                headers={"Authorization": f"Token {self._api_key}", "Content-Type": mime_type},
            )
        except httpx.HTTPError as e:
            raise transport_error(self.name, e) from e
        raise_for_status(self.name, response)
        try:
            return parse_response(response.json(), self.name)
        except (ValueError, KeyError, TypeError) as e:
            raise ProviderError(self.name, f"unexpected response shape: {e}", retryable=False) from e
