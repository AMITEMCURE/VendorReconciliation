"""Vendor Reconciliation Engine.

A standalone, deterministic, configurable and explainable engine that compares
a Vendor Statement against a SAP Vendor Ledger and classifies every transaction
(Matched / Partially Matched / Possible Match / Exception / Missing / Duplicate).

It NEVER mutates the source data and NEVER posts / clears / reverses anything —
it only identifies and classifies differences.
"""
from .config import ReconConfig
from .models import SapTxn, VendorTxn, MatchResult, ReconciliationReport
from .engine import ReconciliationEngine, reconcile

__all__ = [
    "ReconConfig",
    "SapTxn",
    "VendorTxn",
    "MatchResult",
    "ReconciliationReport",
    "ReconciliationEngine",
    "reconcile",
]
