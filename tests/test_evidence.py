from dataclasses import replace
from pathlib import Path

import pytest

from treasury import (EXECUTE_NOW, SCHEDULE, ESCALATE, Evidence, apply_execution, corroborate, load_evidence,
                      load_policy, load_scenario, plan, with_confidence)
from treasury.models import Receipt

ROOT = Path(__file__).resolve().parent.parent
CFG = load_policy(ROOT / "policy.json")
RULES = CFG.evidence
R = Receipt(id="customer_a", payer="Customer A", ccy="USD", amount=8000, due_h=30, confidence=0.9)
UETR = "9f2c1d7e-3b4a-4c8e-9a1f-6d5e2b7c8a90"
INV = Evidence("invoice", "INV-1042", "Customer A", "USD", 8000, 30, "approved")
REM = Evidence("remittance", UETR, "Customer A", "USD", 8000, 28, "sent")


def test_approved_invoice_lifts_a_middling_forecast_into_the_top_tier():
    c = corroborate(R, [INV], 0.6, RULES)
    assert c.confidence == pytest.approx(RULES.invoice_conf) and c.matched == ("INV-1042",)
    assert CFG.tier_for(c.confidence).limit_usd > CFG.tier_for(0.6).limit_usd


def test_invoice_does_not_override_a_contradicting_email():
    c = corroborate(R, [INV], 0.1, RULES, contradicted=True)
    assert c.confidence == 0.1 and not c.matched and "not the timing" in c.rejected[0][1]


def test_bank_remittance_outranks_a_delay_email():
    c = corroborate(R, [REM], 0.1, RULES, contradicted=True)
    assert c.confidence == pytest.approx(RULES.remittance_conf)


def test_evidence_never_lowers_confidence():
    assert corroborate(R, [INV], 0.95, RULES).confidence == 0.95


@pytest.mark.parametrize("ev, why", [
    (replace(INV, payer="Customer B"), "payer"),
    (replace(INV, ccy="EUR"), "currency"),
    (replace(INV, amount=7000), "amount"),
    (replace(INV, date_h=30 + RULES.grace_h + 1), "later than"),
    (replace(INV, status="issued"), "not 'approved'"),
    (replace(INV, status="disputed"), "not 'approved'"),
    (replace(REM, status="recalled"), "not 'sent'"),
    (replace(REM, ref="MT103-12345"), "UETR"),
    (replace(INV, kind="screenshot"), "unknown evidence kind"),
])
def test_mismatched_records_are_rejected_with_a_reason(ev, why):
    c = corroborate(R, [ev], 0.6, RULES)
    assert c.confidence == 0.6 and not c.matched and why in c.rejected[0][1]


def test_payer_match_ignores_case_and_punctuation_and_small_rounding():
    ev = replace(INV, payer="customer-a", amount=8000 * (1 + RULES.amount_tolerance / 2))
    assert corroborate(R, [ev], 0.6, RULES).matched == ("INV-1042",)


def test_a_reference_cannot_corroborate_two_receipts():
    c = corroborate(R, [REM], 0.1, RULES, used_refs=frozenset({UETR}))
    assert c.confidence == 0.1 and "already" in c.rejected[0][1]


def test_strongest_matching_record_wins():
    assert corroborate(R, [INV, REM], 0.5, RULES).confidence == pytest.approx(RULES.remittance_conf)


def test_remittance_turns_the_demo_escalation_back_into_a_scheduled_payment():
    s = load_scenario(ROOT / "scenario.json")
    s = with_confidence(apply_execution(s, plan(s, CFG)), "customer_a", 0.1)
    assert plan(s, CFG).decisions["supplier"].action == ESCALATE
    c = corroborate(s.receipts[0], load_evidence(ROOT / "evidence.json"), 0.1, RULES, contradicted=True)
    p = plan(with_confidence(s, "customer_a", c.confidence), CFG)
    assert p.decisions["supplier"].action == SCHEDULE                 # autonomous again...
    assert all(d.action != EXECUTE_NOW for d in p.decisions.values())  # ...but nothing pays before settlement (R1)


def test_load_evidence_rejects_unknown_kinds(tmp_path):
    f = tmp_path / "ev.json"
    f.write_text('[{"kind": "rumour", "ref": "x", "payer": "A", "ccy": "USD", "amount": 1, "date_h": 1, "status": "sent"}]')
    with pytest.raises(ValueError):
        load_evidence(f)
