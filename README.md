---
# Detailed docs: https://modelscope.cn/docs/studios/create
domain:
# domain: cv/nlp/audio/multi-modal/AutoML
tags:
  - chatbot
  - memory
  - gradio
  - oceanbase
datasets:
  evaluation:
  test:
  train:
models:
# - organization/model
## Entry file for Gradio/Streamlit is app.py by default
# deployspec:
#   entry_file: app.py
license: Apache License 2.0
---

# Endless Context

A lightweight Gradio chat agent with tape-first context management powered by Republic, SeekDB, and OceanBase. Built to be ModelScope-friendly while staying easy to run locally.

## What it is

- Tape-first chat orchestration with Republic (append-only entries + handoff anchors)
- SeekDB-backed persistent tape store via pyobvector dialect
- Three-pane Gradio UI (Tape / Conversation / Anchors) with context-window indicator
- Works out of the box on ModelScope Docker Studio via the included `Dockerfile`
- Supports OpenAI, Qwen (via OpenAI-compatible API base), and other any-llm compatible providers

## Run on ModelScope Docker Studio

1) Keep the provided `Dockerfile` and `docker/entrypoint.sh` (they start SeekDB and the app).
2) Exposed ports: `7860` (Gradio) and `2881` (SeekDB). Entry file is `app.py`.
3) Set environment secrets in Studio, e.g. `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_API_BASE`, `OCEANBASE_HOST`, `OCEANBASE_PORT`, `OCEANBASE_USER`, `OCEANBASE_PASSWORD`, `OCEANBASE_DATABASE`, and optional `REPUBLIC_*` overrides. For Qwen, use `LLM_PROVIDER=openai`, `LLM_MODEL=qwen-plus`, and `LLM_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1`.
4) Build and run; open the forwarded `7860` port to use the chat UI.

## Run locally (preferred: Docker)

### Docker Compose (app + SeekDB)
```bash
cp .env.example .env   # fill in keys
make compose-up        # builds and starts everything
```
The UI is at `http://localhost:7860`. Stop with `make compose-down`.

### Single container
```bash
docker build -t endless-context:latest .
docker run --rm -p 7860:7860 -p 2881:2881 endless-context:latest
```

### Bare-metal (advanced, no containers)
```bash
uv sync
cp .env.example .env
make run
```

## Docs

- `docs/index.md` contains architecture, local/Docker workflows, and configuration details.

## License

Apache License 2.0

## Related

- Republic: https://github.com/PsiACE/republic
- pyobvector: https://github.com/oceanbase/pyobvector
- SeekDB: https://www.oceanbase.ai/product/seekdb
- OceanBase: https://www.oceanbase.com/
