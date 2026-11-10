from treasury import load_policy
from treasury.sim import generate_world, make_policies, replay, run_world
from pathlib import Path
from treasury import usd_eq

ROOT = Path(__file__).resolve().parent.parent
CFG = load_policy(ROOT / "policy.json")
SEEDS = range(500, 560)


def test_world_generation_is_deterministic_and_starts_above_the_floor():
    a, b = generate_world(7), generate_world(7)
    assert a.scenario == b.scenario and a.arrivals == b.arrivals and a.emails == b.emails
    for seed in SEEDS:
        s = generate_world(seed).scenario
        assert usd_eq(s.wallets, s.rates) >= s.reserve_floor_usd
        assert 6 <= len(s.obligations) <= 9


def test_replay_is_deterministic():
    assert run_world(11, CFG) == run_world(11, CFG)


def test_floor_aware_policies_never_breach_the_floor():
    for seed in SEEDS:
        res = run_world(seed, CFG)
        for name in ("priority", "eager_priority", "agent", "agent_no_signal"):
            assert not res[name].floor_breached, (seed, name)


def test_agent_never_pays_an_unvetted_payee():
    for seed in SEEDS:
        res = run_world(seed, CFG)
        assert res["agent"].unvetted_paid == 0 and res["agent_no_signal"].unvetted_paid == 0


def test_the_floor_metric_can_fire_for_floor_blind_policies():
    assert any(run_world(seed, CFG)["fifo"].floor_breached for seed in SEEDS)


def test_outcomes_are_sane():
    for seed in SEEDS:
        for name, o in run_world(seed, CFG).items():
            assert 0 <= o.crit_flagged <= o.crit_late <= o.crit_total, (seed, name)
            assert o.cost_usd >= 0 and o.penalty >= 0 and o.paid_n <= o.n_obl


def test_agent_beats_the_naive_baselines_in_aggregate():
    tot = {k: 0.0 for k in ("fifo", "priority", "eager_priority", "agent")}
    for seed in range(1000, 1150):
        res = run_world(seed, CFG)
        for k in tot:
            tot[k] += res[k].penalty
    assert tot["agent"] < tot["eager_priority"] < tot["fifo"]