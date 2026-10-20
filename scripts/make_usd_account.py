"""Find (or create once) a USD Global Account in the sandbox and print its id.

  python3 scripts/make_usd_account.py

Then add the printed id to .env as:  AIRWALLEX_DEPOSIT_ACCOUNT_ID=<id>
run_live.py --deposit --live will then simulate the customer's USD 8,000 deposit into it for real.
The request_id is fixed, so running this twice can never create a second account (guide: reuse an id only for a retry).
"""
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from treasury.airwallex import AirwallexClient, AirwallexError  # noqa: E402

REQUEST_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "treasury-agent/usd-global-account"))


def has_usd(item: dict) -> bool:
    return '"USD"' in json.dumps(item)


def main() -> None:
    c = AirwallexClient()
    try:
        data = c.get("/api/v1/global_accounts")
        items = data if isinstance(data, list) else data.get("items", [])
        print(f"{len(items)} global account(s) found")
        for it in items:
            print(f"  {it.get('id')}  usd={has_usd(it)}  status={it.get('status')}  name={it.get('account_name')}")
        usd = next((it for it in items if has_usd(it)), None)
        if usd is None:
            usd = c.post("/api/v1/global_accounts/create", {
                "request_id": REQUEST_ID, "country_code": "US",
                "required_features": [{"currency": "USD", "transfer_method": "LOCAL"}]})
            print("created a USD global account")
        print(f"\nAIRWALLEX_DEPOSIT_ACCOUNT_ID={usd.get('id')}")
    except AirwallexError as e:
        print(f"HTTP {e.status}: {e.body if isinstance(e.body, str) else json.dumps(e.body, indent=2)}")
        sys.exit(1)


if __name__ == "__main__":
    main()