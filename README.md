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

For a fuller, browser-rendered architecture guide with diagrams, see [`app/architecture.html`](app/architecture.html).

## High-level design

```mermaid
flowchart TB
    Client[Mobile / web client]

    subgraph API[Slate API]
        Auth[Authentication middleware]
        Routes[FastAPI routes]
        Services[Application services]
        Queue[Postgres-backed job queue]
        Worker[Job worker]
    end

    subgraph Core[Domain and ports]
        Domain[Domain models and rules]
        Ports[Repository, storage, STT, LLM, and audio ports]
    end

    subgraph Infrastructure[Adapters]
        DB[(Postgres)]
        Storage[Object storage]
        Audio[FFmpeg audio assembler]
        STT[Deepgram or Groq STT]
        LLM[Anthropic or OpenAI-compatible LLM]
    end

    Client -->|HTTPS| Auth --> Routes --> Services
    Services --> Domain
    Services --> Ports
    Services --> Queue
    Queue --> Worker
    Worker --> Services
    Ports --> DB
    Ports --> Storage
    Ports --> Audio
    Ports --> STT
    Ports --> LLM
```

## Recording and processing sequence

Audio chunks are uploaded during recording, but they are not transcribed individually. Processing begins only after
the client calls `POST /meetings/{meeting_id}/finish`; the worker then assembles every uploaded chunk and executes
the transcription, speaker, and enhancement stages in order.

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as Slate API
    participant S as Object storage
    participant D as Postgres
    participant W as Job worker
    participant F as FFmpeg
    participant T as Speech-to-text provider
    participant L as LLM provider

    loop While recording, for each audio chunk
        C->>A: POST /meetings/{id}/chunks
        A->>D: Create pending chunk record
        A-->>C: Signed upload URL
        C->>S: Upload chunk directly
        C->>A: POST /chunks/{chunkId}/uploaded
        A->>D: Mark chunk uploaded
    end

    Note over C,W: No transcription or analysis is queued during recording.
    C->>A: POST /meetings/{id}/finish
    A->>D: Set meeting to processing; enqueue transcribe job
    A-->>C: 202 Accepted

    W->>D: Claim transcribe job and list uploaded chunks
    W->>S: Download all uploaded chunks
    W->>F: Concatenate and normalize audio
    W->>T: Transcribe assembled recording with diarization
    T-->>W: Transcript segments and detected languages
    W->>D: Persist transcript; enqueue speaker job
    W->>S: Delete source chunks

    W->>D: Claim speaker job
    W->>L: Suggest speaker names from transcript and attendees
    W->>D: Persist suggestions; enqueue enhance job

    W->>D: Claim enhance job
    W->>L: Generate grounded enhanced notes
    W->>D: Persist enhanced notes; meeting becomes ready
```

## Run locally

```bash
cp .env.example .env          # fill in keys; STT_PROVIDER=fake and LLM_PROVIDER=fake need no keys
make db && make migrate       # Postgres in Docker, then apply migrations
make dev                      # API on :8000 (worker runs in-process when RUN_WORKER_IN_PROCESS=true)
make check                    # lint + typecheck + tests
TEST_DATABASE_URL=postgresql+asyncpg://slate:slate@localhost:5432/slate make test   # include DB tests
```

The database tests drop and recreate the schema, so set `TEST_DATABASE_URL` to a disposable database.

API docs are served at `/docs`.
