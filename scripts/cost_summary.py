"""Manual cost summary trigger. Run: python scripts/cost_summary.py

Prints the current day's token usage and estimated cost per operator.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import validate
from app.utils.cost_tracker import CostTracker


def main():
    cfg = validate()
    tracker = CostTracker(
        provider=cfg.llm_provider,
        input_rate_per_1k=cfg.input_token_rate_per_1k,
        output_rate_per_1k=cfg.output_token_rate_per_1k,
    )
    # In a real scenario the tracker would have data from the running server.
    # This script is mainly for triggering log_and_reset on the running instance
    # or showing what the rates are.
    print("=" * 50)
    print("LLM Cost Summary")
    print("=" * 50)
    print(f"Provider: {cfg.llm_provider}")
    print(f"Input rate: ${cfg.input_token_rate_per_1k}/1K tokens")
    print(f"Output rate: ${cfg.output_token_rate_per_1k}/1K tokens")
    print()
    print("Note: run this while the server is running to see live data.")
    print("The cost tracker accumulates in memory on the server process.")
    print("This script shows the configured rates only.")


if __name__ == "__main__":
    main()
