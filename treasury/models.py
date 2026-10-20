"""Data models for the treasury controller (offline core, no network)."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path

BASE = "USD"  # funding currency: conversions are always USD -> obligation currency
CRIT_RANK = {"operations_stop": 0, "time_sensitive": 1, "important": 2, "deferrable": 3}
METHODS = ("LOCAL", "SWIFT")


@dataclass(frozen=True)
class Obligation:
    id: str
    payee: str
    ccy: str
    amount: float
    due_h: float
    criticality: str
    method: str = "LOCAL"
    new_beneficiary: bool = False


@dataclass(frozen=True)
class Receipt:
    id: str
    payer: str
    ccy: str
    amount: float
    due_h: float
    confidence: float


@dataclass
class Scenario:
    wallets: dict            # settled cash the agent may use (the treasury mandate)
    reserve_floor_usd: float
    horizon_h: float
    obligations: list
    receipts: list           # expected, not yet settled
    rates: dict              # USD per 1 unit of currency, all-in (client) rate

    def clone(self) -> "Scenario":
        return copy.deepcopy(self)


def load_scenario(path: str | Path) -> Scenario:
    raw = json.loads(Path(path).read_text())
    rates = dict(raw.get("rates") or {})
    if not rates:
        raise ValueError("scenario needs a 'rates' object (USD per 1 unit of each currency)")
    rates.setdefault(BASE, 1.0)

    obligations = []
    for o in raw["obligations"]:
        ob = Obligation(
            id=o["id"], payee=o.get("payee", o["id"]), ccy=o["ccy"], amount=float(o["amount"]),
            due_h=float(o["due_h"]), criticality=o["criticality"],
            method=o.get("method", "LOCAL"), new_beneficiary=bool(o.get("new_beneficiary", False)),
        )
        if ob.criticality not in CRIT_RANK:
            raise ValueError(f"{ob.id}: unknown criticality {ob.criticality!r}")
        if ob.method not in METHODS:
            raise ValueError(f"{ob.id}: unknown method {ob.method!r}")
        if ob.ccy not in rates:
            raise ValueError(f"{ob.id}: no rate for {ob.ccy}")
        obligations.append(ob)

    raw_receipts = raw.get("expected_receipts")
    if raw_receipts is None:
        raw_receipts = [raw["expected_receipt"]] if raw.get("expected_receipt") else []
    receipts = []
    for i, r in enumerate(raw_receipts):
        if r["ccy"] not in rates:
            raise ValueError(f"receipt {i}: no rate for {r['ccy']}")
        receipts.append(Receipt(
            id=r.get("id", f"receipt_{i + 1}"), payer=r.get("payer", "unknown"), ccy=r["ccy"],
            amount=float(r["amount"]), due_h=float(r["due_h"]),
            confidence=float(r.get("forecast_confidence", r.get("confidence", 1.0))),
        ))

    wallets = {c: float(v) for c, v in raw["mandate"].items()}
    for c in wallets:
        if c not in rates:
            raise ValueError(f"mandate currency {c} has no rate")
    return Scenario(
        wallets=wallets,
        reserve_floor_usd=float(raw["reserve_floor_usd_equiv"]),
        horizon_h=float(raw.get("horizon_hours", 72)),
        obligations=obligations,
        receipts=receipts,
        rates=rates,
    )