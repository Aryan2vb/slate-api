"""Shared translation of HTTP failures into ProviderError, so every vendor adapter classifies retries the same way."""

import httpx

from app.domain.errors import ProviderError

_RETRYABLE_STATUS = frozenset({408, 409, 425, 429})


def is_retryable_status(status_code: int) -> bool:
    return status_code in _RETRYABLE_STATUS or status_code >= 500


def raise_for_status(provider: str, response: httpx.Response) -> None:
    if response.is_success:
        return
    detail = response.text[:300] if response.text else response.reason_phrase
    raise ProviderError(
        provider, f"HTTP {response.status_code}: {detail}", retryable=is_retryable_status(response.status_code)
    )


def transport_error(provider: str, exc: httpx.HTTPError) -> ProviderError:
    """Network-level failures (timeouts, resets, DNS) are always worth another attempt."""
    return ProviderError(provider, f"{type(exc).__name__}: {exc}", retryable=True)
