"""Vendor reconciliation engine.

Compares SAP open items against a vendor's statement of account and classifies
each row into one of:

  matched          -> same invoice on both sides, amounts agree (within tolerance)
  amount_mismatch  -> same invoice on both sides, amounts differ
  only_in_sap      -> invoice booked in SAP but absent from the vendor statement
  only_in_vendor   -> invoice on the vendor statement but not booked in SAP

Matching key: the invoice number, normalized (uppercased, stripped of spaces
and leading zeros / common punctuation) so that "INV-0012" and "inv12" match.
Amount comparison uses a small absolute tolerance to absorb rounding.
"""
from __future__ import annotations

import re

AMOUNT_TOLERANCE = 0.01


def normalize_invoice(value) -> str:
    """Normalize an invoice number for matching across SAP and vendor formats.

    Uppercases, drops all non-alphanumerics, and removes zero-padding so that
    "INV-0001", "inv 0001" and "INV1" all collapse to "INV1", and "0012" -> "12".
    Zero-padding is stripped both at the very start (numeric codes) and right
    after a leading alphabetic prefix (e.g. INV0001).
    """
    s = str(value or "").upper()
    s = re.sub(r"[^A-Z0-9]", "", s)
    # strip zeros that pad a numeric run following an optional alpha prefix
    m = re.match(r"^([A-Z]*)(0*)(\d.*)?$", s)
    if m:
        prefix = m.group(1)
        rest = m.group(3) or ""
        s = prefix + rest
    return s


def _amounts_agree(a: float, b: float) -> bool:
    return abs(round(a, 2) - round(b, 2)) <= AMOUNT_TOLERANCE


def reconcile(sap_invoices: list[dict], vendor_rows: list[dict]) -> dict:
    """Reconcile SAP invoices against vendor statement rows.

    Each SAP invoice is expected to have: invoice_no, amount, invoice_date,
    document_number, currency, due_date.
    Each vendor row is expected to have: invoice_no, amount, invoice_date (optional).
    Returns a structured result with classified rows and totals.
    """
    # Index vendor rows by normalized invoice number. A vendor may list the same
    # invoice more than once; keep them in a list and consume as matched.
    vendor_index: dict[str, list[dict]] = {}
    for row in vendor_rows:
        key = normalize_invoice(row.get("invoice_no"))
        if not key:
            continue
        vendor_index.setdefault(key, []).append(row)

    matched, amount_mismatch, only_in_sap = [], [], []
    consumed_keys: set[str] = set()

    for sap in sap_invoices:
        key = normalize_invoice(sap.get("invoice_no"))
        candidates = vendor_index.get(key) if key else None
        if candidates:
            vrow = candidates.pop(0)
            consumed_keys.add(key)
            sap_amt = float(sap.get("amount") or 0)
            ven_amt = float(vrow.get("amount") or 0)
            pair = {
                "invoice_no": sap.get("invoice_no"),
                "sap_amount": sap_amt,
                "vendor_amount": ven_amt,
                "difference": round(sap_amt - ven_amt, 2),
                "currency": sap.get("currency", ""),
                "sap_invoice_date": sap.get("invoice_date", ""),
                "vendor_invoice_date": vrow.get("invoice_date", ""),
                "document_number": sap.get("document_number", ""),
                "due_date": sap.get("due_date", ""),
            }
            if _amounts_agree(sap_amt, ven_amt):
                matched.append(pair)
            else:
                amount_mismatch.append(pair)
        else:
            only_in_sap.append({
                "invoice_no": sap.get("invoice_no"),
                "amount": float(sap.get("amount") or 0),
                "currency": sap.get("currency", ""),
                "invoice_date": sap.get("invoice_date", ""),
                "document_number": sap.get("document_number", ""),
                "due_date": sap.get("due_date", ""),
            })

    # Anything left unconsumed in the vendor index is only on the vendor side.
    only_in_vendor = []
    for key, rows in vendor_index.items():
        for vrow in rows:
            only_in_vendor.append({
                "invoice_no": vrow.get("invoice_no"),
                "amount": float(vrow.get("amount") or 0),
                "currency": vrow.get("currency", ""),
                "invoice_date": vrow.get("invoice_date", ""),
            })

    def _sum(rows, key):
        return round(sum(float(r.get(key) or 0) for r in rows), 2)

    summary = {
        "sap_total": _sum(sap_invoices, "amount"),
        "vendor_total": _sum(vendor_rows, "amount"),
        "matched_count": len(matched),
        "amount_mismatch_count": len(amount_mismatch),
        "only_in_sap_count": len(only_in_sap),
        "only_in_vendor_count": len(only_in_vendor),
        "matched_sap_amount": _sum(matched, "sap_amount"),
        "matched_vendor_amount": _sum(matched, "vendor_amount"),
        "only_in_sap_amount": _sum(only_in_sap, "amount"),
        "only_in_vendor_amount": _sum(only_in_vendor, "amount"),
        "net_difference": round(_sum(sap_invoices, "amount") - _sum(vendor_rows, "amount"), 2),
    }

    return {
        "summary": summary,
        "matched": matched,
        "amount_mismatch": amount_mismatch,
        "only_in_sap": only_in_sap,
        "only_in_vendor": only_in_vendor,
    }
