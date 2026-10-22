"""Test suite for the reconciliation engine. Run:

    python -m app.recon_engine.tests

Covers normalisation and each major rule + the spec example. Pure asserts, no
external test framework needed.
"""
from __future__ import annotations

from .config import ReconConfig
from .models import SapTxn, VendorTxn, Status
from .engine import reconcile
from .normalize import normalize_invoice, normalize_amount, normalize_date


def _statuses(report):
    return [r.match_status for r in report.results]


def _by_ref(report, ref):
    """Find a result whose SAP doc or vendor reference contains ref."""
    for r in report.results:
        if ref in (r.vendor_statement_reference or "") or ref in (r.sap_doc_number or ""):
            return r
    return None


# ---------------------------------------------------------------------------
def test_normalisation():
    cfg = ReconConfig()
    assert normalize_invoice("INV-00125", cfg) == "INV125"
    assert normalize_invoice("INV/00125", cfg) == "INV125"
    assert normalize_invoice("inv 00125", cfg) == "INV125"
    assert normalize_invoice("INV\\00125", cfg) == "INV125"
    assert normalize_amount("₹1,25,000") == 125000.0
    assert normalize_amount("(2,500.50)") == -2500.50
    assert normalize_amount("$ 1,000.00") == 1000.0
    assert normalize_date("01-Aug-2026") == "2026-08-01"
    assert normalize_date("15/08/2026") == "2026-08-15"
    assert normalize_date("2026-08-15") == "2026-08-15"
    print("OK  normalisation")


def test_rule1_exact():
    sap = [SapTxn(vendor_code="V1", vendor_name="Acme", sap_doc_number="D1",
                  document_type="Invoice", document_date="2026-08-01",
                  invoice_number="INV-001", amount=100000, currency="INR")]
    ven = [VendorTxn(vendor_code="V1", invoice_number="inv001", document_date="01-Aug-2026",
                     amount=100000, currency="INR", description="Invoice")]
    rep = reconcile(sap, ven)
    r = rep.results[0]
    assert r.match_status == Status.AUTO_MATCHED, r.match_status
    assert r.confidence_score == 100.0
    assert "RULE 1" in r.match_rule
    print("OK  Rule 1 exact match")


def test_rule2_invoice_amount_date_differs():
    sap = [SapTxn(vendor_code="V1", sap_doc_number="D1", document_type="Invoice",
                  document_date="2026-08-06", invoice_number="INV-002",
                  amount=50000, currency="INR")]
    ven = [VendorTxn(vendor_code="V1", invoice_number="INV002", document_date="05-Aug-2026",
                     amount=50000, currency="INR", description="Invoice")]
    rep = reconcile(sap, ven)
    r = rep.results[0]
    assert r.match_status == Status.AUTO_MATCHED, r.match_status   # auto_approve_rule_2 default True
    assert r.confidence_score == 95.0
    assert "RULE 2" in r.match_rule
    print("OK  Rule 2 invoice+amount (date within tolerance)")


def test_rule3_amount_within_tolerance_is_possible():
    cfg = ReconConfig(amount_tolerance_abs=5.0)
    sap = [SapTxn(vendor_code="V1", sap_doc_number="D1", document_type="Invoice",
                  document_date="2026-08-01", invoice_number="INV-003",
                  amount=1000.00, currency="INR")]
    ven = [VendorTxn(vendor_code="V1", invoice_number="INV003", document_date="02-Aug-2026",
                     amount=1003.00, currency="INR", description="Invoice")]
    rep = reconcile(sap, ven, cfg)
    r = rep.results[0]
    assert r.match_status == Status.POSSIBLE_MATCH, r.match_status
    assert r.confidence_score == 90.0
    assert r.exception_type == "AMOUNT WITHIN TOLERANCE"
    print("OK  Rule 3 possible match (amount within tolerance)")


def test_rule5_payment_reference():
    sap = [SapTxn(vendor_code="V1", sap_doc_number="P1", document_type="Payment",
                  document_date="2026-08-10", payment_reference="UTR12345",
                  amount=500000, currency="INR", text="Payment")]
    ven = [VendorTxn(vendor_code="V1", document_date="10-Aug-2026", payment_reference="UTR12345",
                     credit=500000, amount=500000, currency="INR", description="Payment received")]
    rep = reconcile(sap, ven)
    r = rep.results[0]
    assert r.match_status == Status.PAYMENT_MATCHED, r.match_status
    assert r.confidence_score == 100.0
    print("OK  Rule 5 payment reference match")


def test_rule7_one_invoice_many_payments():
    sap = [SapTxn(vendor_code="V1", sap_doc_number="INVBIG", document_type="Invoice",
                  document_date="2026-08-01", invoice_number="BIG-1",
                  amount=1000000, currency="INR")]
    ven = [
        VendorTxn(vendor_code="V1", document_date="05-Aug-2026", payment_reference="P1",
                  credit=400000, amount=400000, currency="INR", description="Payment"),
        VendorTxn(vendor_code="V1", document_date="10-Aug-2026", payment_reference="P2",
                  credit=300000, amount=300000, currency="INR", description="Payment"),
        VendorTxn(vendor_code="V1", document_date="15-Aug-2026", payment_reference="P3",
                  credit=300000, amount=300000, currency="INR", description="Payment"),
    ]
    rep = reconcile(sap, ven)
    matched = [r for r in rep.results if r.match_status == Status.FULLY_MATCHED]
    assert matched, _statuses(rep)
    assert len(matched[0].vendor_ids) == 3, matched[0].vendor_ids
    print("OK  Rule 7 one invoice / many payments")


def test_rule8_one_payment_many_invoices():
    sap = [
        SapTxn(vendor_code="V1", sap_doc_number="I1", document_type="Invoice",
               document_date="2026-08-01", invoice_number="A1", amount=200000, currency="INR"),
        SapTxn(vendor_code="V1", sap_doc_number="I2", document_type="Invoice",
               document_date="2026-08-02", invoice_number="A2", amount=150000, currency="INR"),
        SapTxn(vendor_code="V1", sap_doc_number="I3", document_type="Invoice",
               document_date="2026-08-03", invoice_number="A3", amount=150000, currency="INR"),
    ]
    ven = [VendorTxn(vendor_code="V1", document_date="10-Aug-2026", payment_reference="PAY",
                     credit=500000, amount=500000, currency="INR", description="Payment")]
    rep = reconcile(sap, ven)
    matched = [r for r in rep.results if r.match_status == Status.FULLY_MATCHED]
    assert matched, _statuses(rep)
    assert len(matched[0].sap_ids) == 3, matched[0].sap_ids
    print("OK  Rule 8 one payment / many invoices")


def test_rule6_partial_payment():
    sap = [SapTxn(vendor_code="V1", sap_doc_number="I1", document_type="Invoice",
                  document_date="2026-08-01", invoice_number="INV-6", amount=1000000, currency="INR")]
    ven = [VendorTxn(vendor_code="V1", invoice_number="INV-6", document_date="05-Aug-2026",
                     credit=750000, amount=750000, currency="INR", description="Part payment")]
    rep = reconcile(sap, ven)
    r = [x for x in rep.results if x.match_status == Status.PARTIAL_PAYMENT]
    assert r, _statuses(rep)
    assert r[0].difference == 250000.0, r[0].difference   # outstanding
    print("OK  Rule 6 partial payment (outstanding computed)")


def test_rule9_credit_note_missing_in_sap():
    sap = []
    ven = [VendorTxn(vendor_code="V1", document_date="05-Aug-2026", amount=25000,
                     credit=25000, currency="INR", description="Credit Note CN-1")]
    rep = reconcile(sap, ven)
    assert rep.results[0].match_status == Status.CREDIT_NOTE_MISSING_SAP
    print("OK  Rule 9 credit note missing in SAP")


def test_rule10_debit_note_missing_in_vendor():
    sap = [SapTxn(vendor_code="V1", sap_doc_number="DN1", document_type="Debit Note",
                  document_date="2026-08-01", amount=30000, currency="INR", text="Debit Note")]
    ven = []
    rep = reconcile(sap, ven)
    assert rep.results[0].match_status == Status.DEBIT_NOTE_MISSING_VENDOR
    print("OK  Rule 10 debit note missing in vendor statement")


def test_rule14_duplicate_detection():
    sap = [
        SapTxn(vendor_code="V1", sap_doc_number="D1", document_type="Invoice",
               document_date="2026-08-01", invoice_number="DUP-1", amount=1000, currency="INR"),
        SapTxn(vendor_code="V1", sap_doc_number="D2", document_type="Invoice",
               document_date="2026-08-01", invoice_number="DUP-1", amount=1000, currency="INR"),
    ]
    ven = []
    rep = reconcile(sap, ven)
    dups = [r for r in rep.results if r.match_status == Status.POSSIBLE_DUPLICATE]
    assert len(dups) == 2, _statuses(rep)
    print("OK  Rule 14 duplicate detection")


def test_rule15_16_missing_both_sides():
    sap = [SapTxn(vendor_code="V1", sap_doc_number="S1", document_type="Invoice",
                  document_date="2026-08-10", invoice_number="INV004", amount=25000, currency="INR")]
    ven = [VendorTxn(vendor_code="V1", invoice_number="INV003", document_date="10-Aug-2026",
                     amount=75000, currency="INR", description="Invoice")]
    rep = reconcile(sap, ven)
    st = _statuses(rep)
    assert Status.MISSING_IN_SAP in st           # INV003 (vendor only)
    assert Status.MISSING_IN_VENDOR in st        # INV004 (sap only)
    print("OK  Rules 15/16 missing in SAP / missing in vendor")


def test_no_double_match():
    """A SAP invoice consumed by Rule 1 must not be reused later."""
    sap = [SapTxn(vendor_code="V1", sap_doc_number="D1", document_type="Invoice",
                  document_date="2026-08-01", invoice_number="INV-1", amount=100, currency="INR")]
    ven = [
        VendorTxn(vendor_code="V1", invoice_number="INV-1", document_date="01-Aug-2026",
                  amount=100, currency="INR", description="Invoice"),
        VendorTxn(vendor_code="V1", invoice_number="INV-1", document_date="01-Aug-2026",
                  amount=100, currency="INR", description="Invoice"),
    ]
    rep = reconcile(sap, ven)
    matched = [r for r in rep.results if r.match_status == Status.AUTO_MATCHED]
    missing = [r for r in rep.results if r.match_status == Status.MISSING_IN_SAP]
    assert len(matched) == 1, _statuses(rep)     # only one vendor row can consume the SAP row
    assert len(missing) == 1, _statuses(rep)     # the second vendor row is unmatched
    print("OK  no double match")


def test_cross_vendor_never_matches():
    sap = [SapTxn(vendor_code="V1", sap_doc_number="D1", document_type="Invoice",
                  document_date="2026-08-01", invoice_number="INV-1", amount=100, currency="INR")]
    ven = [VendorTxn(vendor_code="V2", invoice_number="INV-1", document_date="01-Aug-2026",
                     amount=100, currency="INR", description="Invoice")]
    # identified vendor is V1 (from SAP); V2 vendor row is excluded from matching
    rep = reconcile(sap, ven)
    assert Status.AUTO_MATCHED not in _statuses(rep)
    print("OK  cross-vendor never matches")


def test_spec_example_section_12():
    sap = [
        SapTxn(vendor_code="V1", vendor_name="Acme", sap_doc_number="S-INV001",
               document_type="Invoice", document_date="2026-08-01",
               invoice_number="INV001", amount=100000, currency="INR"),
        SapTxn(vendor_code="V1", sap_doc_number="S-INV002", document_type="Invoice",
               document_date="2026-08-06", invoice_number="INV002", amount=50000, currency="INR"),
        SapTxn(vendor_code="V1", sap_doc_number="S-INV004", document_type="Invoice",
               document_date="2026-08-10", invoice_number="INV004", amount=25000, currency="INR"),
    ]
    ven = [
        VendorTxn(vendor_code="V1", invoice_number="INV001", document_date="01-Aug-2026",
                  amount=100000, currency="INR", description="Invoice"),
        VendorTxn(vendor_code="V1", invoice_number="INV002", document_date="05-Aug-2026",
                  amount=50000, currency="INR", description="Invoice"),
        VendorTxn(vendor_code="V1", invoice_number="INV003", document_date="10-Aug-2026",
                  amount=75000, currency="INR", description="Invoice"),
    ]
    rep = reconcile(sap, ven)
    inv001 = _by_ref(rep, "INV001")
    inv002 = _by_ref(rep, "INV002")
    inv003 = _by_ref(rep, "INV003")
    inv004 = _by_ref(rep, "INV004")
    assert inv001.match_status == Status.AUTO_MATCHED, inv001.match_status
    assert inv002.match_status == Status.AUTO_MATCHED, inv002.match_status   # date within tol
    assert inv003.match_status == Status.MISSING_IN_SAP, inv003.match_status
    assert inv004.match_status == Status.MISSING_IN_VENDOR, inv004.match_status
    print("OK  spec example (section 12)")


def test_final_status_fully_reconciled():
    sap = [SapTxn(vendor_code="V1", sap_doc_number="D1", document_type="Invoice",
                  document_date="2026-08-01", invoice_number="INV-1", amount=100, currency="INR")]
    ven = [VendorTxn(vendor_code="V1", invoice_number="INV-1", document_date="01-Aug-2026",
                     debit=100, amount=100, currency="INR", description="Invoice")]
    rep = reconcile(sap, ven)
    assert rep.final_status == "FULLY RECONCILED", (rep.final_status, rep.balance)
    print("OK  final status FULLY RECONCILED")


def test_source_not_mutated():
    s = SapTxn(vendor_code="V1", sap_doc_number="D1", document_type="Invoice",
               document_date="2026-08-01", invoice_number="INV-00125", amount=100, currency="INR")
    original_inv = s.invoice_number
    reconcile([s], [])
    assert s.invoice_number == original_inv     # original untouched (only .id assigned)
    print("OK  source data not mutated")


def test_config_is_used():
    """Tightening date tolerance to 0 should downgrade a date-differing match."""
    sap = [SapTxn(vendor_code="V1", sap_doc_number="D1", document_type="Invoice",
                  document_date="2026-08-06", invoice_number="INV-9", amount=500, currency="INR")]
    ven = [VendorTxn(vendor_code="V1", invoice_number="INV-9", document_date="01-Aug-2026",
                     amount=500, currency="INR", description="Invoice")]
    # Rule 2 matches on invoice+amount regardless of date, so disable rules 1&2
    cfg = ReconConfig(enable_rule_1_exact=False, enable_rule_2_invoice_amount=False,
                      date_tolerance_days=0)
    rep = reconcile(sap, ven, cfg)
    # with date tol 0 and 5-day gap, Rule 3 can't match -> both sides missing
    st = _statuses(rep)
    assert Status.MISSING_IN_SAP in st and Status.MISSING_IN_VENDOR in st, st
    print("OK  config values are honoured")


ALL = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    passed = 0
    for t in ALL:
        t()
        passed += 1
    print(f"\nALL {passed} RECON ENGINE TESTS PASSED")


if __name__ == "__main__":
    main()
