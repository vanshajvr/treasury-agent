"""Serve the demo page and its API from one process (standard library only).

  python3 server.py                  # http://127.0.0.1:8000
  PORT=8080 HOST=0.0.0.0 python3 server.py

Env: PORT, HOST, TRUST_PROXY=1 (use X-Forwarded-For behind a load balancer), PER_IP_PER_MIN, LLM_PER_DAY,
plus the LLM keys signals.py reads (GROQ_API_KEY, ...). Airwallex keys are never needed here: the API only replays
the scenario offline and cannot move money.
"""
from __future__ import annotations

import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from treasury.api import MAX_BODY_BYTES, DemoService

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def make_handler(service: DemoService, trust_proxy: bool):
    class Handler(BaseHTTPRequestHandler):
        server_version = "treasury-demo"
        sys_version = ""

        def _send(self, status: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            for k, v in HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, data: dict) -> None:
            self._send(status, json.dumps(data).encode(), "application/json")

        def _client(self) -> str:
            if trust_proxy and self.headers.get("X-Forwarded-For"):
                return self.headers["X-Forwarded-For"].split(",")[0].strip()
            return self.client_address[0]

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].split("#", 1)[0]
            if path == "/api/health":
                return self._json(*service.health())
            if path.startswith("/api/"):
                return self._json(404, {"error": "not found"})
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            target = (WEB / rel).resolve()
            if not target.is_relative_to(WEB) or not target.is_file():     # no path traversal out of web/
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, target.read_bytes(), ctype, "public, max-age=300")

        do_HEAD = do_GET

        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] != "/api/story":
                return self._json(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._json(400, {"error": "bad Content-Length"})
            if length <= 0 or length > MAX_BODY_BYTES:
                return self._json(413 if length > MAX_BODY_BYTES else 400, {"error": "Body missing or too large."})
            try:
                payload = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {"error": "Body must be JSON."})
            try:
                self._json(*service.story(payload, self._client()))
            except Exception as e:                                             # never leak a traceback
                self.log_error("story failed: %s: %s", type(e).__name__, e)
                self._json(500, {"error": "Something went wrong. Try another email."})

        def log_message(self, format: str, *args) -> None:  # noqa: A002  one line, no request bodies
            print(f"{self._client()} {format % args}", flush=True)

    return Handler


def main() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    service = DemoService(ROOT, per_ip_per_min=int(os.getenv("PER_IP_PER_MIN", "6")),
                          llm_per_day=int(os.getenv("LLM_PER_DAY", "300")))
    host, port = os.getenv("HOST", "127.0.0.1"), int(os.getenv("PORT", "8000"))
    httpd = ThreadingHTTPServer((host, port), make_handler(service, os.getenv("TRUST_PROXY") == "1"))
    print(f"serving http://{host}:{port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
