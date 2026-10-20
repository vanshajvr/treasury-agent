import random
from dataclasses import replace
from pathlib import Path

import pytest

from treasury import (DEFER, ESCALATE, EXECUTE_NOW, HOLD, SCHEDULE, Policy, Tier, apply_deposit,
                      apply_execution, effective_confidence, load_policy, load_scenario, plan, replan,
                      simulate, usd_eq, with_confidence)
from treasury.planner import _weighted_receipts, MAX_BRUTE

ROOT = Path(__file__).resolve().parent.parent
RID = "customer_a"


@pytest.fixture
def policy():
    return load_policy(ROOT / "policy.json")


@pytest.fixture
def base():
    return load_scenario(ROOT / "scenario.json")


def actions(p):
    return {oid: d.action for oid, d in p.decisions.items()}


# ----------------------------------------------------------------- tiers
def test_tier_boundaries(policy):
    assert policy.tier_for(0.8).weight == 1.0
    assert policy.tier_for(0.79).weight == 0.5
    assert policy.tier_for(0.5).weight == 0.5
    assert policy.tier_for(0.499).weight == 0.0
    assert policy.tier_for(0.0).limit_usd == 0


# ----------------------------------------------------------------- the three reference plans
def test_confidence_09(base, policy):
    p = plan(with_confidence(base, RID, 0.9), policy)
    assert actions(p) == {"freight": EXECUTE_NOW, "supplier": SCHEDULE, "contractor": SCHEDULE,
                          "tooling": DEFER, "licence": ESCALATE}
    assert p.decisions["licence"].rules == ["E1"]
    assert p.reliant_usd == pytest.approx(7231.9, abs=1.0)
    assert p.reliant_usd <= p.limit_usd


def test_confidence_06(base, policy):
    p = plan(with_confidence(base, RID, 0.6), policy)
    assert actions(p) == {"freight": EXECUTE_NOW, "supplier": SCHEDULE, "contractor": DEFER,
                          "tooling": DEFER, "licence": ESCALATE}
    assert p.reliant_usd == pytest.approx(3231.9, abs=1.0)
    assert p.reliant_usd <= p.limit_usd == 4000


def test_confidence_04_escalates_supplier_and_holds_the_rest(base, policy):
    p = plan(with_confidence(base, RID, 0.4), policy)
    assert actions(p) == {"freight": EXECUTE_NOW, "supplier": ESCALATE, "contractor": HOLD,
                          "tooling": HOLD, "licence": ESCALATE}
    sup = p.decisions["supplier"]
    assert sup.rules == ["E3"]
    assert any(o.startswith("wait_for_receipt") for o in sup.options)
    assert any(o.startswith("request_extension") for o in sup.options)
    assert p.decisions["contractor"].rules == ["R5"]


# ----------------------------------------------------------------- escalation rules
def test_e2_when_reliant_spend_exceeds_limit(base, policy):
    tight = Policy(tiers=(Tier(0.8, 1.0, 3000), Tier(0.5, 0.5, 1500), Tier(0.0, 0.0, 0)),
                   penalties=policy.penalties, spread_bps=policy.spread_bps, swift_fee=policy.swift_fee)
    p = plan(with_confidence(base, RID, 0.9), tight)
    assert p.decisions["supplier"].action == ESCALATE and p.decisions["supplier"].rules == ["E2"]
    assert p.decisions["contractor"].rules == ["E2"]
    assert p.decisions["freight"].action == EXECUTE_NOW  # settled cash is never gated by the limit


def test_new_beneficiary_always_escalates_even_with_plenty_of_cash(base, policy):
    rich = base.clone()
    rich.wallets = {"USD": 1_000_000, "GBP": 100_000, "EUR": 100_000}
    p = plan(rich, policy)
    assert p.decisions["licence"].action == ESCALATE
    assert "would fit" in p.decisions["licence"].reason


def test_swift_fee_included_and_local_free(base, policy):
    p = plan(with_confidence(base, RID, 0.9), policy)
    sup = p.decisions["supplier"].draw
    assert sup.fee == pytest.approx(12.85)
    assert sup.need == pytest.approx(9812.85)
    assert sup.converted == pytest.approx(9812.85)
    assert p.decisions["freight"].draw.fee == 0 and p.decisions["freight"].draw.converted == 0


def test_receipt_cannot_fund_obligation_due_before_it_lands(base, policy):
    early = base.clone()
    early.receipts = [replace(early.receipts[0], due_h=40, confidence=1.0)]  # after supplier (36h)
    p = plan(early, policy)
    assert p.decisions["supplier"].action in (ESCALATE, DEFER)


def test_brute_force_guard(base, policy):
    big = base.clone()
    big.obligations = [replace(base.obligations[3], id=f"x{i}", due_h=10 + i) for i in range(MAX_BRUTE + 1)]
    with pytest.raises(NotImplementedError):
        plan(big, policy)


# ----------------------------------------------------------------- deposit and incremental replan
def test_deposit_then_replan_executes_supplier(base, policy):
    s0 = with_confidence(base, RID, 0.4)
    p0 = plan(s0, policy)
    s1 = apply_execution(s0, p0)                # freight is paid
    assert [o.id for o in s1.obligations if o.id == "freight"] == []
    s2 = apply_deposit(s1, RID)                 # receipt settles
    p2, locked, recomputed = replan(p0, s2, policy, {"USD"})
    assert actions(p2) == {"supplier": EXECUTE_NOW, "contractor": EXECUTE_NOW,
                           "tooling": DEFER, "licence": ESCALATE}
    s3 = apply_execution(s2, p2)
    assert usd_eq(s3.wallets, s3.rates) >= s3.reserve_floor_usd
    assert all(v >= -1e-6 for v in s3.wallets.values())


def test_replan_locks_decisions_the_change_cannot_affect(base, policy):
    s0 = with_confidence(base, RID, 0.9)
    p0 = plan(s0, policy)                       # freight EXECUTE_NOW (GBP only)
    s1 = apply_deposit(s0, RID)                 # USD deposit
    p1, locked, recomputed = replan(p0, s1, policy, {"USD"})
    assert locked == ["freight"]
    assert p1.decisions["freight"] is p0.decisions["freight"]
    assert "supplier" in recomputed and "freight" not in recomputed
    assert p1.decisions["supplier"].action == EXECUTE_NOW


# ----------------------------------------------------------------- invariants on random scenarios
def test_invariants_hold_on_random_scenarios(base, policy):
    rng = random.Random(7)
    for _ in range(300):
        s = with_confidence(base, RID, rng.choice([0.1, 0.3, 0.45, 0.5, 0.6, 0.79, 0.8, 0.95, 1.0]))
        s.wallets = {"USD": rng.uniform(0, 20000), "GBP": rng.uniform(0, 8000), "EUR": rng.uniform(0, 5000)}
        s.reserve_floor_usd = rng.uniform(0, 6000)
        s.obligations = [replace(o, amount=o.amount * rng.uniform(0.3, 1.8),
                                 new_beneficiary=o.new_beneficiary or rng.random() < 0.1)
                         for o in s.obligations]
        p = plan(s, policy)

        for o in s.obligations:
            d = p.decisions[o.id]
            if o.new_beneficiary:
                assert d.action == ESCALATE
            if o.criticality == "operations_stop":
                assert d.action != DEFER           # funded or escalated, never silently dropped

        # planning cash: the funded set is feasible under the weighted forecast
        funded = [o for o in s.obligations if o.id in p.funded_ids]
        assert simulate(funded, s.wallets, _weighted_receipts(s, policy), s.rates, policy,
                        s.reserve_floor_usd).feasible

        # settled cash: executing EXECUTE_NOW items never overdraws or breaches the floor
        after = apply_execution(s, p)
        assert all(v >= -1e-6 for v in after.wallets.values())
        if usd_eq(s.wallets, s.rates) >= s.reserve_floor_usd:
            assert usd_eq(after.wallets, after.rates) >= s.reserve_floor_usd - 1e-6

        # limit: anything scheduled on forecast must be inside the tier limit, else escalated
        scheduled = [d for d in p.decisions.values() if d.action == SCHEDULE]
        if scheduled:
            assert p.reliant_usd <= p.limit_usd + 1e-6


def test_effective_confidence_defaults_to_one_without_receipts(base):
    s = base.clone()
    s.receipts = []
    assert effective_confidence(s) == 1.0