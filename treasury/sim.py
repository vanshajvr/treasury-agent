"""Randomised treasury worlds, a replay engine and the policies compared in the benchmark.

A World is ground truth: what the planner is told at t=0, which forecast receipts really arrive (and when),
and which emails land in the inbox. Every policy is replayed through the SAME world with the SAME payment
arithmetic (the planner's own `_pay`), so differences in the results come from decisions, not bookkeeping.

Policies
  fifo              pay each obligation when it falls due, if settled cash covers it. No floor, no priorities.
  convert_upfront   convert every foreign-currency need at t=0, then behave like fifo.
  priority          pays on the due date; floor-aware, criticality-ordered, settled cash only, no forecasts.
  eager_priority    as priority but pays as soon as cash allows, critical first (the agent minus planning,
                    forecasts, escalation and the new-payee gate: the strongest naive baseline).
  agent_no_signal   the Treasury Controller, ignoring emails (it still notices a receipt that is overdue).
  agent             the Treasury Controller reading emails (confidence updates) and re-planning on every event.

Simplifications (also written into the benchmark report): receipts are calibrated (a forecast of 0.7 arrives
70% of the time), emails are noise-free confidence updates, FX rates are static, fees follow policy.json.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, replace

from .models import BASE, Obligation, Receipt, Scenario
from .planner import ESCALATE, EXECUTE_NOW, _pay, plan, usd_eq
from .policy import Policy

RATES = {"USD": 1.0, "GBP": 1.347088, "EUR": 1.134843}
HORIZON_H = 72.0
CRITICAL = ("operations_stop", "time_sensitive")
OVERDUE_GRACE_H = 3.0          # a receipt later than this past its due time is treated as overdue
NO_FLOOR = -1e18


# --------------------------------------------------------------------------- world
@dataclass
class World:
    scenario: Scenario         # what everyone knows at t=0
    arrivals: dict             # receipt id -> hour it really settles, or None if it never does within the horizon
    emails: list = field(default_factory=list)   # (hour, receipt id, new confidence)


def generate_world(seed: int, n_min: int = 6, n_max: int = 9) -> World:
    rng = random.Random(seed)
    crits = ["operations_stop", "time_sensitive", "important", "deferrable"]
    obligations = []
    for i in range(rng.randint(n_min, n_max)):
        ccy = rng.choices(["USD", "GBP", "EUR"], [0.4, 0.3, 0.3])[0]
        usd_size = math.exp(rng.uniform(math.log(800), math.log(7000)))
        obligations.append(Obligation(
            id=f"ob{i}", payee=f"payee_{i}", ccy=ccy, amount=round(usd_size / RATES[ccy], 2),
            due_h=round(rng.uniform(6, 70), 1), criticality=rng.choices(crits, [0.10, 0.20, 0.30, 0.40])[0],
            method="SWIFT" if ccy == "EUR" and rng.random() < 0.8 else "LOCAL",
            new_beneficiary=rng.random() < 0.08))
    need_usd = sum(o.amount * RATES[o.ccy] for o in obligations)
    floor = need_usd * rng.uniform(0.05, 0.15)
    cash_usd = max(need_usd * rng.uniform(0.35, 0.8), floor * 1.5)
    usd_share = rng.uniform(0.5, 1.0)
    gbp_share = (1 - usd_share) * rng.uniform(0, 1)
    wallets = {"USD": round(cash_usd * usd_share, 2),
               "GBP": round(cash_usd * gbp_share / RATES["GBP"], 2),
               "EUR": round(cash_usd * (1 - usd_share - gbp_share) / RATES["EUR"], 2)}

    receipts, arrivals, emails = [], {}, []
    k = rng.choice([1, 2])
    for j in range(k):
        conf = rng.choice([0.95, 0.9, 0.75, 0.6, 0.4, 0.25])
        due = round(rng.uniform(12, 60), 1)
        rid = f"rcpt{j}"
        receipts.append(Receipt(id=rid, payer=f"Customer {j}", ccy="USD",
                                amount=round(need_usd * rng.uniform(0.15, 0.4) / k, 2), due_h=due, confidence=conf))
        arrives = rng.random() < conf                      # calibrated forecasts
        arrivals[rid] = round(due + rng.uniform(0, 2), 1) if arrives else None
        if not arrives and rng.random() < 0.7:
            emails.append((round(max(0.5, due - rng.uniform(6, 20)), 1), rid, 0.1))      # the payer warns of a delay
        elif arrives and rng.random() < 0.3:
            emails.append((round(max(0.5, due - rng.uniform(4, 12)), 1), rid, min(0.99, conf + 0.15)))
    scenario = Scenario(wallets=wallets, reserve_floor_usd=round(floor, 2), horizon_h=HORIZON_H,
                        obligations=obligations, receipts=receipts, rates=dict(RATES))
    return World(scenario, arrivals, sorted(emails))


# --------------------------------------------------------------------------- engine
@dataclass
class State:
    t: float
    wallets: dict
    pending: dict              # obligation id -> Obligation
    receipts: dict             # receipt id -> Receipt that has not settled yet
    conf: dict                 # receipt id -> confidence as updated by emails


@dataclass
class Outcome:
    n_obl: int = 0
    crit_total: int = 0
    crit_late: int = 0          # critical obligations not paid by their due time
    crit_flagged: int = 0       # ... of which the policy had escalated to a person before the due time
    warn_lead_h: float = 0.0    # total hours of advance warning over the flagged ones
    penalty: float = 0.0        # sum of policy penalty weights over late obligations
    floor_breached: bool = False
    breach_usd: float = 0.0
    cost_usd: float = 0.0       # FX spread + payout fees
    unvetted_paid: int = 0      # payments to beneficiaries nobody approved
    paid_n: int = 0
    escalated_n: int = 0


def _cost(draw, ccy: str, rates: dict, spread_bps: float) -> float:
    return draw.converted * rates[ccy] * (1 - 1 / (1 + spread_bps / 1e4)) + draw.fee * rates[ccy]


def replay(world: World, agent, cfg: Policy) -> Outcome:
    s = world.scenario
    rates, floor = s.rates, s.reserve_floor_usd
    st = State(0.0, dict(s.wallets), {o.id: o for o in s.obligations}, {r.id: r for r in s.receipts},
               {r.id: r.confidence for r in s.receipts})
    paid: dict = {}
    out = Outcome(n_obl=len(s.obligations))
    out.cost_usd += agent.prepare(st, cfg, rates)
    margin = usd_eq(st.wallets, rates) - floor

    times = {0.0} | {o.due_h for o in s.obligations} | {r.due_h + OVERDUE_GRACE_H for r in s.receipts}
    times |= {a for a in world.arrivals.values() if a is not None} | {e[0] for e in world.emails}
    for t in sorted(x for x in times if x <= HORIZON_H):
        st.t = t
        for rid, a in world.arrivals.items():                      # settlements
            if a is not None and a <= t and rid in st.receipts:
                r = st.receipts.pop(rid)
                st.wallets[r.ccy] = st.wallets.get(r.ccy, 0.0) + r.amount
        for te, rid, c in world.emails:                            # inbox
            if te <= t:
                st.conf[rid] = c
        for oid in agent.act(st, t, cfg):                          # decisions
            ob = st.pending.get(oid)
            if ob is None:
                continue
            res = _pay(st.wallets, ob, rates, cfg.fees, floor if agent.floor_aware else NO_FLOOR)
            if res is None:
                continue
            st.wallets, draw = res
            paid[oid] = t
            del st.pending[oid]
            out.cost_usd += _cost(draw, ob.ccy, rates, cfg.spread_bps)
            out.unvetted_paid += int(ob.new_beneficiary)
        margin = min(margin, usd_eq(st.wallets, rates) - floor)

    for o in s.obligations:
        if o.new_beneficiary:
            continue                                               # needs a person's approval: not scored as a miss
        late = o.id not in paid or paid[o.id] > o.due_h + 1e-9
        critical = o.criticality in CRITICAL
        out.crit_total += int(critical)
        if late:
            out.penalty += cfg.penalties[o.criticality]
            if critical:
                out.crit_late += 1
                warned_at = agent.escalated_at.get(o.id, math.inf)
                if warned_at <= o.due_h + 1e-9:
                    out.crit_flagged += 1
                    out.warn_lead_h += o.due_h - warned_at
    out.paid_n = len(paid)
    out.floor_breached = margin < -0.01
    out.breach_usd = max(0.0, -margin)
    out.escalated_n = len(agent.escalated_at)
    return out


# --------------------------------------------------------------------------- policies
class _Base:
    name = "base"
    floor_aware = False

    def __init__(self) -> None:
        self.escalated_at: dict = {}

    def prepare(self, st: State, cfg: Policy, rates: dict) -> float:
        return 0.0

    def act(self, st: State, t: float, cfg: Policy) -> list:
        return []


class Fifo(_Base):
    name = "fifo"

    def act(self, st, t, cfg):
        return [o.id for o in sorted((o for o in st.pending.values() if o.due_h <= t + 1e-9), key=lambda o: o.due_h)]


class ConvertUpfront(Fifo):
    name = "convert_upfront"

    def prepare(self, st, cfg, rates):
        cost, reserved = 0.0, {}
        for o in sorted(st.pending.values(), key=lambda o: o.due_h):
            if o.ccy == BASE:
                continue
            need = o.amount + cfg.fees.get(o.method, {}).get(o.ccy, 0.0)
            avail = st.wallets.get(o.ccy, 0.0) - reserved.get(o.ccy, 0.0)
            if avail < need:
                usd = (need - avail) * rates[o.ccy]
                if st.wallets.get(BASE, 0.0) < usd:
                    continue
                st.wallets[BASE] -= usd
                st.wallets[o.ccy] = st.wallets.get(o.ccy, 0.0) + (need - avail)
                cost += (need - avail) * rates[o.ccy] * (1 - 1 / (1 + cfg.spread_bps / 1e4))
            reserved[o.ccy] = reserved.get(o.ccy, 0.0) + need
        return cost


class Priority(_Base):
    name = "priority"
    floor_aware = True

    def act(self, st, t, cfg):
        due = [o for o in st.pending.values() if o.due_h <= t + 1e-9]
        return [o.id for o in sorted(due, key=lambda o: (cfg_rank(cfg, o), o.due_h))]


class EagerPriority(_Base):
    name = "eager_priority"
    floor_aware = True

    def act(self, st, t, cfg):
        return [o.id for o in sorted(st.pending.values(), key=lambda o: (cfg_rank(cfg, o), o.due_h))]


def cfg_rank(cfg: Policy, o: Obligation) -> float:
    return -cfg.penalties[o.criticality]                           # biggest penalty first


class Agent(_Base):
    floor_aware = True

    def __init__(self, use_emails: bool) -> None:
        super().__init__()
        self.use_emails = use_emails
        self.name = "agent" if use_emails else "agent_no_signal"

    def act(self, st, t, cfg):
        receipts = []
        for r in st.receipts.values():
            conf = st.conf[r.id] if self.use_emails else r.confidence
            if t > r.due_h + OVERDUE_GRACE_H:
                conf = min(conf, 0.1)                              # money visibly did not arrive
            receipts.append(replace(r, due_h=max(r.due_h - t, 0.0), confidence=conf))
        view = Scenario(wallets=dict(st.wallets), reserve_floor_usd=self._floor, horizon_h=max(HORIZON_H - t, 1.0),
                        obligations=[replace(o, due_h=max(o.due_h - t, 0.0)) for o in st.pending.values()],
                        receipts=receipts, rates=self._rates)
        p = plan(view, cfg)
        for oid, d in p.decisions.items():
            if d.action == ESCALATE:
                self.escalated_at.setdefault(oid, t)
        return [oid for oid, d in p.decisions.items() if d.action == EXECUTE_NOW]

    def bind(self, world: World) -> "Agent":
        self._floor, self._rates = world.scenario.reserve_floor_usd, world.scenario.rates
        return self


def make_policies(world: World) -> dict:
    return {"fifo": Fifo(), "convert_upfront": ConvertUpfront(), "priority": Priority(),
            "eager_priority": EagerPriority(), "agent_no_signal": Agent(False).bind(world), "agent": Agent(True).bind(world)}


def run_world(seed: int, cfg: Policy, n_min: int = 6, n_max: int = 9) -> dict:
    """All policies on one generated world: {policy name: Outcome}."""
    world = generate_world(seed, n_min, n_max)
    return {name: replay(world, pol, cfg) for name, pol in make_policies(world).items()}