from __future__ import annotations

import time

import anthropic
from anthropic import AsyncAnthropic

from app.adapters.llm.base import (
    LLMAdapter,
    LLMResponse,
    LLMTimeoutError,
    ToolResult,
)
from app.utils.log import log

LLM_TIMEOUT_S = 30.0
DEFAULT_MAX_TOKENS = 1024


class AnthropicLLMAdapter(LLMAdapter):
    def __init__(self, api_key: str, model: str) -> None:
        self._model = model
        self._client = AsyncAnthropic(api_key=api_key, timeout=LLM_TIMEOUT_S)

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResponse:
        start = time.monotonic()

        # Translate provider-neutral tool schemas into Anthropic shape
        # (parameters → input_schema)
        anthropic_tools = [
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["parameters"],
            }
            for t in tools
        ]

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                tools=anthropic_tools if anthropic_tools else anthropic.NOT_GIVEN,
            )
        except anthropic.APITimeoutError as e:
            raise LLMTimeoutError(f"Anthropic call timed out after {LLM_TIMEOUT_S}s") from e

        latency_ms = int((time.monotonic() - start) * 1000)

        text_parts: list[str] = []
        tool_calls: list[dict] = []
        raw_blocks: list[dict] = []

        for block in response.content:
            block_dict = block.model_dump()
            raw_blocks.append(block_dict)
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    {"id": block.id, "name": block.name, "input": block.input}
                )

        text = "".join(text_parts) if text_parts else None

        # Anthropic assistant message: full content list preserved
        assistant_message = {"role": "assistant", "content": raw_blocks}

        log(
            "llm_called",
            provider="anthropic",
            model=self._model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=latency_ms,
            tool_calls=len(tool_calls),
        )

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            assistant_message=assistant_message,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            stop_reason=response.stop_reason or "",
        )

    def make_tool_result_messages(
        self, results: list[ToolResult]
    ) -> list[dict]:
        # Anthropic: one user message containing all tool_result blocks
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": r.tool_use_id,
                        "content": r.content,
                        **({"is_error": True} if r.is_error else {}),
                    }
                    for r in results
                ],
            }
        ]
