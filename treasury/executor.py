"""Turns EXECUTE_NOW decisions into Airwallex calls.

Per obligation:  settled-cash check (R1) -> quote + drift check (E4) -> conversion -> beneficiary
                 -> transfer -> (sandbox only) simulate SENT then PAID.
Every mutating call uses a request_id stored in the ledger first (R3) and is looked up, not resent,
after an ambiguous outcome (R4). Nothing here decides amounts: they come from the planner's Draw.

A transfer that ends FAILED or CANCELLED (Airwallex auto-cancels a FAILED transfer, so a slow poll may only
see CANCELLED) is recorded with its failure_type and failure_reason and parked in ledger stage FAILED.
It is never retried automatically: a person reviews it and calls `release_failed`, which archives the
attempt so the next run creates a NEW transfer with a fresh request_id (the old one belongs to a finished
operation and would be rejected as duplicate_request_id).
"""
from __future__ import annotations

import math
import time
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


STATUSES = ("DRY_RUN_OK", "PAID_SIM", "TRANSFER_CREATED", "UNCONFIRMED", "E4_DRIFT", "NOT_SETTLED",
            "NO_BENEFICIARY", "TRANSFER_FAILED", "SKIPPED")
FAILED_STATES = ("FAILED", "CANCELLED")  # terminal: the payout did not happen
RECON_TOLERANCE = 0.01  # one cent
_STATUS_RANK = {"NEW": 0, "SCHEDULED": 1, "PROCESSING": 2, "SENT": 3, "PAID": 4}


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
        if self.ledger.stage(ob.id) == "FAILED":  # never auto-retried: a person must release it first
            return ExecResult(ob.id, "TRANSFER_FAILED", self._failure_detail(ob.id, "parked until a person releases it"))
        d = decision.draw

        # R1: re-verify against SETTLED balances right now, not the plan's snapshot
        bal = self.settled_balances()
        if self.ledger.stage(ob.id) == "TRANSFERRED":
            pass  # transfer already created and the wallet already debited: only confirmation is left
        elif self.ledger.stage(ob.id) == "CONVERTED":
            # conversion already booked: the whole payout must now sit in the obligation-currency wallet
            need = d.from_wallet + d.converted
            if need > bal.get(ob.ccy, 0.0) + 1e-6:
                return ExecResult(ob.id, "NOT_SETTLED", {"needed": need, "available": bal.get(ob.ccy, 0.0), "ccy": ob.ccy})
        else:
            if d.from_wallet > bal.get(ob.ccy, 0.0) + 1e-6:
                return ExecResult(ob.id, "NOT_SETTLED", {"needed": d.from_wallet, "available": bal.get(ob.ccy, 0.0), "ccy": ob.ccy})
            if d.usd_spent > bal.get(BASE, 0.0) + 1e-6:
                return ExecResult(ob.id, "NOT_SETTLED", {"needed": d.usd_spent, "available": bal.get(BASE, 0.0), "ccy": BASE})

        beneficiary_id = self._beneficiary_id(ob)
        if beneficiary_id is None:
            return ExecResult(ob.id, "NO_BENEFICIARY", {"payee": ob.payee})
        if not self.dry_run and self.ledger.stage(ob.id) in ("NEW", "QUOTED"):
            self.ledger.update(ob.id, balances_before=bal)  # pre-payment snapshot survives a crash and resume

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
            transfer = self._transfer_read(tid)                        # trust the re-read, not the 2xx
            status = "PAID_SIM" if transfer.get("status") == "PAID" else "UNCONFIRMED"
        final = transfer.get("status")
        if final in FAILED_STATES:
            return self._park_failed(ob, d, tid, quote_info)
        recon = self._reconcile(ob, d, self.ledger.entry(ob.id).get("balances_before", bal), transfer)
        self.ledger.update(ob.id, stage="DONE", final_status=final, reconciliation=recon)
        return ExecResult(ob.id, status, {"transfer_id": tid, "transfer_status": final, "reconciliation": recon, **quote_info})

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
        self.ledger.update(ob.id, stage="QUOTED", quote_id=q["quote_id"], quote_rate=implied)
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
        """Sandbox only. Idempotent: reads the status first and applies just the missing transitions,
        so a re-run after a 5xx or a crash never replays one that already happened."""
        for nxt in ("SENT", "PAID"):  # SENT must come before PAID
            for _ in range(3):
                current = self._transfer_status(transfer_id) or ""
                if current in FAILED_STATES:
                    return  # terminal: nothing left to simulate, the caller records the failure
                if _STATUS_RANK.get(current, -1) >= _STATUS_RANK[nxt]:
                    break
                try:
                    self.c.post(f"/api/v1/simulation/transfers/{transfer_id}/transition", {"next_status": nxt})
                    break
                except AmbiguousResult:
                    time.sleep(1)  # not money: re-read the status and decide again
            else:
                raise AmbiguousResult(f"could not move transfer {transfer_id} to {nxt}")

    def _transfer_status(self, transfer_id: str) -> str | None:
        return self._transfer_read(transfer_id).get("status")

    def _transfer_read(self, transfer_id: str) -> dict:
        return self.c.get(f"/api/v1/transfers/{transfer_id}") or {}

    # ------------------------------------------------------------ failed payouts
    def _park_failed(self, ob: Obligation, d, transfer_id: str | None, quote_info: dict) -> ExecResult:
        t = self._transfer_read(transfer_id) if transfer_id else {}
        note = (f"converted {ob.ccy} {money(d.converted)} stays in the {ob.ccy} wallet; re-plan before retrying"
                if d.converted > 1e-9 else "no conversion was made")
        self.ledger.update(ob.id, stage="FAILED", final_status=t.get("status", "FAILED"),
                           failure_type=t.get("failure_type"), failure_reason=t.get("failure_reason"),
                           funds_note=note, balances_after=self.settled_balances())
        return ExecResult(ob.id, "TRANSFER_FAILED", {**self._failure_detail(ob.id, "escalate to a person"), **quote_info})

    def _failure_detail(self, obligation_id: str, next_step: str) -> dict:
        e = self.ledger.entry(obligation_id)
        return {"transfer_id": e.get("transfer_id"), "transfer_status": e.get("final_status"),
                "failure_type": e.get("failure_type"), "failure_reason": e.get("failure_reason"),
                "funds_note": e.get("funds_note"), "next": next_step}

    def release_failed(self, obligation_id: str, approved_by: str) -> None:
        """A person reviewed a failed payout (e.g. fixed the beneficiary) and approves one new attempt.
        The failed attempt is archived; the next execute() creates a new transfer under a fresh request_id
        and does not repeat a conversion that already settled."""
        e = self.ledger.entry(obligation_id)
        if e["stage"] != "FAILED":
            raise ValueError(f"{obligation_id} is in stage {e['stage']}, not FAILED")
        attempt = {k: e.pop(k, None) for k in ("transfer_id", "transfer_request_id", "final_status",
                                               "failure_type", "failure_reason", "funds_note", "balances_after")}
        history = e.get("failed_attempts", []) + [{**attempt, "released_by": approved_by}]
        stage = "CONVERTED" if e.get("conversion_id") else "NEW"
        self.ledger.update(obligation_id, stage=stage, failed_attempts=history)

    def _reconcile(self, ob: Obligation, d, before: dict, transfer: dict | None = None) -> dict:
        """Did the balances move by exactly what Airwallex says this payout cost?

        Net effect of convert + transfer: obligation-currency wallet -(payer pays - converted), USD wallet -usd_spent.
        "Payer pays" is the transfer's own `amount_payer_pays` when Airwallex reports it, else the plan's estimate.
        Fees can float (the sandbox SWIFT EUR fee was 13.37 one day, 13.53 another), so a fee that differs from the
        policy estimate is reported under `fee`, not as a mismatch. A mismatch means money moved that neither the
        plan nor Airwallex's own record accounts for."""
        after = self.settled_balances()
        planned_pays = d.from_wallet + d.converted                    # in the obligation currency
        charged = (transfer or {}).get("amount_payer_pays")
        pays = float(charged) if charged is not None else planned_pays
        planned = {ob.ccy: pays - d.converted}
        if ob.ccy != BASE:
            # USD is checked at the rate actually quoted: a better or worse quote is not a planning error
            qr = self.ledger.entry(ob.id).get("quote_rate")
            planned[BASE] = d.converted * qr if qr and d.converted > 0 else d.usd_spent
        actual = {c: round(before.get(c, 0.0) - after.get(c, 0.0), 4) for c in planned}
        diff = {c: round(actual[c] - planned[c], 4) for c in planned}
        return {"planned_debit": {c: round(v, 4) for c, v in planned.items()}, "actual_debit": actual,
                "diff": diff, "ok": all(abs(v) <= RECON_TOLERANCE for v in diff.values()),
                "checked_against": "airwallex" if charged is not None else "plan",
                "fee": {"ccy": ob.ccy, "estimated": round(planned_pays - ob.amount, 2),
                        "charged": round(pays - ob.amount, 2), "variance": round(pays - planned_pays, 2)}}

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