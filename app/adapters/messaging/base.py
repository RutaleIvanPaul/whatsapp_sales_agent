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

    @abstractmethod
    async def check_health(self, operator: Operator) -> dict:
        """Check provider session health.

        Returns a dict with at minimum:
            connected: bool
            status_text: str (human-readable status description)
        Provider implementations may include extra fields.
        """
        ...

    @abstractmethod
    async def get_contacts(self, operator: Operator) -> set[str]:
        """Fetch the operator's saved contacts as a set of +E.164 phones."""
        ...
