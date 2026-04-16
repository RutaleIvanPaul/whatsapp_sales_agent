from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class LLMTimeoutError(Exception):
    """Raised when an LLM call exceeds its configured timeout."""


@dataclass
class LLMResponse:
    text: str | None
    tool_calls: list[dict]              # [{"id", "name", "input"}]
    raw_content: list[dict]             # full response.content list, must be appended
                                        # verbatim to messages on the next turn so
                                        # tool_use blocks are preserved
    input_tokens: int
    output_tokens: int
    stop_reason: str = ""


class LLMAdapter(ABC):
    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
        max_tokens: int = 1024,
    ) -> LLMResponse: ...
