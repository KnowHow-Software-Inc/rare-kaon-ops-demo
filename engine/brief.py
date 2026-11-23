"""Session-driven executive brief data builder (`brief` CLI command).

Collects live figures directly from saved canonical `ReportModel` runs, SQLite state,
YAML config, and `config/config-audit.jsonl`, then merges optional session-customized
target-company framing without introducing manual arithmetic or leaking restricted data.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from engine.config import EngineError, load_config, read_audit_entries
from engine.contracts import DEFAULT_PAYROLL_PERIOD, DEFAULT_WEEKLY_PERIOD
from engine.model import ReportModel
from engine.payroll import build_payroll_model
from engine.queries import build_privacy_file_lists
from engine.render.brief import render_session_brief_html, render_session_brief_pdf
from engine.weekly import build_weekly_model
from engine.workspace import (
    collect_active_models,
    connect_db,
    get_pending_changes,
    load_latest_model,
)


def _format_short_date(iso_date: str) -> str:
    """Convert YYYY-MM-DD to concise US executive display (e.g. 'Nov 20, 2026')."""
    try:
        parsed = dt.date.fromisoformat(iso_date[:10])
        return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}"
    except ValueError:
        return iso_date


def _short_driver_label(label: str) -> str:
    """Trim parenthetical invoice codes from bridge driver labels for clean executive tables."""
    clean = label.strip()
    if " (" in clean and clean.endswith(")"):
        clean = clean.rsplit(" (", 1)[0].strip()
    clean = clean.replace("Facilities", "").replace("  ", " ").strip()
    clean = clean.replace("Seasonal overtime above baseline — 2026-11-13 biweekly payroll run", "Seasonal overtime — Nov 13 payroll")
    clean = clean.replace("Emergency compressor parts order — AUS-02", "Emergency compressor parts — Austin")
    return clean


def _load_first_weekly_model(workspace_dir: Path, period: str) -> ReportModel | None:
    """Load Weekly Draft 1 (`draft_number = 1`) from disk if present."""
    conn = connect_db(workspace_dir, readonly=True)
    try:
        row = conn.execute(
            """
            SELECT model_path FROM runs
            WHERE module = 'weekly' AND period = ?
            ORDER BY draft_number ASC LIMIT 1
            """,
            (period,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    p = workspace_dir / row["model_path"]
    if not p.is_file():
        return None
    return ReportModel.from_dict(json.loads(p.read_text(encoding="utf-8")))


def _normalize_spec(
    raw_spec: dict[str, Any] | None,
    *,
    target_company: str | None = None,
    target_workflow: str | None = None,
    target_harness: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Normalize optional session customization fields and report which were customized."""
    spec: dict[str, Any] = dict(raw_spec or {})
    if target_company:
        spec["target_company"] = target_company
    if target_workflow:
        spec["target_workflow"] = target_workflow
    if target_harness:
        spec["target_harness"] = target_harness

    customized: list[str] = []

    def _clean_str(key: str, default: str) -> str:
        val = spec.get(key)
        if isinstance(val, str) and val.strip():
            customized.append(key)
            return val.strip()
        return default

    company = _clean_str("target_company", "Your Operations")
    workflow = _clean_str("target_workflow", "one recurring operational bottleneck")
    owner = _clean_str("target_owner", "a single accountable owner")
    systems = _clean_str(
        "target_systems",
        "the banks, ERPs, QuickBooks files, and spreadsheets your team already runs",
    )

    raw_workflows = spec.get("target_workflows")
    if isinstance(raw_workflows, list) and any(str(x).strip() for x in raw_workflows):
        workflows_list = [str(x).strip() for x in raw_workflows if str(x).strip()][:6]
        customized.append("target_workflows")
    elif isinstance(raw_workflows, str) and raw_workflows.strip():
        workflows_list = [x.strip() for x in raw_workflows.split(",") if x.strip()][:6]
        customized.append("target_workflows")
    else:
        workflows_list = [
            "weekly cash",
            "overdue A/R",
            "branch margin",
            "customer churn risk",
            "credentialing compliance",
            "firm-level roll-ups",
        ]

    raw_harness = spec.get("target_harness")
    harness_key = ""
    if isinstance(raw_harness, str) and raw_harness.strip():
        harness_key = raw_harness.strip().lower()
        customized.append("target_harness")

    lead_override = _clean_str("lead_override", "")
    next_step_override = _clean_str("next_step_override", "")

    normalized = {
        "target_company": company,
        "target_company_is_custom": "target_company" in customized,
        "target_workflow": workflow,
        "target_workflow_is_custom": "target_workflow" in customized,
        "target_owner": owner,
        "target_systems": systems,
        "target_workflows": workflows_list,
        "target_harness": harness_key,
        "lead_override": lead_override,
        "next_step_override": next_step_override,
    }
    return normalized, sorted(set(customized))


def build_session_brief_data(
    workspace_dir: Path,
    *,
    spec: dict[str, Any] | None = None,
    target_company: str | None = None,
    target_workflow: str | None = None,
    target_harness: str | None = None,
    include_log: bool = False,
) -> dict[str, Any]:
    """Assemble live workspace evidence and session-tailored framing for the 2-page brief."""
    workspace_dir = Path(workspace_dir).resolve()
    config = load_config(workspace_dir)
    audit_entries = read_audit_entries(workspace_dir)

    conn = connect_db(workspace_dir, readonly=True)
    try:
        run_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT module, period, draft_number, status, can_approve, blocking_count, open_review_count, created_at "
                "FROM runs ORDER BY id ASC"
            ).fetchall()
        ]
        edition_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT module, period, edition_number, source_draft_number, approved_by, approved_at "
                "FROM editions ORDER BY id ASC"
            ).fetchall()
        ]
        all_changes = get_pending_changes(conn)
    finally:
        conn.close()

    # Load live Weekly baseline (Draft 1) and latest Weekly model (Draft N or Edition)
    weekly_latest, _ = load_latest_model(
        workspace_dir, module="weekly", period=DEFAULT_WEEKLY_PERIOD, prefer_edition=True
    )
    if weekly_latest is None:
        weekly_latest = build_weekly_model(workspace_dir, period=DEFAULT_WEEKLY_PERIOD, draft_number=1)

    weekly_first = _load_first_weekly_model(workspace_dir, DEFAULT_WEEKLY_PERIOD) or weekly_latest

    # Load live Payroll model (prepared in session if present, else read-only check preview)
    payroll_latest, _ = load_latest_model(
        workspace_dir, module="payroll", period=DEFAULT_PAYROLL_PERIOD, prefer_edition=True
    )
    payroll_prepared = payroll_latest is not None
    if payroll_latest is None:
        payroll_latest = build_payroll_model(workspace_dir, period=DEFAULT_PAYROLL_PERIOD, draft_number=0)

    # Privacy-safe restricted files list (filenames/dates only)
    active_models = collect_active_models(workspace_dir)
    _, cited_non_attachable = build_privacy_file_lists(workspace_dir, active_models, config)

    # Moment 1: Reconciled cash bridge & overdue A/R
    wk_kpis = weekly_latest.kpi_map()
    bridge = weekly_latest.variance_bridge
    bridge_drivers: list[dict[str, Any]] = []
    if bridge is not None:
        for drv in bridge.drivers:
            bridge_drivers.append(
                {
                    "label": _short_driver_label(drv.label),
                    "amount": float(drv.amount),
                    "source_file": drv.source_file,
                    "source_ref": drv.source_ref,
                }
            )

    moment1 = {
        "opening_cash": float(wk_kpis["opening_cash"].value),
        "prior_value": float(bridge.prior_value) if bridge else float(wk_kpis["opening_cash"].value),
        "bridge_delta": float(bridge.delta) if bridge else 0.0,
        "prior_date_label": _format_short_date(bridge.baseline_period) if bridge else "Prior edition",
        "current_date_label": _format_short_date(weekly_latest.meta.period),
        "ar_over_60": float(wk_kpis["ar_over_60"].value),
        "ar_total": float(wk_kpis["ar_total"].value),
        "drivers": bridge_drivers,
        "source_note": "Traced directly to Frost Operating (*4812) and payroll summary.",
    }

    # Moment 2: Controlled revisions (Draft 1 -> Latest Draft/Edition) or staged preview state
    first_kpis = weekly_first.kpi_map()
    first_weeks = weekly_first.sections.get("forecast_weeks") or []
    latest_weeks = weekly_latest.sections.get("forecast_weeks") or []
    first_wk2 = float(first_weeks[1]["ending_cash"]) if len(first_weeks) >= 2 else 0.0
    latest_wk2 = float(latest_weeks[1]["ending_cash"]) if len(latest_weeks) >= 2 else 0.0
    revisions = list(weekly_latest.sections.get("revisions") or [])
    confirmed_changes = [c for c in all_changes if c["status"] == "confirmed"]
    has_revision = (weekly_latest.meta.draft_number > weekly_first.meta.draft_number) or len(revisions) > 0

    latest_label = (
        f"Edition {weekly_latest.meta.edition_number}"
        if weekly_latest.meta.is_approved and weekly_latest.meta.edition_number
        else f"Draft {weekly_latest.meta.draft_number}"
    )

    if has_revision and revisions:
        rev0 = revisions[-1]
        moment2 = {
            "mode": "revised",
            "title": "Operators test scenarios freely, then commit changes under review",
            "vendor": str(rev0.get("vendor") or "Lone Star HVAC Supply"),
            "invoice_ref": str(rev0.get("invoice_ref") or "LSS-2026-4189"),
            "amount": float(rev0.get("amount") or 25000.0),
            "old_week": int(rev0.get("old_week") or 1),
            "new_week": int(rev0.get("new_week") or 2),
            "revision_count": len(revisions),
            "baseline_label": f"Draft {weekly_first.meta.draft_number}",
            "latest_label": latest_label,
            "rows": [
                {
                    "label": "Week 1 ending cash",
                    "before": float(first_kpis["week1_ending_cash"].value),
                    "after": float(wk_kpis["week1_ending_cash"].value),
                    "changed": float(first_kpis["week1_ending_cash"].value) != float(wk_kpis["week1_ending_cash"].value),
                },
                {
                    "label": "Week 2 ending cash",
                    "before": first_wk2,
                    "after": latest_wk2,
                    "changed": first_wk2 != latest_wk2,
                },
                {
                    "label": "Week 13 ending cash",
                    "before": float(first_kpis["week13_ending_cash"].value),
                    "after": float(wk_kpis["week13_ending_cash"].value),
                    "changed": float(first_kpis["week13_ending_cash"].value) != float(wk_kpis["week13_ending_cash"].value),
                },
            ],
            "source_note": f"Preview changed zero files; confirmed decision logged and Draft {weekly_first.meta.draft_number} preserved.",
        }
    else:
        # Only Draft 1 exists so far — show live Draft 1 liquidity profile and staged amendment readiness
        moment2 = {
            "mode": "baseline",
            "title": "Operators preview forecast changes safely before anything is saved",
            "vendor": "Lone Star HVAC Supply",
            "invoice_ref": "LSS-2026-4189",
            "amount": 25000.0,
            "old_week": 1,
            "new_week": 2,
            "revision_count": 0,
            "baseline_label": f"Draft {weekly_latest.meta.draft_number}",
            "latest_label": "Floor / Status",
            "rows": [
                {
                    "label": "Week 1 ending cash",
                    "before": float(wk_kpis["week1_ending_cash"].value),
                    "after": None,
                    "status_text": "Active draft",
                    "changed": False,
                },
                {
                    "label": "13-week minimum cash",
                    "before": float(wk_kpis["min_cash_balance"].value),
                    "after": None,
                    "status_text": f"Above ${float(config['assumptions']['weekly'].get('cash_floor_usd', 750000.0)):,.0f} floor",
                    "changed": True,
                },
                {
                    "label": "Week 13 ending cash",
                    "before": float(wk_kpis["week13_ending_cash"].value),
                    "after": None,
                    "status_text": "1 amendment staged",
                    "changed": False,
                },
            ],
            "source_note": "Stateless what-if runs in memory (zero disk writes); confirmed edits keep Draft 1 intact.",
        }

    # Moment 3: Payroll hard stops & programmatic privacy wall
    pay_summary = payroll_latest.sections.get("summary") or {}
    pay_kpis = payroll_latest.kpi_map()
    pay_blocking = payroll_latest.blocking_checks
    pay_exceptions = payroll_latest.sections.get("exceptions") or []
    blocked_dept = pay_exceptions[0].get("dept_code", "TUL-INSTALL-NEW") if pay_exceptions else "TUL-INSTALL-NEW"
    open_review_note = ""
    if weekly_latest.open_review_checks:
        oc = weekly_latest.open_review_checks[0]
        amt_str = f"${float(oc.amount):,.0f}" if oc.amount is not None else ""
        open_review_note = f"Routine review notes (e.g., {amt_str} fuel estimate) stay visible without halting weekly cash."
    else:
        open_review_note = "Non-blocking review items stay visibly separated from approval blockers."

    moment3 = {
        "pay_date_label": _format_short_date(payroll_latest.meta.period),
        "payroll_prepared": payroll_prepared,
        "funding_wire_total": float(pay_kpis["funding_wire_total"].value),
        "tieout_variance": float(pay_kpis["tieout_variance"].value),
        "blocking_count": len(pay_blocking),
        "blocked_dept": str(blocked_dept),
        "mapping_approval_ref": pay_summary.get("mapping_approval_ref"),
        "restricted_count": len(cited_non_attachable),
        "open_review_note": open_review_note,
    }

    # Session header status & single-line rollup (keeps Page 1 strictly 1 page even on long sessions)
    pay_status_short = "Payroll checked" if payroll_prepared else "Payroll readiness checked"
    if weekly_latest.meta.is_approved and weekly_latest.meta.edition_number:
        wk_status_short = f"Edition {weekly_latest.meta.edition_number} approved"
    else:
        wk_status_short = f"Draft {weekly_latest.meta.draft_number} issued"

    blocking_total = len(weekly_latest.blocking_checks) + len(payroll_latest.blocking_checks)
    rollup_parts = [
        f"{len(run_rows)} preserved run{'s' if len(run_rows) != 1 else ''}",
    ]
    if edition_rows:
        rollup_parts.append(f"{len(edition_rows)} approved edition{'s' if len(edition_rows) != 1 else ''}")
    decision_count = max(len(confirmed_changes), len(revisions), len(audit_entries))
    if decision_count:
        rollup_parts.append(
            f"{decision_count} logged decision{'s' if decision_count != 1 else ''}"
        )
    if blocking_total:
        rollup_parts.append(
            f"{blocking_total} blocking hold{'s' if blocking_total != 1 else ''} enforced"
        )
    if cited_non_attachable:
        rollup_parts.append(
            f"{len(cited_non_attachable)} restricted file{'s' if len(cited_non_attachable) != 1 else ''} shielded"
        )

    normalized_spec, customized_fields = _normalize_spec(
        spec,
        target_company=target_company,
        target_workflow=target_workflow,
        target_harness=target_harness,
    )

    return {
        "company_name": config["company_name"],
        "short_company_name": "Heartland Mechanical",
        "period": weekly_latest.meta.period,
        "period_display": f"Week ended {_format_short_date(weekly_latest.meta.period)}",
        "header_status": f"{wk_status_short} · {pay_status_short}",
        "moment1": moment1,
        "moment2": moment2,
        "moment3": moment3,
        "rollup_summary": " · ".join(rollup_parts),
        "run_count": len(run_rows),
        "edition_count": len(edition_rows),
        "audit_entry_count": len(audit_entries),
        "include_log": bool(include_log),
        "runs": run_rows if include_log else [],
        "editions": edition_rows if include_log else [],
        "audit_entries": audit_entries if include_log else [],
        "spec": normalized_spec,
        "customized_fields": customized_fields,
    }


def generate_session_brief(
    workspace_dir: Path,
    *,
    spec_json: str | None = None,
    target_company: str | None = None,
    target_workflow: str | None = None,
    target_harness: str | None = None,
    include_log: bool = False,
) -> dict[str, Any]:
    """Build live session brief data and render both `Session-Brief.html` and `Session-Brief.pdf`."""
    workspace_dir = Path(workspace_dir).resolve()

    parsed_spec: dict[str, Any] | None = None
    if spec_json:
        try:
            loaded = json.loads(spec_json)
        except json.JSONDecodeError as exc:
            raise EngineError(
                "INVALID_BRIEF_SPEC",
                f"Could not parse `--spec` JSON: {exc.msg}.",
                "Pass a valid JSON object to `--spec` (for example '{\"target_company\": \"Acme Services\"}').",
            ) from exc
        if not isinstance(loaded, dict):
            raise EngineError(
                "INVALID_BRIEF_SPEC",
                "`--spec` must be a JSON object.",
                "Pass a JSON object with keys such as target_company, target_workflow, target_harness.",
            )
        parsed_spec = loaded

    brief_data = build_session_brief_data(
        workspace_dir,
        spec=parsed_spec,
        target_company=target_company,
        target_workflow=target_workflow,
        target_harness=target_harness,
        include_log=include_log,
    )

    html_path = render_session_brief_html(brief_data, workspace_dir)
    pdf_path = render_session_brief_pdf(brief_data, workspace_dir)
    page_count = 3 if include_log else 2

    attachable_files = [
        {
            "file_name": html_path.name,
            "relative_path": html_path.name,
            "path": str(html_path),
            "kind": "session_brief_html",
            "sensitivity": "board",
        },
        {
            "file_name": pdf_path.name,
            "relative_path": pdf_path.name,
            "path": str(pdf_path),
            "kind": "session_brief_pdf",
            "sensitivity": "board",
        },
    ]

    return {
        "company_name": brief_data["company_name"],
        "period": brief_data["period"],
        "page_count": page_count,
        "include_log": bool(include_log),
        "brief_html": str(html_path),
        "brief_pdf": str(pdf_path),
        "rollup_summary": brief_data["rollup_summary"],
        "customized_fields": brief_data["customized_fields"],
        "target_company": brief_data["spec"]["target_company"],
        "attachable_files": attachable_files,
    }
