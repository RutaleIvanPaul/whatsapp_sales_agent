from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from app.adapters.tenant.base import TenantAdapter
from app.models.tenant import Tenant, TenantStatus
from app.utils.crypto import decrypt, encrypt


class SqliteTenantAdapter(TenantAdapter):
    def __init__(self, db_path: str, encryption_key: bytes) -> None:
        self._key = encryption_key
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS tenants (
                tenant_id  TEXT PRIMARY KEY,
                data       TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                status     TEXT NOT NULL
            )"""
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_channel_id ON tenants(channel_id)"
        )
        self._conn.commit()

    def get_by_channel_id(self, channel_id: str) -> Tenant | None:
        row = self._conn.execute(
            "SELECT data FROM tenants WHERE channel_id=?", (channel_id,)
        ).fetchone()
        if row is None:
            return None
        return self._deserialise(json.loads(row[0]))

    def get_all_active(self) -> list[Tenant]:
        rows = self._conn.execute(
            "SELECT data FROM tenants WHERE status=?", (TenantStatus.ACTIVE.value,)
        ).fetchall()
        return [self._deserialise(json.loads(r[0])) for r in rows]

    def update_status(self, tenant_id: str, status: TenantStatus) -> None:
        self._conn.execute(
            "UPDATE tenants SET status=? WHERE tenant_id=?",
            (status.value, tenant_id),
        )
        self._conn.commit()

    def save(self, tenant: Tenant) -> None:
        data = self._serialise(tenant)
        self._conn.execute(
            "INSERT OR REPLACE INTO tenants (tenant_id, data, channel_id, status) VALUES (?,?,?,?)",
            (tenant.tenant_id, json.dumps(data), tenant.whapi_channel_id, tenant.status.value),
        )
        self._conn.commit()

    def _serialise(self, t: Tenant) -> dict:
        d = {}
        for k, v in t.__dict__.items():
            if k in ("whapi_channel_token", "whapi_webhook_secret"):
                d[k] = encrypt(v, self._key)
            elif isinstance(v, TenantStatus):
                d[k] = v.value
            elif isinstance(v, datetime):
                d[k] = v.isoformat()
            else:
                d[k] = v
        return d

    def _deserialise(self, d: dict) -> Tenant:
        for k in ("whapi_channel_token", "whapi_webhook_secret"):
            if d.get(k):
                d[k] = decrypt(d[k], self._key)
        d["status"] = TenantStatus(d["status"])
        if d.get("created_at") is not None:
            d["created_at"] = datetime.fromisoformat(d["created_at"])
        return Tenant(**d)
