from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class LLMTimeoutError(Exception):
    """Raised when an LLM call exceeds its configured timeout."""


@dataclass
class ToolResult:
    """A tool call result, in provider-neutral form. Adapters convert to
    their native message shape via `make_tool_result_messages()`.
    """
    tool_use_id: str    # the id from the tool_call
    content: str         # serialised string result for the model
    is_error: bool = False


@dataclass
class LLMResponse:
    text: str | None
    tool_calls: list[dict]              # [{"id", "name", "input"}]
    assistant_message: dict             # provider-shaped message dict to append
                                        # verbatim to messages on the next turn
                                        # (preserves tool_use blocks / tool_calls)
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

    @abstractmethod
    def make_tool_result_messages(
        self, results: list[ToolResult]
    ) -> list[dict]:
        """Convert tool results into the message(s) to append after tool
        execution.

        Anthropic returns ONE user message containing all tool_results.
        OpenAI/Groq return one tool message per tool_call_id.
        """
        ...
