"""Session-driven executive brief data builder (`brief` CLI command).

Collects live figures directly from saved canonical `ReportModel` runs, SQLite state,
YAML config, `config/config-audit.jsonl`, and `session-events.jsonl`, then merges
optional session-customized target framing using exact `Decimal` arithmetic.
Every claim on Page 1 is grounded in engine-verified state or logged CLI events.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
from typing import Any

import yaml

from engine.config import EngineError, load_config, read_audit_entries, utc_now_iso
from engine.contracts import DEFAULT_PAYROLL_PERIOD, DEFAULT_WEEKLY_PERIOD
from engine.model import ReportModel
from engine.operations import run_what_if
from engine.payroll import build_payroll_model
from engine.queries import build_privacy_file_lists
from engine.render.brief import render_session_brief_html, render_session_brief_pdf
from engine.weekly import build_weekly_model
from engine.workspace import (
    collect_active_models,
    connect_db,
    get_pending_changes,
    load_latest_model,
    read_session_events,
)

_DIGIT_OR_CURRENCY_RE = re.compile(r"[0-9$€£%]")

_FIELD_MAX_CHARS = {
    "target_company": 60,
    "target_workflow": 80,
    "target_owner": 60,
    "target_systems": 120,
    "lead_override": 240,
    "next_step_override": 200,
    "user_focus": 200,
}


def _to_dec(value: Any, default: Decimal = Decimal("0.00")) -> Decimal:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


def _format_short_date(iso_date: str) -> str:
    """Convert YYYY-MM-DD to concise US executive display (e.g. 'Nov 20, 2026')."""
    try:
        parsed = dt.date.fromisoformat(iso_date[:10])
        return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}"
    except ValueError:
        return iso_date


def _format_generated_stamp(iso_ts: str) -> str:
    """Format UTC timestamp concisely for brief headers (e.g. '2026-11-23 18:04 UTC')."""
    clean = iso_ts.replace("T", " ").replace("Z", "")
    return f"{clean[:16]} UTC"


def _compact_driver_label(label: str) -> str:
    """Drop trailing parenthetical invoice refs while keeping source wording faithful."""
    clean = label.strip()
    if " (" in clean and clean.endswith(")"):
        clean = clean.rsplit(" (", 1)[0].strip()
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


def _read_first_staged_amendment(workspace_dir: Path, known_source_files: set[str]) -> dict[str, Any] | None:
    """Read the first un-ingested YAML amendment from `<workspace_dir>/incoming/`."""
    incoming_dir = workspace_dir / "incoming"
    if not incoming_dir.is_dir():
        return None
    for path in sorted(incoming_dir.glob("*.yaml")):
        if path.name in known_source_files:
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(raw, dict) and raw.get("invoice_ref"):
            return {
                "file_name": path.name,
                "change_id": str(raw.get("change_id") or ""),
                "vendor": str(raw.get("vendor") or ""),
                "invoice_ref": str(raw.get("invoice_ref") or ""),
                "amount": _to_dec(raw.get("amount")),
                "old_week": int(raw["old_week"]) if raw.get("old_week") is not None else None,
                "new_week": int(raw["new_week"]) if raw.get("new_week") is not None else None,
            }
    return None


def _build_cash_source_note(weekly_model: ReportModel) -> str:
    """Build an accurate opening-cash source note directly from the model's cash accounts & sources."""
    accounts = weekly_model.sections.get("cash_accounts") or []
    acct_labels: list[str] = []
    for acct in accounts:
        name = str(acct.get("name") or "").strip()
        mask = str(acct.get("mask") or "").strip()
        if name and mask and mask not in name:
            acct_labels.append(f"{name} ({mask})")
        elif name:
            acct_labels.append(name)
    if acct_labels:
        joined = " and ".join(acct_labels) if len(acct_labels) <= 2 else ", ".join(acct_labels)
        return f"Traced to {joined} exports and biweekly payroll summary."
    src_names = [s.file_name for s in weekly_model.sources if s.feed_id in {"frost_operating", "frost_payroll_sweep"}]
    if src_names:
        return f"Traced to {', '.join(src_names)} and payroll summary."
    return "Traced directly to retained bank and payroll workspace sources."


def _normalize_spec(
    raw_spec: dict[str, Any] | None,
    *,
    target_company: str | None = None,
    target_workflow: str | None = None,
    target_harness: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Normalize and cap optional session customization fields."""
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
            text = " ".join(val.strip().split())
            limit = _FIELD_MAX_CHARS.get(key, 200)
            if len(text) > limit:
                text = text[: limit - 1].rstrip() + "…"
            customized.append(key)
            return text
        return default

    company = _clean_str("target_company", "Your Business")
    workflow = _clean_str("target_workflow", "one high-friction workflow")
    owner = _clean_str("target_owner", "a single accountable owner")
    systems = _clean_str(
        "target_systems",
        "the banks, ERPs, QuickBooks files, and spreadsheets your team already runs",
    )

    raw_workflows = spec.get("target_workflows")
    if isinstance(raw_workflows, list) and any(str(x).strip() for x in raw_workflows):
        workflows_list = [" ".join(str(x).strip().split())[:42] for x in raw_workflows if str(x).strip()][:6]
        customized.append("target_workflows")
    elif isinstance(raw_workflows, str) and raw_workflows.strip():
        workflows_list = [" ".join(x.strip().split())[:42] for x in raw_workflows.split(",") if x.strip()][:6]
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
        harness_key = raw_harness.strip().lower()[:40]
        customized.append("target_harness")

    lead_override = _clean_str("lead_override", "")
    next_step_override = _clean_str("next_step_override", "")

    # Optional digit/currency-guarded user focus line for Page 1 lead
    raw_focus = spec.get("user_focus")
    user_focus = ""
    if raw_focus is not None:
        if not isinstance(raw_focus, str):
            raise EngineError(
                "INVALID_BRIEF_SPEC",
                "`user_focus` must be a short plain-text string.",
                "Pass a plain-text description (at most 200 characters) without numbers or currency symbols.",
            )
        focus_clean = " ".join(raw_focus.strip().split())
        if len(focus_clean) > _FIELD_MAX_CHARS["user_focus"]:
            raise EngineError(
                "INVALID_BRIEF_SPEC",
                "`user_focus` exceeds the 200-character limit.",
                "Shorten `user_focus` to 200 characters or fewer.",
            )
        if _DIGIT_OR_CURRENCY_RE.search(focus_clean):
            raise EngineError(
                "INVALID_BRIEF_SPEC",
                "`user_focus` may not contain digits or currency/percent symbols so all figures come from the engine.",
                "Describe the topic in words without numbers (for example 'overdue receivables concentration').",
            )
        if focus_clean:
            user_focus = focus_clean
            customized.append("user_focus")

    # Optional stateless preview override to re-verify in memory at brief time
    preview_override = spec.get("previewed_deferral")
    if preview_override is not None and not isinstance(preview_override, dict):
        raise EngineError(
            "INVALID_BRIEF_SPEC",
            "`previewed_deferral` must be a valid deferral override JSON object.",
            "Pass the exact override dict used in `what-if` (e.g. {'defer_payment': {...}}).",
        )
    if isinstance(preview_override, dict):
        customized.append("previewed_deferral")

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
        "user_focus": user_focus,
        "previewed_deferral": preview_override,
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
    session_events = read_session_events(workspace_dir)
    generated_at = utc_now_iso()

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

    normalized_spec, customized_fields = _normalize_spec(
        spec,
        target_company=target_company,
        target_workflow=target_workflow,
        target_harness=target_harness,
    )

    # Engine-logged event signals (100% engine-verified; zero hallucinated history)
    asked_cash_bridge = any(
        e.get("command") == "explain" and e.get("topic") == "cash_bridge" and e.get("ok") is True
        for e in session_events
    )
    asked_ar_over_60 = any(
        e.get("command") == "explain" and e.get("topic") == "ar_over_60" and e.get("ok") is True
        for e in session_events
    )
    post_refusal_events = [
        e for e in session_events if e.get("command") == "post" and e.get("ok") is False
    ]
    approve_refusal_events = [
        e
        for e in session_events
        if e.get("command") == "approve" and e.get("module") == "payroll" and e.get("ok") is False
    ]
    post_refused = len(post_refusal_events) > 0
    approve_refused = len(approve_refusal_events) > 0

    # ------------------------------------------------------------------
    # Moment 1: Reconciled cash bridge & overdue A/R (exact Decimal)
    # ------------------------------------------------------------------
    wk_kpis = weekly_latest.kpi_map()
    bridge = weekly_latest.variance_bridge
    bridge_drivers: list[dict[str, Any]] = []
    if bridge is not None:
        for drv in bridge.drivers:
            bridge_drivers.append(
                {
                    "label": _compact_driver_label(drv.label),
                    "amount": _to_dec(drv.amount),
                    "source_file": drv.source_file,
                    "source_ref": drv.source_ref,
                }
            )

    moment1 = {
        "evidence_level": "engine_logged" if (asked_cash_bridge or asked_ar_over_60) else "engine_verified",
        "asked_cash_bridge": asked_cash_bridge,
        "asked_ar_over_60": asked_ar_over_60,
        "driver_count": len(bridge_drivers),
        "opening_cash": _to_dec(wk_kpis["opening_cash"].value),
        "prior_value": _to_dec(bridge.prior_value) if bridge else _to_dec(wk_kpis["opening_cash"].value),
        "bridge_delta": _to_dec(bridge.delta) if bridge else Decimal("0.00"),
        "prior_date_label": _format_short_date(bridge.baseline_period) if bridge else "Prior edition",
        "current_date_label": _format_short_date(weekly_latest.meta.period),
        "ar_over_60": _to_dec(wk_kpis["ar_over_60"].value),
        "ar_total": _to_dec(wk_kpis["ar_total"].value),
        "drivers": bridge_drivers,
        "source_note": _build_cash_source_note(weekly_latest),
    }

    # ------------------------------------------------------------------
    # Moment 2: Controlled revisions / preview / declined / staged / baseline
    # ------------------------------------------------------------------
    first_kpis = weekly_first.kpi_map()
    first_weeks = weekly_first.sections.get("forecast_weeks") or []
    latest_weeks = weekly_latest.sections.get("forecast_weeks") or []
    first_wk2 = _to_dec(first_weeks[1]["ending_cash"]) if len(first_weeks) >= 2 else Decimal("0.00")
    latest_wk2 = _to_dec(latest_weeks[1]["ending_cash"]) if len(latest_weeks) >= 2 else Decimal("0.00")
    revisions = list(weekly_latest.sections.get("revisions") or [])
    confirmed_changes = [c for c in all_changes if c["status"] == "confirmed"]
    pending_changes = [c for c in all_changes if c["needs_decision"]]
    declined_changes = [c for c in all_changes if c["status"] == "declined"]
    known_source_files = {str(c.get("source_file") or "") for c in all_changes}
    staged_amendment = _read_first_staged_amendment(workspace_dir, known_source_files)

    cash_floor = _to_dec(config["assumptions"]["weekly"].get("cash_floor_usd", "750000.00"))
    min_cash = _to_dec(wk_kpis["min_cash_balance"].value)
    min_cash_above_floor = min_cash >= cash_floor
    floor_status = (
        f"Above ${int(cash_floor):,} floor" if min_cash_above_floor else f"Below ${int(cash_floor):,} floor"
    )

    latest_label = (
        f"Edition {weekly_latest.meta.edition_number}"
        if weekly_latest.meta.is_approved and weekly_latest.meta.edition_number
        else f"Draft {weekly_latest.meta.draft_number}"
    )

    # Optional in-memory re-verification of a what-if preview passed in --spec
    preview_result: dict[str, Any] | None = None
    if normalized_spec["previewed_deferral"] is not None:
        try:
            preview_result = run_what_if(
                workspace_dir,
                period=DEFAULT_WEEKLY_PERIOD,
                override_json=json.dumps(normalized_spec["previewed_deferral"]),
            )
        except EngineError:
            preview_result = None

    has_revision = (weekly_latest.meta.draft_number > weekly_first.meta.draft_number) and len(revisions) > 0

    if has_revision:
        rev0 = revisions[-1]
        b_wk1 = _to_dec(first_kpis["week1_ending_cash"].value)
        a_wk1 = _to_dec(wk_kpis["week1_ending_cash"].value)
        b_wk13 = _to_dec(first_kpis["week13_ending_cash"].value)
        a_wk13 = _to_dec(wk_kpis["week13_ending_cash"].value)
        moment2: dict[str, Any] = {
            "mode": "revised",
            "evidence_level": "engine_verified",
            "title": "Operators test scenarios freely, then commit changes under review",
            "vendor": str(rev0.get("vendor") or ""),
            "invoice_ref": str(rev0.get("invoice_ref") or ""),
            "amount": _to_dec(rev0.get("amount")),
            "old_week": int(rev0["old_week"]) if rev0.get("old_week") is not None else None,
            "new_week": int(rev0["new_week"]) if rev0.get("new_week") is not None else None,
            "revision_count": len(revisions),
            "baseline_label": f"Draft {weekly_first.meta.draft_number}",
            "latest_label": latest_label,
            "rows": [
                {"label": "Week 1 ending cash", "before": b_wk1, "after": a_wk1, "changed": b_wk1 != a_wk1},
                {"label": "Week 2 ending cash", "before": first_wk2, "after": latest_wk2, "changed": first_wk2 != latest_wk2},
                {"label": "Week 13 ending cash", "before": b_wk13, "after": a_wk13, "changed": b_wk13 != a_wk13},
            ],
            "source_note": f"Confirmed decision logged in audit trail; Draft {weekly_first.meta.draft_number} preserved untouched.",
        }
    elif preview_result is not None:
        base_k = preview_result["baseline"]
        scen_k = preview_result["scenario"]
        ov_info = preview_result.get("override") or {}
        b_wk1 = _to_dec(base_k["week1_ending_cash"])
        a_wk1 = _to_dec(scen_k["week1_ending_cash"])
        b_wk2 = _to_dec(base_k["week2_ending_cash"])
        a_wk2 = _to_dec(scen_k["week2_ending_cash"])
        b_wk13 = _to_dec(base_k["week13_ending_cash"])
        a_wk13 = _to_dec(scen_k["week13_ending_cash"])
        moment2 = {
            "mode": "previewed",
            # The engine re-runs the figures, but only the agent knows the preview happened in session.
            "evidence_level": "agent_reported",
            "title": "Operators tested a payment shift in memory without changing saved files",
            "vendor": str(ov_info.get("vendor") or ""),
            "invoice_ref": str(ov_info.get("invoice_ref") or ""),
            "amount": _to_dec(ov_info.get("amount")),
            "old_week": int(ov_info["old_week"]) if ov_info.get("old_week") is not None else None,
            "new_week": int(ov_info["new_week"]) if ov_info.get("new_week") is not None else None,
            "revision_count": 0,
            "baseline_label": f"Draft {weekly_latest.meta.draft_number}",
            "latest_label": "Preview",
            "rows": [
                {"label": "Week 1 ending cash", "before": b_wk1, "after": a_wk1, "changed": b_wk1 != a_wk1},
                {"label": "Week 2 ending cash", "before": b_wk2, "after": a_wk2, "changed": b_wk2 != a_wk2},
                {"label": "Week 13 ending cash", "before": b_wk13, "after": a_wk13, "changed": b_wk13 != a_wk13},
            ],
            "source_note": "Preview reported by the assistant; figures re-run in memory by the engine (zero disk writes).",
        }
    elif pending_changes:
        pc = pending_changes[-1]
        pl = pc.get("payload") or {}
        moment2 = {
            "mode": "pending",
            "evidence_level": "engine_verified",
            "title": "Forecast changes require explicit operator confirmation before saving",
            "vendor": str(pl.get("vendor") or ""),
            "invoice_ref": str(pl.get("invoice_ref") or ""),
            "amount": _to_dec(pl.get("amount")),
            "old_week": int(pl["old_week"]) if pl.get("old_week") is not None else None,
            "new_week": int(pl["new_week"]) if pl.get("new_week") is not None else None,
            "revision_count": 0,
            "baseline_label": f"Draft {weekly_latest.meta.draft_number}",
            "latest_label": "Status",
            "rows": [
                {"label": "Week 1 ending cash", "before": _to_dec(wk_kpis["week1_ending_cash"].value), "after": None, "status_text": "Active draft", "tone": "neutral"},
                {"label": "13-week minimum cash", "before": min_cash, "after": None, "status_text": floor_status, "tone": "pos" if min_cash_above_floor else "neg"},
                {"label": "Week 13 ending cash", "before": _to_dec(wk_kpis["week13_ending_cash"].value), "after": None, "status_text": "Pending confirmation", "tone": "neutral"},
            ],
            "source_note": f"Change {pc['id']} is ingested and awaiting human confirmation; Draft 1 remains unchanged.",
        }
    elif declined_changes:
        dc = declined_changes[-1]
        pl = dc.get("payload") or {}
        moment2 = {
            "mode": "declined",
            "evidence_level": "engine_verified",
            "title": "A proposed amendment was declined and the baseline forecast stayed intact",
            "vendor": str(pl.get("vendor") or ""),
            "invoice_ref": str(pl.get("invoice_ref") or ""),
            "amount": _to_dec(pl.get("amount")),
            "old_week": int(pl["old_week"]) if pl.get("old_week") is not None else None,
            "new_week": int(pl["new_week"]) if pl.get("new_week") is not None else None,
            "revision_count": 0,
            "baseline_label": f"Draft {weekly_latest.meta.draft_number}",
            "latest_label": "Status",
            "rows": [
                {"label": "Week 1 ending cash", "before": _to_dec(wk_kpis["week1_ending_cash"].value), "after": None, "status_text": "Unchanged", "tone": "neutral"},
                {"label": "13-week minimum cash", "before": min_cash, "after": None, "status_text": floor_status, "tone": "pos" if min_cash_above_floor else "neg"},
                {"label": "Week 13 ending cash", "before": _to_dec(wk_kpis["week13_ending_cash"].value), "after": None, "status_text": "Amendment declined", "tone": "neutral"},
            ],
            "source_note": f"Change {dc['id']} was declined and logged to the audit trail; Draft {weekly_latest.meta.draft_number} remained unchanged.",
        }
    else:
        amendment_status = "1 amendment staged" if staged_amendment else "No pending changes"
        moment2 = {
            "mode": "staged" if staged_amendment else "baseline",
            "evidence_level": "engine_verified",
            "title": "Operators can preview forecast changes safely before anything is saved",
            "vendor": staged_amendment["vendor"] if staged_amendment else "",
            "invoice_ref": staged_amendment["invoice_ref"] if staged_amendment else "",
            "amount": staged_amendment["amount"] if staged_amendment else None,
            "old_week": staged_amendment["old_week"] if staged_amendment else None,
            "new_week": staged_amendment["new_week"] if staged_amendment else None,
            "revision_count": 0,
            "baseline_label": f"Draft {weekly_latest.meta.draft_number}",
            "latest_label": "Status",
            "rows": [
                {"label": "Week 1 ending cash", "before": _to_dec(wk_kpis["week1_ending_cash"].value), "after": None, "status_text": "Active draft", "tone": "neutral"},
                {"label": "13-week minimum cash", "before": min_cash, "after": None, "status_text": floor_status, "tone": "pos" if min_cash_above_floor else "neg"},
                {"label": "Week 13 ending cash", "before": _to_dec(wk_kpis["week13_ending_cash"].value), "after": None, "status_text": amendment_status, "tone": "neutral"},
            ],
            "source_note": "Stateless what-if previews run in memory (zero disk writes); confirmed edits keep prior drafts intact.",
        }

    # ------------------------------------------------------------------
    # Moment 3: Payroll hard stops & programmatic privacy wall
    # ------------------------------------------------------------------
    pay_summary = payroll_latest.sections.get("summary") or {}
    pay_kpis = payroll_latest.kpi_map()
    pay_blocking = list(payroll_latest.blocking_checks)
    pay_blocking_ids = [c.id for c in pay_blocking]
    pay_exceptions = payroll_latest.sections.get("exceptions") or []
    blocked_dept = str(pay_exceptions[0].get("dept_code") or "") if pay_exceptions else ""
    mapping_ref = pay_summary.get("mapping_approval_ref")

    if weekly_latest.open_review_checks:
        oc = weekly_latest.open_review_checks[0]
        amt_str = f"${int(_to_dec(oc.amount)):,}" if oc.amount is not None else ""
        open_review_note = (
            f"Routine review notes (e.g., {amt_str} fuel estimate) stay visible without halting weekly cash."
            if amt_str
            else "Non-blocking review items stay visible without halting weekly cash."
        )
    else:
        open_review_note = "Non-blocking review items stay visibly separated from approval blockers."

    moment3 = {
        "evidence_level": "engine_logged" if (post_refused or approve_refused) else "engine_verified",
        "pay_date_label": _format_short_date(payroll_latest.meta.period),
        "payroll_prepared": payroll_prepared,
        "post_refused": post_refused,
        "approve_refused": approve_refused,
        "post_refusal_code": str(post_refusal_events[-1].get("error_code") or "") if post_refusal_events else "",
        "funding_wire_total": _to_dec(pay_kpis["funding_wire_total"].value),
        "tieout_variance": _to_dec(pay_kpis["tieout_variance"].value),
        "blocking_count": len(pay_blocking),
        "blocking_ids": pay_blocking_ids,
        "blocked_dept": blocked_dept,
        "mapping_approval_ref": mapping_ref,
        "restricted_count": len(cited_non_attachable),
        "open_review_note": open_review_note,
    }

    # Header status & single-line rollup (accurate wording: "prepared" for drafts, "approved" for editions)
    pay_status_short = (
        "Payroll post blocked"
        if post_refused
        else (
            "Payroll approval blocked"
            if approve_refused
            else ("Payroll draft prepared" if payroll_prepared else "Payroll readiness checked")
        )
    )
    if weekly_latest.meta.is_approved and weekly_latest.meta.edition_number:
        wk_status_short = f"Edition {weekly_latest.meta.edition_number} approved"
    else:
        wk_status_short = f"Draft {weekly_latest.meta.draft_number} prepared"

    blocking_total = len(weekly_latest.blocking_checks) + len(payroll_latest.blocking_checks)
    rollup_parts = [f"{len(run_rows)} preserved run{'s' if len(run_rows) != 1 else ''}"]
    if edition_rows:
        rollup_parts.append(f"{len(edition_rows)} approved edition{'s' if len(edition_rows) != 1 else ''}")
    decision_count = max(len(confirmed_changes) + len(declined_changes), len(revisions), len(audit_entries))
    if decision_count:
        rollup_parts.append(f"{decision_count} logged decision{'s' if decision_count != 1 else ''}")
    if post_refusal_events:
        rollup_parts.append(f"{len(post_refusal_events)} blocked post attempt{'s' if len(post_refusal_events) != 1 else ''}")
    if approve_refusal_events:
        n_appr = len(approve_refusal_events)
        rollup_parts.append(f"{n_appr} blocked approval attempt{'s' if n_appr != 1 else ''}")
    if not post_refusal_events and not approve_refusal_events and blocking_total:
        rollup_parts.append(f"{blocking_total} active blocking check{'s' if blocking_total != 1 else ''}")
    if cited_non_attachable:
        n_conf = len(cited_non_attachable)
        rollup_parts.append(f"{n_conf} confidential file{'s' if n_conf != 1 else ''} withheld from sharing")

    evidence = [
        {
            "moment": 1,
            "topic": "reconciled_reporting",
            "evidence_level": moment1["evidence_level"],
            "variant": "past_tense" if (asked_cash_bridge or asked_ar_over_60) else "capability",
        },
        {
            "moment": 2,
            "topic": "controlled_revisions",
            "evidence_level": moment2["evidence_level"],
            "variant": moment2["mode"],
        },
        {
            "moment": 3,
            "topic": "controls_and_privacy",
            "evidence_level": moment3["evidence_level"],
            "variant": "past_refusal" if (post_refused or approve_refused) else ("prepared_hold" if payroll_prepared else "readiness_hold"),
        },
    ]

    return {
        "company_name": config["company_name"],
        "short_company_name": "Heartland Mechanical",
        "period": weekly_latest.meta.period,
        "period_display": f"Week ended {_format_short_date(weekly_latest.meta.period)}",
        "header_status": f"{wk_status_short} · {pay_status_short}",
        "generated_at": generated_at,
        "generated_stamp": _format_generated_stamp(generated_at),
        "moment1": moment1,
        "moment2": moment2,
        "moment3": moment3,
        "evidence": evidence,
        "rollup_summary": " · ".join(rollup_parts),
        "run_count": len(run_rows),
        "edition_count": len(edition_rows),
        "audit_entry_count": len(audit_entries),
        "session_event_count": len(session_events),
        "include_log": bool(include_log),
        "runs": list(reversed(run_rows)) if include_log else [],
        "editions": list(reversed(edition_rows)) if include_log else [],
        "audit_entries": list(reversed(audit_entries)) if include_log else [],
        "session_events": list(reversed(session_events)) if include_log else [],
        "spec": normalized_spec,
        "customized_fields": customized_fields,
    }


def _session_relative_path(path: Path) -> str:
    """Return path relative to CWD when inside CWD, otherwise full POSIX path."""
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


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
            "relative_path": _session_relative_path(html_path),
            "workspace_relative_path": html_path.name,
            "path": str(html_path),
            "kind": "session_brief_html",
            "sensitivity": "leave_behind",
        },
        {
            "file_name": pdf_path.name,
            "relative_path": _session_relative_path(pdf_path),
            "workspace_relative_path": pdf_path.name,
            "path": str(pdf_path),
            "kind": "session_brief_pdf",
            "sensitivity": "leave_behind",
        },
    ]

    return {
        "company_name": brief_data["company_name"],
        "period": brief_data["period"],
        "generated_at": brief_data["generated_at"],
        "page_count": page_count,
        "include_log": bool(include_log),
        "brief_html": str(html_path),
        "brief_pdf": str(pdf_path),
        "brief_html_rel": _session_relative_path(html_path),
        "brief_pdf_rel": _session_relative_path(pdf_path),
        "rollup_summary": brief_data["rollup_summary"],
        "evidence": brief_data["evidence"],
        "customized_fields": brief_data["customized_fields"],
        "target_company": brief_data["spec"]["target_company"],
        "attachable_files": attachable_files,
    }
