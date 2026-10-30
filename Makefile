.PHONY: dev worker test lint fmt typecheck db migrate check

dev:        ; uv run uvicorn app.main:create_app --factory --reload
worker:     ; uv run python -m app.worker
db:         ; docker compose up -d db
migrate:    ; uv run alembic upgrade head
test:       ; uv run pytest -q
lint:       ; uv run ruff check . && uv run ruff format --check .
fmt:        ; uv run ruff check --fix . && uv run ruff format .
typecheck:  ; uv run pyright
check: lint typecheck test
