from __future__ import annotations

from app.adapters.llm.base import LLMAdapter
from app.config import Config


def from_config(cfg: Config, *, model: str | None = None) -> LLMAdapter:
    """Build an LLMAdapter for the configured provider.

    `model` overrides cfg.llm_model — useful for the classifier which uses
    a separate (cheaper) model with the same provider + API key.
    """
    provider = (cfg.llm_provider or "").lower()
    api_key = cfg.llm_api_key
    use_model = model or cfg.llm_model

    if not api_key:
        raise SystemExit(
            f"FATAL: LLM_API_KEY is required for provider={provider!r}"
        )

    if provider == "anthropic":
        from app.adapters.llm.anthropic_adapter import AnthropicLLMAdapter
        return AnthropicLLMAdapter(api_key=api_key, model=use_model)

    if provider == "groq":
        from app.adapters.llm.groq_adapter import GroqLLMAdapter
        return GroqLLMAdapter(api_key=api_key, model=use_model)

    raise SystemExit(
        f"FATAL: Unknown LLM_PROVIDER {provider!r}. Supported: anthropic, groq."
    )
