"""Single-model Excel payroll review workbook renderer (pure openpyxl, zero PIL).

Implements the frozen signature:
- `render_payroll_xlsx(model: ReportModel, out_path: Path) -> Path`

Writes a formatted 5-tab workbook:
  1. `Runs`
  2. `Tie-Out`
  3. `Checks`
  4. `Exceptions`
  5. `Draft Journal` (clearly stamped `BLOCKED — DO NOT POST` when `model.blocking_checks` is non-empty)

All values are read directly from `ReportModel` without recomputing business math.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from engine.model import ReportModel

TWO_PLACES = Decimal("0.01")

# ---------------------------------------------------------------------------
# Palette & reusable openpyxl styles
# ---------------------------------------------------------------------------

_FONT_FAMILY = "Calibri"

FONT_TITLE = Font(name=_FONT_FAMILY, size=14, bold=True, color="FFFFFF")
FONT_SUBTITLE = Font(name=_FONT_FAMILY, size=10, italic=False, color="E2E8F0")
FONT_SECTION = Font(name=_FONT_FAMILY, size=11, bold=True, color="162B4D")
FONT_TH = Font(name=_FONT_FAMILY, size=10, bold=True, color="162B4D")
FONT_BODY = Font(name=_FONT_FAMILY, size=10, bold=False, color="1E293B")
FONT_BODY_BOLD = Font(name=_FONT_FAMILY, size=10, bold=True, color="1E293B")
FONT_MUTED = Font(name=_FONT_FAMILY, size=9, italic=True, color="64748B")

FONT_BLOCKED_BANNER = Font(name=_FONT_FAMILY, size=13, bold=True, color="991B1B")
FONT_READY_BANNER = Font(name=_FONT_FAMILY, size=11, bold=True, color="166534")
FONT_SANDBOX_BANNER = Font(name=_FONT_FAMILY, size=11, bold=True, color="92400E")

FONT_RED = Font(name=_FONT_FAMILY, size=10, bold=True, color="991B1B")
FONT_AMBER = Font(name=_FONT_FAMILY, size=10, bold=True, color="92400E")
FONT_GREEN = Font(name=_FONT_FAMILY, size=10, bold=True, color="166534")
FONT_BLUE = Font(name=_FONT_FAMILY, size=10, bold=True, color="1E40AF")

FILL_NAVY = PatternFill(start_color="162B4D", end_color="162B4D", fill_type="solid")
FILL_TH = PatternFill(start_color="E8EEF5", end_color="E8EEF5", fill_type="solid")
FILL_ZEBRA = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
FILL_RED = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")
FILL_AMBER = PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid")
FILL_GREEN = PatternFill(start_color="DCFCE7", end_color="DCFCE7", fill_type="solid")
FILL_BLUE = PatternFill(start_color="EFF6FF", end_color="EFF6FF", fill_type="solid")

_THIN_SIDE = Side(style="thin", color="CBD5E1")
_NAVY_SIDE = Side(style="medium", color="162B4D")
_RED_SIDE = Side(style="medium", color="991B1B")
_AMBER_SIDE = Side(style="medium", color="92400E")
_GREEN_SIDE = Side(style="medium", color="166534")

BORDER_CELL = Border(left=_THIN_SIDE, right=_THIN_SIDE, top=_THIN_SIDE, bottom=_THIN_SIDE)
BORDER_TH = Border(left=_THIN_SIDE, right=_THIN_SIDE, top=_THIN_SIDE, bottom=_NAVY_SIDE)
BORDER_RED_BOX = Border(left=_RED_SIDE, right=_RED_SIDE, top=_RED_SIDE, bottom=_RED_SIDE)
BORDER_AMBER_BOX = Border(left=_AMBER_SIDE, right=_AMBER_SIDE, top=_AMBER_SIDE, bottom=_AMBER_SIDE)
BORDER_GREEN_BOX = Border(left=_GREEN_SIDE, right=_GREEN_SIDE, top=_GREEN_SIDE, bottom=_GREEN_SIDE)

ALIGN_LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)
ALIGN_RIGHT = Alignment(horizontal="right", vertical="center", wrap_text=False)
ALIGN_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)

FMT_USD = '$#,##0.00;-$#,##0.00;"$0.00"'
FMT_INT = "#,##0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_decimal_cell(value: float | int | str | Decimal | None) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    try:
        return Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def _write_title_block(
    ws: Worksheet,
    *,
    title: str,
    subtitle: str,
    col_span: int,
) -> int:
    """Write navy header banner across rows 1-2; returns next free row index."""
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=col_span)
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=col_span)

    c1 = ws.cell(row=1, column=1, value=title)
    c1.font = FONT_TITLE
    c1.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    c2 = ws.cell(row=2, column=1, value=subtitle)
    c2.font = FONT_SUBTITLE
    c2.alignment = Alignment(horizontal="left", vertical="center", indent=1)

    for r in (1, 2):
        for col in range(1, col_span + 1):
            ws.cell(row=r, column=col).fill = FILL_NAVY
    ws.row_dimensions[1].height = 24
    ws.row_dimensions[2].height = 18
    return 4


def _write_status_banner(
    ws: Worksheet,
    *,
    row: int,
    col_span: int,
    text: str,
    kind: str,
) -> int:
    """Write a full-width status banner (`blocked` | `sandbox` | `ready`)."""
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=col_span)
    cell = ws.cell(row=row, column=1, value=text)
    if kind == "blocked":
        fill, font, border = FILL_RED, FONT_BLOCKED_BANNER, BORDER_RED_BOX
        height = 28
    elif kind == "ready":
        fill, font, border = FILL_GREEN, FONT_READY_BANNER, BORDER_GREEN_BOX
        height = 24
    else:
        fill, font, border = FILL_AMBER, FONT_SANDBOX_BANNER, BORDER_AMBER_BOX
        height = 24

    cell.font = font
    cell.alignment = ALIGN_CENTER
    for col in range(1, col_span + 1):
        c = ws.cell(row=row, column=col)
        c.fill = fill
        c.border = border
    ws.row_dimensions[row].height = height
    return row + 2


def _write_table_headers(ws: Worksheet, row: int, headers: list[str], right_cols: set[int] | None = None) -> int:
    right_cols = right_cols or set()
    for idx, text in enumerate(headers, start=1):
        c = ws.cell(row=row, column=idx, value=text)
        c.font = FONT_TH
        c.fill = FILL_TH
        c.border = BORDER_TH
        c.alignment = ALIGN_RIGHT if idx in right_cols else ALIGN_LEFT
    ws.row_dimensions[row].height = 21
    return row + 1


def _autofit_columns(ws: Worksheet, min_widths: dict[int, float] | None = None, max_width: float = 48.0) -> None:
    min_widths = min_widths or {}
    col_max: dict[int, float] = dict(min_widths)
    for row in ws.iter_rows():
        for cell in row:
            # Skip merged banner cells spanning multiple columns
            if cell.row in (1, 2) or isinstance(cell.value, type(None)):
                continue
            val = cell.value
            text = str(val)
            # Longest line inside cell
            longest = max((len(line) for line in text.splitlines()), default=0)
            est = min(max_width, max(10.0, longest * 1.12 + 3.0))
            if est > col_max.get(cell.column, 0.0):
                col_max[cell.column] = est

    for col_idx, width in col_max.items():
        ws.column_dimensions[get_column_letter(col_idx)].width = round(width, 1)


def _configure_sheet_view(ws: Worksheet, freeze_cell: str) -> None:
    ws.views.sheetView[0].showGridLines = True
    ws.freeze_panes = freeze_cell
    ws.page_setup.orientation = ws.ORIENTATION_LANDSCAPE
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True


# ---------------------------------------------------------------------------
# Tab builders
# ---------------------------------------------------------------------------


def _build_runs_tab(ws: Worksheet, model: ReportModel) -> None:
    meta = model.meta
    summary: dict[str, Any] = dict(model.sections.get("summary") or {})
    ed_str = (
        f"Approved · Edition {meta.edition_number or 1}"
        if (meta.is_approved or meta.status == "approved")
        else f"Draft {meta.draft_number}"
    )

    row = _write_title_block(
        ws,
        title=f"{meta.company_name} — Payroll Pre-Posting Review",
        subtitle=f"Period: {meta.period_label} ({meta.period})  |  Status: {ed_str}  |  Generated: {meta.created_at}",
        col_span=6,
    )

    if model.blocking_checks:
        row = _write_status_banner(
            ws,
            row=row,
            col_span=6,
            text=f"BLOCKED — DO NOT POST  ({len(model.blocking_checks)} blocking check(s) open)",
            kind="blocked",
        )
    elif summary.get("erp_mode") == "sandbox" or not summary.get("mapping_approval_ref"):
        row = _write_status_banner(
            ws,
            row=row,
            col_span=6,
            text="PRE-POSTING REVIEW — SANDBOX / MAPPING APPROVAL REQUIRED BEFORE PRODUCTION POST",
            kind="sandbox",
        )
    else:
        row = _write_status_banner(
            ws,
            row=row,
            col_span=6,
            text=f"READY FOR POSTING REVIEW — {ed_str.upper()}",
            kind="ready",
        )

    # Section 1: Run summary
    sec = ws.cell(row=row, column=1, value="Payroll Run Summary (Single-Model Snapshot)")
    sec.font = FONT_SECTION
    row += 1
    row = _write_table_headers(ws, row, ["Field", "Value", "Control / Context"], right_cols=set())

    mapping_ref = summary.get("mapping_approval_ref")
    var_cents = summary.get("variance_cents")
    var_usd = (Decimal(var_cents) / Decimal(100)) if isinstance(var_cents, int) else None

    summary_rows: list[tuple[str, Any, str, str | None]] = [
        ("Pay Date", summary.get("pay_date", meta.period), "Payroll disbursement date", None),
        ("Pay Period", summary.get("pay_period", meta.period_label), "Voucher pay period", None),
        ("Employee Count", summary.get("employee_count"), "Employees in voucher", FMT_INT),
        ("Gross Wages", _to_decimal_cell(summary.get("gross_wages")), "From payroll register", FMT_USD),
        ("Employer Taxes", _to_decimal_cell(summary.get("employer_taxes")), "FICA / FUTA / SUTA", FMT_USD),
        ("Employer Benefits", _to_decimal_cell(summary.get("employer_benefits")), "Employer benefit contributions", FMT_USD),
        ("Employee Withholdings", _to_decimal_cell(summary.get("employee_withholdings")), "Taxes & employee deductions", FMT_USD),
        ("Net Pay", _to_decimal_cell(summary.get("net_pay")), "Employee direct deposit / check total", FMT_USD),
        ("Total Funding Wire", _to_decimal_cell(summary.get("total_funding_wire")), "Bank funding confirmation", FMT_USD),
        (
            "Register vs. Wire Variance",
            var_usd if var_usd is not None else "—",
            f"{var_cents} cent(s)" if var_cents is not None else "From model",
            FMT_USD if var_usd is not None else None,
        ),
        ("Run Status", str(summary.get("status", "blocked" if model.blocking_checks else "ready")).upper(), "Deterministic gate", None),
        ("ERP Posting Mode", str(summary.get("erp_mode", "sandbox")), "From config/assumptions.yaml", None),
        (
            "Mapping Approval Ref",
            str(mapping_ref) if mapping_ref else "UNSET (null) — Production posting blocked",
            "Required before production ERP post",
            None,
        ),
    ]

    for idx, (lbl, val, ctx, num_fmt) in enumerate(summary_rows):
        c1 = ws.cell(row=row, column=1, value=lbl)
        c2 = ws.cell(row=row, column=2, value=val if val is not None else "—")
        c3 = ws.cell(row=row, column=3, value=ctx)
        c1.font = FONT_BODY_BOLD
        c2.font = FONT_BODY
        c3.font = FONT_MUTED
        c1.alignment = ALIGN_LEFT
        c2.alignment = ALIGN_RIGHT if num_fmt else ALIGN_LEFT
        c3.alignment = ALIGN_LEFT
        if num_fmt and val is not None and not isinstance(val, str):
            c2.number_format = num_fmt
        fill = FILL_ZEBRA if idx % 2 == 1 else None
        for col in (1, 2, 3):
            cell = ws.cell(row=row, column=col)
            cell.border = BORDER_CELL
            if fill:
                cell.fill = fill
        if lbl == "Run Status":
            if str(val) == "BLOCKED":
                c2.fill = FILL_RED
                c2.font = FONT_RED
            elif str(val) == "READY":
                c2.fill = FILL_GREEN
                c2.font = FONT_GREEN
            else:
                c2.fill = FILL_AMBER
                c2.font = FONT_AMBER
        if lbl == "Mapping Approval Ref" and not mapping_ref:
            c2.fill = FILL_RED
            c2.font = FONT_RED
        ws.row_dimensions[row].height = 19
        row += 1

    # Section 2: Model KPIs
    row += 1
    sec2 = ws.cell(row=row, column=1, value="Canonical KPIs (ReportModel.kpis)")
    sec2.font = FONT_SECTION
    row += 1
    row = _write_table_headers(ws, row, ["KPI Key", "Label", "Value", "Unit", "Status", "Note"], right_cols={3})
    for idx, kpi in enumerate(model.kpis):
        val_cell: Any = kpi.value
        num_fmt = None
        if kpi.unit == "USD":
            val_cell = _to_decimal_cell(kpi.value)
            num_fmt = FMT_USD
        elif kpi.unit == "count" and isinstance(kpi.value, (int, float, Decimal)):
            val_cell = int(kpi.value)
            num_fmt = FMT_INT
        vals = [kpi.key, kpi.label, val_cell if val_cell is not None else "—", kpi.unit, kpi.status, kpi.note]
        for col, v in enumerate(vals, start=1):
            c = ws.cell(row=row, column=col, value=v)
            c.font = FONT_BODY_BOLD if col in (2, 3) else FONT_BODY
            c.border = BORDER_CELL
            c.alignment = ALIGN_RIGHT if col == 3 and num_fmt else ALIGN_LEFT
            if col == 3 and num_fmt and v != "—":
                c.number_format = num_fmt
            if idx % 2 == 1:
                c.fill = FILL_ZEBRA
        ws.row_dimensions[row].height = 19
        row += 1

    # Section 3: Retained Sources (respecting restricted privacy wall)
    row += 1
    sec3 = ws.cell(row=row, column=1, value="Source Citations & Data Privacy Policy")
    sec3.font = FONT_SECTION
    row += 1
    row = _write_table_headers(ws, row, ["Feed ID", "File Name", "As Of", "Sensitivity", "Attachable", "Policy Note"])
    for src in model.sources:
        is_restricted = src.sensitivity == "restricted" or not src.attachable
        vals = [
            src.feed_id,
            src.file_name,
            src.as_of or "—",
            src.sensitivity,
            "No — Citation Only" if is_restricted else "Yes",
            "Restricted payroll data — cite filename/date only; never attach." if is_restricted else (src.detail or "Retained input"),
        ]
        for col, v in enumerate(vals, start=1):
            c = ws.cell(row=row, column=col, value=v)
            c.font = FONT_RED if (col in (4, 5) and is_restricted) else FONT_BODY
            c.border = BORDER_CELL
            c.alignment = ALIGN_LEFT
            if col in (4, 5) and is_restricted:
                c.fill = FILL_RED
        ws.row_dimensions[row].height = 19
        row += 1

    _autofit_columns(ws, min_widths={1: 24, 2: 26, 3: 24, 4: 14, 5: 20, 6: 36})
    _configure_sheet_view(ws, "A4")


def _build_tieout_tab(ws: Worksheet, model: ReportModel) -> None:
    meta = model.meta
    row = _write_title_block(
        ws,
        title=f"{meta.company_name} — Payroll Register vs. Funding Wire Tie-Out",
        subtitle=f"Period: {meta.period_label} ({meta.period})  |  Single-model tie-out components",
        col_span=5,
    )

    if model.blocking_checks:
        row = _write_status_banner(
            ws,
            row=row,
            col_span=5,
            text=f"BLOCKED — DO NOT POST  ({len(model.blocking_checks)} blocking check(s) active on this payroll run)",
            kind="blocked",
        )

    header_row = row
    row = _write_table_headers(
        ws,
        row,
        ["Component", "Register Amount", "Funding Amount", "Difference", "Status"],
        right_cols={2, 3, 4},
    )

    tieout_rows = list(model.sections.get("tieout") or [])
    if not tieout_rows:
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=5)
        c = ws.cell(row=row, column=1, value="No tie-out components present in model.sections['tieout'].")
        c.font = FONT_MUTED
        c.border = BORDER_CELL
    else:
        for idx, item in enumerate(tieout_rows):
            status = str(item.get("status", "")).upper()
            vals = [
                item.get("component", ""),
                _to_decimal_cell(item.get("register_amount")),
                _to_decimal_cell(item.get("funding_amount")),
                _to_decimal_cell(item.get("difference")),
                status,
            ]
            for col, v in enumerate(vals, start=1):
                c = ws.cell(row=row, column=col, value=v if v is not None else "—")
                c.border = BORDER_CELL
                if col in (2, 3, 4):
                    c.alignment = ALIGN_RIGHT
                    c.font = FONT_BODY_BOLD if col == 4 else FONT_BODY
                    if v is not None:
                        c.number_format = FMT_USD
                elif col == 5:
                    c.alignment = ALIGN_CENTER
                    if status == "PASS":
                        c.fill = FILL_GREEN
                        c.font = FONT_GREEN
                    else:
                        c.fill = FILL_RED
                        c.font = FONT_RED
                else:
                    c.alignment = ALIGN_LEFT
                    c.font = FONT_BODY_BOLD
                if idx % 2 == 1 and col != 5:
                    c.fill = FILL_ZEBRA
            ws.row_dimensions[row].height = 20
            row += 1

    _autofit_columns(ws, min_widths={1: 30, 2: 18, 3: 18, 4: 16, 5: 14})
    _configure_sheet_view(ws, f"A{header_row + 1}")


def _build_checks_tab(ws: Worksheet, model: ReportModel) -> None:
    meta = model.meta
    row = _write_title_block(
        ws,
        title=f"{meta.company_name} — Payroll Control Checks",
        subtitle=(
            f"Blocking: {len(model.blocking_checks)}  |  "
            f"Open Review (Non-Blocking): {len(model.open_review_checks)}  |  "
            f"Notes: {len(model.note_checks)}  |  "
            f"Can Approve: {'Yes' if model.can_approve else 'No'}"
        ),
        col_span=9,
    )

    header_row = row
    row = _write_table_headers(
        ws,
        row,
        ["Check ID", "Group", "Severity", "Blocks Approval", "Title", "Detail", "Amount", "Owner", "Next Step"],
        right_cols={7},
    )

    ordered = list(model.blocking_checks) + list(model.open_review_checks) + list(model.note_checks)
    if not ordered:
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=9)
        c = ws.cell(row=row, column=1, value="No control checks recorded.")
        c.font = FONT_MUTED
        c.border = BORDER_CELL
    else:
        for idx, chk in enumerate(ordered):
            amt = _to_decimal_cell(chk.amount) if chk.amount is not None else None
            vals = [
                chk.id,
                chk.group,
                chk.severity.upper(),
                "YES — BLOCKING" if chk.blocks_approval else "NO — NON-BLOCKING",
                chk.title,
                chk.detail,
                amt if amt is not None else "—",
                chk.owner or "—",
                chk.next_step or "—",
            ]
            for col, v in enumerate(vals, start=1):
                c = ws.cell(row=row, column=col, value=v)
                c.border = BORDER_CELL
                c.alignment = ALIGN_RIGHT if (col == 7 and amt is not None) else (ALIGN_CENTER if col in (3, 4) else ALIGN_LEFT)
                c.font = FONT_BODY_BOLD if col in (1, 5) else FONT_BODY
                if col == 7 and amt is not None:
                    c.number_format = FMT_USD
                if col in (3, 4):
                    if chk.severity == "blocking":
                        c.fill = FILL_RED
                        c.font = FONT_RED
                    elif chk.severity == "open_review":
                        c.fill = FILL_AMBER
                        c.font = FONT_AMBER
                    else:
                        c.fill = FILL_BLUE
                        c.font = FONT_BLUE
                elif idx % 2 == 1:
                    c.fill = FILL_ZEBRA
            ws.row_dimensions[row].height = 24
            row += 1

    _autofit_columns(ws, min_widths={1: 14, 2: 14, 3: 14, 4: 18, 5: 26, 6: 38, 7: 14, 8: 16, 9: 30})
    _configure_sheet_view(ws, f"A{header_row + 1}")


def _build_exceptions_tab(ws: Worksheet, model: ReportModel) -> None:
    meta = model.meta
    exceptions = list(model.sections.get("exceptions") or [])
    row = _write_title_block(
        ws,
        title=f"{meta.company_name} — Payroll Mapping & Validation Exceptions",
        subtitle=f"Period: {meta.period_label} ({meta.period})  |  Total Exceptions: {len(exceptions)}",
        col_span=8,
    )

    header_row = row
    row = _write_table_headers(
        ws,
        row,
        ["Code", "Severity", "Employee ID", "Employee Name", "Branch", "Dept Code", "Detail", "Remediation"],
    )

    if not exceptions:
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=8)
        c = ws.cell(row=row, column=1, value="No payroll exceptions reported.")
        c.font = FONT_GREEN
        c.fill = FILL_GREEN
        c.border = BORDER_CELL
    else:
        for idx, exc in enumerate(exceptions):
            sev = str(exc.get("severity", "open_review")).lower()
            vals = [
                exc.get("code", ""),
                sev.upper(),
                exc.get("employee_id", ""),
                exc.get("employee_name", ""),
                exc.get("branch", ""),
                exc.get("dept_code", ""),
                exc.get("detail", ""),
                exc.get("remediation", ""),
            ]
            for col, v in enumerate(vals, start=1):
                c = ws.cell(row=row, column=col, value=v)
                c.border = BORDER_CELL
                c.alignment = ALIGN_CENTER if col == 2 else ALIGN_LEFT
                c.font = FONT_BODY_BOLD if col in (1, 3, 6) else FONT_BODY
                if col == 2:
                    if sev == "blocking":
                        c.fill = FILL_RED
                        c.font = FONT_RED
                    else:
                        c.fill = FILL_AMBER
                        c.font = FONT_AMBER
                elif idx % 2 == 1:
                    c.fill = FILL_ZEBRA
            ws.row_dimensions[row].height = 24
            row += 1

    _autofit_columns(ws, min_widths={1: 14, 2: 14, 3: 14, 4: 20, 5: 14, 6: 18, 7: 34, 8: 34})
    _configure_sheet_view(ws, f"A{header_row + 1}")


def _build_draft_journal_tab(ws: Worksheet, model: ReportModel) -> None:
    meta = model.meta
    summary: dict[str, Any] = dict(model.sections.get("summary") or {})
    journal_lines = list(model.sections.get("journal_lines") or [])

    row = _write_title_block(
        ws,
        title=f"{meta.company_name} — Draft Payroll Journal Entry",
        subtitle=(
            f"Period: {meta.period_label} ({meta.period})  |  "
            f"ERP Mode: {summary.get('erp_mode', 'sandbox')}  |  "
            f"Mapping Approval Ref: {summary.get('mapping_approval_ref') or 'UNSET'}"
        ),
        col_span=9,
    )

    if model.blocking_checks:
        row = _write_status_banner(
            ws,
            row=row,
            col_span=9,
            text=f"BLOCKED — DO NOT POST  |  {len(model.blocking_checks)} blocking check(s) must be resolved before ERP posting",
            kind="blocked",
        )
    elif summary.get("erp_mode") == "sandbox" or not summary.get("mapping_approval_ref"):
        row = _write_status_banner(
            ws,
            row=row,
            col_span=9,
            text="DRAFT JOURNAL — SANDBOX / PRE-APPROVAL PREVIEW ONLY (DO NOT POST TO PRODUCTION)",
            kind="sandbox",
        )
    else:
        ed = meta.edition_number or 1
        row = _write_status_banner(
            ws,
            row=row,
            col_span=9,
            text=f"READY FOR POSTING REVIEW — APPROVED EDITION {ed}",
            kind="ready",
        )

    header_row = row
    row = _write_table_headers(
        ws,
        row,
        ["Line #", "Account", "Account Name", "Department", "Branch", "Debit", "Credit", "Memo", "Blocked Reason"],
        right_cols={1, 6, 7},
    )

    if not journal_lines:
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=9)
        c = ws.cell(row=row, column=1, value="No journal lines present in model.sections['journal_lines'].")
        c.font = FONT_MUTED
        c.border = BORDER_CELL
    else:
        for idx, line in enumerate(journal_lines):
            blocked_reason = str(line.get("blocked_reason") or "").strip()
            debit = _to_decimal_cell(line.get("debit"))
            credit = _to_decimal_cell(line.get("credit"))
            vals = [
                line.get("line_no", idx + 1),
                line.get("account", ""),
                line.get("account_name", ""),
                line.get("department", ""),
                line.get("branch", ""),
                debit if debit is not None else Decimal("0.00"),
                credit if credit is not None else Decimal("0.00"),
                line.get("memo", ""),
                blocked_reason or "—",
            ]
            for col, v in enumerate(vals, start=1):
                c = ws.cell(row=row, column=col, value=v)
                c.border = BORDER_CELL
                if col == 1:
                    c.alignment = ALIGN_RIGHT
                    c.font = FONT_BODY
                    c.number_format = FMT_INT
                elif col in (6, 7):
                    c.alignment = ALIGN_RIGHT
                    c.font = FONT_BODY_BOLD if (isinstance(v, Decimal) and v > 0) else FONT_MUTED
                    c.number_format = FMT_USD
                else:
                    c.alignment = ALIGN_LEFT
                    c.font = FONT_RED if (blocked_reason and col in (2, 4, 9)) else FONT_BODY

                if blocked_reason:
                    c.fill = FILL_RED
                elif idx % 2 == 1:
                    c.fill = FILL_ZEBRA
            ws.row_dimensions[row].height = 20
            row += 1

    _autofit_columns(ws, min_widths={1: 8, 2: 14, 3: 26, 4: 18, 5: 14, 6: 15, 7: 15, 8: 28, 9: 32})
    _configure_sheet_view(ws, f"A{header_row + 1}")


# ---------------------------------------------------------------------------
# Public frozen entrypoint
# ---------------------------------------------------------------------------


def render_payroll_xlsx(model: ReportModel, out_path: Path) -> Path:
    """Render the formatted 5-tab payroll review workbook directly from ReportModel."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()

    ws_runs = wb.active
    ws_runs.title = "Runs"
    ws_tieout = wb.create_sheet(title="Tie-Out")
    ws_checks = wb.create_sheet(title="Checks")
    ws_exceptions = wb.create_sheet(title="Exceptions")
    ws_journal = wb.create_sheet(title="Draft Journal")

    _build_runs_tab(ws_runs, model)
    _build_tieout_tab(ws_tieout, model)
    _build_checks_tab(ws_checks, model)
    _build_exceptions_tab(ws_exceptions, model)
    _build_draft_journal_tab(ws_journal, model)

    wb.save(out_path)
    return out_path
