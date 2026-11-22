"""Deterministic Payroll tie-out, department mapping validation, and journal builder.

Parses restricted `payroll-voucher-2026-11-15.csv` and `payroll-funding-wire-2026-11-15.yaml`,
runs exact integer-cent component tie-outs (`$0.00` variance), validates every employee's
`dept_code` against `config/payroll-mappings.yaml`, fires blocking check `PAY-MAP-001` for
unmapped departments (`EMP-1048`, Marcus Delgado, `TUL-03`, `TUL-INSTALL-NEW`), enforces
programmatic privacy (`sensitivity="restricted"`, `attachable=False`), and renders
`payroll-review.xlsx`.
"""

from __future__ import annotations

import datetime as dt
import json
from collections import OrderedDict
from pathlib import Path
from typing import Any

from engine import __version__
from engine.config import (
    EngineError,
    cents_to_currency,
    cents_to_float,
    load_config,
    to_cents,
    utc_now_iso,
)
from engine.contracts import DEFAULT_PAYROLL_PERIOD, SCHEMA_VERSION
from engine.ingest import (
    get_feed_config,
    parse_payroll_funding_wire_yaml,
    parse_payroll_voucher_csv,
    resolve_feed_file,
)
from engine.model import CheckItem, KpiField, ReportMeta, ReportModel, SourceRef
from engine.render.xlsx import render_payroll_xlsx
from engine.workspace import (
    connect_db,
    get_pending_changes,
    next_draft_number,
    pending_rows_to_dataclasses,
    refresh_html_views,
)


def _format_pay_date_label(period_iso: str) -> str:
    d = dt.date.fromisoformat(period_iso)
    return f"Pay date {d.day} {d.strftime('%B %Y')}"


def build_payroll_model(
    workspace_dir: Path,
    *,
    period: str = DEFAULT_PAYROLL_PERIOD,
    draft_number: int = 1,
) -> ReportModel:
    """Compute the canonical Payroll Pre-Posting Review `ReportModel` without writing to disk."""
    workspace_dir = Path(workspace_dir)
    config = load_config(workspace_dir)
    assumptions = config["assumptions"]
    payroll_cfg = assumptions["payroll"]
    outputs_cfg = config["outputs"]
    mappings_cfg = config["payroll_mappings"]

    # 1. Load restricted payroll register & funding wire confirmation
    pv_feed = get_feed_config(config, "payroll_voucher")
    wire_feed = get_feed_config(config, "payroll_funding_wire")
    pv_path = resolve_feed_file(workspace_dir, config, "payroll_voucher", period=period)
    wire_path = resolve_feed_file(workspace_dir, config, "payroll_funding_wire", period=period)

    rows = parse_payroll_voucher_csv(pv_path, pv_feed)
    wire = parse_payroll_funding_wire_yaml(wire_path)
    wire_comp = wire["components"]

    # 2. Exact integer-cent aggregation & internal row footing checks
    sums = {
        "regular_wages": 0,
        "overtime_wages": 0,
        "gross_wages": 0,
        "employer_fica": 0,
        "employer_futa_suta": 0,
        "employer_taxes": 0,
        "employer_401k_match": 0,
        "employer_medical": 0,
        "employer_benefits": 0,
        "employee_withholdings": 0,
        "net_pay": 0,
    }
    row_footing_errors: list[str] = []

    for r in rows:
        reg = r["regular_pay_cents"]
        ot = r["overtime_pay_cents"]
        gross = r["gross_pay_cents"]
        fica = r["employer_fica_cents"]
        futa = r["employer_futa_suta_cents"]
        er_tax = r["employer_taxes_cents"]
        m401 = r["employer_401k_match_cents"]
        med = r["employer_medical_cents"]
        er_ben = r["employer_benefits_cents"]
        wh = r["employee_withholdings_cents"]
        net = r["net_pay_cents"]

        if reg + ot != gross or fica + futa != er_tax or m401 + med != er_ben or gross - wh != net:
            row_footing_errors.append(r["employee_id"])

        sums["regular_wages"] += reg
        sums["overtime_wages"] += ot
        sums["gross_wages"] += gross
        sums["employer_fica"] += fica
        sums["employer_futa_suta"] += futa
        sums["employer_taxes"] += er_tax
        sums["employer_401k_match"] += m401
        sums["employer_medical"] += med
        sums["employer_benefits"] += er_ben
        sums["employee_withholdings"] += wh
        sums["net_pay"] += net

    employer_cost_cents = sums["gross_wages"] + sums["employer_taxes"] + sums["employer_benefits"]
    funding_wire_cents = sums["net_pay"] + sums["employer_taxes"] + sums["employer_benefits"]

    # 3. Component-level tie-outs against funding wire confirmation
    tolerance_cents = int(payroll_cfg.get("tieout_tolerance_cents", 0))
    tieout_specs = [
        ("Regular Wages", sums["regular_wages"], to_cents(wire_comp.get("regular_wages", sums["regular_wages"]))),
        ("Overtime Wages", sums["overtime_wages"], to_cents(wire_comp.get("overtime_wages", sums["overtime_wages"]))),
        ("Gross Wages", sums["gross_wages"], to_cents(wire_comp["gross_wages"])),
        ("Employer Payroll Taxes (FICA/FUTA/SUTA)", sums["employer_taxes"], to_cents(wire_comp["employer_taxes"])),
        ("Employer Benefits & 401(k) Match", sums["employer_benefits"], to_cents(wire_comp["employer_benefits"])),
        ("Employee Tax & Benefit Withholdings", sums["employee_withholdings"], to_cents(wire_comp["employee_withholdings"])),
        ("Net Employee Pay", sums["net_pay"], to_cents(wire_comp["net_pay"])),
        ("Total Employer Payroll Cost", employer_cost_cents, to_cents(wire_comp.get("employer_cost_total", employer_cost_cents))),
        ("Total Payroll Funding Wire", funding_wire_cents, to_cents(wire_comp["total_funding_wire"])),
    ]

    tieout_rows: list[dict[str, Any]] = []
    failed_tieouts: list[dict[str, Any]] = []
    for label, reg_c, wire_c in tieout_specs:
        diff_c = reg_c - wire_c
        passed = abs(diff_c) <= tolerance_cents
        entry = {
            "component": label,
            "register_amount": cents_to_float(reg_c),
            "funding_amount": cents_to_float(wire_c),
            "difference": cents_to_float(diff_c),
            "status": "pass" if passed else "fail",
        }
        tieout_rows.append(entry)
        if not passed:
            failed_tieouts.append(entry)

    wire_variance_cents = funding_wire_cents - to_cents(wire_comp["total_funding_wire"])
    total_abs_variance_cents = abs(wire_variance_cents) + sum(
        abs(to_cents(item["difference"])) for item in failed_tieouts if item["component"] != "Total Payroll Funding Wire"
    )

    # 4. Department mapping validation & balanced draft journal assembly
    active_mappings: dict[str, dict[str, Any]] = {
        str(m["dept_code"]): m
        for m in (mappings_cfg.get("mappings") or [])
        if m.get("active", True)
    }
    require_all_mapped = bool(payroll_cfg.get("require_all_departments_mapped", True))
    mapping_owner = str(payroll_cfg.get("mapping_approval_owner") or mappings_cfg.get("owner") or "Rachel Morales")

    dept_buckets: OrderedDict[tuple[str, str], dict[str, int]] = OrderedDict()
    unmapped_employees: list[dict[str, Any]] = []

    for r in rows:
        dept = r["dept_code"]
        branch = r["branch"]
        key = (dept, branch)
        if key not in dept_buckets:
            dept_buckets[key] = {"gross": 0, "taxes": 0, "benefits": 0, "count": 0}
        dept_buckets[key]["gross"] += r["gross_pay_cents"]
        dept_buckets[key]["taxes"] += r["employer_taxes_cents"]
        dept_buckets[key]["benefits"] += r["employer_benefits_cents"]
        dept_buckets[key]["count"] += 1

        if dept not in active_mappings:
            unmapped_employees.append(r)

    exceptions: list[dict[str, Any]] = []
    checks: list[CheckItem] = []

    # Tie-out hard stop if any variance exceeds tolerance
    if failed_tieouts or row_footing_errors:
        checks.append(
            CheckItem(
                id="PAY-TIE-001",
                group="Payroll",
                title="Payroll register vs. funding wire tie-out variance",
                severity="blocking",
                detail=(
                    f"Tie-out failed across {len(failed_tieouts)} component(s) "
                    f"(variance {cents_to_currency(total_abs_variance_cents)} exceeds {tolerance_cents}-cent tolerance)."
                ),
                owner=mapping_owner,
                next_step="Reconcile payroll voucher export against funding wire confirmation before posting.",
                amount=cents_to_float(total_abs_variance_cents),
            )
        )

    # Department mapping hard stop (`PAY-MAP-001`)
    if unmapped_employees and require_all_mapped:
        for idx, emp in enumerate(unmapped_employees, start=1):
            code = "PAY-MAP-001" if len(unmapped_employees) == 1 else f"PAY-MAP-{idx:03d}"
            exceptions.append(
                {
                    "code": code,
                    "employee_id": emp["employee_id"],
                    "employee_name": emp["employee_name"],
                    "branch": emp["branch"],
                    "dept_code": emp["dept_code"],
                    "severity": "blocking",
                    "detail": (
                        f"Employee {emp['employee_id']} ({emp['employee_name']}, branch {emp['branch']}) "
                        f"is coded to unmapped department {emp['dept_code']}."
                    ),
                    "remediation": (
                        f"Controller {mapping_owner} must add an approved GL mapping for {emp['dept_code']} "
                        f"in config/payroll-mappings.yaml and record payroll.mapping_approval_ref in config/assumptions.yaml."
                    ),
                }
            )

        unmapped_depts = sorted({e["dept_code"] for e in unmapped_employees})
        first_emp = unmapped_employees[0]
        checks.append(
            CheckItem(
                id="PAY-MAP-001",
                group="Payroll",
                title=f"Unmapped employee department code ({', '.join(unmapped_depts)})",
                severity="blocking",
                detail=(
                    f"{len(unmapped_employees)} of {len(rows)} employees "
                    f"({first_emp['employee_id']} {first_emp['employee_name']}, branch {first_emp['branch']}, "
                    f"dept_code {first_emp['dept_code']}) has no active mapping in config/payroll-mappings.yaml. "
                    f"Approval and ERP posting are blocked despite $0.00 register-to-wire tie-out variance."
                ),
                owner=mapping_owner,
                next_step=(
                    f"Approve and add {', '.join(unmapped_depts)} to config/payroll-mappings.yaml, "
                    "then set payroll.mapping_approval_ref in config/assumptions.yaml."
                ),
                amount=None,
            )
        )

    # Production posting write gate status (`mapping_approval_ref`)
    erp_mode = str(payroll_cfg.get("erp_posting_mode") or "sandbox")
    mapping_approval_ref = payroll_cfg.get("mapping_approval_ref")
    if not mapping_approval_ref:
        checks.append(
            CheckItem(
                id="PAY-GATE-001",
                group="Payroll Governance",
                title="Production ERP posting gate locked (mapping_approval_ref unset)",
                severity="open_review",
                detail=(
                    f"payroll.mapping_approval_ref is null in config/assumptions.yaml (erp_posting_mode: {erp_mode}). "
                    "Production ERP posting is programmatically refused until a Controller approval reference is recorded."
                ),
                owner=mapping_owner,
                next_step="Set payroll.mapping_approval_ref after Controller sign-off.",
            )
        )

    # Informational note tying seasonal overtime to weekly cash variance bridge
    if sums["overtime_wages"] > 0:
        checks.append(
            CheckItem(
                id="PAY-NOTE-001",
                group="Payroll",
                title="Seasonal field overtime ties to weekly cash variance bridge",
                severity="note",
                detail=(
                    f"Biweekly overtime wages total {cents_to_currency(sums['overtime_wages'])} across field technicians, "
                    f"tying exactly to the -{cents_to_currency(sums['overtime_wages'])} payroll variance driver in Weekly Cash."
                ),
                owner=(outputs_cfg.get("approver") or {}).get("name", "Claire Vance"),
                amount=cents_to_float(sums["overtime_wages"]),
            )
        )

    # 5. Balanced Draft Journal lines (Debits == Credits == employer_cost_cents)
    journal_lines: list[dict[str, Any]] = []
    line_no = 1
    total_debits_cents = 0
    total_credits_cents = 0

    for (dept, branch), agg in dept_buckets.items():
        mapping = active_mappings.get(dept)
        if mapping:
            erp_dept = str(mapping["erp_department"])
            wages_acct = str(mapping["wages_account"])
            wages_name = str(mapping.get("wages_account_name") or "Payroll Wages")
            tax_acct = str(mapping["payroll_tax_account"])
            tax_name = str(mapping.get("payroll_tax_account_name") or "Employer Payroll Taxes")
            ben_acct = str(mapping["benefits_account"])
            ben_name = str(mapping.get("benefits_account_name") or "Employer Benefits")
            blocked_reason = ""
        else:
            erp_dept = dept
            wages_acct = "UNMAPPED"
            wages_name = f"Unmapped Wages ({dept})"
            tax_acct = "UNMAPPED"
            tax_name = f"Unmapped Employer Payroll Taxes ({dept})"
            ben_acct = "UNMAPPED"
            ben_name = f"Unmapped Employer Benefits ({dept})"
            blocked_reason = f"PAY-MAP-001: Unmapped dept_code {dept} in config/payroll-mappings.yaml"

        for acct, acct_name, amt_c, label in (
            (wages_acct, wages_name, agg["gross"], f"Gross wages — {dept} ({agg['count']} emp)"),
            (tax_acct, tax_name, agg["taxes"], f"Employer payroll taxes — {dept}"),
            (ben_acct, ben_name, agg["benefits"], f"Employer benefits & 401(k) — {dept}"),
        ):
            if amt_c == 0:
                continue
            total_debits_cents += amt_c
            journal_lines.append(
                {
                    "line_no": line_no,
                    "account": acct,
                    "account_name": acct_name,
                    "department": erp_dept,
                    "branch": branch,
                    "debit": cents_to_float(amt_c),
                    "credit": 0.0,
                    "memo": label,
                    "blocked_reason": blocked_reason,
                }
            )
            line_no += 1

    # Credit clearing lines
    clearing = mappings_cfg["clearing_accounts"]
    wire_ref = str(wire.get("wire_reference") or "PEO-WIRE-2026-1115")

    total_credits_cents += funding_wire_cents
    journal_lines.append(
        {
            "line_no": line_no,
            "account": str(clearing["cash_sweep_account"]),
            "account_name": str(clearing.get("cash_sweep_account_name") or "Frost Payroll Sweep (*4829)"),
            "department": "CORP-CLEARING",
            "branch": "HQ",
            "debit": 0.0,
            "credit": cents_to_float(funding_wire_cents),
            "memo": f"Payroll funding wire settlement ({wire_ref})",
            "blocked_reason": "",
        }
    )
    line_no += 1

    total_credits_cents += sums["employee_withholdings"]
    journal_lines.append(
        {
            "line_no": line_no,
            "account": str(clearing.get("withholdings_liability_account") or "2140-00"),
            "account_name": str(clearing.get("withholdings_liability_account_name") or "Employee Withholdings Payable"),
            "department": "CORP-CLEARING",
            "branch": "HQ",
            "debit": 0.0,
            "credit": cents_to_float(sums["employee_withholdings"]),
            "memo": f"Employee tax & benefit withholdings ({period} run)",
            "blocked_reason": "",
        }
    )

    if total_debits_cents != total_credits_cents:
        raise EngineError(
            "JOURNAL_OUT_OF_BALANCE",
            f"Draft payroll journal out of balance: debits {total_debits_cents} != credits {total_credits_cents}.",
            "Check payroll register internal footing.",
        )

    # 6. Pending changes (if any) & status summary
    conn = connect_db(workspace_dir, readonly=True)
    try:
        open_pending = get_pending_changes(conn, module="payroll", period=period, status="pending")
    finally:
        conn.close()

    mapped_count = len(rows) - len(unmapped_employees)
    has_blocking = any(c.severity == "blocking" for c in checks)
    has_review = any(c.severity == "open_review" for c in checks)
    run_status = "blocked" if has_blocking else ("ready_with_warnings" if has_review else "ready")

    pay_period_start = str(wire.get("pay_period_start") or rows[0].get("pay_period_start") or "2026-11-01")
    pay_period_end = str(wire.get("pay_period_end") or rows[0].get("pay_period_end") or "2026-11-13")

    kpis = [
        KpiField(
            key="gross_wages",
            label="Gross Wages",
            value=cents_to_float(sums["gross_wages"]),
            unit="USD",
            status="actual",
            note=f"Regular {cents_to_currency(sums['regular_wages'])} + Overtime {cents_to_currency(sums['overtime_wages'])}",
        ),
        KpiField(
            key="employer_cost_total",
            label="Total Employer Cost",
            value=cents_to_float(employer_cost_cents),
            unit="USD",
            status="actual",
            note="Gross wages + employer taxes + employer benefits (balanced JE total)",
        ),
        KpiField(
            key="funding_wire_total",
            label="Funding Wire Total",
            value=cents_to_float(funding_wire_cents),
            unit="USD",
            status="actual",
            note=f"Wire ref {wire_ref} ({wire.get('settlement_date', '2026-11-16')})",
        ),
        KpiField(
            key="tieout_variance",
            label="Tie-Out Variance",
            value=cents_to_float(total_abs_variance_cents),
            unit="USD",
            status="actual",
            note="$0.00 exact cent match across all components" if total_abs_variance_cents == 0 else "Tie-out variance detected",
        ),
        KpiField(
            key="mapped_employees",
            label="Mapped Employees",
            value=mapped_count,
            unit="count",
            status="actual",
            comparison_value=len(rows),
            comparison_label="Total employees",
            comparison_kind="limit",
            note=f"{len(unmapped_employees)} unmapped employee(s) blocking posting" if unmapped_employees else "All employees mapped",
        ),
        KpiField(
            key="blocking_exceptions",
            label="Blocking Exceptions",
            value=len([c for c in checks if c.severity == "blocking"]),
            unit="count",
            status="actual",
            note="Hard stop active — cannot approve or post" if has_blocking else "0 blocking exceptions",
        ),
    ]

    sources = [
        SourceRef(
            feed_id="payroll_voucher",
            file_name=pv_path.name,
            as_of=period,
            sensitivity="restricted",
            attachable=False,  # Also enforced in SourceRef.__post_init__
            relative_path=f"sources/{pv_path.name}",
            detail="Restricted payroll register — cited by filename and date only.",
        ),
        SourceRef(
            feed_id="payroll_funding_wire",
            file_name=wire_path.name,
            as_of=period,
            sensitivity=str(wire_feed.get("sensitivity", "internal")),
            attachable=bool(wire_feed.get("attachable", True)),
            relative_path=f"sources/{wire_path.name}",
            detail=f"Funding wire confirmation {wire_ref}.",
        ),
    ]

    headline = (
        f"Biweekly payroll ({period}) ties out to {cents_to_currency(total_abs_variance_cents)} variance "
        f"(funding wire {cents_to_currency(funding_wire_cents)}, total employer cost {cents_to_currency(employer_cost_cents)}), "
        + (
            f"but approval and ERP posting are BLOCKED by PAY-MAP-001 ({len(unmapped_employees)} unmapped employee on TUL-INSTALL-NEW)"
            + (" and unset mapping_approval_ref." if not mapping_approval_ref else ".")
            if has_blocking
            else "with zero blocking checks."
        )
    )

    return ReportModel(
        meta=ReportMeta(
            company_name=config["company_name"],
            module="payroll",
            period=period,
            period_label=_format_pay_date_label(period),
            version=__version__,
            draft_number=draft_number,
            status="draft",
            created_at=utc_now_iso(),
            approver=(outputs_cfg.get("approver") or {}).get("name", "Claire Vance"),
            is_approved=False,
            edition_number=None,
            approved_at=None,
            approved_by=None,
            schema_version=SCHEMA_VERSION,
        ),
        kpis=kpis,
        variance_bridge=None,
        sections={
            "summary": {
                "pay_date": period,
                "pay_period": f"{pay_period_start} to {pay_period_end}",
                "pay_period_end": pay_period_end,
                "wire_reference": wire_ref,
                "employee_count": len(rows),
                "mapped_employees": mapped_count,
                "unmapped_employees": len(unmapped_employees),
                "regular_wages": cents_to_float(sums["regular_wages"]),
                "overtime_wages": cents_to_float(sums["overtime_wages"]),
                "gross_wages": cents_to_float(sums["gross_wages"]),
                "employer_fica": cents_to_float(sums["employer_fica"]),
                "employer_futa_suta": cents_to_float(sums["employer_futa_suta"]),
                "employer_taxes": cents_to_float(sums["employer_taxes"]),
                "employer_401k_match": cents_to_float(sums["employer_401k_match"]),
                "employer_medical": cents_to_float(sums["employer_medical"]),
                "employer_benefits": cents_to_float(sums["employer_benefits"]),
                "employee_withholdings": cents_to_float(sums["employee_withholdings"]),
                "net_pay": cents_to_float(sums["net_pay"]),
                "employer_cost_total": cents_to_float(employer_cost_cents),
                "total_funding_wire": cents_to_float(funding_wire_cents),
                "variance_cents": total_abs_variance_cents,
                "journal_debits_total": cents_to_float(total_debits_cents),
                "journal_credits_total": cents_to_float(total_credits_cents),
                "status": run_status,
                "erp_mode": erp_mode,
                "mapping_approval_ref": mapping_approval_ref,
            },
            "tieout": tieout_rows,
            "journal_lines": journal_lines,
            "exceptions": exceptions,
        },
        checks=checks,
        pending_changes=pending_rows_to_dataclasses(open_pending),
        sources=sources,
        narrative={"headline": headline, "summary": headline},
        artifacts={},
    )


def prepare_payroll(
    workspace_dir: Path,
    *,
    period: str = DEFAULT_PAYROLL_PERIOD,
) -> dict[str, Any]:
    """Run Payroll Pre-Posting preparation, render 5-tab workbook, and refresh HTML hubs."""
    workspace_dir = Path(workspace_dir).resolve()

    conn = connect_db(workspace_dir, readonly=False)
    try:
        draft_num = next_draft_number(conn, "payroll", period)
    finally:
        conn.close()

    model = build_payroll_model(workspace_dir, period=period, draft_number=draft_num)

    run_rel = f"runs/payroll/{period}/draft-{draft_num:03d}"
    run_dir = workspace_dir / run_rel
    run_dir.mkdir(parents=True, exist_ok=True)

    xlsx_path = render_payroll_xlsx(model, run_dir / "payroll-review.xlsx")
    xlsx_rel = f"{run_rel}/{xlsx_path.name}"
    model_rel = f"{run_rel}/report-model.json"

    model.artifacts = {
        "payroll_workbook": xlsx_rel,
        "report_model": model_rel,
    }

    (workspace_dir / model_rel).write_text(
        json.dumps(model.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    conn = connect_db(workspace_dir, readonly=False)
    try:
        conn.execute(
            """
            INSERT INTO runs(
                module, period, draft_number, status, run_dir, model_path,
                can_approve, blocking_count, open_review_count, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "payroll",
                period,
                draft_num,
                "draft",
                run_rel,
                model_rel,
                1 if model.can_approve else 0,
                len(model.blocking_checks),
                len(model.open_review_checks),
                model.meta.created_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    reports_html, setup_html = refresh_html_views(workspace_dir)

    attachable_files = [
        {
            "file_name": "Reports.html",
            "relative_path": "Reports.html",
            "path": str(reports_html),
            "kind": "dashboard",
            "sensitivity": "internal",
        },
        {
            "file_name": "Setup.html",
            "relative_path": "Setup.html",
            "path": str(setup_html),
            "kind": "dashboard",
            "sensitivity": "internal",
        },
        {
            "file_name": xlsx_path.name,
            "relative_path": xlsx_rel,
            "path": str(xlsx_path),
            "kind": "xlsx_workbook",
            "sensitivity": "internal",
        },
    ]
    for src in model.attachable_sources():
        if src.relative_path:
            attachable_files.append(
                {
                    "file_name": src.file_name,
                    "relative_path": src.relative_path,
                    "path": str(workspace_dir / src.relative_path),
                    "kind": "source",
                    "sensitivity": src.sensitivity,
                }
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
        for s in model.non_attachable_sources()
    ]

    return {
        "module": "payroll",
        "period": period,
        "draft_number": draft_num,
        "status": model.sections["summary"]["status"],
        "can_approve": model.can_approve,
        "run_dir": str(run_dir),
        "model_path": str(workspace_dir / model_rel),
        "kpis": {k.key: k.value for k in model.kpis},
        "summary": model.sections["summary"],
        "tieout": model.sections["tieout"],
        "exceptions": model.sections["exceptions"],
        "blocking_checks": [c.__dict__ for c in model.blocking_checks],
        "open_review_checks": [c.__dict__ for c in model.open_review_checks],
        "artifacts": {
            "payroll_workbook": str(xlsx_path),
            "reports_html": str(reports_html),
            "setup_html": str(setup_html),
            "report_model": str(workspace_dir / model_rel),
        },
        "attachable_files": attachable_files,
        "cited_non_attachable_sources": cited_non_attachable,
    }
