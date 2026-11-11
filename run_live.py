"""Plan the scenario, then execute the EXECUTE_NOW decisions against the Airwallex sandbox.

  python3 run_live.py --only freight                 # DRY RUN (default): balance reads + quotes, moves nothing
  python3 run_live.py --only freight --live          # real sandbox payout
  python3 run_live.py --deposit --live               # story: pay what is settled, receipt lands, re-plan, pay the rest
  python3 run_live.py --scenario my_test.json ...    # experiment without touching scenario.json

Escalated / held / scheduled items are printed and never executed. Re-running is safe: the ledger
remembers request ids and completed steps.
"""
import argparse
import json
import os
from pathlib import Path

from treasury import (EXECUTE_NOW, apply_deposit, format_plan, load_policy, load_scenario, plan, replan,
                      with_confidence)
from treasury.airwallex import AirwallexClient
from treasury.executor import Executor
from treasury.ledger import Ledger
from treasury.models import BASE

DONE = ("PAID_SIM", "TRANSFER_CREATED", "SKIPPED")


def run_batch(ex, s, p, policy, only, dry):
    """Execute the EXECUTE_NOW decisions of plan p. Returns the ids that are done (or would be, on a dry run)."""
    ok_ids = set()
    todo = [o for o in s.obligations if p.decisions[o.id].action == EXECUTE_NOW and (not only or o.id in only)]
    for ob in todo:
        r = ex.execute(ob, p.decisions[ob.id])
        if r.status == "E4_DRIFT":  # re-plan once with the quoted rate, then escalate
            s2 = s.clone()
            s2.rates[ob.ccy] = r.detail["quote_rate"]
            d2 = plan(s2, policy).decisions[ob.id]
            print(f"{ob.id}: E4 drift {r.detail['drift']:.2%}; re-planned at {r.detail['quote_rate']} -> {d2.action}")
            r = ex.execute(ob, d2) if d2.action == EXECUTE_NOW else r
            if r.status == "E4_DRIFT":
                print(f"{ob.id}: ESCALATE (E4) - quote still differs from planning rate; a person must approve")
                continue
        print(f"{ob.id}: {r.status} {json.dumps(r.detail)}")
        if r.status == "TRANSFER_FAILED":
            print(f"{ob.id}: ESCALATE - transfer {r.detail.get('transfer_status')} "
                  f"({r.detail.get('failure_type')}: {r.detail.get('failure_reason')}); {r.detail.get('funds_note')}. "
                  f"Not retried: a person must call Executor.release_failed after fixing the cause")
            continue
        rec = r.detail.get("reconciliation")
        if rec and not rec["ok"]:
            print(f"{ob.id}: RECONCILIATION MISMATCH expected {rec['planned_debit']} actual {rec['actual_debit']} "
                  f"diff {rec['diff']} - money moved that Airwallex's transfer record does not explain; investigate")
        fee = (rec or {}).get("fee") or {}
        if abs(fee.get("variance", 0)) > 0.005:
            print(f"{ob.id}: fee {fee['ccy']} {fee['charged']:.2f} charged vs {fee['estimated']:.2f} estimated "
                  f"({fee['variance']:+.2f}); reconciled against Airwallex's record")
        if r.status in DONE or (dry and r.status == "DRY_RUN_OK"):
            ok_ids.add(ob.id)
    print(f"not executed in this batch: {[o.id for o in s.obligations if o not in todo]}")
    return ok_ids


def book(s, p, done_ids):
    """Offline view of the mandate after the given payouts (same arithmetic as the planner's apply_execution)."""
    s = s.clone()
    for oid in done_ids:
        d = p.decisions[oid].draw
        s.wallets[d.ccy] = s.wallets.get(d.ccy, 0.0) - d.from_wallet
        s.wallets[BASE] = s.wallets.get(BASE, 0.0) - d.usd_spent
    s.obligations = [o for o in s.obligations if o.id not in done_ids]
    return s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="scenario.json", help="scenario file (copy it for experiments)")
    ap.add_argument("--live", action="store_true", help="actually call mutating endpoints")
    ap.add_argument("--only", nargs="*", help="obligation ids to execute (default: all EXECUTE_NOW)")
    ap.add_argument("--confidence", type=float, help="override forecast confidence")
    ap.add_argument("--no-simulate", action="store_true", help="do not push the transfer to SENT/PAID")
    ap.add_argument("--deposit", action="store_true",
                    help="after the first batch, the forecast receipt settles: re-plan and run the second batch")
    ap.add_argument("--deposit-account", default=os.getenv("AIRWALLEX_DEPOSIT_ACCOUNT_ID"),
                    help="USD global account id: also simulate the deposit in the sandbox (optional)")
    a = ap.parse_args()

    policy = load_policy("policy.json")
    s = load_scenario(a.scenario)
    if a.confidence is not None:
        s = with_confidence(s, s.receipts[0].id, a.confidence)
    bens = json.loads(Path("beneficiaries.json").read_text()) if Path("beneficiaries.json").exists() else {}
    client = AirwallexClient()
    ex = Executor(client, Ledger("ledger.json"), bens, dry_run=not a.live, simulate=not a.no_simulate)
    mode = "LIVE" if a.live else "DRY RUN"

    p = plan(s, policy)
    print(format_plan(p, s))
    print(f"\n--- batch 1: settled cash only ({mode}) ---")
    done = run_batch(ex, s, p, policy, a.only, not a.live)
    if not a.deposit:
        return

    s1 = book(s, p, done)
    receipt = s1.receipts[0]
    print(f"\n--- the forecast receipt settles: {receipt.payer} pays {receipt.ccy} {receipt.amount:,.0f} ---")
    if a.deposit_account and a.live:
        client.post("/api/v1/simulation/deposit/create", {"global_account_id": a.deposit_account,
                                                           "amount": receipt.amount, "payer_name": receipt.payer})
        print("sandbox deposit simulated")
    else:
        print("(mandate view only: the sandbox wallets already hold far more; pass --deposit-account to simulate a real deposit)")
    s2 = apply_deposit(s1, receipt.id)
    p2, locked, recomputed = replan(plan(s1, policy), s2, policy, {receipt.ccy})
    print(f"locked (unaffected): {locked or 'none'} | recomputed: {recomputed}\n")
    print(format_plan(p2, s2))
    print(f"\n--- batch 2: after settlement ({mode}) ---")
    run_batch(ex, s2, p2, policy, a.only, not a.live)


if __name__ == "__main__":
    main()