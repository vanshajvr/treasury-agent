"""Offline demo of the Treasury Controller flow (no network unless an LLM key is set; no money moves).

goal -> initial plan -> new information -> revised decision -> evidence check -> human choice or bank evidence
     -> deposit -> incremental recompute -> execution -> remaining reserve

    python run_demo.py               a person answers the escalation ("wait_for_receipt")
    python run_demo.py --remittance  the payer's bank remittance advice arrives instead and code lifts the limit
"""
import sys

from treasury import load_evidence, load_policy, load_scenario
from treasury.story import build_story

EMAIL = ("Hi, our internal approval has been delayed and we won't be able to release the USD 8,000 "
         "payment this week. Likely next month. Sorry for the trouble.")


def cash_line(c: dict) -> str:
    w = ", ".join(f"{k} {v:,.2f}" for k, v in c["wallets"].items())
    return f"settled cash: {w} | USD-equiv {c['usd_equiv']:,.2f} (floor {c['floor']:,.0f})"


def print_plan(p: dict) -> None:
    print(f"confidence {p['confidence']:.2f} | autonomous limit {p['limit_usd']:,.0f} USD | "
          f"forecast-reliant spend {p['reliant_usd']:,.0f} USD")
    for d in p["decisions"]:
        print(f"  {d['id']:<11} {d['action']:<20} {d['ccy']} {d['amount']:>9,.2f} due {d['due_h']:g}h")
        if "convert" in d:
            cv = d["convert"]
            fee = f" (incl. {d['ccy']} {cv['fee']:.2f} SWIFT fee)" if cv["fee"] else ""
            print(f"      convert USD {cv['usd']:,.2f} -> {d['ccy']} {cv['amount']:,.2f}{fee}")
        print(f"      {d['reason']}")
        for o in d["options"]:
            print(f"      option: {o}")


def main() -> None:
    path = "remittance" if "--remittance" in sys.argv else "person"
    story = build_story(load_scenario("scenario.json"), load_policy("policy.json"), EMAIL,
                        load_evidence("evidence.json"), path)
    for i, st in enumerate(story["steps"], 1):
        print(f"\n{'=' * 78}\n{i}. {st['title'].upper()}\n{'=' * 78}\n{st['text']}\n")
        if "receipt" in st:
            r = st["receipt"]
            print(f"forecast: {r['payer']} pays {r['ccy']} {r['amount']:,.0f} at {r['due_h']:g}h "
                  f"(confidence {r['confidence']:.2f})")
        if "email" in st:
            sg = st["signal"]
            print(f'"{st["email"]}"\n\nsignal [{sg["source"]}]: confidence {sg["before"]:.2f} -> {sg["after"]:.2f} '
                  f'({sg["reason"]})')
            if sg["evidence"]:
                print(f'evidence quoted from the email: "{sg["evidence"]}"')
        for r in st.get("remittance", []):
            print(f"  remittance UETR {r['ref']}: {r['payer']} {r['ccy']} {r['amount']:,.0f}, "
                  f"value date {r['date_h']:g}h, {r['status']}")
        if "corroboration" in st:
            c = st["corroboration"]
            for ref, why in c["rejected"]:
                print(f"  {ref}: not used. {why}")
            print(f"{c['reason']} (confidence {c['confidence']:.2f})")
        if "locked" in st:
            print(f"locked (unaffected): {st['locked'] or 'none'} | recomputed: {st['recomputed']}")
        if st["plan"]:
            print_plan(st["plan"])
        if "executed" in st:
            print(f"executed: {st['executed']}")
        print(cash_line(st["cash"]))
        if "floor_ok" in st:
            print(f"reserve floor respected: {st['floor_ok']} | still open: {st['still_open']}")


if __name__ == "__main__":
    main()
