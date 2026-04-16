from __future__ import annotations

from abc import ABC, abstractmethod


class VisionAdapter(ABC):
    @abstractmethod
    async def describe(self, image_url: str) -> str: ...
