"""Frozen cross-workstream contracts for `rare-kaon-ops-demo`.

This module is the single source of truth shared by:
- Wave 1 · A (synthetic data generator & `seed/`)
- Wave 1 · C (PDF / HTML / XLSX renderers)
- Wave 1 · D (`SKILL.md`, pre-sales docs & quickstart card)
- Wave 2 · B (engine, workspace, parsers & CLI)
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any

from engine import __version__

# ---------------------------------------------------------------------------
# Demo company identity & workspace constants
# ---------------------------------------------------------------------------

COMPANY_NAME = "Heartland Mechanical Services, LLC"
DEFAULT_WEEKLY_PERIOD = "2026-11-20"
PRIOR_WEEKLY_PERIOD = "2026-11-13"
DEFAULT_PAYROLL_PERIOD = "2026-11-15"

WORKSPACE_DIR_NAME = "heartland-demo"
WORKSPACE_MARKER = ".rare-kaon-demo-workspace.json"
FALLBACK_HOME_SUBDIR = ".rare-kaon-ops-demo"
SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# CLI commands & explain topics (frozen interface for SKILL.md & engine/cli.py)
# ---------------------------------------------------------------------------

CLI_COMMANDS = (
    "init",
    "reset",
    "overview",
    "readiness",
    "explain",
    "what-if",
    "ingest",
    "change",
    "prepare",
    "approve",
    "post",
    "config",
)

EXPLAIN_TOPICS = (
    "cash_bridge",
    "ar_over_60",
    "payroll",
    "guardrails",
    "architecture",
    "transfer",
)

# ---------------------------------------------------------------------------
# Canonical seed/ relative file paths (frozen between Workstream A & B)
# ---------------------------------------------------------------------------

SEED_PATHS = {
    "feeds_config": "config/feeds.yaml",
    "assumptions_config": "config/assumptions.yaml",
    "outputs_config": "config/outputs.yaml",
    "payroll_mappings": "config/payroll-mappings.yaml",
    "frost_operating": "sources/frost-operating-2026-11-20.csv",
    "frost_payroll_sweep": "sources/frost-payroll-sweep-2026-11-20.csv",
    "ar_aging": "sources/ar-aging-2026-11-18.xlsx",
    "ap_scheduled": "sources/ap-scheduled-payments-2026-11-20.csv",
    "payroll_voucher": "sources/payroll-voucher-2026-11-15.csv",
    "payroll_funding_wire": "sources/payroll-funding-wire-2026-11-15.yaml",
    "prior_issued_weekly": "history/issued-2026-11-13-model.json",
    "incoming_amendment_yaml": "incoming/lone-star-payment-amendment-2026-11-20.yaml",
    "incoming_amendment_txt": "incoming/lone-star-amendment-email.txt",
}

# ---------------------------------------------------------------------------
# Canonical KPI keys (frozen between Workstream A history, B engine, C renderers)
# ---------------------------------------------------------------------------

WEEKLY_KPI_KEYS = (
    "opening_cash",
    "week1_ending_cash",
    "week13_ending_cash",
    "min_cash_balance",
    "ar_total",
    "ar_over_60",
)

PAYROLL_KPI_KEYS = (
    "gross_wages",
    "employer_cost_total",
    "funding_wire_total",
    "tieout_variance",
    "mapped_employees",
    "blocking_exceptions",
)

# ---------------------------------------------------------------------------
# Canonical `ReportModel.sections` shapes (documented contract for A, B, C)
# ---------------------------------------------------------------------------
#
# For module == "weekly", `model.sections` contains:
# {
#   "cash_accounts": [
#       {"account_id": str, "name": str, "bank": str, "mask": str,
#        "closing_balance": float, "as_of": str, "source_file": str},
#   ],
#   "forecast_weeks": [
#       {"week_num": int (1..13), "week_ending": str (YYYY-MM-DD),
#        "opening_cash": float, "inflows": float, "outflows": float,
#        "net_cash": float, "ending_cash": float, "notes": str},
#   ],
#   "ar_aging": {
#       "as_of": str, "source_file": str, "total": float,
#       "buckets": {"current": float, "d1_30": float, "d31_60": float,
#                   "d61_90": float, "over_90": float},
#       "overdue_items": [
#           {"invoice_id": str, "customer": str, "branch": str,
#            "invoice_date": str, "due_date": str, "days_past_due": int,
#            "bucket": "61-90" | ">90", "amount": float, "note": str},
#       ],
#   },
#   "scheduled_payments": [
#       {"invoice_ref": str, "vendor": str, "branch": str, "due_date": str,
#        "forecast_week": int, "amount": float, "paid": bool,
#        "status": str, "note": str},
#   ],
#   "revisions": [
#       {"change_id": str, "invoice_ref": str, "vendor": str, "amount": float,
#        "old_due_date": str, "new_due_date": str, "old_week": int,
#        "new_week": int, "confirmed_by": str, "confirmed_at": str,
#        "source_file": str},
#   ],
# }
#
# For module == "payroll", `model.sections` contains:
# {
#   "summary": {
#       "pay_date": str, "pay_period": str, "employee_count": int,
#       "gross_wages": float, "employer_taxes": float, "employer_benefits": float,
#       "employee_withholdings": float, "net_pay": float,
#       "total_funding_wire": float, "variance_cents": int,
#       "status": "blocked" | "ready_with_warnings" | "ready",
#       "erp_mode": "sandbox" | "production",
#       "mapping_approval_ref": str | None,
#   },
#   "tieout": [
#       {"component": str, "register_amount": float, "funding_amount": float,
#        "difference": float, "status": "pass" | "fail"},
#   ],
#   "journal_lines": [
#       {"line_no": int, "account": str, "account_name": str, "department": str,
#        "branch": str, "debit": float, "credit": float, "memo": str,
#        "blocked_reason": str},
#   ],
#   "exceptions": [
#       {"code": str, "employee_id": str, "employee_name": str, "branch": str,
#        "dept_code": str, "severity": "blocking" | "open_review",
#        "detail": str, "remediation": str},
#   ],
# }

# ---------------------------------------------------------------------------
# Zero-leakage hygiene guard (no real client, prospect, or host paths allowed)
# ---------------------------------------------------------------------------

FORBIDDEN_PATH_PATTERNS = (
    "/users/",
    "/home/",
    "c:\\users\\",
)

_HYGIENE_SALT = "rk-ops-demo-hygiene-v1:"
_FORBIDDEN_TOKEN_HASHES = frozenset(
    {
        "d9731169afa0ce06",
        "c084ef237957f589",
        "2743f623c1a2dd33",
        "caa06f5b87da3fbb",
        "5985e43d8ba04e86",
        "f5c78c53a0631b0f",
        "4a98a33a1380b090",
        "652d6983833f7300",
        "d7f1da178db4a9e4",
        "3270bc3e58c6930d",
        "a61965be91ab4d70",
        "d3283a977d1ff571",
        "52922777350cbfae",
        "69eb36b8623cbc55",
        "0b6a880439d48547",
        "896dae406773d18c",
        "18be7aa254e56b16",
        "80774db494a17728",
        "716ee803d53559be",
        "ef76be4b6779f7b2",
        "44227125922b2ffc",
    }
)


def find_forbidden_matches(text: str) -> list[str]:
    """Return any leaked host-path patterns or hashed hygiene-blocklist hits in `text`."""
    lower = text.lower()
    hits: list[str] = []
    for pat in FORBIDDEN_PATH_PATTERNS:
        if pat in lower:
            hits.append(f"path:{pat}")

    tokens = re.findall(r"[a-z0-9]+", lower)
    seen_hashes: set[str] = set()
    for idx, tok in enumerate(tokens):
        candidates = [tok]
        if idx + 1 < len(tokens):
            candidates.append(f"{tok} {tokens[idx + 1]}")
        for cand in candidates:
            digest = hashlib.sha256((_HYGIENE_SALT + cand).encode("utf-8")).hexdigest()[:16]
            if digest in _FORBIDDEN_TOKEN_HASHES and digest not in seen_hashes:
                seen_hashes.add(digest)
                hits.append(f"hash:{digest}")

    extra_env = os.environ.get("RARE_KAON_HYGIENE_TERMS", "")
    for raw in extra_env.split(","):
        term = raw.strip().lower()
        if term and term in lower:
            hits.append(f"env:{term}")

    return hits


def ok_envelope(command: str, data: dict[str, Any], *, workspace: str | None = None) -> dict[str, Any]:
    """Build a standard success JSON envelope for CLI commands."""
    out: dict[str, Any] = {
        "ok": True,
        "command": command,
        "version": __version__,
        "data": data,
    }
    if workspace is not None:
        out["workspace"] = workspace
    return out


def error_envelope(
    command: str,
    error_code: str,
    cause: str,
    suggested_action: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a standard error JSON envelope for CLI commands."""
    out: dict[str, Any] = {
        "ok": False,
        "command": command,
        "version": __version__,
        "error_code": error_code,
        "cause": cause,
        "suggested_action": suggested_action,
    }
    if details:
        out["details"] = details
    return out
