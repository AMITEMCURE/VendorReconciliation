"""Parse a vendor's FULL transaction ledger (Excel/CSV) for transaction-level
reconciliation.

Expected columns (aliases tolerated): Date, Transaction Type, Reference No,
Debit, Credit, Currency, Narration.

We classify each row by transaction type, compute the balance bridge
(Opening + Debits - Credits should equal Closing), and group totals per type.

Sign convention: Debit increases the payable balance (invoices, debit notes),
Credit decreases it (payments, credit notes).
"""
from __future__ import annotations

import io
import re

import pandas as pd

DATE_ALIASES = {"date", "docdate", "documentdate", "postingdate", "trndate", "transactiondate"}
TYPE_ALIASES = {"transactiontype", "type", "trntype", "documenttype", "particulars", "nature"}
REF_ALIASES = {"referenceno", "reference", "refno", "documentno", "docno", "invoiceno",
               "voucherno", "billno"}
DEBIT_ALIASES = {"debit", "dr", "debitamount", "dramount"}
CREDIT_ALIASES = {"credit", "cr", "creditamount", "cramount"}
AMOUNT_ALIASES = {"amount", "amt", "value"}
CURRENCY_ALIASES = {"currency", "curr", "ccy"}
NARRATION_ALIASES = {"narration", "remarks", "remark", "description", "details", "note", "notes"}

# Canonical transaction types + the keywords that map to them.
TYPE_KEYWORDS = {
    "opening_balance": ["opening", "openingbalance", "openingbal", "bf", "broughtforward"],
    "closing_balance": ["closing", "closingbalance", "closingbal", "cf", "carriedforward", "balance c"],
    "invoice": ["invoice", "inv", "sales", "bill", "supply"],
    "payment": ["payment", "paid", "receipt", "rtgs", "neft", "cheque", "bank"],
    "credit_note": ["creditnote", "cn", "credit note", "creditmemo"],
    "debit_note": ["debitnote", "dn", "debit note", "debitmemo"],
}


def _norm(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def _pick(columns, aliases):
    norm_map = {_norm(c): c for c in columns}
    for alias in aliases:
        if alias in norm_map:
            return norm_map[alias]
    for norm, original in norm_map.items():
        if any(alias in norm for alias in aliases):
            return original
    return None


def _to_float(v) -> float:
    if v is None:
        return 0.0
    s = str(v).strip()
    if not s:
        return 0.0
    neg = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[^0-9.\-]", "", s.replace(",", ""))
    try:
        val = float(s) if s not in ("", "-", ".") else 0.0
    except ValueError:
        val = 0.0
    return -val if neg else val


def _classify(type_text: str, ref: str) -> str:
    t = _norm(type_text)
    if not t:
        return "other"
    for canon, keys in TYPE_KEYWORDS.items():
        for k in keys:
            if _norm(k) in t:
                return canon
    return "other"


def _normalize_date(v) -> str:
    s = str(v or "").strip().split(" ")[0].split("T")[0]
    if not s:
        return ""
    parts = re.split(r"[-/.]", s)
    if len(parts) == 3:
        a, b, c = (p.strip() for p in parts)
        try:
            if len(a) == 4:
                y, m, d = int(a), int(b), int(c)
            else:
                d, m, y = int(a), int(b), int(c)
                if y < 100:
                    y += 2000
            if 1 <= m <= 12 and 1 <= d <= 31:
                return f"{d:02d}-{m:02d}-{y:04d}"
        except ValueError:
            pass
    return s


def parse_ledger(filename: str, content: bytes) -> dict:
    """Parse a vendor ledger file into structured transactions + a balance bridge."""
    warnings: list[str] = []
    name = (filename or "").lower()
    try:
        if name.endswith(".csv") or name.endswith(".txt"):
            df = pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False)
        else:
            df = pd.read_excel(io.BytesIO(content), dtype=str).fillna("")
    except Exception as e:
        raise ValueError(f"Could not read the ledger file: {e}")

    if df.empty:
        return {"transactions": [], "columns_detected": {}, "row_count": 0,
                "bridge": {}, "by_type": {}, "warnings": ["The ledger file has no data rows."]}

    columns = [str(c) for c in df.columns]
    date_col = _pick(columns, DATE_ALIASES)
    type_col = _pick(columns, TYPE_ALIASES)
    ref_col = _pick(columns, REF_ALIASES)
    debit_col = _pick(columns, DEBIT_ALIASES)
    credit_col = _pick(columns, CREDIT_ALIASES)
    amount_col = _pick(columns, AMOUNT_ALIASES)
    ccy_col = _pick(columns, CURRENCY_ALIASES)
    narr_col = _pick(columns, NARRATION_ALIASES)

    if not type_col:
        raise ValueError(
            "Could not find a 'Transaction Type' column. Columns found: " + ", ".join(columns))
    if not debit_col and not credit_col and not amount_col:
        raise ValueError(
            "Could not find Debit/Credit (or Amount) columns. Columns found: " + ", ".join(columns))

    transactions = []
    for _, r in df.iterrows():
        ttype_raw = str(r.get(type_col, "")).strip()
        ref = str(r.get(ref_col, "")).strip() if ref_col else ""
        if not ttype_raw and not ref:
            continue
        debit = _to_float(r.get(debit_col, 0)) if debit_col else 0.0
        credit = _to_float(r.get(credit_col, 0)) if credit_col else 0.0
        if not debit_col and not credit_col and amount_col:
            # single Amount column: positive = debit, negative = credit
            amt = _to_float(r.get(amount_col, 0))
            if amt >= 0:
                debit = amt
            else:
                credit = -amt
        canon = _classify(ttype_raw, ref)
        transactions.append({
            "date": _normalize_date(r.get(date_col, "")) if date_col else "",
            "type": canon,
            "type_label": ttype_raw,
            "reference_no": ref,
            "debit": debit,
            "credit": credit,
            "currency": str(r.get(ccy_col, "")).strip() if ccy_col else "",
            "narration": str(r.get(narr_col, "")).strip() if narr_col else "",
        })

    if not transactions:
        warnings.append("No usable transaction rows were found.")

    # --- balance bridge -----------------------------------------------------
    def _bal(txn):
        # opening/closing balances are stated as a single figure in debit or credit
        return (txn["debit"] or 0) - (txn["credit"] or 0)

    opening = sum(_bal(t) for t in transactions if t["type"] == "opening_balance")
    closing_stated = sum(_bal(t) for t in transactions if t["type"] == "closing_balance")

    movement = [t for t in transactions if t["type"] not in ("opening_balance", "closing_balance")]
    total_debits = round(sum(t["debit"] for t in movement), 2)
    total_credits = round(sum(t["credit"] for t in movement), 2)
    computed_closing = round(opening + total_debits - total_credits, 2)
    difference = round(computed_closing - closing_stated, 2)

    bridge = {
        "opening_balance": round(opening, 2),
        "total_debits": total_debits,
        "total_credits": total_credits,
        "computed_closing": computed_closing,
        "stated_closing": round(closing_stated, 2),
        "difference": difference,
        "balanced": abs(difference) <= 0.01,
        "has_opening": any(t["type"] == "opening_balance" for t in transactions),
        "has_closing": any(t["type"] == "closing_balance" for t in transactions),
    }
    if not bridge["has_closing"]:
        warnings.append("No 'Closing Balance' row found — cannot verify the ledger closes correctly.")
    if not bridge["has_opening"]:
        warnings.append("No 'Opening Balance' row found — assuming opening balance of 0.")

    # --- per-type grouping --------------------------------------------------
    by_type: dict[str, dict] = {}
    for t in movement:
        g = by_type.setdefault(t["type"], {"count": 0, "debit": 0.0, "credit": 0.0})
        g["count"] += 1
        g["debit"] = round(g["debit"] + t["debit"], 2)
        g["credit"] = round(g["credit"] + t["credit"], 2)

    return {
        "transactions": transactions,
        "columns_detected": {
            "date": date_col, "type": type_col, "reference_no": ref_col,
            "debit": debit_col, "credit": credit_col, "amount": amount_col,
            "currency": ccy_col, "narration": narr_col,
        },
        "row_count": len(transactions),
        "bridge": bridge,
        "by_type": by_type,
        "warnings": warnings,
    }
