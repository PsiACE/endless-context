from __future__ import annotations

from typing import Any

from republic.core.errors import ErrorKind
from republic.core.results import ErrorPayload, StructuredOutput
from republic.tape.entries import TapeEntry

from endless_context.agent import SimpleAgent


class FakeTape:
    def __init__(self, entries: list[TapeEntry] | None = None) -> None:
        self.entries = list(entries or [])

    def read_entries(self) -> list[TapeEntry]:
        return [entry.copy() for entry in self.entries]

    def handoff(self, name: str, *, state: dict[str, Any] | None = None, **meta: Any) -> list[TapeEntry]:
        anchor = TapeEntry(
            id=len(self.entries) + 1,
            kind="anchor",
            payload={"name": name, "state": state or {}},
            meta={},
        )
        event = TapeEntry(
            id=len(self.entries) + 2,
            kind="event",
            payload={"name": "handoff", "data": {"name": name}},
            meta={},
        )
        self.entries.extend([anchor, event])
        return [anchor, event]

    def reset(self) -> None:
        self.entries.clear()


class FakeLLM:
    def __init__(self, tape: FakeTape) -> None:
        self._tape = tape
        self.chat_calls: list[dict[str, Any]] = []

    def tape(self, _name: str) -> FakeTape:
        return self._tape

    def chat(self, **kwargs: Any) -> StructuredOutput:
        self.chat_calls.append(kwargs)
        prompt = kwargs["prompt"]
        self._tape.entries.append(
            TapeEntry(
                id=len(self._tape.entries) + 1,
                kind="message",
                payload={"role": "user", "content": prompt},
                meta={},
            )
        )
        self._tape.entries.append(
            TapeEntry(
                id=len(self._tape.entries) + 1,
                kind="message",
                payload={"role": "assistant", "content": "ok"},
                meta={},
            )
        )
        return StructuredOutput("ok", None)


class FakeErrorLLM(FakeLLM):
    def chat(self, **kwargs: Any) -> StructuredOutput:
        self.chat_calls.append(kwargs)
        return StructuredOutput(None, ErrorPayload(ErrorKind.TEMPORARY, "upstream unavailable"))


def test_snapshot_latest_uses_last_anchor():
    tape = FakeTape(
        [
            TapeEntry(1, "message", {"role": "user", "content": "a"}, {}),
            TapeEntry(2, "anchor", {"name": "handoff:first", "state": {"phase": "First"}}, {}),
            TapeEntry(3, "message", {"role": "assistant", "content": "b"}, {}),
            TapeEntry(4, "anchor", {"name": "handoff:second", "state": {"phase": "Second"}}, {}),
            TapeEntry(5, "message", {"role": "user", "content": "c"}, {}),
        ]
    )
    agent = SimpleAgent(llm=FakeLLM(tape), tape_name="t1")

    snapshot = agent.snapshot(view_mode="latest")

    assert snapshot.active_anchor is not None
    assert snapshot.active_anchor.name == "handoff:second"
    assert [entry.id for entry in snapshot.context_entries] == [5]


def test_reply_passes_from_anchor_context():
    tape = FakeTape(
        [
            TapeEntry(1, "anchor", {"name": "handoff:phase-a", "state": {"phase": "Phase A"}}, {}),
            TapeEntry(2, "message", {"role": "assistant", "content": "seed"}, {}),
        ]
    )
    llm = FakeLLM(tape)
    agent = SimpleAgent(llm=llm, tape_name="t1")

    reply = agent.reply("hello", view_mode="from-anchor", anchor_name="handoff:phase-a")

    assert reply == "ok"
    assert llm.chat_calls
    context = llm.chat_calls[-1]["context"]
    assert context.anchor == "handoff:phase-a"


def test_reply_returns_error_prefix_when_llm_failed():
    tape = FakeTape()
    agent = SimpleAgent(llm=FakeErrorLLM(tape), tape_name="t1")

    reply = agent.reply("hello")

    assert reply == "Error: upstream unavailable"


def test_handoff_normalizes_name_and_appends():
    tape = FakeTape()
    agent = SimpleAgent(llm=FakeLLM(tape), tape_name="t1")

    anchor_name = agent.handoff(
        "Implementation Details",
        phase="Implementation",
        summary="Checkpoint",
        facts=["A", "B"],
    )

    assert anchor_name == "handoff:implementation-details"
    anchors = [entry for entry in tape.entries if entry.kind == "anchor"]
    assert len(anchors) == 1
    assert anchors[0].payload["name"] == "handoff:implementation-details"


def test_context_window_progression_after_handoff():
    tape = FakeTape()
    llm = FakeLLM(tape)
    agent = SimpleAgent(llm=llm, tape_name="t1")

    agent.reply("first")
    before = agent.snapshot(view_mode="latest")
    assert before.active_anchor is not None
    assert before.active_anchor.name == "handoff:auto-bootstrap"
    assert [entry.kind for entry in before.context_entries] == ["event", "message", "message"]

    agent.handoff("phase-1", phase="Phase 1")
    after_handoff = agent.snapshot(view_mode="latest")
    assert after_handoff.active_anchor is not None
    assert after_handoff.active_anchor.name == "handoff:phase-1"
    assert [entry.kind for entry in after_handoff.context_entries] == ["event"]

    agent.reply("second")
    after_reply = agent.snapshot(view_mode="latest")
    assert [entry.kind for entry in after_reply.context_entries] == ["event", "message", "message"]
    assert after_reply.context_entry_count == 3


def test_snapshot_from_missing_anchor_uses_latest_anchor():
    tape = FakeTape(
        [
            TapeEntry(1, "message", {"role": "user", "content": "a"}, {}),
            TapeEntry(2, "anchor", {"name": "handoff:one", "state": {"phase": "One"}}, {}),
            TapeEntry(3, "message", {"role": "assistant", "content": "b"}, {}),
        ]
    )
    agent = SimpleAgent(llm=FakeLLM(tape), tape_name="t1")

    snapshot = agent.snapshot(view_mode="from-anchor", anchor_name="handoff:not-found")

    assert snapshot.active_anchor is not None
    assert snapshot.active_anchor.name == "handoff:one"
    assert [entry.id for entry in snapshot.context_entries] == [3]


def test_reply_from_missing_anchor_falls_back_to_latest_anchor():
    tape = FakeTape(
        [
            TapeEntry(1, "anchor", {"name": "handoff:one", "state": {"phase": "One"}}, {}),
            TapeEntry(2, "message", {"role": "assistant", "content": "b"}, {}),
        ]
    )
    llm = FakeLLM(tape)
    agent = SimpleAgent(llm=llm, tape_name="t1")

    reply = agent.reply("hello", view_mode="from-anchor", anchor_name="handoff:not-found")

    assert reply == "ok"
    context = llm.chat_calls[-1]["context"]
    assert context.anchor == "handoff:one"


def test_snapshot_from_anchor_without_any_anchor_creates_bootstrap_anchor():
    tape = FakeTape(
        [
            TapeEntry(1, "message", {"role": "user", "content": "a"}, {}),
        ]
    )
    agent = SimpleAgent(llm=FakeLLM(tape), tape_name="t1")

    snapshot = agent.snapshot(view_mode="from-anchor", anchor_name="handoff:not-found")

    assert snapshot.active_anchor is not None
    assert snapshot.active_anchor.name == "handoff:auto-bootstrap"
    assert any(entry.kind == "anchor" for entry in snapshot.entries)
