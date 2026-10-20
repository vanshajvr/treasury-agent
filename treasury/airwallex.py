"""Thin Airwallex REST client: auth, token refresh, retries, idempotent POSTs.

Policy rules enforced here
  R3  every mutating call carries a request_id that is decided BEFORE the call and reused on retry.
  R4  after a timeout / unclear response, the caller's `lookup` is asked whether the operation
      already happened before anything is sent again.
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Callable

import requests
from dotenv import load_dotenv

DEFAULT_BASE = "https://api.sandbox.airwallex.com"
TOKEN_TTL_S = 25 * 60  # tokens last 30 min; refresh early
RETRY_STATUS = {429, 500, 502, 503, 504}


class AirwallexError(Exception):
    def __init__(self, status: int, body, message: str = ""):
        super().__init__(message or f"HTTP {status}: {body}")
        self.status = status
        self.body = body


class AmbiguousResult(Exception):
    """The call may or may not have happened (timeout / connection drop) and could not be resolved."""


def new_request_id() -> str:
    return str(uuid.uuid4())  # 36 chars, inside the 10-50 limit for transfers


class AirwallexClient:
    def __init__(self, client_id: str | None = None, api_key: str | None = None,
                 base_url: str | None = None, timeout: float = 20.0, max_retries: int = 3,
                 session: requests.Session | None = None):
        load_dotenv()
        cid = client_id or os.getenv("AIRWALLEX_CLIENT_ID") or os.getenv("CLIENT_ID")
        key = api_key or os.getenv("AIRWALLEX_API_KEY") or os.getenv("API_KEY")
        if not cid or not key:
            raise RuntimeError("Missing credentials: set AIRWALLEX_CLIENT_ID and AIRWALLEX_API_KEY in .env")
        self.client_id: str = cid
        self.api_key: str = key
        self.base_url = (base_url or os.getenv("AIRWALLEX_BASE_URL") or DEFAULT_BASE).rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.http = session or requests.Session()
        self._token: str | None = None
        self._token_at = 0.0

    # ------------------------------------------------------------------ auth
    def login(self) -> str:
        r = self.http.post(f"{self.base_url}/api/v1/authentication/login",
                           headers={"x-client-id": self.client_id, "x-api-key": self.api_key},
                           timeout=self.timeout)
        if r.status_code not in (200, 201):
            raise AirwallexError(r.status_code, _body(r), "login failed")
        token: str = r.json()["token"]
        self._token = token
        self._token_at = time.time()
        return token

    def _auth_header(self) -> dict:
        if not self._token or time.time() - self._token_at > TOKEN_TTL_S:
            self.login()
        return {"Authorization": f"Bearer {self._token}"}

    # ------------------------------------------------------------------ requests
    def request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None,
                headers: dict | None = None) -> dict:
        """One logical request. Retries 429/5xx/timeouts with the SAME body, so a request_id inside
        `json` is reused. Raises AmbiguousResult if a mutating call may have gone through."""
        url = f"{self.base_url}{path}"
        mutating = method.upper() != "GET"
        relogged = False
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                r = self.http.request(method, url, params=params, json=json, timeout=self.timeout,
                                      headers={**self._auth_header(), **(headers or {})})
            except (requests.Timeout, requests.ConnectionError) as e:
                last_exc = e
                if mutating:
                    raise AmbiguousResult(f"{method} {path}: {e}") from e  # caller must look up, not resend
                time.sleep(min(2 ** attempt, 8))
                continue
            if r.status_code == 401 and not relogged:
                relogged = True
                self._token = None
                continue
            if r.status_code in RETRY_STATUS:
                if mutating and r.status_code >= 500:
                    raise AmbiguousResult(f"{method} {path}: HTTP {r.status_code}")
                time.sleep(min(2 ** attempt, 8))  # 429 or GET: safe to retry
                last_exc = AirwallexError(r.status_code, _body(r))
                continue
            if r.status_code >= 400:
                raise AirwallexError(r.status_code, _body(r))
            return r.json() if r.content else {}
        raise last_exc or AirwallexError(0, None, "request failed")

    def get(self, path: str, **kw) -> dict:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: dict, **kw) -> dict:
        return self.request("POST", path, json=body, **kw)

    def post_idempotent(self, path: str, body: dict, request_id: str,
                        lookup: Callable[[str], dict | None]) -> dict:
        """POST with a pre-decided request_id (R3). On an ambiguous outcome, ask `lookup(request_id)`
        whether it already happened (R4); resend only if the lookup says it did not."""
        body = {**body, "request_id": request_id}
        try:
            return self.post(path, body)
        except AmbiguousResult:
            found = lookup(request_id)
            if found is not None:
                return found
            return self.post(path, body)  # same request_id: the 7-day duplicate window protects us


def _body(r: requests.Response):
    try:
        return r.json()
    except ValueError:
        return r.text