#!/usr/bin/env -S uv run python
"""Drive _human_text and _render_structured with real-shaped payloads for alignment."""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if os.path.isfile(os.path.join(sys.path[0], ".env")):
    with open(os.path.join(sys.path[0], ".env")) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"'))

from app import _human_text, _render_structured

# Real-shaped payloads (from republic.tape.entries + bub usage)
SAMPLES: list[tuple[str, dict]] = [
    ("message", {"role": "user", "content": "What's the weather in Beijing?"}),
    ("message", {"role": "assistant", "content": "Let me check.\n\n"}),
    (
        "tool_call",
        {
            "calls": [
                {
                    "id": "call_abc123",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"location": "Beijing", "unit": "celsius"}',
                    },
                },
                {
                    "id": "call_def456",
                    "type": "function",
                    "function": {"name": "search", "arguments": '{"query": "weather"}'},
                },
            ]
        },
    ),
    ("tool_result", {"results": ["Sunny, 25°C"]}),
    (
        "tool_result",
        {
            "results": [
                {"message": "Tool execution failed.", "kind": "tool", "details": {"error": "Timeout"}}
            ]
        },
    ),
    ("event", {"name": "loop.step.finish", "data": {"step": 1, "visible_text": True}}),
    ("event", {"name": "command", "data": {"raw": ",tools", "output": "skills.list, ..."}}),
    ("anchor", {"name": "handoff:impl", "state": {"phase": "Implementation", "facts": ["A", "B"]}}),
    ("system", {"content": "You are a helpful assistant."}),
    ("error", {"kind": "tool", "message": "Tool execution failed.", "details": {"error": "..."}}),
]


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&([a-z]+);", lambda m: {"quot": '"', "lt": "<", "gt": ">", "amp": "&"}.get(m.group(1), m.group(0)), text)
    return " ".join(text.split())


def main() -> None:
    print("=== Human view (summary) ===\n")
    for kind, payload in SAMPLES:
        human = _human_text(kind, payload)
        print(f"  {kind}: {human!r}")

    print("\n=== Structured view (HTML stripped to one line) ===\n")
    for kind, payload in SAMPLES:
        structured = _render_structured(kind, payload)
        line = _strip_html(structured)[:120]
        print(f"  {kind}: {line}...")

    print("\n=== Full structured HTML for tool_call (first) ===\n")
    for kind, payload in SAMPLES:
        if kind == "tool_call":
            print(_render_structured(kind, payload))
            break

    print("\n=== Full structured HTML for tool_result (first) ===\n")
    for kind, payload in SAMPLES:
        if kind == "tool_result":
            print(_render_structured(kind, payload))
            break


if __name__ == "__main__":
    main()
