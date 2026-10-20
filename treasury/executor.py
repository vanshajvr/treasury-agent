"""Turns EXECUTE_NOW decisions into Airwallex calls.

Per obligation:  settled-cash check (R1) -> quote + drift check (E4) -> conversion -> beneficiary
                 -> transfer -> (sandbox only) simulate SENT then PAID.
Every mutating call uses a request_id stored in the ledger first (R3) and is looked up, not resent,
after an ambiguous outcome (R4). Nothing here decides amounts: they come from the planner's Draw.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .airwallex import AirwallexError, AmbiguousResult
from .ledger import Ledger
from .models import BASE, Obligation
from .planner import EXECUTE_NOW, Decision

DRIFT_LIMIT = 0.005  # E4: 0.5%

class Api(Protocol):
    """What the executor needs from an API client (AirwallexClient, or a fake in tests)."""
    def get(self, path: str, **kw: Any) -> Any: ...
    def post(self, path: str, body: dict, **kw: Any) -> Any: ...
    def post_idempotent(self, path: str, body: dict, request_id: str,
                        lookup: Callable[[str], dict | None]) -> Any: ...

@dataclass
class ExecResult:
    obligation_id: str
    status: str          # see STATUSES
    detail: dict = field(default_factory=dict)


STATUSES = ("DRY_RUN_OK", "PAID_SIM", "TRANSFER_CREATED", "E4_DRIFT", "NOT_SETTLED",
            "NO_BENEFICIARY", "FAILED", "SKIPPED")


def money(x: float) -> str:
    """Round UP to cents so a conversion never comes up a cent short."""
    return f"{math.ceil(round(x * 100, 6)) / 100:.2f}"


class Executor:
    def __init__(self, client: Api, ledger: Ledger, beneficiaries: dict,
                 *, dry_run: bool = True, simulate: bool = True, reason: str = "professional_business_services",
                 quote_validity: str = "MIN_15"):
        """beneficiaries: payee -> {"beneficiary_id": "..."}  or  {"beneficiary": {...create payload...},
        "transfer_methods": ["LOCAL"]}. The second form is created once and its id stored in the ledger."""
        self.c = client
        self.ledger = ledger
        self.beneficiaries = beneficiaries
        self.dry_run = dry_run
        self.simulate = simulate
        self.reason = reason
        self.quote_validity = quote_validity

    # ------------------------------------------------------------ reads
    def settled_balances(self) -> dict:
        data = self.c.get("/api/v1/balances/current")
        rows = data if isinstance(data, list) else data.get("items", [])
        return {r["currency"]: float(r.get("available_amount", 0)) for r in rows}

    # ------------------------------------------------------------ main entry
    def execute(self, ob: Obligation, decision: Decision) -> ExecResult:
        if decision.action != EXECUTE_NOW or decision.draw is None:
            return ExecResult(ob.id, "SKIPPED", {"why": f"action is {decision.action}"})
        if self.ledger.stage(ob.id) == "DONE":
            return ExecResult(ob.id, "SKIPPED", {"why": "already completed in ledger"})
        d = decision.draw

        # R1: re-verify against SETTLED balances right now, not the plan's snapshot
        bal = self.settled_balances()
        if d.from_wallet > bal.get(ob.ccy, 0.0) + 1e-6:
            return ExecResult(ob.id, "NOT_SETTLED", {"needed": d.from_wallet, "available": bal.get(ob.ccy, 0.0), "ccy": ob.ccy})
        if d.usd_spent > bal.get(BASE, 0.0) + 1e-6:
            return ExecResult(ob.id, "NOT_SETTLED", {"needed": d.usd_spent, "available": bal.get(BASE, 0.0), "ccy": BASE})

        beneficiary_id = self._beneficiary_id(ob)
        if beneficiary_id is None:
            return ExecResult(ob.id, "NO_BENEFICIARY", {"payee": ob.payee})

        # conversion leg (only when the planner drew from USD)
        quote_info = {}
        if d.converted > 1e-9 and self.ledger.stage(ob.id) in ("NEW", "QUOTED"):
            drift = self._quote(ob, d)
            quote_info = drift
            if drift["drift"] > DRIFT_LIMIT:
                self.ledger.update(ob.id, stage="NEW")  # quote is single-use and discarded
                return ExecResult(ob.id, "E4_DRIFT", drift)
            if self.dry_run:
                return ExecResult(ob.id, "DRY_RUN_OK", {"would_convert": money(d.converted), "ccy": ob.ccy, **drift,
                                                        "would_transfer": money(ob.amount), "method": ob.method})
            self._convert(ob, d)
        elif self.dry_run:
            return ExecResult(ob.id, "DRY_RUN_OK", {"would_convert": None, "would_transfer": money(ob.amount),
                                                    "method": ob.method})

        if self.ledger.stage(ob.id) == "TRANSFERRED":  # resumed after a crash: transfer already exists
            transfer = {"id": self.ledger.entry(ob.id).get("transfer_id")}
        else:
            transfer = self._transfer(ob, beneficiary_id)
            self.ledger.update(ob.id, stage="TRANSFERRED", transfer_id=transfer.get("id"))
        status = "TRANSFER_CREATED"
        tid = transfer.get("id")
        if self.simulate and tid:
            self._simulate(tid)
            status = "PAID_SIM"
        self.ledger.update(ob.id, stage="DONE")
        return ExecResult(ob.id, status, {"transfer_id": transfer.get("id"), "transfer_status": transfer.get("status"), **quote_info})

    # ------------------------------------------------------------ steps
    def _beneficiary_id(self, ob: Obligation) -> str | None:
        e = self.ledger.entry(ob.id)
        if e.get("beneficiary_id"):
            return e["beneficiary_id"]
        spec = self.beneficiaries.get(ob.payee)
        if not spec:
            return None
        if spec.get("beneficiary_id"):
            bid = spec["beneficiary_id"]
        elif self.dry_run:
            return "DRY_RUN_PENDING_CREATE"
        else:
            rid = self.ledger.request_id(ob.id, "beneficiary")
            body = {"beneficiary": spec["beneficiary"], "transfer_methods": spec.get("transfer_methods", [ob.method])}
            res = self.c.post_idempotent("/api/v1/beneficiaries/create", body, rid,
                                         lookup=lambda r: None)  # no request_id lookup exists; creation is not money
            bid = res["beneficiary_id"] if "beneficiary_id" in res else res["id"]
        self.ledger.update(ob.id, beneficiary_id=bid)
        return bid

    def _quote(self, ob: Obligation, d) -> dict:
        """Create a quote and compare its implied USD-per-unit rate with the planning rate (E4)."""
        q = self.c.post("/api/v1/fx/quotes/create", {
            "sell_currency": BASE, "buy_currency": ob.ccy, "buy_amount": money(d.converted),
            "validity": self.quote_validity})
        planned = d.usd_spent / d.converted  # planning all-in USD per unit
        if q.get("sell_amount") and q.get("buy_amount"):
            implied = float(q["sell_amount"]) / float(q["buy_amount"])
        else:
            implied = float(q["client_rate"])  # assumed USD per unit; verify on first live run
        drift = abs(implied - planned) / planned
        self.ledger.update(ob.id, stage="QUOTED", quote_id=q["quote_id"])
        return {"planned_rate": round(planned, 6), "quote_rate": round(implied, 6), "drift": round(drift, 6)}

    def _convert(self, ob: Obligation, d) -> None:
        e = self.ledger.entry(ob.id)
        rid = self.ledger.request_id(ob.id, "conversion")
        body = {"sell_currency": BASE, "buy_currency": ob.ccy, "buy_amount": money(d.converted),
                "quote_id": e["quote_id"]}
        res = self.c.post_idempotent("/api/v1/fx/conversions/create", body, rid,
                                     lookup=lambda r: self._find("/api/v1/fx/conversions", r))
        self.ledger.update(ob.id, stage="CONVERTED", conversion_id=res.get("conversion_id") or res.get("id"))

    def _transfer(self, ob: Obligation, beneficiary_id: str) -> dict:
        rid = self.ledger.request_id(ob.id, "transfer")
        body = {"beneficiary_id": beneficiary_id, "source_currency": ob.ccy, "transfer_currency": ob.ccy,
                "transfer_amount": round(ob.amount, 2), "transfer_method": ob.method,
                "reason": self.reason, "reference": ob.id}
        return self.c.post_idempotent("/api/v1/transfers/create", body, rid,
                                      lookup=lambda r: self._find("/api/v1/transfers", r))

    def _simulate(self, transfer_id: str) -> None:
        for nxt in ("SENT", "PAID"):  # SENT must come before PAID
            self.c.post(f"/api/v1/simulation/transfers/{transfer_id}/transition", {"next_status": nxt})

    def _find(self, path: str, request_id: str) -> dict | None:
        """R4: has an operation with this request_id already happened? Filter client-side too,
        because the server-side filter name is not confirmed."""
        try:
            data = self.c.get(path, params={"request_id": request_id, "page_size": 100})
        except AirwallexError:
            raise AmbiguousResult(f"cannot confirm {request_id} via {path}; do not resend") from None
        for item in data.get("items", []):
            if item.get("request_id") == request_id:
                return item
        return None