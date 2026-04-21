from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from app.adapters.operator.base import OperatorAdapter
from app.models.operator import Operator, OperatorStatus
from app.utils.crypto import decrypt, encrypt
from app.utils.log import log


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
        try:
            return self._deserialise(json.loads(row[0]))
        except Exception as e:
            log(
                "error",
                component="operator_storage",
                error_type="corrupt_operator",
                message=type(e).__name__,
                channel_id=channel_id,
            )
            return None

    def get_all_active(self) -> list[Operator]:
        rows = self._conn.execute(
            "SELECT data FROM operators WHERE status=?", (OperatorStatus.ACTIVE.value,)
        ).fetchall()
        results = []
        for r in rows:
            try:
                results.append(self._deserialise(json.loads(r[0])))
            except Exception as e:
                log(
                    "error",
                    component="operator_storage",
                    error_type="corrupt_operator_skipped",
                    message=type(e).__name__,
                )
        return results

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
                # These fields are stored as ciphertext in the Operator
                # dataclass (_deserialise does not decrypt). On a load→save
                # cycle, pass through as-is to avoid double-encryption.
                # For NEW operators (create_test_operator), the caller
                # passes plaintext — use encrypt_if_plaintext to handle both.
                d[k] = self._encrypt_if_plaintext(v)
            elif isinstance(v, OperatorStatus):
                d[k] = v.value
            elif isinstance(v, datetime):
                d[k] = v.isoformat()
            else:
                d[k] = v
        return d

    def _encrypt_if_plaintext(self, value: str) -> str:
        """Encrypt only if the value is plaintext (not already ciphertext).

        Ciphertext from our encrypt() is always valid base64 and decrypts
        successfully. If decrypt succeeds, the value is already encrypted
        — return it as-is. If decrypt fails, it's plaintext — encrypt it.
        """
        try:
            decrypt(value, self._key)
            return value  # already ciphertext
        except Exception:
            return encrypt(value, self._key)  # plaintext → encrypt

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
        d.setdefault("google_sheet_name", "Sheet1")
        return Operator(**d)
