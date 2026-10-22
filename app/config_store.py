"""Persist the reconciliation-engine ReconConfig in SQLite (single-row table).

Admins edit tolerances/toggles via the Settings tab; the engine reads the
current config on each run. Falls back to ReconConfig defaults if unset.
"""
from __future__ import annotations

import json
import os
import sqlite3

from .recon_engine.config import ReconConfig

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "history.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_config_db() -> None:
    with _conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS engine_config (id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL)"
        )


def get_config() -> ReconConfig:
    with _conn() as conn:
        row = conn.execute("SELECT payload FROM engine_config WHERE id = 1").fetchone()
    if not row:
        return ReconConfig()
    try:
        return ReconConfig.from_dict(json.loads(row["payload"]))
    except (json.JSONDecodeError, TypeError):
        return ReconConfig()


def save_config(data: dict) -> ReconConfig:
    """Merge incoming values onto the current config and persist. Returns the
    effective config (so unknown keys are ignored and defaults fill gaps)."""
    current = get_config().to_dict()
    current.update({k: v for k, v in (data or {}).items() if k in current})
    cfg = ReconConfig.from_dict(current)
    with _conn() as conn:
        conn.execute(
            "INSERT INTO engine_config (id, payload) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
            (json.dumps(cfg.to_dict()),),
        )
    return cfg
