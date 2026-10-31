"""Receives the mobile app's own log events so client-side failures show up next to server logs.

Events are logged as `app.<event>` with `source="app"` and the caller's user id (bound by authentication), e.g.
`app.upload.failed`. Payloads are small, typed and bounded; nothing here is persisted beyond the log stream.
"""

from datetime import datetime
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.deps import CurrentUser, rate_limit

router = APIRouter(tags=["client-logs"])
log = structlog.get_logger("slate.app")

Scalar = str | int | float | bool | None
MAX_FIELD_CHARS = 500


class ClientLogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: Literal["debug", "info", "warn", "error"]
    event: Annotated[str, Field(pattern=r"^[a-z0-9_.]{1,80}$")]
    at: datetime | None = None
    fields: dict[Annotated[str, Field(pattern=r"^[A-Za-z0-9_]{1,40}$")], Scalar] = Field(
        default_factory=dict, max_length=30
    )

    @field_validator("fields")
    @classmethod
    def _truncate(cls, value: dict[str, Scalar]) -> dict[str, Scalar]:
        return {k: v[:MAX_FIELD_CHARS] if isinstance(v, str) else v for k, v in value.items()}


class ClientLogBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[ClientLogEntry] = Field(min_length=1, max_length=50)
    app_version: str | None = Field(default=None, max_length=40)
    platform: str | None = Field(default=None, max_length=40)


# Method names, resolved per call: binding log.info etc. at import would freeze the logging configuration.
_LEVEL = {"debug": "debug", "info": "info", "warn": "warning", "error": "error"}


@router.post(
    "/client-logs",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(rate_limit("client_logs"))],
)
async def ingest(batch: ClientLogBatch, _user: CurrentUser) -> None:
    for entry in batch.entries:
        getattr(log, _LEVEL[entry.level])(
            f"app.{entry.event}",
            source="app",
            client_at=entry.at.isoformat() if entry.at else None,
            app_version=batch.app_version,
            platform=batch.platform,
            **{f"app_{k}": v for k, v in entry.fields.items()},  # prefixed so they cannot clobber server fields
        )
