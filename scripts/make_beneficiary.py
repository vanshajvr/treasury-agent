"""Create the freight beneficiary (GB / GBP / LOCAL) in the sandbox.

  python3 scripts/make_beneficiary.py schema     # print the required fields for this corridor
  python3 scripts/make_beneficiary.py validate   # dry check of the payload below, nothing created
  python3 scripts/make_beneficiary.py create     # create it and save the id to beneficiaries.json

Edit PAYLOAD after reading the schema output. Sandbox data only: use fake names and numbers.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from treasury.airwallex import AirwallexClient, AirwallexError  # noqa: E402

PAYEE = "Freight forwarder"  # must match the payee in scenario.json
OUT = Path("beneficiaries.json")

PAYLOAD = {
    "entity_type": "COMPANY",
    "company_name": "Northwind Freight Ltd",
    "address": {"street_address": "1 Test Street", "city": "London", "postcode": "EC1A 1BB", "country_code": "GB"},
    "bank_details": {
        "account_currency": "GBP",
        "account_name": "Northwind Freight Ltd",
        "account_number": "12345678",
        "account_routing_type1": "sort_code",   # lowercase, per the guide
        "account_routing_value1": "200000",
        "bank_country_code": "GB",
        "bank_name": "Test Bank PLC",           # required for GB
    },
}
BODY = {"beneficiary": PAYLOAD, "transfer_methods": ["LOCAL"]}


def main(mode: str) -> None:
    c = AirwallexClient()
    try:
        if mode == "schema":
            out = c.post("/api/v1/beneficiaries/schema",
                         {"bank_country_code": "GB", "account_currency": "GBP",
                          "entity_type": "COMPANY", "transfer_method": "LOCAL"})
            print(json.dumps(out, indent=2))
        elif mode == "validate":
            print(json.dumps(c.post("/api/v1/beneficiaries/validate", BODY), indent=2))
        elif mode == "create":
            out = c.post("/api/v1/beneficiaries/create", BODY)
            bid = out.get("beneficiary_id") or out.get("id")
            data = json.loads(OUT.read_text()) if OUT.exists() else {}
            data[PAYEE] = {"beneficiary_id": bid}
            OUT.write_text(json.dumps(data, indent=2))
            print(f"created {bid} -> saved to {OUT}")
        else:
            sys.exit(__doc__)
    except AirwallexError as e:
        print(f"HTTP {e.status}: {json.dumps(e.body, indent=2) if not isinstance(e.body, str) else e.body}")
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")