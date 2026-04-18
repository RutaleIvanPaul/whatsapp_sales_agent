from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from app.adapters.operator.base import OperatorAdapter
from app.models.operator import Operator, OperatorStatus
from app.utils.crypto import decrypt, encrypt


class SqliteOperatorAdapter(OperatorAdapter):
    def __init__(self, db_path: str, encryption_key: bytes) -> None:
        self._key = encryption_key
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS operators (
                operator_id  TEXT PRIMARY KEY,
                data         TEXT NOT NULL,
                channel_id   TEXT NOT NULL,
                status       TEXT NOT NULL
            )"""
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_channel_id ON operators(channel_id)"
        )
        self._conn.commit()

    def get_by_channel_id(self, channel_id: str) -> Operator | None:
        row = self._conn.execute(
            "SELECT data FROM operators WHERE channel_id=?", (channel_id,)
        ).fetchone()
        if row is None:
            return None
        return self._deserialise(json.loads(row[0]))

    def get_all_active(self) -> list[Operator]:
        rows = self._conn.execute(
            "SELECT data FROM operators WHERE status=?", (OperatorStatus.ACTIVE.value,)
        ).fetchall()
        return [self._deserialise(json.loads(r[0])) for r in rows]

    def update_status(self, operator_id: str, status: OperatorStatus) -> None:
        self._conn.execute(
            "UPDATE operators SET status=? WHERE operator_id=?",
            (status.value, operator_id),
        )
        self._conn.commit()

    def save(self, operator: Operator) -> None:
        data = self._serialise(operator)
        self._conn.execute(
            "INSERT OR REPLACE INTO operators (operator_id, data, channel_id, status) VALUES (?,?,?,?)",
            (operator.operator_id, json.dumps(data), operator.whapi_channel_id, operator.status.value),
        )
        self._conn.commit()

    def _serialise(self, o: Operator) -> dict:
        d = {}
        for k, v in o.__dict__.items():
            if k in ("whapi_channel_token", "whapi_webhook_secret"):
                d[k] = encrypt(v, self._key)
            elif isinstance(v, OperatorStatus):
                d[k] = v.value
            elif isinstance(v, datetime):
                d[k] = v.isoformat()
            else:
                d[k] = v
        return d

    def _deserialise(self, d: dict) -> Operator:
        # Sensitive fields (whapi_channel_token, whapi_webhook_secret) remain
        # as ciphertext in the Operator dataclass. Callers must decrypt per-use
        # via app.utils.crypto.decrypt(). This is defense-in-depth against
        # plaintext leakage via memory dumps or accidental logging.
        d["status"] = OperatorStatus(d["status"])
        if d.get("created_at") is not None:
            d["created_at"] = datetime.fromisoformat(d["created_at"])
        # Backward compat: existing DB rows may not have these fields
        d.setdefault("excluded_phones", [])
        d.setdefault("included_phones", [])
        return Operator(**d)
