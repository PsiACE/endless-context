from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyobvector  # noqa: F401
from bub.tape.store import TapeEntry
from sqlalchemy import create_engine, text
from sqlalchemy.dialects import registry
from sqlalchemy.engine import URL

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FORK_SUFFIX_DELIMITER = "__"


def _validate_identifier(value: str, field_name: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"Invalid {field_name}: {value!r}")
    return value


def _register_oceanbase_dialect() -> None:
    registry.register("mysql.oceanbase", "pyobvector.schema.dialect", "OceanBaseDialect")


def _safe_load_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if isinstance(value, dict):
        return value
    return {}


def _to_json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if asyncio.iscoroutine(value):
        # Keep tape persistence robust even if an upstream tool returns a
        # coroutine object by mistake (should be awaited before persistence).
        value.close()
        return {"_type": "coroutine", "note": "unawaited_tool_result"}
    if isinstance(value, dict):
        return {str(key): _to_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_json_safe(item) for item in value]
    return str(value)


@dataclass(frozen=True)
class SeekDBConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    table_name: str

    @classmethod
    def from_env(cls) -> SeekDBConfig:
        return cls(
            host=os.getenv("OCEANBASE_HOST", "127.0.0.1"),
            port=int(os.getenv("OCEANBASE_PORT", "2881")),
            user=os.getenv("OCEANBASE_USER", "root"),
            password=os.getenv("OCEANBASE_PASSWORD", ""),
            database=_validate_identifier(os.getenv("OCEANBASE_DATABASE", "bub"), "database name"),
            table_name=_validate_identifier(os.getenv("BUB_TAPE_TABLE", "bub_tape_entries"), "table name"),
        )


class SeekDBTapeStore:
    """SeekDB-backed tape store compatible with Bub's FileTapeStore protocol."""

    def __init__(self, config: SeekDBConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._fork_start_ids: dict[str, int] = {}
        _register_oceanbase_dialect()
        self._ensure_database()
        self._engine = create_engine(self._build_url(config.database), pool_pre_ping=True, future=True)
        self._ensure_table()

    @classmethod
    def from_env(cls) -> SeekDBTapeStore:
        return cls(SeekDBConfig.from_env())

    def list_tapes(self) -> list[str]:
        sql = text(
            f"""
            SELECT DISTINCT tape_name
            FROM `{self._config.table_name}`
            WHERE tape_name NOT LIKE :fork_pattern
              AND tape_name NOT LIKE :archive_pattern
            ORDER BY tape_name ASC
            """
        )
        with self._engine.connect() as conn:
            rows = conn.execute(
                sql,
                {
                    "fork_pattern": f"%{_FORK_SUFFIX_DELIMITER}%",
                    "archive_pattern": "%::archived::%",
                },
            ).fetchall()
        return [str(row[0]) for row in rows]

    def fork(self, source: str) -> str:
        fork_suffix = uuid.uuid4().hex[:8]
        fork_name = f"{source}{_FORK_SUFFIX_DELIMITER}{fork_suffix}"
        with self._lock:
            with self._engine.begin() as conn:
                rows = conn.execute(
                    text(
                        f"""
                        SELECT entry_id, kind, payload_json, meta_json
                        FROM `{self._config.table_name}`
                        WHERE tape_name = :source
                        ORDER BY entry_id ASC
                        """
                    ),
                    {"source": source},
                ).fetchall()
                if rows:
                    conn.execute(
                        text(
                            f"""
                            INSERT INTO `{self._config.table_name}`
                                (tape_name, entry_id, kind, payload_json, meta_json)
                            VALUES
                                (:tape_name, :entry_id, :kind, :payload_json, :meta_json)
                            """
                        ),
                        [
                            {
                                "tape_name": fork_name,
                                "entry_id": int(row[0]),
                                "kind": str(row[1]),
                                "payload_json": str(row[2]),
                                "meta_json": str(row[3]),
                            }
                            for row in rows
                        ],
                    )
                start_id = int(rows[-1][0]) + 1 if rows else 1
                self._fork_start_ids[fork_name] = start_id
        return fork_name

    def merge(self, source: str, target: str) -> None:
        with self._lock:
            with self._engine.begin() as conn:
                start_id = self._fork_start_ids.get(source, 1)
                source_rows = conn.execute(
                    text(
                        f"""
                        SELECT kind, payload_json, meta_json
                        FROM `{self._config.table_name}`
                        WHERE tape_name = :source
                          AND entry_id >= :start_id
                        ORDER BY entry_id ASC
                        """
                    ),
                    {"source": source, "start_id": start_id},
                ).fetchall()
                if source_rows:
                    target_next_id = int(
                        conn.execute(
                            text(
                                f"""
                                SELECT COALESCE(MAX(entry_id), 0) + 1
                                FROM `{self._config.table_name}`
                                WHERE tape_name = :target
                                """
                            ),
                            {"target": target},
                        ).scalar_one()
                    )
                    payloads = []
                    for offset, row in enumerate(source_rows):
                        payloads.append(
                            {
                                "tape_name": target,
                                "entry_id": target_next_id + offset,
                                "kind": str(row[0]),
                                "payload_json": str(row[1]),
                                "meta_json": str(row[2]),
                            }
                        )
                    conn.execute(
                        text(
                            f"""
                            INSERT INTO `{self._config.table_name}`
                                (tape_name, entry_id, kind, payload_json, meta_json)
                            VALUES
                                (:tape_name, :entry_id, :kind, :payload_json, :meta_json)
                            """
                        ),
                        payloads,
                    )
                conn.execute(
                    text(f"DELETE FROM `{self._config.table_name}` WHERE tape_name = :source"),
                    {"source": source},
                )
                self._fork_start_ids.pop(source, None)

    def reset(self, tape: str) -> None:
        with self._lock:
            with self._engine.begin() as conn:
                conn.execute(
                    text(f"DELETE FROM `{self._config.table_name}` WHERE tape_name = :tape"),
                    {"tape": tape},
                )
            self._fork_start_ids.pop(tape, None)

    def read(self, tape: str) -> list[TapeEntry] | None:
        sql = text(
            f"""
            SELECT entry_id, kind, payload_json, meta_json, created_at
            FROM `{self._config.table_name}`
            WHERE tape_name = :tape
            ORDER BY entry_id ASC
            """
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sql, {"tape": tape}).fetchall()

        if not rows:
            return None

        entries: list[TapeEntry] = []
        for row in rows:
            payload = _safe_load_json(row[2])
            meta = _safe_load_json(row[3])
            created_at = row[4]
            if created_at is not None:
                meta.setdefault("created_at", created_at.isoformat())
            entry = self.entry_from_payload(
                {
                    "id": int(row[0]),
                    "kind": str(row[1]),
                    "payload": payload,
                    "meta": meta,
                }
            )
            if entry is not None:
                entries.append(entry)
        return entries

    def append(self, tape: str, entry: TapeEntry) -> None:
        raw_kind = str(getattr(entry, "kind", "event"))
        raw_payload = dict(getattr(entry, "payload", {}))
        raw_meta = dict(getattr(entry, "meta", {}))

        with self._lock:
            with self._engine.begin() as conn:
                next_id = int(
                    conn.execute(
                        text(
                            f"""
                            SELECT COALESCE(MAX(entry_id), 0) + 1
                            FROM `{self._config.table_name}`
                            WHERE tape_name = :tape
                            """
                        ),
                        {"tape": tape},
                    ).scalar_one()
                )
                stored = TapeEntry(next_id, raw_kind, raw_payload, raw_meta)
                payload_doc = self.entry_to_payload(stored)
                payload_json = json.dumps(
                    _to_json_safe(payload_doc["payload"]), ensure_ascii=False, separators=(",", ":")
                )
                meta_json = json.dumps(_to_json_safe(payload_doc["meta"]), ensure_ascii=False, separators=(",", ":"))
                conn.execute(
                    text(
                        f"""
                        INSERT INTO `{self._config.table_name}`
                            (tape_name, entry_id, kind, payload_json, meta_json)
                        VALUES
                            (:tape_name, :entry_id, :kind, :payload_json, :meta_json)
                        """
                    ),
                    {
                        "tape_name": tape,
                        "entry_id": int(payload_doc["id"]),
                        "kind": str(payload_doc["kind"]),
                        "payload_json": payload_json,
                        "meta_json": meta_json,
                    },
                )

    @staticmethod
    def entry_to_payload(entry: TapeEntry) -> dict[str, object]:
        return {
            "id": int(getattr(entry, "id", 0)),
            "kind": str(getattr(entry, "kind", "event")),
            "payload": dict(getattr(entry, "payload", {})),
            "meta": dict(getattr(entry, "meta", {})),
        }

    @staticmethod
    def entry_from_payload(payload: object) -> TapeEntry | None:
        if not isinstance(payload, dict):
            return None
        entry_id = payload.get("id")
        kind = payload.get("kind")
        entry_payload = payload.get("payload")
        meta = payload.get("meta")
        if not isinstance(entry_id, int):
            return None
        if not isinstance(kind, str):
            return None
        if not isinstance(entry_payload, dict):
            return None
        if not isinstance(meta, dict):
            meta = {}
        return TapeEntry(entry_id, kind, dict(entry_payload), dict(meta))

    def archive(self, tape: str) -> Path | None:
        with self._lock:
            with self._engine.begin() as conn:
                count = int(
                    conn.execute(
                        text(
                            f"""
                            SELECT COUNT(*)
                            FROM `{self._config.table_name}`
                            WHERE tape_name = :tape
                            """
                        ),
                        {"tape": tape},
                    ).scalar_one()
                )
                if count == 0:
                    return None
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                archived_name = f"{tape}::archived::{stamp}"
                conn.execute(
                    text(
                        f"""
                        UPDATE `{self._config.table_name}`
                        SET tape_name = :archived
                        WHERE tape_name = :tape
                        """
                    ),
                    {"archived": archived_name, "tape": tape},
                )
                self._fork_start_ids.pop(tape, None)
                return Path("/seekdb/archive") / archived_name

    def _ensure_database(self) -> None:
        admin_engine = create_engine(self._build_url(database=None), pool_pre_ping=True, future=True)
        try:
            with admin_engine.begin() as conn:
                conn.execute(
                    text(f"CREATE DATABASE IF NOT EXISTS `{self._config.database}` DEFAULT CHARACTER SET utf8mb4")
                )
        finally:
            admin_engine.dispose()

    def _ensure_table(self) -> None:
        create_sql = f"""
        CREATE TABLE IF NOT EXISTS `{self._config.table_name}` (
            id BIGINT NOT NULL AUTO_INCREMENT,
            tape_name VARCHAR(255) NOT NULL,
            entry_id BIGINT NOT NULL,
            kind VARCHAR(64) NOT NULL,
            payload_json LONGTEXT NOT NULL,
            meta_json LONGTEXT NOT NULL,
            created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            PRIMARY KEY (id),
            UNIQUE KEY uniq_tape_entry (tape_name, entry_id),
            KEY idx_tape_name_created (tape_name, created_at)
        ) DEFAULT CHARSET = utf8mb4
        """
        with self._engine.begin() as conn:
            conn.execute(text(create_sql))

    def _build_url(self, database: str | None) -> URL:
        return URL.create(
            drivername="mysql+oceanbase",
            username=self._config.user,
            password=self._config.password or None,
            host=self._config.host,
            port=self._config.port,
            database=database,
            query={"charset": "utf8mb4"},
        )

    def dispose(self) -> None:
        self._engine.dispose()
