"""Parse uploaded SAP ledger and Vendor statement files (Excel/CSV) into the
reconciliation engine's SapTxn / VendorTxn dataclasses.

Column detection is by header alias (case/space/punctuation-insensitive), so
files don't need exact header names.
"""
from __future__ import annotations

import io
import re

import pandas as pd

from .recon_engine.models import SapTxn, VendorTxn
from .recon_engine.normalize import normalize_amount


def _read(filename: str, content: bytes) -> pd.DataFrame:
    name = (filename or "").lower()
    if name.endswith(".csv") or name.endswith(".txt"):
        return pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False)
    return pd.read_excel(io.BytesIO(content), dtype=str).fillna("")


def _norm(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def _pick(columns, aliases, taken=None):
    """Resolve a source column for a field from its alias set.

    Pass 1: exact normalized match (strongest).
    Pass 2: substring match, but only if the alias covers most of the header so a
    short alias like 'vendor' does NOT swallow 'vendorname'. Columns already
    assigned to another field (``taken``) are skipped.
    """
    taken = taken or set()
    norm_map = {_norm(c): c for c in columns}
    # Pass 1 — exact
    for a in aliases:
        if a in norm_map and norm_map[a] not in taken:
            return norm_map[a]
    # Pass 2 — substring, guarded against short-alias collisions
    for norm, original in norm_map.items():
        if original in taken:
            continue
        for a in aliases:
            if a in norm and len(a) >= max(4, len(norm) - 3):
                return original
    return None


def _g(row, col):
    return str(row.get(col, "")).strip() if col else ""


def _num(row, col):
    return normalize_amount(row.get(col, 0)) if col else 0.0


# ---- SAP (Dataset A) -------------------------------------------------------
SAP_MAP = {
    "company_code": {"companycode", "bukrs", "cocode", "company"},
    "vendor_code": {"vendorcode", "vendor", "lifnr", "vendorno", "vendornumber", "supplier"},
    "vendor_name": {"vendorname", "name", "name1"},
    "sap_doc_number": {"sapdocumentnumber", "documentnumber", "docno", "docnumber", "belnr", "sapdocno"},
    "document_type": {"documenttype", "doctype", "blart", "type"},
    "posting_date": {"postingdate", "pstngdate", "budat"},
    "document_date": {"documentdate", "docdate", "bldat", "date"},
    "invoice_number": ["assignment", "allocnmbr", "invoicenumber", "invoiceno",
                       "referencenumber", "reference", "refno", "xblnr"],
    "amount": {"amountindoccurr", "amount", "amtdoccur", "amountdoccurrency", "wrbtr", "value"},
    "currency": {"localcurrency", "currency", "waers", "curr", "ccy"},
    "debit_credit": {"debitcredit", "shkzg", "drcr", "dc"},
    "tds_amount": {"tdsamount", "tds", "withholdingtax", "wtax"},
    "gst_amount": {"gstamount", "gst", "tax", "taxamount"},
    "clearing_document": {"clearingdocument", "augbl", "clearingdoc"},
    "clearing_date": {"clearingdate", "augdt"},
    "payment_reference": {"paymentreference", "paymentref", "utr", "paymentdocument"},
    "text": {"text", "itemtext", "sgtxt", "narration", "description"},
}


def _pick_all(cols, field_map, priority):
    """Resolve every field to a column, assigning more-specific fields first
    (``priority`` order) so each column is claimed once. Prevents e.g. a
    'Vendor Name' column being grabbed by the vendor_code field."""
    picked = {}
    taken = set()
    ordered = priority + [f for f in field_map if f not in priority]
    for field in ordered:
        col = _pick(cols, field_map[field], taken)
        picked[field] = col
        if col:
            taken.add(col)
    return picked


def parse_sap(filename: str, content: bytes) -> list[SapTxn]:
    df = _read(filename, content)
    if df.empty:
        return []
    cols = [str(c) for c in df.columns]
    picked = _pick_all(cols, SAP_MAP, ["vendor_name", "document_type", "sap_doc_number", "invoice_number"])
    out: list[SapTxn] = []
    for _, r in df.iterrows():
        t = SapTxn(
            company_code=_g(r, picked["company_code"]),
            vendor_code=_g(r, picked["vendor_code"]),
            vendor_name=_g(r, picked["vendor_name"]),
            sap_doc_number=_g(r, picked["sap_doc_number"]),
            document_type=_g(r, picked["document_type"]),
            posting_date=_g(r, picked["posting_date"]),
            document_date=_g(r, picked["document_date"]),
            invoice_number=_g(r, picked["invoice_number"]),
            amount=_num(r, picked["amount"]),
            currency=_g(r, picked["currency"]),
            debit_credit=_g(r, picked["debit_credit"]),
            tds_amount=_num(r, picked["tds_amount"]),
            gst_amount=_num(r, picked["gst_amount"]),
            clearing_document=_g(r, picked["clearing_document"]),
            clearing_date=_g(r, picked["clearing_date"]),
            payment_reference=_g(r, picked["payment_reference"]),
            text=_g(r, picked["text"]),
        )
        # Keep a row if it carries ANY meaningful value — not only invoice/doc/
        # amount — so files that lead with Supplier, dates or document type still
        # populate. Only fully-blank rows are skipped.
        if any([t.invoice_number, t.sap_doc_number, t.amount, t.payment_reference,
                t.vendor_code, t.document_type, t.posting_date, t.document_date,
                t.clearing_document, t.text]):
            out.append(t)
    return out


# ---- Vendor statement (Dataset B) ------------------------------------------
VEN_MAP = {
    "vendor_name": {"vendorname", "name", "customername"},
    "vendor_code": {"vendorcode", "vendorno", "customer", "customercode"},
    "invoice_number": ["assignment", "invoicenumber", "invoiceno", "invoice", "billno",
                       "invref", "reference", "refno"],
    "document_number": {"documentno", "documentnumber", "docno", "voucherno", "billdoc"},
    "document_date": {"documentdate", "docdate", "invoicedate", "billdate", "date"},
    "debit": {"debit", "dr", "debitamount", "debitinr", "debitamt", "withdrawal"},
    "credit": {"credit", "cr", "creditamount", "creditinr", "creditamt", "deposit"},
    "amount": {"localcrcyamt", "localcurrencyamount", "localcrcyamount", "amtinlc",
               "amountinlocalcurrency", "amountindoccurr", "amountdoccur",
               "amount", "amt", "value", "transactionamount", "netamount", "grossamount"},
    "currency": {"lcurr", "localcrcy", "localcurrency", "currency", "curr", "ccy"},
    "payment_reference": {"paymentreference", "paymentref", "utr", "chequeno",
                          "clrngdoc", "clearingdoc", "clearing"},
    "description": {"transactiontype", "transtype", "txntype", "remark", "description",
                    "narration", "particulars", "remarks", "details", "type"},
    "balance": {"balance", "runningbalance", "closingbalance"},
}


def parse_vendor(filename: str, content: bytes) -> list[VendorTxn]:
    df = _read(filename, content)
    if df.empty:
        return []
    cols = [str(c) for c in df.columns]
    picked = _pick_all(cols, VEN_MAP, ["vendor_name", "invoice_number", "document_number", "description"])
    out: list[VendorTxn] = []
    for _, r in df.iterrows():
        t = VendorTxn(
            vendor_name=_g(r, picked["vendor_name"]),
            vendor_code=_g(r, picked["vendor_code"]),
            invoice_number=_g(r, picked["invoice_number"]),
            document_number=_g(r, picked["document_number"]),
            document_date=_g(r, picked["document_date"]),
            debit=_num(r, picked["debit"]),
            credit=_num(r, picked["credit"]),
            amount=_num(r, picked["amount"]),
            currency=_g(r, picked["currency"]),
            payment_reference=_g(r, picked["payment_reference"]),
            description=_g(r, picked["description"]),
            balance=_num(r, picked["balance"]),
        )
        if any([t.invoice_number, t.document_number, t.amount, t.debit, t.credit, t.payment_reference]):
            out.append(t)
    return out
