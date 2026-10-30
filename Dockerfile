FROM python:3.13-slim AS base
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11.13 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev
RUN useradd --create-home app && chown -R app /app
USER app
EXPOSE 8000
# Same image runs the worker with: uv run python -m app.worker
CMD ["sh", "-c", "uv run alembic upgrade head && uv run uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000}"]
