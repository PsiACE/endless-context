from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from endless_context.agent import BubAgent, estimate_tokens


@dataclass
class _LoopResult:
    immediate_output: str
    assistant_output: str
    exit_requested: bool
    steps: int
    error: str | None = None


@dataclass
class _TapeRef:
    name: str


@dataclass
class _Entry:
    id: int
    kind: str
    payload: dict[str, Any]
    meta: dict[str, Any]


class _FakeTapeService:
    def __init__(self, name: str, entries: list[_Entry] | None = None) -> None:
        self.tape = _TapeRef(name=name)
        self.entries: list[_Entry] = list(entries or [])
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.reset_calls: list[bool] = []

    def read_entries(self) -> list[_Entry]:
        return list(self.entries)

    def ensure_bootstrap_anchor(self) -> None:
        has_anchor = any(item.kind == "anchor" for item in self.entries)
        if has_anchor:
            return
        self.handoff("session/start", state={"owner": "human"})

    def handoff(self, name: str, state: dict[str, Any] | None = None) -> list[_Entry]:
        anchor = _Entry(
            id=len(self.entries) + 1,
            kind="anchor",
            payload={"name": name, "state": state or {}},
            meta={},
        )
        self.entries.append(anchor)
        return [anchor]

    def append_event(self, name: str, data: dict[str, Any]) -> None:
        self.events.append((name, dict(data)))
        self.entries.append(
            _Entry(
                id=len(self.entries) + 1,
                kind="event",
                payload={"name": name, "data": dict(data)},
                meta={},
            )
        )

    def reset(self, archive: bool = False) -> str:
        self.reset_calls.append(archive)
        self.entries.clear()
        self.handoff("session/start", state={"owner": "human"})
        return "ok"


class _FakeSession:
    def __init__(self, tape: _FakeTapeService) -> None:
        self.tape = tape


class _FakeRuntime:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session
        self.settings = _Settings()
        self.handle_input_calls: list[tuple[str, str]] = []
        self.reset_calls: list[str] = []
        self.next_result = _LoopResult("", "ok", False, 1)

    def get_session(self, session_id: str) -> _FakeSession:
        self._last_session_id = session_id
        return self._session

    async def handle_input(self, session_id: str, text: str) -> _LoopResult:
        self.handle_input_calls.append((session_id, text))
        self._session.tape.entries.append(
            _Entry(
                id=len(self._session.tape.entries) + 1,
                kind="message",
                payload={"role": "user", "content": text},
                meta={},
            )
        )
        if self.next_result.assistant_output:
            self._session.tape.entries.append(
                _Entry(
                    id=len(self._session.tape.entries) + 1,
                    kind="message",
                    payload={"role": "assistant", "content": self.next_result.assistant_output},
                    meta={},
                )
            )
        return self.next_result

    def reset_session_context(self, session_id: str) -> None:
        self.reset_calls.append(session_id)


class _Settings:
    def model_copy(self, update: dict[str, object]) -> _Settings:
        self.last_update = update
        return self


def _build_agent(entries: list[_Entry] | None = None) -> tuple[BubAgent, _FakeRuntime, _FakeTapeService]:
    tape = _FakeTapeService(name="endless-context:default", entries=entries)
    runtime = _FakeRuntime(_FakeSession(tape))
    agent = BubAgent(runtime=runtime)
    return agent, runtime, tape


def test_snapshot_latest_uses_last_anchor() -> None:
    agent, _, _ = _build_agent(
        [
            _Entry(1, "message", {"role": "user", "content": "a"}, {}),
            _Entry(2, "anchor", {"name": "handoff:first", "state": {"phase": "First"}}, {}),
            _Entry(3, "message", {"role": "assistant", "content": "b"}, {}),
            _Entry(4, "anchor", {"name": "handoff:second", "state": {"phase": "Second"}}, {}),
            _Entry(5, "message", {"role": "user", "content": "c"}, {}),
        ]
    )

    snapshot = agent.snapshot(view_mode="latest")

    assert snapshot.active_anchor is not None
    assert snapshot.active_anchor.name == "handoff:second"
    assert [entry.id for entry in snapshot.context_entries] == [5]


def test_reply_returns_assistant_output_and_records_context_event() -> None:
    agent, runtime, tape = _build_agent(
        [
            _Entry(1, "anchor", {"name": "handoff:phase-a", "state": {"phase": "Phase A"}}, {}),
            _Entry(2, "message", {"role": "assistant", "content": "seed"}, {}),
        ]
    )

    reply = agent.reply("hello", view_mode="from-anchor", anchor_name="handoff:phase-a")

    assert reply == "ok"
    assert runtime.handle_input_calls[-1][1] == "hello"
    assert any(name == "gradio.context_selection" for name, _ in tape.events)


def test_reply_returns_error_prefix_when_runtime_failed() -> None:
    agent, runtime, _ = _build_agent()
    runtime.next_result = _LoopResult("", "", False, 1, error="upstream unavailable")

    reply = agent.reply("hello")

    assert reply == "Error: upstream unavailable"


def test_handoff_normalizes_name_and_appends() -> None:
    agent, _, tape = _build_agent()

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
    assert anchors[0].payload["state"]["facts"] == ["A", "B"]


def test_snapshot_from_missing_anchor_uses_latest_anchor() -> None:
    agent, _, _ = _build_agent(
        [
            _Entry(1, "message", {"role": "user", "content": "a"}, {}),
            _Entry(2, "anchor", {"name": "handoff:one", "state": {"phase": "One"}}, {}),
            _Entry(3, "message", {"role": "assistant", "content": "b"}, {}),
        ]
    )

    snapshot = agent.snapshot(view_mode="from-anchor", anchor_name="handoff:not-found")

    assert snapshot.active_anchor is not None
    assert snapshot.active_anchor.name == "handoff:one"
    assert [entry.id for entry in snapshot.context_entries] == [3]


def test_snapshot_from_anchor_without_any_anchor_creates_bootstrap_anchor() -> None:
    agent, _, tape = _build_agent(
        [
            _Entry(1, "message", {"role": "user", "content": "a"}, {}),
        ]
    )

    snapshot = agent.snapshot(view_mode="from-anchor", anchor_name="handoff:not-found")

    assert snapshot.active_anchor is not None
    assert snapshot.active_anchor.name == "session/start"
    assert any(entry.kind == "anchor" for entry in tape.entries)


def test_reset_archives_and_resets_runtime_context() -> None:
    agent, runtime, tape = _build_agent(
        [
            _Entry(1, "message", {"role": "user", "content": "a"}, {}),
        ]
    )

    agent.reset()

    assert tape.reset_calls == [True]
    assert runtime.reset_calls == ["endless-context:default"]


def test_estimate_tokens_prefers_usage_event() -> None:
    entries = [
        _Entry(1, "message", {"role": "user", "content": "hello"}, {}),
        _Entry(
            2,
            "event",
            {
                "name": "run",
                "data": {
                    "status": "ok",
                    "usage": {
                        "input_tokens": 123,
                        "output_tokens": 45,
                        "total_tokens": 168,
                    },
                },
            },
            {},
        ),
    ]

    assert estimate_tokens(entries) == 123
