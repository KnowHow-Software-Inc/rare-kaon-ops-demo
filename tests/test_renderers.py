"""Unit and parity tests for single-model PDF, HTML, and XLSX renderers."""

from __future__ import annotations

import ast
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from engine.contracts import COMPANY_NAME, find_forbidden_matches
from engine.model import (
    BridgeDriver,
    CheckItem,
    KpiField,
    PendingChange,
    ReportMeta,
    ReportModel,
    SourceRef,
    VarianceBridge,
)
from engine.render.html import (
    format_kpi_value as html_format_kpi_value,
    render_reports_html,
    render_setup_html,
)
from engine.render.pdf import (
    format_kpi_value as pdf_format_kpi_value,
    render_weekly_pdfs,
)
from engine.render.xlsx import render_payroll_xlsx


# ---------------------------------------------------------------------------
# Realistic synthetic ReportModel builders (100% clean-room Heartland data)
# ---------------------------------------------------------------------------


def _build_13_weeks(*, revised: bool = False) -> list[dict[str, Any]]:
    week_endings = [
        "2026-11-20",
        "2026-11-27",
        "2026-12-04",
        "2026-12-11",
        "2026-12-18",
        "2026-12-25",
        "2027-01-01",
        "2027-01-08",
        "2027-01-15",
        "2027-01-22",
        "2027-01-29",
        "2027-02-05",
        "2027-02-12",
    ]
    inflows = [
        215000.00, 198500.00, 240000.00, 185000.00, 210000.00,
        160000.00, 172000.00, 225000.00, 232000.00, 195000.00,
        248000.00, 212000.00, 238000.00,
    ]
    outflows = [
        248250.00, 186400.00, 265000.00, 192000.00, 235000.00,
        198000.00, 224550.00, 188000.00, 205000.00, 182000.00,
        214000.00, 191000.00, 199000.00,
    ]
    notes = [
        "Scheduled AP batch + Lone Star Supply LSS-2026-4189",
        "Bi-weekly payroll sweep & routine supplier runs",
        "Monthly equipment lease & commercial rent",
        "Standard HVAC material replenishment",
        "Bi-weekly payroll sweep & fleet fuel auto-draft",
        "Holiday week reduced collections",
        "Year-end insurance installment & payroll sweep",
        "Q1 preventive maintenance billings start",
        "Bi-weekly payroll sweep & quarterly tax deposit",
        "Routine vendor disbursements",
        "Bi-weekly payroll sweep & progress billings",
        "Monthly equipment lease & commercial rent",
        "Routine operations & service collections",
    ]
    if revised:
        outflows[0] = round(outflows[0] - 25000.00, 2)
        outflows[1] = round(outflows[1] + 25000.00, 2)
        notes[0] = "LSS-2026-4189 ($25,000) deferred to W2 (CHG-001)"
        notes[1] = "Includes deferred LSS-2026-4189 ($25,000) from W1"

    opening = 1840000.00
    rows: list[dict[str, Any]] = []
    for idx in range(13):
        net = round(inflows[idx] - outflows[idx], 2)
        ending = round(opening + net, 2)
        rows.append(
            {
                "week_num": idx + 1,
                "week_ending": week_endings[idx],
                "opening_cash": opening,
                "inflows": inflows[idx],
                "outflows": outflows[idx],
                "net_cash": net,
                "ending_cash": ending,
                "notes": notes[idx],
            }
        )
        opening = ending
    return rows


def _make_weekly_model(*, revised: bool = False, approved: bool = False) -> ReportModel:
    weeks = _build_13_weeks(revised=revised)
    w1_end = weeks[0]["ending_cash"]
    w13_end = weeks[-1]["ending_cash"]
    min_wk = min(weeks, key=lambda w: w["ending_cash"])

    revisions: list[dict[str, Any]] = []
    if revised:
        revisions.append(
            {
                "change_id": "CHG-001",
                "invoice_ref": "LSS-2026-4189",
                "vendor": "Lone Star Supply Co.",
                "amount": 25000.00,
                "old_due_date": "2026-11-20",
                "new_due_date": "2026-11-27",
                "old_week": 1,
                "new_week": 2,
                "confirmed_by": "Claire Vance",
                "confirmed_at": "2026-11-20T15:10:00Z",
                "source_file": "lone-star-payment-amendment-2026-11-20.yaml",
            }
        )

    return ReportModel(
        meta=ReportMeta(
            company_name=COMPANY_NAME,
            module="weekly",
            period="2026-11-20",
            period_label="Week ended 20 November 2026",
            version="0.1.0",
            draft_number=2 if revised else 1,
            status="approved" if approved else "draft",
            created_at="2026-11-20T15:15:00Z" if revised else "2026-11-20T14:00:00Z",
            approver="Claire Vance",
            is_approved=approved,
            edition_number=1 if approved else None,
            approved_at="2026-11-20T16:00:00Z" if approved else None,
            approved_by="Claire Vance" if approved else None,
        ),
        kpis=[
            KpiField(
                key="opening_cash",
                label="Opening Cash",
                value=1840000.00,
                unit="USD",
                status="actual",
                comparison_value=1805850.25,
                comparison_label="Prior week",
                comparison_kind="prior",
                note="Frost Operating + Payroll Sweep",
            ),
            KpiField(
                key="week1_ending_cash",
                label="Week 1 Ending Cash",
                value=w1_end,
                unit="USD",
                status="forecast",
                note="Revised +$25,000 from deferral" if revised else "Baseline W1 projection",
            ),
            KpiField(
                key="week13_ending_cash",
                label="Week 13 Ending Cash",
                value=w13_end,
                unit="USD",
                status="forecast",
                note="Unchanged by W1→W2 timing shift",
            ),
            KpiField(
                key="min_cash_balance",
                label="Minimum Projected Cash",
                value=min_wk["ending_cash"],
                unit="USD",
                status="forecast",
                comparison_value=1500000.00,
                comparison_label="Cash floor",
                comparison_kind="limit",
                note=f"Occurs in Week {min_wk['week_num']} ({min_wk['week_ending']})",
            ),
            KpiField(
                key="ar_total",
                label="Total Accounts Receivable",
                value=1482650.00,
                unit="USD",
                status="actual",
                note="As of 2026-11-18 snapshot",
            ),
            KpiField(
                key="ar_over_60",
                label="Overdue A/R (>60 Days)",
                value=142350.00,
                unit="USD",
                status="actual",
                note="3 invoices across 61–90 and >90",
            ),
        ],
        variance_bridge=VarianceBridge(
            baseline_period="2026-11-13",
            baseline_label="Issued Edition 1 (13 Nov 2026)",
            shared_date="2026-11-20",
            prior_value=1805850.25,
            current_value=1840000.00,
            delta=34149.75,
            drivers=[
                BridgeDriver(
                    key="early_ar",
                    label="Early collection — Methodist Health Facilities",
                    amount=64250.00,
                    source_file="frost-operating-2026-11-20.csv",
                    source_date="2026-11-18",
                    source_ref="INV-2026-8814",
                    note="Collected 5 days ahead of forecast",
                ),
                BridgeDriver(
                    key="emergency_parts",
                    label="Unplanned chiller compressor order — Red River Parts",
                    amount=-18900.25,
                    source_file="frost-operating-2026-11-20.csv",
                    source_date="2026-11-19",
                    source_ref="PO-77412",
                    note="Emergency Tulsa branch dispatch",
                ),
                BridgeDriver(
                    key="routine_timing",
                    label="Net routine AP & clearing timing variance",
                    amount=-11200.00,
                    source_file="frost-operating-2026-11-20.csv",
                    source_date="2026-11-20",
                    source_ref="BATCH-1120",
                ),
            ],
            moved_payments=[
                {
                    "invoice_ref": "LSS-2026-4189",
                    "vendor": "Lone Star Supply Co.",
                    "amount": 25000.00,
                    "old_week": 1,
                    "new_week": 2,
                }
            ]
            if revised
            else [],
        ),
        sections={
            "cash_accounts": [
                {
                    "account_id": "frost_operating",
                    "name": "Frost Bank Operating",
                    "bank": "Frost Bank",
                    "mask": "****4410",
                    "closing_balance": 1715000.00,
                    "as_of": "2026-11-20",
                    "source_file": "frost-operating-2026-11-20.csv",
                },
                {
                    "account_id": "frost_payroll_sweep",
                    "name": "Frost Bank Payroll Sweep",
                    "bank": "Frost Bank",
                    "mask": "****4428",
                    "closing_balance": 125000.00,
                    "as_of": "2026-11-20",
                    "source_file": "frost-payroll-sweep-2026-11-20.csv",
                },
            ],
            "forecast_weeks": weeks,
            "ar_aging": {
                "as_of": "2026-11-18",
                "source_file": "ar-aging-2026-11-18.xlsx",
                "total": 1482650.00,
                "buckets": {
                    "current": 864200.00,
                    "d1_30": 312100.00,
                    "d31_60": 164000.00,
                    "d61_90": 84150.00,
                    "over_90": 58200.00,
                },
                "overdue_items": [
                    {
                        "invoice_id": "INV-2026-7920",
                        "customer": "Prairie Medical Center",
                        "branch": "Tulsa",
                        "invoice_date": "2026-08-25",
                        "due_date": "2026-09-24",
                        "days_past_due": 55,
                        "bucket": "61-90",
                        "amount": 48900.00,
                        "note": "Remittance scheduled for 2026-11-25",
                    },
                    {
                        "invoice_id": "INV-2026-7845",
                        "customer": "Cedar Ridge Logistics",
                        "branch": "Oklahoma City",
                        "invoice_date": "2026-08-19",
                        "due_date": "2026-09-18",
                        "days_past_due": 61,
                        "bucket": "61-90",
                        "amount": 35250.00,
                        "note": "PO revision confirmed by customer AP",
                    },
                    {
                        "invoice_id": "INV-2026-7410",
                        "customer": "Midwest Cold Storage",
                        "branch": "Dallas",
                        "invoice_date": "2026-07-02",
                        "due_date": "2026-08-01",
                        "days_past_due": 109,
                        "bucket": ">90",
                        "amount": 58200.00,
                        "note": "Warranty retainage sign-off pending",
                    },
                ],
            },
            "revisions": revisions,
        },
        checks=[
            CheckItem(
                id="WK-REV-001",
                group="Weekly Cash",
                title="Estimated fleet fuel card auto-draft",
                severity="open_review",
                detail="Placeholder $14,200.00 used pending monthly fleet statement.",
                owner="Claire Vance",
                next_step="Replace with final statement once posted on 2026-11-24.",
                amount=14200.00,
            ),
            CheckItem(
                id="WK-NOTE-001",
                group="Weekly Cash",
                title="A/R snapshot precedes bank date by 2 days",
                severity="note",
                detail="A/R aging is dated 2026-11-18 while bank balances are 2026-11-20.",
                owner="Marcus Holt",
            ),
        ],
        pending_changes=[]
        if revised
        else [
            PendingChange(
                id="CHG-001",
                period="2026-11-20",
                module="weekly",
                kind="payment_deferral",
                summary="Defer Lone Star Supply LSS-2026-4189 ($25,000.00) from Week 1 to Week 2.",
                source_file="lone-star-payment-amendment-2026-11-20.yaml",
            )
        ],
        sources=[
            SourceRef(
                feed_id="frost_operating",
                file_name="frost-operating-2026-11-20.csv",
                as_of="2026-11-20",
                sensitivity="internal",
                attachable=True,
                relative_path="sources/frost-operating-2026-11-20.csv",
            ),
            SourceRef(
                feed_id="ar_aging",
                file_name="ar-aging-2026-11-18.xlsx",
                as_of="2026-11-18",
                sensitivity="internal",
                attachable=True,
                relative_path="sources/ar-aging-2026-11-18.xlsx",
            ),
            SourceRef(
                feed_id="payroll_voucher",
                file_name="payroll-voucher-2026-11-15.csv",
                as_of="2026-11-15",
                sensitivity="restricted",
                attachable=True,  # Forced to False by SourceRef.__post_init__
                relative_path="sources/SECRET-DO-NOT-LEAK/payroll-voucher-2026-11-15.csv",
                row_refs=["EMP-1048-SSN-REDACTED"],
            ),
        ],
        narrative={
            "headline": "Opening cash stands at $1,840,000.00 (+$34,149.75 vs. prior issued edition) with all 13 forecast weeks above the $1,500,000.00 minimum cash floor."
        },
        artifacts={
            "liquidity_pdf": "runs/weekly/2026-11-20/draft-002/Weekly-Liquidity-Update.pdf"
            if revised
            else "runs/weekly/2026-11-20/draft-001/Weekly-Liquidity-Update.pdf",
            "cash_flow_pdf": "runs/weekly/2026-11-20/draft-002/13-Week-Cash-Flow.pdf"
            if revised
            else "runs/weekly/2026-11-20/draft-001/13-Week-Cash-Flow.pdf",
        },
    )


def _make_payroll_model(*, blocked: bool = True) -> ReportModel:
    checks: list[CheckItem] = [
        CheckItem(
            id="PAY-REV-001",
            group="Payroll",
            title="Overtime spike in Dallas Service branch",
            severity="open_review",
            detail="Dallas overtime is 14.2% above 4-period average due to cold-snap dispatches.",
            owner="Devon Brooks",
            next_step="Confirm branch manager timesheet approval.",
            amount=6420.50,
        )
    ]
    exceptions: list[dict[str, Any]] = []
    journal_lines: list[dict[str, Any]] = [
        {
            "line_no": 1,
            "account": "5100",
            "account_name": "Direct Field Wages — Service",
            "department": "DAL-SERVICE",
            "branch": "Dallas",
            "debit": 112450.00,
            "credit": 0.00,
            "memo": "Bi-weekly payroll 2026-11-15",
            "blocked_reason": "",
        },
        {
            "line_no": 2,
            "account": "UNMAPPED" if blocked else "5120",
            "account_name": "Unmapped Department Expense" if blocked else "Direct Field Wages — Install",
            "department": "TUL-INSTALL-NEW",
            "branch": "Tulsa",
            "debit": 18750.00,
            "credit": 0.00,
            "memo": "EMP-1048 new Tulsa install crew",
            "blocked_reason": "Unmapped dept_code TUL-INSTALL-NEW in config/payroll-mappings.yaml" if blocked else "",
        },
        {
            "line_no": 3,
            "account": "5200",
            "account_name": "Employer Payroll Taxes & Benefits",
            "department": "CORP-SHARED",
            "branch": "HQ",
            "debit": 24890.40,
            "credit": 0.00,
            "memo": "Employer FICA, FUTA, SUTA, medical",
            "blocked_reason": "",
        },
        {
            "line_no": 4,
            "account": "1020",
            "account_name": "Frost Bank Payroll Sweep",
            "department": "CORP-SHARED",
            "branch": "HQ",
            "debit": 0.00,
            "credit": 156090.40,
            "memo": "Total payroll funding wire",
            "blocked_reason": "",
        },
    ]

    if blocked:
        checks.insert(
            0,
            CheckItem(
                id="PAY-MAP-001",
                group="Payroll",
                title="Unmapped employee department code",
                severity="blocking",
                detail="Employee EMP-1048 uses unmapped department code TUL-INSTALL-NEW ($18,750.00).",
                owner="Claire Vance",
                next_step="Add GL mapping for TUL-INSTALL-NEW and record mapping_approval_ref.",
                amount=18750.00,
            ),
        )
        exceptions.append(
            {
                "code": "UNMAPPED_DEPT",
                "employee_id": "EMP-1048",
                "employee_name": "Jordan Ramirez",
                "branch": "Tulsa",
                "dept_code": "TUL-INSTALL-NEW",
                "severity": "blocking",
                "detail": "No GL account mapping configured for TUL-INSTALL-NEW.",
                "remediation": "Approve and add mapping to config/payroll-mappings.yaml before posting.",
            }
        )

    return ReportModel(
        meta=ReportMeta(
            company_name=COMPANY_NAME,
            module="payroll",
            period="2026-11-15",
            period_label="Pay date 15 November 2026",
            version="0.1.0",
            draft_number=1,
            status="draft" if blocked else "approved",
            created_at="2026-11-16T09:30:00Z",
            approver="Claire Vance",
            is_approved=not blocked,
            edition_number=None if blocked else 1,
            approved_at=None if blocked else "2026-11-16T11:00:00Z",
            approved_by=None if blocked else "Claire Vance",
        ),
        kpis=[
            KpiField(key="gross_wages", label="Gross Wages", value=131200.00, unit="USD", status="actual"),
            KpiField(key="employer_cost_total", label="Total Employer Cost", value=156090.40, unit="USD", status="actual"),
            KpiField(key="funding_wire_total", label="Funding Wire Total", value=156090.40, unit="USD", status="actual"),
            KpiField(key="tieout_variance", label="Tie-Out Variance", value=0.00, unit="USD", status="actual", note="$0.00 exact cent match"),
            KpiField(key="mapped_employees", label="Mapped Employees", value=47 if blocked else 48, unit="count", status="actual", comparison_value=48, comparison_label="Total employees", comparison_kind="limit"),
            KpiField(key="blocking_exceptions", label="Blocking Exceptions", value=1 if blocked else 0, unit="count", status="actual"),
        ],
        sections={
            "summary": {
                "pay_date": "2026-11-15",
                "pay_period": "2026-11-01 to 2026-11-14",
                "employee_count": 48,
                "gross_wages": 131200.00,
                "employer_taxes": 11490.40,
                "employer_benefits": 13400.00,
                "employee_withholdings": 34610.00,
                "net_pay": 96590.00,
                "total_funding_wire": 156090.40,
                "variance_cents": 0,
                "status": "blocked" if blocked else "ready",
                "erp_mode": "sandbox" if blocked else "production",
                "mapping_approval_ref": None if blocked else "APR-2026-1116-CV",
            },
            "tieout": [
                {"component": "Net Employee Pay", "register_amount": 96590.00, "funding_amount": 96590.00, "difference": 0.00, "status": "pass"},
                {"component": "Employee Tax & Benefit Withholdings", "register_amount": 34610.00, "funding_amount": 34610.00, "difference": 0.00, "status": "pass"},
                {"component": "Employer Payroll Taxes", "register_amount": 11490.40, "funding_amount": 11490.40, "difference": 0.00, "status": "pass"},
                {"component": "Employer Benefit Contributions", "register_amount": 13400.00, "funding_amount": 13400.00, "difference": 0.00, "status": "pass"},
                {"component": "Total Payroll Funding Wire", "register_amount": 156090.40, "funding_amount": 156090.40, "difference": 0.00, "status": "pass"},
            ],
            "journal_lines": journal_lines,
            "exceptions": exceptions,
        },
        checks=checks,
        sources=[
            SourceRef(
                feed_id="payroll_voucher",
                file_name="payroll-voucher-2026-11-15.csv",
                as_of="2026-11-15",
                sensitivity="restricted",
                attachable=True,
                relative_path="sources/RESTRICTED-PATH/payroll-voucher-2026-11-15.csv",
            ),
            SourceRef(
                feed_id="payroll_funding_wire",
                file_name="payroll-funding-wire-2026-11-15.yaml",
                as_of="2026-11-15",
                sensitivity="internal",
                attachable=True,
                relative_path="sources/payroll-funding-wire-2026-11-15.yaml",
            ),
        ],
        artifacts={
            "payroll_workbook": "runs/payroll/2026-11-15/draft-001/payroll-review.xlsx",
        },
    )


def _sample_config() -> dict[str, Any]:
    return {
        "company_name": COMPANY_NAME,
        "feeds": {
            "feeds": [
                {
                    "id": "frost_operating",
                    "name": "Frost Bank Operating Account Export",
                    "module": "weekly",
                    "provider": "Frost Bank Treasury Portal",
                    "owner": "Marcus Holt",
                    "where": "Treasury Export Folder -> Operating CSV",
                    "due_rule": "Fridays 10:00 CT",
                    "sensitivity": "internal",
                    "column_aliases": {
                        "post_date": ["Posting Date", "Trans Date"],
                        "amount": ["Amount (USD)", "Net Amount"],
                    },
                    "active_from": "2025-01-01",
                    "active_until": None,
                    "replaced_by_id": None,
                },
                {
                    "id": "legacy_ar_export",
                    "name": "Legacy Dispatch Billing A/R Export",
                    "module": "weekly",
                    "provider": "Legacy Dispatch System",
                    "owner": "Marcus Holt",
                    "where": "Shared Finance Drive -> Archive",
                    "due_rule": "Wednesdays 16:00 CT",
                    "sensitivity": "internal",
                    "column_aliases": {
                        "invoice_id": ["Inv #", "Invoice No"],
                    },
                    "active_from": "2024-01-01",
                    "active_until": "2026-12-31",
                    "replaced_by_id": "ar_aging_v2",
                },
                {
                    "id": "payroll_voucher",
                    "name": "Bi-Weekly Payroll Register Voucher",
                    "module": "payroll",
                    "provider": "Heartland Payroll Bureau",
                    "owner": "Devon Brooks",
                    "where": "Restricted Payroll Vault",
                    "due_rule": "1st and 15th of month",
                    "sensitivity": "restricted",
                    "column_aliases": {
                        "dept_code": ["Cost Center", "Dept"],
                        "gross_pay": ["Gross Wages", "Gross"],
                    },
                    "active_from": "2025-01-01",
                    "active_until": None,
                    "replaced_by_id": None,
                },
            ]
        },
        "assumptions": {
            "weekly": {
                "cash_floor_usd": 1500000.00,
                "forecast_horizon_weeks": 13,
            },
            "payroll": {
                "tieout_tolerance_cents": 0,
                "erp_posting_mode": "sandbox",
                "mapping_approval_ref": None,
            },
        },
        "outputs": {
            "send_mode": "draft_only",
            "approver": {
                "name": "Claire Vance",
                "role": "VP Finance",
            },
            "distribution": ["Executive Leadership (Draft Review Only)"],
        },
    }


def _sample_audit_entries() -> list[dict[str, Any]]:
    return [
        {
            "timestamp": "2026-11-18T11:20:00Z",
            "actor": "Claire Vance",
            "field_path": "assumptions.weekly.cash_floor",
            "old_value": 1250000.00,
            "new_value": 1500000.00,
            "reason": "Board Q4 liquidity covenant floor update.",
        },
        {
            "timestamp": "2026-11-20T15:10:00Z",
            "actor": "Claire Vance",
            "field_path": "pending_change.CHG-001",
            "old_value": "Week 1 (2026-11-20)",
            "new_value": "Week 2 (2026-11-27)",
            "reason": "Confirmed Lone Star Supply LSS-2026-4189 deferral.",
        },
    ]


def _count_pdf_pages(pdf_path: Path) -> int:
    data = pdf_path.read_bytes()
    assert data.startswith(b"%PDF-")
    # ReportLab emits uncompressed `/Type /Page` dictionaries (distinct from `/Type /Pages`).
    return len(re.findall(rb"/Type\s*/Page\b(?!s)", data))


# ---------------------------------------------------------------------------
# PDF renderer tests
# ---------------------------------------------------------------------------


def test_render_weekly_pdfs_draft_and_revised(tmp_path: Path) -> None:
    draft_dir = tmp_path / "draft-001"
    draft_model = _make_weekly_model(revised=False)
    draft_out = render_weekly_pdfs(draft_model, draft_dir)

    assert set(draft_out.keys()) == {"liquidity_pdf", "cash_flow_pdf"}
    assert draft_out["liquidity_pdf"].name == "Weekly-Liquidity-Update.pdf"
    assert draft_out["cash_flow_pdf"].name == "13-Week-Cash-Flow.pdf"
    assert draft_out["liquidity_pdf"].stat().st_size > 2000
    assert draft_out["cash_flow_pdf"].stat().st_size > 2000
    assert 1 <= _count_pdf_pages(draft_out["liquidity_pdf"]) <= 2
    assert _count_pdf_pages(draft_out["cash_flow_pdf"]) == 1

    rev_dir = tmp_path / "draft-002"
    rev_model = _make_weekly_model(revised=True, approved=True)
    rev_out = render_weekly_pdfs(rev_model, rev_dir)

    assert rev_out["liquidity_pdf"].stat().st_size > 2000
    assert rev_out["cash_flow_pdf"].stat().st_size > 2000
    assert 1 <= _count_pdf_pages(rev_out["liquidity_pdf"]) <= 2
    assert _count_pdf_pages(rev_out["cash_flow_pdf"]) == 1


# ---------------------------------------------------------------------------
# HTML renderer tests (single-model parity & zero external URLs)
# ---------------------------------------------------------------------------


def test_render_reports_html_single_model_parity_and_privacy(tmp_path: Path) -> None:
    weekly_draft = _make_weekly_model(revised=False, approved=False)
    weekly_approved = _make_weekly_model(revised=True, approved=True)
    payroll_blocked = _make_payroll_model(blocked=True)
    models = [weekly_draft, weekly_approved, payroll_blocked]

    out_path = render_reports_html(models, _sample_config(), tmp_path)
    assert out_path == tmp_path / "Reports.html"
    html_text = out_path.read_text(encoding="utf-8")

    # Zero external CDN / web dependencies
    assert "http://" not in html_text
    assert "https://" not in html_text

    # Single-model KPI parity: every KPI formatted value appears verbatim in Reports.html
    for model in models:
        for kpi in model.kpis:
            formatted = html_format_kpi_value(kpi)
            assert formatted == pdf_format_kpi_value(kpi)
            assert formatted in html_text, f"Missing KPI {kpi.key}={formatted} in Reports.html"
            assert f'data-kpi-key="{kpi.key}"' in html_text

    # Draft vs Approved edition badges
    assert "Draft 1" in html_text
    assert "Approved &middot; Edition 1" in html_text

    # Blocking vs Open Review badges strictly distinguished
    assert "Blocking Checks" in html_text
    assert "Open Review Items" in html_text
    assert "YES — Blocks Approval" in html_text
    assert "NO — Non-Blocking" in html_text

    # Relative artifact links (executive deliverables only; omit internal report-model.json)
    assert 'href="runs/weekly/2026-11-20/draft-001/Weekly-Liquidity-Update.pdf"' in html_text
    assert 'href="runs/payroll/2026-11-15/draft-001/payroll-review.xlsx"' in html_text
    assert 'href="runs/weekly/2026-11-20/draft-001/report-model.json"' not in html_text
    assert 'href="runs/payroll/2026-11-15/draft-001/report-model.json"' not in html_text

    # Restricted source badge & zero leakage of restricted path or row_refs
    assert "Non-Attachable · Citation Only" in html_text
    assert "payroll-voucher-2026-11-15.csv" in html_text
    assert "SECRET-DO-NOT-LEAK" not in html_text
    assert "EMP-1048-SSN-REDACTED" not in html_text
    assert "RESTRICTED-PATH" not in html_text


def test_render_setup_html_cutovers_governance_and_audit(tmp_path: Path) -> None:
    out_path = render_setup_html(_sample_config(), _sample_audit_entries(), tmp_path)
    assert out_path == tmp_path / "Setup.html"
    html_text = out_path.read_text(encoding="utf-8")

    # Zero external web/CDN URLs
    assert "http://" not in html_text
    assert "https://" not in html_text

    # Feed sensitivity badges, column aliases, and cutover dates
    assert "restricted · non-attachable" in html_text
    assert "Posting Date" in html_text
    assert "2026-12-31" in html_text
    assert "ar_aging_v2" in html_text
    assert "Cutover Sunset" in html_text

    # Assumptions highlights: ERP mode and mapping_approval_ref
    assert "sandbox" in html_text
    assert "mapping_approval_ref" in html_text
    assert "Production Blocked" in html_text

    # Outputs guardrail
    assert "draft_only" in html_text

    # Audit entries rendered
    assert "assumptions.weekly.cash_floor" in html_text
    assert "Board Q4 liquidity covenant floor update." in html_text
    assert "pending_change.CHG-001" in html_text
    assert "Claire Vance" in html_text


# ---------------------------------------------------------------------------
# XLSX renderer tests
# ---------------------------------------------------------------------------


def test_render_payroll_xlsx_blocked_has_five_tabs_and_warning(tmp_path: Path) -> None:
    model = _make_payroll_model(blocked=True)
    out_path = tmp_path / "payroll-review.xlsx"
    returned = render_payroll_xlsx(model, out_path)

    assert returned == out_path
    assert out_path.exists() and out_path.stat().st_size > 4000

    wb = load_workbook(out_path)
    assert wb.sheetnames == ["Runs", "Tie-Out", "Checks", "Exceptions", "Draft Journal"]

    # Draft Journal must be clearly stamped BLOCKED — DO NOT POST at the top
    ws_journal = wb["Draft Journal"]
    top_text = " ".join(
        str(ws_journal.cell(row=r, column=1).value or "") for r in range(1, 7)
    )
    assert "BLOCKED" in top_text
    assert "DO NOT POST" in top_text

    # Blocked journal line details present
    journal_vals = [
        str(cell.value)
        for row in ws_journal.iter_rows(min_row=6, values_only=False)
        for cell in row
        if cell.value is not None
    ]
    assert any("TUL-INSTALL-NEW" in v for v in journal_vals)
    assert any("Unmapped dept_code" in v for v in journal_vals)

    # Tie-Out tab preserves exact values
    ws_tieout = wb["Tie-Out"]
    tieout_cells = [
        cell.value
        for row in ws_tieout.iter_rows(values_only=False)
        for cell in row
        if cell.value is not None
    ]
    assert "Total Payroll Funding Wire" in tieout_cells
    assert any(Decimal(str(v)) == Decimal("156090.4") for v in tieout_cells if isinstance(v, (int, float, Decimal)))

    # Checks tab separates blocking vs non-blocking explicitly
    ws_checks = wb["Checks"]
    checks_text = " ".join(
        str(cell.value)
        for row in ws_checks.iter_rows(values_only=False)
        for cell in row
        if cell.value is not None
    )
    assert "PAY-MAP-001" in checks_text
    assert "YES — BLOCKING" in checks_text
    assert "NO — NON-BLOCKING" in checks_text

    # Exceptions tab contains EMP-1048
    ws_exc = wb["Exceptions"]
    exc_text = " ".join(
        str(cell.value)
        for row in ws_exc.iter_rows(values_only=False)
        for cell in row
        if cell.value is not None
    )
    assert "EMP-1048" in exc_text
    assert "TUL-INSTALL-NEW" in exc_text

    # Runs tab never leaks restricted relative path
    ws_runs = wb["Runs"]
    runs_text = " ".join(
        str(cell.value)
        for row in ws_runs.iter_rows(values_only=False)
        for cell in row
        if cell.value is not None
    )
    assert "payroll-voucher-2026-11-15.csv" in runs_text
    assert "RESTRICTED-PATH" not in runs_text


def test_render_payroll_xlsx_clear_model_not_stamped_blocked(tmp_path: Path) -> None:
    model = _make_payroll_model(blocked=False)
    out_path = tmp_path / "payroll-clear.xlsx"
    render_payroll_xlsx(model, out_path)

    wb = load_workbook(out_path)
    assert wb.sheetnames == ["Runs", "Tie-Out", "Checks", "Exceptions", "Draft Journal"]

    ws_journal = wb["Draft Journal"]
    top_text = " ".join(
        str(ws_journal.cell(row=r, column=1).value or "") for r in range(1, 7)
    )
    assert "BLOCKED — DO NOT POST" not in top_text
    assert "READY FOR POSTING REVIEW" in top_text


# ---------------------------------------------------------------------------
# Pure-Python dependency & hygiene guardrails
# ---------------------------------------------------------------------------


def test_renderers_do_not_import_pillow_or_forbidden_terms() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    owned_files = [
        repo_root / "engine/render/pdf.py",
        repo_root / "engine/render/html.py",
        repo_root / "engine/render/xlsx.py",
        repo_root / "tests/test_renderers.py",
    ]
    forbidden_imports = {"PIL", "Pillow", "numpy", "pandas"}
    for path in owned_files:
        source = path.read_text(encoding="utf-8")
        hits = find_forbidden_matches(source)
        assert hits == [], f"Forbidden hygiene matches {hits} found in {path}"
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_mod = alias.name.split(".")[0]
                    assert root_mod not in forbidden_imports, f"Forbidden import {alias.name} in {path}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                root_mod = node.module.split(".")[0]
                assert root_mod not in forbidden_imports, f"Forbidden import {node.module} in {path}"
