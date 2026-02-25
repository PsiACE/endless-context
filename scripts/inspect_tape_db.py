#!/usr/bin/env -S uv run python
"""Read tape entries from SeekDB and print sample payloads per kind (for UI alignment)."""
from __future__ import annotations

import json
import os
import sys

# Load .env when run from repo root
if os.path.isfile(".env"):
    with open(".env") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"'))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def main() -> None:
    from endless_context.tape_store import SeekDBTapeStore

    store = SeekDBTapeStore.from_env()
    tapes = store.list_tapes()
    if not tapes:
        print("No tapes in DB. Create a session and send a message (optionally with tool use) first.")
        _print_canonical_shapes()
        return

    # Prefer a tape that might have tool calls
    tape_name = tapes[-1]
    entries = store.read(tape_name)
    if not entries:
        print(f"Tape {tape_name!r} has no entries.")
        _print_canonical_shapes()
        return

    by_kind: dict[str, list] = {}
    for e in entries:
        k = getattr(e, "kind", "?")
        by_kind.setdefault(k, []).append(e)

    print(f"Tape: {tape_name!r}  total entries: {len(entries)}\n")
    for kind in ("message", "tool_call", "tool_result", "event", "anchor", "system", "error"):
        if kind not in by_kind:
            continue
        payloads = [getattr(e, "payload", {}) for e in by_kind[kind][:2]]
        print(f"--- kind={kind!r} (showing up to 2 samples) ---")
        for i, p in enumerate(payloads):
            if isinstance(p, dict):
                print(json.dumps(p, ensure_ascii=False, indent=2))
            else:
                print(p)
        print()


def _print_canonical_shapes() -> None:
    """Print canonical payload shapes from Republic tape entries (for UI alignment)."""
    print("Canonical payload shapes (from republic.tape.entries):\n")
    print('--- kind="tool_call" ---')
    print(
        json.dumps(
            {
                "calls": [
                    {
                        "id": "call_abc123",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"location": "Beijing"}'},
                    }
                ]
            },
            indent=2,
        )
    )
    print('\n--- kind="tool_result" ---')
    print(json.dumps({"results": ["Sunny, 25°C"]}, indent=2))
    print()


if __name__ == "__main__":
    main()
