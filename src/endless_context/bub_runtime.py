from __future__ import annotations

import asyncio
import atexit
import importlib
import inspect
import json
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


def _json_dump_tool_result(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return json.dumps({"_type": type(value).__name__, "value": str(value)}, ensure_ascii=False)


def _patch_republic_tool_history_replay() -> None:
    context_module = importlib.import_module("republic.tape.context")
    if getattr(context_module, "_ec_tool_history_replay_patched", False):
        return

    def _default_messages_with_tool_events(entries: list[Any]) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        pending_calls: list[dict[str, Any]] = []

        def append_tool_message(call: dict[str, Any], content: str) -> None:
            call_id = ""
            if isinstance(call.get("id"), str):
                call_id = call["id"]
            tool_name = ""
            fn = call.get("function")
            if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                tool_name = fn["name"]
            msg: dict[str, Any] = {"role": "tool", "content": content}
            if call_id:
                msg["tool_call_id"] = call_id
            if tool_name:
                msg["name"] = tool_name
            messages.append(msg)

        def flush_pending_with_placeholder() -> None:
            for call in pending_calls:
                append_tool_message(call, "")
            pending_calls.clear()

        for entry in entries:
            kind = getattr(entry, "kind", "")
            payload = getattr(entry, "payload", {})
            if not isinstance(payload, dict):
                continue
            if kind == "message":
                if pending_calls:
                    flush_pending_with_placeholder()
                messages.append(dict(payload))
                continue
            if kind == "tool_call":
                if pending_calls:
                    flush_pending_with_placeholder()
                calls = payload.get("calls")
                if not isinstance(calls, list):
                    continue
                normalized_calls = [dict(call) for call in calls if isinstance(call, dict)]
                if not normalized_calls:
                    continue
                messages.append({"role": "assistant", "content": "", "tool_calls": normalized_calls})
                pending_calls = normalized_calls
                continue
            if kind != "tool_result":
                continue
            results = payload.get("results")
            if not isinstance(results, list):
                results = []
            for index, call in enumerate(pending_calls):
                content = (
                    _json_dump_tool_result(results[index])
                    if index < len(results)
                    else ""
                )
                append_tool_message(call, content)
            pending_calls = []
        if pending_calls:
            flush_pending_with_placeholder()
        return messages

    context_module._default_messages = _default_messages_with_tool_events  # type: ignore[attr-defined]
    context_module._ec_tool_history_replay_patched = True


def _patch_context_selection_iter() -> None:
    """Make ContextSelection iterable so bub builtin tape.info can use 'for msg in messages'.

    Republic's read_messages() returns ContextSelection (messages + error), not a list.
    Bub's tape.info iterates over it; without __iter__ that raises 'ContextSelection' object
    is not iterable. Other builtins (tape.anchors, tape.search, tape.reset, tape.handoff,
    skills.list, skills.describe) use list-returning APIs (read_entries, discover_skills,
    load_skill_body) and work with SeekDB store without this patch.
    """
    context_module = importlib.import_module("republic.tape.context")
    if getattr(context_module.ContextSelection, "_ec_iter_patched", False):
        return

    _ContextSelection = context_module.ContextSelection

    def ___iter__(self: Any) -> Any:
        return iter(self.messages)

    _ContextSelection.__iter__ = ___iter__  # type: ignore[method-assign]
    _ContextSelection._ec_iter_patched = True  # type: ignore[attr-defined]


def _resolve_awaitable(value: Any) -> Any:
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)

    result: dict[str, Any] = {"value": None, "error": None}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(value)
        except Exception as exc:  # pragma: no cover - defensive fallback
            result["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if result["error"] is not None:
        raise result["error"]
    return result["value"]


def _patch_republic_tool_executor() -> None:
    executor_module = importlib.import_module("republic.tools.executor")
    tool_executor_cls = getattr(executor_module, "ToolExecutor", None)
    if tool_executor_cls is None:
        return
    if getattr(tool_executor_cls, "_ec_async_tool_patched", False):
        return

    original_handle = tool_executor_cls._handle_tool_response
    ErrorKind = executor_module.ErrorKind
    ErrorPayload = executor_module.ErrorPayload

    def _handle_tool_response_with_await(
        self: Any,
        tool_response: Any,
        tool_map: dict[str, Any],
        context: Any,
    ) -> tuple[Any, Any]:
        outcome, error = original_handle(self, tool_response, tool_map, context)
        if error is not None or outcome is self._skip:
            return outcome, error
        try:
            return _resolve_awaitable(outcome), None
        except Exception as exc:
            tool_name = ""
            if isinstance(tool_response, dict):
                function = tool_response.get("function")
                if isinstance(function, dict):
                    raw_name = function.get("name")
                    if isinstance(raw_name, str):
                        tool_name = raw_name
            message = f"Tool '{tool_name}' execution failed." if tool_name else "Tool execution failed."
            failure = ErrorPayload(ErrorKind.TOOL, message, details={"error": repr(exc)})
            return failure.as_dict(), failure

    tool_executor_cls._handle_tool_response = _handle_tool_response_with_await
    tool_executor_cls._ec_async_tool_patched = True


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
    _patch_republic_tool_history_replay()
    _patch_republic_tool_executor()
    _patch_context_selection_iter()
    settings = load_settings(workspace)
    if not settings.api_base:
        # Bub Settings use env_prefix BUB_, so LLM_API_BASE in .env is not loaded into api_base.
        # pydantic-settings does not put .env into os.environ, so we load .env here.
        try:
            from dotenv import load_dotenv

            load_dotenv(workspace / ".env")
        except ImportError:
            pass
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
