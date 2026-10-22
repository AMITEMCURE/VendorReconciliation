"""SAP 'All open items' client.

Reuses the field mapping proven in the payment portal. We call the SAP HTTP
service with basic auth and map the raw line items into a clean invoice shape:

  Invoice No.      <- ALLOC_NMBR
  Invoice Date     <- DOC_DATE
  Document Number  <- DOC_NO
  Currency         <- CURRENCY
  Amount           <- AMT_DOCCUR
  Due Date         <- BLINE_DATE
  Business Area    <- BUS_AREA
  Profit Center    <- REF_KEY_1
"""
import asyncio

import httpx

from .config import settings


def _to_float(v) -> float:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return 0.0


def map_item(it: dict) -> dict:
    """Map one raw SAP line item to the reconciliation invoice shape."""
    amount_raw = it.get("AMT_DOCCUR_LONG") or it.get("AMT_DOCCUR") or "0"
    lc_amount_raw = it.get("LC_AMOUNT_LONG") or it.get("LC_AMOUNT") or "0"
    wtax_raw = it.get("WI_TAX_AMT_LONG") or it.get("WI_TAX_AMT") or "0"
    return {
        "invoice_no": (it.get("ALLOC_NMBR") or "").strip(),
        "invoice_date": (it.get("DOC_DATE") or "").strip(),
        "document_number": (it.get("DOC_NO") or "").strip(),
        "currency": (it.get("CURRENCY") or "").strip(),
        "amount": _to_float(amount_raw),
        "due_date": (it.get("BLINE_DATE") or "").strip(),
        "business_area": (it.get("BUS_AREA") or "").strip(),
        "profit_center": (it.get("REF_KEY_1") or "").strip(),
        "company_code": (it.get("COMP_CODE") or "").strip(),
        "po_number": (it.get("REF_DOC") or "").strip(),
        "fiscal_year": (it.get("FISC_YEAR") or "").strip(),
        "item_text": (it.get("ITEM_TEXT") or "").strip(),
        "vendor": (it.get("VENDOR") or "").strip(),
        # ---- extra fields for the Data Upload open-items table ----
        "document_type": (it.get("DOC_TYPE") or "").strip(),
        "assignment": (it.get("ALLOC_NMBR") or "").strip(),
        "clearing_document": (it.get("CLR_DOC_NO") or "").strip(),
        "reference": (it.get("REF_DOC_NO_LONG") or it.get("REF_DOC_NO") or "").strip(),
        "posting_date": (it.get("PSTNG_DATE") or "").strip(),
        "document_date": (it.get("DOC_DATE") or "").strip(),
        "amount_doc_curr": _to_float(amount_raw),
        "amount_local_curr": _to_float(lc_amount_raw),
        "local_currency": (it.get("LOC_CURRCY") or "").strip(),
        "payment_block": (it.get("PMNT_BLOCK") or "").strip(),
        "withholding_tax_amt": _to_float(wtax_raw),
    }


async def fetch_open_items(client: httpx.AsyncClient, vendor: str, company_code: str) -> dict:
    """Fetch + map open items for a single vendor. Never raises — returns a dict
    with an 'error' key on failure and an empty invoice list."""
    # SAP treats the company code case-sensitively, so always send it uppercased.
    cc = (company_code or settings.SAP_DEFAULT_COMPANY_CODE or "").strip().upper()
    params = {
        "IM_VEND_OPEN_ITEMS": "X",
        "IM_GL_OPEN_ITEMS": "",
        "IM_COMPANYCODE": cc,
        "IM_VENDOR": (vendor or "").strip(),
    }
    out = {"vendor": vendor, "vendor_name": "", "message": "", "invoices": [], "error": ""}
    try:
        resp = await client.get(
            settings.SAP_OPEN_ITEMS_URL,
            params=params,
            auth=(settings.SAP_API_USERNAME, settings.SAP_API_PASSWORD),
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPError as e:
        out["error"] = f"Could not reach SAP: {e}"
        return out

    if resp.status_code == 401:
        out["error"] = "SAP authentication failed"
        return out
    if resp.status_code >= 400:
        out["error"] = f"SAP returned {resp.status_code}"
        return out

    try:
        data = resp.json()
    except Exception:
        out["error"] = "SAP did not return JSON"
        return out

    out["message"] = data.get("EX_MESSSAGE") or data.get("EX_MESSAGE") or ""
    out["vendor_name"] = (data.get("EX_VENDORNAME") or "").strip()
    line_block = data.get("GT_VENDLINEITEMS") or {}
    raw_items = line_block.get("item") if isinstance(line_block, dict) else None
    if raw_items is None:
        raw_items = []
    elif isinstance(raw_items, dict):
        raw_items = [raw_items]
    out["invoices"] = [map_item(it) for it in raw_items if isinstance(it, dict)]
    return out


async def fetch_open_items_single(vendor: str, company_code: str) -> dict:
    cc = company_code or settings.SAP_DEFAULT_COMPANY_CODE
    async with httpx.AsyncClient(timeout=30.0, verify=True) as client:
        return await fetch_open_items(client, vendor, cc)


def map_all_item(it: dict) -> dict:
    """Map one raw SAP 'All_items' line (German field names) to our table shape.

    This endpoint returns ALL transactions (invoices, payments, cleared items)
    for a date range, not just open items. SHKZG is the debit/credit indicator
    (S = debit, H = credit)."""
    doc_amt = it.get("WRSHB") or "0"
    lc_amt = it.get("DMSHB") or "0"
    wtax = it.get("QSSHB") or "0"
    return {
        # fields the 15-column Data Upload table reads
        "document_type": (it.get("BLART") or "").strip(),
        "assignment": (it.get("ZUONR") or "").strip(),
        "document_number": (it.get("BELNR") or "").strip(),
        "item_text": (it.get("SGTXT") or "").strip(),
        "clearing_document": (it.get("AUGBL") or "").strip(),
        "reference": (it.get("XBLNR") or "").strip(),
        "business_area": (it.get("GSBER") or "").strip(),
        "document_date": (it.get("BLDAT") or "").strip(),
        "posting_date": (it.get("BUDAT") or "").strip(),
        "amount_doc_curr": _to_float(doc_amt),
        "amount_local_curr": _to_float(lc_amt),
        "local_currency": (it.get("HWAER") or "").strip(),
        "payment_block": (it.get("ZLSPR") or "").strip(),
        "withholding_tax_amt": _to_float(wtax),
        # extra context
        "currency": (it.get("WAERS") or "").strip(),
        "debit_credit": (it.get("SHKZG") or "").strip(),
        "company_code": (it.get("BUKRS") or "").strip(),
        "vendor": (it.get("KONTO") or it.get("U_LIFNR") or "").strip(),
    }


async def fetch_all_items(client: httpx.AsyncClient, vendor: str, company_code: str,
                          date_from: str, date_to: str) -> dict:
    """Fetch + map ALL transactions for a vendor over a date range from the SAP
    All_items service. Never raises — returns a dict with an 'error' key on
    failure and an empty item list."""
    cc = (company_code or settings.SAP_DEFAULT_COMPANY_CODE or "").strip().upper()
    params = {
        "IM_VEND_All_ITEMS": "X",
        "IM_COMPANYCODE": cc,
        "IM_VENDOR": (vendor or "").strip(),
        "IM_FROMDATE": (date_from or "").strip(),
        "IM_TODATE": (date_to or "").strip(),
    }
    out = {"vendor": vendor, "vendor_name": "", "message": "", "invoices": [], "error": ""}
    try:
        resp = await client.get(
            settings.SAP_ALL_ITEMS_URL,
            params=params,
            auth=(settings.SAP_API_USERNAME, settings.SAP_API_PASSWORD),
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPError as e:
        out["error"] = f"Could not reach SAP: {e}"
        return out

    if resp.status_code == 401:
        out["error"] = "SAP authentication failed"
        return out
    if resp.status_code >= 400:
        out["error"] = f"SAP returned {resp.status_code}"
        return out

    try:
        data = resp.json()
    except Exception:
        out["error"] = "SAP did not return JSON"
        return out

    out["message"] = data.get("EX_MESSSAGE") or data.get("EX_MESSAGE") or ""
    out["vendor_name"] = (data.get("EX_VENDORNAMEE") or data.get("EX_VENDORNAME") or "").strip()

    # GT_AllITEMS is a list whose element(s) carry an "item" array.
    block = data.get("GT_AllITEMS")
    raw_items: list = []
    if isinstance(block, list):
        for entry in block:
            if isinstance(entry, dict):
                inner = entry.get("item")
                if isinstance(inner, dict):
                    raw_items.append(inner)
                elif isinstance(inner, list):
                    raw_items.extend([x for x in inner if isinstance(x, dict)])
    elif isinstance(block, dict):
        inner = block.get("item")
        if isinstance(inner, dict):
            raw_items = [inner]
        elif isinstance(inner, list):
            raw_items = [x for x in inner if isinstance(x, dict)]

    out["invoices"] = [map_all_item(it) for it in raw_items]
    return out


async def fetch_all_items_many(vendors: list[str], company_code: str,
                               date_from: str, date_to: str) -> list[dict]:
    """Fetch All_items for several vendors concurrently over the same date range."""
    cc = company_code or settings.SAP_DEFAULT_COMPANY_CODE
    cleaned = [v.strip() for v in vendors if v and v.strip()]
    if not cleaned:
        return []
    async with httpx.AsyncClient(timeout=60.0, verify=True) as client:
        tasks = [fetch_all_items(client, v, cc, date_from, date_to) for v in cleaned]
        return await asyncio.gather(*tasks)


async def fetch_open_items_many(vendors: list[str], company_code: str) -> list[dict]:
    """Fetch open items for several vendors concurrently, sharing one client.

    Returns one result dict per vendor (same shape as fetch_open_items), in the
    same order as the input list. Never raises — per-vendor errors land on the
    'error' key of that vendor's result."""
    cc = company_code or settings.SAP_DEFAULT_COMPANY_CODE
    cleaned = [v.strip() for v in vendors if v and v.strip()]
    if not cleaned:
        return []
    async with httpx.AsyncClient(timeout=30.0, verify=True) as client:
        tasks = [fetch_open_items(client, v, cc) for v in cleaned]
        return await asyncio.gather(*tasks)
