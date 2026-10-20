import os, requests
from dotenv import load_dotenv

load_dotenv()
BASE = "https://api-demo.airwallex.com"
ACCOUNT_ID = "2670e727-c1cd-4adc-8755-e6309563ae60"

h = {"x-client-id": os.environ["AIRWALLEX_CLIENT_ID"],
     "x-api-key": os.environ["AIRWALLEX_API_KEY"]}
token = requests.post(f"{BASE}/api/v1/authentication/login", headers=h).json()["token"]
auth = {"Authorization": f"Bearer {token}"}

d = requests.post(f"{BASE}/api/v1/simulation/deposit/create", headers=auth,
                  json={"global_account_id": ACCOUNT_ID, "amount": 25000,
                        "payer_name": "Seed funding"})
print("deposit:", d.status_code, d.text)

b = requests.get(f"{BASE}/api/v1/balances/current", headers=auth)
print("balances:", b.status_code, b.text)