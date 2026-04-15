from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from app.adapters.storage.base import StorageAdapter
from app.models.session import Session, Stage


class SqliteStorageAdapter(StorageAdapter):
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                tenant_id  TEXT NOT NULL,
                phone      TEXT NOT NULL,
                data       TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, phone)
            )"""
        )
        self._conn.commit()

    def get(self, tenant_id: str, phone: str) -> Session | None:
        row = self._conn.execute(
            "SELECT data FROM sessions WHERE tenant_id=? AND phone=?",
            (tenant_id, phone),
        ).fetchone()
        if row is None:
            return None
        return _deserialise_session(json.loads(row[0]))

    def set(self, tenant_id: str, phone: str, session: Session) -> None:
        now = datetime.utcnow().isoformat()
        data = _serialise_session(session)
        self._conn.execute(
            "INSERT OR REPLACE INTO sessions (tenant_id, phone, data, updated_at) VALUES (?,?,?,?)",
            (tenant_id, phone, json.dumps(data), now),
        )
        self._conn.commit()

    def delete(self, tenant_id: str, phone: str) -> None:
        self._conn.execute(
            "DELETE FROM sessions WHERE tenant_id=? AND phone=?",
            (tenant_id, phone),
        )
        self._conn.commit()


def _serialise_session(s: Session) -> dict:
    d = {}
    for k, v in s.__dict__.items():
        if isinstance(v, Stage):
            d[k] = v.value
        elif isinstance(v, datetime):
            d[k] = v.isoformat()
        else:
            d[k] = v
    return d


def _deserialise_session(d: dict) -> Session:
    d["stage"] = Stage(d["stage"])
    for field in ("handed_off_at", "last_holding_sent", "last_active", "created_at"):
        if d.get(field) is not None:
            d[field] = datetime.fromisoformat(d[field])
    return Session(**d)
