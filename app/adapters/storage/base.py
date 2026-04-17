from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.session import Session


class StorageAdapter(ABC):
    @abstractmethod
    def get(self, operator_id: str, phone: str) -> Session | None: ...

    @abstractmethod
    def set(self, operator_id: str, phone: str, session: Session) -> None: ...

    @abstractmethod
    def delete(self, operator_id: str, phone: str) -> None: ...

    @abstractmethod
    def get_by_stage(self, operator_id: str, stage: str) -> list[Session]: ...
