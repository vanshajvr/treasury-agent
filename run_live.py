"""Plan the scenario, then execute the EXECUTE_NOW decisions against the Airwallex sandbox.

  python3 run_live.py --only freight              # DRY RUN (default): reads + quote, moves nothing
  python3 run_live.py --only freight --live       # real sandbox calls, SENT -> PAID simulated
  python3 run_live.py --live                      # every EXECUTE_NOW decision
  python3 run_live.py --scenario my_test.json ... # experiment without touching scenario.json

Escalated / held / scheduled items are printed and never executed. Re-running is safe: the ledger
remembers request ids and completed steps.
"""
import argparse
import json
from pathlib import Path

from treasury import (EXECUTE_NOW, format_plan, load_policy, load_scenario, plan, with_confidence)
from treasury.airwallex import AirwallexClient
from treasury.executor import Executor
from treasury.ledger import Ledger


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="scenario.json", help="scenario file (copy it for experiments)")
    ap.add_argument("--live", action="store_true", help="actually call mutating endpoints")
    ap.add_argument("--only", nargs="*", help="obligation ids to execute (default: all EXECUTE_NOW)")
    ap.add_argument("--confidence", type=float, help="override forecast confidence")
    ap.add_argument("--no-simulate", action="store_true", help="do not push the transfer to SENT/PAID")
    a = ap.parse_args()

    policy = load_policy("policy.json")
    s = load_scenario(a.scenario)
    if a.confidence is not None:
        s = with_confidence(s, s.receipts[0].id, a.confidence)
    bens = json.loads(Path("beneficiaries.json").read_text()) if Path("beneficiaries.json").exists() else {}

    p = plan(s, policy)
    print(format_plan(p, s))
    print(f"\n--- executing ({'LIVE' if a.live else 'DRY RUN'}) ---")

    ex = Executor(AirwallexClient(), Ledger("ledger.json"), bens, dry_run=not a.live, simulate=not a.no_simulate)
    todo = [o for o in s.obligations if p.decisions[o.id].action == EXECUTE_NOW and (not a.only or o.id in a.only)]
    for ob in todo:
        r = ex.execute(ob, p.decisions[ob.id])
        if r.status == "E4_DRIFT":  # re-plan once with the quoted rate, then escalate
            s2 = s.clone()
            s2.rates[ob.ccy] = r.detail["quote_rate"]
            p2 = plan(s2, policy)
            d2 = p2.decisions[ob.id]
            print(f"{ob.id}: E4 drift {r.detail['drift']:.2%}; re-planned at {r.detail['quote_rate']} -> {d2.action}")
            r = ex.execute(ob, d2) if d2.action == EXECUTE_NOW else r
            if r.status == "E4_DRIFT":
                print(f"{ob.id}: ESCALATE (E4) - quote still differs from planning rate; a person must approve")
                continue
        print(f"{ob.id}: {r.status} {json.dumps(r.detail)}")
        rec = r.detail.get("reconciliation")
        if rec and not rec["ok"]:
            print(f"{ob.id}: RECONCILIATION MISMATCH planned {rec['planned_debit']} actual {rec['actual_debit']} "
                  f"diff {rec['diff']} - update policy.json fees or investigate")
    skipped = [o.id for o in s.obligations if o not in todo]
    print(f"\nnot executed (by plan or --only): {skipped}")


if __name__ == "__main__":
    main()