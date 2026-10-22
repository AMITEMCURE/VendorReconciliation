"""Vendor Reconciliation Portal — FastAPI backend.

Fetches vendor open items from SAP and reconciles them against an uploaded
vendor statement of account (Excel/CSV). Serves the static frontend too.
"""
from __future__ import annotations

import json
import os

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import authenticate, create_access_token, get_current_user
from .config import settings
from .export import (
    build_excel, build_template_csv, build_template_excel, TEMPLATE_COLUMNS,
    build_txn_template_csv, build_txn_template_excel, TXN_TEMPLATE_COLUMNS,
    build_txn_excel, build_engine_excel,
    build_sap_template_excel, build_sap_template_csv,
    build_vendor_stmt_template_excel, build_vendor_stmt_template_csv,
    SAP_TEMPLATE_COLUMNS, VENDOR_STMT_TEMPLATE_COLUMNS,
)
from . import history_store
from . import config_store
from .reconcile import reconcile
from .sap_client import (
    fetch_open_items_single, fetch_open_items_many, fetch_all_items_many,
)
from .statement_parser import parse_statement
from .txn_parser import parse_ledger
from .engine_parser import parse_sap, parse_vendor
from .recon_engine.engine import reconcile as engine_reconcile

app = FastAPI(title="Vendor Reconciliation Portal", version="1.0.0")


@app.on_event("startup")
def _startup():
    history_store.init_db()
    config_store.init_config_db()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)


class LoginIn(BaseModel):
    username: str
    password: str


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "sap_configured": bool(settings.SAP_OPEN_ITEMS_URL and settings.SAP_API_USERNAME),
        "default_company_code": settings.SAP_DEFAULT_COMPANY_CODE,
    }


@app.post("/api/auth/login")
def login(body: LoginIn):
    user = authenticate(body.username, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = create_access_token(user["username"])
    return {"token": token, "user": user}


@app.get("/api/auth/me")
def me(user: dict = Depends(get_current_user)):
    return user


# The login hero image (reuses the same logo file as the payment portal).
@app.get("/assets/login-logo.png")
def login_logo():
    return FileResponse(os.path.join(STATIC_DIR, "LogoF.png"))


@app.get("/api/template/columns")
def template_columns():
    """The exact column headers a vendor statement should contain."""
    return {"columns": TEMPLATE_COLUMNS}


@app.get("/api/template/download")
def template_download(fmt: str = "xlsx"):
    """Download a blank vendor statement template (xlsx or csv)."""
    if fmt == "csv":
        return Response(
            content=build_template_csv(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="vendor_statement_template.csv"'},
        )
    return Response(
        content=build_template_excel(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="vendor_statement_template.xlsx"'},
    )


@app.get("/api/txn-template/columns")
def txn_template_columns():
    """Column headers for a vendor's full transaction ledger."""
    return {"columns": TXN_TEMPLATE_COLUMNS}


@app.get("/api/txn-template/download")
def txn_template_download(fmt: str = "xlsx"):
    """Download a blank transaction-ledger template (xlsx or csv)."""
    if fmt == "csv":
        return Response(
            content=build_txn_template_csv(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="vendor_ledger_template.csv"'},
        )
    return Response(
        content=build_txn_template_excel(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="vendor_ledger_template.xlsx"'},
    )


@app.post("/api/txn-reconcile")
async def txn_reconcile(
    ledger: UploadFile = File(...),
    vendor: str = Form(""),
    user: dict = Depends(get_current_user),
):
    """Analyze a vendor's full transaction ledger: verify the balance bridge
    (Opening + Debits - Credits = Closing) and group by transaction type.

    NOTE: The SAP all-transactions side is not wired yet (pending the SAP API
    details from your team). For now this validates and summarizes the vendor
    ledger; SAP-side matching will be added once the endpoint is confirmed.
    """
    content = await ledger.read()
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded ledger is empty")
    try:
        parsed = parse_ledger(ledger.filename or "ledger", content)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    response = {
        "vendor": vendor.strip(),
        "vendor_name": "",
        "company_code": "",
        "recon_type": "Transaction",
        "sap_side": "not_configured",   # placeholder until SAP all-transactions API is wired
        "row_count": parsed["row_count"],
        "columns_detected": parsed["columns_detected"],
        "bridge": parsed["bridge"],
        "by_type": parsed["by_type"],
        "transactions": parsed["transactions"],
        "warnings": parsed["warnings"],
    }

    # Save to history (Transaction type). Best-effort — never fail the analysis.
    try:
        response["history_id"] = history_store.save_txn_run(user["username"], response)
    except Exception:
        response["history_id"] = None

    return response


@app.get("/api/sap/open-items")
async def sap_open_items(vendor: str, company_code: str = "", _: dict = Depends(get_current_user)):
    """Fetch a vendor's open items from SAP (used to preview before reconciling)."""
    if not settings.SAP_OPEN_ITEMS_URL:
        raise HTTPException(status_code=503, detail="SAP API is not configured on the server")
    if not vendor.strip():
        raise HTTPException(status_code=400, detail="vendor is required")

    r = await fetch_open_items_single(vendor.strip(), company_code.strip())
    if r["error"]:
        raise HTTPException(status_code=502, detail=r["error"])
    return {
        "vendor": vendor,
        "vendor_name": r["vendor_name"],
        "company_code": company_code or settings.SAP_DEFAULT_COMPANY_CODE,
        "message": r["message"],
        "count": len(r["invoices"]),
        "invoices": r["invoices"],
    }


def _split_vendor_codes(raw: str) -> list[str]:
    """Split a free-text vendor field into individual codes.

    Accepts separation by comma, whitespace, semicolon or newline so a user can
    paste a list however they have it."""
    import re
    parts = re.split(r"[\s,;]+", (raw or "").strip())
    # De-duplicate while preserving order.
    seen, out = set(), []
    for p in parts:
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


@app.get("/api/sap/open-items-multi")
async def sap_open_items_multi(
    vendors: str,
    company_code: str = "",
    date_from: str = "",
    date_to: str = "",
    _: dict = Depends(get_current_user),
):
    """Fetch transactions for MULTIPLE vendor codes at once (Data Upload screen).

    `vendors` is a free-text list separated by comma / space / semicolon /
    newline. When a date range (date_from/date_to, YYYY-MM-DD) is supplied, the
    SAP 'All_items' service is used (all transactions for the period); otherwise
    the open-items service is used. Returns one combined, flat list of line
    items (each tagged with its vendor code + name) plus a per-vendor summary."""
    codes = _split_vendor_codes(vendors)
    if not codes:
        raise HTTPException(status_code=400, detail="Enter at least one vendor code")

    date_from, date_to = date_from.strip(), date_to.strip()
    use_all_items = bool(date_from and date_to)

    if use_all_items:
        if not settings.SAP_ALL_ITEMS_URL:
            raise HTTPException(status_code=503, detail="SAP All-items API is not configured on the server")
        results = await fetch_all_items_many(codes, company_code.strip(), date_from, date_to)
    else:
        if not settings.SAP_OPEN_ITEMS_URL:
            raise HTTPException(status_code=503, detail="SAP API is not configured on the server")
        results = await fetch_open_items_many(codes, company_code.strip())

    cc = company_code.strip() or settings.SAP_DEFAULT_COMPANY_CODE

    # One shared request ID for the whole batch: a single vendor (party) can
    # have several vendor codes in SAP, so all codes entered together belong to
    # the same reconciliation and share one VEN/RECO/<CC>/<YEAR>/NNN id.
    request_id = history_store.next_request_id(cc)

    items: list[dict] = []
    per_vendor: list[dict] = []
    for code, r in zip(codes, results):
        vname = r.get("vendor_name", "")
        rows = r.get("invoices", []) if not r.get("error") else []
        for inv in rows:
            row = dict(inv)
            row["vendor_code"] = inv.get("vendor") or code
            row["vendor_name"] = vname
            row["request_id"] = request_id
            items.append(row)
        per_vendor.append({
            "request_id": request_id,
            "vendor": code,
            "vendor_name": vname,
            "count": len(rows),
            "error": r.get("error", ""),
            "message": r.get("message", ""),
        })

    return {
        "company_code": cc,
        "request_id": request_id,
        "requested": codes,
        "source": "all_items" if use_all_items else "open_items",
        "date_from": date_from,
        "date_to": date_to,
        "total_count": len(items),
        "per_vendor": per_vendor,
        "items": items,
    }


@app.post("/api/sap/upload-preview")
async def sap_upload_preview(
    sap_file: UploadFile = File(...),
    company_code: str = Form(""),
    vendor_name: str = Form(""),
    fin_year: str = Form(""),
    _: dict = Depends(get_current_user),
):
    """Parse an uploaded SAP Excel/CSV and return rows in the same 15-column
    shape the Data Upload table uses — for on-screen preview without reconciling
    and without calling SAP. Also issues a unique reconciliation request ID
    (VEN/RECO/<financial year>/<company code>/<NNN>)."""
    content = await sap_file.read()
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded SAP file is empty")
    try:
        sap_txns = parse_sap(sap_file.filename or "sap", content)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Could not parse the SAP file: {e}")

    # Diagnostic: what headers did we see, and how many raw rows?
    detected_cols: list[str] = []
    raw_rows = 0
    try:
        from .engine_parser import _read
        _df = _read(sap_file.filename or "sap", content)
        detected_cols = [str(c) for c in _df.columns]
        raw_rows = int(len(_df))
    except Exception:
        pass

    items = []
    for t in sap_txns:
        items.append({
            "document_type": t.document_type,
            "vendor_code": t.vendor_code,
            "vendor": t.vendor_code,
            "vendor_name": t.vendor_name,
            "document_number": t.sap_doc_number,
            "assignment": t.invoice_number,
            "item_text": t.text,
            "clearing_document": t.clearing_document,
            "reference": t.invoice_number,
            "business_area": "",
            "document_date": t.document_date,
            "posting_date": t.posting_date,
            "amount_doc_curr": t.amount,
            "amount_local_curr": t.amount,
            "local_currency": t.currency,
            "payment_block": "",
            "withholding_tax_amt": t.tds_amount,
            "currency": t.currency,
            "debit_credit": t.debit_credit,
        })
    # Issue a unique request ID for this reconciliation batch.
    cc = (company_code or "").strip().upper()
    fy = (fin_year or "").strip()
    request_id = history_store.next_request_id(cc, fy or None)
    # Stamp the request id onto every row so it travels with the data.
    for row in items:
        row["request_id"] = request_id

    return {
        "source": "upload",
        "total_count": len(items),
        "items": items,
        "request_id": request_id,
        "company_code": cc,
        "vendor_name": (vendor_name or "").strip(),
        "fin_year": fy,
        "columns_detected": detected_cols,
        "raw_row_count": raw_rows,
    }


@app.post("/api/reconcile")
async def do_reconcile(
    vendor: str = Form(...),
    company_code: str = Form(""),
    statement: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """Fetch SAP open items for the vendor, parse the uploaded statement, and
    reconcile the two sides. Returns the classified result + parse metadata."""
    if not settings.SAP_OPEN_ITEMS_URL:
        raise HTTPException(status_code=503, detail="SAP API is not configured on the server")
    vendor = vendor.strip()
    if not vendor:
        raise HTTPException(status_code=400, detail="vendor is required")

    content = await statement.read()
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded statement is empty")

    try:
        parsed = parse_statement(statement.filename or "statement", content)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    sap = await fetch_open_items_single(vendor, company_code.strip())
    if sap["error"]:
        raise HTTPException(status_code=502, detail=sap["error"])

    result = reconcile(sap["invoices"], parsed["rows"])

    response = {
        "vendor": vendor,
        "vendor_name": sap["vendor_name"],
        "company_code": company_code or settings.SAP_DEFAULT_COMPANY_CODE,
        "sap_message": sap["message"],
        "sap_count": len(sap["invoices"]),
        "statement_row_count": parsed["row_count"],
        "columns_detected": parsed["columns_detected"],
        "warnings": parsed["warnings"],
        "result": result,
    }

    # Persist this run to history (stamped with the user + timestamp).
    try:
        run_id = history_store.save_run(user["username"], response)
        response["history_id"] = run_id
    except Exception:
        # History is best-effort; never fail the reconcile because of it.
        response["history_id"] = None

    return response


@app.post("/api/reconcile/export")
async def export_reconcile(
    vendor: str = Form(...),
    vendor_name: str = Form(""),
    result: str = Form(...),
    _: dict = Depends(get_current_user),
):
    """Build an Excel report from a reconciliation result (JSON string)."""
    try:
        result_obj = json.loads(result)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="result must be valid JSON")

    data = build_excel(vendor, vendor_name, result_obj)
    safe_vendor = "".join(c for c in vendor if c.isalnum() or c in ("-", "_")) or "vendor"
    filename = f"reconciliation_{safe_vendor}.xlsx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Reconciliation ENGINE (SAP file vs Vendor file, full 18-rule engine)
# ---------------------------------------------------------------------------

@app.get("/api/engine/sap-template/download")
def engine_sap_template(fmt: str = "xlsx"):
    """Download a SAP ledger template (Dataset A) for the Engine tab."""
    if fmt == "csv":
        return Response(
            content=build_sap_template_csv(), media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="sap_ledger_template.csv"'})
    return Response(
        content=build_sap_template_excel(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="sap_ledger_template.xlsx"'})


@app.get("/api/engine/vendor-template/download")
def engine_vendor_template(fmt: str = "xlsx"):
    """Download a full vendor-statement template (Dataset B) for the Engine tab."""
    if fmt == "csv":
        return Response(
            content=build_vendor_stmt_template_csv(), media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="vendor_statement_full_template.csv"'})
    return Response(
        content=build_vendor_stmt_template_excel(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="vendor_statement_full_template.xlsx"'})


@app.get("/api/engine/config")
def engine_get_config(_: dict = Depends(get_current_user)):
    """Return the current engine configuration (tolerances + rule toggles)."""
    return config_store.get_config().to_dict()


@app.put("/api/engine/config")
def engine_put_config(payload: dict, user: dict = Depends(get_current_user)):
    """Update the engine configuration. Unknown keys are ignored."""
    cfg = config_store.save_config(payload or {})
    return cfg.to_dict()


def _open_items_to_saptxn(vendor: str, vendor_name: str, company_code: str, invoices: list) -> list:
    """Map SAP open-items invoices (from the live API) into engine SapTxn rows."""
    from .recon_engine.models import SapTxn
    out = []
    for inv in invoices:
        out.append(SapTxn(
            company_code=inv.get("company_code") or company_code,
            vendor_code=inv.get("vendor") or vendor,
            vendor_name=vendor_name,
            sap_doc_number=inv.get("document_number") or "",
            document_type="Invoice",
            posting_date=inv.get("invoice_date") or "",
            document_date=inv.get("invoice_date") or "",
            invoice_number=inv.get("invoice_no") or "",
            amount=float(inv.get("amount") or 0),
            currency=inv.get("currency") or "",
            payment_reference="",
            text=inv.get("item_text") or "",
        ))
    return out


def _items_to_saptxn(items: list) -> list:
    """Map the on-screen Data Upload items (from a live fetch or All_items) into
    engine SapTxn rows. The invoice/matching key is the Assignment, falling back
    to Reference / Document Number."""
    from .recon_engine.models import SapTxn
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        inv = (it.get("assignment") or it.get("reference")
               or it.get("invoice_no") or it.get("document_number") or "").strip()
        out.append(SapTxn(
            company_code=it.get("company_code") or "",
            vendor_code=(it.get("vendor_code") or it.get("vendor") or "").strip(),
            vendor_name=it.get("vendor_name") or "",
            sap_doc_number=(it.get("document_number") or "").strip(),
            document_type=it.get("document_type") or "",
            posting_date=it.get("posting_date") or "",
            document_date=it.get("document_date") or "",
            invoice_number=inv,
            amount=float(it.get("amount_doc_curr") or it.get("amount") or 0),
            currency=it.get("local_currency") or it.get("currency") or "",
            debit_credit=it.get("debit_credit") or "",
            tds_amount=float(it.get("withholding_tax_amt") or 0),
            clearing_document=it.get("clearing_document") or "",
            payment_reference=(it.get("reference") or "").strip(),
            text=it.get("item_text") or "",
        ))
    return out


@app.post("/api/engine/reconcile-items")
async def engine_run_items(
    sap_items: str = Form(...),
    vendor_file: UploadFile = File(...),
    vendor: str = Form(""),
    company_code: str = Form(""),
    vendor_name: str = Form(""),
    request_id: str = Form(""),
    date_from: str = Form(""),
    date_to: str = Form(""),
    user: dict = Depends(get_current_user),
):
    """Run the engine using the SAP data ALREADY fetched/shown on screen
    (passed as a JSON array), reconciled against the uploaded vendor statement.

    This is the correct path for both the live All_items fetch and the open-items
    fetch — it uses exactly the rows the user is looking at (so the Assignment /
    Reference the user sees is what the engine matches on)."""
    try:
        items = json.loads(sap_items)
        if not isinstance(items, list):
            raise ValueError("sap_items must be a JSON array")
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid sap_items: {e}")

    ven_bytes = await vendor_file.read()
    if not ven_bytes:
        raise HTTPException(status_code=400, detail="The vendor file is empty")

    sap_txns = _items_to_saptxn(items)
    if not sap_txns:
        raise HTTPException(status_code=400, detail="No SAP rows to reconcile — fetch or load SAP data first")

    # Request ID: prefer the explicit form value, else recover it from the rows
    # (every row was stamped with it at fetch/upload time).
    req_id = (request_id or "").strip()
    if not req_id:
        for it in items:
            if isinstance(it, dict) and it.get("request_id"):
                req_id = str(it["request_id"]).strip()
                break

    try:
        ven_txns = parse_vendor(vendor_file.filename or "vendor", ven_bytes)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Could not parse the vendor file: {e}")

    vendor_cols: list[str] = []
    try:
        from .engine_parser import _read
        vendor_cols = [str(c) for c in _read(vendor_file.filename or "vendor", ven_bytes).columns]
    except Exception:
        pass

    cfg = config_store.get_config()
    report = engine_reconcile(sap_txns, ven_txns, cfg)
    result = report.as_dict()
    if not report.vendor_name and vendor_name:
        result["vendor_name"] = vendor_name

    # Identify which vendor rows look like opening/closing balances (for debug).
    def _is_bal(t):
        dt = (getattr(t, "document_type", "") or "").strip().upper()
        hay = " ".join(str(getattr(t, x, "") or "").lower()
                       for x in ("document_type", "description"))
        return dt in ("OP", "CL") or "opening" in hay or "closing" in hay
    ven_balance_rows = [
        {"invoice_number": t.invoice_number, "amount": t.amount,
         "description": t.description, "doc_date": t.document_date}
        for t in ven_txns if _is_bal(t)
    ]

    # Balance breakdown to debug closing-balance mismatches.
    def _dt(t):
        return (getattr(t, "document_type", "") or "").strip().upper()
    sap_op = sum(t.amount for t in sap_txns if _dt(t) == "OP")
    sap_txn_only = [t for t in sap_txns if _dt(t) not in ("OP", "CL")]
    sap_txn_sum = sum(t.amount for t in sap_txn_only)
    sap_raw_total = sum(t.amount for t in sap_txns)
    # Distinct vendor codes on the SAP side (to detect vendor-filter drops).
    sap_codes = {}
    for t in sap_txns:
        k = (t.vendor_code or "").strip()
        sap_codes[k] = sap_codes.get(k, 0) + 1
    ven_codes = {}
    for t in ven_txns:
        k = (getattr(t, "vendor_code", "") or "").strip()
        ven_codes[k] = ven_codes.get(k, 0) + 1

    balance_debug = {
        "sap_row_count_total": len(sap_txns),
        "sap_opening_sum": round(sap_op, 2),
        "sap_txn_count": len(sap_txn_only),
        "sap_txn_sum": round(sap_txn_sum, 2),
        "sap_computed_closing": round(sap_op + sap_txn_sum, 2),
        "sap_raw_total_all_rows": round(sap_raw_total, 2),
        "vendor_row_count_total": len(ven_txns),
        "sap_vendor_codes": sap_codes,
        "vendor_vendor_codes": ven_codes,
        "result_count": len(report.results),
    }

    diagnostics = {
        "balance_debug": balance_debug,
        "sap_sample": [
            {"invoice_number": t.invoice_number, "amount": t.amount,
             "currency": t.currency, "document_type": t.document_type}
            for t in sap_txns[:5]
        ],
        "vendor_balance_rows": ven_balance_rows,
        "vendor_sample": [
            {"invoice_number": t.invoice_number, "amount": t.amount,
             "debit": t.debit, "credit": t.credit, "currency": t.currency}
            for t in ven_txns[:5]
        ],
        "vendor_columns": vendor_cols,
    }

    try:
        response_for_history = {
            "vendor": vendor or report.vendor_code or "",
            "vendor_name": vendor_name or report.vendor_name or "",
            "company_code": company_code or "",
            "request_id": req_id,
            "recon_type": "Engine",
            "engine_result": result,
            "counts": report.counts,
            "final_status": report.final_status,
            "balance": report.balance,
            "diagnostics": diagnostics,
            "date_from": date_from.strip(),
            "date_to": date_to.strip(),
        }
        result["history_id"] = history_store.save_engine_run(user["username"], response_for_history)
    except Exception:
        result["history_id"] = None

    result["sap_source"] = "onscreen_items"
    result["sap_row_count"] = len(sap_txns)
    result["vendor_row_count"] = len(ven_txns)
    result["diagnostics"] = diagnostics
    return result


@app.post("/api/engine/reconcile-live")
async def engine_run_live(
    vendor: str = Form(...),
    company_code: str = Form(""),
    vendor_file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """Run the engine using the SAP side fetched LIVE from the open-items API
    (no SAP file upload). Only open invoices are available from that service;
    payments/cleared items await the SAP all-transactions API.

    The vendor statement is still uploaded (it is the vendor's own data)."""
    if not settings.SAP_OPEN_ITEMS_URL:
        raise HTTPException(status_code=503, detail="SAP API is not configured on the server")
    vendor = vendor.strip()
    if not vendor:
        raise HTTPException(status_code=400, detail="vendor is required")

    ven_bytes = await vendor_file.read()
    if not ven_bytes:
        raise HTTPException(status_code=400, detail="The vendor file is empty")

    sap = await fetch_open_items_single(vendor, company_code.strip())
    if sap["error"]:
        raise HTTPException(status_code=502, detail=sap["error"])

    cc = company_code.strip() or settings.SAP_DEFAULT_COMPANY_CODE
    sap_txns = _open_items_to_saptxn(vendor, sap["vendor_name"], cc, sap["invoices"])

    try:
        ven_txns = parse_vendor(vendor_file.filename or "vendor", ven_bytes)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Could not parse the vendor file: {e}")

    cfg = config_store.get_config()
    report = engine_reconcile(sap_txns, ven_txns, cfg)
    result = report.as_dict()
    if not report.vendor_name:
        result["vendor_name"] = sap["vendor_name"]

    try:
        response_for_history = {
            "vendor": vendor,
            "vendor_name": sap["vendor_name"],
            "company_code": cc,
            "recon_type": "Engine",
            "engine_result": result,
            "counts": report.counts,
            "final_status": report.final_status,
            "balance": report.balance,
        }
        result["history_id"] = history_store.save_engine_run(user["username"], response_for_history)
    except Exception:
        result["history_id"] = None

    result["sap_source"] = "live_open_items"
    result["sap_row_count"] = len(sap_txns)
    result["vendor_row_count"] = len(ven_txns)
    result["vendor_name"] = sap["vendor_name"]
    result["vendor_code"] = vendor
    result["notice"] = ("SAP side is LIVE open items only. Payments, cleared items, "
                        "credit/debit notes and TDS will be included once the SAP "
                        "all-transactions API is connected.")
    return result


@app.post("/api/engine/reconcile")
async def engine_run(
    sap_file: UploadFile = File(...),
    vendor_file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """Run the full reconciliation engine on an uploaded SAP ledger + vendor
    statement. Parses both files, applies the current config, returns the
    classified per-transaction results, balance bridge and final status."""
    sap_bytes = await sap_file.read()
    ven_bytes = await vendor_file.read()
    if not sap_bytes:
        raise HTTPException(status_code=400, detail="The SAP file is empty")
    if not ven_bytes:
        raise HTTPException(status_code=400, detail="The vendor file is empty")

    try:
        sap_txns = parse_sap(sap_file.filename or "sap", sap_bytes)
        ven_txns = parse_vendor(vendor_file.filename or "vendor", ven_bytes)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Could not parse the files: {e}")

    cfg = config_store.get_config()
    report = engine_reconcile(sap_txns, ven_txns, cfg)
    result = report.as_dict()

    # Save to history as an 'Engine' run (best-effort).
    try:
        response_for_history = {
            "vendor": report.vendor_code or "",
            "vendor_name": report.vendor_name or "",
            "company_code": "",
            "recon_type": "Engine",
            "engine_result": result,
            "counts": report.counts,
            "final_status": report.final_status,
            "balance": report.balance,
        }
        result["history_id"] = history_store.save_engine_run(user["username"], response_for_history)
    except Exception:
        result["history_id"] = None

    result["sap_row_count"] = len(sap_txns)
    result["vendor_row_count"] = len(ven_txns)
    return result


# ---------------------------------------------------------------------------
# Reconciliation history (per-user; SQLite-backed)
# ---------------------------------------------------------------------------

@app.get("/api/history")
def history_list(
    date_from: str = "",
    date_to: str = "",
    user: dict = Depends(get_current_user),
):
    """List the current user's reconciliation runs, newest first, optionally
    filtered by date range (YYYY-MM-DD, inclusive)."""
    runs = history_store.list_runs(user["username"], date_from.strip(), date_to.strip())
    return {"count": len(runs), "runs": runs}


@app.delete("/api/history")
def history_clear(user: dict = Depends(get_current_user)):
    """Clear ALL reconciliation runs for the current user."""
    removed = history_store.clear_runs(user["username"])
    return {"cleared": removed}


class ResolveIn(BaseModel):
    transaction_id: str
    resolved: bool = True
    note: str = ""


@app.post("/api/history/{run_id}/resolve")
def history_resolve(run_id: int, body: ResolveIn, user: dict = Depends(get_current_user)):
    """Mark (or unmark) one open item of a run as resolved, with an optional
    note. The Diff is recomputed to exclude resolved items."""
    payload = history_store.set_resolution(
        user["username"], run_id, body.transaction_id.strip(), body.resolved, body.note.strip())
    if payload is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return payload


@app.get("/api/history/{run_id}")
def history_get(run_id: int, user: dict = Depends(get_current_user)):
    """Return the full stored reconcile response for one run (own runs only)."""
    payload = history_store.get_run(user["username"], run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return payload


@app.get("/api/history/{run_id}/export")
def history_export(run_id: int, user: dict = Depends(get_current_user)):
    """Re-build the Excel report for a stored run (own runs only)."""
    payload = history_store.get_run(user["username"], run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if (payload.get("recon_type") == "Engine") or payload.get("engine_result"):
        data = build_engine_excel(payload.get("vendor", ""), payload)
    elif (payload.get("recon_type") == "Transaction") or payload.get("bridge"):
        data = build_txn_excel(payload.get("vendor", ""), payload)
    else:
        data = build_excel(payload.get("vendor", ""), payload.get("vendor_name", ""),
                           payload.get("result", {}))
    vendor = payload.get("vendor", "vendor")
    safe_vendor = "".join(c for c in vendor if c.isalnum() or c in ("-", "_")) or "vendor"
    filename = f"reconciliation_{safe_vendor}_{run_id}.xlsx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# Serve the frontend (mounted last so /api routes take precedence).
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
