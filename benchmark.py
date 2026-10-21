"""Benchmark the Treasury Controller against naive baselines on randomised treasury worlds.

  python3 benchmark.py                       # 2000 worlds, seed 1000
  python3 benchmark.py --n 2000 --seed 7     # bigger run
Writes results/benchmark.md and results/benchmark.json. Fully offline and deterministic for a given seed.
"""
import argparse
import json
import random
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from pathlib import Path

from treasury import load_policy
from treasury.sim import run_world

ORDER = ["fifo", "convert_upfront", "priority", "eager_priority", "agent_no_signal", "agent"]
LABEL = {"fifo": "Pay on due date (FIFO)", "convert_upfront": "Convert everything upfront",
         "priority": "Priority + floor, pay on due date", "eager_priority": "Priority + floor, pay ASAP", "agent_no_signal": "Agent, ignoring emails",
         "agent": "Agent (full)"}


def _job(args):
    seed, = args
    return run_world(seed, load_policy("policy.json"))


def boot_ci(values, reps=1000, seed=0):
    """95% bootstrap interval for the mean."""
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(reps))
    return means[int(0.025 * reps)], means[int(0.975 * reps)]


def summarise(rows: list) -> dict:
    n = len(rows)
    ct, cl = sum(r["crit_total"] for r in rows), sum(r["crit_late"] for r in rows)
    flagged = sum(r["crit_flagged"] for r in rows)
    pen = [r["penalty"] for r in rows]
    lo, hi = boot_ci(pen)
    return {
        "crit_on_time_pct": 100 * (1 - cl / ct) if ct else 100.0,
        "crit_late": cl, "crit_silent": cl - flagged, "crit_total": ct,
        "warn_lead_h": sum(r["warn_lead_h"] for r in rows) / flagged if flagged else 0.0,
        "penalty_mean": sum(pen) / n, "penalty_ci": [lo, hi],
        "floor_breach_pct": 100 * sum(r["floor_breached"] for r in rows) / n,
        "breach_usd_mean": sum(r["breach_usd"] for r in rows) / n,
        "cost_mean": sum(r["cost_usd"] for r in rows) / n,
        "unvetted_per_100": 100 * sum(r["unvetted_paid"] for r in rows) / n,
        "escalations_per_world": sum(r["escalated_n"] for r in rows) / n,
    }


def paired(rows: dict, a: str, b: str) -> dict:
    """Per-world comparison of policy a against policy b on identical worlds (negative = a is better)."""
    out = {}
    for key in ("penalty", "crit_late", "cost_usd"):
        d = [x[key] - y[key] for x, y in zip(rows[a], rows[b])]
        lo, hi = boot_ci(d)
        out[key] = {"mean_diff": sum(d) / len(d), "ci": [lo, hi],
                    "a_better": sum(v < -1e-9 for v in d), "tie": sum(abs(v) <= 1e-9 for v in d),
                    "a_worse": sum(v > 1e-9 for v in d)}
    return out


def report(summary: dict, n: int, seed: int, pair: dict) -> str:
    L = ["# Benchmark: Treasury Controller vs naive baselines", "",
         f"{n} randomised treasury worlds (seed {seed}), 6-9 obligations each, 72-hour horizon, 1-2 forecast "
         "receipts. Every policy is replayed through the same world with the same payment arithmetic.", "",
         "| Policy | Critical paid on time | Critical late (silent) | Penalty score (mean, 95% CI) | Floor breached | "
         "FX + fees (USD) | Unvetted payees paid /100 | Escalations /world | Warned ahead (h) |",
         "|---|---|---|---|---|---|---|---|---|"]
    for k in ORDER:
        s = summary[k]
        L.append(f"| {LABEL[k]} | {s['crit_on_time_pct']:.1f}% | {s['crit_late']} ({s['crit_silent']}) | "
                 f"{s['penalty_mean']:,.0f} [{s['penalty_ci'][0]:,.0f}, {s['penalty_ci'][1]:,.0f}] | "
                 f"{s['floor_breach_pct']:.1f}% | {s['cost_mean']:.0f} | {s['unvetted_per_100']:.1f} | "
                 f"{s['escalations_per_world']:.2f} | {s['warn_lead_h']:.1f} |")
    L += ["", "## Agent vs the strongest naive baseline (pay ASAP), same worlds, paired", "",
          "| Metric (agent minus baseline) | Mean difference | 95% CI | Agent better / tie / worse |", "|---|---|---|---|"]
    for key, name in (("penalty", "Penalty score"), ("crit_late", "Critical obligations late"), ("cost_usd", "FX + fees (USD)")):
        q = pair[key]
        L.append(f"| {name} | {q['mean_diff']:,.2f} | [{q['ci'][0]:,.2f}, {q['ci'][1]:,.2f}] | "
                 f"{q['a_better']} / {q['tie']} / {q['a_worse']} |")
    L += ["", "**How to read it.** *Critical* = operations_stop and time_sensitive obligations; *silent* = late with no "
          "prior escalation to a person; *warned ahead* = mean hours between that escalation and the due time. *Penalty score* sums the policy's lateness weights over late obligations "
          "(lower is better). *Floor breached* = pooled cash fell below the reserve floor at any point. Obligations to "
          "new beneficiaries need a person's approval, so they are excluded from the lateness metrics for every policy.",
          "", "**Assumptions and limits.** Forecasts are calibrated (confidence c arrives with probability c); emails "
          "are noise-free confidence updates (the LLM's real accuracy is not simulated here); FX rates are static; "
          "fees follow policy.json; the exact optimiser handles up to 16 open obligations, so worlds stay small; the "
          "agent's penalty weights are also the scoring weights, so also compare the unweighted columns. In the few worlds "
          "where the agent has more critical items late than pay-ASAP, the inspected cases are rule R5 at work: it "
          "escalates a larger critical payment it cannot fund inside the floor and holds cash for that decision, but "
          "this benchmark simulates no person answering the escalation, so held items stay unpaid. In the rest, the agent "
          "discounts forecast receipts by their confidence and so declines to commit cash that is only safe if the "
          "receipts arrive in full; the baseline spends first and happens to be right when they do arrive. The email signal "
          "adds little here because execution only ever spends settled cash; its value is an earlier warning, "
          "measured in the last column."]
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=1000)
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args()
    seeds = [(a.seed + i,) for i in range(a.n)]
    with ProcessPoolExecutor(a.jobs) as pool:
        worlds = list(pool.map(_job, seeds, chunksize=20))
    rows = {k: [asdict(w[k]) for w in worlds] for k in ORDER}
    summary = {k: summarise(rows[k]) for k in ORDER}
    pair = paired(rows, "agent", "eager_priority")
    md = report(summary, a.n, a.seed, pair)
    Path("results").mkdir(exist_ok=True)
    Path("results/benchmark.md").write_text(md)
    Path("results/benchmark.json").write_text(json.dumps({"n": a.n, "seed": a.seed, "summary": summary, "paired_agent_vs_eager": pair}, indent=2))
    print(md)


if __name__ == "__main__":
    main()