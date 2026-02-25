from __future__ import annotations

import html
import json
import os
import threading
import time
from typing import Any

import gradio as gr

from endless_context.agent import BubAgent, ConversationSnapshot, ViewMode

_agent: BubAgent | None = None


def get_agent() -> BubAgent:
    global _agent
    if _agent is None:
        _agent = BubAgent()
    return _agent


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _kind_label(kind: str) -> str:
    return kind.upper()[:10]


# Unified: one ordered key list per kind for structured view. Unknown kinds use payload keys.
_ORDERED_KEYS: dict[str, list[str]] = {
    "message": ["role", "content"],
    "event": ["name", "data"],
    "anchor": ["name", "state"],
    "system": ["content"],
    "error": ["kind", "message", "details"],
    "tool_call": ["calls"],
    "tool_result": ["results"],
}


def _args_summary(arguments: Any, max_values: int = 4, max_len: int = 24) -> str:
    """Param values from JSON string or dict (for human line); truncated for one-line."""
    obj: dict[str, Any] | None = None
    if isinstance(arguments, dict):
        obj = arguments
    elif isinstance(arguments, str) and arguments.strip():
        try:
            parsed = json.loads(arguments)
            obj = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass
    if not obj:
        return ""
    parts: list[str] = []
    for v in list(obj.values())[:max_values]:
        s = str(v).strip().replace("\n", " ")
        if len(s) > max_len:
            s = s[: max_len - 1] + "…"
        parts.append(s)
    return ", ".join(parts)


def _human_text(kind: str, payload: dict[str, Any]) -> str:
    """One-line summary: same rule set for all kinds (primary line + optional suffix)."""
    # 1) List-shaped: calls / results
    calls = payload.get("calls")
    if isinstance(calls, list) and calls:
        parts: list[str] = []
        for call in calls[:3]:
            if not isinstance(call, dict):
                continue
            fn = call.get("function")
            name = fn.get("name") if isinstance(fn, dict) else "?"
            args_raw = fn.get("arguments") if isinstance(fn, dict) else None
            ps = _args_summary(args_raw)
            parts.append(f"{name}({ps})" if ps else f"{name}()")
        if len(calls) > 3:
            parts.append("…")
        return ", ".join(parts) if parts else "tool_call"

    results = payload.get("results")
    if isinstance(results, list):
        if not results:
            return "tool_result (0 results)"
        first = results[0]
        if isinstance(first, dict):
            for k in ("message", "error", "content"):
                v = first.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()[:90]
            return f"results: {len(results)} item(s)"
        if isinstance(first, str) and first.strip():
            return first.strip()[:90]
        return f"results: {len(results)} item(s)"

    # 2) Primary line from common fields (same order for all kinds)
    role = payload.get("role")
    content = payload.get("content")
    if isinstance(content, str) and content.strip():
        prefix = f"{role}: " if isinstance(role, str) and role.strip() else ""
        return f"{prefix}{content.strip().replace(chr(10), ' ')}"

    for key in ("message", "name", "content"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            line = v.strip()[:120]
            if key == "name" and kind == "event":
                data = payload.get("data")
                if isinstance(data, dict) and data:
                    line += " (" + ", ".join(str(x) for x in list(data.keys())[:3]) + ")"
                line = "event: " + line
            elif key == "name" and kind == "anchor":
                state = payload.get("state")
                if isinstance(state, dict):
                    phase = state.get("phase")
                    if isinstance(phase, str) and phase.strip():
                        line += f" ({phase.strip()})"
            return line

    data = payload.get("data")
    if isinstance(data, dict):
        for k in ("message", "error", "name", "status"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()[:120]

    compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return compact[:120]


def _kv_row(key: str, value: Any) -> str:
    if isinstance(value, (dict, list)):
        shown = json.dumps(value, ensure_ascii=False, indent=2)
    else:
        shown = str(value)
    return (
        "<div class='entry-kv'>"
        f"<span class='entry-k'>{html.escape(str(key))}</span>"
        f"<span class='entry-v'>{html.escape(shown)}</span>"
        "</div>"
    )


def _parse_arguments_for_display(arguments: Any) -> Any:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str) and arguments.strip():
        try:
            return json.loads(arguments)
        except json.JSONDecodeError:
            return arguments
    return arguments


def _structured_value(key: str, value: Any) -> str:
    """One rule: value is list of dicts → numbered blocks; else kv row."""
    if not isinstance(value, list):
        return _kv_row(key, value)
    blocks: list[str] = []
    for i, item in enumerate(value):
        if key == "calls" and isinstance(item, dict):
            fn = item.get("function")
            name = fn.get("name") if isinstance(fn, dict) else None
            args_raw = fn.get("arguments") if isinstance(fn, dict) else None
            rows = (
                _kv_row("id", item.get("id"))
                + _kv_row("name", name)
                + _kv_row("arguments", _parse_arguments_for_display(args_raw))
            )
            blocks.append(
                f"<div class='entry-call-block'><div class='entry-call-title'>{key} {i + 1}</div>{rows}</div>"
            )
        elif isinstance(item, dict):
            rows = "".join(_kv_row(k, v) for k, v in item.items())
            blocks.append(
                f"<div class='entry-result-block'><div class='entry-result-title'>{key} {i + 1}</div>{rows}</div>"
            )
        else:
            blocks.append(
                "<div class='entry-result-block'>"
                f"<div class='entry-result-title'>{key} {i + 1}</div>"
                f"{_kv_row('value', item)}</div>"
            )
    return "".join(blocks) if blocks else _kv_row(key, value)


def _render_structured(kind: str, payload: dict[str, Any]) -> str:
    """Unified: ordered keys per kind, then each value via _structured_value (list→blocks, else kv)."""
    ordered = _ORDERED_KEYS.get(kind, list(payload.keys()))
    seen: set[str] = set()
    out: list[str] = []
    for key in ordered:
        if key not in payload:
            continue
        seen.add(key)
        out.append(_structured_value(key, payload[key]))
    for key, value in payload.items():
        if key not in seen:
            out.append(_structured_value(key, value))
    if not out:
        out.append(_kv_row("payload", "(empty)"))
    return "<div class='entry-structured'>{}</div>".format("".join(out))


def _render_log_html(snapshot: ConversationSnapshot, show_system_events: bool = False) -> str:
    context_ids = {entry.id for entry in snapshot.context_entries}
    active_anchor_id = snapshot.active_anchor.entry_id if snapshot.active_anchor else None
    rows: list[str] = []
    for entry in snapshot.entries:
        if not show_system_events and getattr(entry, "kind", "") == "event":
            continue
        is_context = entry.id in context_ids
        is_active_anchor = entry.kind == "anchor" and entry.id == active_anchor_id
        classes = ["tape-entry"]
        if is_context:
            classes.append("in-context")
        if is_active_anchor:
            classes.append("active-anchor")
        payload = getattr(entry, "payload", {})
        if not isinstance(payload, dict):
            payload = {}
        human = html.escape(_human_text(entry.kind, payload))
        structured = _render_structured(entry.kind, payload)
        raw_payload = html.escape(json.dumps(payload, ensure_ascii=False, indent=2))
        rows.append(
            f"<details class='{' '.join(classes)}' title='Entry #{entry.id}'>"
            "<summary class='entry-summary'>"
            f"<span class='entry-badge'>{_kind_label(entry.kind)}</span>"
            f"<span class='entry-text'>{human}</span>"
            "</summary>"
            f"{structured}"
            "<details class='entry-raw-block'>"
            "<summary class='entry-raw-summary'>Raw payload</summary>"
            f"<pre class='entry-raw'>{raw_payload}</pre>"
            "</details>"
            "</details>"
        )
    if not rows:
        rows.append("<div class='tape-empty'>Tape is empty. Send a message to begin.</div>")
    return "<div class='tape-list'>{}</div>".format("".join(rows))


def _context_source_label(snapshot: ConversationSnapshot, view_mode: ViewMode) -> str:
    if view_mode == "full":
        return "Full Context"
    if view_mode == "latest":
        if snapshot.active_anchor:
            return f"Latest: {snapshot.active_anchor.label}"
        return "Latest (no anchor)"
    if snapshot.active_anchor:
        return f"Anchor: {snapshot.active_anchor.label}"
    return "Anchor: not found"


def _token_health(estimated_tokens: int) -> tuple[str, str]:
    if estimated_tokens > 3000:
        return "HIGH", "ctx-high"
    if estimated_tokens > 2000:
        return "MODERATE", "ctx-moderate"
    return "OK", "ctx-ok"


def _render_context(snapshot: ConversationSnapshot, view_mode: ViewMode) -> str:
    source = html.escape(_context_source_label(snapshot, view_mode))
    status_label, status_class = _token_health(snapshot.estimated_tokens)
    progress = min(int((snapshot.estimated_tokens / 4000) * 100), 100)
    return (
        "<div class='ctx-bar'>"
        "<div class='ctx-info'>"
        f"<span class='ctx-source'>{source}</span>"
        f"<span class='ctx-stats'>{snapshot.context_entry_count} / {snapshot.total_entries} entries"
        f" &middot; ~{snapshot.estimated_tokens} tok"
        f" &middot; <b class='{status_class}'>{status_label}</b></span>"
        "</div>"
        "<div class='ctx-track'>"
        f"<div class='ctx-fill {status_class}' style='width:{progress}%'></div>"
        "</div>"
        "</div>"
    )


def _render_tape_footer(snapshot: ConversationSnapshot, view_mode: ViewMode) -> str:
    if view_mode == "full":
        left = "All entries in context"
    elif view_mode == "latest":
        left = "From latest anchor"
    else:
        left = f"From: {snapshot.active_anchor.label}" if snapshot.active_anchor else "From: anchor (missing)"
    return (
        f"**{left}** &nbsp; "
        f"{snapshot.context_entry_count} in context &middot; "
        f"{snapshot.total_entries} total &middot; "
        f"~{snapshot.estimated_tokens} tokens"
    )


def _anchor_rows(snapshot: ConversationSnapshot) -> list[list[str]]:
    active_name = snapshot.active_anchor.name if snapshot.active_anchor else ""
    rows: list[list[str]] = []
    for anchor in snapshot.anchors:
        rows.append(
            [
                "\u2713" if anchor.name == active_name else "",
                anchor.label,
                anchor.name,
                anchor.summary or "-",
            ]
        )
    return rows


# ---------------------------------------------------------------------------
# View builder (single source of truth)
# ---------------------------------------------------------------------------


def _build_view(
    view_mode: ViewMode, anchor_name: str | None, show_system_events: bool = False
) -> tuple[
    list[dict[str, str]],
    str,
    dict[str, Any],
    list[list[str]],
    str,
    str,
]:
    snapshot = get_agent().snapshot(view_mode=view_mode, anchor_name=anchor_name)
    anchor_choices = [anchor.name for anchor in snapshot.anchors]

    if view_mode == "from-anchor":
        resolved_anchor = (
            anchor_name if anchor_name in anchor_choices else (anchor_choices[-1] if anchor_choices else None)
        )
        if resolved_anchor != anchor_name:
            snapshot = get_agent().snapshot(view_mode=view_mode, anchor_name=resolved_anchor)
        anchor_update = gr.update(choices=anchor_choices, value=resolved_anchor, interactive=True)
    else:
        anchor_update = gr.update(choices=anchor_choices, value=None, interactive=False)

    return (
        snapshot.messages,
        _render_log_html(snapshot, show_system_events),
        anchor_update,
        _anchor_rows(snapshot),
        _render_tape_footer(snapshot, view_mode),
        _render_context(snapshot, view_mode),
    )


# ---------------------------------------------------------------------------
# Event handlers (each self-contained, no cascade)
# ---------------------------------------------------------------------------


def _refresh(view_mode: ViewMode, anchor_name: str | None, show_system_events: bool = False):
    chat, log, anchor_upd, anchors, footer, ctx = _build_view(view_mode, anchor_name, show_system_events)
    return chat, log, anchor_upd, anchors, footer, ctx, ""


def _send_stage1(message: str, chat_history: list[dict[str, str]] | None):
    text = message.strip()
    if not text:
        return "", chat_history or [], "", ""
    history = list(chat_history or [])
    history.append({"role": "user", "content": text})
    return "", history, "", text


def _send_stage2(
    pending_message: str,
    view_mode: ViewMode,
    anchor_name: str | None,
    show_system_events: bool,
):
    text = pending_message.strip()
    if not text:
        chat, log, anchor_upd, anchors, footer, ctx = _build_view(view_mode, anchor_name, show_system_events)
        yield chat, log, anchor_upd, anchors, footer, ctx, "", ""
        return

    state: dict[str, Any] = {"done": False, "reply": "", "error": None}

    def _worker() -> None:
        try:
            state["reply"] = get_agent().reply(text, view_mode=view_mode, anchor_name=anchor_name)
        except Exception as exc:  # pragma: no cover - defensive guard
            state["error"] = exc
        finally:
            state["done"] = True

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()

    while not bool(state["done"]):
        chat, log, anchor_upd, anchors, footer, ctx = _build_view(view_mode, anchor_name, show_system_events)
        if not chat or chat[-1].get("role") != "user" or chat[-1].get("content") != text:
            chat = [*chat, {"role": "user", "content": text}]
        yield chat, log, anchor_upd, anchors, footer, ctx, "", text
        time.sleep(0.2)

    reply = str(state.get("reply") or "")
    if state.get("error") is not None:
        reply = f"Error: {state['error']}"
    status = reply if reply.startswith("Error:") else ""
    chat, log, anchor_upd, anchors, footer, ctx = _build_view(view_mode, anchor_name, show_system_events)
    yield chat, log, anchor_upd, anchors, footer, ctx, status, ""


def _send(message: str, view_mode: ViewMode, anchor_name: str | None, show_system_events: bool = False):
    """Backward-compatible sync wrapper kept for tests."""
    last = None
    for update in _send_stage2(message, view_mode, anchor_name, show_system_events):
        last = update
    if last is None:
        chat, log, anchor_upd, anchors, footer, ctx = _build_view(view_mode, anchor_name, show_system_events)
        return "", chat, log, anchor_upd, anchors, footer, ctx, ""
    chat, log, anchor_upd, anchors, footer, ctx, status, _pending = last
    return "", chat, log, anchor_upd, anchors, footer, ctx, status


def _create_handoff(name: str, phase: str, summary: str, facts_text: str, show_system_events: bool = False):
    if not name.strip():
        chat, log, anchor_upd, anchors, footer, ctx = _build_view("latest", None, show_system_events)
        return (
            name,
            phase,
            summary,
            facts_text,
            "latest",
            chat,
            log,
            anchor_upd,
            anchors,
            footer,
            ctx,
            "Name is required.",
        )

    facts = [line.strip() for line in facts_text.splitlines() if line.strip()]
    normalized = get_agent().handoff(name=name, phase=phase, summary=summary, facts=facts)
    chat, log, anchor_upd, anchors, footer, ctx = _build_view("latest", None, show_system_events)
    return "", "", "", "", "latest", chat, log, anchor_upd, anchors, footer, ctx, f"Handoff created: {normalized}"


def _switch_view(target_mode: ViewMode, show_system_events: bool = False):
    chat, log, anchor_upd, anchors, footer, ctx = _build_view(target_mode, None, show_system_events)
    return target_mode, chat, log, anchor_upd, anchors, footer, ctx, ""


def _select_anchor_from_table(
    rows: list[list[str]],
    show_or_evt: bool | gr.SelectData,
    evt: gr.SelectData | None = None,
):
    if evt is None:
        show_system_events = False
        if not hasattr(show_or_evt, "index"):
            return _switch_view("latest", show_system_events)
        evt = show_or_evt  # type: ignore[assignment]
    else:
        show_system_events = bool(show_or_evt)
    row_index = evt.index[0] if isinstance(evt.index, tuple) else evt.index
    if not isinstance(row_index, int) or row_index < 0 or row_index >= len(rows):
        return _switch_view("latest", show_system_events)
    anchor_name = rows[row_index][2]
    if not isinstance(anchor_name, str) or not anchor_name.strip():
        return _switch_view("latest", show_system_events)
    chat, log, anchor_upd, anchors, footer, ctx = _build_view("from-anchor", anchor_name, show_system_events)
    return "from-anchor", chat, log, anchor_upd, anchors, footer, ctx, ""


# ---------------------------------------------------------------------------
# CSS (theme-aware, no hardcoded dark-mode colors)
# ---------------------------------------------------------------------------

CSS = """
/* Tape entry list */
.tape-list { display: flex; flex-direction: column; gap: 4px; max-height: 560px; overflow-y: auto; padding: 2px 0; }
.tape-entry {
  display: block;
  padding: 5px 10px; border-radius: 6px;
  border-left: 3px solid transparent;
  transition: background 0.15s;
}
.entry-summary { display: flex; align-items: center; gap: 8px; cursor: pointer; list-style: none; }
.entry-summary::-webkit-details-marker { display: none; }
.tape-entry:hover { background: color-mix(in srgb, var(--body-text-color) 6%, transparent); }
.tape-entry.in-context { border-left-color: #2ea043; background: color-mix(in srgb, #2ea043 8%, transparent); }
.tape-entry.active-anchor { border-left-color: #d29922; background: color-mix(in srgb, #d29922 8%, transparent); }
.tape-empty { padding: 20px; text-align: center; opacity: 0.5; }

/* Entry badges */
.entry-badge {
  font-size: 10px; font-weight: 600; padding: 1px 6px; border-radius: 4px;
  text-transform: uppercase; white-space: nowrap; flex-shrink: 0;
  background: color-mix(in srgb, #8b949e 18%, transparent); color: #8b949e;
}
.entry-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; opacity: 0.85; }
.entry-structured {
  margin: 8px 0 6px 0;
  padding: 8px;
  border-radius: 6px;
  background: color-mix(in srgb, var(--body-text-color) 4%, transparent);
  border: 1px solid var(--border-color-primary);
}
.entry-kv { display: grid; grid-template-columns: 120px 1fr; gap: 8px; padding: 2px 0; font-size: 12px; }
.entry-k { opacity: 0.65; font-family: monospace; }
.entry-v { white-space: pre-wrap; word-break: break-word; }
.entry-call-block, .entry-result-block {
  margin: 8px 0; padding: 8px; border-radius: 6px;
  border: 1px solid var(--border-color-primary);
  background: color-mix(in srgb, var(--body-text-color) 3%, transparent);
}
.entry-call-title, .entry-result-title {
  font-size: 11px; font-weight: 600; opacity: 0.8; margin-bottom: 6px;
}
.entry-raw-block { margin: 0 0 2px 0; }
.entry-raw-summary { cursor: pointer; font-size: 12px; opacity: 0.8; }
.entry-raw {
  margin: 6px 0 0 0;
  padding: 8px;
  border-radius: 6px;
  border: 1px solid var(--border-color-primary);
  background: color-mix(in srgb, var(--body-text-color) 2%, transparent);
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-word;
}

/* Context bar (above chatbot) */
.ctx-bar { padding: 8px 12px; border-radius: 8px; border: 1px solid var(--border-color-primary); margin-bottom: 6px; }
.ctx-info {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  flex-wrap: wrap;
}
.ctx-source { font-weight: 600; }
.ctx-stats { opacity: 0.65; }
.ctx-track {
  height: 4px;
  border-radius: 4px;
  background: var(--border-color-primary);
  margin-top: 6px;
  overflow: hidden;
}
.ctx-fill { height: 100%; border-radius: 4px; transition: width 0.3s; }
.ctx-ok { color: #2ea043; } .ctx-fill.ctx-ok { background: #2ea043; }
.ctx-moderate { color: #d29922; } .ctx-fill.ctx-moderate { background: #d29922; }
.ctx-high { color: #f85149; } .ctx-fill.ctx-high { background: #f85149; }
"""

# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

with gr.Blocks(title="Endless Context") as demo:
    gr.Markdown("## Endless Context\nAppend-only tape &middot; Handoff anchors &middot; Context assembly")

    with gr.Row():
        # ---- Left: Tape ----
        with gr.Column(scale=3):
            gr.Markdown("#### Tape")
            view_mode = gr.Radio(
                choices=["latest", "full", "from-anchor"],
                value="latest",
                label="Context view",
            )
            show_system_events = gr.Checkbox(
                value=False,
                label="Show events",
                info="Only affects tape rendering; does not affect context selection.",
            )
            anchor_selector = gr.Dropdown(
                choices=[],
                value=None,
                label="Anchor",
                info="Active when view is from-anchor.",
                interactive=False,
            )
            log_html = gr.HTML()
            tape_footer = gr.Markdown()

        # ---- Center: Conversation ----
        with gr.Column(scale=6):
            gr.Markdown("#### Conversation")
            context_indicator = gr.HTML()
            chatbot = gr.Chatbot(height=480, label="Messages")
            user_input = gr.Textbox(label="Message", placeholder="Type a message and press Enter...", lines=3)
            with gr.Row():
                send_button = gr.Button("Send", variant="primary")
                refresh_button = gr.Button("Refresh", variant="secondary")

        # ---- Right: Anchors ----
        with gr.Column(scale=3):
            gr.Markdown("#### Anchors")
            with gr.Row():
                full_tape_button = gr.Button("Full tape", size="sm")
                latest_anchor_button = gr.Button("Latest", size="sm")
            anchors_table = gr.Dataframe(
                headers=["", "Label", "Name", "Summary"],
                datatype=["str", "str", "str", "str"],
                interactive=False,
                row_count=6,
                column_count=(4, "fixed"),
                value=[],
            )
            with gr.Accordion("Create Handoff", open=False):
                handoff_name = gr.Textbox(label="Name", placeholder="e.g. impl-details")
                handoff_phase = gr.Textbox(label="Phase", placeholder="e.g. Implementation")
                handoff_summary = gr.Textbox(label="Summary")
                handoff_facts = gr.Textbox(label="Facts (one per line)", lines=3)
                handoff_button = gr.Button("Create", variant="secondary")

    status_text = gr.Markdown()
    pending_message = gr.State("")

    # ------------------------------------------------------------------
    # Shared output lists (avoids repetition)
    # ------------------------------------------------------------------
    _core = [chatbot, log_html, anchor_selector, anchors_table, tape_footer, context_indicator, status_text]

    # ------------------------------------------------------------------
    # Event wiring — uses .input (not .change) to prevent cascade
    # ------------------------------------------------------------------

    demo.load(
        fn=_refresh, inputs=[view_mode, anchor_selector, show_system_events], outputs=_core, show_progress="hidden"
    )

    # User manually switches radio / dropdown — .input fires only on direct interaction
    view_mode.input(
        fn=_refresh,
        inputs=[view_mode, anchor_selector, show_system_events],
        outputs=_core,
        show_progress="hidden",
    )
    anchor_selector.input(
        fn=_refresh,
        inputs=[view_mode, anchor_selector, show_system_events],
        outputs=_core,
        show_progress="hidden",
    )
    show_system_events.input(
        fn=_refresh,
        inputs=[view_mode, anchor_selector, show_system_events],
        outputs=_core,
        show_progress="hidden",
    )

    refresh_button.click(
        fn=_refresh,
        inputs=[view_mode, anchor_selector, show_system_events],
        outputs=_core,
        show_progress="hidden",
    )

    send_button.click(
        fn=_send_stage1,
        inputs=[user_input, chatbot],
        outputs=[user_input, chatbot, status_text, pending_message],
        queue=False,
        show_progress="hidden",
    ).then(
        fn=_send_stage2,
        inputs=[pending_message, view_mode, anchor_selector, show_system_events],
        outputs=_core + [pending_message],
        show_progress="hidden",
    )
    user_input.submit(
        fn=_send_stage1,
        inputs=[user_input, chatbot],
        outputs=[user_input, chatbot, status_text, pending_message],
        queue=False,
        show_progress="hidden",
    ).then(
        fn=_send_stage2,
        inputs=[pending_message, view_mode, anchor_selector, show_system_events],
        outputs=_core + [pending_message],
        show_progress="hidden",
    )

    handoff_button.click(
        fn=_create_handoff,
        inputs=[handoff_name, handoff_phase, handoff_summary, handoff_facts, show_system_events],
        outputs=[handoff_name, handoff_phase, handoff_summary, handoff_facts, view_mode] + _core,
        show_progress="hidden",
    )

    full_tape_button.click(
        fn=lambda show: _switch_view("full", show),
        inputs=[show_system_events],
        outputs=[view_mode] + _core,
        show_progress="hidden",
    )
    latest_anchor_button.click(
        fn=lambda show: _switch_view("latest", show),
        inputs=[show_system_events],
        outputs=[view_mode] + _core,
        show_progress="hidden",
    )
    anchors_table.select(
        fn=_select_anchor_from_table,
        inputs=[anchors_table, show_system_events],
        outputs=[view_mode] + _core,
        show_progress="hidden",
    )

if __name__ == "__main__":
    demo.launch(
        server_name=os.getenv("GRADIO_SERVER_NAME", "0.0.0.0"),
        server_port=int(os.getenv("GRADIO_SERVER_PORT", "7860")),
        theme=gr.themes.Soft(),
        css=CSS,
        show_error=True,
    )
