"""SQLite-backed reconciliation history.

Each reconcile is saved as one row, stamped with the username and a UTC
timestamp, plus the full result JSON so a run can be re-opened and re-exported.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "history.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reconciliation_history (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                username        TEXT    NOT NULL,
                created_at      TEXT    NOT NULL,   -- UTC ISO timestamp
                recon_type      TEXT    NOT NULL DEFAULT 'Closing Balance',
                vendor          TEXT    NOT NULL,
                vendor_name     TEXT,
                company_code    TEXT,
                sap_count       INTEGER,
                statement_rows  INTEGER,
                matched_count       INTEGER,
                amount_mismatch_count INTEGER,
                only_in_sap_count     INTEGER,
                only_in_vendor_count  INTEGER,
                sap_total       REAL,
                vendor_total    REAL,
                net_difference  REAL,
                payload         TEXT    NOT NULL    -- full JSON of the reconcile response
            )
            """
        )
        # Migration: add recon_type to older DBs that predate this column.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(reconciliation_history)")}
        if "recon_type" not in cols:
            conn.execute(
                "ALTER TABLE reconciliation_history "
                "ADD COLUMN recon_type TEXT NOT NULL DEFAULT 'Closing Balance'"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_hist_user_date "
            "ON reconciliation_history (username, created_at)"
        )
        # Per-(company code, year) running counter for reconciliation request IDs
        # of the form VEN/RECO/<CC>/<YEAR>/<NNN>.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recon_request_seq (
                company_code TEXT NOT NULL,
                year         TEXT NOT NULL,
                last_seq     INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (company_code, year)
            )
            """
        )


def next_request_id(company_code: str, year: str | int | None = None) -> str:
    """Return the next reconciliation request ID for a company code + year.

    Format: VEN/RECO/<YEAR>/<COMPANY_CODE>/<NNN> where NNN is a zero-padded,
    per-(company, year) sequence starting at 001. YEAR is the financial year
    (e.g. "2025-26") when provided, else the current calendar year. Atomic
    within SQLite so two concurrent requests never collide.
    """
    cc = (company_code or "NA").strip().upper() or "NA"
    yr = str(year or datetime.now(timezone.utc).year).strip() or str(datetime.now(timezone.utc).year)
    with _conn() as conn:
        conn.execute(
            "INSERT INTO recon_request_seq (company_code, year, last_seq) VALUES (?, ?, 1) "
            "ON CONFLICT(company_code, year) DO UPDATE SET last_seq = last_seq + 1",
            (cc, yr),
        )
        row = conn.execute(
            "SELECT last_seq FROM recon_request_seq WHERE company_code = ? AND year = ?",
            (cc, yr),
        ).fetchone()
    seq = row["last_seq"] if row else 1
    # Financial year first, then company code, then the running serial number.
    return f"VEN/RECO/{yr}/{cc}/{seq:03d}"


def _insert(row: tuple) -> int:
    with _conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO reconciliation_history
                (username, created_at, recon_type, vendor, vendor_name, company_code,
                 sap_count, statement_rows, matched_count, amount_mismatch_count,
                 only_in_sap_count, only_in_vendor_count, sap_total, vendor_total,
                 net_difference, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )
        return cur.lastrowid


def save_run(username: str, response: dict) -> int:
    """Persist a Closing Balance (open-items) reconcile. Returns the new row id."""
    summary = (response.get("result") or {}).get("summary") or {}
    row = (
        username,
        datetime.now(timezone.utc).isoformat(),
        "Closing Balance",
        response.get("vendor") or "",
        response.get("vendor_name") or "",
        response.get("company_code") or "",
        response.get("sap_count") or 0,
        response.get("statement_row_count") or 0,
        summary.get("matched_count") or 0,
        summary.get("amount_mismatch_count") or 0,
        summary.get("only_in_sap_count") or 0,
        summary.get("only_in_vendor_count") or 0,
        summary.get("sap_total") or 0,
        summary.get("vendor_total") or 0,
        summary.get("net_difference") or 0,
        json.dumps(response),
    )
    return _insert(row)


def save_txn_run(username: str, response: dict) -> int:
    """Persist a Transaction (full-ledger) reconcile. Returns the new row id.

    The transaction ledger has a balance bridge rather than the open-items
    summary, so we map its figures onto the shared columns:
      statement_rows  <- transaction count
      sap_total       <- stated closing balance
      vendor_total    <- computed closing balance
      net_difference  <- bridge difference
    """
    bridge = response.get("bridge") or {}
    row = (
        username,
        datetime.now(timezone.utc).isoformat(),
        "Transaction",
        response.get("vendor") or "",
        response.get("vendor_name") or "",
        response.get("company_code") or "",
        0,                                        # sap_count (n/a until SAP wired)
        response.get("row_count") or 0,           # statement_rows -> txn count
        0, 0, 0, 0,                               # matched/mismatch/miss counts (n/a)
        bridge.get("stated_closing") or 0,        # sap_total -> stated closing
        bridge.get("computed_closing") or 0,      # vendor_total -> computed closing
        bridge.get("difference") or 0,            # net_difference -> bridge diff
        json.dumps(response),
    )
    return _insert(row)


def save_engine_run(username: str, response: dict) -> int:
    """Persist an Engine (full-rule) reconcile. Maps engine figures onto the
    shared columns:
      matched_count       <- count of matched/auto-matched/payment-matched
      amount_mismatch_count <- possible matches
      only_in_sap_count   <- missing in SAP
      only_in_vendor_count <- missing in vendor
      sap_total/vendor_total <- SAP/vendor closing balance
      net_difference      <- balance difference
    """
    counts = response.get("counts") or {}
    balance = response.get("balance") or {}
    matched = (counts.get("AUTO MATCHED", 0) + counts.get("PAYMENT MATCHED", 0)
               + counts.get("FULLY MATCHED", 0))
    possible = counts.get("POSSIBLE MATCH", 0) + counts.get("POSSIBLE DUPLICATE", 0)
    row = (
        username,
        datetime.now(timezone.utc).isoformat(),
        "Engine",
        response.get("vendor") or "",
        response.get("vendor_name") or "",
        response.get("company_code") or "",
        0,
        counts.get("total", 0),
        matched,
        possible,
        counts.get("MISSING IN SAP", 0),
        counts.get("MISSING IN VENDOR STATEMENT", 0),
        balance.get("sap_closing_balance") or 0,
        balance.get("vendor_closing_balance") or 0,
        balance.get("balance_difference") or 0,
        json.dumps(response),
    )
    return _insert(row)


def list_runs(username: str, date_from: str = "", date_to: str = "") -> list[dict]:
    """List a user's runs (newest first), optionally filtered by date range.

    date_from / date_to are 'YYYY-MM-DD' (inclusive). Comparison is on the
    calendar date portion of the UTC timestamp.
    """
    sql = ["SELECT id, username, created_at, recon_type, vendor, vendor_name, company_code,",
           "sap_count, statement_rows, matched_count, amount_mismatch_count,",
           "only_in_sap_count, only_in_vendor_count, sap_total, vendor_total, net_difference, payload",
           "FROM reconciliation_history WHERE username = ?"]
    params: list = [username]
    if date_from:
        sql.append("AND substr(created_at, 1, 10) >= ?")
        params.append(date_from)
    if date_to:
        sql.append("AND substr(created_at, 1, 10) <= ?")
        params.append(date_to)
    sql.append("ORDER BY id DESC")
    with _conn() as conn:
        rows = conn.execute(" ".join(sql), params).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        payload_json = d.pop("payload", None)
        # Surface the final status (and total row count) from the stored payload
        # so the history LIST can show whether a run completed, not just blank.
        final_status, total_rows = "", None
        record_tally = None
        match_count = 0           # cleanly matched (both sides tally)
        possibly_match_count = 0  # matched via the recon engine (possible / TDS / rounding)
        open_count = 0            # true open items that still don't tally
        if payload_json:
            try:
                p = json.loads(payload_json)
                final_status = p.get("final_status") or (p.get("engine_result") or {}).get("final_status") or ""
                counts = p.get("counts") or (p.get("engine_result") or {}).get("counts") or {}
                total_rows = counts.get("total")
                # Per-side record tally (SAP vs Vendor transaction counts, matched,
                # open) so the history list can show both sides and prove the tally.
                record_tally = counts.get("record_tally")
                d["request_id"] = p.get("request_id") or ""
                # Clean matches (tally on both sides).
                match_count = (counts.get("AUTO MATCHED", 0) + counts.get("PAYMENT MATCHED", 0)
                               + counts.get("FULLY MATCHED", 0))
                # Engine-assisted matches (fuzzy / TDS / rounding / bank charges).
                possibly_match_count = (counts.get("POSSIBLE MATCH", 0) + counts.get("POSSIBLE DUPLICATE", 0)
                                        + counts.get("PARTIAL PAYMENT", 0) + counts.get("RECONCILED - TDS", 0)
                                        + counts.get("BANK CHARGES", 0) + counts.get("ROUNDING DIFFERENCE", 0))
                # Open items = everything that is neither a clean nor engine match.
                open_count = (counts.get("MISSING IN SAP", 0) + counts.get("MISSING IN VENDOR STATEMENT", 0)
                              + counts.get("UNALLOCATED PAYMENT", 0) + counts.get("CREDIT NOTE MISSING IN SAP", 0)
                              + counts.get("PAYMENT NOT REFLECTED BY VENDOR", 0)
                              + counts.get("DEBIT NOTE MISSING IN VENDOR STATEMENT", 0))
            except (json.JSONDecodeError, TypeError):
                pass
        d["final_status"] = final_status
        d["total_rows"] = total_rows
        d["record_tally"] = record_tally
        # Record-level category counts (reconcile to SAP + Vendor records).
        # Prefer the engine's record-level tally; fall back to result-row counts.
        rt = record_tally or {}
        d["match_count"] = rt.get("rec_match", match_count)
        d["possibly_match_count"] = rt.get("rec_possibly_match", possibly_match_count)
        d["mismatch_count"] = rt.get("rec_mismatch", 0)
        d["miss_sap_count"] = rt.get("rec_miss_sap", d.get("only_in_sap_count") or 0)
        d["miss_vendor_count"] = rt.get("rec_miss_vendor", d.get("only_in_vendor_count") or 0)
        d["open_count"] = open_count
        out.append(d)
    return out


def clear_runs(username: str) -> int:
    """Delete all reconciliation runs for a user. Returns the number removed.
    Also resets the per-(company, year) request-ID counter so IDs restart."""
    with _conn() as conn:
        cur = conn.execute("DELETE FROM reconciliation_history WHERE username = ?", (username,))
        removed = cur.rowcount
        # If no runs remain for anyone, reset the shared request-ID sequence.
        remaining = conn.execute("SELECT COUNT(*) FROM reconciliation_history").fetchone()[0]
        if remaining == 0:
            try:
                conn.execute("DELETE FROM recon_request_seq")
            except sqlite3.Error:
                pass
    return removed


def get_run(username: str, run_id: int) -> dict | None:
    """Return one run's full payload — but only if it belongs to the user."""
    with _conn() as conn:
        r = conn.execute(
            "SELECT payload FROM reconciliation_history WHERE id = ? AND username = ?",
            (run_id, username),
        ).fetchone()
    if not r:
        return None
    try:
        return json.loads(r["payload"])
    except (json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Open-item resolution: mark/unmark an open item as resolved (with a note).
# Resolutions live inside the run payload under "resolutions": {txn_id: {...}}.
# The engine bridge is recomputed to exclude resolved items so the Diff closes.
# ---------------------------------------------------------------------------
_OPEN_STATUSES = {
    "UNALLOCATED PAYMENT", "MISSING IN SAP", "CREDIT NOTE MISSING IN SAP",
    "MISSING IN VENDOR STATEMENT", "PAYMENT NOT REFLECTED BY VENDOR",
    "DEBIT NOTE MISSING IN VENDOR STATEMENT",
}
_VENDOR_ONLY = {"MISSING IN SAP", "UNALLOCATED PAYMENT", "CREDIT NOTE MISSING IN SAP"}
_SAP_ONLY = {"MISSING IN VENDOR STATEMENT", "PAYMENT NOT REFLECTED BY VENDOR",
             "DEBIT NOTE MISSING IN VENDOR STATEMENT"}
_REDUCES = {"UNALLOCATED PAYMENT", "CREDIT NOTE MISSING IN SAP", "PAYMENT NOT REFLECTED BY VENDOR"}


def _recompute_bridge(payload: dict) -> None:
    """Recompute the engine bridge in-place, excluding resolved open items so
    the 'Diff should be zero' reflects outstanding (unresolved) items only."""
    er = payload.get("engine_result") or {}
    results = er.get("results") or []
    bal = er.get("balance") or {}
    bridge = bal.get("bridge")
    if bridge is None:
        return
    resolved = payload.get("resolutions") or {}
    sap_close = bal.get("sap_closing_balance", 0)
    ven_close = bal.get("vendor_closing_balance", 0)

    by_status = {}
    sap_only = 0.0
    vendor_only = 0.0
    for r in results:
        tid = r.get("transaction_id", "")
        if resolved.get(tid, {}).get("resolved"):
            continue  # resolved items drop out of the bridge
        st = r.get("match_status")
        if st in _SAP_ONLY:
            raw = round(r.get("sap_amount") or 0, 2)
            by_status[st] = round(by_status.get(st, 0) + raw, 2)
            sap_only = round(sap_only + raw, 2)
        elif st in _VENDOR_ONLY:
            raw = round(r.get("vendor_amount") or 0, 2)
            by_status[st] = round(by_status.get(st, 0) + raw, 2)
            vendor_only = round(vendor_only + raw, 2)

    # Resolved items are treated as "accepted" differences: remove them from the
    # gap too, so the residual closes toward zero as items are resolved.
    resolved_raw = 0.0
    for r in results:
        tid = r.get("transaction_id", "")
        if not resolved.get(tid, {}).get("resolved"):
            continue
        st = r.get("match_status")
        if st in _SAP_ONLY:
            resolved_raw = round(resolved_raw + (r.get("sap_amount") or 0), 2)
        elif st in _VENDOR_ONLY:
            resolved_raw = round(resolved_raw + (r.get("vendor_amount") or 0), 2)

    opening_gap = bridge.get("opening_gap", 0)
    gap = round(sap_close + ven_close - resolved_raw, 2)
    tds_adjustment = round(gap - opening_gap - sap_only - vendor_only, 2)
    bridge["by_status_signed"] = by_status
    bridge["sap_only_total"] = sap_only
    bridge["vendor_only_total"] = vendor_only
    bridge["gap"] = gap
    bridge["tds_adjustment"] = tds_adjustment
    bridge["residual"] = 0.0
    bridge["balanced"] = True


def set_resolution(username: str, run_id: int, transaction_id: str,
                   resolved: bool, note: str = "") -> dict | None:
    """Mark/unmark an open item resolved on a run. Returns the updated payload."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT payload FROM reconciliation_history WHERE id = ? AND username = ?",
            (run_id, username),
        ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            return None

        res = payload.setdefault("resolutions", {})
        if resolved:
            res[transaction_id] = {
                "resolved": True,
                "note": note or "",
                "at": datetime.now(timezone.utc).isoformat(),
                "by": username,
            }
        else:
            res.pop(transaction_id, None)

        _recompute_bridge(payload)
        conn.execute(
            "UPDATE reconciliation_history SET payload = ? WHERE id = ? AND username = ?",
            (json.dumps(payload), run_id, username),
        )
    return payload
