from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from republic.tape.entries import TapeEntry

from endless_context.agent import AnchorState, ConversationSnapshot


def _load_app_module():
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    spec = importlib.util.spec_from_file_location("app_module_for_test", app_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load app.py module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


app_module = _load_app_module()


def _build_snapshot(view_mode: str, anchor_name: str | None) -> ConversationSnapshot:
    entries = [
        TapeEntry(1, "message", {"role": "user", "content": "hi"}, {}),
        TapeEntry(2, "anchor", {"name": "handoff:phase-1", "state": {"phase": "Phase 1"}}, {}),
        TapeEntry(3, "message", {"role": "assistant", "content": "ok"}, {}),
    ]
    anchors = [
        AnchorState(
            entry_id=2,
            name="handoff:phase-1",
            label="Phase 1",
            summary="checkpoint",
            facts=["f1"],
            created_at="2026-02-10T00:00:00",
        )
    ]
    if view_mode == "full":
        active_anchor = None
        context_entries = entries
    elif view_mode == "from-anchor" and anchor_name == "handoff:phase-1":
        active_anchor = anchors[0]
        context_entries = [entries[2]]
    elif view_mode == "latest":
        active_anchor = anchors[0]
        context_entries = [entries[2]]
    else:
        active_anchor = None
        context_entries = entries
    return ConversationSnapshot(
        tape_name="t1",
        entries=entries,
        anchors=anchors,
        active_anchor=active_anchor,
        context_entries=context_entries,
        estimated_tokens=42,
    )


class FakeAgent:
    def __init__(self) -> None:
        self.snapshot_calls: list[tuple[str, str | None]] = []
        self.handoff_calls: list[dict[str, Any]] = []
        self.reply_calls: list[dict[str, Any]] = []

    def snapshot(self, *, view_mode: str = "latest", anchor_name: str | None = None) -> ConversationSnapshot:
        self.snapshot_calls.append((view_mode, anchor_name))
        return _build_snapshot(view_mode, anchor_name)

    def handoff(self, name: str, *, phase: str = "", summary: str = "", facts: list[str] | None = None) -> str:
        self.handoff_calls.append({"name": name, "phase": phase, "summary": summary, "facts": list(facts or [])})
        return "handoff:phase-1"

    def reply(self, message: str, *, view_mode: str = "latest", anchor_name: str | None = None) -> str:
        self.reply_calls.append({"message": message, "view_mode": view_mode, "anchor_name": anchor_name})
        if message == "boom":
            return "Error: simulated"
        return "ok"


def test_build_view_from_anchor_fallbacks_to_latest_anchor(monkeypatch):
    fake_agent = FakeAgent()
    monkeypatch.setattr(app_module, "get_agent", lambda: fake_agent)

    _, _, anchor_update, _, _, _ = app_module._build_view("from-anchor", "handoff:missing")

    assert anchor_update["interactive"] is True
    assert anchor_update["value"] == "handoff:phase-1"
    assert fake_agent.snapshot_calls[0] == ("from-anchor", "handoff:missing")
    assert fake_agent.snapshot_calls[1] == ("from-anchor", "handoff:phase-1")


def test_send_with_error_sets_status(monkeypatch):
    fake_agent = FakeAgent()
    monkeypatch.setattr(app_module, "get_agent", lambda: fake_agent)

    _, _, _, _, _, _, _, status = app_module._send("boom", "latest", None)

    assert status == "Error: simulated"
    assert fake_agent.reply_calls[-1]["message"] == "boom"


def test_create_handoff_success_resets_fields(monkeypatch):
    fake_agent = FakeAgent()
    monkeypatch.setattr(app_module, "get_agent", lambda: fake_agent)

    result = app_module._create_handoff(
        "phase-1",
        "Phase 1",
        "Checkpoint",
        "fact-a\nfact-b\n",
    )

    assert result[0] == ""
    assert result[4] == "latest"
    assert "Handoff created: handoff:phase-1" in result[-1]
    assert fake_agent.handoff_calls
    assert fake_agent.handoff_calls[-1]["facts"] == ["fact-a", "fact-b"]


def test_render_log_html_marks_context_and_active_anchor():
    snapshot = _build_snapshot("latest", None)

    content = app_module._render_log_html(snapshot)

    assert "in-context" in content
    assert "active-anchor" in content


def test_send_with_blank_message_does_not_call_reply(monkeypatch):
    fake_agent = FakeAgent()
    monkeypatch.setattr(app_module, "get_agent", lambda: fake_agent)

    _, _, _, _, _, _, _, status = app_module._send("   ", "latest", None)

    assert status == ""
    assert fake_agent.reply_calls == []


def test_ui_contains_bottom_message_input_textbox():
    components = app_module.demo.config.get("components", [])
    message_boxes = [
        component
        for component in components
        if component.get("type") == "textbox" and component.get("props", {}).get("label") == "Message"
    ]

    assert message_boxes, "Message input textbox should exist in the conversation area."


def test_context_indicator_contains_status_and_progress():
    snapshot = ConversationSnapshot(
        tape_name="t1",
        entries=[],
        anchors=[],
        active_anchor=None,
        context_entries=[],
        estimated_tokens=3200,
    )
    content = app_module._render_context(snapshot, "full")

    assert "HIGH" in content
    assert "ctx-high" in content
    assert "ctx-fill" in content


class _FakeSelectEvent:
    def __init__(self, index):
        self.index = index


def test_switch_view_returns_full_mode(monkeypatch):
    fake_agent = FakeAgent()
    monkeypatch.setattr(app_module, "get_agent", lambda: fake_agent)

    mode, *_ = app_module._switch_view("full")

    assert mode == "full"


def test_select_anchor_from_table_switches_to_from_anchor(monkeypatch):
    fake_agent = FakeAgent()
    monkeypatch.setattr(app_module, "get_agent", lambda: fake_agent)

    rows = [["", "Phase 1", "handoff:phase-1", "checkpoint"]]
    mode, _, _, anchor_update, *_ = app_module._select_anchor_from_table(rows, _FakeSelectEvent((0, 2)))

    assert mode == "from-anchor"
    assert anchor_update["value"] == "handoff:phase-1"


def test_context_source_label_matches_design_modes():
    latest_snapshot = _build_snapshot("latest", None)
    full_snapshot = _build_snapshot("full", None)
    from_anchor_snapshot = _build_snapshot("from-anchor", "handoff:phase-1")

    assert app_module._context_source_label(latest_snapshot, "latest") == "Latest: Phase 1"
    assert app_module._context_source_label(full_snapshot, "full") == "Full Context"
    assert app_module._context_source_label(from_anchor_snapshot, "from-anchor") == "Anchor: Phase 1"


def test_refresh_full_mode_disables_anchor_selector(monkeypatch):
    fake_agent = FakeAgent()
    monkeypatch.setattr(app_module, "get_agent", lambda: fake_agent)

    _, _, anchor_update, _, tape_footer, _, _ = app_module._refresh("full", None)

    assert anchor_update["interactive"] is False
    assert anchor_update["value"] is None
    assert "All entries in context" in tape_footer


def test_select_anchor_from_table_invalid_index_falls_back_to_latest(monkeypatch):
    fake_agent = FakeAgent()
    monkeypatch.setattr(app_module, "get_agent", lambda: fake_agent)

    rows = [["", "Phase 1", "handoff:phase-1", "checkpoint"]]
    mode, _, _, anchor_update, *_ = app_module._select_anchor_from_table(rows, _FakeSelectEvent((99, 0)))

    assert mode == "latest"
    assert anchor_update["interactive"] is False


def test_ui_context_view_control_has_expected_choices_and_default():
    components = app_module.demo.config.get("components", [])
    radios = [
        component
        for component in components
        if component.get("type") == "radio" and component.get("props", {}).get("label") == "Context view"
    ]

    assert radios, "Context view radio should exist in the tape panel."
    radio = radios[0]
    choices = [value for value, _label in radio.get("props", {}).get("choices", [])]
    assert choices == ["latest", "full", "from-anchor"]
    assert radio.get("props", {}).get("value") == "latest"
