"""Per-model pricing (USD per million tokens) used for the running cost estimate."""
from __future__ import annotations

# (input, output) USD per MTok at the standard rate. Haiku 5.5 is billed at this
# rate for prompts up to 100K tokens, which every request this tool sends is.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

DEFAULT_MODEL = "claude-haiku-5-5"
API_MODELS = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]
MODEL_LABELS = {
    "claude-haiku-5-5": "Claude Haiku 5.5 (fastest, cheapest)",
    "claude-sonnet-5-5": "Claude Sonnet 5.5 (higher accuracy)",
    "claude-opus-5-5": "Claude Opus 5.5 (highest accuracy, most expensive)",
}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    prices = PRICES.get(model)
    if prices is None:
        # Unknown model: assume Sonnet-class pricing so the estimate errs high.
        prices = PRICES["claude-sonnet-5-5"]
    return (input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000
