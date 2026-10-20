"""Offline demo of the Treasury Controller flow (no network, no money moves).

goal -> initial plan -> new information -> revised decision -> human choice
     -> deposit -> incremental recompute -> execution -> remaining reserve
"""
from treasury import (apply_deposit, apply_execution, format_plan, load_policy, load_scenario,
                      plan, propose_confidence, replan, usd_eq, with_confidence)

EMAIL = ("Hi, our internal approval has been delayed and we won't be able to release the USD 8,000 "
         "payment this week. Likely next month. Sorry for the trouble.")


def banner(text: str) -> None:
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}")


def wallets_line(s) -> str:
    cash = ", ".join(f"{c} {v:,.2f}" for c, v in s.wallets.items())
    return f"settled cash: {cash} | USD-equiv {usd_eq(s.wallets, s.rates):,.2f} (floor {s.reserve_floor_usd:,.0f})"


policy = load_policy("policy.json")
s0 = load_scenario("scenario.json")
receipt = s0.receipts[0]

banner("1. GOAL: pay what matters over 72h, keep the reserve floor, never act on unsettled money")
print(wallets_line(s0))
print(f"forecast: {receipt.payer} pays {receipt.ccy} {receipt.amount:,.0f} at {receipt.due_h:g}h "
      f"(confidence {receipt.confidence:.2f})")

banner("2. INITIAL PLAN (confidence 0.90)")
p1 = plan(s0, policy)
print(format_plan(p1, s0))
s1 = apply_execution(s0, p1)
print(f"\nexecuted now: {[o for o, d in p1.decisions.items() if d.action == 'EXECUTE_NOW']}")
print(wallets_line(s1))

banner("3. NEW INFORMATION: customer email arrives")
print(f'"{EMAIL}"')
conf, why = propose_confidence(EMAIL, receipt.confidence)
print(f"\nsignal (stub for the LLM step): confidence {receipt.confidence:.2f} -> {conf:.2f} ({why})")
s1 = with_confidence(s1, receipt.id, conf)

banner("4. REVISED DECISION")
p2 = plan(s1, policy)
print(format_plan(p2, s1))
print("\nthe SAME supplier conversion is now blocked from autonomous execution and escalated to a person")

banner("5. HUMAN CHOICE (simulated): wait_for_receipt")
print("approver picks 'wait_for_receipt'. Contractor and tooling stay on HOLD so the cash is preserved.")

banner("6. DEPOSIT SETTLES: USD 8,000 arrives; re-plan only what the USD pool can affect")
s2 = apply_deposit(s1, receipt.id)
print(wallets_line(s2))
p3, locked, recomputed = replan(p2, s2, policy, {"USD"})
print(f"locked (unaffected): {locked or 'none'} | recomputed: {recomputed}\n")
print(format_plan(p3, s2))

banner("7. EXECUTION AND FINAL CHECK")
s3 = apply_execution(s2, p3)
print(f"executed: {[o for o, d in p3.decisions.items() if d.action == 'EXECUTE_NOW']}")
print(wallets_line(s3))
ok = usd_eq(s3.wallets, s3.rates) >= s3.reserve_floor_usd
print(f"reserve floor respected: {ok}")
print(f"still open: {[o.id for o in s3.obligations]} -> deferred or waiting for a person")