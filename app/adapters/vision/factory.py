from __future__ import annotations

from app.adapters.vision.base import VisionAdapter
from app.config import Config


def from_config(cfg: Config) -> VisionAdapter:
    provider = (cfg.vision_provider or "").lower()
    api_key = cfg.vision_api_key
    model = cfg.vision_model

    if not api_key:
        raise SystemExit(
            f"FATAL: VISION_API_KEY is required for provider={provider!r}"
        )

    if provider == "anthropic":
        from app.adapters.vision.anthropic_adapter import AnthropicVisionAdapter
        return AnthropicVisionAdapter(api_key=api_key, model=model)

    if provider == "groq":
        from app.adapters.vision.groq_adapter import GroqVisionAdapter
        return GroqVisionAdapter(api_key=api_key, model=model)

    raise SystemExit(
        f"FATAL: Unknown VISION_PROVIDER {provider!r}. Supported: anthropic, groq."
    )
