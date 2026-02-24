#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
import urllib.request
from typing import Any

from gradio_client import Client

from endless_context.agent import SimpleAgent


def _bool_env(name: str) -> bool:
    value = os.getenv(name, "")
    return value.lower() in {"1", "true", "yes", "on"}


def _print_env_summary() -> None:
    provider = os.getenv("LLM_PROVIDER") or "unset"
    model = os.getenv("LLM_MODEL") or os.getenv("REPUBLIC_MODEL") or "unset"
    api_base = os.getenv("LLM_API_BASE") or os.getenv("REPUBLIC_API_BASE") or "(provider default)"
    db_host = os.getenv("OCEANBASE_HOST", "127.0.0.1")
    db_port = os.getenv("OCEANBASE_PORT", "2881")
    db_name = os.getenv("OCEANBASE_DATABASE", "republic")
    has_api_key = bool(os.getenv("LLM_API_KEY") or os.getenv("REPUBLIC_API_KEY"))

    print("=== ENV SUMMARY ===")
    print(f"LLM_PROVIDER={provider}")
    print(f"LLM_MODEL/REPUBLIC_MODEL={model}")
    print(f"LLM_API_BASE/REPUBLIC_API_BASE={api_base}")
    print(f"HAS_API_KEY={has_api_key}")
    print(f"OCEANBASE_HOST={db_host}")
    print(f"OCEANBASE_PORT={db_port}")
    print(f"OCEANBASE_DATABASE={db_name}")
    print("===================")


def _check_http_health(base_url: str) -> None:
    print(f"[health] GET {base_url}")
    start = time.time()
    req = urllib.request.Request(base_url, method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - local debugging only
        elapsed = time.time() - start
        print(f"[health] status={resp.status} elapsed={elapsed:.2f}s")


def _simulate_gradio_send(base_url: str, message: str) -> tuple[bool, str]:
    print("[gradio] connect")
    client = Client(base_url)

    print("[gradio] call /_send")
    start = time.time()
    result = client.predict(message=message, view_mode="latest", anchor_name=None, api_name="/_send")
    elapsed = time.time() - start

    # _send returns:
    # (message, messages, tape_entries, anchor, table, tape_footer, context_indicator, status_text)
    status_text = ""
    assistant_preview = ""
    chat_messages: list[dict[str, Any]] = []
    if isinstance(result, tuple) and len(result) >= 8:
        chat_messages = result[1] if isinstance(result[1], list) else []
        status_text = str(result[7] or "")

    for item in reversed(chat_messages):
        if isinstance(item, dict) and item.get("role") == "assistant":
            content = item.get("content")
            if isinstance(content, str):
                assistant_preview = content[:160]
            elif isinstance(content, list) and content:
                first = content[0]
                if isinstance(first, dict) and isinstance(first.get("text"), str):
                    assistant_preview = first["text"][:160]
            break

    print(f"[gradio] elapsed={elapsed:.2f}s")
    print(f"[gradio] status_text={status_text!r}")
    print(f"[gradio] assistant_preview={assistant_preview!r}")

    ok = not status_text.startswith("Error:")
    return ok, status_text


def _simulate_agent_direct(message: str) -> tuple[bool, str]:
    print("[agent] direct call SimpleAgent.reply()")
    agent = SimpleAgent()
    start = time.time()
    reply = agent.reply(message, view_mode="latest", anchor_name=None)
    elapsed = time.time() - start
    print(f"[agent] elapsed={elapsed:.2f}s")
    print(f"[agent] reply_preview={reply[:200]!r}")
    ok = not reply.startswith("Error:")
    return ok, reply


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test chat send path (Gradio + SimpleAgent).")
    parser.add_argument("--base-url", default="http://127.0.0.1:7860", help="Gradio server URL")
    parser.add_argument("--message", default="你好，请简短回复：pong", help="Test message to send")
    parser.add_argument(
        "--skip-direct",
        action="store_true",
        help="Skip direct SimpleAgent path and only test Gradio HTTP path",
    )
    args = parser.parse_args()

    if _bool_env("SMOKE_DEBUG_ENV"):
        _print_env_summary()

    try:
        _check_http_health(args.base_url)
    except Exception as exc:
        print("[health] FAILED")
        traceback.print_exc()
        print(f"[health] error={exc}")
        return 10

    gradio_ok = False
    direct_ok = False

    try:
        gradio_ok, gradio_msg = _simulate_gradio_send(args.base_url, args.message)
    except Exception as exc:
        print("[gradio] FAILED")
        traceback.print_exc()
        print(f"[gradio] error={exc}")
        gradio_msg = str(exc)

    if not args.skip_direct:
        try:
            direct_ok, direct_msg = _simulate_agent_direct(args.message)
        except Exception as exc:
            print("[agent] FAILED")
            traceback.print_exc()
            print(f"[agent] error={exc}")
            direct_msg = str(exc)
    else:
        direct_ok, direct_msg = True, "skipped"

    print("=== RESULT ===")
    print(f"gradio_ok={gradio_ok}")
    print(f"direct_ok={direct_ok}")
    print("==============")

    if gradio_ok and direct_ok:
        return 0

    # Non-zero exit for CI or fast diagnosis
    print("gradio_message:", gradio_msg)
    print("direct_message:", direct_msg)
    return 2


if __name__ == "__main__":
    sys.exit(main())
