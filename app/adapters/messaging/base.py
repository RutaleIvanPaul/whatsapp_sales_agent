from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.operator import Operator


class MessagingAdapter(ABC):
    # Note: methods are async because real implementations (e.g. Whapi via
    # httpx.AsyncClient) require async I/O. CLAUDE.md shows sync signatures
    # but the implementation reality of FastAPI + httpx mandates async here.

    @abstractmethod
    async def send_text(self, phone: str, text: str, operator: Operator) -> None: ...

    @abstractmethod
    async def send_image(
        self, phone: str, image_url: str, caption: str, operator: Operator
    ) -> None: ...
