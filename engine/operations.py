"""Controlled operational mutations & stateless simulation: what-if, change, approve, post.

Guardrails enforced in code:
- `run_what_if`: clones SQLite into `:memory:` via `conn.backup()` and writes ZERO bytes to disk.
- `decide_change`: confirms or declines a queued `PendingChange` with audit logging.
- `approve_run`: refuses if `--by` is missing, `blocking_checks` exist, or open `pending_changes`
  exist; mints immutable numbered editions without touching prior drafts.
- `post_payroll`: refuses production posting if any blocking check exists OR
  `payroll.mapping_approval_ref` is null/empty; dry-run preview in sandbox mode with zero network calls.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from engine.config import (
    EngineError,
    append_audit_entry,
    cents_to_float,
    load_config,
    read_audit_entries,
    to_cents,
    utc_now_iso,
)
from engine.contracts import DEFAULT_PAYROLL_PERIOD, DEFAULT_WEEKLY_PERIOD
from engine.render.html import render_setup_html
from engine.render.pdf import render_weekly_pdfs
from engine.render.xlsx import render_payroll_xlsx
from engine.weekly import build_weekly_model
from engine.workspace import (
    clone_db_to_memory,
    connect_db,
    get_pending_changes,
    load_latest_model,
    next_edition_number,
    refresh_html_views,
)


# ---------------------------------------------------------------------------
# 1. Stateless in-memory `what-if` simulation (ZERO bytes written to disk)
# ---------------------------------------------------------------------------


def _parse_override_payload(raw_override: str) -> list[dict[str, Any]]:
    """Accept canonical `{"defer_payment": {...}}`, list `{"defer_payments": [...]}`, or shorthand `{"invoice_ref": ..., "new_week": ...}`."""
    try:
        parsed = json.loads(raw_override)
    except json.JSONDecodeError as exc:
        raise EngineError(
            "INVALID_OVERRIDE_JSON",
            f"Could not parse `--override` as JSON: {exc}",
            'Pass valid JSON such as `{"defer_payment": {"invoice_ref": "LSS-2026-4189", "new_week": 2, "new_due_date": "2026-12-02"}}`.',
        ) from exc

    if not isinstance(parsed, dict):
        raise EngineError(
            "INVALID_OVERRIDE_JSON",
            "`--override` must be a JSON object.",
            'Pass a JSON object such as `{"defer_payment": {"invoice_ref": "LSS-2026-4189", "new_week": 2}}`.',
        )

    items: list[dict[str, Any]] = []
    if "defer_payment" in parsed and isinstance(parsed["defer_payment"], dict):
        items.append(parsed["defer_payment"])
    elif "defer_payments" in parsed and isinstance(parsed["defer_payments"], list):
        items.extend(x for x in parsed["defer_payments"] if isinstance(x, dict))
    elif "invoice_ref" in parsed:
        items.append(parsed)
    else:
        raise EngineError(
            "INVALID_OVERRIDE_SCHEMA",
            "Override JSON must specify `defer_payment`, `defer_payments`, or top-level `invoice_ref` + `new_week`.",
            'Example: `{"defer_payment": {"invoice_ref": "LSS-2026-4189", "new_week": 2, "new_due_date": "2026-12-02"}}`.',
        )

    normalized: list[dict[str, Any]] = []
    for item in items:
        inv_ref = str(item.get("invoice_ref") or "").strip()
        if not inv_ref:
            raise EngineError(
                "INVALID_OVERRIDE_SCHEMA",
                "Each payment deferral override must include a non-empty `invoice_ref`.",
                'Provide an exact invoice reference such as `"LSS-2026-4189"`.',
            )
        if "new_week" not in item:
            raise EngineError(
                "INVALID_OVERRIDE_SCHEMA",
                f"Override for {inv_ref} is missing `new_week`.",
                "Specify `new_week` as an integer between 1 and 13.",
            )
        try:
            new_week = int(item["new_week"])
        except (TypeError, ValueError) as exc:
            raise EngineError(
                "INVALID_OVERRIDE_SCHEMA",
                f"Invalid `new_week` for {inv_ref}: {item['new_week']!r}.",
                "Specify `new_week` as an integer between 1 and 13.",
            ) from exc
        if not (1 <= new_week <= 13):
            raise EngineError(
                "INVALID_OVERRIDE_SCHEMA",
                f"`new_week` must be between 1 and 13 (got {new_week}).",
                "Specify a forecast week from 1 through 13.",
            )
        normalized.append(
            {
                "invoice_ref": inv_ref,
                "new_week": new_week,
                "new_due_date": str(item.get("new_due_date") or "").strip(),
            }
        )
    return normalized


def run_what_if(
    workspace_dir: Path,
    *,
    period: str = DEFAULT_WEEKLY_PERIOD,
    override_json: str,
) -> dict[str, Any]:
    """Run a stateless 13-week cash what-if simulation completely in RAM (zero disk writes)."""
    workspace_dir = Path(workspace_dir).resolve()
    deferrals = _parse_override_payload(override_json)

    # Clone workspace.db into `:memory:` via conn.backup() on a read-only URI connection
    mem_conn = clone_db_to_memory(workspace_dir)
    try:
        baseline_model = build_weekly_model(workspace_dir, period=period, draft_number=0, conn=mem_conn)
        extra_map = {
            d["invoice_ref"]: {"new_week": d["new_week"], "new_due_date": d["new_due_date"]}
            for d in deferrals
        }
        scenario_model = build_weekly_model(
            workspace_dir,
            period=period,
            draft_number=0,
            conn=mem_conn,
            extra_deferrals=extra_map,
        )
    finally:
        mem_conn.close()

    base_weeks = baseline_model.sections["forecast_weeks"]
    scen_weeks = scenario_model.sections["forecast_weeks"]
    base_ap = {r["invoice_ref"]: r for r in baseline_model.sections["scheduled_payments"]}

    applied_details = []
    for d in deferrals:
        orig = base_ap[d["invoice_ref"]]
        applied_details.append(
            {
                "invoice_ref": d["invoice_ref"],
                "vendor": orig["vendor"],
                "branch": orig["branch"],
                "amount": orig["amount"],
                "old_week": orig["forecast_week"],
                "new_week": d["new_week"],
                "old_due_date": orig["due_date"],
                "new_due_date": d["new_due_date"] or orig["due_date"],
            }
        )

    def _summary(model_obj: Any, weeks: list[dict[str, Any]]) -> dict[str, Any]:
        kmap = model_obj.kpi_map()
        return {
            "opening_cash": kmap["opening_cash"].value,
            "week1_ending_cash": weeks[0]["ending_cash"],
            "week2_outflows": weeks[1]["outflows"],
            "week2_ending_cash": weeks[1]["ending_cash"],
            "week13_ending_cash": weeks[-1]["ending_cash"],
            "min_cash_balance": kmap["min_cash_balance"].value,
        }

    base_sum = _summary(baseline_model, base_weeks)
    scen_sum = _summary(scenario_model, scen_weeks)

    deltas = {
        "week1_ending_cash": cents_to_float(
            to_cents(scen_sum["week1_ending_cash"]) - to_cents(base_sum["week1_ending_cash"])
        ),
        "week2_outflows": cents_to_float(
            to_cents(scen_sum["week2_outflows"]) - to_cents(base_sum["week2_outflows"])
        ),
        "week2_ending_cash": cents_to_float(
            to_cents(scen_sum["week2_ending_cash"]) - to_cents(base_sum["week2_ending_cash"])
        ),
        "week13_ending_cash": cents_to_float(
            to_cents(scen_sum["week13_ending_cash"]) - to_cents(base_sum["week13_ending_cash"])
        ),
        "min_cash_balance": cents_to_float(
            to_cents(scen_sum["min_cash_balance"]) - to_cents(base_sum["min_cash_balance"])
        ),
    }

    weekly_deltas: list[dict[str, Any]] = []
    for bw, sw in zip(base_weeks, scen_weeks):
        out_d = to_cents(sw["outflows"]) - to_cents(bw["outflows"])
        end_d = to_cents(sw["ending_cash"]) - to_cents(bw["ending_cash"])
        if out_d != 0 or end_d != 0:
            weekly_deltas.append(
                {
                    "week_num": bw["week_num"],
                    "week_ending": bw["week_ending"],
                    "baseline_outflows": bw["outflows"],
                    "scenario_outflows": sw["outflows"],
                    "outflows_delta": cents_to_float(out_d),
                    "baseline_ending_cash": bw["ending_cash"],
                    "scenario_ending_cash": sw["ending_cash"],
                    "ending_cash_delta": cents_to_float(end_d),
                }
            )

    cash_floor = float(baseline_model.sections.get("cash_floor", 750000.0))
    return {
        "period": period,
        "stateless": True,
        "files_modified": 0,
        "bytes_written": 0,
        "storage_note": "Simulated entirely in an in-memory SQLite clone (`conn.backup()`); zero files or saved records were modified.",
        "overrides_applied": applied_details,
        "baseline": {**base_sum, "weeks": base_weeks},
        "scenario": {**scen_sum, "weeks": scen_weeks},
        "deltas": deltas,
        "weekly_deltas": weekly_deltas,
        "cash_floor_usd": cash_floor,
        "floor_breached_baseline": to_cents(base_sum["min_cash_balance"]) < to_cents(cash_floor),
        "floor_breached_scenario": to_cents(scen_sum["min_cash_balance"]) < to_cents(cash_floor),
    }


# ---------------------------------------------------------------------------
# 2. `change confirm | decline`
# ---------------------------------------------------------------------------


def decide_change(
    workspace_dir: Path,
    *,
    action: str,
    change_id: str,
    by: str | None = None,
    reason: str = "",
) -> dict[str, Any]:
    """Confirm or decline a pending operator change with append-only audit logging."""
    workspace_dir = Path(workspace_dir).resolve()
    if action not in ("confirm", "decline"):
        raise EngineError(
            "INVALID_CHANGE_ACTION",
            f"Unknown change action {action!r}.",
            "Use `change confirm` or `change decline`.",
        )

    config = load_config(workspace_dir)
    actor = (by or "").strip() or str((config["outputs"].get("approver") or {}).get("name") or "Operator")
    now = utc_now_iso()

    conn = connect_db(workspace_dir, readonly=False)
    try:
        row = conn.execute("SELECT * FROM pending_changes WHERE id = ?", (change_id,)).fetchone()
        if not row:
            raise EngineError(
                "PENDING_CHANGE_NOT_FOUND",
                f"No pending change found with id {change_id!r}.",
                "Run `ingest` on an incoming amendment first or inspect open changes with `overview`.",
                details={"change_id": change_id},
            )
        current_status = str(row["status"])
        if current_status != "pending":
            raise EngineError(
                "PENDING_CHANGE_ALREADY_DECIDED",
                f"Change {change_id} is already `{current_status}` (decided by {row['decided_by'] or 'unknown'}).",
                "No further decision is required for this change.",
                details={"change_id": change_id, "status": current_status},
            )

        new_status = "confirmed" if action == "confirm" else "declined"
        conn.execute(
            """
            UPDATE pending_changes
            SET status = ?, needs_decision = 0, decided_by = ?, decided_at = ?
            WHERE id = ?
            """,
            (new_status, actor, now, change_id),
        )
        conn.commit()
        payload = json.loads(row["payload_json"])
        summary = str(row["summary"])
        period = str(row["period"])
        module = str(row["module"])
    finally:
        conn.close()

    audit_entry = append_audit_entry(
        workspace_dir,
        actor=actor,
        field_path=f"pending_change.{change_id}",
        old_value="pending",
        new_value=new_status,
        reason=reason.strip() or summary,
        target="pending_changes",
        action=f"change_{action}",
    )

    setup_html = render_setup_html(config, read_audit_entries(workspace_dir), workspace_dir)

    return {
        "change_id": change_id,
        "module": module,
        "period": period,
        "previous_status": "pending",
        "status": new_status,
        "needs_decision": False,
        "decided_by": actor,
        "decided_at": now,
        "summary": summary,
        "payload": payload,
        "audit_entry": audit_entry,
        "setup_html": str(setup_html),
        "next_step": (
            f"Run `prepare --module {module} --period {period}` to generate a new draft incorporating {change_id}."
            if new_status == "confirmed"
            else "Change declined; existing drafts and forecast remain unchanged."
        ),
    }


# ---------------------------------------------------------------------------
# 3. `approve` — immutable numbered editions with hard-stop enforcement
# ---------------------------------------------------------------------------


def approve_run(
    workspace_dir: Path,
    *,
    module: str,
    period: str,
    by: str | None,
) -> dict[str, Any]:
    """Approve the latest draft into an immutable numbered edition if zero blockers/pending changes remain."""
    workspace_dir = Path(workspace_dir).resolve()
    if module not in ("weekly", "payroll"):
        raise EngineError(
            "INVALID_MODULE",
            f"Unknown module {module!r}.",
            "Specify `--module weekly` or `--module payroll`.",
        )
    if not by or not by.strip():
        raise EngineError(
            "APPROVER_REQUIRED",
            "Approval requires an explicit human approver via `--by \"<name>\"`.",
            "Re-run `approve` with `--by \"<approver name>\"`.",
        )

    approver = by.strip()
    model, model_path = load_latest_model(workspace_dir, module=module, period=period, prefer_edition=False)
    if model is None or model_path is None:
        raise EngineError(
            "NO_DRAFT_FOUND",
            f"No prepared draft found for module `{module}`, period `{period}`.",
            f"Run `prepare --module {module} --period {period}` first.",
        )

    # Check both model-level and DB-level open pending changes
    conn = connect_db(workspace_dir, readonly=True)
    try:
        db_open_pending = get_pending_changes(conn, module=module, period=period, status="pending")
    finally:
        conn.close()

    if model.blocking_checks:
        raise EngineError(
            "BLOCKING_CHECKS_OPEN",
            (
                f"Cannot approve {module} ({period}): {len(model.blocking_checks)} blocking check(s) remain open: "
                + "; ".join(f"{c.id} ({c.title})" for c in model.blocking_checks)
                + "."
            ),
            "Resolve all blocking checks and re-run `prepare` before requesting approval.",
            details={
                "module": module,
                "period": period,
                "blocking_checks": [c.__dict__ for c in model.blocking_checks],
            },
        )

    open_pending_ids = {p.id for p in model.pending_changes if p.needs_decision} | {p["id"] for p in db_open_pending}
    if open_pending_ids:
        raise EngineError(
            "PENDING_CHANGES_OPEN",
            f"Cannot approve {module} ({period}): {len(open_pending_ids)} unconfirmed pending change(s) await decision ({', '.join(sorted(open_pending_ids))}).",
            "Confirm or decline every pending change with `change confirm|decline --id <ref> --by \"<name>\"`, then re-run `prepare`.",
            details={"pending_change_ids": sorted(open_pending_ids)},
        )

    # Mint immutable numbered edition (leaving source draft folder completely intact)
    conn = connect_db(workspace_dir, readonly=False)
    try:
        edition_num = next_edition_number(conn, module, period)
    finally:
        conn.close()

    now = utc_now_iso()
    ed_model = copy.deepcopy(model)
    ed_model.meta.status = "approved"
    ed_model.meta.is_approved = True
    ed_model.meta.edition_number = edition_num
    ed_model.meta.approved_by = approver
    ed_model.meta.approved_at = now

    ed_rel = f"editions/{module}/{period}/edition-{edition_num:03d}"
    ed_dir = workspace_dir / ed_rel
    ed_dir.mkdir(parents=True, exist_ok=True)

    new_artifacts: dict[str, str] = {}
    attachable_files: list[dict[str, Any]] = []

    if module == "weekly":
        pdfs = render_weekly_pdfs(ed_model, ed_dir)
        liq_rel = f"{ed_rel}/{pdfs['liquidity_pdf'].name}"
        cf_rel = f"{ed_rel}/{pdfs['cash_flow_pdf'].name}"
        new_artifacts["liquidity_pdf"] = liq_rel
        new_artifacts["cash_flow_pdf"] = cf_rel
        attachable_files.extend(
            [
                {
                    "file_name": pdfs["liquidity_pdf"].name,
                    "relative_path": liq_rel,
                    "path": str(pdfs["liquidity_pdf"]),
                    "kind": "pdf_report",
                    "sensitivity": "board",
                },
                {
                    "file_name": pdfs["cash_flow_pdf"].name,
                    "relative_path": cf_rel,
                    "path": str(pdfs["cash_flow_pdf"]),
                    "kind": "pdf_report",
                    "sensitivity": "board",
                },
            ]
        )
    else:
        xlsx_path = render_payroll_xlsx(ed_model, ed_dir / "payroll-review.xlsx")
        xlsx_rel = f"{ed_rel}/{xlsx_path.name}"
        new_artifacts["payroll_workbook"] = xlsx_rel
        attachable_files.append(
            {
                "file_name": xlsx_path.name,
                "relative_path": xlsx_rel,
                "path": str(xlsx_path),
                "kind": "xlsx_workbook",
                "sensitivity": "internal",
            }
        )

    ed_model_rel = f"{ed_rel}/report-model.json"
    new_artifacts["report_model"] = ed_model_rel
    ed_model.artifacts = new_artifacts
    (workspace_dir / ed_model_rel).write_text(
        json.dumps(ed_model.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    conn = connect_db(workspace_dir, readonly=False)
    try:
        conn.execute(
            """
            INSERT INTO editions(
                module, period, edition_number, source_draft_number,
                edition_dir, model_path, approved_by, approved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                module,
                period,
                edition_num,
                model.meta.draft_number,
                ed_rel,
                ed_model_rel,
                approver,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    audit_entry = append_audit_entry(
        workspace_dir,
        actor=approver,
        field_path=f"edition.{module}.{period}",
        old_value=f"draft-{model.meta.draft_number:03d}",
        new_value=f"edition-{edition_num:03d}",
        reason=f"Approved {module} {period} Draft {model.meta.draft_number} as Edition {edition_num}.",
        target=module,
        action="approve",
    )

    reports_html, setup_html = refresh_html_views(workspace_dir)
    attachable_files.insert(
        0,
        {
            "file_name": "Reports.html",
            "relative_path": "Reports.html",
            "path": str(reports_html),
            "kind": "dashboard",
            "sensitivity": "internal",
        },
    )

    cited_non_attachable = [
        {
            "feed_id": s.feed_id,
            "file_name": s.file_name,
            "as_of": s.as_of,
            "sensitivity": s.sensitivity,
            "attachable": False,
            "citation_rule": "Cite by filename and date only; never attach or reproduce row-level employee pay.",
        }
        for s in ed_model.non_attachable_sources()
    ]

    return {
        "module": module,
        "period": period,
        "source_draft_number": model.meta.draft_number,
        "edition_number": edition_num,
        "status": "approved",
        "approved_by": approver,
        "approved_at": now,
        "edition_dir": str(ed_dir),
        "model_path": str(workspace_dir / ed_model_rel),
        "open_review_checks_carried": [c.__dict__ for c in ed_model.open_review_checks],
        "audit_entry": audit_entry,
        "artifacts": {
            **{k: str(workspace_dir / v) for k, v in new_artifacts.items()},
            "reports_html": str(reports_html),
            "setup_html": str(setup_html),
        },
        "attachable_files": attachable_files,
        "cited_non_attachable_sources": cited_non_attachable,
    }


# ---------------------------------------------------------------------------
# 4. `post` — gated ERP payroll posting (hard stops + sandbox dry-run)
# ---------------------------------------------------------------------------


def post_payroll(
    workspace_dir: Path,
    *,
    module: str = "payroll",
    period: str = DEFAULT_PAYROLL_PERIOD,
    target: str = "sandbox",
) -> dict[str, Any]:
    """Enforce deterministic payroll posting gates; never executes live ERP network calls."""
    workspace_dir = Path(workspace_dir).resolve()
    if module != "payroll":
        raise EngineError(
            "UNSUPPORTED_POST_MODULE",
            f"ERP posting is only supported for `payroll` (got {module!r}).",
            "Use `--module payroll`.",
        )
    if target not in ("sandbox", "production"):
        raise EngineError(
            "INVALID_POST_TARGET",
            f"Invalid posting target {target!r}.",
            "Specify `--target sandbox` or `--target production`.",
        )

    model, _ = load_latest_model(workspace_dir, module="payroll", period=period, prefer_edition=True)
    if model is None:
        raise EngineError(
            "NO_DRAFT_FOUND",
            f"No prepared payroll run found for period {period}.",
            f"Run `prepare --module payroll --period {period}` first.",
        )

    config = load_config(workspace_dir)
    payroll_cfg = config["assumptions"]["payroll"]
    mapping_approval_ref = payroll_cfg.get("mapping_approval_ref")
    erp_mode = str(payroll_cfg.get("erp_posting_mode") or "sandbox")
    approval_owner = str(payroll_cfg.get("mapping_approval_owner") or "Rachel Morales")

    blocking = model.blocking_checks
    ref_missing = not (isinstance(mapping_approval_ref, str) and mapping_approval_ref.strip())

    # Gate 1: Blocking checks refuse both sandbox and production posting
    if blocking:
        extra_gate = (
            f" Additionally, `payroll.mapping_approval_ref` is unset (null) in config/assumptions.yaml, "
            f"which independently blocks production ERP posting."
            if (target == "production" and ref_missing)
            else ""
        )
        raise EngineError(
            "PAYROLL_BLOCKING_CHECKS_OPEN",
            (
                f"Refused {target} ERP posting for payroll {period}: {len(blocking)} blocking check(s) remain open ("
                + "; ".join(f"{c.id}: {c.title}" for c in blocking)
                + f").{extra_gate}"
            ),
            (
                f"Resolve blocking check(s) in config/payroll-mappings.yaml, have {approval_owner} record "
                "`payroll.mapping_approval_ref` in config/assumptions.yaml, and re-run `prepare --module payroll`."
            ),
            details={
                "module": "payroll",
                "period": period,
                "target": target,
                "erp_posting_mode": erp_mode,
                "mapping_approval_ref": mapping_approval_ref,
                "mapping_approval_ref_unset": ref_missing,
                "blocking_checks": [c.__dict__ for c in blocking],
                "exceptions": model.sections.get("exceptions", []),
                "live_network_call": False,
            },
        )

    # Gate 2: Production posting additionally requires non-empty `mapping_approval_ref`
    if target == "production" and ref_missing:
        raise EngineError(
            "MAPPING_APPROVAL_REF_REQUIRED",
            (
                f"Refused production ERP posting for payroll {period}: `payroll.mapping_approval_ref` "
                f"in config/assumptions.yaml is null/empty (current erp_posting_mode: {erp_mode})."
            ),
            (
                f"Obtain written department-mapping sign-off from {approval_owner} and record it via "
                '`config set --target assumptions --path payroll.mapping_approval_ref --value \'"<REF>"\' --by "<name>"`.'
            ),
            details={
                "module": "payroll",
                "period": period,
                "target": target,
                "erp_posting_mode": erp_mode,
                "mapping_approval_ref": None,
                "live_network_call": False,
            },
        )

    summary = model.sections["summary"]
    journal_lines = model.sections["journal_lines"]
    payload_preview = {
        "batch_id": f"PR-{period}-{summary.get('wire_reference', 'WIRE')}",
        "company": model.meta.company_name,
        "posting_date": period,
        "target": target,
        "mapping_approval_ref": mapping_approval_ref,
        "currency": "USD",
        "total_debits": summary["journal_debits_total"],
        "total_credits": summary["journal_credits_total"],
        "line_count": len(journal_lines),
        "lines": journal_lines,
    }

    cited_non_attachable = [
        {
            "feed_id": s.feed_id,
            "file_name": s.file_name,
            "as_of": s.as_of,
            "sensitivity": s.sensitivity,
            "attachable": False,
            "citation_rule": "Cite by filename and date only; never attach or reproduce row-level employee pay.",
        }
        for s in model.non_attachable_sources()
    ]

    return {
        "module": "payroll",
        "period": period,
        "target": target,
        "erp_posting_mode": erp_mode,
        "mapping_approval_ref": mapping_approval_ref,
        "dry_run": True,
        "live_network_call": False,
        "erp_posted": False,
        "status": "sandbox_dry_run_preview" if target == "sandbox" else "production_gate_passed_dry_run",
        "note": (
            "Dry-run ERP journal payload preview generated locally. Zero live ERP credentials or network calls exist in this demo."
        ),
        "erp_payload_preview": payload_preview,
        "attachable_files": [],
        "cited_non_attachable_sources": cited_non_attachable,
    }
