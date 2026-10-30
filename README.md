# slate-api

Backend for Slate, a notepad-first AI meeting companion. The app records audio and timestamped notes. This
service transcribes the recording, names the speakers, and returns enhanced notes. Your own lines are kept
verbatim, and each AI addition links back to the transcript.

## Architecture

Ports and adapters, with dependencies pointing inward:

```
app/domain     entities + domain errors           (no framework, no vendors)
app/ports      Protocols: repositories/UoW, auth, storage, STT, LLM, audio
app/services   use cases; depend only on ports
app/jobs       Postgres-backed queue runner: transcribe → speakers → enhance
app/adapters   SQLAlchemy, Supabase (auth JWKS, storage), Deepgram, Groq, Anthropic, ffmpeg
app/api        FastAPI routes (thin), DTOs, error mapping
app/container  composition root: the only place that picks concrete adapters
```

Patterns in use:

- **Repository and Unit of Work** for persistence.
- **Strategy and Factory** for providers, chosen from settings.
- **Command registry** for job handlers.
- **Dependency injection** through FastAPI dependencies and the container.

Tests swap in the in-memory fakes from `tests/fakes.py`.

## Run locally

```bash
cp .env.example .env          # fill in keys; STT_PROVIDER=fake and LLM_PROVIDER=fake need no keys
make db && make migrate       # Postgres in Docker, then apply migrations
make dev                      # API on :8000 (worker runs in-process when RUN_WORKER_IN_PROCESS=true)
make check                    # lint + typecheck + tests
TEST_DATABASE_URL=postgresql+asyncpg://slate:slate@localhost:5432/slate make test   # include DB tests
```

API docs are served at `/docs`.
