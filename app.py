from __future__ import annotations

import html
import os
from typing import Any

import gradio as gr

from endless_context.agent import ConversationSnapshot, SimpleAgent, ViewMode

_agent: SimpleAgent | None = None


def get_agent() -> SimpleAgent:
    global _agent
    if _agent is None:
        _agent = SimpleAgent()
    return _agent


def _summarize_entry_payload(kind: str, payload: dict[str, Any]) -> str:
    if kind == "message":
        role = payload.get("role", "unknown")
        content = str(payload.get("content", "")).strip().replace("\n", " ")
        return f"{role}: {content[:140]}"
    if kind == "anchor":
        name = str(payload.get("name", ""))
        state = payload.get("state")
        if isinstance(state, dict):
            phase = state.get("phase")
            if isinstance(phase, str) and phase.strip():
                return f"{name} ({phase.strip()})"
        return name
    if kind == "tool_call":
        return "tool call"
    if kind == "tool_result":
        return "tool result"
    if kind == "event":
        return str(payload.get("name", "event"))
    if kind == "error":
        return str(payload.get("message", "error"))
    return str(payload)[:140]


def _render_log_html(snapshot: ConversationSnapshot) -> str:
    context_ids = {entry.id for entry in snapshot.context_entries}
    active_anchor_id = snapshot.active_anchor.entry_id if snapshot.active_anchor else None
    rows: list[str] = []
    for entry in snapshot.entries:
        is_context = entry.id in context_ids
        is_active_anchor = entry.kind == "anchor" and entry.id == active_anchor_id
        classes = ["log-row"]
        if is_context:
            classes.append("in-context")
        if is_active_anchor:
            classes.append("active-anchor")
        kind = html.escape(entry.kind)
        summary = html.escape(_summarize_entry_payload(entry.kind, entry.payload))
        rows.append(
            "<div class='{classes}'>"
            "<span class='entry-id'>#{entry_id}</span>"
            "<span class='entry-kind'>{kind}</span>"
            "<span class='entry-summary'>{summary}</span>"
            "</div>".format(
                classes=" ".join(classes),
                entry_id=entry.id,
                kind=kind,
                summary=summary,
            )
        )
    if not rows:
        rows.append("<div class='log-empty'>Tape is empty. Send a message to append facts.</div>")
    return "<div class='log-container'>{}</div>".format("".join(rows))


def _context_source_label(snapshot: ConversationSnapshot, view_mode: ViewMode) -> str:
    if view_mode == "full":
        return "Full Context"
    if view_mode == "latest":
        if snapshot.active_anchor:
            return f"From Latest: {snapshot.active_anchor.label}"
        return "Latest Anchor (none)"
    if snapshot.active_anchor:
        return f"From Anchor: {snapshot.active_anchor.label}"
    return "From Anchor: not found"


def _token_health(estimated_tokens: int) -> tuple[str, str]:
    if estimated_tokens > 3000:
        return "HIGH", "token-high"
    if estimated_tokens > 2000:
        return "MODERATE", "token-moderate"
    return "OPTIMAL", "token-optimal"


def _render_context(snapshot: ConversationSnapshot, view_mode: ViewMode) -> str:
    source = _context_source_label(snapshot, view_mode)
    status_label, status_class = _token_health(snapshot.estimated_tokens)
    progress = min(int((snapshot.estimated_tokens / 4000) * 100), 100)
    return (
        "<div class='context-indicator'>"
        "<div class='context-main'>"
        f"<span class='context-source'>{html.escape(source)}</span>"
        f"<span class='context-count'>{snapshot.context_entry_count} in context / {snapshot.total_entries} total</span>"
        "</div>"
        "<div class='context-main'>"
        f"<span class='context-anchors'>Anchors: {len(snapshot.anchors)}</span>"
        f"<span class='token-pill {status_class}'>~{snapshot.estimated_tokens} tokens · {status_label}</span>"
        "</div>"
        "<div class='context-bar-track'>"
        f"<div class='context-bar-fill {status_class}' style='width:{progress}%'></div>"
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
        f"**{left}**  \n"
        f"{snapshot.context_entry_count} in context · "
        f"{snapshot.total_entries} total · "
        f"~{snapshot.estimated_tokens} tokens"
    )


def _render_chat_header(snapshot: ConversationSnapshot, view_mode: ViewMode) -> str:
    source = _context_source_label(snapshot, view_mode)
    return (
        "<div class='chat-header'>"
        "<div class='chat-header-left'>"
        "<span class='chat-title'>Conversation</span>"
        f"<span class='chat-source'>{html.escape(source)}</span>"
        "</div>"
        "<div class='chat-header-right'>"
        "<span>Recording</span>"
        "<span class='record-dot'></span>"
        "</div>"
        "</div>"
    )


def _anchor_rows(snapshot: ConversationSnapshot) -> list[list[str]]:
    active_name = snapshot.active_anchor.name if snapshot.active_anchor else ""
    rows: list[list[str]] = []
    for anchor in snapshot.anchors:
        rows.append(
            [
                "yes" if anchor.name == active_name else "",
                anchor.label,
                anchor.name,
                str(len(anchor.facts)),
                anchor.summary,
            ]
        )
    return rows


def _build_view(view_mode: ViewMode, anchor_name: str | None) -> tuple[
    list[dict[str, str]],
    str,
    dict[str, Any],
    list[list[str]],
    str,
    str,
    str,
]:
    snapshot = get_agent().snapshot(view_mode=view_mode, anchor_name=anchor_name)
    anchor_choices = [anchor.name for anchor in snapshot.anchors]

    if view_mode == "from-anchor":
        resolved_anchor = (
            anchor_name
            if anchor_name in anchor_choices
            else (anchor_choices[-1] if anchor_choices else None)
        )
        if resolved_anchor != anchor_name:
            snapshot = get_agent().snapshot(view_mode=view_mode, anchor_name=resolved_anchor)
        anchor_update = gr.update(choices=anchor_choices, value=resolved_anchor, interactive=True)
    else:
        anchor_update = gr.update(choices=anchor_choices, value=None, interactive=False)

    return (
        snapshot.messages,
        _render_log_html(snapshot),
        anchor_update,
        _anchor_rows(snapshot),
        _render_tape_footer(snapshot, view_mode),
        _render_chat_header(snapshot, view_mode),
        _render_context(snapshot, view_mode),
    )


def _refresh(view_mode: ViewMode, anchor_name: str | None):
    chat, log_html, anchor_update, anchors, tape_footer, chat_header, context = _build_view(view_mode, anchor_name)
    return chat, log_html, anchor_update, anchors, tape_footer, chat_header, context, ""


def _send(message: str, view_mode: ViewMode, anchor_name: str | None):
    status = ""
    if message.strip():
        reply = get_agent().reply(message, view_mode=view_mode, anchor_name=anchor_name)
        if reply.startswith("Error:"):
            status = reply
    chat, log_html, anchor_update, anchors, tape_footer, chat_header, context = _build_view(view_mode, anchor_name)
    return "", chat, log_html, anchor_update, anchors, tape_footer, chat_header, context, status


def _create_handoff(
    name: str,
    phase: str,
    summary: str,
    facts_text: str,
):
    if not name.strip():
        chat, log_html, anchor_update, anchors, tape_footer, chat_header, context = _build_view("latest", None)
        return (
            name,
            phase,
            summary,
            facts_text,
            "latest",
            chat,
            log_html,
            anchor_update,
            anchors,
            tape_footer,
            chat_header,
            context,
            "Error: handoff name cannot be empty.",
        )

    facts = [line.strip() for line in facts_text.splitlines() if line.strip()]
    normalized = get_agent().handoff(
        name=name,
        phase=phase,
        summary=summary,
        facts=facts,
    )

    chat, log_html, anchor_update, anchors, tape_footer, chat_header, context = _build_view("latest", None)
    return (
        "",
        "",
        "",
        "",
        "latest",
        chat,
        log_html,
        anchor_update,
        anchors,
        tape_footer,
        chat_header,
        context,
        f"Handoff created: {normalized}",
    )


def _switch_view_mode(target_mode: ViewMode):
    chat, log_html, anchor_update, anchors, tape_footer, chat_header, context = _build_view(target_mode, None)
    return target_mode, chat, log_html, anchor_update, anchors, tape_footer, chat_header, context, ""


def _select_anchor_from_table(rows: list[list[str]], evt: gr.SelectData):
    row_index = evt.index[0] if isinstance(evt.index, tuple) else evt.index
    if not isinstance(row_index, int):
        return _switch_view_mode("latest")
    if row_index < 0 or row_index >= len(rows):
        return _switch_view_mode("latest")
    anchor_name = rows[row_index][2]
    if not isinstance(anchor_name, str) or not anchor_name.strip():
        return _switch_view_mode("latest")

    chat, log_html, anchor_update, anchors, tape_footer, chat_header, context = _build_view(
        "from-anchor",
        anchor_name,
    )
    return "from-anchor", chat, log_html, anchor_update, anchors, tape_footer, chat_header, context, ""


CSS = """
.app-shell { background: #0d1117; color: #e6edf3; font-family: 'JetBrains Mono', monospace; }
.log-container { display: flex; flex-direction: column; gap: 6px; max-height: 620px; overflow: auto; }
.log-row {
  display: grid;
  grid-template-columns: 74px 98px 1fr;
  gap: 8px;
  padding: 7px 10px;
  border: 1px solid #2b303b;
  border-radius: 8px;
  background: #121a24;
}
.log-row.in-context { border-color: #2ea043; background: #0f2a1f; }
.log-row.active-anchor { border-color: #d29922; background: #2a2110; }
.entry-id { color: #7d8590; }
.entry-kind { color: #58a6ff; text-transform: uppercase; font-size: 11px; }
.entry-summary { color: #c9d1d9; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.log-empty { color: #8b949e; border: 1px dashed #30363d; border-radius: 8px; padding: 14px; }
.chat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border: 1px solid #2b303b;
  border-radius: 8px;
  padding: 8px 10px;
  margin-bottom: 8px;
  background: #121a24;
}
.chat-header-left { display: flex; align-items: center; gap: 8px; }
.chat-title { font-weight: 600; }
.chat-source { color: #d29922; font-size: 12px; }
.chat-header-right { display: flex; align-items: center; gap: 6px; color: #8b949e; font-size: 12px; }
.record-dot { width: 8px; height: 8px; border-radius: 50%; background: #2ea043; display: inline-block; }
.context-indicator { border: 1px solid #2b303b; border-radius: 8px; padding: 10px; background: #121a24; }
.context-main { display: flex; justify-content: space-between; gap: 10px; margin-bottom: 6px; font-size: 12px; }
.context-source { color: #58a6ff; }
.context-count, .context-anchors { color: #8b949e; }
.token-pill { font-weight: 600; padding: 2px 8px; border-radius: 999px; }
.context-bar-track { height: 6px; width: 100%; border-radius: 999px; background: #30363d; overflow: hidden; }
.context-bar-fill { height: 100%; border-radius: 999px; }
.token-optimal { color: #2ea043; background-color: rgba(46, 160, 67, 0.12); }
.token-moderate { color: #d29922; background-color: rgba(210, 153, 34, 0.12); }
.token-high { color: #f85149; background-color: rgba(248, 81, 73, 0.12); }
.context-bar-fill.token-optimal { background: #2ea043; }
.context-bar-fill.token-moderate { background: #d29922; }
.context-bar-fill.token-high { background: #f85149; }
"""

with gr.Blocks(title="Punch Tape Agent") as demo:
    gr.Markdown(
        """
        # Punch Tape Console
        Append-only conversation tape with handoff anchors and context slicing.
        """,
        elem_classes=["app-shell"],
    )
    gr.Markdown("`Append-only Tape` | `Handoff Anchors` | `Context Assembly`")

    with gr.Row():
        with gr.Column(scale=4):
            gr.Markdown("### Tape")
            view_mode = gr.Radio(
                choices=["latest", "full", "from-anchor"],
                value="latest",
                label="Context View",
            )
            anchor_selector = gr.Dropdown(
                choices=[],
                value=None,
                label="Anchor",
                info="Enabled when Context View is from-anchor.",
                interactive=False,
            )
            log_html = gr.HTML(label="Tape Entries")
            tape_footer = gr.Markdown()

        with gr.Column(scale=6):
            gr.Markdown("### Conversation")
            chat_header = gr.HTML()
            chatbot = gr.Chatbot(height=560, label="Messages")
            user_input = gr.Textbox(
                label="Message",
                placeholder="Type message and press Enter...",
                lines=4,
            )
            with gr.Row():
                send_button = gr.Button("Send", variant="primary")
                refresh_button = gr.Button("Refresh")

        with gr.Column(scale=4):
            gr.Markdown("### Anchors")
            with gr.Row():
                full_tape_button = gr.Button("Use Full Tape")
                latest_anchor_button = gr.Button("Use Latest")
            anchors_table = gr.Dataframe(
                headers=["active", "label", "name", "facts", "summary"],
                datatype=["str", "str", "str", "str", "str"],
                interactive=True,
                row_count=8,
                column_count=(5, "fixed"),
                value=[],
            )
            gr.Markdown("#### Create Handoff")
            handoff_name = gr.Textbox(label="Name", placeholder="e.g. impl-details")
            handoff_phase = gr.Textbox(label="Phase", placeholder="e.g. Implementation Details")
            handoff_summary = gr.Textbox(label="Summary")
            handoff_facts = gr.Textbox(label="Facts (one per line)", lines=4)
            handoff_button = gr.Button("Create Handoff", variant="secondary")

    context_indicator = gr.Markdown()
    status_text = gr.Markdown()

    demo.load(
        fn=_refresh,
        inputs=[view_mode, anchor_selector],
        outputs=[
            chatbot,
            log_html,
            anchor_selector,
            anchors_table,
            tape_footer,
            chat_header,
            context_indicator,
            status_text,
        ],
    )

    view_mode.change(
        fn=_refresh,
        inputs=[view_mode, anchor_selector],
        outputs=[
            chatbot,
            log_html,
            anchor_selector,
            anchors_table,
            tape_footer,
            chat_header,
            context_indicator,
            status_text,
        ],
    )
    anchor_selector.change(
        fn=_refresh,
        inputs=[view_mode, anchor_selector],
        outputs=[
            chatbot,
            log_html,
            anchor_selector,
            anchors_table,
            tape_footer,
            chat_header,
            context_indicator,
            status_text,
        ],
    )
    refresh_button.click(
        fn=_refresh,
        inputs=[view_mode, anchor_selector],
        outputs=[
            chatbot,
            log_html,
            anchor_selector,
            anchors_table,
            tape_footer,
            chat_header,
            context_indicator,
            status_text,
        ],
    )

    send_button.click(
        fn=_send,
        inputs=[user_input, view_mode, anchor_selector],
        outputs=[
            user_input,
            chatbot,
            log_html,
            anchor_selector,
            anchors_table,
            tape_footer,
            chat_header,
            context_indicator,
            status_text,
        ],
    )
    user_input.submit(
        fn=_send,
        inputs=[user_input, view_mode, anchor_selector],
        outputs=[
            user_input,
            chatbot,
            log_html,
            anchor_selector,
            anchors_table,
            tape_footer,
            chat_header,
            context_indicator,
            status_text,
        ],
    )

    handoff_button.click(
        fn=_create_handoff,
        inputs=[handoff_name, handoff_phase, handoff_summary, handoff_facts],
        outputs=[
            handoff_name,
            handoff_phase,
            handoff_summary,
            handoff_facts,
            view_mode,
            chatbot,
            log_html,
            anchor_selector,
            anchors_table,
            tape_footer,
            chat_header,
            context_indicator,
            status_text,
        ],
    )

    full_tape_button.click(
        fn=lambda: _switch_view_mode("full"),
        outputs=[
            view_mode,
            chatbot,
            log_html,
            anchor_selector,
            anchors_table,
            tape_footer,
            chat_header,
            context_indicator,
            status_text,
        ],
    )
    latest_anchor_button.click(
        fn=lambda: _switch_view_mode("latest"),
        outputs=[
            view_mode,
            chatbot,
            log_html,
            anchor_selector,
            anchors_table,
            tape_footer,
            chat_header,
            context_indicator,
            status_text,
        ],
    )
    anchors_table.select(
        fn=_select_anchor_from_table,
        inputs=[anchors_table],
        outputs=[
            view_mode,
            chatbot,
            log_html,
            anchor_selector,
            anchors_table,
            tape_footer,
            chat_header,
            context_indicator,
            status_text,
        ],
    )

if __name__ == "__main__":
    demo.launch(
        server_name=os.getenv("GRADIO_SERVER_NAME", "0.0.0.0"),
        server_port=int(os.getenv("GRADIO_SERVER_PORT", "7860")),
        theme=gr.themes.Base(),
        css=CSS,
    )
