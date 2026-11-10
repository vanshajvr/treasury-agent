"""Public demo API: run the scenario against an email a visitor types. Framework-free, so any server can wrap it.

POST /api/story  {"email": "...", "start_confidence": 0.9 (optional)}
  -> {"stories": {"person": {...}, "remittance": {...}}, "signal_source": "llm" | "stub" | "ignored"}

What a visitor can and cannot do:
  * text goes to the model, whose output is validated in code (signals.py); nothing here can move money
    or reach Airwallex: the story is a pure, offline replay;
  * each IP gets `per_ip_per_min` requests; the whole service gets `llm_per_day` model calls, after which
    it answers with the deterministic keyword rule (same caps, no cost);
  * provider error text is never returned (it could echo request details).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .evidence import load_evidence
from .models import load_scenario
from .policy import load_policy
from .signals import Signal, keyword_signal, propose_signal
from .story import PATHS, build_story

MAX_EMAIL_CHARS = 2000
MAX_BODY_BYTES = 8192


class RateLimiter:
    """Sliding one-minute window per key, plus a daily budget shared by everyone."""

    def __init__(self, per_min: int, per_day: int, clock: Callable[[], float] = time.time):
        self.per_min, self.per_day, self.clock = per_min, per_day, clock
        self.hits: dict = {}
        self.day, self.day_count = 0, 0
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self.clock()
        with self.lock:
            q = self.hits.setdefault(key, deque())
            while q and now - q[0] >= 60:
                q.popleft()
            if len(q) >= self.per_min:
                return False
            q.append(now)
            if len(self.hits) > 10_000:                      # forget idle visitors
                self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] < 60}
            return True

    def take_daily(self) -> bool:
        day = int(self.clock() // 86400)
        with self.lock:
            if day != self.day:
                self.day, self.day_count = day, 0
            if self.day_count >= self.per_day:
                return False
            self.day_count += 1
            return True


@dataclass
class DemoService:
    root: Path
    per_ip_per_min: int = 6
    llm_per_day: int = 300
    signal: Callable[..., Signal] = propose_signal
    limiter: RateLimiter = field(init=False)

    def __post_init__(self) -> None:
        self.limiter = RateLimiter(self.per_ip_per_min, self.llm_per_day)
        self.scenario = load_scenario(self.root / "scenario.json")
        self.policy = load_policy(self.root / "policy.json")
        self.evidence = load_evidence(self.root / "evidence.json")

    def health(self) -> tuple:
        return 200, {"ok": True}

    def story(self, payload: object, client: str) -> tuple:
        """(HTTP status, JSON body)."""
        if not self.limiter.allow(client):
            return 429, {"error": "Too many requests. Try again in a minute."}
        if not isinstance(payload, dict):
            return 400, {"error": "Send a JSON object."}
        email = payload.get("email")
        if not isinstance(email, str) or not email.strip():
            return 400, {"error": "Write an email first."}
        if len(email) > MAX_EMAIL_CHARS:
            return 400, {"error": f"Keep the email under {MAX_EMAIL_CHARS} characters."}
        start = payload.get("start_confidence")
        if start is not None:
            if isinstance(start, bool) or not isinstance(start, (int, float)) or not 0.0 <= start <= 1.0:
                return 400, {"error": "start_confidence must be a number from 0 to 1."}

        receipt = self.scenario.receipts[0]
        current = float(start) if start is not None else receipt.confidence
        use_model = self.limiter.take_daily()
        sig = self.signal(email, current, receipt) if use_model else keyword_signal(email, current)
        if not use_model:
            sig = Signal(sig.confidence, f"{sig.reason} [daily model budget used; keyword rule]", "stub", category=sig.category)
        elif sig.source == "stub" and "[model unavailable" in sig.reason:
            sig = Signal(sig.confidence, sig.reason.split(" [model unavailable")[0] + " [model unavailable; keyword rule]",
                         "stub", category=sig.category)
        pinned = lambda *_a, **_k: sig  # noqa: E731  one model call serves both endings
        stories = {p: build_story(self.scenario, self.policy, email, self.evidence, p, signal=pinned,
                                  start_confidence=start) for p in PATHS}
        return 200, {"stories": stories, "signal_source": sig.source}
