"""Normalisation helpers (spec steps 2-4) + document-kind classification.

These never modify source records; callers store the results in separate
`normalised_*` fields.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from .config import ReconConfig
from .models import DocKind


def normalize_invoice(value, cfg: ReconConfig) -> str:
    """Uppercase, strip spaces/hyphens/slashes/backslashes/special chars, and
    optionally strip leading zeros (configurable).  INV-00125 -> INV125."""
    s = str(value or "").upper()
    s = re.sub(r"[^A-Z0-9]", "", s)            # drop all non-alphanumerics
    if cfg.strip_leading_zeros:
        # strip zero-padding both at the very start and right after an alpha prefix
        m = re.match(r"^([A-Z]*)(0*)(\d.*)?$", s)
        if m:
            s = m.group(1) + (m.group(3) or "")
    return s


def normalize_amount(value) -> float:
    """Convert to a numeric value. Handles commas, currency symbols, negative
    signs and parentheses (accounting negatives).  '₹1,25,000' -> 125000.0,
    '(2,500.50)' -> -2500.50."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return 0.0
    neg = (s.startswith("(") and s.endswith(")")) or s.lstrip().startswith("-")
    s = re.sub(r"[^0-9.]", "", s.replace(",", ""))
    try:
        val = float(s) if s not in ("", ".") else 0.0
    except ValueError:
        val = 0.0
    return -val if neg else val


def normalize_date(value) -> str:
    """Return an ISO date string YYYY-MM-DD, or "" if unparseable.

    Accepts ISO, dd-mm-yyyy, dd/mm/yyyy, and common textual month formats.
    """
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    if not s:
        return ""
    s = s.split("T")[0].split(" ")[0]
    # numeric separators
    parts = re.split(r"[-/.]", s)
    if len(parts) == 3 and all(p.strip().isdigit() for p in parts):
        a, b, c = (int(p) for p in parts)
        try:
            if len(str(parts[0])) == 4:          # yyyy-mm-dd
                return date(a, b, c).strftime("%Y-%m-%d")
            y = c + 2000 if c < 100 else c        # dd-mm-yyyy
            return date(y, b, a).strftime("%Y-%m-%d")
        except ValueError:
            return s
    # textual, e.g. 01-Aug-2026 / 1 Aug 2026 / Aug 1 2026
    for fmt in ("%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y",
                "%b %d %Y", "%B %d %Y", "%d-%b-%y", "%d %b %y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s


def date_diff_days(d1: str, d2: str):
    """Absolute calendar-day difference between two ISO dates, or None."""
    try:
        a = datetime.strptime(d1, "%Y-%m-%d").date()
        b = datetime.strptime(d2, "%Y-%m-%d").date()
        return abs((a - b).days)
    except (ValueError, TypeError):
        return None


# ---- document-kind classification -----------------------------------------
_PAYMENT_KEYS = ("payment", "paid", "receipt", "rtgs", "neft", "cheque", "chq", "bank", "kz", "zp")
_CREDIT_NOTE_KEYS = ("credit note", "creditnote", "credit memo", "cn", "kg")
_DEBIT_NOTE_KEYS = ("debit note", "debitnote", "debit memo", "dn")
_INVOICE_KEYS = ("invoice", "inv", "kr", "re", "bill", "supply")


def _norm_text(v) -> str:
    return re.sub(r"[^a-z0-9 ]", "", str(v or "").lower())


# SAP document-type (BLART) codes are the AUTHORITATIVE signal for what a row
# is. Mirrors DOC_TYPE_LABELS on the frontend. A payment reference / clearing
# doc on an INVOICE row must NOT turn it into a payment.
_SAP_DOCTYPE_KIND = {
    # Invoices (payable-increasing)
    "KR": DocKind.INVOICE, "RE": DocKind.INVOICE, "MA": DocKind.INVOICE,
    "MI": DocKind.INVOICE, "KA": DocKind.INVOICE,
    # Credit memo / note (payable-reducing)
    "KG": DocKind.CREDIT_NOTE,
    # Payments
    "KZ": DocKind.PAYMENT, "PE": DocKind.PAYMENT, "ZP": DocKind.PAYMENT,
    "DZ": DocKind.PAYMENT,
    # SA (adjustment) and AB (clearing) are intentionally left to fall through
    # to amount/keyword logic — they can be either side.
}


def classify_sap(txn) -> str:
    # 1) Document-type CODE is authoritative when it's a known BLART code.
    code = (str(txn.document_type or "").strip().upper())
    if code in _SAP_DOCTYPE_KIND:
        return _SAP_DOCTYPE_KIND[code]
    # 2) Otherwise fall back to free-text keywords / payment-reference heuristics.
    hay = " ".join(_norm_text(x) for x in (txn.document_type, txn.text))
    return _classify_from_text(hay, txn.payment_reference)


def classify_vendor(txn) -> str:
    # Transaction Type lives in `description`; also look at the invoice number.
    hay = " ".join(_norm_text(x) for x in (txn.description, txn.invoice_number))
    kind = _classify_from_text(hay, txn.payment_reference)

    # The AMOUNT SIGN is the authoritative signal on a vendor statement:
    #   positive = increases payable  -> invoice / debit note
    #   negative = reduces payable    -> payment / credit note
    # A keyword/payment-ref guess that CONTRADICTS the sign is wrong (e.g. a
    # positive vendor invoice with a stray reference mis-tagged as a payment).
    amt = normalize_amount(txn.amount)
    if amt == 0 and (txn.debit or txn.credit):
        amt = normalize_amount(txn.debit) - normalize_amount(txn.credit)

    if amt > 0:
        # Payable-increasing. Only invoice/debit note are valid.
        if kind in (DocKind.INVOICE, DocKind.DEBIT_NOTE):
            return kind
        return DocKind.INVOICE
    if amt < 0:
        # Payable-reducing. Only payment/credit note are valid.
        if kind in (DocKind.PAYMENT, DocKind.CREDIT_NOTE):
            return kind
        return DocKind.PAYMENT

    # Amount is zero / unknown — fall back to keyword or debit/credit shape.
    if kind != DocKind.OTHER:
        return kind
    if (txn.credit or 0) > 0 and (txn.debit or 0) == 0:
        return DocKind.PAYMENT
    if (txn.debit or 0) > 0 and (txn.credit or 0) == 0:
        return DocKind.INVOICE
    return DocKind.OTHER


def _classify_from_text(hay: str, payment_ref) -> str:
    for k in _CREDIT_NOTE_KEYS:
        if k in hay:
            return DocKind.CREDIT_NOTE
    for k in _DEBIT_NOTE_KEYS:
        if k in hay:
            return DocKind.DEBIT_NOTE
    for k in _PAYMENT_KEYS:
        if k in hay:
            return DocKind.PAYMENT
    for k in _INVOICE_KEYS:
        if k in hay:
            return DocKind.INVOICE
    if payment_ref and str(payment_ref).strip():
        return DocKind.PAYMENT
    return DocKind.OTHER
