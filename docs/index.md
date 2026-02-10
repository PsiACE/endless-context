# PowerAgent Docs

## Overview

PowerAgent is a minimal Gradio chat agent backed by PowerMem. It stores chat turns as memories and uses recent semantic matches to enrich future responses. The design prioritizes clarity, low operational overhead, and portability to ModelScope.

## Features

- Long-term memory with vector search (SeekDB + OceanBase)
- Gradio chat UI with streaming
- Works on ModelScope Docker Studio via bundled `Dockerfile` + `docker/entrypoint.sh`
- Supports OpenAI, Qwen, and other LLM/embedding providers

## Architecture

- **UI layer**: Gradio `ChatInterface` in `app.py`
- **Agent layer**: `SimpleAgent` in `src/poweragent/agent.py`
- **Memory layer**: PowerMem using OceanBase-compatible storage

## Quick start

### Docker Compose (recommended for local)

```bash
cp .env.example .env
make compose-up
```

Starts SeekDB + app together. Stop with `make compose-down`. UI at `http://localhost:7860`.

### Single container

```bash
docker build -t poweragent:latest .
docker run --rm -p 7860:7860 -p 2881:2881 poweragent:latest
```

### ModelScope Docker Studio

- Use the provided `Dockerfile` (bundles SeekDB + app) and `docker/entrypoint.sh` (starts SeekDB, waits for it, then launches the app).
- Exposed ports: `7860` (Gradio UI) and `2881` (SeekDB). Entry file is `app.py`.
- Set environment secrets in Studio: `DATABASE_PROVIDER=oceanbase`, `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL`, `EMBEDDING_PROVIDER`, `EMBEDDING_API_KEY`, `EMBEDDING_MODEL`, `EMBEDDING_DIMS`, and `OCEANBASE_*` if overriding defaults.
- Build and run the container; open the forwarded `7860` port to chat.

## Configuration (.env)

- `DATABASE_PROVIDER=oceanbase`
- `OCEANBASE_HOST`, `OCEANBASE_PORT`, `OCEANBASE_USER`, `OCEANBASE_PASSWORD`, `OCEANBASE_DATABASE`
- `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL`
- `EMBEDDING_PROVIDER`, `EMBEDDING_API_KEY`, `EMBEDDING_MODEL`, `EMBEDDING_DIMS`

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
2. Agent queries memory for related items.
3. Agent calls the LLM with system prompt + memory context + history + user input.
4. Agent writes the user/assistant turn to memory.
5. Response is returned to the UI.

## Testing

`pytest` covers agent message assembly and memory integration.

## Risks and mitigations

- External DB dependency: SeekDB must be reachable; use Docker Compose or Docker Studio when possible.
- LLM credentials: Missing keys fail at runtime; keep `.env` minimal and explicit.
