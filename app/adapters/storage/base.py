from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.session import Session


class StorageAdapter(ABC):
    @abstractmethod
    def get(self, tenant_id: str, phone: str) -> Session | None: ...

    @abstractmethod
    def set(self, tenant_id: str, phone: str, session: Session) -> None: ...

    @abstractmethod
    def delete(self, tenant_id: str, phone: str) -> None: ...
