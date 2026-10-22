"""Configuration for the reconciliation engine.

Every tolerance, threshold and rule toggle lives here — nothing is hard-coded in
the engine. An admin can build a ReconConfig (or load one from JSON) and pass it
in. `from_dict` / `to_dict` make it easy to persist/edit via an admin screen.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class ReconConfig:
    # ---- normalisation --------------------------------------------------
    strip_leading_zeros: bool = True          # INV00125 vs INV0125
    invoice_strip_chars: str = " -/\\.#,"     # removed from invoice numbers
    # Configurable vendor-name -> vendor-code mapping (used when the statement
    # has no vendor code). Keys are compared case-insensitively/normalised.
    vendor_name_map: dict = field(default_factory=dict)

    # ---- tolerances -----------------------------------------------------
    date_tolerance_days: int = 3              # +/- calendar days
    amount_tolerance_abs: float = 1.0         # Rule 3 absolute amount tolerance
    amount_tolerance_pct: float = 0.0         # optional % tolerance (0 = off)
    rounding_tolerance_abs: float = 0.05      # Rule 13
    bank_charge_tolerance_abs: float = 0.0    # Rule 12 absolute (currency units)
    bank_charge_tolerance_pct: float = 0.1    # Rule 12 as % of amount (0.1 = 0.1%)
    forex_tolerance_pct: float = 0.5          # acceptable forex diff on payments
    gst_tolerance_pct: float = 18.0           # expected GST rate (% of taxable value)
    tds_tolerance_pct: float = 10.0           # expected TDS rate (% deducted at source)

    # ---- confidence thresholds -----------------------------------------
    auto_match_min_confidence: float = 95.0   # >= this may be AUTO MATCHED
    review_min_confidence: float = 70.0       # below this -> not a suggested match

    # ---- per-rule toggles ----------------------------------------------
    enable_rule_1_exact: bool = True
    enable_rule_2_invoice_amount: bool = True
    enable_rule_3_invoice_date_amount: bool = True
    enable_invoice_amount_date_tds: bool = True  # Rule 3b: invoices by amount+date (TDS/charge gap, refs differ)
    enable_rule_4_vendor_amount_date: bool = True
    enable_rule_5_payment_reference: bool = True
    enable_payment_amount_date: bool = True    # Rule 5b: payments by amount+date (TDS gap)
    payment_date_tolerance_days: int = 15      # wider window for payment timing differences
    enable_one_to_many_payments: bool = True   # Rule 7: many payments -> one invoice
    enable_many_to_one_invoices: bool = True   # Rule 8: one payment -> many invoices
    enable_partial_payment: bool = True        # Rule 6
    enable_tds_adjustment: bool = True         # Rule 11
    enable_bank_charges: bool = True           # Rule 12
    enable_rounding: bool = True               # Rule 13
    enable_duplicate_detection: bool = True    # Rule 14
    enable_offsetting_pairs: bool = True       # Rule 19: +X and -X on same side net to zero

    # Which rules are allowed to AUTO-approve (vs. flag for manual review).
    auto_approve_rule_2: bool = True
    auto_approve_rule_3: bool = False          # spec: send to review if amount differs
    auto_approve_rule_4: bool = False          # spec: never auto-approve vendor+amt+date

    # ---- combination search limits (perf guard for many-to-one/one-to-many)
    max_combination_size: int = 6             # max items summed in a combination
    max_combination_candidates: int = 40      # cap candidate pool before combining

    # ---- final-status balance tolerance --------------------------------
    closing_balance_tolerance_abs: float = 1.0

    def amount_tolerance_for(self, amount: float) -> float:
        """Effective amount tolerance for Rule 3, combining abs + pct."""
        pct = abs(amount) * (self.amount_tolerance_pct / 100.0)
        return max(self.amount_tolerance_abs, pct)

    def bank_charge_tolerance_for(self, amount: float) -> float:
        pct = abs(amount) * (self.bank_charge_tolerance_pct / 100.0)
        return max(self.bank_charge_tolerance_abs, pct)

    def expected_gst_for(self, taxable_value: float) -> float:
        """Expected GST amount for a taxable value at the configured rate."""
        return abs(taxable_value) * (self.gst_tolerance_pct / 100.0)

    def expected_tds_for(self, amount: float) -> float:
        """Expected TDS amount deducted from a payment at the configured rate."""
        return abs(amount) * (self.tds_tolerance_pct / 100.0)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ReconConfig":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})
