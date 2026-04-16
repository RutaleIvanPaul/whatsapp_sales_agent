from __future__ import annotations

from app.adapters.llm.anthropic_adapter import AnthropicLLMAdapter
from app.adapters.llm.base import LLMAdapter
from app.config import Config


def from_config(cfg: Config) -> LLMAdapter:
    if not cfg.anthropic_api_key:
        raise SystemExit("FATAL: ANTHROPIC_API_KEY is required for LLM adapter.")
    return AnthropicLLMAdapter(api_key=cfg.anthropic_api_key, model=cfg.llm_model)
