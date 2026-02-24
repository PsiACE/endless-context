from __future__ import annotations

import html
import os
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

_KIND_BADGE: dict[str, tuple[str, str]] = {
    "message": ("MSG", "badge-message"),
    "anchor": ("ANCHOR", "badge-anchor"),
    "event": ("EVENT", "badge-event"),
    "tool_call": ("TOOL", "badge-tool"),
    "tool_result": ("RESULT", "badge-tool"),
    "error": ("ERROR", "badge-error"),
    "system": ("SYS", "badge-event"),
}


def _summarize_entry_payload(kind: str, payload: dict[str, Any]) -> str:
    if kind == "message":
        role = payload.get("role", "unknown")
        content = str(payload.get("content", "")).strip().replace("\n", " ")
        return f"{role}: {content[:120]}"
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
    return str(payload)[:120]


def _render_log_html(snapshot: ConversationSnapshot) -> str:
    context_ids = {entry.id for entry in snapshot.context_entries}
    active_anchor_id = snapshot.active_anchor.entry_id if snapshot.active_anchor else None
    rows: list[str] = []
    for entry in snapshot.entries:
        is_context = entry.id in context_ids
        is_active_anchor = entry.kind == "anchor" and entry.id == active_anchor_id
        classes = ["tape-entry"]
        if is_context:
            classes.append("in-context")
        if is_active_anchor:
            classes.append("active-anchor")
        badge_label, badge_class = _KIND_BADGE.get(entry.kind, (entry.kind.upper()[:6], "badge-event"))
        summary = html.escape(_summarize_entry_payload(entry.kind, entry.payload))
        rows.append(
            f"<div class='{' '.join(classes)}' title='Entry #{entry.id}'>"
            f"<span class='entry-badge {badge_class}'>{badge_label}</span>"
            f"<span class='entry-text'>{summary}</span>"
            "</div>"
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
    view_mode: ViewMode, anchor_name: str | None
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
        _render_log_html(snapshot),
        anchor_update,
        _anchor_rows(snapshot),
        _render_tape_footer(snapshot, view_mode),
        _render_context(snapshot, view_mode),
    )


# ---------------------------------------------------------------------------
# Event handlers (each self-contained, no cascade)
# ---------------------------------------------------------------------------


def _refresh(view_mode: ViewMode, anchor_name: str | None):
    chat, log, anchor_upd, anchors, footer, ctx = _build_view(view_mode, anchor_name)
    return chat, log, anchor_upd, anchors, footer, ctx, ""


def _send_stage1(message: str, chat_history: list[dict[str, str]] | None):
    text = message.strip()
    if not text:
        return "", chat_history or [], "", ""
    history = list(chat_history or [])
    history.append({"role": "user", "content": text})
    return "", history, "", text


def _send_stage2(pending_message: str, view_mode: ViewMode, anchor_name: str | None):
    status = ""
    text = pending_message.strip()
    reply = ""
    if text:
        reply = get_agent().reply(text, view_mode=view_mode, anchor_name=anchor_name)
        if reply.startswith("Error:"):
            status = reply
    chat, log, anchor_upd, anchors, footer, ctx = _build_view(view_mode, anchor_name)
    # Comma commands are usually persisted as events instead of assistant messages.
    # Keep them visible in chat to avoid confusing "disappearing" interactions.
    if text.startswith(","):
        if not chat or chat[-1].get("role") != "user" or chat[-1].get("content") != text:
            chat.append({"role": "user", "content": text})
        if reply and not reply.startswith("Error:"):
            chat.append({"role": "assistant", "content": reply})
    return chat, log, anchor_upd, anchors, footer, ctx, status, ""


def _create_handoff(name: str, phase: str, summary: str, facts_text: str):
    if not name.strip():
        chat, log, anchor_upd, anchors, footer, ctx = _build_view("latest", None)
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
    chat, log, anchor_upd, anchors, footer, ctx = _build_view("latest", None)
    return "", "", "", "", "latest", chat, log, anchor_upd, anchors, footer, ctx, f"Handoff created: {normalized}"


def _switch_view(target_mode: ViewMode):
    chat, log, anchor_upd, anchors, footer, ctx = _build_view(target_mode, None)
    return target_mode, chat, log, anchor_upd, anchors, footer, ctx, ""


def _select_anchor_from_table(rows: list[list[str]], evt: gr.SelectData):
    row_index = evt.index[0] if isinstance(evt.index, tuple) else evt.index
    if not isinstance(row_index, int) or row_index < 0 or row_index >= len(rows):
        return _switch_view("latest")
    anchor_name = rows[row_index][2]
    if not isinstance(anchor_name, str) or not anchor_name.strip():
        return _switch_view("latest")
    chat, log, anchor_upd, anchors, footer, ctx = _build_view("from-anchor", anchor_name)
    return "from-anchor", chat, log, anchor_upd, anchors, footer, ctx, ""


# ---------------------------------------------------------------------------
# CSS (theme-aware, no hardcoded dark-mode colors)
# ---------------------------------------------------------------------------

CSS = """
/* Tape entry list */
.tape-list { display: flex; flex-direction: column; gap: 4px; max-height: 560px; overflow-y: auto; padding: 2px 0; }
.tape-entry {
  display: flex; align-items: center; gap: 8px;
  padding: 5px 10px; border-radius: 6px;
  border-left: 3px solid transparent;
  transition: background 0.15s;
}
.tape-entry:hover { background: color-mix(in srgb, var(--body-text-color) 6%, transparent); }
.tape-entry.in-context { border-left-color: #2ea043; background: color-mix(in srgb, #2ea043 8%, transparent); }
.tape-entry.active-anchor { border-left-color: #d29922; background: color-mix(in srgb, #d29922 8%, transparent); }
.tape-empty { padding: 20px; text-align: center; opacity: 0.5; }

/* Entry badges */
.entry-badge {
  font-size: 10px; font-weight: 600; padding: 1px 6px; border-radius: 4px;
  text-transform: uppercase; white-space: nowrap; flex-shrink: 0;
}
.badge-message { background: color-mix(in srgb, #58a6ff 18%, transparent); color: #58a6ff; }
.badge-anchor  { background: color-mix(in srgb, #d29922 18%, transparent); color: #d29922; }
.badge-event   { background: color-mix(in srgb, #8b949e 18%, transparent); color: #8b949e; }
.badge-tool    { background: color-mix(in srgb, #a371f7 18%, transparent); color: #a371f7; }
.badge-error   { background: color-mix(in srgb, #f85149 18%, transparent); color: #f85149; }
.entry-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; opacity: 0.85; }

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

    demo.load(fn=_refresh, inputs=[view_mode, anchor_selector], outputs=_core, show_progress="hidden")

    # User manually switches radio / dropdown — .input fires only on direct interaction
    view_mode.input(fn=_refresh, inputs=[view_mode, anchor_selector], outputs=_core, show_progress="hidden")
    anchor_selector.input(fn=_refresh, inputs=[view_mode, anchor_selector], outputs=_core, show_progress="hidden")

    refresh_button.click(fn=_refresh, inputs=[view_mode, anchor_selector], outputs=_core, show_progress="hidden")

    send_button.click(
        fn=_send_stage1,
        inputs=[user_input, chatbot],
        outputs=[user_input, chatbot, status_text, pending_message],
        queue=False,
        show_progress="hidden",
    ).then(
        fn=_send_stage2,
        inputs=[pending_message, view_mode, anchor_selector],
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
        inputs=[pending_message, view_mode, anchor_selector],
        outputs=_core + [pending_message],
        show_progress="hidden",
    )

    handoff_button.click(
        fn=_create_handoff,
        inputs=[handoff_name, handoff_phase, handoff_summary, handoff_facts],
        outputs=[handoff_name, handoff_phase, handoff_summary, handoff_facts, view_mode] + _core,
        show_progress="hidden",
    )

    full_tape_button.click(fn=lambda: _switch_view("full"), outputs=[view_mode] + _core, show_progress="hidden")
    latest_anchor_button.click(fn=lambda: _switch_view("latest"), outputs=[view_mode] + _core, show_progress="hidden")
    anchors_table.select(
        fn=_select_anchor_from_table,
        inputs=[anchors_table],
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
