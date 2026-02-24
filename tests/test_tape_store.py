from __future__ import annotations

import datetime as dt
import threading
from dataclasses import dataclass
from typing import Any

import pytest

from endless_context.tape_store import SeekDBConfig, SeekDBTapeStore, _safe_load_json, _validate_identifier


@dataclass
class _Entry:
    kind: str
    payload: dict[str, Any]
    meta: dict[str, Any]


class _FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None, scalar: Any = None) -> None:
        self._rows = rows or []
        self._scalar = scalar

    def fetchall(self):
        return self._rows

    def scalar_one(self):
        return self._scalar


class _FakeConnection:
    def __init__(self, state: dict[str, list[dict[str, Any]]]) -> None:
        self._state = state

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def execute(self, statement, params=None):  # noqa: ANN001
        sql = " ".join(str(statement).strip().lower().split())
        params = params or {}

        if "select distinct tape_name" in sql:
            tapes = sorted(self._state.keys())
            if isinstance(params, dict):
                fork_pattern = str(params.get("fork_pattern", "%__%")).replace("%", "")
                archive_pattern = str(params.get("archive_pattern", "%::archived::%")).replace("%", "")
                tapes = [name for name in tapes if fork_pattern not in name and archive_pattern not in name]
            return _FakeResult(rows=[(name,) for name in tapes])

        if "select entry_id, kind, payload_json, meta_json, created_at" in sql:
            tape = str(params["tape"])
            records = sorted(self._state.get(tape, []), key=lambda item: item["entry_id"])
            rows = [
                (
                    record["entry_id"],
                    record["kind"],
                    record["payload_json"],
                    record["meta_json"],
                    record["created_at"],
                )
                for record in records
            ]
            return _FakeResult(rows=rows)

        if "select entry_id, kind, payload_json, meta_json" in sql and "where tape_name = :source" in sql:
            source = str(params["source"])
            rows = [
                (record["entry_id"], record["kind"], record["payload_json"], record["meta_json"])
                for record in sorted(self._state.get(source, []), key=lambda item: item["entry_id"])
            ]
            return _FakeResult(rows=rows)

        if "select kind, payload_json, meta_json" in sql and "entry_id >= :start_id" in sql:
            source = str(params["source"])
            start_id = int(params["start_id"])
            rows = [
                (record["kind"], record["payload_json"], record["meta_json"])
                for record in sorted(self._state.get(source, []), key=lambda item: item["entry_id"])
                if int(record["entry_id"]) >= start_id
            ]
            return _FakeResult(rows=rows)

        if "select coalesce(max(entry_id), 0) + 1" in sql:
            key = "tape"
            if isinstance(params, dict):
                if "target" in params:
                    key = "target"
            tape = str(params[key])
            records = self._state.get(tape, [])
            next_id = max((int(record["entry_id"]) for record in records), default=0) + 1
            return _FakeResult(scalar=next_id)

        if "select count(*)" in sql:
            tape = str(params["tape"])
            return _FakeResult(scalar=len(self._state.get(tape, [])))

        if "insert into" in sql:
            rows = params if isinstance(params, list) else [params]
            for row in rows:
                tape = str(row["tape_name"])
                record = {
                    "entry_id": int(row["entry_id"]),
                    "kind": str(row["kind"]),
                    "payload_json": str(row["payload_json"]),
                    "meta_json": str(row["meta_json"]),
                    "created_at": dt.datetime(2026, 2, 10, 12, 0, 0),
                }
                self._state.setdefault(tape, []).append(record)
                self._state[tape].sort(key=lambda item: item["entry_id"])
            return _FakeResult()

        if "update" in sql and "set tape_name = :archived" in sql:
            tape = str(params["tape"])
            archived = str(params["archived"])
            records = self._state.pop(tape, [])
            if records:
                self._state.setdefault(archived, []).extend(records)
                self._state[archived].sort(key=lambda item: item["entry_id"])
            return _FakeResult()

        if "delete from" in sql and "where tape_name = :source" in sql:
            self._state.pop(str(params["source"]), None)
            return _FakeResult()

        if "delete from" in sql and "where tape_name = :tape" in sql:
            self._state.pop(str(params["tape"]), None)
            return _FakeResult()

        return _FakeResult()


class _FakeEngine:
    def __init__(self) -> None:
        self.state: dict[str, list[dict[str, Any]]] = {}

    def connect(self):
        return _FakeConnection(self.state)

    def begin(self):
        return _FakeConnection(self.state)

    def dispose(self):
        return None


def _make_store(fake_engine: _FakeEngine) -> SeekDBTapeStore:
    store = SeekDBTapeStore.__new__(SeekDBTapeStore)
    store._config = SeekDBConfig(
        host="127.0.0.1",
        port=2881,
        user="root",
        password="",
        database="bub",
        table_name="bub_tape_entries",
    )
    store._lock = threading.Lock()
    store._fork_start_ids = {}
    store._engine = fake_engine
    return store


def test_validate_identifier_and_safe_load_json():
    assert _validate_identifier("valid_name_1", "table") == "valid_name_1"
    assert _safe_load_json('{"k":"v"}') == {"k": "v"}
    assert _safe_load_json("[1,2,3]") == {}
    assert _safe_load_json("{invalid") == {}
    with pytest.raises(ValueError):
        _validate_identifier("invalid-name", "table")


def test_seekdb_tape_store_append_read_and_list():
    engine = _FakeEngine()
    store = _make_store(engine)

    store.append(
        "session-a",
        _Entry(
            kind="message",
            payload={"role": "user", "content": "hi"},
            meta={"run_id": "r1"},
        ),
    )
    store.append(
        "session-a", _Entry(kind="anchor", payload={"name": "handoff:phase-1", "state": {"phase": "P1"}}, meta={})
    )

    tapes = store.list_tapes()
    assert tapes == ["session-a"]

    entries = store.read("session-a")
    assert entries is not None
    assert [entry.id for entry in entries] == [1, 2]
    assert entries[0].kind == "message"
    assert entries[1].kind == "anchor"
    assert entries[0].meta.get("run_id") == "r1"
    assert "created_at" in entries[0].meta


def test_seekdb_tape_store_list_excludes_fork_and_archived():
    engine = _FakeEngine()
    store = _make_store(engine)

    store.append("normal-a", _Entry(kind="system", payload={"content": "a"}, meta={}))
    store.append("normal-b", _Entry(kind="system", payload={"content": "b"}, meta={}))
    store.append("normal-a__f0rk1234", _Entry(kind="system", payload={"content": "fork"}, meta={}))
    store.append("normal-b::archived::20260224T000000Z", _Entry(kind="system", payload={"content": "arch"}, meta={}))

    assert store.list_tapes() == ["normal-a", "normal-b"]


def test_seekdb_tape_store_fork_and_merge_appends_only_new_entries():
    engine = _FakeEngine()
    store = _make_store(engine)

    store.append("session", _Entry(kind="message", payload={"role": "user", "content": "root-1"}, meta={}))
    fork_name = store.fork("session")
    store.append(fork_name, _Entry(kind="message", payload={"role": "assistant", "content": "fork-1"}, meta={}))
    store.append(fork_name, _Entry(kind="message", payload={"role": "assistant", "content": "fork-2"}, meta={}))

    store.merge(fork_name, "session")
    merged = store.read("session")
    assert merged is not None
    assert [entry.payload["content"] for entry in merged if "content" in entry.payload] == [
        "root-1",
        "fork-1",
        "fork-2",
    ]
    assert [entry.id for entry in merged] == [1, 2, 3]
    assert store.read(fork_name) is None


def test_seekdb_tape_store_archive_and_reset():
    engine = _FakeEngine()
    store = _make_store(engine)

    store.append("session-x", _Entry(kind="message", payload={"role": "user", "content": "x"}, meta={}))
    archive_path = store.archive("session-x")
    assert archive_path is not None
    assert "session-x::archived::" in str(archive_path)
    assert store.read("session-x") is None

    store.append("session-y", _Entry(kind="message", payload={"role": "user", "content": "y"}, meta={}))
    store.reset("session-y")
    assert store.read("session-y") is None
