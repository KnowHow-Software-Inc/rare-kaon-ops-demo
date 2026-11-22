"""Deterministic synthetic dataset generator for Heartland Mechanical Services, LLC.

Generates all 13 files under `seed/` (matching `SEED_PATHS` in `engine/contracts.py`)
plus `tests/fixtures/expected_values.json` using fixed-seed deterministic generation
and exact integer-cent arithmetic.

Run:
    .venv/bin/python scripts/generate_demo_data.py
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import random
import re
import sys
import zipfile
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import yaml
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine import __version__  # noqa: E402
from engine.contracts import (  # noqa: E402
    COMPANY_NAME,
    DEFAULT_PAYROLL_PERIOD,
    DEFAULT_WEEKLY_PERIOD,
    PRIOR_WEEKLY_PERIOD,
    SCHEMA_VERSION,
    SEED_PATHS,
)
from engine.model import (  # noqa: E402
    CheckItem,
    KpiField,
    ReportMeta,
    ReportModel,
    SourceRef,
)

FIXED_SEED = 20261120
FIXED_XLSX_DT = dt.datetime(2026, 11, 18, 22, 40, 0)
FIXED_ZIP_DT = (2026, 11, 18, 22, 40, 0)


# ---------------------------------------------------------------------------
# Exact integer-cent helpers
# ---------------------------------------------------------------------------


def to_cents(amount: str | int | Decimal) -> int:
    """Convert a dollar string/int/Decimal to exact integer cents."""
    if isinstance(amount, int):
        return amount * 100
    dec = Decimal(str(amount))
    return int((dec * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cents_to_float(cents: int) -> float:
    """Convert integer cents to exact 2-decimal float for JSON/YAML serialization."""
    return float(Decimal(cents) / Decimal("100"))


def cents_to_str(cents: int) -> str:
    """Format integer cents as plain decimal string (`1234.56`)."""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{whole}.{frac:02d}"


def cents_to_currency(cents: int) -> str:
    """Format integer cents as US currency string (`$1,234.56`)."""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}${whole:,}.{frac:02d}"


def pct_of_cents(base_cents: int, rate: str) -> int:
    """Multiply integer cents by a Decimal rate with half-up rounding to exact cents."""
    val = (Decimal(base_cents) * Decimal(rate)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(val)


class _NoAliasSafeDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    """Write deterministic, anchor-free YAML preserving insertion order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.dump(
        data,
        Dumper=_NoAliasSafeDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )
    path.write_text(rendered, encoding="utf-8", newline="\n")


def write_json(path: Path, data: Any) -> None:
    """Write deterministic formatted JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def save_deterministic_xlsx(wb: Workbook, path: Path) -> None:
    """Save an openpyxl workbook with pinned metadata and ZIP timestamps for byte reproducibility."""
    wb.properties.creator = "Heartland Mechanical Services ERP Export"
    wb.properties.lastModifiedBy = "Heartland Mechanical Services ERP Export"
    wb.properties.created = FIXED_XLSX_DT
    wb.properties.modified = FIXED_XLSX_DT

    raw_buf = io.BytesIO()
    wb.save(raw_buf)
    raw_buf.seek(0)

    fixed_iso = FIXED_XLSX_DT.strftime("%Y-%m-%dT%H:%M:%SZ")
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(raw_buf, "r") as zin, zipfile.ZipFile(
        path, "w", compression=zipfile.ZIP_DEFLATED
    ) as zout:
        for info in sorted(zin.infolist(), key=lambda i: i.filename):
            data = zin.read(info.filename)
            if info.filename == "docProps/core.xml":
                xml_text = data.decode("utf-8")
                xml_text = re.sub(
                    r"(<dcterms:modified[^>]*>)[^<]*(</dcterms:modified>)",
                    rf"\g<1>{fixed_iso}\g<2>",
                    xml_text,
                )
                data = xml_text.encode("utf-8")
            new_info = zipfile.ZipInfo(filename=info.filename, date_time=FIXED_ZIP_DT)
            new_info.compress_type = zipfile.ZIP_DEFLATED
            new_info.external_attr = 0o644 << 16
            zout.writestr(new_info, data)


# ---------------------------------------------------------------------------
# Arc 4: Payroll voucher (18 employees), funding wire, and mappings
# ---------------------------------------------------------------------------


def build_payroll_data() -> dict[str, Any]:
    """Generate the 18-employee biweekly payroll register, funding wire, and GL mappings."""
    # (emp_id, name, branch, dept_code, title, regular_cents, overtime_cents,
    #  futa_suta_cents, medical_er_cents, withholding_rate)
    # Exact sum of overtime_cents across field staff = 1,148,025 cents ($11,480.25).
    employee_specs = [
        # DAL-01 (9 employees)
        ("EMP-1001", "Derrick Holloway", "DAL-01", "DAL-SVC-FIELD", "Lead HVAC Technician", 368000, 142550, 0, 62500, "0.285"),
        ("EMP-1004", "Brenda Castellanos", "DAL-01", "DAL-SVC-FIELD", "Commercial Refrigeration Tech", 352000, 134075, 0, 62500, "0.278"),
        ("EMP-1007", "Trevor Callahan", "DAL-01", "DAL-SVC-FIELD", "Senior HVAC Technician", 336000, 128500, 0, 34500, "0.272"),
        ("EMP-1011", "Kendra Washington", "DAL-01", "DAL-SVC-FIELD", "Field Technician II", 296000, 108400, 1840, 34500, "0.264"),
        ("EMP-1014", "Hector Villanueva", "DAL-01", "DAL-SVC-FIELD", "Chiller Specialist", 384000, 151200, 0, 62500, "0.292"),
        ("EMP-1018", "Nadia Okafor", "DAL-01", "DAL-CTRL-ENG", "Controls Specialist", 416000, 0, 0, 34500, "0.295"),
        ("EMP-1021", "Grant Whitaker", "DAL-01", "DAL-CTRL-ENG", "Building Automation Tech", 376000, 0, 0, 62500, "0.282"),
        ("EMP-1025", "Sharon Lindqvist", "DAL-01", "DAL-OPS-MGMT", "Branch Manager", 495000, 0, 0, 62500, "0.312"),
        ("EMP-1028", "Caleb Montoya", "DAL-01", "DAL-OPS-MGMT", "Service Dispatcher", 248000, 0, 0, 34500, "0.248"),
        # AUS-02 (5 employees)
        ("EMP-1031", "Miguel Cisneros", "AUS-02", "AUS-SVC-FIELD", "Lead HVAC Technician", 364000, 139000, 0, 62500, "0.284"),
        ("EMP-1034", "Hannah Kowalski", "AUS-02", "AUS-SVC-FIELD", "Commercial Refrigeration Tech", 344000, 121500, 0, 34500, "0.275"),
        ("EMP-1037", "Devon Brooks", "AUS-02", "AUS-SVC-FIELD", "Field Technician II", 288000, 96800, 2260, 34500, "0.261"),
        ("EMP-1040", "Priya Subramanian", "AUS-02", "AUS-CTRL-ENG", "Controls Specialist", 412000, 0, 0, 62500, "0.296"),
        ("EMP-1042", "Lori McAllister", "AUS-02", "AUS-OPS-MGMT", "Service Dispatcher", 244000, 0, 0, 34500, "0.246"),
        # TUL-03 (4 employees; EMP-1048 is intentionally unmapped on TUL-INSTALL-NEW)
        ("EMP-1044", "Wyatt Henderson", "TUL-03", "TUL-SVC-FIELD", "Lead HVAC Technician", 340000, 126000, 3150, 62500, "0.274"),
        ("EMP-1046", "Tanya Petrov", "TUL-03", "TUL-SVC-FIELD", "Commercial HVAC Technician", 304000, 0, 2890, 34500, "0.265"),
        ("EMP-1047", "Owen Gallagher", "TUL-03", "TUL-OPS-ADMIN", "Staff Accountant", 292000, 0, 2640, 34500, "0.268"),
        ("EMP-1048", "Marcus Delgado", "TUL-03", "TUL-INSTALL-NEW", "Field Technician II", 284000, 0, 3420, 34500, "0.258"),
    ]

    rows: list[dict[str, Any]] = []
    totals = {
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
        "employer_cost_total": 0,
        "funding_wire_total": 0,
    }

    for (
        emp_id,
        name,
        branch,
        dept_code,
        title,
        reg_c,
        ot_c,
        futa_suta_c,
        med_er_c,
        wh_rate,
    ) in employee_specs:
        gross_c = reg_c + ot_c
        fica_c = pct_of_cents(gross_c, "0.0765")
        er_tax_c = fica_c + futa_suta_c
        match_401k_c = pct_of_cents(gross_c, "0.04")
        er_ben_c = match_401k_c + med_er_c
        wh_c = pct_of_cents(gross_c, wh_rate)
        net_c = gross_c - wh_c
        er_cost_c = gross_c + er_tax_c + er_ben_c
        wire_c = net_c + er_tax_c + er_ben_c
        assert wire_c == er_cost_c - wh_c

        row = {
            "employee_id": emp_id,
            "employee_name": name,
            "branch": branch,
            "dept_code": dept_code,
            "job_title": title,
            "pay_period_start": "2026-11-01",
            "pay_period_end": "2026-11-13",
            "pay_date": DEFAULT_PAYROLL_PERIOD,
            "regular_pay_cents": reg_c,
            "overtime_pay_cents": ot_c,
            "gross_pay_cents": gross_c,
            "employer_fica_cents": fica_c,
            "employer_futa_suta_cents": futa_suta_c,
            "employer_taxes_cents": er_tax_c,
            "employer_401k_match_cents": match_401k_c,
            "employer_medical_cents": med_er_c,
            "employer_benefits_cents": er_ben_c,
            "employee_withholdings_cents": wh_c,
            "net_pay_cents": net_c,
            "employer_cost_cents": er_cost_c,
            "funding_wire_cents": wire_c,
        }
        rows.append(row)

        totals["regular_wages"] += reg_c
        totals["overtime_wages"] += ot_c
        totals["gross_wages"] += gross_c
        totals["employer_fica"] += fica_c
        totals["employer_futa_suta"] += futa_suta_c
        totals["employer_taxes"] += er_tax_c
        totals["employer_401k_match"] += match_401k_c
        totals["employer_medical"] += med_er_c
        totals["employer_benefits"] += er_ben_c
        totals["employee_withholdings"] += wh_c
        totals["net_pay"] += net_c
        totals["employer_cost_total"] += er_cost_c
        totals["funding_wire_total"] += wire_c

    assert totals["overtime_wages"] == 1_148_025, f"Expected $11,480.25 overtime, got {totals['overtime_wages']}"
    assert totals["gross_wages"] - totals["employee_withholdings"] == totals["net_pay"]
    assert totals["funding_wire_total"] + totals["employee_withholdings"] == totals["employer_cost_total"]

    mappings_yaml = {
        "schema_version": SCHEMA_VERSION,
        "effective_date": "2026-11-01",
        "owner": "Rachel Morales",
        "clearing_accounts": {
            "cash_sweep_account": "1020-00",
            "cash_sweep_account_name": "Frost Payroll Sweep (*4829)",
            "withholdings_liability_account": "2140-00",
            "withholdings_liability_account_name": "Employee Withholdings Payable",
        },
        "mappings": [
            {
                "dept_code": "DAL-SVC-FIELD",
                "branch": "DAL-01",
                "erp_department": "100-DAL-SERVICE",
                "wages_account": "5100-10",
                "wages_account_name": "Direct Field Labor - Dallas",
                "payroll_tax_account": "5120-10",
                "payroll_tax_account_name": "Employer Payroll Taxes - Dallas",
                "benefits_account": "5130-10",
                "benefits_account_name": "Employer Benefits & 401(k) - Dallas",
                "active": True,
            },
            {
                "dept_code": "DAL-CTRL-ENG",
                "branch": "DAL-01",
                "erp_department": "110-DAL-CONTROLS",
                "wages_account": "5105-10",
                "wages_account_name": "Controls Engineering Labor - Dallas",
                "payroll_tax_account": "5120-10",
                "payroll_tax_account_name": "Employer Payroll Taxes - Dallas",
                "benefits_account": "5130-10",
                "benefits_account_name": "Employer Benefits & 401(k) - Dallas",
                "active": True,
            },
            {
                "dept_code": "DAL-OPS-MGMT",
                "branch": "DAL-01",
                "erp_department": "120-DAL-OPS",
                "wages_account": "6100-10",
                "wages_account_name": "Branch Operations & Dispatch - Dallas",
                "payroll_tax_account": "6120-10",
                "payroll_tax_account_name": "Ops Employer Payroll Taxes - Dallas",
                "benefits_account": "6130-10",
                "benefits_account_name": "Ops Employer Benefits - Dallas",
                "active": True,
            },
            {
                "dept_code": "AUS-SVC-FIELD",
                "branch": "AUS-02",
                "erp_department": "200-AUS-SERVICE",
                "wages_account": "5100-20",
                "wages_account_name": "Direct Field Labor - Austin",
                "payroll_tax_account": "5120-20",
                "payroll_tax_account_name": "Employer Payroll Taxes - Austin",
                "benefits_account": "5130-20",
                "benefits_account_name": "Employer Benefits & 401(k) - Austin",
                "active": True,
            },
            {
                "dept_code": "AUS-CTRL-ENG",
                "branch": "AUS-02",
                "erp_department": "210-AUS-CONTROLS",
                "wages_account": "5105-20",
                "wages_account_name": "Controls Engineering Labor - Austin",
                "payroll_tax_account": "5120-20",
                "payroll_tax_account_name": "Employer Payroll Taxes - Austin",
                "benefits_account": "5130-20",
                "benefits_account_name": "Employer Benefits & 401(k) - Austin",
                "active": True,
            },
            {
                "dept_code": "AUS-OPS-MGMT",
                "branch": "AUS-02",
                "erp_department": "220-AUS-OPS",
                "wages_account": "6100-20",
                "wages_account_name": "Branch Operations & Dispatch - Austin",
                "payroll_tax_account": "6120-20",
                "payroll_tax_account_name": "Ops Employer Payroll Taxes - Austin",
                "benefits_account": "6130-20",
                "benefits_account_name": "Ops Employer Benefits - Austin",
                "active": True,
            },
            {
                "dept_code": "TUL-SVC-FIELD",
                "branch": "TUL-03",
                "erp_department": "300-TUL-SERVICE",
                "wages_account": "5100-30",
                "wages_account_name": "Direct Field Labor - Tulsa",
                "payroll_tax_account": "5120-30",
                "payroll_tax_account_name": "Employer Payroll Taxes - Tulsa",
                "benefits_account": "5130-30",
                "benefits_account_name": "Employer Benefits & 401(k) - Tulsa",
                "active": True,
            },
            {
                "dept_code": "TUL-OPS-ADMIN",
                "branch": "TUL-03",
                "erp_department": "320-TUL-ADMIN",
                "wages_account": "6100-30",
                "wages_account_name": "Branch Admin & Accounting - Tulsa",
                "payroll_tax_account": "6120-30",
                "payroll_tax_account_name": "Admin Employer Payroll Taxes - Tulsa",
                "benefits_account": "6130-30",
                "benefits_account_name": "Admin Employer Benefits - Tulsa",
                "active": True,
            },
            # Intentionally omitted: TUL-INSTALL-NEW (EMP-1048 Marcus Delgado)
        ],
    }

    funding_wire_yaml = {
        "schema_version": SCHEMA_VERSION,
        "wire_reference": "PEO-WIRE-2026-1115",
        "bank_account": "Frost Payroll Sweep (*4829)",
        "provider": "Heartland PEO Services (Biweekly Field & Branch Batch)",
        "pay_period_start": "2026-11-01",
        "pay_period_end": "2026-11-13",
        "pay_date": DEFAULT_PAYROLL_PERIOD,
        "settlement_date": "2026-11-16",
        "employee_count": len(rows),
        "components": {
            "gross_wages": cents_to_float(totals["gross_wages"]),
            "regular_wages": cents_to_float(totals["regular_wages"]),
            "overtime_wages": cents_to_float(totals["overtime_wages"]),
            "employer_fica": cents_to_float(totals["employer_fica"]),
            "employer_futa_suta": cents_to_float(totals["employer_futa_suta"]),
            "employer_taxes": cents_to_float(totals["employer_taxes"]),
            "employer_401k_match": cents_to_float(totals["employer_401k_match"]),
            "employer_medical": cents_to_float(totals["employer_medical"]),
            "employer_benefits": cents_to_float(totals["employer_benefits"]),
            "employee_withholdings": cents_to_float(totals["employee_withholdings"]),
            "net_pay": cents_to_float(totals["net_pay"]),
            "employer_cost_total": cents_to_float(totals["employer_cost_total"]),
            "total_funding_wire": cents_to_float(totals["funding_wire_total"]),
        },
        "tieout_note": (
            "Funding wire equals gross wages + employer taxes + employer benefits "
            "minus employee withholdings (equivalently net pay + employer taxes + employer benefits)."
        ),
    }

    return {
        "rows": rows,
        "totals_cents": totals,
        "mappings_yaml": mappings_yaml,
        "funding_wire_yaml": funding_wire_yaml,
    }


def write_payroll_voucher_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write the restricted 18-employee payroll voucher CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "employee_id",
        "employee_name",
        "branch",
        "dept_code",
        "job_title",
        "pay_period_start",
        "pay_period_end",
        "pay_date",
        "regular_pay",
        "overtime_pay",
        "gross_pay",
        "employer_fica",
        "employer_futa_suta",
        "employer_taxes",
        "employer_401k_match",
        "employer_medical",
        "employer_benefits",
        "employee_withholdings",
        "net_pay",
    ]
    with path.open("w", encoding="utf-8", newline="\n") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for r in rows:
            writer.writerow(
                {
                    "employee_id": r["employee_id"],
                    "employee_name": r["employee_name"],
                    "branch": r["branch"],
                    "dept_code": r["dept_code"],
                    "job_title": r["job_title"],
                    "pay_period_start": r["pay_period_start"],
                    "pay_period_end": r["pay_period_end"],
                    "pay_date": r["pay_date"],
                    "regular_pay": cents_to_str(r["regular_pay_cents"]),
                    "overtime_pay": cents_to_str(r["overtime_pay_cents"]),
                    "gross_pay": cents_to_str(r["gross_pay_cents"]),
                    "employer_fica": cents_to_str(r["employer_fica_cents"]),
                    "employer_futa_suta": cents_to_str(r["employer_futa_suta_cents"]),
                    "employer_taxes": cents_to_str(r["employer_taxes_cents"]),
                    "employer_401k_match": cents_to_str(r["employer_401k_match_cents"]),
                    "employer_medical": cents_to_str(r["employer_medical_cents"]),
                    "employer_benefits": cents_to_str(r["employer_benefits_cents"]),
                    "employee_withholdings": cents_to_str(r["employee_withholdings_cents"]),
                    "net_pay": cents_to_str(r["net_pay_cents"]),
                }
            )


# ---------------------------------------------------------------------------
# Arc 1: Bank exports (Frost Operating *4812 & Frost Payroll Sweep *4829)
# ---------------------------------------------------------------------------


def build_bank_data(field_payroll_wire_cents: int) -> dict[str, Any]:
    """Generate two weeks of Operating and Payroll Sweep bank activity with exact rollforward.

    Key tie-outs:
    - Consolidated closing balance on 2026-11-13 = $1,795,675.65 (Prior Edition 1 Opening Cash)
    - Prior Edition 1 Week 1 (ending 2026-11-20) planned net cash = +$12,790.00 -> $1,808,465.65
    - Actual consolidated closing balance on 2026-11-20 = $1,842,615.40 (Current Opening Cash)
    - Exact variance bridge delta = +$34,149.75:
        +$64,250.00  Methodist Health Facilities INV-2026-8814 early collection (2026-11-18)
        -$11,480.25  2026-11-13 biweekly payroll seasonal overtime above baseline (2026-11-16)
        -$18,620.00  AUS-02 emergency compressor parts order AUS-EMERG-9042 (2026-11-19)
    """
    biweekly_payroll_baseline_cents = 26_548_000  # $265,480.00
    overtime_variance_cents = 1_148_025  # $11,480.25
    total_payroll_wire_cents = biweekly_payroll_baseline_cents + overtime_variance_cents  # $276,960.25
    corp_payroll_wire_cents = total_payroll_wire_cents - field_payroll_wire_cents
    assert corp_payroll_wire_cents > 0

    # Target closing balances on 2026-11-13:
    #   Operating (*4812):      $1,611,475.65  (161,147,565 cents)
    #   Payroll Sweep (*4829):    $184,200.00  ( 18,420,000 cents)
    #   Total 2026-11-13:       $1,795,675.65  (179,567,565 cents)
    #
    # Week 1 (2026-11-09..2026-11-13) Operating transactions:
    op_week1_txns = [
        ("2026-11-09", "INV-2026-8740", "ACH CREDIT BAYLOR SCOTT & WHITE FACILITIES - DAL-01 MAINT", 0, 14_285_000),
        ("2026-11-09", "CAR-2026-4410", "ACH DEBIT CARRIER ENTERPRISE SOUTH CENTRAL - PARTS", 8_432_050, 0),
        ("2026-11-10", "INV-2026-8752", "ACH CREDIT DELL SETON MEDICAL CENTER - AUS-02 CONTROLS", 0, 9_841_025),
        # Intentional vendor casing/abbreviation quirk vs AP schedule ("Lone Star HVAC Supply")
        ("2026-11-11", "LSS-2026-3912", "ACH DEBIT LONE STAR HVAC SUPP TX - INV LSS-2026-3912", 4_128_000, 0),
        ("2026-11-12", "INV-2026-8761", "ACH CREDIT TULSA REGIONAL AIRPORT AUTH - TUL-03 CHILLER", 0, 6_319_040),
        ("2026-11-12", "WEX-2026-10", "ACH DEBIT WEX FLEET COMMERCIAL FUEL - OCT STATEMENT", 1_394_580, 0),
        ("2026-11-13", "INV-2026-8774", "ACH CREDIT NORTH TEXAS LOGISTICS PARK - DAL-01 RETROFIT", 0, 5_164_500),
    ]
    op_week1_net = sum(cr - dr for _, _, _, dr, cr in op_week1_txns)
    op_target_1113 = 161_147_565  # $1,611,475.65
    op_opening_1109 = op_target_1113 - op_week1_net

    # Week 2 (2026-11-16..2026-11-20) Operating transactions:
    # Planned inflows = 156,200 + 118,450 + 93,800 = $368,450.00
    # Plus early AR variance driver = +$64,250.00 (Methodist Health Facilities INV-2026-8814)
    # Planned non-payroll outflows = 54,280 + 21,400 + 14,500 = $90,180.00
    # Plus emergency compressor variance driver = -$18,620.00 (AUS-EMERG-9042)
    # Plus sweep funding transfer to *4829 = -$276,960.25
    op_week2_txns = [
        ("2026-11-16", "INV-2026-8790", "ACH CREDIT PARKLAND HEALTH SYSTEM - DAL-01 SERVICE", 0, 15_620_000),
        ("2026-11-16", "SWEEP-2026-1116", "INTERNAL TRANSFER TO FROST PAYROLL SWEEP *4829", total_payroll_wire_cents, 0),
        ("2026-11-17", "INV-2026-8798", "ACH CREDIT PLANO ISD FACILITIES - DAL-01 PREVENTIVE", 0, 11_845_000),
        ("2026-11-17", "TRN-2026-6620", "ACH DEBIT TRANE COMM SUPPLY LLC - COMMERCIAL COILS", 5_428_000, 0),
        ("2026-11-18", "INV-2026-8805", "ACH CREDIT DOMAIN NORTHSIDE PROPERTY MGMT - AUS-02", 0, 9_380_000),
        ("2026-11-18", "INV-2026-8814", "ACH CREDIT METHODIST HEALTH FACILITIES - INV-2026-8814", 0, 6_425_000),
        ("2026-11-18", "RENT-2026-11", "ACH DEBIT PROLOGIS INDUSTRIAL TEXAS - SHOP LEASES", 2_140_000, 0),
        ("2026-11-19", "INS-2026-11", "ACH DEBIT LIBERTY MUTUAL COMMERCIAL - FLEET & GL", 1_450_000, 0),
        ("2026-11-19", "AUS-EMERG-9042", "ACH DEBIT GULF COAST REFRIGERATION PARTS - AUS-02 EMERGENCY COMPRESSOR", 1_862_000, 0),
    ]

    op_rows: list[dict[str, Any]] = []
    running = op_opening_1109
    op_balance_1113 = 0
    for post_date, ref, desc, dr, cr in op_week1_txns + op_week2_txns:
        running = running + cr - dr
        op_rows.append(
            {
                "post_date": post_date,
                "reference": ref,
                "description": desc,
                "debit_cents": dr,
                "credit_cents": cr,
                "running_bal_cents": running,
            }
        )
        if post_date <= PRIOR_WEEKLY_PERIOD:
            op_balance_1113 = running
    op_balance_1120 = running
    assert op_balance_1113 == 161_147_565
    assert op_balance_1120 == 165_841_540  # $1,658,415.40

    # Frost Payroll Sweep (*4829) transactions:
    # Maintains target payroll reserve buffer of $184,200.00 after biweekly wire settlements.
    sweep_opening_1109 = 18_420_000  # $184,200.00
    sweep_txns = [
        ("2026-11-10", "FEE-2026-1110", "FROST TREASURY WIRE FEE REBATE - SWEEP ACCOUNT", 0, 4_500),
        ("2026-11-10", "FEE-2026-1110B", "FROST COMMERCIAL WIRE SERVICE CHARGE", 4_500, 0),
        ("2026-11-16", "SWEEP-2026-1116", "INTERNAL TRANSFER FROM FROST OPERATING *4812", 0, total_payroll_wire_cents),
        (
            "2026-11-16",
            "PEO-WIRE-CORP-1115",
            "OUTGOING WIRE HEARTLAND PEO - SALARIED & CORPORATE PAYROLL (2026-11-13 RUN)",
            corp_payroll_wire_cents,
            0,
        ),
        (
            "2026-11-16",
            "PEO-WIRE-2026-1115",
            "OUTGOING WIRE HEARTLAND PEO - FIELD & BRANCH BATCH (2026-11-13 RUN; OT +$11,480.25)",
            field_payroll_wire_cents,
            0,
        ),
    ]
    sweep_rows: list[dict[str, Any]] = []
    running = sweep_opening_1109
    sweep_balance_1113 = sweep_opening_1109
    for post_date, ref, desc, dr, cr in sweep_txns:
        running = running + cr - dr
        sweep_rows.append(
            {
                "post_date": post_date,
                "reference": ref,
                "description": desc,
                "debit_cents": dr,
                "credit_cents": cr,
                "running_bal_cents": running,
            }
        )
        if post_date <= PRIOR_WEEKLY_PERIOD:
            sweep_balance_1113 = running
    sweep_balance_1120 = running
    assert sweep_balance_1113 == 18_420_000
    assert sweep_balance_1120 == 18_420_000

    return {
        "operating": {
            "opening_1109_cents": op_opening_1109,
            "closing_1113_cents": op_balance_1113,
            "closing_1120_cents": op_balance_1120,
            "rows": op_rows,
        },
        "payroll_sweep": {
            "opening_1109_cents": sweep_opening_1109,
            "closing_1113_cents": sweep_balance_1113,
            "closing_1120_cents": sweep_balance_1120,
            "rows": sweep_rows,
        },
        "total_1113_cents": op_balance_1113 + sweep_balance_1113,
        "total_1120_cents": op_balance_1120 + sweep_balance_1120,
        "biweekly_payroll_baseline_cents": biweekly_payroll_baseline_cents,
        "total_payroll_wire_cents": total_payroll_wire_cents,
        "corp_payroll_wire_cents": corp_payroll_wire_cents,
        "field_payroll_wire_cents": field_payroll_wire_cents,
    }


def write_bank_csv(path: Path, account_label: str, mask: str, rows: list[dict[str, Any]]) -> None:
    """Write a Frost bank CSV with 2-line bank preamble and quirky bank headers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(
            f"Frost Commercial Banking - Account Activity Export,"
            f"Account: {account_label} ({mask}),As Of: {DEFAULT_WEEKLY_PERIOD}\n"
        )
        f.write(
            "Export Generated: 2026-11-20 17:15 CST,Currency: USD,"
            "Statement Period: 2026-11-09 to 2026-11-20\n"
        )
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["Post Date", "Reference", "Transaction Desc", "Debit Amt", "Credit Amt", "Running Bal"])
        for r in rows:
            writer.writerow(
                [
                    r["post_date"],
                    r["reference"],
                    r["description"],
                    cents_to_str(r["debit_cents"]) if r["debit_cents"] else "",
                    cents_to_str(r["credit_cents"]) if r["credit_cents"] else "",
                    cents_to_str(r["running_bal_cents"]),
                ]
            )


# ---------------------------------------------------------------------------
# Arc 2: Multi-branch A/R Aging XLSX (as of 2026-11-18)
# ---------------------------------------------------------------------------


def build_ar_aging_data(rng: random.Random) -> dict[str, Any]:
    """Generate 28 realistic commercial HVAC customer A/R aging rows totaling ~$2.15M."""
    as_of = dt.date(2026, 11, 18)

    # 3 locked overdue items in 61-90 and >90 (exact from synthetic-data-brief.md)
    locked_overdue = [
        {
            "invoice_id": "INV-2026-7941",
            "customer": "Richardson ISD Facilities",
            "branch": "DAL-01",
            "invoice_date": dt.date(2026, 8, 6),
            "due_date": dt.date(2026, 9, 5),
            "amount_cents": 4_890_000,  # $48,900.00 (74 days past due)
            "note": "Awaiting signed final lien waiver from DAL-01 project manager",
        },
        {
            "invoice_id": "INV-2026-8012",
            "customer": "Barton Creek Hospitality Group",
            "branch": "AUS-02",
            "invoice_date": dt.date(2026, 8, 13),
            "due_date": dt.date(2026, 9, 12),
            "amount_cents": 2_245_000,  # $22,450.00 (67 days past due)
            "note": "Partial dispute over after-hours chiller dispatch rate",
        },
        {
            "invoice_id": "INV-2026-7305",
            "customer": "Capitol Tower Property Management",
            "branch": "AUS-02",
            "invoice_date": dt.date(2026, 7, 7),
            "due_date": dt.date(2026, 8, 6),
            "amount_cents": 3_175_000,  # $31,750.00 (104 days past due)
            "note": "Escalated to Controller Rachel Morales; payment plan under review",
        },
    ]

    # 25 additional commercial HVAC accounts across DAL-01 (~55%), AUS-02 (~30%), TUL-03 (~15%)
    # (invoice_id, customer, branch, days_past_due, amount_cents, note)
    other_specs = [
        # DAL-01 (12 accounts -> 13 total with Richardson ISD)
        ("INV-2026-8904", "Baylor Scott & White Facilities", "DAL-01", 0, 18_425_000, "Net 30 quarterly chiller PM"),
        ("INV-2026-8910", "Methodist Health Facilities", "DAL-01", 0, 14_680_000, "New November maintenance billing cycle"),
        ("INV-2026-8886", "Parkland Health System", "DAL-01", 0, 13_940_000, "Hospital air-handler retrofit phase 2"),
        ("INV-2026-8862", "Plano ISD Facilities", "DAL-01", 12, 11_265_000, "District fall heating startup"),
        ("INV-2026-8845", "Dallas Market Center Operations", "DAL-01", 18, 9_875_000, "Main hall rooftop condenser service"),
        ("INV-2026-8829", "Irving Medical Pavilion", "DAL-01", 0, 8_642_500, "Net 30 controls maintenance"),
        ("INV-2026-8795", "Las Colinas Office Trust", "DAL-01", 24, 7_815_000, "ACH scheduled by property AP"),
        ("INV-2026-8778", "Garland Cold Storage Inc", "DAL-01", 39, 7_140_000, "Walk-in compressor rebuild; PO verified"),
        ("INV-2026-8769", "Frisco Commercial Plaza LLC", "DAL-01", 0, 6_935_000, "Current commercial service contract"),
        ("INV-2026-8755", "North Texas Logistics Park", "DAL-01", 14, 6_420_000, "Warehouse makeup-air installation"),
        ("INV-2026-8738", "Uptown Tower Realty Partners", "DAL-01", 44, 5_680_000, "Tenant improvement balancing follow-up"),
        ("INV-2026-8719", "McKinney Civic Center", "DAL-01", 8, 4_912_500, "Municipal net-30 voucher approved"),
        # AUS-02 (7 accounts -> 9 total with Barton Creek & Capitol Tower)
        ("INV-2026-8901", "Dell Seton Medical Center", "AUS-02", 0, 12_840_000, "Building automation upgrade milestone"),
        ("INV-2026-8874", "Domain Northside Property Mgmt", "AUS-02", 0, 10_495_000, "Retail complex VAV controls"),
        ("INV-2026-8851", "Austin Tech Ridge Campus", "AUS-02", 16, 9_620_000, "Cleanroom cooling redundancy work"),
        ("INV-2026-8833", "Round Rock Data Facilities", "AUS-02", 0, 8_945_000, "CRAC unit preventive service"),
        ("INV-2026-8809", "Cedar Park Medical Offices", "AUS-02", 27, 6_285_000, "Payment queued for next check run"),
        ("INV-2026-8782", "South Congress Retail Holdings", "AUS-02", 36, 4_890_000, "Awaiting tenant CAM reconciliation"),
        ("INV-2026-8760", "Mueller District Commercial", "AUS-02", 9, 4_415_000, "Net 30 quarterly billing"),
        # TUL-03 (6 accounts)
        ("INV-2026-8892", "Tulsa Regional Airport Authority", "TUL-03", 0, 7_640_000, "Concourse boiler & chiller service"),
        ("INV-2026-8868", "Saint Francis Health Facilities", "TUL-03", 0, 6_890_000, "Medical campus controls contract"),
        ("INV-2026-8840", "Broken Arrow Industrial Park", "TUL-03", 19, 5_415_000, "Plant HVAC retrofit progress draw"),
        ("INV-2026-8818", "MidTown Tulsa Medical Plaza", "TUL-03", 34, 4_260_000, "QuickBooks-to-ERP transition invoice"),
        ("INV-2026-8792", "Jenks Cold Logistics LLC", "TUL-03", 11, 3_895_000, "Refrigeration rack servicing"),
        ("INV-2026-8765", "Owasso Commercial Properties", "TUL-03", 47, 3_245_000, "Follow-up call scheduled with AP"),
    ]

    # Use fixed RNG only for subtle deterministic tie-break ordering of current-invoice due offsets
    rows: list[dict[str, Any]] = []
    for spec in other_specs:
        inv_id, cust, branch, dpd, amt_c, note = spec
        if dpd == 0:
            days_until_due = rng.randint(5, 22)
            due_date = as_of + dt.timedelta(days=days_until_due)
        else:
            due_date = as_of - dt.timedelta(days=dpd)
        inv_date = due_date - dt.timedelta(days=30)
        rows.append(
            {
                "invoice_id": inv_id,
                "customer": cust,
                "branch": branch,
                "invoice_date": inv_date,
                "due_date": due_date,
                "amount_cents": amt_c,
                "note": note,
            }
        )

    rows.extend(locked_overdue)
    rows.sort(key=lambda r: (r["branch"], r["invoice_id"]))

    buckets = {"current": 0, "d1_30": 0, "d31_60": 0, "d61_90": 0, "over_90": 0}
    branch_totals = {"DAL-01": 0, "AUS-02": 0, "TUL-03": 0}
    overdue_items: list[dict[str, Any]] = []
    formatted_rows: list[dict[str, Any]] = []

    for r in rows:
        raw_dpd = (as_of - r["due_date"]).days
        dpd = max(0, raw_dpd)
        amt = r["amount_cents"]
        row_buckets = {"current": 0, "d1_30": 0, "d31_60": 0, "d61_90": 0, "over_90": 0}
        if dpd == 0:
            bucket_key = "current"
            bucket_label = "Current"
        elif dpd <= 30:
            bucket_key = "d1_30"
            bucket_label = "1-30"
        elif dpd <= 60:
            bucket_key = "d31_60"
            bucket_label = "31-60"
        elif dpd <= 90:
            bucket_key = "d61_90"
            bucket_label = "61-90"
        else:
            bucket_key = "over_90"
            bucket_label = ">90"

        row_buckets[bucket_key] = amt
        buckets[bucket_key] += amt
        branch_totals[r["branch"]] += amt

        item = {
            "invoice_id": r["invoice_id"],
            "customer": r["customer"],
            "branch": r["branch"],
            "invoice_date": r["invoice_date"].isoformat(),
            "due_date": r["due_date"].isoformat(),
            "days_past_due": dpd,
            "bucket": bucket_label,
            "amount_cents": amt,
            "row_buckets": row_buckets,
            "note": r["note"],
        }
        formatted_rows.append(item)

        if bucket_label in ("61-90", ">90"):
            overdue_items.append(
                {
                    "invoice_id": r["invoice_id"],
                    "customer": r["customer"],
                    "branch": r["branch"],
                    "invoice_date": r["invoice_date"].isoformat(),
                    "due_date": r["due_date"].isoformat(),
                    "days_past_due": dpd,
                    "bucket": bucket_label,
                    "amount": cents_to_float(amt),
                    "note": r["note"],
                }
            )

    overdue_items.sort(key=lambda x: (-x["days_past_due"], x["invoice_id"]))
    total_cents = sum(buckets.values())
    over_60_cents = buckets["d61_90"] + buckets["over_90"]
    assert over_60_cents == 10_310_000, f"Expected $103,100.00 >60d A/R, got {over_60_cents}"
    assert len(formatted_rows) == 28
    assert len(overdue_items) == 3

    return {
        "as_of": as_of.isoformat(),
        "rows": formatted_rows,
        "buckets_cents": buckets,
        "branch_totals_cents": branch_totals,
        "total_cents": total_cents,
        "ar_over_60_cents": over_60_cents,
        "overdue_items": overdue_items,
    }


def write_ar_aging_xlsx(path: Path, ar_data: dict[str, Any]) -> None:
    """Write the realistic A/R aging XLSX with rows 1-3 title banner, currency strings, and subtotal row."""
    wb = Workbook()
    ws = wb.active
    ws.title = "AR Aging Detail"

    title_font = Font(name="Calibri", size=13, bold=True, color="1B365D")
    sub_font = Font(name="Calibri", size=10, italic=True, color="4A5568")
    header_font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1B365D", end_color="1B365D", fill_type="solid")
    total_font = Font(name="Calibri", size=10, bold=True)
    total_fill = PatternFill(start_color="E2E8F0", end_color="E2E8F0", fill_type="solid")

    # Rows 1-3: Title banner (intentional export quirk)
    ws["A1"] = f"{COMPANY_NAME} — Consolidated Accounts Receivable Aging Detail"
    ws["A1"].font = title_font
    ws["A2"] = f"As of: {ar_data['as_of']} | Branches: DAL-01, AUS-02, TUL-03 | Currency: USD"
    ws["A2"].font = sub_font
    ws["A3"] = "Exported 2026-11-18 16:40 CST (mid-week aging snapshot; 2 days prior to Friday cash cutoff)"
    ws["A3"].font = sub_font

    # Row 4: Table headers
    headers = [
        "Invoice ID",
        "Customer Name",
        "Branch",
        "Invoice Date",
        "Due Date",
        "Days Past Due",
        "Current",
        "1-30 Days",
        "31-60 Days",
        "61-90 Days",
        ">90 Days",
        "Total Open",
        "Collection Note",
    ]
    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=4, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center" if col_idx <= 6 else "right")

    # Detail rows (rows 5..32) with formatted currency strings (intentional quirk handled by parser)
    for r in ar_data["rows"]:
        rb = r["row_buckets"]
        ws.append(
            [
                r["invoice_id"],
                r["customer"],
                r["branch"],
                r["invoice_date"],
                r["due_date"],
                r["days_past_due"],
                cents_to_currency(rb["current"]),
                cents_to_currency(rb["d1_30"]),
                cents_to_currency(rb["d31_60"]),
                cents_to_currency(rb["d61_90"]),
                cents_to_currency(rb["over_90"]),
                cents_to_currency(r["amount_cents"]),
                r["note"],
            ]
        )

    # Bottom subtotal row (intentional quirk stripped cleanly by parser rather than double-counted)
    b = ar_data["buckets_cents"]
    subtotal_row_idx = 5 + len(ar_data["rows"])
    ws.append(
        [
            "TOTAL",
            "Total Accounts Receivable",
            "",
            "",
            "",
            "",
            cents_to_currency(b["current"]),
            cents_to_currency(b["d1_30"]),
            cents_to_currency(b["d31_60"]),
            cents_to_currency(b["d61_90"]),
            cents_to_currency(b["over_90"]),
            cents_to_currency(ar_data["total_cents"]),
            f"{len(ar_data['rows'])} open invoices",
        ]
    )
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=subtotal_row_idx, column=col_idx)
        cell.font = total_font
        cell.fill = total_fill

    col_widths = [16, 34, 10, 14, 14, 14, 15, 15, 15, 15, 15, 16, 46]
    for idx, width in enumerate(col_widths, start=1):
        col_letter = ws.cell(row=4, column=idx).column_letter
        ws.column_dimensions[col_letter].width = width

    save_deterministic_xlsx(wb, path)


# ---------------------------------------------------------------------------
# Arc 3: Scheduled AP Payments + Staged Lone Star Amendment
# ---------------------------------------------------------------------------


def build_ap_scheduled_data() -> list[dict[str, Any]]:
    """Build scheduled vendor disbursements including LSS-2026-4189 ($25,000.00, Week 1)."""
    return [
        # Already settled prior to cutoff (tests `paid: true` exclusion from future outflows)
        {
            "invoice_ref": "TRN-2026-6620",
            "vendor": "Trane Commercial Supply",
            "branch": "DAL-01",
            "due_date": "2026-11-17",
            "forecast_week": 0,
            "amount_cents": 5_428_000,
            "paid": True,
            "status": "paid",
            "note": "Cleared on Frost Operating *4812 on 2026-11-17",
        },
        # Week 1 (ending 2026-11-27): unpaid sum = $148,750.00
        {
            "invoice_ref": "LSS-2026-4189",
            "vendor": "Lone Star HVAC Supply",
            "branch": "AUS-02",
            "due_date": "2026-11-25",
            "forecast_week": 1,
            "amount_cents": 2_500_000,  # $25,000.00 — staged deferral target
            "paid": False,
            "status": "scheduled",
            "note": "Rooftop unit replacement parts for AUS-02 retrofit project",
        },
        {
            "invoice_ref": "CAR-2026-4518",
            "vendor": "Carrier Enterprise South Central",
            "branch": "DAL-01",
            "due_date": "2026-11-24",
            "forecast_week": 1,
            "amount_cents": 6_485_000,
            "paid": False,
            "status": "scheduled",
            "note": "Hospital chiller stock coils and variable frequency drives",
        },
        {
            "invoice_ref": "JCI-2026-7712",
            "vendor": "Johnson Controls Distribution",
            "branch": "DAL-01",
            "due_date": "2026-11-25",
            "forecast_week": 1,
            "amount_cents": 3_760_000,
            "paid": False,
            "status": "scheduled",
            "note": "BACnet controller modules for Plano ISD & Parkland",
        },
        {
            "invoice_ref": "OKC-2026-1190",
            "vendor": "MidContinent Mechanical Supply",
            "branch": "TUL-03",
            "due_date": "2026-11-26",
            "forecast_week": 1,
            "amount_cents": 2_130_000,
            "paid": False,
            "status": "scheduled",
            "note": "Tulsa branch sheet metal & duct insulation batch",
        },
        # Week 2 (ending 2026-12-04): unpaid sum = $116,420.00
        {
            "invoice_ref": "TRN-2026-6744",
            "vendor": "Trane Commercial Supply",
            "branch": "DAL-01",
            "due_date": "2026-12-01",
            "forecast_week": 2,
            "amount_cents": 5_842_000,
            "paid": False,
            "status": "scheduled",
            "note": "Commercial packaged rooftop unit deposit",
        },
        {
            "invoice_ref": "FER-2026-3081",
            "vendor": "Ferguson HVAC Wholesale",
            "branch": "AUS-02",
            "due_date": "2026-12-03",
            "forecast_week": 2,
            "amount_cents": 3_410_000,
            "paid": False,
            "status": "scheduled",
            "note": "Austin commercial controls valves and actuators",
        },
        {
            "invoice_ref": "GCR-2026-9105",
            "vendor": "Gulf Coast Refrigeration Parts",
            "branch": "TUL-03",
            "due_date": "2026-12-04",
            "forecast_week": 2,
            "amount_cents": 2_390_000,
            "paid": False,
            "status": "scheduled",
            "note": "Refrigerant cylinders and rack sensors",
        },
        # Week 3 (ending 2026-12-11): unpaid sum = $108,950.00
        {
            "invoice_ref": "CAR-2026-4602",
            "vendor": "Carrier Enterprise South Central",
            "branch": "DAL-01",
            "due_date": "2026-12-08",
            "forecast_week": 3,
            "amount_cents": 6_215_000,
            "paid": False,
            "status": "scheduled",
            "note": "Dallas winter maintenance stock replenishment",
        },
        {
            "invoice_ref": "LSS-2026-4235",
            "vendor": "Lone Star HVAC Supply",
            "branch": "AUS-02",
            "due_date": "2026-12-10",
            "forecast_week": 3,
            "amount_cents": 4_680_000,
            "paid": False,
            "status": "scheduled",
            "note": "Austin air-handler motors and blower assemblies",
        },
        # Week 4 (ending 2026-12-18): unpaid sum = $112,300.00
        {
            "invoice_ref": "TRN-2026-6819",
            "vendor": "Trane Commercial Supply",
            "branch": "DAL-01",
            "due_date": "2026-12-15",
            "forecast_week": 4,
            "amount_cents": 6_950_000,
            "paid": False,
            "status": "scheduled",
            "note": "Chiller condenser bundle balance payment",
        },
        {
            "invoice_ref": "JCI-2026-7804",
            "vendor": "Johnson Controls Distribution",
            "branch": "AUS-02",
            "due_date": "2026-12-17",
            "forecast_week": 4,
            "amount_cents": 4_280_000,
            "paid": False,
            "status": "scheduled",
            "note": "Domain Northside VAV automation panels",
        },
    ]


def write_ap_scheduled_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write the AP scheduled payments CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "invoice_ref",
        "vendor",
        "branch",
        "due_date",
        "forecast_week",
        "amount",
        "paid",
        "status",
        "note",
    ]
    with path.open("w", encoding="utf-8", newline="\n") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for r in rows:
            writer.writerow(
                {
                    "invoice_ref": r["invoice_ref"],
                    "vendor": r["vendor"],
                    "branch": r["branch"],
                    "due_date": r["due_date"],
                    "forecast_week": r["forecast_week"],
                    "amount": cents_to_str(r["amount_cents"]),
                    "paid": "true" if r["paid"] else "false",
                    "status": r["status"],
                    "note": r["note"],
                }
            )


def build_incoming_amendment() -> tuple[dict[str, Any], str]:
    """Build the staged Lone Star payment deferral YAML and companion email text."""
    amendment_yaml = {
        "change_id": "CHG-LSS-4189",
        "period": DEFAULT_WEEKLY_PERIOD,
        "module": "weekly",
        "kind": "payment_deferral",
        "invoice_ref": "LSS-2026-4189",
        "vendor": "Lone Star HVAC Supply",
        "branch": "AUS-02",
        "amount": 25000.00,
        "old_due_date": "2026-11-25",
        "new_due_date": "2026-12-02",
        "old_week": 1,
        "new_week": 2,
        "requested_by": "Tomás Herrera",
        "requested_by_role": "Regional Operations Manager, Austin",
        "vendor_contact": "Derek Shaw, AR Manager, Lone Star HVAC Supply",
        "received_at": "2026-11-20T10:42:00Z",
        "companion_memo": "lone-star-amendment-email.txt",
        "summary": (
            "Defer Lone Star HVAC Supply invoice LSS-2026-4189 ($25,000.00) by 7 days "
            "from Week 1 (2026-11-25) to Week 2 (2026-12-02)."
        ),
        "rationale": (
            "Vendor confirmed a 7-day net-terms courtesy extension with zero late fee while "
            "AUS-02 rooftop unit commissioning inspection completes."
        ),
    }

    email_txt = (
        "From: Tomás Herrera <tomas.herrera@heartlandmechanical.example>\n"
        "To: Claire Vance <claire.vance@heartlandmechanical.example>\n"
        "Cc: Rachel Morales <rachel.morales@heartlandmechanical.example>\n"
        "Date: Fri, 20 Nov 2026 10:42:00 -0600\n"
        "Subject: Fwd: Payment Date Extension Confirmed — Invoice LSS-2026-4189 ($25,000.00)\n"
        "\n"
        "Claire,\n"
        "\n"
        "Confirming below that Lone Star HVAC Supply agreed to push payment of invoice\n"
        "LSS-2026-4189 ($25,000.00, AUS-02 rooftop unit replacement parts) out one week\n"
        "from Wednesday, November 25, 2026 (Week 1) to Wednesday, December 2, 2026 (Week 2)\n"
        "with no late fee or credit hold.\n"
        "\n"
        "Best,\n"
        "Tomás Herrera\n"
        "Regional Operations Manager, Austin (AUS-02)\n"
        "Heartland Mechanical Services, LLC\n"
        "\n"
        "---------- Forwarded message ----------\n"
        "From: Derek Shaw <dshaw@lonestarhvacsupply.example>\n"
        "Date: Fri, 20 Nov 2026 10:18:00 -0600\n"
        "Subject: RE: Invoice LSS-2026-4189 Payment Timing\n"
        "To: Tomás Herrera <tomas.herrera@heartlandmechanical.example>\n"
        "\n"
        "Tomás,\n"
        "\n"
        "Happy to accommodate. We have updated invoice LSS-2026-4189 ($25,000.00) in our\n"
        "billing system from due date 2026-11-25 to 2026-12-02 as a one-time 7-day courtesy\n"
        "extension while your Austin team finishes commissioning. No finance charges apply.\n"
        "\n"
        "Regards,\n"
        "Derek Shaw\n"
        "Accounts Receivable Manager | Lone Star HVAC Supply\n"
    )
    return amendment_yaml, email_txt


# ---------------------------------------------------------------------------
# 13-Week Forecast Computation (Exact Integer Cents)
# ---------------------------------------------------------------------------


def compute_13_week_forecast(
    *,
    opening_cash_cents: int,
    week1_ending: dt.date,
    inflows_by_week_cents: dict[int, int],
    recurring_outflows_by_week_cents: dict[int, int],
    biweekly_payroll_cents: int,
    biweekly_payroll_weeks: set[int],
    ap_rows: list[dict[str, Any]],
    open_review_estimates: list[dict[str, Any]],
    override_invoice_week: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Compute a 13-week cash forecast in exact integer cents."""
    override_invoice_week = override_invoice_week or {}
    ap_by_week: dict[int, int] = {w: 0 for w in range(1, 14)}
    for r in ap_rows:
        if r["paid"]:
            continue
        week = override_invoice_week.get(r["invoice_ref"], r["forecast_week"])
        if 1 <= week <= 13:
            ap_by_week[week] += r["amount_cents"]

    est_by_week: dict[int, int] = {w: 0 for w in range(1, 14)}
    est_notes_by_week: dict[int, list[str]] = {w: [] for w in range(1, 14)}
    for est in open_review_estimates:
        w = int(est["forecast_week"])
        est_by_week[w] += to_cents(str(est["amount_usd"]))
        est_notes_by_week[w].append(f"Includes {est['title']} ({cents_to_currency(to_cents(str(est['amount_usd'])))} est.)")

    weeks: list[dict[str, Any]] = []
    running = opening_cash_cents
    for w in range(1, 14):
        w_end = week1_ending + dt.timedelta(days=7 * (w - 1))
        inflow_c = inflows_by_week_cents[w]
        payroll_c = biweekly_payroll_cents if w in biweekly_payroll_weeks else 0
        outflow_c = recurring_outflows_by_week_cents[w] + ap_by_week[w] + payroll_c + est_by_week[w]
        net_c = inflow_c - outflow_c
        ending_c = running + net_c

        notes_parts: list[str] = []
        if payroll_c:
            notes_parts.append("Biweekly payroll week")
        if ap_by_week[w]:
            notes_parts.append(f"Scheduled AP {cents_to_currency(ap_by_week[w])}")
        notes_parts.extend(est_notes_by_week[w])
        if override_invoice_week:
            for inv_ref, new_w in override_invoice_week.items():
                if new_w == w:
                    notes_parts.append(f"Includes deferred {inv_ref}")

        weeks.append(
            {
                "week_num": w,
                "week_ending": w_end.isoformat(),
                "opening_cash": cents_to_float(running),
                "inflows": cents_to_float(inflow_c),
                "outflows": cents_to_float(outflow_c),
                "net_cash": cents_to_float(net_c),
                "ending_cash": cents_to_float(ending_c),
                "opening_cash_cents": running,
                "inflows_cents": inflow_c,
                "outflows_cents": outflow_c,
                "net_cash_cents": net_c,
                "ending_cash_cents": ending_c,
                "notes": "; ".join(notes_parts) if notes_parts else "Baseline operating cadence",
            }
        )
        running = ending_c
    return weeks


# ---------------------------------------------------------------------------
# Declarative Config YAML builders
# ---------------------------------------------------------------------------


def build_feeds_config() -> dict[str, Any]:
    """Build `seed/config/feeds.yaml` with column aliases, sensitivity, and Dec 1 ERP cutover."""
    bank_aliases = {
        "post_date": ["Post Date", "Date", "Transaction Date"],
        "reference": ["Reference", "Ref", "Check/Ref"],
        "description": ["Transaction Desc", "Description", "Memo"],
        "debit": ["Debit Amt", "Debit", "Withdrawal"],
        "credit": ["Credit Amt", "Credit", "Deposit"],
        "running_balance": ["Running Bal", "Balance", "Ending Balance"],
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "feeds": [
            {
                "id": "frost_operating",
                "name": "Frost Commercial Operating (*4812)",
                "module": "weekly",
                "provider": "Frost Commercial Banking",
                "owner": "Claire Vance",
                "where": "Frost Treasury Portal > Operating *4812 CSV Export",
                "due_rule": "Fridays by 17:30 CST",
                "sensitivity": "internal",
                "attachable": True,
                "format": "csv",
                "file_pattern": "sources/frost-operating-*.csv",
                "default_file": SEED_PATHS["frost_operating"],
                "active_from": "2026-05-01",
                "active_until": None,
                "replaced_by_id": None,
                "parser": {"skip_rows": 2},
                "column_aliases": bank_aliases,
            },
            {
                "id": "frost_payroll_sweep",
                "name": "Frost Payroll Sweep (*4829)",
                "module": "weekly",
                "provider": "Frost Commercial Banking",
                "owner": "Claire Vance",
                "where": "Frost Treasury Portal > Payroll Sweep *4829 CSV Export",
                "due_rule": "Fridays by 17:30 CST",
                "sensitivity": "internal",
                "attachable": True,
                "format": "csv",
                "file_pattern": "sources/frost-payroll-sweep-*.csv",
                "default_file": SEED_PATHS["frost_payroll_sweep"],
                "active_from": "2026-05-01",
                "active_until": None,
                "replaced_by_id": None,
                "parser": {"skip_rows": 2},
                "column_aliases": bank_aliases,
            },
            {
                "id": "ar_aging",
                "name": "Consolidated Commercial A/R Aging",
                "module": "weekly",
                "provider": "Branch Billing Consolidation (DAL-01 / AUS-02 / TUL-03)",
                "owner": "Rachel Morales",
                "where": "Finance SharePoint > Weekly Cash > A/R Aging Exports",
                "due_rule": "Wednesdays by 17:00 CST (2-day mid-week lag)",
                "sensitivity": "internal",
                "attachable": True,
                "format": "xlsx",
                "file_pattern": "sources/ar-aging-*.xlsx",
                "default_file": SEED_PATHS["ar_aging"],
                "active_from": "2026-05-01",
                "active_until": None,
                "replaced_by_id": None,
                "parser": {
                    "header_row": 4,
                    "skip_subtotal_labels": ["TOTAL", "Total", "Subtotal", "Grand Total"],
                    "strip_currency_symbols": True,
                },
                "column_aliases": {
                    "invoice_id": ["Invoice ID", "Invoice #", "Inv No"],
                    "customer": ["Customer Name", "Customer", "Account"],
                    "branch": ["Branch", "Branch Code", "Location"],
                    "invoice_date": ["Invoice Date", "Inv Date"],
                    "due_date": ["Due Date", "Terms Due"],
                    "days_past_due": ["Days Past Due", "Past Due Days", "Age"],
                    "current": ["Current", "0 Days"],
                    "d1_30": ["1-30 Days", "1-30"],
                    "d31_60": ["31-60 Days", "31-60"],
                    "d61_90": ["61-90 Days", "61-90"],
                    "over_90": [">90 Days", "90+ Days", "Over 90"],
                    "total": ["Total Open", "Total", "Balance Due"],
                    "note": ["Collection Note", "Notes", "Status Note"],
                },
            },
            {
                "id": "ap_scheduled",
                "name": "AP Scheduled Vendor Disbursements",
                "module": "weekly",
                "provider": "Accounts Payable Payment Queue",
                "owner": "Rachel Morales",
                "where": "Finance SharePoint > Weekly Cash > AP Payment Schedules",
                "due_rule": "Fridays by 14:00 CST",
                "sensitivity": "internal",
                "attachable": True,
                "format": "csv",
                "file_pattern": "sources/ap-scheduled-payments-*.csv",
                "default_file": SEED_PATHS["ap_scheduled"],
                "active_from": "2026-05-01",
                "active_until": None,
                "replaced_by_id": None,
                "parser": {"skip_rows": 0},
                "column_aliases": {
                    "invoice_ref": ["invoice_ref", "Invoice Ref", "Invoice #"],
                    "vendor": ["vendor", "Vendor Name", "Supplier"],
                    "branch": ["branch", "Branch", "Branch Code"],
                    "due_date": ["due_date", "Due Date", "Payment Date"],
                    "forecast_week": ["forecast_week", "Forecast Week", "Week"],
                    "amount": ["amount", "Amount", "Payment Amount"],
                    "paid": ["paid", "Paid", "Settled"],
                    "status": ["status", "Status"],
                    "note": ["note", "Notes", "Memo"],
                },
            },
            {
                "id": "tulsa_qb_ar",
                "name": "Tulsa QuickBooks Legacy A/R Feed",
                "module": "weekly",
                "provider": "QuickBooks Desktop (Tulsa Comfort Systems)",
                "owner": "Owen Gallagher",
                "where": "Tulsa Branch QuickBooks Export (merged into Consolidated A/R Aging)",
                "due_rule": "Wednesdays by 15:00 CST through 2026-11-30 cutover",
                "sensitivity": "internal",
                "attachable": True,
                "format": "xlsx",
                "file_pattern": "sources/ar-aging-*.xlsx",
                "default_file": SEED_PATHS["ar_aging"],
                "active_from": "2026-10-01",
                "active_until": "2026-11-30",
                "replaced_by_id": "unified_erp_ar",
                "parser": {"header_row": 4},
                "column_aliases": {
                    "invoice_id": ["Invoice ID", "Num"],
                    "customer": ["Customer Name", "Name"],
                    "branch": ["Branch", "Class"],
                    "total": ["Total Open", "Open Balance"],
                },
            },
            {
                "id": "unified_erp_ar",
                "name": "Unified Cloud ERP Consolidated A/R Feed",
                "module": "weekly",
                "provider": "Heartland Unified Cloud ERP",
                "owner": "Rachel Morales",
                "where": "Unified ERP > Receivables > Multi-Branch Aging API Export",
                "due_rule": "Wednesdays by 16:00 CST starting 2026-12-01",
                "sensitivity": "internal",
                "attachable": True,
                "format": "xlsx",
                "file_pattern": "sources/unified-erp-ar-*.xlsx",
                "default_file": None,
                "active_from": "2026-12-01",
                "active_until": None,
                "replaced_by_id": None,
                "replaces_id": "tulsa_qb_ar",
                "parser": {"header_row": 1},
                "column_aliases": {
                    "invoice_id": ["invoice_id", "Invoice ID"],
                    "customer": ["customer_name", "Customer Name"],
                    "branch": ["branch_code", "Branch"],
                    "total": ["open_amount", "Total Open"],
                },
            },
            {
                "id": "payroll_voucher",
                "name": "Biweekly PEO Payroll Register (Restricted)",
                "module": "payroll",
                "provider": "Heartland PEO Services",
                "owner": "Rachel Morales",
                "where": "Restricted HR/Payroll Vault > Biweekly Registers",
                "due_rule": "Biweekly Mondays by 10:00 CST",
                "sensitivity": "restricted",
                "attachable": False,
                "format": "csv",
                "file_pattern": "sources/payroll-voucher-*.csv",
                "default_file": SEED_PATHS["payroll_voucher"],
                "active_from": "2026-05-01",
                "active_until": None,
                "replaced_by_id": None,
                "parser": {"skip_rows": 0},
                "column_aliases": {
                    "employee_id": ["employee_id", "Emp ID", "Employee ID"],
                    "employee_name": ["employee_name", "Employee Name", "Name"],
                    "branch": ["branch", "Branch", "Branch Code"],
                    "dept_code": ["dept_code", "Dept Code", "Department"],
                    "job_title": ["job_title", "Title", "Job Title"],
                    "regular_pay": ["regular_pay", "Regular Pay"],
                    "overtime_pay": ["overtime_pay", "Overtime Pay"],
                    "gross_pay": ["gross_pay", "Gross Pay", "Gross Wages"],
                    "employer_fica": ["employer_fica", "ER FICA"],
                    "employer_futa_suta": ["employer_futa_suta", "ER FUTA/SUTA"],
                    "employer_taxes": ["employer_taxes", "Employer Taxes"],
                    "employer_401k_match": ["employer_401k_match", "ER 401k"],
                    "employer_medical": ["employer_medical", "ER Medical"],
                    "employer_benefits": ["employer_benefits", "Employer Benefits"],
                    "employee_withholdings": ["employee_withholdings", "EE Withholdings"],
                    "net_pay": ["net_pay", "Net Pay"],
                },
            },
            {
                "id": "payroll_funding_wire",
                "name": "Biweekly Payroll Funding Wire Confirmation",
                "module": "payroll",
                "provider": "Frost Commercial Banking / Heartland PEO",
                "owner": "Claire Vance",
                "where": "Finance SharePoint > Payroll > Wire Confirmations",
                "due_rule": "Biweekly Mondays by 12:00 CST",
                "sensitivity": "internal",
                "attachable": True,
                "format": "yaml",
                "file_pattern": "sources/payroll-funding-wire-*.yaml",
                "default_file": SEED_PATHS["payroll_funding_wire"],
                "active_from": "2026-05-01",
                "active_until": None,
                "replaced_by_id": None,
                "parser": {},
                "column_aliases": {},
            },
        ],
    }


def build_assumptions_config(
    *,
    biweekly_payroll_baseline_cents: int,
    field_payroll_wire_cents: int,
    inflows_by_week_cents: dict[int, int],
    recurring_outflows_by_week_cents: dict[int, int],
    open_review_estimates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build `seed/config/assumptions.yaml`."""
    return {
        "schema_version": SCHEMA_VERSION,
        "company": {
            "name": COMPANY_NAME,
            "branches": [
                {"code": "DAL-01", "name": "Dallas Metro Commercial", "revenue_share_pct": 55},
                {"code": "AUS-02", "name": "Austin Commercial & Controls", "revenue_share_pct": 30},
                {"code": "TUL-03", "name": "Tulsa Comfort Systems", "revenue_share_pct": 15},
            ],
            "erp_cutover_date": "2026-12-01",
        },
        "weekly": {
            "cash_floor_usd": 750000.00,
            "horizon_weeks": 13,
            "biweekly_payroll_baseline_usd": cents_to_float(biweekly_payroll_baseline_cents),
            "biweekly_payroll_weeks": [2, 4, 6, 8, 10, 12],
            "recurring_inflows_by_week": {
                w: cents_to_float(c) for w, c in sorted(inflows_by_week_cents.items())
            },
            "recurring_operating_outflows_by_week": {
                w: cents_to_float(c) for w, c in sorted(recurring_outflows_by_week_cents.items())
            },
            "open_review_estimates": open_review_estimates,
        },
        "payroll": {
            "tieout_tolerance_cents": 0,
            "erp_posting_mode": "sandbox",
            "mapping_approval_ref": None,
            "mapping_approval_owner": "Rachel Morales",
            "require_all_departments_mapped": True,
            "overtime_baseline_usd": 0.00,
            "field_batch_funding_baseline_usd": cents_to_float(field_payroll_wire_cents - 1_148_025),
        },
    }


def build_outputs_config() -> dict[str, Any]:
    """Build `seed/config/outputs.yaml`."""
    return {
        "schema_version": SCHEMA_VERSION,
        "send_mode": "draft_only",
        "approver": {
            "name": "Claire Vance",
            "title": "VP Finance",
            "email": "claire.vance@heartlandmechanical.example",
        },
        "controller": {
            "name": "Rachel Morales",
            "title": "Controller",
            "email": "rachel.morales@heartlandmechanical.example",
        },
        "operations_contacts": [
            {
                "name": "Tomás Herrera",
                "title": "Regional Operations Manager, Austin",
                "branch": "AUS-02",
                "email": "tomas.herrera@heartlandmechanical.example",
            }
        ],
        "recipients": {
            "board_pack": [
                {
                    "name": "David Park",
                    "title": "Operating Partner, Sponsor",
                    "email": "david.park@heartland-sponsor.example",
                },
                {
                    "name": "Claire Vance",
                    "title": "VP Finance",
                    "email": "claire.vance@heartlandmechanical.example",
                },
            ],
            "payroll_review": [
                {
                    "name": "Rachel Morales",
                    "title": "Controller",
                    "email": "rachel.morales@heartlandmechanical.example",
                },
                {
                    "name": "Claire Vance",
                    "title": "VP Finance",
                    "email": "claire.vance@heartlandmechanical.example",
                },
            ],
        },
        "deliverables": {
            "weekly": [
                "Weekly-Liquidity-Update.pdf",
                "13-Week-Cash-Flow.pdf",
                "Reports.html",
                "Setup.html",
                "report-model.json",
            ],
            "payroll": [
                "payroll-review.xlsx",
                "Reports.html",
                "Setup.html",
                "report-model.json",
            ],
        },
        "privacy": {
            "never_attach_sensitivity": ["restricted"],
            "cite_restricted_by_filename_and_date_only": True,
        },
    }


# ---------------------------------------------------------------------------
# Prior Issued Edition 1 (`history/issued-2026-11-13-model.json`)
# ---------------------------------------------------------------------------


def build_prior_issued_weekly_model(bank_data: dict[str, Any]) -> ReportModel:
    """Build Edition 1 (`2026-11-13`) strictly conforming to ReportModel & contracts.py."""
    opening_1113_cents = bank_data["total_1113_cents"]  # $1,795,675.65

    # Prior Edition 1 13-week forecast (Week 1 ends 2026-11-20 = shared_date)
    # Week 1 planned inflows = $368,450.00; planned outflows = $355,660.00 -> net = +$12,790.00
    # Week 1 ending cash = $1,808,465.65 (exact baseline for 2026-11-20 variance bridge!)
    prior_week_specs = [
        # (week_num, inflows_cents, outflows_cents, notes)
        (1, 36_845_000, 35_566_000, "Biweekly payroll week ($265,480 plan); planned AP & shop leases"),
        (2, 37_925_000, 26_875_000, "Includes Methodist Health Facilities INV-2026-8814 ($64,250.00) expected"),
        (3, 39_200_000, 51_610_000, "Biweekly payroll week; estimated fleet fuel card auto-draft ($14,200.00)"),
        (4, 40_500_000, 22_895_000, "Commercial service collections and scheduled vendor AP"),
        (5, 39_800_000, 49_778_000, "Biweekly payroll week; scheduled Q4 distributor payments"),
        (6, 29_500_000, 23_500_000, "Holiday week reduced collections; baseline operating outflows"),
        (7, 36_000_000, 50_048_000, "Biweekly payroll week; year-end operating cadence"),
        (8, 39_500_000, 23_500_000, "Baseline operating cadence"),
        (9, 40_200_000, 50_048_000, "Biweekly payroll week; Q1 preventive maintenance billing"),
        (10, 41_000_000, 23_500_000, "Baseline operating cadence"),
        (11, 40_500_000, 50_048_000, "Biweekly payroll week; baseline operating cadence"),
        (12, 41_200_000, 23_500_000, "Baseline operating cadence"),
        (13, 41_500_000, 50_048_000, "Biweekly payroll week; baseline operating cadence"),
    ]

    prior_weeks: list[dict[str, Any]] = []
    running = opening_1113_cents
    week1_ending_date = dt.date(2026, 11, 20)
    min_cash_cents = running
    for w_num, inf_c, out_c, notes in prior_week_specs:
        w_end = week1_ending_date + dt.timedelta(days=7 * (w_num - 1))
        net_c = inf_c - out_c
        end_c = running + net_c
        min_cash_cents = min(min_cash_cents, end_c)
        prior_weeks.append(
            {
                "week_num": w_num,
                "week_ending": w_end.isoformat(),
                "opening_cash": cents_to_float(running),
                "inflows": cents_to_float(inf_c),
                "outflows": cents_to_float(out_c),
                "net_cash": cents_to_float(net_c),
                "ending_cash": cents_to_float(end_c),
                "notes": notes,
            }
        )
        running = end_c

    week1_end_cents = to_cents(str(prior_weeks[0]["ending_cash"]))
    assert week1_end_cents == 180_846_565  # $1,808,465.65

    prior_ar_buckets = {
        "current": 1_248_600.00,
        "d1_30": 612_450.00,
        "d31_60": 273_150.00,
        "d61_90": 48_900.00,
        "over_90": 31_750.00,
    }
    prior_ar_total = round(sum(prior_ar_buckets.values()), 2)
    prior_ar_over_60 = round(prior_ar_buckets["d61_90"] + prior_ar_buckets["over_90"], 2)

    return ReportModel(
        meta=ReportMeta(
            company_name=COMPANY_NAME,
            module="weekly",
            period=PRIOR_WEEKLY_PERIOD,
            period_label="Week ended 13 November 2026",
            version=__version__,
            draft_number=1,
            status="approved",
            created_at="2026-11-13T16:15:00Z",
            approver="Claire Vance",
            is_approved=True,
            edition_number=1,
            approved_at="2026-11-13T17:05:00Z",
            approved_by="Claire Vance",
            schema_version=SCHEMA_VERSION,
        ),
        kpis=[
            KpiField(
                key="opening_cash",
                label="Opening Cash",
                value=cents_to_float(opening_1113_cents),
                unit="USD",
                status="actual",
                note="Frost Operating (*4812) + Frost Payroll Sweep (*4829) as of 2026-11-13.",
            ),
            KpiField(
                key="week1_ending_cash",
                label="Week 1 Ending Cash",
                value=prior_weeks[0]["ending_cash"],
                unit="USD",
                status="forecast",
                note="Projected consolidated cash for week ending 2026-11-20.",
            ),
            KpiField(
                key="week13_ending_cash",
                label="Week 13 Ending Cash",
                value=prior_weeks[-1]["ending_cash"],
                unit="USD",
                status="forecast",
                note="Projected consolidated cash for week ending 2027-02-12.",
            ),
            KpiField(
                key="min_cash_balance",
                label="Minimum Forecast Cash",
                value=cents_to_float(min_cash_cents),
                unit="USD",
                status="forecast",
                comparison_value=750000.00,
                comparison_label="Cash floor",
                comparison_kind="limit",
                note="Remains well above the $750,000 minimum liquidity floor.",
            ),
            KpiField(
                key="ar_total",
                label="Total Open A/R",
                value=prior_ar_total,
                unit="USD",
                status="actual",
                note="Consolidated multi-branch aging as of 2026-11-11.",
            ),
            KpiField(
                key="ar_over_60",
                label="A/R Over 60 Days",
                value=prior_ar_over_60,
                unit="USD",
                status="actual",
                note="Two accounts >60 days past due as of 2026-11-11 (Richardson ISD & Capitol Tower).",
            ),
        ],
        variance_bridge=None,
        sections={
            "cash_accounts": [
                {
                    "account_id": "frost_operating",
                    "name": "Frost Commercial Operating",
                    "bank": "Frost Commercial Banking",
                    "mask": "*4812",
                    "closing_balance": cents_to_float(bank_data["operating"]["closing_1113_cents"]),
                    "as_of": PRIOR_WEEKLY_PERIOD,
                    "source_file": "frost-operating-2026-11-13.csv",
                },
                {
                    "account_id": "frost_payroll_sweep",
                    "name": "Frost Payroll Sweep",
                    "bank": "Frost Commercial Banking",
                    "mask": "*4829",
                    "closing_balance": cents_to_float(bank_data["payroll_sweep"]["closing_1113_cents"]),
                    "as_of": PRIOR_WEEKLY_PERIOD,
                    "source_file": "frost-payroll-sweep-2026-11-13.csv",
                },
            ],
            "forecast_weeks": prior_weeks,
            "ar_aging": {
                "as_of": "2026-11-11",
                "source_file": "ar-aging-2026-11-11.xlsx",
                "total": prior_ar_total,
                "buckets": prior_ar_buckets,
                "overdue_items": [
                    {
                        "invoice_id": "INV-2026-7305",
                        "customer": "Capitol Tower Property Management",
                        "branch": "AUS-02",
                        "invoice_date": "2026-07-07",
                        "due_date": "2026-08-06",
                        "days_past_due": 97,
                        "bucket": ">90",
                        "amount": 31750.00,
                        "note": "Escalated to Controller Rachel Morales; payment plan under review",
                    },
                    {
                        "invoice_id": "INV-2026-7941",
                        "customer": "Richardson ISD Facilities",
                        "branch": "DAL-01",
                        "invoice_date": "2026-08-06",
                        "due_date": "2026-09-05",
                        "days_past_due": 67,
                        "bucket": "61-90",
                        "amount": 48900.00,
                        "note": "Awaiting signed final lien waiver from DAL-01 project manager",
                    },
                ],
            },
            "scheduled_payments": [
                {
                    "invoice_ref": "TRN-2026-6620",
                    "vendor": "Trane Commercial Supply",
                    "branch": "DAL-01",
                    "due_date": "2026-11-17",
                    "forecast_week": 1,
                    "amount": 54280.00,
                    "paid": False,
                    "status": "scheduled",
                    "note": "Commercial coils disbursement planned for Week 1",
                },
                {
                    "invoice_ref": "LSS-2026-4189",
                    "vendor": "Lone Star HVAC Supply",
                    "branch": "AUS-02",
                    "due_date": "2026-11-25",
                    "forecast_week": 2,
                    "amount": 25000.00,
                    "paid": False,
                    "status": "scheduled",
                    "note": "Rooftop unit replacement parts for AUS-02 retrofit project",
                },
            ],
            "revisions": [],
        },
        checks=[
            CheckItem(
                id="WK-NOTE-001",
                group="Weekly Cash",
                title="Edition 1 baseline controls passed",
                severity="note",
                detail="All bank tie-outs and liquidity floor checks passed for 2026-11-13 issuance.",
                owner="Claire Vance",
                next_step="Use Edition 1 as the variance baseline for week ended 2026-11-20.",
            )
        ],
        pending_changes=[],
        sources=[
            SourceRef(
                feed_id="frost_operating",
                file_name="frost-operating-2026-11-13.csv",
                as_of=PRIOR_WEEKLY_PERIOD,
                sensitivity="internal",
                attachable=True,
                detail="Prior period operating bank statement cutoff.",
            ),
            SourceRef(
                feed_id="frost_payroll_sweep",
                file_name="frost-payroll-sweep-2026-11-13.csv",
                as_of=PRIOR_WEEKLY_PERIOD,
                sensitivity="internal",
                attachable=True,
                detail="Prior period payroll sweep cutoff.",
            ),
            SourceRef(
                feed_id="ar_aging",
                file_name="ar-aging-2026-11-11.xlsx",
                as_of="2026-11-11",
                sensitivity="internal",
                attachable=True,
                detail="Prior period multi-branch A/R aging snapshot.",
            ),
        ],
        narrative={
            "headline": "Opening cash of $1.80M provides $1.05M+ liquidity headroom above the $750k floor.",
            "highlights": [
                "Week 1 (ending 2026-11-20) projected ending cash of $1,808,465.65.",
                "Methodist Health Facilities invoice INV-2026-8814 ($64,250.00) forecast for collection in Week 2 (ending 2026-11-27).",
                "Tulsa QuickBooks A/R feed remains active through 2026-11-30 ahead of the 2026-12-01 cloud ERP cutover.",
            ],
        },
        artifacts={
            "liquidity_pdf": "editions/weekly/2026-11-13/edition-001/Weekly-Liquidity-Update.pdf",
            "cash_flow_pdf": "editions/weekly/2026-11-13/edition-001/13-Week-Cash-Flow.pdf",
        },
    )


# ---------------------------------------------------------------------------
# Main generator entrypoint
# ---------------------------------------------------------------------------


def generate_all(output_root: Path = REPO_ROOT) -> dict[str, Any]:
    """Generate all 13 seed files and tests/fixtures/expected_values.json deterministically."""
    rng = random.Random(FIXED_SEED)
    seed_root = output_root / "seed"

    # 1. Payroll dataset (Arc 4)
    payroll = build_payroll_data()
    write_payroll_voucher_csv(seed_root / SEED_PATHS["payroll_voucher"], payroll["rows"])
    write_yaml(seed_root / SEED_PATHS["payroll_funding_wire"], payroll["funding_wire_yaml"])
    write_yaml(seed_root / SEED_PATHS["payroll_mappings"], payroll["mappings_yaml"])

    # 2. Bank exports (Arc 1)
    bank = build_bank_data(payroll["totals_cents"]["funding_wire_total"])
    write_bank_csv(
        seed_root / SEED_PATHS["frost_operating"],
        "Heartland Mechanical Operating",
        "*4812",
        bank["operating"]["rows"],
    )
    write_bank_csv(
        seed_root / SEED_PATHS["frost_payroll_sweep"],
        "Heartland Mechanical Payroll Sweep",
        "*4829",
        bank["payroll_sweep"]["rows"],
    )

    # 3. A/R aging XLSX (Arc 2)
    ar_data = build_ar_aging_data(rng)
    write_ar_aging_xlsx(seed_root / SEED_PATHS["ar_aging"], ar_data)

    # 4. AP scheduled payments & staged Lone Star amendment (Arc 3)
    ap_rows = build_ap_scheduled_data()
    write_ap_scheduled_csv(seed_root / SEED_PATHS["ap_scheduled"], ap_rows)
    amendment_yaml, amendment_txt = build_incoming_amendment()
    write_yaml(seed_root / SEED_PATHS["incoming_amendment_yaml"], amendment_yaml)
    amendment_txt_path = seed_root / SEED_PATHS["incoming_amendment_txt"]
    amendment_txt_path.parent.mkdir(parents=True, exist_ok=True)
    amendment_txt_path.write_text(amendment_txt, encoding="utf-8", newline="\n")

    # 5. Forecast parameters & declarative configs (Arcs 1, 3, 4, 5)
    inflows_by_week_cents = {
        1: 31_500_000,  # $315,000.00 (Thanksgiving week; Methodist $64,250 collected early on Nov 18)
        2: 39_200_000,
        3: 40_500_000,
        4: 39_800_000,
        5: 29_500_000,  # Christmas week
        6: 36_000_000,  # New Year's week
        7: 39_500_000,
        8: 40_200_000,
        9: 41_000_000,
        10: 40_500_000,
        11: 41_200_000,
        12: 41_500_000,
        13: 42_000_000,
    }
    recurring_outflows_by_week_cents = {
        1: 12_000_000,  # Weeks 1-4 pair with explicit scheduled AP schedule
        2: 12_000_000,
        3: 12_000_000,
        4: 12_000_000,
        5: 23_500_000,  # Weeks 5-13 roll to full baseline recurring operating outflows
        6: 23_500_000,
        7: 23_500_000,
        8: 23_500_000,
        9: 23_500_000,
        10: 23_500_000,
        11: 23_500_000,
        12: 23_500_000,
        13: 23_500_000,
    }
    open_review_estimates = [
        {
            "id": "WK-REV-001",
            "title": "Estimated monthly fleet fuel card auto-draft",
            "vendor": "WEX Fleet Commercial Fuel",
            "forecast_week": 2,
            "week_ending": "2026-12-04",
            "amount_usd": 14200.00,
            "severity": "open_review",
            "owner": "Claire Vance",
            "detail": (
                "Estimated monthly fleet fuel card auto-draft ($14,200.00 placeholder for Week 2) "
                "has not yet been confirmed by statement."
            ),
            "next_step": "Replace placeholder with actual WEX statement balance upon receipt (expected 2026-11-30).",
        }
    ]

    write_yaml(seed_root / SEED_PATHS["feeds_config"], build_feeds_config())
    write_yaml(
        seed_root / SEED_PATHS["assumptions_config"],
        build_assumptions_config(
            biweekly_payroll_baseline_cents=bank["biweekly_payroll_baseline_cents"],
            field_payroll_wire_cents=payroll["totals_cents"]["funding_wire_total"],
            inflows_by_week_cents=inflows_by_week_cents,
            recurring_outflows_by_week_cents=recurring_outflows_by_week_cents,
            open_review_estimates=open_review_estimates,
        ),
    )
    write_yaml(seed_root / SEED_PATHS["outputs_config"], build_outputs_config())

    # 6. Prior issued Edition 1 (`history/issued-2026-11-13-model.json`)
    prior_model = build_prior_issued_weekly_model(bank)
    write_json(seed_root / SEED_PATHS["prior_issued_weekly"], prior_model.to_dict())

    # 7. Compute baseline vs deferred 13-week forecast for expected_values.json
    baseline_weeks = compute_13_week_forecast(
        opening_cash_cents=bank["total_1120_cents"],
        week1_ending=dt.date(2026, 11, 27),
        inflows_by_week_cents=inflows_by_week_cents,
        recurring_outflows_by_week_cents=recurring_outflows_by_week_cents,
        biweekly_payroll_cents=bank["biweekly_payroll_baseline_cents"],
        biweekly_payroll_weeks={2, 4, 6, 8, 10, 12},
        ap_rows=ap_rows,
        open_review_estimates=open_review_estimates,
    )
    deferred_weeks = compute_13_week_forecast(
        opening_cash_cents=bank["total_1120_cents"],
        week1_ending=dt.date(2026, 11, 27),
        inflows_by_week_cents=inflows_by_week_cents,
        recurring_outflows_by_week_cents=recurring_outflows_by_week_cents,
        biweekly_payroll_cents=bank["biweekly_payroll_baseline_cents"],
        biweekly_payroll_weeks={2, 4, 6, 8, 10, 12},
        ap_rows=ap_rows,
        open_review_estimates=open_review_estimates,
        override_invoice_week={"LSS-2026-4189": 2},
    )

    prior_week1_end_cents = to_cents(str(prior_model.sections["forecast_weeks"][0]["ending_cash"]))
    bridge_delta_cents = bank["total_1120_cents"] - prior_week1_end_cents
    assert bridge_delta_cents == 3_414_975  # +$34,149.75

    def _strip_cents_keys(weeks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {k: v for k, v in w.items() if not k.endswith("_cents")}
            for w in weeks
        ]

    base_min_cents = min(w["ending_cash_cents"] for w in baseline_weeks)
    def_min_cents = min(w["ending_cash_cents"] for w in deferred_weeks)
    assert base_min_cents > 75_000_000  # Comfortably above $750k cash floor
    assert def_min_cents > 75_000_000

    p_tot = payroll["totals_cents"]
    expected_values = {
        "schema_version": SCHEMA_VERSION,
        "generator_seed": FIXED_SEED,
        "company_name": COMPANY_NAME,
        "periods": {
            "weekly": DEFAULT_WEEKLY_PERIOD,
            "prior_weekly": PRIOR_WEEKLY_PERIOD,
            "payroll": DEFAULT_PAYROLL_PERIOD,
        },
        "cash_floor_usd": 750000.00,
        "bank_accounts": {
            "as_of_2026_11_13": {
                "frost_operating": cents_to_float(bank["operating"]["closing_1113_cents"]),
                "frost_payroll_sweep": cents_to_float(bank["payroll_sweep"]["closing_1113_cents"]),
                "total_cash": cents_to_float(bank["total_1113_cents"]),
            },
            "as_of_2026_11_20": {
                "frost_operating": cents_to_float(bank["operating"]["closing_1120_cents"]),
                "frost_payroll_sweep": cents_to_float(bank["payroll_sweep"]["closing_1120_cents"]),
                "total_cash": cents_to_float(bank["total_1120_cents"]),
            },
            "opening_2026_11_09": {
                "frost_operating": cents_to_float(bank["operating"]["opening_1109_cents"]),
                "frost_payroll_sweep": cents_to_float(bank["payroll_sweep"]["opening_1109_cents"]),
            },
            "transaction_counts": {
                "frost_operating": len(bank["operating"]["rows"]),
                "frost_payroll_sweep": len(bank["payroll_sweep"]["rows"]),
            },
        },
        "variance_bridge": {
            "baseline_period": PRIOR_WEEKLY_PERIOD,
            "shared_date": DEFAULT_WEEKLY_PERIOD,
            "prior_value": cents_to_float(prior_week1_end_cents),
            "current_value": cents_to_float(bank["total_1120_cents"]),
            "delta": cents_to_float(bridge_delta_cents),
            "drivers": [
                {
                    "key": "early_ar_methodist",
                    "label": "Early collection — Methodist Health Facilities (INV-2026-8814)",
                    "amount": 64250.00,
                    "source_file": "frost-operating-2026-11-20.csv",
                    "source_date": "2026-11-18",
                    "source_ref": "INV-2026-8814",
                },
                {
                    "key": "payroll_overtime_20261113",
                    "label": "Seasonal overtime above baseline — 2026-11-13 biweekly payroll run",
                    "amount": -11480.25,
                    "source_file": "payroll-voucher-2026-11-15.csv",
                    "source_date": DEFAULT_PAYROLL_PERIOD,
                    "source_ref": "PEO-WIRE-2026-1115",
                },
                {
                    "key": "emergency_compressor_aus02",
                    "label": "Emergency compressor parts order — AUS-02",
                    "amount": -18620.00,
                    "source_file": "frost-operating-2026-11-20.csv",
                    "source_date": "2026-11-19",
                    "source_ref": "AUS-EMERG-9042",
                },
            ],
        },
        "ar_aging": {
            "as_of": ar_data["as_of"],
            "account_count": len(ar_data["rows"]),
            "total": cents_to_float(ar_data["total_cents"]),
            "buckets": {k: cents_to_float(v) for k, v in ar_data["buckets_cents"].items()},
            "branch_totals": {k: cents_to_float(v) for k, v in ar_data["branch_totals_cents"].items()},
            "ar_over_60": cents_to_float(ar_data["ar_over_60_cents"]),
            "overdue_items": ar_data["overdue_items"],
        },
        "weekly_forecast": {
            "open_review_fuel_estimate": 14200.00,
            "baseline": {
                "opening_cash": cents_to_float(bank["total_1120_cents"]),
                "week1_ending_cash": baseline_weeks[0]["ending_cash"],
                "week2_outflows": baseline_weeks[1]["outflows"],
                "week2_ending_cash": baseline_weeks[1]["ending_cash"],
                "week13_ending_cash": baseline_weeks[-1]["ending_cash"],
                "min_cash_balance": cents_to_float(base_min_cents),
                "weeks": _strip_cents_keys(baseline_weeks),
            },
            "deferred_lone_star": {
                "invoice_ref": "LSS-2026-4189",
                "amount": 25000.00,
                "old_due_date": "2026-11-25",
                "new_due_date": "2026-12-02",
                "old_week": 1,
                "new_week": 2,
                "week1_ending_cash": deferred_weeks[0]["ending_cash"],
                "week2_outflows": deferred_weeks[1]["outflows"],
                "week2_ending_cash": deferred_weeks[1]["ending_cash"],
                "week13_ending_cash": deferred_weeks[-1]["ending_cash"],
                "min_cash_balance": cents_to_float(def_min_cents),
                "deltas": {
                    "week1_ending_cash": cents_to_float(
                        deferred_weeks[0]["ending_cash_cents"] - baseline_weeks[0]["ending_cash_cents"]
                    ),
                    "week2_outflows": cents_to_float(
                        deferred_weeks[1]["outflows_cents"] - baseline_weeks[1]["outflows_cents"]
                    ),
                    "week2_ending_cash": cents_to_float(
                        deferred_weeks[1]["ending_cash_cents"] - baseline_weeks[1]["ending_cash_cents"]
                    ),
                    "week13_ending_cash": cents_to_float(
                        deferred_weeks[-1]["ending_cash_cents"] - baseline_weeks[-1]["ending_cash_cents"]
                    ),
                },
                "weeks": _strip_cents_keys(deferred_weeks),
            },
        },
        "payroll": {
            "pay_date": DEFAULT_PAYROLL_PERIOD,
            "pay_period_end": PRIOR_WEEKLY_PERIOD,
            "employee_count": len(payroll["rows"]),
            "mapped_employees": len(payroll["rows"]) - 1,
            "unmapped_employees": 1,
            "blocked_employee_id": "EMP-1048",
            "blocked_employee_name": "Marcus Delgado",
            "blocked_branch": "TUL-03",
            "blocked_dept_code": "TUL-INSTALL-NEW",
            "regular_wages": cents_to_float(p_tot["regular_wages"]),
            "overtime_wages": cents_to_float(p_tot["overtime_wages"]),
            "gross_wages": cents_to_float(p_tot["gross_wages"]),
            "employer_fica": cents_to_float(p_tot["employer_fica"]),
            "employer_futa_suta": cents_to_float(p_tot["employer_futa_suta"]),
            "employer_taxes": cents_to_float(p_tot["employer_taxes"]),
            "employer_401k_match": cents_to_float(p_tot["employer_401k_match"]),
            "employer_medical": cents_to_float(p_tot["employer_medical"]),
            "employer_benefits": cents_to_float(p_tot["employer_benefits"]),
            "employee_withholdings": cents_to_float(p_tot["employee_withholdings"]),
            "net_pay": cents_to_float(p_tot["net_pay"]),
            "employer_cost_total": cents_to_float(p_tot["employer_cost_total"]),
            "funding_wire_total": cents_to_float(p_tot["funding_wire_total"]),
            "variance_cents": 0,
            "mapping_approval_ref": None,
        },
        "cutover": {
            "legacy_feed_id": "tulsa_qb_ar",
            "legacy_active_until": "2026-11-30",
            "replacement_feed_id": "unified_erp_ar",
            "replacement_active_from": "2026-12-01",
        },
    }

    write_json(output_root / "tests" / "fixtures" / "expected_values.json", expected_values)
    return expected_values


def main() -> None:
    expected = generate_all(REPO_ROOT)
    print(
        f"Generated {len(SEED_PATHS)} seed files and tests/fixtures/expected_values.json "
        f"for {expected['company_name']}."
    )
    print(
        f"  Opening Cash (2026-11-20): ${expected['bank_accounts']['as_of_2026_11_20']['total_cash']:,.2f} "
        f"(Bridge Delta: +${expected['variance_bridge']['delta']:,.2f})"
    )
    print(
        f"  A/R Aging (2026-11-18):    ${expected['ar_aging']['total']:,.2f} across "
        f"{expected['ar_aging']['account_count']} accounts "
        f"(>60d: ${expected['ar_aging']['ar_over_60']:,.2f})"
    )
    print(
        f"  Payroll (2026-11-15):      {expected['payroll']['employee_count']} employees, "
        f"Wire ${expected['payroll']['funding_wire_total']:,.2f}, "
        f"Variance $0.00, Blocked: {expected['payroll']['blocked_employee_id']}"
    )


if __name__ == "__main__":
    main()
