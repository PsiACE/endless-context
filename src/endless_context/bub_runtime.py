from __future__ import annotations

import atexit
import importlib
import os
import threading
from pathlib import Path
from typing import Any

from bub.app.runtime import AppRuntime
from bub.config import load_settings

from endless_context.tape_store import SeekDBTapeStore

_PATCH_LOCK = threading.Lock()
_PATCHED = False
_RUNTIME_CLEANUP_REGISTERED = False


def _patch_bub_store_builder() -> None:
    global _PATCHED
    if _PATCHED:
        return
    with _PATCH_LOCK:
        if _PATCHED:
            return
        runtime_module = importlib.import_module("bub.app.runtime")

        def _build_seekdb_store(_settings: Any, _workspace: Path) -> SeekDBTapeStore:
            return SeekDBTapeStore.from_env()

        runtime_module.build_tape_store = _build_seekdb_store  # type: ignore[assignment]
        _PATCHED = True


def _register_runtime_cleanup(runtime: AppRuntime) -> None:
    global _RUNTIME_CLEANUP_REGISTERED
    if _RUNTIME_CLEANUP_REGISTERED:
        return

    def _cleanup() -> None:
        try:
            runtime.__exit__(None, None, None)
        except Exception:
            return

    atexit.register(_cleanup)
    _RUNTIME_CLEANUP_REGISTERED = True


def build_runtime(
    workspace: Path,
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    allowed_tools: set[str] | None = None,
    allowed_skills: set[str] | None = None,
    enable_scheduler: bool = True,
) -> AppRuntime:
    _patch_bub_store_builder()
    settings = load_settings(workspace)
    if not settings.api_base:
        llm_api_base = (os.getenv("LLM_API_BASE") or "").strip()
        if llm_api_base:
            settings = settings.model_copy(update={"api_base": llm_api_base})
    updates: dict[str, object] = {}
    if model:
        updates["model"] = model
    if max_tokens is not None:
        updates["max_tokens"] = max_tokens
    if updates:
        settings = settings.model_copy(update=updates)
    runtime = AppRuntime(
        workspace.resolve(),
        settings,
        allowed_tools=allowed_tools,
        allowed_skills=allowed_skills,
        enable_scheduler=enable_scheduler,
    )
    runtime.__enter__()
    _register_runtime_cleanup(runtime)
    return runtime
