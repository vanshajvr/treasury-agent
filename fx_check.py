import os, requests
from dotenv import load_dotenv
load_dotenv()
BASE = "https://api-demo.airwallex.com"
h = {"x-client-id": os.environ["AIRWALLEX_CLIENT_ID"],
     "x-api-key": os.environ["AIRWALLEX_API_KEY"]}
token = requests.post(f"{BASE}/api/v1/authentication/login", headers=h).json()["token"]
auth = {"Authorization": f"Bearer {token}"}

r = requests.get(f"{BASE}/api/v1/fx/rates/current", headers=auth,
                 params={"buy_currency": "USD", "sell_currency": "HKD", "buy_amount": 1000})
print(r.status_code, r.text)