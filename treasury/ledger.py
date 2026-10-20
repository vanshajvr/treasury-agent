"""Crash-safe execution ledger (R3).

Every request_id is generated and written to disk BEFORE the API call that uses it. After a crash,
a re-run reads the same ids back, so a retry can never become a second payment.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path


class Ledger:
    def __init__(self, path: str | os.PathLike = "ledger.json"):
        self.path = Path(path)
        self.data: dict = json.loads(self.path.read_text()) if self.path.exists() else {}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        os.replace(tmp, self.path)  # atomic: never leaves a half-written file

    def entry(self, obligation_id: str) -> dict:
        return self.data.setdefault(obligation_id, {"stage": "NEW"})

    def request_id(self, obligation_id: str, kind: str) -> str:
        """Return the stored id for (obligation, kind), creating and persisting it on first use."""
        e = self.entry(obligation_id)
        key = f"{kind}_request_id"
        if key not in e:
            e[key] = str(uuid.uuid4())
            self._save()
        return e[key]

    def update(self, obligation_id: str, **fields) -> None:
        self.entry(obligation_id).update(fields)
        self._save()

    def stage(self, obligation_id: str) -> str:
        return self.entry(obligation_id)["stage"]