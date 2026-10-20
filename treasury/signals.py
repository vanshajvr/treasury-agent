"""Text -> forecast-confidence signal.

STUB: a keyword rule so the offline demo and tests are deterministic.
In the LLM stage this function is replaced by a model call that returns the same
(confidence, reason) pair. Whatever it returns, it can only *propose* a confidence value;
amounts, limits, floors and tiers stay in policy code.
"""
from __future__ import annotations

NEGATIVE = ("delay", "delayed", "cannot", "can't", "won't", "unable", "dispute", "next month", "postpone")
POSITIVE = ("payment sent", "has been paid", "confirmed transfer", "remittance advice")


def propose_confidence(email_text: str, current: float) -> tuple:
    t = email_text.lower()
    if any(k in t for k in NEGATIVE):
        return min(current, 0.4), "email signals a delay or inability to pay"
    if any(k in t for k in POSITIVE):
        return max(current, 0.95), "email confirms payment was sent"
    return current, "no clear signal; confidence unchanged"