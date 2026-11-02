"""Deterministic context budget calculations."""

DEFAULT_CONTEXT_TOKENS = 8192


def estimate_tokens(text: str) -> int:
    """Conservative, provider-neutral estimate used before model-specific tokenizers."""
    return max(1, (len(text) + 3) // 4)


def fits_context(texts: list[str], budget: int = DEFAULT_CONTEXT_TOKENS) -> bool:
    return sum(estimate_tokens(value) for value in texts) <= budget
