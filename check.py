import os, requests
from dotenv import load_dotenv

load_dotenv()
BASE = "https://api-demo.airwallex.com"  # sandbox; confirm against the API reference

r = requests.post(f"{BASE}/api/v1/authentication/login", headers={
    "x-client-id": os.environ["AIRWALLEX_CLIENT_ID"],
    "x-api-key": os.environ["AIRWALLEX_API_KEY"],
})
print("login:", r.status_code)
r.raise_for_status()
token = r.json()["token"]

a = requests.get(f"{BASE}/api/v1/global_accounts",
                 headers={"Authorization": f"Bearer {token}"})
print("global accounts:", a.status_code, a.json())