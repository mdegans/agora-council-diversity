"""Cost estimates for a replay run. Estimates only: nothing here is billed.

Token counts come from the archive's recorded usage of the original sends
(Opus 4.6 tokenizer), scaled by the adapted request's size and by a
tokenizer factor for the target model. Claude Haiku 5.5 counts the same text
as about 30% more tokens than the older tokenizer Opus 4.6 shares with Haiku
4.5, so its factor is 1.3.
"""

from __future__ import annotations

from dataclasses import dataclass

BATCH_DISCOUNT = 0.5
OUTPUT_TOKENS_PER_ANSWER = 4000
HAIKU_TIER_LIMIT = 100_000  # prompts above this are billed at the higher card

# USD per million tokens (Steward, 2026-11-04): input, 1h cache write, cache read, output
_OPUS46 = {"input": 5.0, "write_1h": 10.0, "read": 0.50, "output": 25.0}
_HAIKU55_LOW = {"input": 0.10, "write_1h": 0.20, "read": 0.01, "output": 0.50}
_HAIKU55_HIGH = {"input": 0.50, "write_1h": 1.00, "read": 0.05, "output": 2.50}

TOKENIZER_FACTOR = {"claude-opus-4-6": 1.0, "claude-haiku-5-5": 1.3}


def card(model: str, prompt_tokens: int) -> dict[str, float]:
    if model == "claude-opus-4-6":
        return _OPUS46
    if model == "claude-haiku-5-5":
        return _HAIKU55_LOW if prompt_tokens <= HAIKU_TIER_LIMIT else _HAIKU55_HIGH
    raise KeyError(f"no prices for {model}")


@dataclass
class ItemEstimate:
    prompt_tokens: int
    prefix_tokens: int  # cached: written by sample 1, read by the rest
    rest_tokens: int  # after the last breakpoint: uncached input every time
    samples: int
    tier: str
    usd: float


def estimate_item(
    model: str,
    recorded_tokens: int,
    original_chars: int,
    adapted_chars: int,
    prefix_chars: int,
    samples: int,
    output_tokens: int = OUTPUT_TOKENS_PER_ANSWER,
    batch: bool = True,
) -> ItemEstimate:
    """Cost of `samples` sends of one seat-item: sample 1 writes the 1h
    cache, samples 2..N read it (they go in a later batch)."""
    factor = TOKENIZER_FACTOR[model]
    prompt = round(recorded_tokens * adapted_chars / original_chars * factor)
    prefix = round(prompt * prefix_chars / adapted_chars)
    rest = prompt - prefix
    c = card(model, prompt)
    per_m = 1e-6
    first = (prefix * c["write_1h"] + rest * c["input"] + output_tokens * c["output"]) * per_m
    later = (prefix * c["read"] + rest * c["input"] + output_tokens * c["output"]) * per_m
    usd = first + (samples - 1) * later
    if batch:
        usd *= BATCH_DISCOUNT
    tier = ">100k" if model == "claude-haiku-5-5" and prompt > HAIKU_TIER_LIMIT else "base"
    return ItemEstimate(prompt, prefix, rest, samples, tier, usd)
