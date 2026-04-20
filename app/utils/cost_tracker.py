"""Per-operator daily LLM cost tracking.

Records token usage from every LLM call. Background task logs a summary
at midnight UTC and resets counters. Manual trigger via scripts/cost_summary.py.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.utils.log import log

# Module-level singleton — set by main.py at startup, used by conversation.py
_instance: CostTracker | None = None


def get_tracker() -> CostTracker | None:
    return _instance


def set_tracker(tracker: CostTracker) -> None:
    global _instance
    _instance = tracker


class CostTracker:
    def __init__(
        self,
        provider: str,
        input_rate_per_1k: float,
        output_rate_per_1k: float,
    ) -> None:
        self._provider = provider
        self._input_rate = input_rate_per_1k
        self._output_rate = output_rate_per_1k
        # {(operator_id, date_str): {input_tokens, output_tokens, vision_calls}}
        self._counters: dict[tuple[str, str], dict] = {}

    def record(
        self,
        operator_id: str,
        input_tokens: int,
        output_tokens: int,
        is_vision: bool = False,
    ) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = (operator_id, today)
        if key not in self._counters:
            self._counters[key] = {
                "input_tokens": 0,
                "output_tokens": 0,
                "vision_calls": 0,
            }
        c = self._counters[key]
        c["input_tokens"] += input_tokens
        c["output_tokens"] += output_tokens
        if is_vision:
            c["vision_calls"] += 1

    def get_summary(self, operator_id: str | None = None) -> list[dict]:
        """Get current day's summary. If operator_id given, filter to that operator."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        results = []
        for (op_id, date_str), c in self._counters.items():
            if date_str != today:
                continue
            if operator_id and op_id != operator_id:
                continue
            cost = (
                (c["input_tokens"] / 1000) * self._input_rate
                + (c["output_tokens"] / 1000) * self._output_rate
            )
            results.append({
                "operator_id": op_id,
                "date": date_str,
                "input_tokens": c["input_tokens"],
                "output_tokens": c["output_tokens"],
                "vision_calls": c["vision_calls"],
                "estimated_cost_usd": round(cost, 4),
                "provider": self._provider,
            })
        return results

    def log_and_reset(self) -> None:
        """Log summaries for all operators and clear previous day's data."""
        yesterday = None
        for (op_id, date_str), c in list(self._counters.items()):
            cost = (
                (c["input_tokens"] / 1000) * self._input_rate
                + (c["output_tokens"] / 1000) * self._output_rate
            )
            log(
                "llm_cost_summary",
                operator_id=op_id,
                date=date_str,
                input_tokens=c["input_tokens"],
                output_tokens=c["output_tokens"],
                vision_calls=c["vision_calls"],
                estimated_cost_usd=round(cost, 4),
                provider=self._provider,
            )
            yesterday = date_str
        # Clear old entries (keep today's if midnight just rolled over)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._counters = {
            k: v for k, v in self._counters.items() if k[1] == today
        }

    async def start_midnight_task(self) -> None:
        """Background task: log cost summary at midnight UTC daily."""
        while True:
            now = datetime.now(timezone.utc)
            # Seconds until next midnight UTC
            tomorrow = now.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            if tomorrow <= now:
                tomorrow = tomorrow.replace(day=tomorrow.day + 1)
            wait_s = (tomorrow - now).total_seconds()
            await asyncio.sleep(wait_s)
            self.log_and_reset()
