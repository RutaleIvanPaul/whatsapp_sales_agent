from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.product import Product


class InventoryAdapter(ABC):
    @abstractmethod
    def search(self, query: str, shown_ids: list[str]) -> list[Product]: ...

    @abstractmethod
    def get_all(self) -> list[Product]: ...
