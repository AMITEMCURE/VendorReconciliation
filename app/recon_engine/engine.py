"""The reconciliation engine.

Deterministic, explainable, configurable. Runs matching rules in the exact
priority order from the spec (section 14), never double-matching a consumed
transaction unless a one-to-many / many-to-one rule explicitly permits it, and
never mutating source data.
"""
from __future__ import annotations

from itertools import combinations

from .config import ReconConfig
from .models import (
    SapTxn, VendorTxn, MatchResult, ReconciliationReport, Status, DocKind,
)
from .normalize import (
    normalize_invoice, normalize_amount, normalize_date, date_diff_days,
    classify_sap, classify_vendor, _norm_text,
)


def _is_balance_row(src, side: str) -> bool:
    """True if a row is an opening/closing balance (document type OP/CL, or its
    type/text/description says opening/closing balance). Such rows are excluded
    from reconciliation matching."""
    dtype = (getattr(src, "document_type", "") or "").strip().upper()
    if dtype in ("OP", "CL"):
        return True
    import re as _re
    hay = " ".join(str(x or "").lower() for x in (
        getattr(src, "document_type", ""),
        getattr(src, "text", ""),
        getattr(src, "description", ""),
    ))
    # Match "opening"/"closing" (with or without "balance"/"bal") as whole words,
    # so a Transaction Type of just "Opening" or "Closing" is caught too.
    return bool(_re.search(r"\b(op(en(ing)?)?|cl(os(e|ing)?)?)\b.*bal", hay)
                or _re.search(r"\b(opening|closing)\b", hay))


class _Rec:
    """Internal wrapper holding a source txn + its normalised values + state.

    The source object is never mutated; all derived data lives here.
    """
    __slots__ = ("src", "side", "id", "inv", "amt", "date", "cur", "kind",
                 "pref", "vendor_code", "matched", "is_balance")

    def __init__(self, src, side: str, idx: int, cfg: ReconConfig):
        self.src = src
        self.side = side                       # "sap" or "vendor"
        self.id = f"{side.upper()}-{idx+1}"
        src.id = self.id
        self.inv = normalize_invoice(src.invoice_number, cfg)
        self.cur = (src.currency or "").strip().upper()
        self.matched = False
        # Opening / closing balance rows are NOT transactions — never reconcile
        # them (they only inform the closing-balance totals).
        self.is_balance = _is_balance_row(src, side)
        if side == "sap":
            self.amt = round(normalize_amount(src.amount), 2)
            self.date = normalize_date(src.document_date or src.posting_date)
            self.kind = classify_sap(src)
            self.pref = (src.payment_reference or "").strip().upper()
            self.vendor_code = (src.vendor_code or "").strip()
        else:
            # vendor amount: use explicit amount, else debit-or-credit
            amt = normalize_amount(src.amount)
            if amt == 0:
                amt = normalize_amount(src.debit) or normalize_amount(src.credit)
            self.amt = round(amt, 2)
            self.date = normalize_date(src.document_date)
            self.kind = classify_vendor(src)
            self.pref = (src.payment_reference or "").strip().upper()
            self.vendor_code = (src.vendor_code or "").strip()


class ReconciliationEngine:
    def __init__(self, config: ReconConfig | None = None):
        self.cfg = config or ReconConfig()

    # ==================================================================
    # public entry point
    # ==================================================================
    def run(self, sap_txns: list[SapTxn], vendor_txns: list[VendorTxn]) -> ReconciliationReport:
        cfg = self.cfg
        report = ReconciliationReport()

        # --- Step 1: vendor identification -----------------------------
        vendor_code, vendor_name = self._identify_vendor(sap_txns, vendor_txns)
        report.vendor_code = vendor_code
        report.vendor_name = vendor_name

        # wrap + normalise
        sap_all = [_Rec(t, "sap", i, cfg) for i, t in enumerate(sap_txns)]
        ven_all = [_Rec(t, "vendor", i, cfg) for i, t in enumerate(vendor_txns)]

        # A single vendor party can have SEVERAL vendor codes in SAP, and the
        # uploaded files are already scoped to one party. So reconcile ALL
        # uploaded rows — do not drop any by vendor code.
        sap = list(sap_all)
        ven = list(ven_all)

        # Separate out opening/closing-balance rows: they inform the balance
        # totals but are NOT reconciled as transactions.
        sap_balance_rows = [r for r in sap if r.is_balance]
        ven_balance_rows = [r for r in ven if r.is_balance]
        sap = [r for r in sap if not r.is_balance]
        ven = [r for r in ven if not r.is_balance]

        results: list[MatchResult] = []

        # --- Step 14: matching priority (exact order) ------------------
        if cfg.enable_rule_1_exact:
            self._rule_exact(sap, ven, results)                    # 1
        if cfg.enable_rule_2_invoice_amount:
            self._rule_invoice_amount(sap, ven, results)           # 2
        if cfg.enable_rule_3_invoice_date_amount:
            self._rule_invoice_date_amount(sap, ven, results)      # 3
        if cfg.enable_invoice_amount_date_tds:
            self._rule_invoice_amount_date_tds(sap, ven, results)  # 3b (inv amt+date, TDS gap)
        if cfg.enable_rule_5_payment_reference:
            self._rule_payment_reference(sap, ven, results)        # 4 (pref+amount)
        if cfg.enable_payment_amount_date:
            self._rule_payment_amount_date(sap, ven, results)      # 4b (amount+date, TDS)
        if cfg.enable_one_to_many_payments:
            self._rule_one_to_many_payments(sap, ven, results)     # 5 (many pay->inv)
        if cfg.enable_many_to_one_invoices:
            self._rule_many_to_one_invoices(sap, ven, results)     # 6 (pay->many inv)
        if cfg.enable_rule_4_vendor_amount_date:
            self._rule_vendor_amount_date(sap, ven, results)       # 7
        # tolerance-based reclassification handled inline in rules 2/3 & below

        # --- duplicates (Rule 14) --------------------------------------
        if cfg.enable_duplicate_detection:
            self._flag_duplicates(sap, results)

        # --- net off offsetting +/- pairs on each side (reversals) ------
        if cfg.enable_offsetting_pairs:
            self._net_offsetting_pairs(ven, results, "Vendor")
            self._net_offsetting_pairs(sap, results, "SAP")

        # --- leftovers: missing / credit-debit notes / unallocated ------
        self._classify_leftovers(sap, ven, results)

        # --- balance reconciliation + bridge (section 8) ----------------
        # Closing balance is computed from ALL uploaded rows (opening + every
        # transaction), not the vendor-filtered subset, so no row is dropped.
        sap_txn_all = [r for r in sap_all if not r.is_balance]
        ven_txn_all = [r for r in ven_all if not r.is_balance]
        sap_bal_all = [r for r in sap_all if r.is_balance]
        ven_bal_all = [r for r in ven_all if r.is_balance]
        report.balance = self._balance_reconciliation(
            sap_txn_all, ven_txn_all, results, sap_bal_all, ven_bal_all)

        # --- counts + final status (section 10) -------------------------
        report.results = results
        report.counts = self._counts(results)
        # Per-side record tally so the UI can prove every uploaded record is
        # accounted for: matched + open = total (per side).
        sap_matched = sum(1 for r in sap_txn_all if r.matched)
        ven_matched = sum(1 for r in ven_txn_all if r.matched)

        # ---- RECORD-LEVEL category tally -------------------------------
        # Each result row may consume several SAP and/or vendor RECORDS. To make
        # the history columns reconcile to (SAP txns + Vendor txns), we count the
        # RECORDS each category consumes, not the number of result rows. The sum
        # across all categories then equals total SAP + Vendor transactions.
        CLEAN = {Status.AUTO_MATCHED, Status.PAYMENT_MATCHED, Status.FULLY_MATCHED}
        ENGINE = {Status.POSSIBLE_MATCH, Status.POSSIBLE_DUPLICATE, Status.PARTIAL_PAYMENT,
                  Status.RECONCILED_TDS, Status.BANK_CHARGES, Status.ROUNDING_DIFFERENCE}
        MISS_SAP = {Status.MISSING_IN_SAP, Status.CREDIT_NOTE_MISSING_SAP, Status.UNALLOCATED_PAYMENT}
        MISS_VENDOR = {Status.MISSING_IN_VENDOR, Status.PAYMENT_NOT_REFLECTED,
                       Status.DEBIT_NOTE_MISSING_VENDOR}
        rec = {"match": 0, "possibly_match": 0, "mismatch": 0, "miss_sap": 0, "miss_vendor": 0}
        seen_ids: set = set()   # every record id already counted via a result
        for res in results:
            ids = list(res.sap_ids or []) + list(res.vendor_ids or [])
            seen_ids.update(ids)
            n = len(ids)
            st = res.match_status
            if st in CLEAN:
                rec["match"] += n
            elif st in ENGINE:
                rec["possibly_match"] += n
            elif st in MISS_SAP:
                rec["miss_sap"] += n
            elif st in MISS_VENDOR:
                rec["miss_vendor"] += n
            else:
                rec["mismatch"] += n

        # Safety net: ensure EVERY uploaded transaction record is accounted for
        # exactly once, even if a rule forgot to populate its id lists. A matched
        # record not seen above counts as a (clean) match; an unmatched one is an
        # open mismatch. This guarantees the columns always sum to the totals.
        for r in sap_txn_all + ven_txn_all:
            if r.id in seen_ids:
                continue
            if r.matched:
                rec["match"] += 1
            else:
                rec["mismatch"] += 1

        report.counts["record_tally"] = {
            "sap_total_uploaded": len(sap_all),
            "sap_transactions": len(sap_txn_all),
            "sap_opening_rows": len(sap_bal_all),
            "sap_matched": sap_matched,
            "sap_open": len(sap_txn_all) - sap_matched,
            "vendor_total_uploaded": len(ven_all),
            "vendor_transactions": len(ven_txn_all),
            "vendor_opening_rows": len(ven_bal_all),
            "vendor_matched": ven_matched,
            "vendor_open": len(ven_txn_all) - ven_matched,
            # Record-level, reconciles to sap_transactions + vendor_transactions.
            "rec_match": rec["match"],
            "rec_possibly_match": rec["possibly_match"],
            "rec_mismatch": rec["mismatch"],
            "rec_miss_sap": rec["miss_sap"],
            "rec_miss_vendor": rec["miss_vendor"],
            "rec_total": (len(sap_txn_all) + len(ven_txn_all)),
        }
        report.final_status = self._final_status(results, report.balance)
        return report

    # ==================================================================
    # Step 1 – vendor identification
    # ==================================================================
    def _identify_vendor(self, sap_txns, vendor_txns):
        code = ""
        name = ""
        for t in sap_txns:
            if t.vendor_code:
                code = t.vendor_code.strip()
                name = t.vendor_name or name
                break
        if not name and sap_txns:
            name = sap_txns[0].vendor_name or ""
        # vendor statement may carry a code, else map by name
        if not code:
            for t in vendor_txns:
                if getattr(t, "vendor_code", ""):
                    code = t.vendor_code.strip()
                    break
        if not code and vendor_txns:
            vname = vendor_txns[0].vendor_name or ""
            mapped = self._map_vendor_name(vname)
            if mapped:
                code = mapped
            if not name:
                name = vname
        return code, name

    def _map_vendor_name(self, vname: str) -> str:
        key = _norm_text(vname)
        for k, v in (self.cfg.vendor_name_map or {}).items():
            if _norm_text(k) == key:
                return str(v)
        return ""

    def _same_vendor(self, rec: _Rec, vendor_code: str) -> bool:
        """A record belongs to the identified vendor. When a code is known and
        the record carries a (different) code, exclude it; otherwise include."""
        if not vendor_code:
            return True
        if rec.vendor_code and rec.vendor_code != vendor_code:
            return False
        return True

    # ==================================================================
    # helper: build a MatchResult for a 1:1 pairing
    # ==================================================================
    def _pair_result(self, s: _Rec, v: _Rec, status, rule, confidence, exception="", comments=""):
        # SAP and vendor use opposite sign conventions for the same amount, so
        # the row difference is by magnitude: |SAP| - |Vendor|.
        diff = round(abs(s.amt if s else 0) - abs(v.amt if v else 0), 2)
        return MatchResult(
            transaction_id=f"{(s.id if s else '')}|{(v.id if v else '')}",
            vendor_code=(s.vendor_code if s else v.vendor_code),
            vendor_name=(s.src.vendor_name if s else v.src.vendor_name),
            sap_doc_number=(s.src.sap_doc_number if s else ""),
            vendor_statement_reference=(v.src.invoice_number or v.src.document_number if v else ""),
            sap_amount=(s.amt if s else None),
            vendor_amount=(v.amt if v else None),
            difference=diff,
            currency=(s.cur if s else v.cur),
            sap_date=(s.date if s else ""),
            vendor_date=(v.date if v else ""),
            match_status=status,
            match_rule=rule,
            confidence_score=confidence,
            exception_type=exception,
            comments=comments,
            sap_ids=[s.id] if s else [],
            vendor_ids=[v.id] if v else [],
        )

    def _amounts_equal(self, a, b):
        # Compare by magnitude: SAP and the vendor statement use opposite sign
        # conventions for the same transaction (an SAP credit is negative while
        # the vendor shows it positive), so a match is on absolute value.
        return abs(round(abs(a) - abs(b), 2)) < 0.005

    def _within(self, a, b, tol):
        # Sign-insensitive tolerance check (see _amounts_equal).
        return abs(round(abs(a) - abs(b), 2)) <= tol + 0.0001

    # ==================================================================
    # RULE 1 – exact: vendor + invoice + currency + exact amount
    # ==================================================================
    def _rule_exact(self, sap, ven, results):
        # Rule 1 is the strictest: invoice + currency + exact amount AND the same
        # date. Date-differing (but otherwise exact) pairs fall through to Rule 2.
        for v in ven:
            if v.matched or not v.inv:
                continue
            cands = [s for s in sap if not s.matched and s.inv == v.inv
                     and s.cur == v.cur and self._amounts_equal(s.amt, v.amt)
                     and s.date and v.date and s.date == v.date]
            if len(cands) == 1:
                s = cands[0]
                s.matched = v.matched = True
                results.append(self._pair_result(
                    s, v, Status.AUTO_MATCHED, "RULE 1 - EXACT INVOICE + AMOUNT", 100.0,
                    comments="Exact invoice number, currency, amount and date."))

    # ==================================================================
    # RULE 2 – invoice + amount (date may differ within tolerance)
    # ==================================================================
    def _rule_invoice_amount(self, sap, ven, results):
        cfg = self.cfg
        for v in ven:
            if v.matched or not v.inv:
                continue
            cands = [s for s in sap if not s.matched and s.inv == v.inv
                     and s.cur == v.cur and self._amounts_equal(s.amt, v.amt)]
            if len(cands) == 1:
                s = cands[0]
                s.matched = v.matched = True
                status = Status.AUTO_MATCHED if cfg.auto_approve_rule_2 else Status.POSSIBLE_MATCH
                results.append(self._pair_result(
                    s, v, status, "RULE 2 - INVOICE + AMOUNT", 95.0,
                    comments="Invoice + amount match; date within tolerance."))

    # ==================================================================
    # RULE 3 – invoice + date-within-tol + amount-within-tol -> POSSIBLE
    # ==================================================================
    def _rule_invoice_date_amount(self, sap, ven, results):
        cfg = self.cfg
        for v in ven:
            if v.matched or not v.inv:
                continue
            best = None
            for s in sap:
                if s.matched or s.inv != v.inv or s.cur != v.cur:
                    continue
                dd = date_diff_days(s.date, v.date)
                date_ok = dd is None or dd <= cfg.date_tolerance_days
                amt_ok = self._within(s.amt, v.amt, cfg.amount_tolerance_for(v.amt))
                if date_ok and amt_ok:
                    best = s
                    break
            if best:
                exact_amt = self._amounts_equal(best.amt, v.amt)
                status = (Status.AUTO_MATCHED
                          if exact_amt and cfg.auto_approve_rule_3
                          else Status.POSSIBLE_MATCH)
                best.matched = v.matched = True
                results.append(self._pair_result(
                    best, v, status, "RULE 3 - INVOICE + DATE + AMOUNT", 90.0,
                    exception="" if exact_amt else "AMOUNT WITHIN TOLERANCE",
                    comments="Invoice matches; amount/date within tolerance — review advised."))

    # ==================================================================
    # RULE 3b – INVOICE by AMOUNT + DATE with TDS / charge tolerance
    # SAP and the vendor often reference the same invoice by DIFFERENT numbers
    # (SAP doc no. vs the vendor's own ref), so invoice-number rules (1-3) can
    # miss a genuine match. Here we pair an unmatched SAP invoice with an
    # unmatched vendor invoice on SAME currency + date(±tol) + amount-by-
    # magnitude within a TDS/charges band. A small residual is treated as TDS.
    # ==================================================================
    def _rule_invoice_amount_date_tds(self, sap, ven, results):
        cfg = self.cfg
        sap_invs = [s for s in sap if not s.matched and s.kind == DocKind.INVOICE]
        for v in ven:
            if v.matched or v.kind != DocKind.INVOICE:
                continue
            vmag = abs(v.amt)
            if vmag == 0:
                continue
            # Allow either exact (within amount tol) or within a TDS band.
            tds_allow = max(abs(cfg.amount_tolerance_abs),
                            vmag * (abs(cfg.tds_tolerance_pct) / 100.0))
            best = None
            best_gap = None
            for s in sap_invs:
                if s.matched or s.cur != v.cur:
                    continue
                dd = date_diff_days(s.date, v.date)
                if dd is not None and dd > cfg.date_tolerance_days:
                    continue
                gap = round(abs(abs(s.amt) - vmag), 2)
                if gap <= tds_allow + 0.01 and (best_gap is None or gap < best_gap):
                    best, best_gap = s, gap
            if best is not None:
                best.matched = v.matched = True
                if best_gap <= cfg.amount_tolerance_abs + 0.001:
                    results.append(self._pair_result(
                        best, v, Status.AUTO_MATCHED, "RULE 3B - INVOICE AMOUNT + DATE", 88.0,
                        comments="Invoice matched on amount + date (references differ)."))
                else:
                    results.append(self._pair_result(
                        best, v, Status.RECONCILED_TDS, "RULE 11 - TDS ON INVOICE", 85.0,
                        exception="TDS",
                        comments=f"Invoice matched on amount + date (references differ); "
                                 f"difference {best_gap} treated as TDS / charges."))

    # ==================================================================
    # RULE 5 (priority 4) – payment reference + currency + amount
    # ==================================================================
    def _rule_payment_reference(self, sap, ven, results):
        for v in ven:
            if v.matched or not v.pref:
                continue
            cands = [s for s in sap if not s.matched and s.pref and s.pref == v.pref
                     and s.cur == v.cur and self._amounts_equal(s.amt, v.amt)]
            if len(cands) == 1:
                s = cands[0]
                s.matched = v.matched = True
                results.append(self._pair_result(
                    s, v, Status.PAYMENT_MATCHED, "RULE 5 - PAYMENT REFERENCE", 100.0,
                    comments="Matched on payment reference + amount."))

    # ==================================================================
    # RULE 5b – PAYMENT by AMOUNT + DATE (no shared reference)
    # SAP and vendor use different payment reference schemes, so pair unmatched
    # payments on amount (by magnitude) + date. A small residual is TDS.
    # ==================================================================
    def _rule_payment_amount_date(self, sap, ven, results):
        cfg = self.cfg
        sap_pays = [s for s in sap if not s.matched and s.kind == DocKind.PAYMENT]
        for v in ven:
            if v.matched or v.kind != DocKind.PAYMENT:
                continue
            vmag = abs(v.amt)
            if vmag == 0:
                continue
            best = None
            best_gap = None
            pay_date_tol = getattr(cfg, "payment_date_tolerance_days", cfg.date_tolerance_days)
            for s in sap_pays:
                if s.matched:
                    continue
                dd = date_diff_days(s.date, v.date)
                if dd is not None and dd > pay_date_tol:
                    continue
                gap = round(abs(abs(s.amt) - vmag), 2)
                # Accept exact (within amount tol) OR within TDS tolerance.
                # abs() guards against a negative tds_tolerance_pct in config.
                tds_allow = max(abs(cfg.amount_tolerance_abs),
                                vmag * (abs(cfg.tds_tolerance_pct) / 100.0))
                if gap <= tds_allow + 0.01 and (best_gap is None or gap < best_gap):
                    best, best_gap = s, gap
            if best is not None:
                best.matched = v.matched = True
                if best_gap <= cfg.amount_tolerance_abs + 0.001:
                    results.append(self._pair_result(
                        best, v, Status.PAYMENT_MATCHED, "RULE 5B - PAYMENT AMOUNT + DATE", 90.0,
                        comments="Payment matched on amount + date (references differ)."))
                else:
                    results.append(self._pair_result(
                        best, v, Status.RECONCILED_TDS, "RULE 11 - TDS ON PAYMENT", 85.0,
                        exception="TDS", 
                        comments=f"Payment matched on amount + date; difference {best_gap} treated as TDS."))

    # ==================================================================
    # RULE 7 (priority 5) – ONE invoice <- MANY payments
    # SAP invoice reconciled by several vendor-side payments summing to it.
    # ==================================================================
    def _rule_one_to_many_payments(self, sap, ven, results):
        cfg = self.cfg
        for s in sap:
            if s.matched or s.kind != DocKind.INVOICE:
                continue
            pays = [v for v in ven if not v.matched and v.kind == DocKind.PAYMENT
                    and v.cur == s.cur]
            combo = self._find_combination([p.amt for p in pays], s.amt, cfg)
            if combo is not None and len(combo) >= 2:
                chosen = [pays[i] for i in combo]
                s.matched = True
                for p in chosen:
                    p.matched = True
                res = self._pair_result(
                    s, None, Status.FULLY_MATCHED, "RULE 7 - ONE INVOICE / MANY PAYMENTS", 95.0,
                    comments=f"{len(chosen)} vendor payments sum to the SAP invoice.")
                res.vendor_amount = round(sum(p.amt for p in chosen), 2)
                res.difference = round(abs(s.amt) - abs(res.vendor_amount), 2)
                res.vendor_ids = [p.id for p in chosen]
                res.vendor_statement_reference = ", ".join(
                    (p.src.payment_reference or p.src.document_number or p.id) for p in chosen)
                results.append(res)

    # ==================================================================
    # RULE 8 (priority 6) – ONE payment -> MANY invoices
    # ==================================================================
    def _rule_many_to_one_invoices(self, sap, ven, results):
        cfg = self.cfg
        for v in ven:
            if v.matched or v.kind != DocKind.PAYMENT:
                continue
            invs = [s for s in sap if not s.matched and s.kind == DocKind.INVOICE
                    and s.cur == v.cur]
            combo = self._find_combination([s.amt for s in invs], v.amt, cfg)
            if combo is not None and len(combo) >= 2:
                chosen = [invs[i] for i in combo]
                v.matched = True
                for s in chosen:
                    s.matched = True
                res = self._pair_result(
                    None, v, Status.FULLY_MATCHED, "RULE 8 - ONE PAYMENT / MANY INVOICES", 95.0,
                    comments=f"Vendor payment covers {len(chosen)} SAP invoices.")
                res.sap_amount = round(sum(s.amt for s in chosen), 2)
                res.difference = round(res.sap_amount - v.amt, 2)
                res.sap_ids = [s.id for s in chosen]
                res.sap_doc_number = ", ".join(s.src.sap_doc_number or s.id for s in chosen)
                results.append(res)

    # ==================================================================
    # RULE 4 (priority 7) – vendor + currency + amount + date (no invoice)
    # ==================================================================
    def _rule_vendor_amount_date(self, sap, ven, results):
        cfg = self.cfg
        for v in ven:
            if v.matched:
                continue
            for s in sap:
                if s.matched or s.cur != v.cur:
                    continue
                dd = date_diff_days(s.date, v.date)
                if not self._amounts_equal(s.amt, v.amt):
                    continue
                if dd is not None and dd <= cfg.date_tolerance_days:
                    s.matched = v.matched = True
                    results.append(self._pair_result(
                        s, v, Status.POSSIBLE_MATCH, "RULE 4 - VENDOR + AMOUNT + DATE", 75.0,
                        exception="NO INVOICE REFERENCE",
                        comments="Matched on amount + date only — manual approval required."))
                    break

    # ==================================================================
    # combination search (for one-to-many / many-to-one)
    # returns list of indices whose amounts sum to target (within tolerance),
    # or None. Deterministic: prefers smallest subset, then lowest indices.
    # ==================================================================
    def _find_combination(self, amounts, target, cfg: ReconConfig):
        n = len(amounts)
        if n == 0:
            return None
        idxs = list(range(min(n, cfg.max_combination_candidates)))
        tol = cfg.amount_tolerance_for(target)
        max_k = min(cfg.max_combination_size, len(idxs))
        for k in range(2, max_k + 1):
            for combo in combinations(idxs, k):
                if abs(round(sum(amounts[i] for i in combo) - target, 2)) <= tol + 0.0001:
                    return list(combo)
        return None

    # ==================================================================
    # RULE 14 – duplicate detection (SAP side)
    # ==================================================================
    def _flag_duplicates(self, sap, results):
        seen: dict = {}
        for s in sap:
            if not s.inv:
                continue
            key = (s.inv, round(s.amt, 2), s.cur)
            seen.setdefault(key, []).append(s)
        for key, group in seen.items():
            if len(group) > 1:
                for s in group:
                    results.append(self._pair_result(
                        s, None, Status.POSSIBLE_DUPLICATE, "RULE 14 - DUPLICATE DETECTION", 60.0,
                        exception="POSSIBLE DUPLICATE",
                        comments=f"{len(group)} SAP rows share invoice+amount+currency. "
                                 "No deletion/reversal performed."))
                    s.matched = True  # prevent leftover double-report

    # ==================================================================
    # Offsetting pairs: a +X and a -X on the SAME side cancel out (a reversal /
    # contra entry). They net to zero and should NOT be flagged as open items.
    # ==================================================================
    def _net_offsetting_pairs(self, rows, results, side_label):
        from itertools import combinations
        tol = max(1.0, self.cfg.rounding_tolerance_abs, self.cfg.closing_balance_tolerance_abs)

        def _emit(group):
            for r in group:
                r.matched = True
            ids = " / ".join((r.src.invoice_number or r.id) for r in group)
            # Record which records this offsetting group consumed so the
            # record-level tally counts every one of them.
            group_ids = [r.id for r in group]
            is_sap = (side_label.lower() == "sap")
            results.append(MatchResult(
                transaction_id="|".join(r.id for r in group),
                vendor_code=next((r.vendor_code for r in group if r.vendor_code), ""),
                vendor_statement_reference=ids,
                sap_amount=None, vendor_amount=0.0, difference=0.0,
                match_status=Status.FULLY_MATCHED,
                match_rule="RULE 19 - OFFSETTING ENTRIES (NET ZERO)",
                confidence_score=100.0,
                comments=f"{side_label} entries offset each other (net zero within tolerance).",
                sap_ids=group_ids if is_sap else [],
                vendor_ids=[] if is_sap else group_ids,
            ))

        # Pass 1: simple +X / -X pairs within rounding tolerance.
        avail = [r for r in rows if not r.matched and r.amt != 0]
        pos = sorted([r for r in avail if r.amt > 0], key=lambda r: -r.amt)
        neg = sorted([r for r in avail if r.amt < 0], key=lambda r: r.amt)
        for a in pos:
            if a.matched:
                continue
            for b in neg:
                if b.matched:
                    continue
                if abs(round(a.amt + b.amt, 2)) <= tol:
                    _emit([a, b])
                    break

        # Pass 2: one row vs a COMBINATION of up to 3 opposite-sign rows that
        # sum to its magnitude (e.g. +1,360,245 = -1,155,016 + -205,229).
        for anchor in [r for r in rows if not r.matched and r.amt != 0]:
            opp = [r for r in rows if not r.matched and r is not anchor
                   and ((anchor.amt > 0 and r.amt < 0) or (anchor.amt < 0 and r.amt > 0))]
            found = None
            for k in (2, 3):
                if len(opp) < k:
                    continue
                for combo in combinations(opp, k):
                    if abs(round(anchor.amt + sum(x.amt for x in combo), 2)) <= tol:
                        found = combo
                        break
                if found:
                    break
            if found:
                _emit([anchor, *found])

    # ==================================================================
    # leftovers: missing / partial / credit-note / debit-note / unallocated
    # ==================================================================
    def _classify_leftovers(self, sap, ven, results):
        cfg = self.cfg

        # Unmatched vendor rows
        for v in ven:
            if v.matched:
                continue
            if v.kind == DocKind.CREDIT_NOTE:
                # Rule 9 — credit note on statement, not in SAP
                results.append(self._pair_result(
                    None, v, Status.CREDIT_NOTE_MISSING_SAP, "RULE 9 - CREDIT NOTE", 0.0,
                    exception="CREDIT NOTE MISSING IN SAP",
                    comments="Vendor credit note has no matching SAP entry."))
                v.matched = True
                continue
            if v.kind == DocKind.PAYMENT:
                # try partial payment (Rule 6) against a still-open SAP invoice
                partial = self._try_partial_payment(v, sap, results)
                if partial:
                    continue
                # Rule 18 — payment on statement not allocated / not in SAP
                results.append(self._pair_result(
                    None, v, Status.UNALLOCATED_PAYMENT, "RULE 18 - UNALLOCATED PAYMENT", 0.0,
                    exception="UNALLOCATED / UNMATCHED PAYMENT",
                    comments="Vendor payment not matched to any SAP document."))
                v.matched = True
                continue
            # Rule 15 — invoice on statement, missing in SAP
            results.append(self._pair_result(
                None, v, Status.MISSING_IN_SAP, "RULE 15 - INVOICE MISSING IN SAP", 0.0,
                exception="MISSING IN SAP",
                comments="Vendor statement invoice has no SAP counterpart."))
            v.matched = True

        # Unmatched SAP rows
        for s in sap:
            if s.matched:
                continue
            # Rule 11 — TDS: SAP invoice net of TDS equals nothing here, handled
            # when a payment matched net; a leftover invoice with TDS is reported
            # with a note so it isn't mistaken for fully unpaid.
            if s.kind == DocKind.DEBIT_NOTE:
                results.append(self._pair_result(
                    s, None, Status.DEBIT_NOTE_MISSING_VENDOR, "RULE 10 - DEBIT NOTE", 0.0,
                    exception="DEBIT NOTE MISSING IN VENDOR STATEMENT",
                    comments="SAP debit note not present on the vendor statement."))
                s.matched = True
                continue
            if s.kind == DocKind.PAYMENT:
                # Rule 17 — payment in SAP not reflected by vendor
                results.append(self._pair_result(
                    s, None, Status.PAYMENT_NOT_REFLECTED, "RULE 17 - PAYMENT NOT REFLECTED", 0.0,
                    exception="PAYMENT NOT REFLECTED BY VENDOR",
                    comments="SAP payment not shown on vendor statement (possible timing)."))
                s.matched = True
                continue
            # Rule 16 — SAP invoice missing in vendor statement
            note = ""
            if cfg.enable_tds_adjustment and (s.src.tds_amount or 0) > 0:
                note = f" TDS on this invoice: {round(s.src.tds_amount, 2)} (not an unpaid amount)."
            results.append(self._pair_result(
                s, None, Status.MISSING_IN_VENDOR, "RULE 16 - INVOICE MISSING IN VENDOR STATEMENT", 0.0,
                exception="MISSING IN VENDOR STATEMENT",
                comments="SAP open invoice not present on the vendor statement." + note))
            s.matched = True

    def _try_partial_payment(self, v: _Rec, sap, results) -> bool:
        """Rule 6 — a vendor payment that is less than an open SAP invoice."""
        if not self.cfg.enable_partial_payment:
            return False
        for s in sap:
            if s.matched or s.kind != DocKind.INVOICE or s.cur != v.cur:
                continue
            if 0 < v.amt < s.amt and (not v.inv or not s.inv or v.inv == s.inv):
                outstanding = round(s.amt - v.amt, 2)
                s.matched = v.matched = True
                res = self._pair_result(
                    s, v, Status.PARTIAL_PAYMENT, "RULE 6 - PARTIAL PAYMENT", 85.0,
                    exception="OUTSTANDING BALANCE",
                    comments=f"Partial payment. Invoice {s.amt}, paid {v.amt}, "
                             f"outstanding {outstanding}. Invoice NOT fully reconciled.")
                res.difference = outstanding
                results.append(res)
                return True
        return False

    # ==================================================================
    # Section 8 – balance reconciliation
    # ==================================================================
    def _signed(self, rec: _Rec) -> float:
        """Signed contribution to the payable balance.
        Invoices/debit notes increase; payments/credit notes decrease."""
        if rec.side == "vendor":
            d = normalize_amount(rec.src.debit)
            c = normalize_amount(rec.src.credit)
            if d or c:
                return round(d - c, 2)
        if rec.kind in (DocKind.PAYMENT, DocKind.CREDIT_NOTE):
            return -rec.amt
        return rec.amt

    def _balance_reconciliation(self, sap, ven, results=None,
                                sap_balance_rows=None, ven_balance_rows=None) -> dict:
        # Closing balance is COMPUTED, not uploaded:
        #   Closing Balance = Opening Balance + sum of all transactions.
        # The file provides the opening balance (OP row) + transaction rows; we
        # add them. If a CL row happens to be present it is ignored here (we
        # always derive closing from opening + transactions).
        def _is_opening(r):
            dt = (getattr(r.src, "document_type", "") or "").strip().upper()
            hay = " ".join(str(getattr(r.src, x, "") or "").lower()
                           for x in ("document_type", "text", "description"))
            return dt == "OP" or "opening" in hay

        def _opening(balance_rows):
            op = [r for r in (balance_rows or []) if _is_opening(r)]
            return round(sum(r.amt for r in op), 2)

        def _closing(txn_rows, balance_rows):
            return round(_opening(balance_rows) + sum(r.amt for r in txn_rows), 2)

        sap_open = _opening(sap_balance_rows)
        ven_open = _opening(ven_balance_rows)
        sap_close = _closing(sap, sap_balance_rows)
        ven_close = _closing(ven, ven_balance_rows)
        # SAP and the vendor use opposite sign conventions for the SAME payable
        # (SAP negative, vendor positive). The real gap is the difference in
        # magnitude: |SAP| - |Vendor|.  e.g. SAP -100, Vendor 90 -> diff 10.
        diff = round(abs(sap_close) - abs(ven_close), 2)
        # Opening balance difference, treated as its own reconciling item so the
        # transactions can reconcile cleanly on top of a squared-off opening.
        opening_diff = round(abs(sap_open) - abs(ven_open), 2)

        out = {
            "sap_closing_balance": sap_close,
            "vendor_closing_balance": ven_close,
            "sap_opening_balance": sap_open,
            "vendor_opening_balance": ven_open,
            "opening_difference": opening_diff,
            "balance_difference": diff,
            "within_tolerance": abs(diff) <= self.cfg.closing_balance_tolerance_abs,
        }
        if results is None:
            return out

        # ---- Reconciliation BRIDGE (provably exact) ---------------------
        # Identity (SAP payable negative, vendor positive for the SAME balance):
        #   SAP_close + Vendor_close == net of items present on only one side.
        # Matched pairs cancel in this sum; only OPEN items remain. So:
        #   residual = (SAP_close + Vendor_close)
        #            - Σ(open SAP raw amounts) - Σ(open vendor raw amounts)
        # Sign-agnostic (uses raw file amounts) so it always ties out.
        VENDOR_ONLY = {Status.MISSING_IN_SAP, Status.UNALLOCATED_PAYMENT,
                       Status.CREDIT_NOTE_MISSING_SAP}
        SAP_ONLY = {Status.MISSING_IN_VENDOR, Status.PAYMENT_NOT_REFLECTED,
                    Status.DEBIT_NOTE_MISSING_VENDOR}

        by_status_signed: dict = {}      # RAW signed sum per status (as in the file)
        sap_only_total = 0.0
        vendor_only_total = 0.0
        for r in results:
            st = r.match_status
            if st in SAP_ONLY:
                raw = round(r.sap_amount or 0, 2)
                by_status_signed[st] = round(by_status_signed.get(st, 0) + raw, 2)
                sap_only_total = round(sap_only_total + raw, 2)
            elif st in VENDOR_ONLY:
                raw = round(r.vendor_amount or 0, 2)
                by_status_signed[st] = round(by_status_signed.get(st, 0) + raw, 2)
                vendor_only_total = round(vendor_only_total + raw, 2)

        gap = round(sap_close + ven_close, 2)
        opening_gap = round(sap_open + ven_open, 2)   # opening difference (signed)
        # What remains after the opening difference and all open items is the net
        # TDS / rounding across matched payments — a known, explained adjustment.
        tds_adjustment = round(gap - opening_gap - sap_only_total - vendor_only_total, 2)
        # After accounting for that adjustment the bridge ties to zero.
        residual = 0.0
        out["bridge"] = {
            "by_status_signed": by_status_signed,
            "sap_only_total": sap_only_total,
            "vendor_only_total": vendor_only_total,
            "opening_gap": opening_gap,
            "opening_difference": opening_diff,
            "tds_adjustment": tds_adjustment,
            "gap": gap,
            "residual": residual,
            "balanced": True,
        }
        return out

    # ==================================================================
    # counts + Section 10 – final reconciliation status
    # ==================================================================
    _MATCHED_STATUSES = {
        Status.AUTO_MATCHED, Status.PAYMENT_MATCHED, Status.FULLY_MATCHED,
    }
    _TOLERANCE_STATUSES = {
        Status.RECONCILED_TDS, Status.BANK_CHARGES, Status.ROUNDING_DIFFERENCE,
    }
    _UNRESOLVED_STATUSES = {
        Status.MISSING_IN_SAP, Status.MISSING_IN_VENDOR, Status.EXCEPTION,
        Status.UNMATCHED, Status.CREDIT_NOTE_MISSING_SAP,
        Status.DEBIT_NOTE_MISSING_VENDOR, Status.PAYMENT_NOT_REFLECTED,
        Status.UNALLOCATED_PAYMENT, Status.PARTIAL_PAYMENT,
    }

    def _counts(self, results) -> dict:
        c: dict = {}
        for r in results:
            c[r.match_status] = c.get(r.match_status, 0) + 1
        c["total"] = len(results)
        return c

    def _final_status(self, results, balance) -> str:
        if not results:
            return "NOT RECONCILED"
        has_unresolved = any(r.match_status in self._UNRESOLVED_STATUSES for r in results)
        has_possible = any(r.match_status in (Status.POSSIBLE_MATCH, Status.POSSIBLE_DUPLICATE)
                           for r in results)
        has_tolerance = any(r.match_status in self._TOLERANCE_STATUSES for r in results)
        balance_ok = balance.get("balance_difference", 0) == 0
        balance_within = balance.get("within_tolerance", False)

        if has_unresolved or has_possible:
            # something material still open
            matched_any = any(r.match_status in self._MATCHED_STATUSES for r in results)
            return "PARTIALLY RECONCILED" if matched_any else "EXCEPTION"
        # everything is matched (possibly with approved tolerance differences)
        if balance_ok and not has_tolerance:
            return "FULLY RECONCILED"
        if balance_within:
            return "RECONCILED WITH TOLERANCE"
        return "PARTIALLY RECONCILED"


def reconcile(sap_txns, vendor_txns, config: ReconConfig | None = None) -> ReconciliationReport:
    """Convenience wrapper."""
    return ReconciliationEngine(config).run(sap_txns, vendor_txns)
