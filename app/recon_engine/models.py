"""Data models for the reconciliation engine.

Source records (SapTxn / VendorTxn) are treated as immutable inputs — the engine
copies their raw values into `original_*` fields and writes derived values into
`normalised_*` fields, never mutating the caller's data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---- match status / exception vocab (stable strings, used in output) -------
class Status:
    AUTO_MATCHED = "AUTO MATCHED"
    PAYMENT_MATCHED = "PAYMENT MATCHED"
    FULLY_MATCHED = "FULLY MATCHED"
    PARTIAL_PAYMENT = "PARTIAL PAYMENT"
    POSSIBLE_MATCH = "POSSIBLE MATCH"
    POSSIBLE_DUPLICATE = "POSSIBLE DUPLICATE"
    MISSING_IN_SAP = "MISSING IN SAP"
    MISSING_IN_VENDOR = "MISSING IN VENDOR STATEMENT"
    PAYMENT_NOT_REFLECTED = "PAYMENT NOT REFLECTED BY VENDOR"
    UNALLOCATED_PAYMENT = "UNALLOCATED PAYMENT"
    CREDIT_NOTE_MISSING_SAP = "CREDIT NOTE MISSING IN SAP"
    DEBIT_NOTE_MISSING_VENDOR = "DEBIT NOTE MISSING IN VENDOR STATEMENT"
    RECONCILED_TDS = "RECONCILED - TDS"
    BANK_CHARGES = "BANK CHARGES"
    ROUNDING_DIFFERENCE = "ROUNDING DIFFERENCE"
    EXCEPTION = "EXCEPTION"
    UNMATCHED = "UNMATCHED"


class DocKind:
    """Coarse classification of a source row, derived from its document type."""
    INVOICE = "invoice"
    PAYMENT = "payment"
    CREDIT_NOTE = "credit_note"
    DEBIT_NOTE = "debit_note"
    OTHER = "other"


@dataclass
class SapTxn:
    """One SAP ledger line (Dataset A). Amounts as provided; engine normalises."""
    company_code: str = ""
    vendor_code: str = ""
    vendor_name: str = ""
    sap_doc_number: str = ""
    document_type: str = ""
    posting_date: str = ""
    document_date: str = ""
    invoice_number: str = ""      # invoice / reference number
    amount: float = 0.0
    currency: str = ""
    debit_credit: str = ""        # 'D'/'H'/'S' etc. — original indicator
    tds_amount: float = 0.0
    gst_amount: float = 0.0
    clearing_document: str = ""
    clearing_date: str = ""
    payment_reference: str = ""
    text: str = ""
    # engine-assigned id (stable within a run)
    id: str = ""


@dataclass
class VendorTxn:
    """One vendor-statement line (Dataset B)."""
    vendor_name: str = ""
    invoice_number: str = ""
    document_number: str = ""
    document_date: str = ""
    debit: float = 0.0
    credit: float = 0.0
    amount: float = 0.0
    currency: str = ""
    payment_reference: str = ""
    description: str = ""
    balance: float = 0.0
    vendor_code: str = ""         # optional; used if present
    id: str = ""


@dataclass
class MatchResult:
    """One row of the reconciliation output (spec section 11)."""
    transaction_id: str = ""
    vendor_code: str = ""
    vendor_name: str = ""
    sap_doc_number: str = ""
    vendor_statement_reference: str = ""
    sap_amount: Optional[float] = None
    vendor_amount: Optional[float] = None
    difference: Optional[float] = None
    currency: str = ""
    sap_date: str = ""
    vendor_date: str = ""
    match_status: str = ""
    match_rule: str = ""
    confidence_score: float = 0.0
    exception_type: str = ""
    comments: str = ""
    # linkage (for one-to-many / many-to-one auditing)
    sap_ids: list = field(default_factory=list)
    vendor_ids: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "transaction_id": self.transaction_id,
            "vendor_code": self.vendor_code,
            "vendor_name": self.vendor_name,
            "sap_doc_number": self.sap_doc_number,
            "vendor_statement_reference": self.vendor_statement_reference,
            "sap_amount": self.sap_amount,
            "vendor_amount": self.vendor_amount,
            "difference": self.difference,
            "currency": self.currency,
            "sap_date": self.sap_date,
            "vendor_date": self.vendor_date,
            "match_status": self.match_status,
            "match_rule": self.match_rule,
            "confidence_score": self.confidence_score,
            "exception_type": self.exception_type,
            "comments": self.comments,
            "sap_ids": self.sap_ids,
            "vendor_ids": self.vendor_ids,
        }


@dataclass
class ReconciliationReport:
    vendor_code: str = ""
    vendor_name: str = ""
    results: list = field(default_factory=list)          # list[MatchResult]
    balance: dict = field(default_factory=dict)          # balance reconciliation
    final_status: str = ""
    counts: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "vendor_code": self.vendor_code,
            "vendor_name": self.vendor_name,
            "final_status": self.final_status,
            "counts": self.counts,
            "balance": self.balance,
            "warnings": self.warnings,
            "results": [r.as_dict() for r in self.results],
        }
