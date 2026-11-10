"""Policy configuration. Every threshold lives in policy.json, never in a prompt."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .models import CRIT_RANK


@dataclass(frozen=True)
class Tier:
    min_conf: float
    weight: float      # share of an expected receipt counted when planning
    limit_usd: float   # max forecast-reliant spend (USD equivalent) the agent may commit alone


@dataclass(frozen=True)
class EvidenceRules:
    """How far structured evidence may lift a forecast receipt (see treasury/evidence.py)."""
    invoice_conf: float = 0.8       # approved invoice: enough to reach the top tier
    remittance_conf: float = 0.97   # bank remittance advice with a UETR
    amount_tolerance: float = 0.005  # relative difference allowed between record and forecast
    grace_h: float = 6.0            # how late the record's date may be versus the forecast due time


@dataclass(frozen=True)
class Policy:
    tiers: tuple       # sorted by min_conf, highest first
    penalties: dict    # criticality -> lateness penalty weight
    spread_bps: float
    swift_fee: dict    # currency -> flat fee per SWIFT payout
    local_fee: dict = field(default_factory=dict)  # currency -> flat fee per LOCAL payout (observed, see POLICY.md)
    evidence: EvidenceRules = field(default_factory=EvidenceRules)

    @property
    def fees(self) -> dict:
        """method -> currency -> flat fee, the table the planner charges against."""
        return {"SWIFT": self.swift_fee, "LOCAL": self.local_fee}

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
                  spread_bps=float(raw["spread_bps"]), swift_fee=dict(raw.get("swift_fee", {})),
                  local_fee=dict(raw.get("local_fee", {})), evidence=EvidenceRules(**raw.get("evidence", {})))