from __future__ import annotations

from app.adapters.vision.anthropic_adapter import AnthropicVisionAdapter
from app.adapters.vision.base import VisionAdapter
from app.config import Config


def from_config(cfg: Config) -> VisionAdapter:
    if not cfg.anthropic_api_key:
        raise SystemExit("FATAL: ANTHROPIC_API_KEY is required for vision adapter.")
    return AnthropicVisionAdapter(api_key=cfg.anthropic_api_key, model=cfg.vision_model)
