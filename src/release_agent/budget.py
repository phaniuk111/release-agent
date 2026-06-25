"""
Budget manager for Vertex AI Gen AI usage.

Tracks approximate spend based on tokens.
Hard limit: £10.

If approaching limit:
- Uses LangGraph interrupt to ask user for confirmation.
- If no positive response within timeout (in CLI), it kills the process.

This is conservative to ensure we never cross the budget.
"""

import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

# Conservative pricing estimate for Gemini 2.0 Flash on Vertex AI (in GBP)
# These are rounded UP for safety.
GEMINI_FLASH_INPUT_COST_PER_1K = 0.00008   # £ per 1K input tokens
GEMINI_FLASH_OUTPUT_COST_PER_1K = 0.00032  # £ per 1K output tokens

BUDGET_LIMIT_GBP = 10.0
WARNING_THRESHOLD = 8.0   # Start warning at £8


@dataclass
class BudgetTracker:
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_gbp: float = 0.0
    confirmed_over_budget: bool = False
    # The tracker is a process-global shared by all threads (one compiled graph
    # per worker), so guard mutation against concurrent FastAPI requests.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add_usage(self, input_tokens: int, output_tokens: int) -> float:
        """Add token usage and return the incremental cost."""
        input_cost = (input_tokens / 1000) * GEMINI_FLASH_INPUT_COST_PER_1K
        output_cost = (output_tokens / 1000) * GEMINI_FLASH_OUTPUT_COST_PER_1K
        cost = input_cost + output_cost

        with self._lock:
            self.total_input_tokens += input_tokens
            self.total_output_tokens += output_tokens
            self.total_cost_gbp += cost
        return cost

    @property
    def projected_total(self) -> float:
        return self.total_cost_gbp

    def is_over_budget(self) -> bool:
        return self.total_cost_gbp >= BUDGET_LIMIT_GBP

    def should_ask_user(self) -> bool:
        return self.total_cost_gbp >= WARNING_THRESHOLD and not self.confirmed_over_budget

    def get_status(self) -> str:
        return (
            f"Budget: £{self.total_cost_gbp:.4f} / £{BUDGET_LIMIT_GBP:.2f} "
            f"(tokens in: {self.total_input_tokens}, out: {self.total_output_tokens})"
        )


# Global tracker for the process
_budget_tracker: Optional[BudgetTracker] = None

def get_budget_tracker() -> BudgetTracker:
    global _budget_tracker
    if _budget_tracker is None:
        _budget_tracker = BudgetTracker()
    return _budget_tracker


def reset_budget_tracker():
    global _budget_tracker
    _budget_tracker = BudgetTracker()


def check_budget_before_call(estimated_input_tokens: int = 2000, estimated_output_tokens: int = 500) -> None:
    """
    Call this before making an LLM request.
    Raises BudgetInterrupt if we are over or will exceed.
    """
    tracker = get_budget_tracker()
    projected_cost = (estimated_input_tokens / 1000 * GEMINI_FLASH_INPUT_COST_PER_1K) + \
                     (estimated_output_tokens / 1000 * GEMINI_FLASH_OUTPUT_COST_PER_1K)

    if tracker.total_cost_gbp + projected_cost > BUDGET_LIMIT_GBP:
        raise BudgetInterrupt(
            f"Projected cost £{tracker.total_cost_gbp + projected_cost:.4f} would exceed £{BUDGET_LIMIT_GBP} budget.\n"
            f"Current spend: {tracker.get_status()}"
        )

    if tracker.should_ask_user():
        raise BudgetInterrupt(
            f"Budget warning: Current spend £{tracker.total_cost_gbp:.4f}. "
            f"Continue? (this may push toward the £{BUDGET_LIMIT_GBP} limit)"
        )


class BudgetInterrupt(Exception):
    """Special exception to trigger user confirmation via the agent's interrupt system."""
    pass


def confirm_budget_continue(user_response: str) -> bool:
    """Check user response to budget question."""
    resp = user_response.strip().lower()
    if resp in {"y", "yes", "continue", "ok", "proceed"}:
        tracker = get_budget_tracker()
        tracker.confirmed_over_budget = True
        return True
    return False


def get_budget_status() -> str:
    return get_budget_tracker().get_status()


# Optional: simple timeout input for CLI usage
def ask_user_with_timeout(prompt: str, timeout: int = 60) -> Optional[str]:
    """
    Ask user a question with timeout.
    Returns None if no response in time → we will kill.
    """
    print(f"\n⚠️  {prompt}")
    print(f"You have {timeout} seconds to respond (y/yes to continue, anything else to stop).")

    import threading
    import queue

    result_queue: queue.Queue = queue.Queue()

    def _input():
        try:
            user_input = input("Response: ").strip()
            result_queue.put(user_input)
        except EOFError:
            result_queue.put(None)

    thread = threading.Thread(target=_input, daemon=True)
    thread.start()

    try:
        response = result_queue.get(timeout=timeout)
        return response
    except queue.Empty:
        # Honor the Optional[str] contract: return None so the CALLER decides how
        # to stop (the CLI prints a message and breaks). Hard sys.exit() here was
        # both a contract violation and unsafe for any non-CLI caller.
        print("\n⏰ No response received within timeout.")
        return None