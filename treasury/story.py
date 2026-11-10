"""The demo scenario as structured steps: one source for run_demo.py, the web page and the API.

goal -> initial plan -> customer email -> revised decision -> evidence check
     -> (a person answers | the payer's bank sends a remittance advice)
     -> deposit settles -> incremental re-plan -> execution -> remaining reserve

Nothing here makes a decision of its own: every number comes from plan(), replan(), the signal and corroborate().
"""
from __future__ import annotations

from typing import Callable

from .evidence import corroborate
from .models import Scenario
from .planner import ESCALATE, EXECUTE_NOW, SCHEDULE, Plan, apply_deposit, apply_execution, plan, replan, usd_eq, with_confidence
from .policy import Policy
from .signals import Signal, propose_signal

PATHS = ("person", "remittance")


def cash(s: Scenario) -> dict:
    total = usd_eq(s.wallets, s.rates)
    return {"wallets": {c: round(v, 2) for c, v in s.wallets.items()}, "usd_equiv": round(total, 2),
            "floor": s.reserve_floor_usd, "headroom": round(total - s.reserve_floor_usd, 2)}


def plan_view(p: Plan, s: Scenario) -> dict:
    by_id = {o.id: o for o in s.obligations}
    rows = []
    for oid, d in p.decisions.items():
        o = by_id[oid]
        row = {"id": oid, "payee": o.payee, "ccy": o.ccy, "amount": o.amount, "due_h": o.due_h,
               "criticality": o.criticality, "method": o.method, "new_beneficiary": o.new_beneficiary,
               "action": d.action, "reason": d.reason, "rules": list(d.rules), "options": list(d.options)}
        if d.draw and d.draw.converted > 0:
            row["convert"] = {"usd": round(d.draw.usd_spent, 2), "amount": round(d.draw.converted, 2),
                              "fee": round(d.draw.fee, 2)}
        rows.append(row)
    return {"confidence": round(p.confidence, 4), "limit_usd": p.limit_usd, "reliant_usd": round(p.reliant_usd, 2),
            "decisions": rows}


def _executed(p: Plan) -> list:
    return [oid for oid, d in p.decisions.items() if d.action == EXECUTE_NOW]


def _signal_view(sig: Signal, before: float) -> dict:
    return {"before": before, "after": round(sig.confidence, 4), "source": sig.source, "reason": sig.reason,
            "evidence": sig.evidence, "category": sig.category}


def _names(ids: list, s: Scenario) -> str:
    by_id = {o.id: o.payee for o in s.obligations}
    names = [by_id.get(i, i) for i in ids]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _verb(ids: list, one: str, many: str) -> str:
    return one if len(ids) == 1 else many


def _needs_person(p: Plan) -> list:
    """Escalated because of money or forecast risk (E2/E3), not the always-on new-payee rule."""
    return [oid for oid, d in p.decisions.items() if d.action == ESCALATE and {"E2", "E3"} & set(d.rules)]


def _first_plan_text(p: Plan, s: Scenario) -> str:
    out = []
    now = _executed(p)
    sched = [oid for oid, d in p.decisions.items() if d.action == SCHEDULE]
    person = _needs_person(p)
    if now:
        out.append(f"{_names(now, s)} {_verb(now, 'is', 'are')} paid now.")
    if sched:
        out.append(f"{_names(sched, s)} {_verb(sched, 'waits', 'wait')} for the customer's money.")
    if person:
        out.append(f"{_names(person, s)} {_verb(person, 'needs', 'need')} a person.")
    return " ".join(out) or "Nothing can be paid yet."


def _corr_view(c) -> dict:
    return {"confidence": c.confidence, "reason": c.reason, "matched": list(c.matched),
            "rejected": [list(r) for r in c.rejected]}


def build_story(scenario: Scenario, policy: Policy, email: str, evidence: list, path: str = "person",
                signal: Callable[..., Signal] = propose_signal, start_confidence: float | None = None) -> dict:
    """Run the whole scenario. `signal(email, current, receipt)` is injectable so the API and tests can pin it.
    `start_confidence` overrides the forecast's confidence before anything happens."""
    if path not in PATHS:
        raise ValueError(f"path must be one of {PATHS}")
    receipt = scenario.receipts[0]
    if start_confidence is not None:
        scenario = with_confidence(scenario, receipt.id, min(1.0, max(0.0, float(start_confidence))))
        receipt = scenario.receipts[0]
    s0 = scenario
    steps = []

    def step(key: str, title: str, text: str, s: Scenario, p: Plan | None = None, planned_on: Scenario | None = None,
             **extra) -> None:
        """`s` is the cash position after the step; `planned_on` the scenario `p` was made from (default `s`)."""
        steps.append({"key": key, "title": title, "text": text, "cash": cash(s),
                      "plan": plan_view(p, planned_on or s) if p else None, **extra})

    step("goal", "Goal", "Pay what matters, stay above the floor, never spend money that hasn't arrived.", s0,
         receipt={"payer": receipt.payer, "ccy": receipt.ccy, "amount": receipt.amount, "due_h": receipt.due_h,
                  "confidence": receipt.confidence})

    p1 = plan(s0, policy)
    s1 = apply_execution(s0, p1)
    step("plan", "First plan", _first_plan_text(p1, s0), s1, p1, planned_on=s0, executed=_executed(p1))

    # the email: the model proposes, code validates (signals.py)
    sig = signal(email, receipt.confidence, receipt)
    conf = sig.confidence
    s1 = with_confidence(s1, receipt.id, conf)
    p2 = plan(s1, policy)
    step("email", "Customer email", "The model reads it and suggests a new confidence. Code checks the quote.", s1, None,
         email=email, signal=_signal_view(sig, receipt.confidence))
    before, after = set(_needs_person(p1)), set(_needs_person(p2))
    added, removed = sorted(after - before), sorted(before - after)
    if added:
        text = f"{_names(added, s1)} now {_verb(added, 'depends', 'depend')} on money we no longer trust, so a person decides."
    elif removed:
        text = f"{_names(removed, s1)} no longer {_verb(removed, 'needs', 'need')} a person."
    else:
        text = "The plan doesn't change."
    step("revised", "New plan", text, s1, p2)

    # structured evidence: matched in code (evidence.py)
    contradicted = conf < receipt.confidence
    c = corroborate(receipt, [e for e in evidence if e.kind == "invoice"], conf, policy.evidence, contradicted=contradicted)
    if c.confidence > conf:
        conf = c.confidence
        s1 = with_confidence(s1, receipt.id, conf)
        p2 = plan(s1, policy)
        step("evidence", "Invoice check", f"The approved invoice matches, so confidence rises to {conf:.2f}.", s1, p2,
             corroboration=_corr_view(c))
    else:
        if contradicted and c.rejected and not c.matched:
            text = "An approved invoice proves the debt, not the timing, so it changes nothing."
        elif c.matched:
            text = "The invoice matches, but confidence is already higher."
        else:
            text = "No invoice matches this payment."
        step("evidence", "Invoice check", text, s1, None, corroboration=_corr_view(c))

    if path == "remittance":
        rem = [e for e in evidence if e.kind == "remittance"]
        c = corroborate(receipt, rem, conf, policy.evidence, contradicted=contradicted)
        raised = c.confidence > conf
        s1 = with_confidence(s1, receipt.id, c.confidence)
        p2 = plan(s1, policy)
        text = (f"The bank's record matches exactly, so confidence rises to {c.confidence:.2f}. No person needed."
                if raised else "Confidence is already high; the bank record changes nothing.")
        step("bank", "Bank confirms", text, s1, p2,
             remittance=[{"ref": e.ref, "payer": e.payer, "ccy": e.ccy, "amount": e.amount, "date_h": e.date_h,
                          "status": e.status} for e in rem],
             corroboration=_corr_view(c))
    else:
        if _needs_person(p2):
            step("person", "Person decides", "The approver chooses to wait for the customer's money.", s1, p2,
                 choice="wait_for_receipt")
        else:
            step("person", "Person decides", "Nothing needs a person's decision.", s1, p2)

    s2 = apply_deposit(s1, receipt.id)
    p3, locked, recomputed = replan(p2, s2, policy, {receipt.ccy})
    step("deposit", "Money arrives",
         f"{receipt.ccy} {receipt.amount:,.0f} settles. Only bills that use {receipt.ccy} are re-planned.", s2, p3,
         locked=locked, recomputed=recomputed)

    s3 = apply_execution(s2, p3)
    ok = usd_eq(s3.wallets, s3.rates) >= s3.reserve_floor_usd
    paid, left = _executed(p3), [o.id for o in s3.obligations]
    parts = [f"{_names(paid, s2)} {_verb(paid, 'is', 'are')} paid." if paid else "Nothing more is paid."]
    if left:
        parts.append(f"{_names(left, s2)} {_verb(left, 'waits', 'wait')} for a person or a later date.")
    step("final", "Done", " ".join(parts), s3, None, executed=paid, floor_ok=ok, still_open=left)
    return {"path": path, "steps": steps}
