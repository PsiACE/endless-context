# Endless Context Docs

## Overview

Endless Context is a minimal Gradio chat agent backed by Republic tape primitives and a SeekDB/OceanBase persistent store. The design prioritizes auditability, explicit context slicing, and low operational overhead.

## Features

- Tape-first chat orchestration with Republic
- SeekDB-backed tape store via pyobvector SQLAlchemy dialect
- Gradio three-pane UI (Tape / Conversation / Anchors)
- Works on ModelScope Docker Studio via bundled `Dockerfile` + `docker/entrypoint.sh`
- Supports OpenAI, Qwen, and other any-llm compatible providers

## Architecture

- **UI layer**: Gradio `Blocks` in `app.py`
- **Agent layer**: `SimpleAgent` in `src/endless_context/agent.py`
- **Tape store layer**: `SeekDBTapeStore` in `src/endless_context/tape_store.py`

## Quick start

### Docker Compose (recommended for local)

```bash
cp .env.example .env
make compose-up
```

Starts SeekDB + app together. Stop with `make compose-down`. UI at `http://localhost:7860`.

### Single container

```bash
docker build -t endless-context:latest .
docker run --rm -p 7860:7860 -p 2881:2881 endless-context:latest
```

### ModelScope Docker Studio

- Use the provided `Dockerfile` (bundles SeekDB + app) and `docker/entrypoint.sh` (starts SeekDB, waits for it, then launches the app).
- Exposed ports: `7860` (Gradio UI) and `2881` (SeekDB). Entry file is `app.py`.
- Set environment secrets in Studio: `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL`, and `OCEANBASE_*` values (`HOST`, `PORT`, `USER`, `PASSWORD`, `DATABASE`), with optional `REPUBLIC_*` overrides.
- Build and run the container; open the forwarded `7860` port to chat.

## Configuration (.env)

- `OCEANBASE_HOST`, `OCEANBASE_PORT`, `OCEANBASE_USER`, `OCEANBASE_PASSWORD`, `OCEANBASE_DATABASE`
- `REPUBLIC_TAPE_TABLE`
- `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL`
- Optional `REPUBLIC_MODEL`, `REPUBLIC_API_BASE`, `REPUBLIC_VERBOSE`

## Development workflow (Makefile)

- `make install` — `uv sync` + `uv run prek install`
- `make compose-up|down|logs` — Docker Compose lifecycle (local recommended)
- `make docker-build` — build single-container image for ModelScope
- `make run` — bare-metal run (`uv run python app.py`, requires local SeekDB or external DB)
- `make test` — `uv run pytest`
- `make check` — lock consistency + `prek run -a`
- `make lint` / `make fmt` — ruff check/format
- `make docs-test` / `make docs` — build/serve docs

## Data flow

1. User sends a message through Gradio.
2. Agent resolves context window (`full`, `latest`, or `from-anchor`).
3. Republic executes chat and appends structured tape entries.
4. Tape entries are persisted in SeekDB via `SeekDBTapeStore`.
5. UI refreshes Tape/Anchors/Context indicator from the persisted tape.

## Testing

`pytest` covers context slicing, handoff behavior, and reply path wiring.

## Risks and mitigations

- External DB dependency: SeekDB must be reachable; use Docker Compose or Docker Studio when possible.
- LLM credentials: Missing keys fail at runtime; keep `.env` minimal and explicit.
