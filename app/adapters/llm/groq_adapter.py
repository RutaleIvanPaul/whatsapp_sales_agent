from __future__ import annotations

import json
import time

import groq
from groq import AsyncGroq

from app.adapters.llm.base import (
    LLMAdapter,
    LLMResponse,
    LLMTimeoutError,
    ToolResult,
)
from app.utils.log import log

LLM_TIMEOUT_S = 30.0
DEFAULT_MAX_TOKENS = 1024


class GroqLLMAdapter(LLMAdapter):
    """OpenAI-compatible LLM via Groq (Llama models)."""

    def __init__(self, api_key: str, model: str) -> None:
        self._model = model
        self._client = AsyncGroq(api_key=api_key, timeout=LLM_TIMEOUT_S)

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResponse:
        # OpenAI-shape: system goes in messages list, not as a separate param
        full_messages: list[dict] = []
        if system:
            full_messages.append({"role": "system", "content": system})
        full_messages.extend(messages)

        # Translate provider-neutral tool schemas into OpenAI/Groq shape
        groq_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            }
            for t in tools
        ]

        kwargs = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": full_messages,
        }
        if groq_tools:
            kwargs["tools"] = groq_tools

        start = time.monotonic()
        try:
            response = await self._client.chat.completions.create(**kwargs)
        except groq.APITimeoutError as e:
            raise LLMTimeoutError(f"Groq call timed out after {LLM_TIMEOUT_S}s") from e

        latency_ms = int((time.monotonic() - start) * 1000)

        choice = response.choices[0]
        msg = choice.message
        text = msg.content or None

        tool_calls: list[dict] = []
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(
                {"id": tc.id, "name": tc.function.name, "input": args}
            )

        # OpenAI/Groq assistant message: dict with content + tool_calls list
        # (must be passed back verbatim so the model can match tool_call_ids
        # against subsequent tool messages)
        assistant_message: dict = {"role": "assistant"}
        if text is not None:
            assistant_message["content"] = text
        else:
            # OpenAI requires content key even when null when tool_calls present
            assistant_message["content"] = None
        if msg.tool_calls:
            assistant_message["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]

        usage = response.usage
        log(
            "llm_called",
            provider="groq",
            model=self._model,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            latency_ms=latency_ms,
            tool_calls=len(tool_calls),
        )

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            assistant_message=assistant_message,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            stop_reason=choice.finish_reason or "",
        )

    def make_tool_result_messages(
        self, results: list[ToolResult]
    ) -> list[dict]:
        # OpenAI/Groq: one tool message per tool_call_id
        return [
            {
                "role": "tool",
                "tool_call_id": r.tool_use_id,
                "content": r.content,
            }
            for r in results
        ]
