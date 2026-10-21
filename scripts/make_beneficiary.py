"""Create sandbox beneficiaries for the scenario payees.

  python3 scripts/make_beneficiary.py schema   [freight|supplier|contractor]   # required fields for the corridor
  python3 scripts/make_beneficiary.py validate [freight|supplier|contractor]   # dry check, nothing created
  python3 scripts/make_beneficiary.py create   [freight|supplier|contractor]   # create, save id to beneficiaries.json

Default payee is freight. Compare each payload with the `schema` output first. Sandbox data only: fake names and numbers.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from treasury.airwallex import AirwallexClient, AirwallexError  # noqa: E402

OUT = Path("beneficiaries.json")

PAYEES = {
    # key: payee name exactly as in scenario.json, corridor, payload
    "freight": {
        "payee": "Freight forwarder", "country": "GB", "currency": "GBP", "method": "LOCAL",
        "beneficiary": {
            "entity_type": "COMPANY", "company_name": "Northwind Freight Ltd",
            "address": {"street_address": "1 Test Street", "city": "London", "postcode": "EC1A 1BB", "country_code": "GB"},
            "bank_details": {"account_currency": "GBP", "account_name": "Northwind Freight Ltd",
                             "account_number": "12345678", "account_routing_type1": "sort_code",
                             "account_routing_value1": "200000", "bank_country_code": "GB", "bank_name": "Test Bank PLC"},
        },
    },
    "supplier": {
        "payee": "EU component supplier", "country": "DE", "currency": "EUR", "method": "SWIFT",
        "beneficiary": {
            "entity_type": "COMPANY", "company_name": "Bauteile GmbH",
            "address": {"street_address": "1 Teststrasse", "city": "Berlin", "postcode": "10115", "country_code": "DE"},
            "bank_details": {"account_currency": "EUR", "account_name": "Bauteile GmbH",
                             "iban": "DE89370400440532013000", "swift_code": "COBADEFFXXX",
                             "bank_country_code": "DE", "bank_name": "Test Bank AG"},
        },
    },
    "contractor": {
        "payee": "US contractor", "country": "US", "currency": "USD", "method": "LOCAL",
        "beneficiary": {
            "entity_type": "COMPANY", "company_name": "Lakeside Design LLC",
            "address": {"street_address": "1 Test Avenue", "city": "New York", "state": "NY",
                        "postcode": "10001", "country_code": "US"},
            "bank_details": {"account_currency": "USD", "account_name": "Lakeside Design LLC",
                             "account_number": "123456789", "account_routing_type1": "aba",
                             "account_routing_value1": "021000021", "bank_account_category": "Checking",
                             "bank_country_code": "US", "bank_name": "Test Bank NA"},
        },
    },
}


def main(mode: str, key: str) -> None:
    if key not in PAYEES or mode not in ("schema", "validate", "create"):
        sys.exit(__doc__)
    spec = PAYEES[key]
    body = {"beneficiary": spec["beneficiary"], "transfer_methods": [spec["method"]]}
    c = AirwallexClient()
    try:
        if mode == "schema":
            out = c.post("/api/v1/beneficiaries/schema",
                         {"bank_country_code": spec["country"], "account_currency": spec["currency"],
                          "entity_type": spec["beneficiary"]["entity_type"], "transfer_method": spec["method"]})
            print(json.dumps(out, indent=2))
        elif mode == "validate":
            print(json.dumps(c.post("/api/v1/beneficiaries/validate", body), indent=2))
        else:
            out = c.post("/api/v1/beneficiaries/create", body)
            bid = out.get("beneficiary_id") or out.get("id")
            data = json.loads(OUT.read_text()) if OUT.exists() else {}
            data[spec["payee"]] = {"beneficiary_id": bid}
            OUT.write_text(json.dumps(data, indent=2))
            print(f"created {bid} for '{spec['payee']}' -> saved to {OUT}")
    except AirwallexError as e:
        print(f"HTTP {e.status}: {e.body if isinstance(e.body, str) else json.dumps(e.body, indent=2)}")
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "", sys.argv[2] if len(sys.argv) > 2 else "freight")