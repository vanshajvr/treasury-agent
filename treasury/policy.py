"""Policy configuration. Every threshold lives in policy.json, never in a prompt."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .models import CRIT_RANK


@dataclass(frozen=True)
class Tier:
    min_conf: float
    weight: float      # share of an expected receipt counted when planning
    limit_usd: float   # max forecast-reliant spend (USD equivalent) the agent may commit alone


@dataclass(frozen=True)
class Policy:
    tiers: tuple       # sorted by min_conf, highest first
    penalties: dict    # criticality -> lateness penalty weight
    spread_bps: float
    swift_fee: dict    # currency -> flat fee per SWIFT payout

    def tier_for(self, confidence: float) -> Tier:
        for t in self.tiers:
            if confidence >= t.min_conf:
                return t
        return self.tiers[-1]


def load_policy(path: str | Path) -> Policy:
    raw = json.loads(Path(path).read_text())
    tiers = tuple(sorted((Tier(t["min_conf"], t["weight"], t["limit_usd"]) for t in raw["tiers"]),
                         key=lambda t: -t.min_conf))
    if tiers[-1].min_conf > 0:
        raise ValueError("lowest tier must start at min_conf 0.0")
    missing = set(CRIT_RANK) - set(raw["penalties"])
    if missing:
        raise ValueError(f"penalties missing for: {sorted(missing)}")
    return Policy(tiers=tiers, penalties=dict(raw["penalties"]),
                  spread_bps=float(raw["spread_bps"]), swift_fee=dict(raw.get("swift_fee", {})))