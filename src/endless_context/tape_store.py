from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from typing import Any

import pyobvector  # noqa: F401
from republic.tape.entries import TapeEntry
from republic.tape.store import TapeStore
from sqlalchemy import create_engine, text
from sqlalchemy.dialects import registry
from sqlalchemy.engine import URL

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
            database=_validate_identifier(os.getenv("OCEANBASE_DATABASE", "republic"), "database name"),
            table_name=_validate_identifier(os.getenv("REPUBLIC_TAPE_TABLE", "republic_tape_entries"), "table name"),
        )


class SeekDBTapeStore(TapeStore):
    """TapeStore backed by SeekDB/OceanBase using the pyobvector SQLAlchemy dialect."""

    def __init__(self, config: SeekDBConfig) -> None:
        self._config = config
        self._append_lock = threading.Lock()
        _register_oceanbase_dialect()
        self._ensure_database()
        self._engine = create_engine(self._build_url(config.database), pool_pre_ping=True, future=True)
        self._ensure_table()

    @classmethod
    def from_env(cls) -> SeekDBTapeStore:
        return cls(SeekDBConfig.from_env())

    def list_tapes(self) -> list[str]:
        sql = text(f"SELECT DISTINCT tape_name FROM `{self._config.table_name}` ORDER BY tape_name ASC")
        with self._engine.connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [str(row[0]) for row in rows]

    def reset(self, tape: str) -> None:
        sql = text(f"DELETE FROM `{self._config.table_name}` WHERE tape_name = :tape")
        with self._engine.begin() as conn:
            conn.execute(sql, {"tape": tape})

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
            entries.append(
                TapeEntry(
                    id=int(row[0]),
                    kind=str(row[1]),
                    payload=payload,
                    meta=meta,
                )
            )
        return entries

    def append(self, tape: str, entry: TapeEntry) -> None:
        payload_json = json.dumps(entry.payload, ensure_ascii=False, separators=(",", ":"))
        meta_json = json.dumps(entry.meta, ensure_ascii=False, separators=(",", ":"))
        with self._append_lock:
            with self._engine.begin() as conn:
                next_id_sql = text(
                    f"""
                    SELECT COALESCE(MAX(entry_id), 0) + 1
                    FROM `{self._config.table_name}`
                    WHERE tape_name = :tape
                    """
                )
                next_id = int(conn.execute(next_id_sql, {"tape": tape}).scalar_one())

                insert_sql = text(
                    f"""
                    INSERT INTO `{self._config.table_name}`
                        (tape_name, entry_id, kind, payload_json, meta_json)
                    VALUES
                        (:tape_name, :entry_id, :kind, :payload_json, :meta_json)
                    """
                )
                conn.execute(
                    insert_sql,
                    {
                        "tape_name": tape,
                        "entry_id": next_id,
                        "kind": entry.kind,
                        "payload_json": payload_json,
                        "meta_json": meta_json,
                    },
                )

    def _ensure_database(self) -> None:
        admin_engine = create_engine(self._build_url(database=None), pool_pre_ping=True, future=True)
        try:
            with admin_engine.begin() as conn:
                conn.execute(
                    text(
                        f"CREATE DATABASE IF NOT EXISTS `{self._config.database}` "
                        "DEFAULT CHARACTER SET utf8mb4"
                    )
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
            KEY idx_tape_created (tape_name, created_at)
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
