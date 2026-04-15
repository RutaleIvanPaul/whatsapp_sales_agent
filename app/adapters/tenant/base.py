from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.tenant import Tenant, TenantStatus


class TenantAdapter(ABC):
    @abstractmethod
    def get_by_channel_id(self, channel_id: str) -> Tenant | None: ...

    @abstractmethod
    def get_all_active(self) -> list[Tenant]: ...

    @abstractmethod
    def update_status(self, tenant_id: str, status: TenantStatus) -> None: ...
