from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Product:
    id: str
    name: str
    price: str
    description: str
    keywords: str
    image_url: str
    available: bool
    slug: str | None
    attributes: str | None
