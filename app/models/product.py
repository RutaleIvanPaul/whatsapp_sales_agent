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
    # Optional per-product haggling guidance, e.g. "firm — premium",
    # "clearance, up to 60% off", "bundle: 2 for 15% off". Overrides
    # the operator-level haggling_policy for this specific item.
    haggling_notes: str | None = None
