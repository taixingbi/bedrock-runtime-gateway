"""Rough token-count estimate for TPM (tokens-per-minute) rate limiting
(TenantPolicy.tpm_limit) -- gates BEFORE a call, so it must estimate,
never measure: Bedrock doesn't return real input-token counts until
AFTER a call completes, and completion (output) tokens are unknowable
until the model actually finishes generating.

Heuristic, not exact: ~4 characters per token is a common rule-of-thumb
for English text (real tokenizers vary by model/language) -- good
enough to bound a rate limit, not meant to match Bedrock's own billed
token count (telemetry/cost.py's estimate_cost uses the REAL post-call
input_tokens/output_tokens for that, unrelated to this).

Conservative by design: reserves the full requested max_tokens as the
worst-case output cost, and never refunds unused budget after a call
actually completes with fewer output tokens than requested -- a
tenant's real throughput is bounded by worst-case usage, not actual,
which is a known, deliberate simplification (a refund-on-completion
mechanism is a real, more precise follow-up, not built here).
"""
from __future__ import annotations

from typing import Iterable, Protocol

_CHARS_PER_TOKEN_ESTIMATE = 4


class _HasContent(Protocol):
    content: str


def estimate_tokens(messages: Iterable[_HasContent], max_tokens: int) -> int:
    input_chars = sum(len(m.content) for m in messages)
    estimated_input_tokens = max(1, input_chars // _CHARS_PER_TOKEN_ESTIMATE)
    return estimated_input_tokens + max_tokens
