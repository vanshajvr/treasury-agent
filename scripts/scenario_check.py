import os, json, requests
from dotenv import load_dotenv

load_dotenv()
BASE = os.getenv("AIRWALLEX_BASE_URL", "https://api.sandbox.airwallex.com")
h = {"x-client-id": os.environ["AIRWALLEX_CLIENT_ID"],
     "x-api-key": os.environ["AIRWALLEX_API_KEY"]}
token = requests.post(f"{BASE}/api/v1/authentication/login", headers=h).json()["token"]
auth = {"Authorization": f"Bearer {token}"}

def usd_per(ccy):
    if ccy == "USD":
        return 1.0
    r = requests.get(f"{BASE}/api/v1/fx/rates/current", headers=auth,
                     params={"buy_currency": ccy, "sell_currency": "USD",
                             "buy_amount": 1000})
    r.raise_for_status()
    return r.json()["rate"]  # sell currency per 1 unit bought, i.e. USD per ccy

s = json.load(open("scenario.json"))
fx = {c: usd_per(c) for c in ("GBP", "EUR", "USD")}
print("USD per unit:", fx)

SWIFT_FEE_EUR = 12.85
need = {}
for o in s["obligations"]:
    amt = o["amount"] + (SWIFT_FEE_EUR if o["method"] == "SWIFT" else 0)
    need[o["id"]] = amt * fx[o["ccy"]]

mandate_usd = sum(v * fx[c] for c, v in s["mandate"].items())
usable = mandate_usd - s["reserve_floor_usd_equiv"]

for k, v in need.items():
    print(f"{k:12s} {v:10.2f} USD")
total = sum(need.values())
print(f"total need {total:.2f} | usable {usable:.2f} | shortfall {total - usable:.2f}")