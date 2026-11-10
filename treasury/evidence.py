"""Structured evidence -> forecast confidence. Deterministic: no model, no network.

The email signal (signals.py) reads untrusted free text, so it may lower confidence freely but raise it by at most
MAX_RAISE per email. Structured records from systems of record are different: they are matched field by field in
code, so a match may lift a receipt into a higher confidence tier and with it the autonomous limit (E2).

  approved invoice   (accounts receivable: the payer approved our invoice for payment)
                     -> confidence at least `invoice_conf`, unless a customer email already contradicted the
                        forecast: an approved invoice proves the debt, not the timing.
  bank remittance    (the payer's bank reports the payment as sent, with a UETR reference)
                     -> confidence at least `remittance_conf`, even after a delay email: the bank outranks the email.

A record counts only if payer, currency, amount (within `amount_tolerance`) and date (within `grace_h` of the
receipt's due time) all match and its reference has not already corroborated another receipt. Evidence never
lowers confidence, and the money is still not spendable until it settles (rule R1).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .models import Receipt
from .policy import EvidenceRules

KINDS = ("invoice", "remittance")
UETR = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


@dataclass(frozen=True)
class Evidence:
    kind: str          # "invoice" | "remittance"
    ref: str           # invoice number, or the remittance's UETR
    payer: str
    ccy: str
    amount: float
    date_h: float      # invoice: payment date the payer approved; remittance: value date (hours from now)
    status: str        # invoice: "approved" | "issued" | "disputed"; remittance: "sent" | "recalled"


@dataclass(frozen=True)
class Corroboration:
    confidence: float
    reason: str
    matched: tuple = ()      # refs that raised (or would have raised) confidence
    rejected: tuple = ()     # (ref, why) for every record considered and not used


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _mismatch(ev: Evidence, r: Receipt, rules: EvidenceRules) -> str | None:
    """Why `ev` does not corroborate `r`, or None if it does."""
    if _norm(ev.payer) != _norm(r.payer):
        return f"payer {ev.payer!r} is not {r.payer!r}"
    if ev.ccy != r.ccy:
        return f"currency {ev.ccy} is not {r.ccy}"
    if abs(ev.amount - r.amount) > rules.amount_tolerance * r.amount:
        return f"amount {ev.ccy} {ev.amount:,.2f} does not match the forecast {r.ccy} {r.amount:,.2f}"
    if ev.date_h > r.due_h + rules.grace_h:
        return f"dated {ev.date_h:g}h, later than the forecast {r.due_h:g}h + {rules.grace_h:g}h grace"
    if ev.kind == "invoice" and ev.status != "approved":
        return f"invoice status is {ev.status!r}, not 'approved' by the payer"
    if ev.kind == "remittance":
        if ev.status != "sent":
            return f"remittance status is {ev.status!r}, not 'sent'"
        if not UETR.match(ev.ref.lower()):
            return "remittance reference is not a valid UETR"
    return None


def corroborate(receipt: Receipt, evidence: list, current: float, rules: EvidenceRules,
                contradicted: bool = False, used_refs: frozenset = frozenset()) -> Corroboration:
    """New confidence for `receipt` given structured evidence. `contradicted`: a customer email has already
    lowered this forecast. `used_refs`: references that corroborated a different receipt and cannot be reused."""
    best, matched, rejected, why = current, [], [], []
    for ev in evidence:
        if ev.kind not in KINDS:
            rejected.append((ev.ref, f"unknown evidence kind {ev.kind!r}"))
            continue
        if ev.ref in used_refs:
            rejected.append((ev.ref, "reference already corroborated another receipt"))
            continue
        miss = _mismatch(ev, receipt, rules)
        if miss:
            rejected.append((ev.ref, miss))
            continue
        if ev.kind == "invoice" and contradicted:
            rejected.append((ev.ref, "an approved invoice proves the debt, not the timing; it does not "
                                     "override the payer's email saying the payment is delayed"))
            continue
        target = rules.invoice_conf if ev.kind == "invoice" else rules.remittance_conf
        matched.append(ev.ref)
        if target > best:
            best = target
            why = [f"{ev.kind} {ev.ref} matches payer, currency, amount and date"]
    if best > current:
        return Corroboration(best, f"{why[0]}: confidence {current:.2f} -> {best:.2f}", tuple(matched), tuple(rejected))
    reason = "no evidence raised confidence" if not matched else "matched evidence is no stronger than current confidence"
    return Corroboration(current, reason, tuple(matched), tuple(rejected))


def load_evidence(path: str | Path) -> list:
    raw = json.loads(Path(path).read_text())
    out = []
    for i, e in enumerate(raw):
        if e.get("kind") not in KINDS:
            raise ValueError(f"evidence {i}: kind must be one of {KINDS}")
        out.append(Evidence(kind=e["kind"], ref=str(e["ref"]), payer=e["payer"], ccy=e["ccy"],
                            amount=float(e["amount"]), date_h=float(e["date_h"]), status=e["status"]))
    return out
