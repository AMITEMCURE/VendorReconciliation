"""Build an Excel workbook from a reconciliation result."""
from __future__ import annotations

import io

import pandas as pd


def build_excel(vendor: str, vendor_name: str, result: dict) -> bytes:
    summary = result.get("summary", {})

    summary_rows = [
        ("Vendor Code", vendor),
        ("Vendor Name", vendor_name),
        ("SAP Total", summary.get("sap_total")),
        ("Vendor Statement Total", summary.get("vendor_total")),
        ("Net Difference (SAP - Vendor)", summary.get("net_difference")),
        ("Matched", summary.get("matched_count")),
        ("Amount Mismatches", summary.get("amount_mismatch_count")),
        ("Invoice Missing in SAP", summary.get("only_in_sap_count")),
        ("Missing in Vendor", summary.get("only_in_vendor_count")),
    ]
    df_summary = pd.DataFrame(summary_rows, columns=["Metric", "Value"])

    df_matched = pd.DataFrame(result.get("matched", []))
    df_mismatch = pd.DataFrame(result.get("amount_mismatch", []))
    df_only_sap = pd.DataFrame(result.get("only_in_sap", []))
    df_only_vendor = pd.DataFrame(result.get("only_in_vendor", []))

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df_summary.to_excel(writer, sheet_name="Summary", index=False)
        (df_matched if not df_matched.empty else pd.DataFrame(columns=["invoice_no"])).to_excel(
            writer, sheet_name="Matched", index=False)
        (df_mismatch if not df_mismatch.empty else pd.DataFrame(columns=["invoice_no"])).to_excel(
            writer, sheet_name="Amount Mismatch", index=False)
        (df_only_sap if not df_only_sap.empty else pd.DataFrame(columns=["invoice_no"])).to_excel(
            writer, sheet_name="Invoice Missing in SAP", index=False)
        (df_only_vendor if not df_only_vendor.empty else pd.DataFrame(columns=["invoice_no"])).to_excel(
            writer, sheet_name="Missing in Vendor", index=False)
    buf.seek(0)
    return buf.getvalue()


# Columns a vendor should fill in. These match what statement_parser detects.
TEMPLATE_COLUMNS = ["Invoice No", "Invoice Date", "Currency", "Amount"]

# A couple of example rows so vendors see the expected format.
TEMPLATE_SAMPLE = [
    {"Invoice No": "081/EXT/2026", "Invoice Date": "17-03-2026", "Currency": "USD", "Amount": 53.45},
    {"Invoice No": "062/EXT/2026", "Invoice Date": "01-03-2026", "Currency": "USD", "Amount": 545.46},
]


def build_template_excel() -> bytes:
    """A vendor statement template workbook with headers + sample rows."""
    df = pd.DataFrame(TEMPLATE_SAMPLE, columns=TEMPLATE_COLUMNS)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Vendor Statement", index=False)
        ws = writer.sheets["Vendor Statement"]
        # widen columns for readability
        widths = {"A": 22, "B": 16, "C": 12, "D": 16}
        for col, w in widths.items():
            ws.column_dimensions[col].width = w
        # Force the Invoice Date column (B) to Text so dd-mm-yyyy is kept as typed
        # and Excel doesn't reinterpret it as a date serial.
        for row in range(2, len(TEMPLATE_SAMPLE) + 2):
            ws[f"B{row}"].number_format = "@"
    buf.seek(0)
    return buf.getvalue()


def build_template_csv() -> bytes:
    df = pd.DataFrame(TEMPLATE_SAMPLE, columns=TEMPLATE_COLUMNS)
    return df.to_csv(index=False).encode("utf-8")


# ---------------------------------------------------------------------------
# Transaction ledger template (full-ledger / transaction reconciliation)
# ---------------------------------------------------------------------------
# Columns for a vendor's FULL ledger over a period. Debit increases the payable
# (invoices, debit notes); Credit decreases it (payments, credit notes).
# Opening/Closing Balance are single rows carrying the balance in Debit or Credit.
TXN_TEMPLATE_COLUMNS = [
    "Date", "Transaction Type", "Reference No", "Debit", "Credit", "Currency", "Narration",
]

TXN_TEMPLATE_SAMPLE = [
    {"Date": "01-03-2026", "Transaction Type": "Opening Balance", "Reference No": "",
     "Debit": 10000.00, "Credit": "", "Currency": "USD", "Narration": "Opening as on 01-Mar-2026"},
    {"Date": "05-03-2026", "Transaction Type": "Invoice", "Reference No": "081/EXT/2026",
     "Debit": 53.45, "Credit": "", "Currency": "USD", "Narration": "Supply invoice"},
    {"Date": "12-03-2026", "Transaction Type": "Payment", "Reference No": "PMT-5567",
     "Debit": "", "Credit": 5000.00, "Currency": "USD", "Narration": "Payment against invoices"},
    {"Date": "20-03-2026", "Transaction Type": "Credit Note", "Reference No": "CN-118",
     "Debit": "", "Credit": 250.00, "Currency": "USD", "Narration": "Rate difference credit"},
    {"Date": "31-03-2026", "Transaction Type": "Closing Balance", "Reference No": "",
     "Debit": 4803.45, "Credit": "", "Currency": "USD", "Narration": "Closing as on 31-Mar-2026"},
]


def build_txn_template_excel() -> bytes:
    df = pd.DataFrame(TXN_TEMPLATE_SAMPLE, columns=TXN_TEMPLATE_COLUMNS)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Vendor Ledger", index=False)
        ws = writer.sheets["Vendor Ledger"]
        widths = {"A": 14, "B": 18, "C": 18, "D": 14, "E": 14, "F": 10, "G": 32}
        for col, w in widths.items():
            ws.column_dimensions[col].width = w
        # Keep the Date column (A) as text so dd-mm-yyyy stays as typed
        for row in range(2, len(TXN_TEMPLATE_SAMPLE) + 2):
            ws[f"A{row}"].number_format = "@"
    buf.seek(0)
    return buf.getvalue()


def build_txn_template_csv() -> bytes:
    df = pd.DataFrame(TXN_TEMPLATE_SAMPLE, columns=TXN_TEMPLATE_COLUMNS)
    return df.to_csv(index=False).encode("utf-8")


# ---------------------------------------------------------------------------
# Engine templates — SAP ledger (Dataset A) and Vendor statement (Dataset B).
# These headers match what engine_parser detects.
# ---------------------------------------------------------------------------
SAP_TEMPLATE_COLUMNS = [
    "Document Type", "Supplier", "Document Number", "Assignment", "Text",
    "Clearing Document", "Reference", "Business Area", "Document Date", "Posting Date",
    "Amount in Doc. Curr.", "Amount in Local Currency", "Local Currency",
    "Payment block", "Withholding Tax Amt",
]

SAP_TEMPLATE_SAMPLE = [
    {"Document Type": "Invoice", "Supplier": "104741", "Document Number": "5100000001",
     "Assignment": "INV-001", "Text": "Supply invoice", "Clearing Document": "",
     "Reference": "INV-001", "Business Area": "EP00", "Document Date": "01-08-2026",
     "Posting Date": "01-08-2026", "Amount in Doc. Curr.": 100000,
     "Amount in Local Currency": 100000, "Local Currency": "INR",
     "Payment block": "", "Withholding Tax Amt": 0},
    {"Document Type": "Payment", "Supplier": "104741", "Document Number": "5100000002",
     "Assignment": "", "Text": "Payment against INV-001", "Clearing Document": "1400000005",
     "Reference": "", "Business Area": "EP00", "Document Date": "12-08-2026",
     "Posting Date": "12-08-2026", "Amount in Doc. Curr.": 100000,
     "Amount in Local Currency": 100000, "Local Currency": "INR",
     "Payment block": "", "Withholding Tax Amt": 0},
]

VENDOR_STMT_TEMPLATE_COLUMNS = [
    "Transaction Type", "Vendor Name", "Invoice Number", "Document Number", "Document Date",
    "Amount", "Currency", "Payment Reference",
]

VENDOR_STMT_TEMPLATE_SAMPLE = [
    {"Transaction Type": "Invoice", "Vendor Name": "ACME EXPORTS", "Invoice Number": "INV-001",
     "Document Number": "D-001", "Document Date": "01-08-2026", "Amount": 100000,
     "Currency": "INR", "Payment Reference": ""},
    {"Transaction Type": "Payment", "Vendor Name": "ACME EXPORTS", "Invoice Number": "",
     "Document Number": "P-001", "Document Date": "12-08-2026", "Amount": 100000,
     "Currency": "INR", "Payment Reference": "UTR12345"},
]


def _template_bytes(columns, sample, sheet_name, date_col_letter=None) -> bytes:
    df = pd.DataFrame(sample, columns=columns)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name=sheet_name, index=False)
        ws = writer.sheets[sheet_name]
        for i in range(len(columns)):
            ws.column_dimensions[chr(65 + i)].width = 18
    buf.seek(0)
    return buf.getvalue()


def build_sap_template_excel() -> bytes:
    return _template_bytes(SAP_TEMPLATE_COLUMNS, SAP_TEMPLATE_SAMPLE, "SAP Ledger")


def build_sap_template_csv() -> bytes:
    return pd.DataFrame(SAP_TEMPLATE_SAMPLE, columns=SAP_TEMPLATE_COLUMNS).to_csv(index=False).encode("utf-8")


def build_vendor_stmt_template_excel() -> bytes:
    return _template_bytes(VENDOR_STMT_TEMPLATE_COLUMNS, VENDOR_STMT_TEMPLATE_SAMPLE, "Vendor Statement")


def build_vendor_stmt_template_csv() -> bytes:
    return pd.DataFrame(VENDOR_STMT_TEMPLATE_SAMPLE, columns=VENDOR_STMT_TEMPLATE_COLUMNS).to_csv(index=False).encode("utf-8")


def build_engine_excel(vendor: str, response: dict) -> bytes:
    """Excel report for a full-engine reconciliation: a summary sheet
    (final status + counts + balance) and a per-transaction results sheet."""
    result = response.get("engine_result", response) or {}
    counts = result.get("counts", {}) or {}
    balance = result.get("balance", {}) or {}

    summary_rows = [
        ("Vendor", result.get("vendor_code") or vendor),
        ("Vendor Name", result.get("vendor_name") or ""),
        ("Reconciliation Type", "Engine (18-rule)"),
        ("Final Status", result.get("final_status") or ""),
        ("SAP Closing Balance", balance.get("sap_closing_balance")),
        ("Vendor Closing Balance", balance.get("vendor_closing_balance")),
        ("Balance Difference", balance.get("balance_difference")),
    ]
    for k, v in counts.items():
        summary_rows.append((f"Count: {k}", v))
    df_summary = pd.DataFrame(summary_rows, columns=["Metric", "Value"])
    df_results = pd.DataFrame(result.get("results", []) or [])

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df_summary.to_excel(writer, sheet_name="Summary", index=False)
        (df_results if not df_results.empty else pd.DataFrame(columns=["transaction_id"])).to_excel(
            writer, sheet_name="Results", index=False)
    buf.seek(0)
    return buf.getvalue()


def build_txn_excel(vendor: str, response: dict) -> bytes:
    """Excel report for a Transaction (full-ledger) reconciliation:
    a balance-bridge summary sheet, a by-type sheet, and all transactions."""
    bridge = response.get("bridge", {}) or {}
    summary_rows = [
        ("Vendor", vendor),
        ("Reconciliation Type", "Transaction"),
        ("Opening Balance", bridge.get("opening_balance")),
        ("Total Debits", bridge.get("total_debits")),
        ("Total Credits", bridge.get("total_credits")),
        ("Computed Closing", bridge.get("computed_closing")),
        ("Stated Closing", bridge.get("stated_closing")),
        ("Difference", bridge.get("difference")),
        ("Balanced", "Yes" if bridge.get("balanced") else "No"),
    ]
    df_summary = pd.DataFrame(summary_rows, columns=["Metric", "Value"])

    by_type = response.get("by_type", {}) or {}
    df_by_type = pd.DataFrame(
        [{"Transaction Type": k, "Count": v.get("count"),
          "Total Debit": v.get("debit"), "Total Credit": v.get("credit")}
         for k, v in by_type.items()]
    )

    df_txns = pd.DataFrame(response.get("transactions", []) or [])

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df_summary.to_excel(writer, sheet_name="Balance Bridge", index=False)
        (df_by_type if not df_by_type.empty else pd.DataFrame(columns=["Transaction Type"])).to_excel(
            writer, sheet_name="By Type", index=False)
        (df_txns if not df_txns.empty else pd.DataFrame(columns=["date"])).to_excel(
            writer, sheet_name="Transactions", index=False)
    buf.seek(0)
    return buf.getvalue()
