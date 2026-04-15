from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.operator import Operator, OperatorStatus


class OperatorAdapter(ABC):
    @abstractmethod
    def get_by_channel_id(self, channel_id: str) -> Operator | None: ...

    @abstractmethod
    def get_all_active(self) -> list[Operator]: ...

    @abstractmethod
    def update_status(self, operator_id: str, status: OperatorStatus) -> None: ...
