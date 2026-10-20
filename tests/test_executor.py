from pathlib import Path

import pytest

from treasury import EXECUTE_NOW, load_policy, load_scenario, plan, with_confidence
from treasury.airwallex import AmbiguousResult
from treasury.executor import Executor
from treasury.ledger import Ledger

ROOT = Path(__file__).resolve().parent.parent
BEN = {"freight": {"beneficiary_id": "b1"}, "Supplier": {"beneficiary_id": "b2"}}


class FakeClient:
    def __init__(self, usd=10000.0, gbp=10000.0, quote_mult=1.0, lose_transfer_response=False):
        self.bal = {"USD": usd, "GBP": gbp, "EUR": 10000.0}
        self.mult = quote_mult
        self.lose = lose_transfer_response
        self.calls = []
        self.transfers = []

    def get(self, path, params=None, **kw):
        self.calls.append(("GET", path))
        if path == "/api/v1/balances/current":
            return [{"currency": c, "available_amount": v} for c, v in self.bal.items()]
        if path == "/api/v1/transfers":
            return {"items": self.transfers}
        return {"items": []}

    def post(self, path, body, **kw):
        self.calls.append(("POST", path, body))
        if path.endswith("/quotes/create"):
            return {"quote_id": "q1", "buy_amount": body["buy_amount"],
                    "sell_amount": f"{float(body['buy_amount']) * {'GBP': 1.347088, 'EUR': 1.134843}[body['buy_currency']] * self.mult:.4f}"}
        return {"id": "x"}

    def post_idempotent(self, path, body, request_id, lookup):
        body = {**body, "request_id": request_id}
        if path.endswith("/transfers/create"):
            self.calls.append(("POST", path, body))
            if not any(t["request_id"] == request_id for t in self.transfers):
                self.transfers.append({"id": "t1", "request_id": request_id, "status": "PROCESSING"})
            if self.lose:
                self.lose = False
                found = lookup(request_id)
                assert found is not None
                return found
            return self.transfers[-1]
        self.calls.append(("POST", path, body))
        return {"id": "c1"}


def setup(tmp_path, client, **kw):
    pol = load_policy(ROOT / "policy.json")
    s = with_confidence(load_scenario(ROOT / "scenario.json"), "customer_a", 0.9)
    p = plan(s, pol)
    ob = next(o for o in s.obligations if o.id == "freight")
    ex = Executor(client, Ledger(tmp_path / "l.json"), {ob.payee: {"beneficiary_id": "b1"}}, **kw)
    return s, p, ob, ex


def test_dry_run_sends_nothing_mutating(tmp_path):
    c = FakeClient()
    s, p, ob, ex = setup(tmp_path, c, dry_run=True)
    r = ex.execute(ob, p.decisions["freight"])
    assert r.status == "DRY_RUN_OK"
    assert not [x for x in c.calls if x[0] == "POST" and "transfers" in x[1]]


def test_live_pays_and_second_run_is_noop(tmp_path):
    c = FakeClient()
    s, p, ob, ex = setup(tmp_path, c, dry_run=False)
    assert ex.execute(ob, p.decisions["freight"]).status == "PAID_SIM"
    assert ex.execute(ob, p.decisions["freight"]).status == "SKIPPED"
    assert len([t for t in c.transfers]) == 1


def test_unsettled_cash_blocks_execution(tmp_path):
    c = FakeClient(gbp=100.0)
    s, p, ob, ex = setup(tmp_path, c, dry_run=False)
    assert ex.execute(ob, p.decisions["freight"]).status == "NOT_SETTLED"
    assert not c.transfers


def test_lost_transfer_response_is_looked_up_not_resent(tmp_path):
    c = FakeClient(lose_transfer_response=True)
    s, p, ob, ex = setup(tmp_path, c, dry_run=False)
    assert ex.execute(ob, p.decisions["freight"]).status == "PAID_SIM"
    assert len(c.transfers) == 1


def test_request_id_persisted_and_reused(tmp_path):
    l1 = Ledger(tmp_path / "l.json")
    rid = l1.request_id("freight", "transfer")
    assert Ledger(tmp_path / "l.json").request_id("freight", "transfer") == rid


def test_e4_drift_blocks_conversion(tmp_path):
    pol = load_policy(ROOT / "policy.json")
    s = with_confidence(load_scenario(ROOT / "scenario.json"), "customer_a", 0.9)
    s.wallets = {"USD": 20000.0, "GBP": 0.0, "EUR": 0.0}  # force a conversion for freight
    p = plan(s, pol)
    ob = next(o for o in s.obligations if o.id == "freight")
    assert p.decisions["freight"].action == EXECUTE_NOW and p.decisions["freight"].draw.converted > 0
    c = FakeClient(quote_mult=1.02)
    ex = Executor(c, Ledger(tmp_path / "l.json"), {ob.payee: {"beneficiary_id": "b1"}}, dry_run=False)
    r = ex.execute(ob, p.decisions["freight"])
    assert r.status == "E4_DRIFT" and r.detail["drift"] > 0.005
    assert not [x for x in c.calls if len(x) > 1 and "conversions/create" in x[1]]
    ok = Executor(FakeClient(quote_mult=1.003), Ledger(tmp_path / "l2.json"), {ob.payee: {"beneficiary_id": "b1"}}, dry_run=False)
    assert ok.execute(ob, p.decisions["freight"]).status == "PAID_SIM"