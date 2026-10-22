"""Parse an uploaded vendor statement (Excel .xlsx/.xls or CSV) into rows of
{invoice_no, amount, invoice_date, currency}.

Vendors format their statements differently, so we detect columns by matching
header names against a set of common aliases (case-insensitive, ignoring spaces
and punctuation).
"""
from __future__ import annotations

import io
import re

import pandas as pd

INVOICE_ALIASES = {
    "invoiceno", "invoicenumber", "invoice", "invno", "billno", "billnumber",
    "documentno", "docno", "reference", "referenceno", "refno", "invoiceref",
}
AMOUNT_ALIASES = {
    "amount", "amt", "invoiceamount", "invamount", "value", "amountdoc",
    "debit", "grossamount", "netamount", "totalamount", "balance",
}
DATE_ALIASES = {
    "invoicedate", "date", "docdate", "documentdate", "billdate", "postingdate",
}
CURRENCY_ALIASES = {"currency", "curr", "ccy"}


def _norm(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def _pick_column(columns: list[str], aliases: set[str]) -> str | None:
    norm_map = {_norm(c): c for c in columns}
    # exact alias hit first
    for alias in aliases:
        if alias in norm_map:
            return norm_map[alias]
    # then substring / contains match
    for norm, original in norm_map.items():
        if any(alias in norm for alias in aliases):
            return original
    return None


def _normalize_date(v) -> str:
    """Normalize a statement date to dd-mm-yyyy.

    The template asks vendors for dd-mm-yyyy, but we also accept a few common
    variants (slashes, ISO yyyy-mm-dd, Excel-parsed timestamps) and re-emit
    them as dd-mm-yyyy. Unparseable values are returned trimmed as-is.
    """
    s = str(v or "").strip()
    if not s:
        return ""
    # strip any time component Excel/pandas may have added
    s = s.split(" ")[0].split("T")[0]
    parts = re.split(r"[-/.]", s)
    if len(parts) == 3:
        a, b, c = (p.strip() for p in parts)
        try:
            if len(a) == 4:            # yyyy-mm-dd  -> dd-mm-yyyy
                y, m, d = int(a), int(b), int(c)
            else:                       # dd-mm-yyyy (template) or dd/mm/yy
                d, m, y = int(a), int(b), int(c)
                if y < 100:            # 2-digit year -> 2000s
                    y += 2000
            if 1 <= m <= 12 and 1 <= d <= 31:
                return f"{d:02d}-{m:02d}-{y:04d}"
        except ValueError:
            pass
    return s


def _to_float(v) -> float:
    if v is None:
        return 0.0
    s = str(v).strip()
    if not s:
        return 0.0
    # remove thousands separators and currency symbols; handle (123) as negative
    neg = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[^0-9.\-]", "", s.replace(",", ""))
    try:
        val = float(s) if s not in ("", "-", ".") else 0.0
    except ValueError:
        val = 0.0
    return -val if neg else val


def parse_statement(filename: str, content: bytes) -> dict:
    """Parse the uploaded file. Returns {rows, columns_detected, row_count, warnings}."""
    warnings: list[str] = []
    name = (filename or "").lower()

    try:
        if name.endswith(".csv") or name.endswith(".txt"):
            df = pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False)
        else:
            df = pd.read_excel(io.BytesIO(content), dtype=str)
            df = df.fillna("")
    except Exception as e:
        raise ValueError(f"Could not read the statement file: {e}")

    if df.empty:
        return {"rows": [], "columns_detected": {}, "row_count": 0,
                "warnings": ["The statement file has no data rows."]}

    columns = [str(c) for c in df.columns]
    inv_col = _pick_column(columns, INVOICE_ALIASES)
    amt_col = _pick_column(columns, AMOUNT_ALIASES)
    date_col = _pick_column(columns, DATE_ALIASES)
    ccy_col = _pick_column(columns, CURRENCY_ALIASES)

    if not inv_col:
        raise ValueError(
            "Could not find an invoice-number column in the statement. "
            f"Columns found: {', '.join(columns)}"
        )
    if not amt_col:
        raise ValueError(
            "Could not find an amount column in the statement. "
            f"Columns found: {', '.join(columns)}"
        )

    rows = []
    for _, r in df.iterrows():
        inv = str(r.get(inv_col, "")).strip()
        if not inv:
            continue
        rows.append({
            "invoice_no": inv,
            "amount": _to_float(r.get(amt_col, 0)),
            "invoice_date": _normalize_date(r.get(date_col, "")) if date_col else "",
            "currency": str(r.get(ccy_col, "")).strip() if ccy_col else "",
        })

    if not rows:
        warnings.append("No usable rows were found after parsing (empty invoice numbers).")

    return {
        "rows": rows,
        "columns_detected": {
            "invoice_no": inv_col, "amount": amt_col,
            "invoice_date": date_col, "currency": ccy_col,
        },
        "row_count": len(rows),
        "warnings": warnings,
    }
