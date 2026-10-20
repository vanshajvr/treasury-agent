"""Deterministic planner and policy gate. No network, no LLM, no randomness.

Pipeline for one run:
  1. optimise  - choose which obligations to fund (exact brute force over subsets)
  2. classify  - split funded items into EXECUTE_NOW (settled cash) and SCHEDULE_ON_DEPOSIT
  3. gate      - apply escalation rules E1/E2/E3 and the hold rule R5
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

from treasury.models import BASE, CRIT_RANK, Obligation, Scenario
from treasury.policy import Policy

EPS = 1e-6
MAX_BRUTE = 16  # 2**16 subsets is still instant; beyond that use a solver backend

EXECUTE_NOW = "EXECUTE_NOW"
SCHEDULE = "SCHEDULE_ON_DEPOSIT"
HOLD = "HOLD"
DEFER = "DEFER"
ESCALATE = "ESCALATE"


@dataclass(frozen=True)
class Draw:
    """How one payout is funded."""
    ccy: str          # obligation currency
    need: float       # amount incl. any SWIFT fee, in obligation currency
    from_wallet: float  # taken from the wallet of that currency
    converted: float  # bought via USD -> ccy conversion
    usd_spent: float  # USD paid for the conversion (all-in rate)
    fee: float        # SWIFT fee included in `need`


@dataclass
class Decision:
    obligation_id: str
    action: str
    reason: str
    rules: list = field(default_factory=list)
    draw: Draw | None = None
    reliant_usd: float = 0.0
    options: list = field(default_factory=list)


@dataclass
class Plan:
    decisions: dict
    confidence: float
    tier_weight: float
    limit_usd: float
    reliant_usd: float
    funded_ids: list
    locked_ids: list
    cost_usd: float


@dataclass
class SimResult:
    feasible: bool
    wallets: dict
    draws: dict
    cost_usd: float


# --------------------------------------------------------------------------- helpers
def rank(ob: Obligation) -> tuple:
    return (CRIT_RANK[ob.criticality], ob.due_h)


def usd_eq(wallets: dict, rates: dict) -> float:
    return sum(v * rates[c] for c, v in wallets.items())


def _fee(ob: Obligation, swift_fee: dict) -> float:
    return swift_fee.get(ob.ccy, 0.0) if ob.method == "SWIFT" else 0.0


def _pay(wallets: dict, ob: Obligation, rates: dict, swift_fee: dict, floor: float):
    """Pay one obligation from settled cash. Returns (new_wallets, Draw) or None if infeasible."""
    w = dict(wallets)
    fee = _fee(ob, swift_fee)
    need = ob.amount + fee
    if ob.ccy == BASE:
        if w.get(BASE, 0.0) + EPS < need:
            return None
        w[BASE] = w.get(BASE, 0.0) - need
        draw = Draw(ob.ccy, need, need, 0.0, 0.0, fee)
    else:
        take = min(max(w.get(ob.ccy, 0.0), 0.0), need)
        buy = need - take
        usd = buy * rates[ob.ccy]
        if w.get(BASE, 0.0) + EPS < usd:
            return None
        w[ob.ccy] = w.get(ob.ccy, 0.0) - take
        w[BASE] = w.get(BASE, 0.0) - usd
        draw = Draw(ob.ccy, need, take, buy, usd, fee)
    if usd_eq(w, rates) + EPS < floor:
        return None
    return w, draw


def _earmark(wallets: dict, ob: Obligation, rates: dict, swift_fee: dict, floor: float):
    """Reserve whatever settled cash allows for an item that cannot execute yet.

    Returns (new_wallets, reliant_usd): reliant_usd is the part that must come from a forecast receipt.
    """
    w = dict(wallets)
    need = ob.amount + _fee(ob, swift_fee)
    if ob.ccy == BASE:
        usd_cost = need
    else:
        take = min(max(w.get(ob.ccy, 0.0), 0.0), need)
        w[ob.ccy] = w.get(ob.ccy, 0.0) - take
        usd_cost = (need - take) * rates[ob.ccy]
    other = usd_eq(w, rates) - w.get(BASE, 0.0)
    headroom = max(0.0, w.get(BASE, 0.0) - max(0.0, floor - other))
    spend = min(headroom, usd_cost)
    w[BASE] = w.get(BASE, 0.0) - spend
    return w, usd_cost - spend


def simulate(funded: list, wallets: dict, receipts_w: list, rates: dict, policy: Policy,
             floor: float) -> SimResult:
    """Time-ordered cash simulation. receipts_w = [(due_h, ccy, weighted_amount)].

    A receipt only helps obligations due at or after its own due time, and the reserve
    floor must hold after every payment, not just at the end.
    """
    w = dict(wallets)
    events = [(h, 0, i) for i, (h, _, _) in enumerate(receipts_w)]
    events += [(o.due_h, 1, i) for i, o in enumerate(funded)]
    events.sort()
    draws, cost = {}, 0.0
    for _, kind, i in events:
        if kind == 0:
            _, ccy, amt = receipts_w[i]
            w[ccy] = w.get(ccy, 0.0) + amt
            continue
        ob = funded[i]
        res = _pay(w, ob, rates, policy.swift_fee, floor)
        if res is None:
            return SimResult(False, w, draws, cost)
        w, d = res
        draws[ob.id] = d
        cost += policy.spread_bps / 10_000 * d.usd_spent + d.fee * rates[ob.ccy]
    return SimResult(True, w, draws, cost)


def effective_confidence(scenario: Scenario) -> float:
    """Amount-weighted confidence of unsettled receipts; 1.0 when nothing is forecast."""
    total = sum(r.amount * scenario.rates[r.ccy] for r in scenario.receipts)
    if total <= 0:
        return 1.0
    return sum(r.confidence * r.amount * scenario.rates[r.ccy] for r in scenario.receipts) / total


def _weighted_receipts(scenario: Scenario, policy: Policy) -> list:
    return [(r.due_h, r.ccy, r.amount * policy.tier_for(r.confidence).weight) for r in scenario.receipts]


def _classify(funded: list, settled: dict, rates: dict, policy: Policy, floor: float):
    """Priority-ordered pass over SETTLED cash only (rule R1)."""
    w = dict(settled)
    cls, draws, reliant = {}, {}, {}
    for ob in sorted(funded, key=rank):
        res = _pay(w, ob, rates, policy.swift_fee, floor)
        if res is not None:
            w, d = res
            cls[ob.id], draws[ob.id] = EXECUTE_NOW, d
        else:
            w, r = _earmark(w, ob, rates, policy.swift_fee, floor)
            cls[ob.id], reliant[ob.id] = SCHEDULE, r
    return cls, draws, reliant


def _options(ob: Obligation, scenario: Scenario, policy: Policy, funded_final: list) -> list:
    opts = []
    for r in scenario.receipts:
        if r.due_h <= ob.due_h:
            opts.append(f"wait_for_receipt: {r.payer} {r.ccy} {r.amount:,.0f} expected at {r.due_h:g}h, "
                        f"before {ob.id} is due at {ob.due_h:g}h (confidence {r.confidence:.2f}); "
                        f"re-plan when it settles")
    # Could the item be paid from settled cash alone if lower-priority work were dropped?
    rk = CRIT_RANK[ob.criticality]
    high = [o for o in scenario.obligations if not o.new_beneficiary and CRIT_RANK[o.criticality] <= rk]
    sim = simulate(high, scenario.wallets, [], scenario.rates, policy, scenario.reserve_floor_usd)
    if sim.feasible:
        lower = [o.id for o in funded_final if CRIT_RANK[o.criticality] > rk]
        if lower:
            opts.append(f"reprioritise: skip {', '.join(lower)} to pay {ob.id} from settled cash")
    opts.append(f"request_extension: ask {ob.payee} for a later due date")
    return opts


# --------------------------------------------------------------------------- main entry
def plan(scenario: Scenario, policy: Policy, locked: dict | None = None) -> Plan:
    """Plan every open obligation. `locked` maps obligation id -> Decision to keep unchanged."""
    locked = locked or {}
    rates, floor = scenario.rates, scenario.reserve_floor_usd
    by_id = {o.id: o for o in scenario.obligations}
    locked_obs = [by_id[i] for i in locked if i in by_id]
    free = [o for o in scenario.obligations if not o.new_beneficiary and o.id not in locked]
    e1 = [o for o in scenario.obligations if o.new_beneficiary and o.id not in locked]
    if len(free) > MAX_BRUTE:
        raise NotImplementedError(f"{len(free)} open obligations: brute force is capped at {MAX_BRUTE}; "
                                  "plug in a solver backend")

    receipts_w = _weighted_receipts(scenario, policy)
    if not simulate(locked_obs, scenario.wallets, receipts_w, rates, policy, floor).feasible:
        raise ValueError("locked decisions are no longer feasible; re-plan without locking")

    # 1. optimise: minimise deferred penalty, then prefer deferring later-due items, then lowest cost
    best = None
    for k in range(len(free) + 1):
        for subset in combinations(free, k):
            funded = locked_obs + list(subset)
            sim = simulate(funded, scenario.wallets, receipts_w, rates, policy, floor)
            if not sim.feasible:
                continue
            chosen = {o.id for o in subset}
            deferred = [o for o in free if o.id not in chosen]
            key = (sum(policy.penalties[o.criticality] for o in deferred),
                   -sum(o.due_h for o in deferred), sim.cost_usd)
            if best is None or key < best[0]:
                best = (key, list(subset), sim)
    if best is None:  # unreachable: the k=0 subset is feasible once the locked check passes
        raise ValueError("no feasible funding set")
    _, subset, plan_sim = best
    funded_final = locked_obs + subset
    funded_ids = {o.id for o in funded_final}

    # 2. classify against settled cash only
    cls, now_draws, reliant = _classify(funded_final, scenario.wallets, rates, policy, floor)
    reliant_total = sum(reliant.values())
    conf = effective_confidence(scenario)
    tier = policy.tier_for(conf)
    over_limit = reliant_total > tier.limit_usd + EPS

    # 3. gate
    decisions: dict = {}
    for ob in sorted(scenario.obligations, key=rank):
        if ob.id in locked:
            decisions[ob.id] = locked[ob.id]
        elif ob.new_beneficiary:
            fits = simulate(funded_final + [ob], scenario.wallets, receipts_w, rates, policy, floor).feasible
            note = ("would fit within the reserve floor if approved" if fits else
                    "would NOT fit within the reserve floor; a person must choose what to give up")
            decisions[ob.id] = Decision(ob.id, ESCALATE, f"new beneficiary, never auto-paid (E1); {note}", ["E1"])
        elif ob.id in funded_ids and cls[ob.id] == EXECUTE_NOW:
            decisions[ob.id] = Decision(ob.id, EXECUTE_NOW, "funded from settled cash", [],
                                        now_draws[ob.id])
        elif ob.id in funded_ids:
            d = plan_sim.draws[ob.id]
            if over_limit:
                decisions[ob.id] = Decision(
                    ob.id, ESCALATE,
                    f"forecast-reliant spend {reliant_total:,.0f} USD exceeds the autonomous limit "
                    f"{tier.limit_usd:,.0f} USD at confidence {conf:.2f} (E2); approve the scheduled payment",
                    ["E2"], d, reliant[ob.id])
            else:
                decisions[ob.id] = Decision(
                    ob.id, SCHEDULE,
                    f"funded once the expected receipt settles; reliant spend {reliant[ob.id]:,.0f} USD "
                    f"is within the {tier.limit_usd:,.0f} USD limit (R1: never executes before settlement)",
                    [], d, reliant[ob.id])
        elif ob.criticality in ("operations_stop", "time_sensitive"):
            decisions[ob.id] = Decision(
                ob.id, ESCALATE,
                f"{ob.criticality} obligation cannot be funded from planning cash within the reserve floor (E3)",
                ["E3"], None, 0.0, _options(ob, scenario, policy, funded_final))
        else:
            decisions[ob.id] = Decision(ob.id, DEFER, "not fundable within the reserve floor; "
                                        "lowest penalty to defer", [])

    # R5: do not spend cash a person may need to redirect while a critical item is escalated (E3)
    e3 = [by_id[i] for i, d in decisions.items() if "E3" in d.rules]
    if e3:
        top = min(CRIT_RANK[o.criticality] for o in e3)
        for oid, d in list(decisions.items()):
            if d.action == EXECUTE_NOW and oid not in locked and CRIT_RANK[by_id[oid].criticality] > top:
                decisions[oid] = Decision(oid, HOLD, "funded, but held while a more critical obligation is "
                                          "escalated (R5)", ["R5"], d.draw)

    return Plan(decisions, conf, tier.weight, tier.limit_usd, reliant_total,
                [o.id for o in sorted(funded_final, key=rank)], sorted(locked), plan_sim.cost_usd)


# --------------------------------------------------------------------------- state changes (offline ledger)
def apply_execution(scenario: Scenario, p: Plan) -> Scenario:
    """Book every EXECUTE_NOW decision against the wallets and close the obligation."""
    s = scenario.clone()
    done = set()
    for oid, d in p.decisions.items():
        if d.action != EXECUTE_NOW or d.draw is None:
            continue
        s.wallets[d.draw.ccy] = s.wallets.get(d.draw.ccy, 0.0) - d.draw.from_wallet
        s.wallets[BASE] = s.wallets.get(BASE, 0.0) - d.draw.usd_spent
        done.add(oid)
    s.obligations = [o for o in s.obligations if o.id not in done]
    return s


def apply_deposit(scenario: Scenario, receipt_id: str) -> Scenario:
    """A forecast receipt has settled: move it from 'expected' into the wallets."""
    s = scenario.clone()
    r = next(x for x in s.receipts if x.id == receipt_id)
    s.wallets[r.ccy] = s.wallets.get(r.ccy, 0.0) + r.amount
    s.receipts = [x for x in s.receipts if x.id != receipt_id]
    return s


def with_confidence(scenario: Scenario, receipt_id: str, confidence: float) -> Scenario:
    from dataclasses import replace
    s = scenario.clone()
    s.receipts = [replace(r, confidence=confidence) if r.id == receipt_id else r for r in s.receipts]
    return s


def _touches(d: Draw, changed: set) -> bool:
    return d.ccy in changed or (d.usd_spent > 0 and BASE in changed)


def replan(prev: Plan, scenario: Scenario, policy: Policy, changed: set):
    """Re-plan only what the change can affect. Returns (new_plan, locked_ids, recomputed_ids)."""
    open_ids = {o.id for o in scenario.obligations}
    locked = {oid: d for oid, d in prev.decisions.items()
              if oid in open_ids and d.action in (EXECUTE_NOW, SCHEDULE) and d.draw is not None
              and not _touches(d.draw, changed)}
    new = plan(scenario, policy, locked)
    recomputed = sorted(open_ids - set(locked))
    return new, sorted(locked), recomputed


# --------------------------------------------------------------------------- reporting
def format_plan(p: Plan, scenario: Scenario) -> str:
    by_id = {o.id: o for o in scenario.obligations}
    lines = [f"confidence {p.confidence:.2f} | autonomous limit {p.limit_usd:,.0f} USD | "
             f"forecast-reliant spend {p.reliant_usd:,.0f} USD"]
    for oid, d in p.decisions.items():
        ob = by_id.get(oid)
        head = f"  {oid:<11} {d.action:<20}"
        if ob:
            head += f" {ob.ccy} {ob.amount:>9,.2f} due {ob.due_h:g}h"
        lines.append(head)
        if d.draw and d.draw.converted > 0:
            fee = f" (incl. {d.draw.ccy} {d.draw.fee:.2f} SWIFT fee)" if d.draw.fee else ""
            lines.append(f"      convert USD {d.draw.usd_spent:,.2f} -> {d.draw.ccy} {d.draw.converted:,.2f}{fee}")
        lines.append(f"      {d.reason}")
        for opt in d.options:
            lines.append(f"      option: {opt}")
    return "\n".join(lines)