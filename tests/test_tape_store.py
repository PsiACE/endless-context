from __future__ import annotations

import datetime as dt
import threading

import pytest
from republic.tape.entries import TapeEntry

from endless_context.tape_store import SeekDBConfig, SeekDBTapeStore, _safe_load_json, _validate_identifier


class _FakeResult:
    def __init__(self, rows=None, scalar=None) -> None:
        self._rows = rows or []
        self._scalar = scalar

    def fetchall(self):
        return self._rows

    def scalar_one(self):
        return self._scalar


class _FakeConnection:
    def __init__(self, state: dict[str, list[dict]]) -> None:
        self._state = state

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def execute(self, statement, params=None):
        params = params or {}
        sql = " ".join(str(statement).strip().lower().split())

        if "select distinct tape_name" in sql:
            rows = [(name,) for name in sorted(self._state.keys())]
            return _FakeResult(rows=rows)

        if "delete from" in sql and "where tape_name = :tape" in sql:
            self._state.pop(params["tape"], None)
            return _FakeResult()

        if "select entry_id, kind, payload_json, meta_json, created_at" in sql:
            records = self._state.get(params["tape"], [])
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

        if "select coalesce(max(entry_id), 0) + 1" in sql:
            records = self._state.get(params["tape"], [])
            next_id = max((record["entry_id"] for record in records), default=0) + 1
            return _FakeResult(scalar=next_id)

        if "insert into" in sql:
            tape = params["tape_name"]
            record = {
                "entry_id": params["entry_id"],
                "kind": params["kind"],
                "payload_json": params["payload_json"],
                "meta_json": params["meta_json"],
                "created_at": dt.datetime(2026, 2, 10, 12, 0, 0),
            }
            self._state.setdefault(tape, []).append(record)
            self._state[tape].sort(key=lambda item: item["entry_id"])
            return _FakeResult()

        return _FakeResult()


class _FakeEngine:
    def __init__(self) -> None:
        self.state: dict[str, list[dict]] = {}

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
        database="republic",
        table_name="republic_tape_entries",
    )
    store._append_lock = threading.Lock()
    store._engine = fake_engine
    return store


def test_validate_identifier_and_safe_load_json():
    assert _validate_identifier("valid_name_1", "table") == "valid_name_1"
    assert _safe_load_json('{"k":"v"}') == {"k": "v"}
    assert _safe_load_json("[1,2,3]") == {}
    assert _safe_load_json("{invalid") == {}
    with pytest.raises(ValueError):
        _validate_identifier("invalid-name", "table")


def test_seekdb_tape_store_append_read_and_reset():
    engine = _FakeEngine()
    store = _make_store(engine)

    store.append("tape-a", TapeEntry.message({"role": "user", "content": "hi"}, run_id="r1"))
    store.append("tape-a", TapeEntry.anchor("handoff:phase-1", state={"phase": "P1"}))

    tapes = store.list_tapes()
    assert tapes == ["tape-a"]

    entries = store.read("tape-a")
    assert entries is not None
    assert [entry.id for entry in entries] == [1, 2]
    assert entries[0].kind == "message"
    assert entries[1].kind == "anchor"
    assert entries[0].meta.get("run_id") == "r1"
    assert "created_at" in entries[0].meta

    store.reset("tape-a")
    assert store.read("tape-a") is None


def test_seekdb_tape_store_lists_tapes_in_sorted_order():
    engine = _FakeEngine()
    store = _make_store(engine)

    store.append("tape-z", TapeEntry.system("z"))
    store.append("tape-a", TapeEntry.system("a"))
    store.append("tape-m", TapeEntry.system("m"))

    assert store.list_tapes() == ["tape-a", "tape-m", "tape-z"]
