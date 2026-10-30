"""Domain error hierarchy. The HTTP layer maps these to status codes in one place (app/api/errors.py)."""


class DomainError(Exception):
    """Base for every expected failure."""


class NotFoundError(DomainError):
    """The resource does not exist or is not visible to the caller. Never reveal which."""


class ConflictError(DomainError):
    """The request conflicts with current state."""


class InvalidInputError(DomainError):
    """The request is well-formed but semantically invalid."""


class AuthenticationError(DomainError):
    """The caller's credentials are missing, invalid or expired."""


class ProviderError(DomainError):
    """An external provider (STT, LLM, storage) failed.

    `retryable` tells the job runner whether another attempt can succeed (timeouts, 429, 5xx) or not (bad input).
    """

    def __init__(self, provider: str, message: str, *, retryable: bool) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.retryable = retryable
