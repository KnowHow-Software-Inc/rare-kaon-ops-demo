"""Invariant and hygiene tests for the Heartland Mechanical synthetic dataset."""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import zipfile
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import yaml
from openpyxl import load_workbook

from engine.contracts import (
    COMPANY_NAME,
    DEFAULT_PAYROLL_PERIOD,
    DEFAULT_WEEKLY_PERIOD,
    PRIOR_WEEKLY_PERIOD,
    SEED_PATHS,
    WEEKLY_KPI_KEYS,
    find_forbidden_matches,
)
from engine.model import ReportModel
from scripts.generate_demo_data import generate_all

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_ROOT = REPO_ROOT / "seed"
EXPECTED_VALUES_PATH = REPO_ROOT / "tests" / "fixtures" / "expected_values.json"


def _cents(val: str | int | float | Decimal | None) -> int:
    if val is None:
        return 0
    s = str(val).strip().replace("$", "").replace(",", "")
    if not s:
        return 0
    return int((Decimal(s) * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _load_expected() -> dict[str, Any]:
    return json.loads(EXPECTED_VALUES_PATH.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_all_seed_paths_exist_and_nonempty() -> None:
    assert EXPECTED_VALUES_PATH.is_file()
    assert EXPECTED_VALUES_PATH.stat().st_size > 0
    for key, rel_path in SEED_PATHS.items():
        full = SEED_ROOT / rel_path
        assert full.is_file(), f"Missing seed file for {key}: {rel_path}"
        assert full.stat().st_size > 0, f"Empty seed file for {key}: {rel_path}"


def test_bank_csv_quirks_and_exact_cent_rollforward() -> None:
    expected = _load_expected()
    feeds_cfg = yaml.safe_load((SEED_ROOT / SEED_PATHS["feeds_config"]).read_text(encoding="utf-8"))
    feeds_by_id = {f["id"]: f for f in feeds_cfg["feeds"]}

    def _verify_bank_csv(feed_id: str, opening_1109_cents: int) -> tuple[int, int, list[dict[str, str]]]:
        feed = feeds_by_id[feed_id]
        skip_rows = int(feed["parser"]["skip_rows"])
        assert skip_rows == 2
        lines = (SEED_ROOT / SEED_PATHS[feed_id]).read_text(encoding="utf-8").splitlines()
        assert len(lines) > skip_rows + 1
        assert "Frost Commercial Banking" in lines[0]

        reader = csv.DictReader(lines[skip_rows:])
        assert reader.fieldnames is not None
        for required_header in ("Post Date", "Transaction Desc", "Debit Amt", "Credit Amt", "Running Bal"):
            assert required_header in reader.fieldnames
        # Confirm every header is covered by feeds.yaml column_aliases
        alias_values = {v for vals in feed["column_aliases"].values() for v in vals}
        for col in reader.fieldnames:
            assert col in alias_values, f"Header {col!r} not mapped in feeds.yaml for {feed_id}"

        rows = list(reader)
        running = opening_1109_cents
        bal_1113 = opening_1109_cents
        for row in rows:
            dr = _cents(row["Debit Amt"])
            cr = _cents(row["Credit Amt"])
            reported = _cents(row["Running Bal"])
            running = running + cr - dr
            assert running == reported, f"Running balance mismatch on {feed_id} row {row}"
            if row["Post Date"] <= PRIOR_WEEKLY_PERIOD:
                bal_1113 = running
        return bal_1113, running, rows

    op_1113, op_1120, op_rows = _verify_bank_csv(
        "frost_operating", _cents(expected["bank_accounts"]["opening_2026_11_09"]["frost_operating"])
    )
    sw_1113, sw_1120, sw_rows = _verify_bank_csv(
        "frost_payroll_sweep", _cents(expected["bank_accounts"]["opening_2026_11_09"]["frost_payroll_sweep"])
    )

    assert op_1113 == _cents(expected["bank_accounts"]["as_of_2026_11_13"]["frost_operating"])
    assert sw_1113 == _cents(expected["bank_accounts"]["as_of_2026_11_13"]["frost_payroll_sweep"])
    assert op_1113 + sw_1113 == _cents(expected["bank_accounts"]["as_of_2026_11_13"]["total_cash"])

    assert op_1120 == _cents(expected["bank_accounts"]["as_of_2026_11_20"]["frost_operating"])
    assert sw_1120 == _cents(expected["bank_accounts"]["as_of_2026_11_20"]["frost_payroll_sweep"])
    assert op_1120 + sw_1120 == _cents(expected["bank_accounts"]["as_of_2026_11_20"]["total_cash"])
    assert 180_000_000 <= op_1120 + sw_1120 <= 190_000_000  # ~$1.84M

    # Verify story-arc transactions and vendor abbreviation quirk
    op_by_ref = {r["Reference"]: r for r in op_rows}
    assert _cents(op_by_ref["INV-2026-8814"]["Credit Amt"]) == 6_425_000
    assert "METHODIST HEALTH FACILITIES" in op_by_ref["INV-2026-8814"]["Transaction Desc"]
    assert _cents(op_by_ref["AUS-EMERG-9042"]["Debit Amt"]) == 1_862_000
    assert "LONE STAR HVAC SUPP TX" in op_by_ref["LSS-2026-3912"]["Transaction Desc"]

    # Verify sweep wire ties to payroll-funding-wire-2026-11-15.yaml to $0.00
    wire_cfg = yaml.safe_load((SEED_ROOT / SEED_PATHS["payroll_funding_wire"]).read_text(encoding="utf-8"))
    sw_by_ref = {r["Reference"]: r for r in sw_rows}
    assert _cents(sw_by_ref["PEO-WIRE-2026-1115"]["Debit Amt"]) == _cents(
        wire_cfg["components"]["total_funding_wire"]
    )


def test_prior_issued_weekly_model_roundtrip_and_variance_bridge() -> None:
    expected = _load_expected()
    raw = json.loads((SEED_ROOT / SEED_PATHS["prior_issued_weekly"]).read_text(encoding="utf-8"))
    model = ReportModel.from_dict(raw)

    assert model.meta.company_name == COMPANY_NAME
    assert model.meta.module == "weekly"
    assert model.meta.period == PRIOR_WEEKLY_PERIOD
    assert model.meta.status == "approved"
    assert model.meta.is_approved is True
    assert model.meta.edition_number == 1
    assert model.can_approve is True

    # Verify all canonical weekly KPI keys exist
    kpi_map = model.kpi_map()
    for key in WEEKLY_KPI_KEYS:
        assert key in kpi_map, f"Missing canonical weekly KPI key: {key}"

    # Verify weekly section schemas from contracts.py
    for section_key in ("cash_accounts", "forecast_weeks", "ar_aging", "scheduled_payments", "revisions"):
        assert section_key in model.sections

    # Opening cash on 2026-11-13 ties to bank accounts on 2026-11-13
    acct_sum = sum(_cents(a["closing_balance"]) for a in model.sections["cash_accounts"])
    assert acct_sum == _cents(kpi_map["opening_cash"].value)
    assert acct_sum == _cents(expected["bank_accounts"]["as_of_2026_11_13"]["total_cash"])

    # Verify all 13 prior forecast weeks roll forward to the penny
    weeks = model.sections["forecast_weeks"]
    assert len(weeks) == 13
    prev_end = acct_sum
    for idx, w in enumerate(weeks, start=1):
        assert w["week_num"] == idx
        assert _cents(w["opening_cash"]) == prev_end
        net = _cents(w["inflows"]) - _cents(w["outflows"])
        assert _cents(w["net_cash"]) == net
        assert _cents(w["ending_cash"]) == prev_end + net
        prev_end = _cents(w["ending_cash"])

    # Verify variance bridge math on shared_date == 2026-11-20
    assert weeks[0]["week_ending"] == DEFAULT_WEEKLY_PERIOD
    prior_val = _cents(weeks[0]["ending_cash"])
    current_val = _cents(expected["bank_accounts"]["as_of_2026_11_20"]["total_cash"])
    delta = current_val - prior_val
    assert prior_val == _cents(expected["variance_bridge"]["prior_value"])
    assert delta == _cents(expected["variance_bridge"]["delta"])
    driver_sum = sum(_cents(d["amount"]) for d in expected["variance_bridge"]["drivers"])
    assert driver_sum == delta
    assert delta == 6_425_000 - 1_148_025 - 1_862_000


def test_ar_aging_xlsx_quirks_buckets_and_overdue_trace() -> None:
    expected = _load_expected()
    wb = load_workbook(SEED_ROOT / SEED_PATHS["ar_aging"], data_only=True)
    ws = wb.active

    # Rows 1-3 are title banner; row 4 is headers
    assert COMPANY_NAME in str(ws["A1"].value)
    assert "2026-11-18" in str(ws["A2"].value)
    assert ws["A3"].value is not None

    headers = [ws.cell(row=4, column=c).value for c in range(1, 14)]
    assert headers[:6] == ["Invoice ID", "Customer Name", "Branch", "Invoice Date", "Due Date", "Days Past Due"]

    as_of = dt.date(2026, 11, 18)
    detail_rows: list[dict[str, Any]] = []
    subtotal_row: list[Any] | None = None

    for row_idx in range(5, ws.max_row + 1):
        vals = [ws.cell(row=row_idx, column=c).value for c in range(1, 14)]
        if str(vals[0]).strip().upper() == "TOTAL":
            subtotal_row = vals
            continue
        detail_rows.append(dict(zip(headers, vals)))

    assert subtotal_row is not None, "Expected bottom TOTAL subtotal row in A/R XLSX"
    assert len(detail_rows) == expected["ar_aging"]["account_count"] == 28

    # Check formatted currency string quirk
    assert str(detail_rows[0]["Total Open"]).startswith("$")

    bucket_sums = {"current": 0, "d1_30": 0, "d31_60": 0, "d61_90": 0, "over_90": 0}
    total_sum = 0
    overdue_found: dict[str, dict[str, Any]] = {}

    for r in detail_rows:
        due = dt.date.fromisoformat(str(r["Due Date"]))
        expected_dpd = max(0, (as_of - due).days)
        assert int(r["Days Past Due"]) == expected_dpd

        c_cur = _cents(r["Current"])
        c_1_30 = _cents(r["1-30 Days"])
        c_31_60 = _cents(r["31-60 Days"])
        c_61_90 = _cents(r["61-90 Days"])
        c_90p = _cents(r[">90 Days"])
        c_tot = _cents(r["Total Open"])
        assert c_cur + c_1_30 + c_31_60 + c_61_90 + c_90p == c_tot

        bucket_sums["current"] += c_cur
        bucket_sums["d1_30"] += c_1_30
        bucket_sums["d31_60"] += c_31_60
        bucket_sums["d61_90"] += c_61_90
        bucket_sums["over_90"] += c_90p
        total_sum += c_tot

        if c_61_90 > 0 or c_90p > 0:
            overdue_found[str(r["Invoice ID"])] = {
                "customer": str(r["Customer Name"]),
                "branch": str(r["Branch"]),
                "days_past_due": expected_dpd,
                "amount_cents": c_tot,
                "bucket": "61-90" if c_61_90 > 0 else ">90",
            }

    # Subtotal row matches detail sums exactly
    assert _cents(subtotal_row[6]) == bucket_sums["current"]
    assert _cents(subtotal_row[7]) == bucket_sums["d1_30"]
    assert _cents(subtotal_row[8]) == bucket_sums["d31_60"]
    assert _cents(subtotal_row[9]) == bucket_sums["d61_90"]
    assert _cents(subtotal_row[10]) == bucket_sums["over_90"]
    assert _cents(subtotal_row[11]) == total_sum

    assert total_sum == _cents(expected["ar_aging"]["total"])
    assert 210_000_000 <= total_sum <= 220_000_000  # ~$2.15M
    assert bucket_sums["d61_90"] == 7_135_000
    assert bucket_sums["over_90"] == 3_175_000
    assert bucket_sums["d61_90"] + bucket_sums["over_90"] == _cents(expected["ar_aging"]["ar_over_60"])

    # Exact 3 overdue items
    assert set(overdue_found.keys()) == {"INV-2026-7941", "INV-2026-8012", "INV-2026-7305"}
    assert overdue_found["INV-2026-7941"]["amount_cents"] == 4_890_000
    assert overdue_found["INV-2026-7941"]["days_past_due"] == 74
    assert overdue_found["INV-2026-7941"]["bucket"] == "61-90"
    assert overdue_found["INV-2026-8012"]["amount_cents"] == 2_245_000
    assert overdue_found["INV-2026-8012"]["days_past_due"] == 67
    assert overdue_found["INV-2026-8012"]["bucket"] == "61-90"
    assert overdue_found["INV-2026-7305"]["amount_cents"] == 3_175_000
    assert overdue_found["INV-2026-7305"]["days_past_due"] == 104
    assert overdue_found["INV-2026-7305"]["bucket"] == ">90"


def test_ap_schedule_amendment_and_13_week_deferral_math() -> None:
    expected = _load_expected()
    assumptions = yaml.safe_load((SEED_ROOT / SEED_PATHS["assumptions_config"]).read_text(encoding="utf-8"))
    amendment = yaml.safe_load((SEED_ROOT / SEED_PATHS["incoming_amendment_yaml"]).read_text(encoding="utf-8"))
    email_txt = (SEED_ROOT / SEED_PATHS["incoming_amendment_txt"]).read_text(encoding="utf-8")

    with (SEED_ROOT / SEED_PATHS["ap_scheduled"]).open("r", encoding="utf-8") as f:
        ap_rows = list(csv.DictReader(f))

    lss_rows = [r for r in ap_rows if r["invoice_ref"] == "LSS-2026-4189"]
    assert len(lss_rows) == 1
    lss = lss_rows[0]
    assert lss["vendor"] == "Lone Star HVAC Supply"
    assert lss["branch"] == "AUS-02"
    assert lss["due_date"] == "2026-11-25"
    assert int(lss["forecast_week"]) == 1
    assert _cents(lss["amount"]) == 2_500_000
    assert lss["paid"] == "false"

    # Staged amendment consistency
    assert amendment["invoice_ref"] == "LSS-2026-4189"
    assert _cents(amendment["amount"]) == 2_500_000
    assert amendment["old_due_date"] == "2026-11-25"
    assert amendment["new_due_date"] == "2026-12-02"
    assert amendment["old_week"] == 1
    assert amendment["new_week"] == 2
    assert "LSS-2026-4189" in email_txt
    assert "2026-12-02" in email_txt

    # Open review fleet fuel card placeholder ($14,200.00 in Week 2)
    estimates = assumptions["weekly"]["open_review_estimates"]
    assert len(estimates) == 1
    assert estimates[0]["severity"] == "open_review"
    assert _cents(estimates[0]["amount_usd"]) == 1_420_000
    assert estimates[0]["forecast_week"] == 2

    # Independently recompute baseline vs deferred 13-week forecast
    opening_cash = _cents(expected["bank_accounts"]["as_of_2026_11_20"]["total_cash"])
    cash_floor = _cents(assumptions["weekly"]["cash_floor_usd"])
    assert cash_floor == 75_000_000
    payroll_amt = _cents(assumptions["weekly"]["biweekly_payroll_baseline_usd"])
    payroll_weeks = set(assumptions["weekly"]["biweekly_payroll_weeks"])
    inflows_map = {int(k): _cents(v) for k, v in assumptions["weekly"]["recurring_inflows_by_week"].items()}
    rec_out_map = {
        int(k): _cents(v) for k, v in assumptions["weekly"]["recurring_operating_outflows_by_week"].items()
    }

    def _run_forecast(move_lss_to_week: int | None) -> list[tuple[int, int, int]]:
        ap_by_w = {w: 0 for w in range(1, 14)}
        for r in ap_rows:
            if r["paid"].lower() == "true":
                continue
            w = int(r["forecast_week"])
            if move_lss_to_week is not None and r["invoice_ref"] == "LSS-2026-4189":
                w = move_lss_to_week
            if 1 <= w <= 13:
                ap_by_w[w] += _cents(r["amount"])
        est_by_w = {w: 0 for w in range(1, 14)}
        for est in estimates:
            est_by_w[int(est["forecast_week"])] += _cents(est["amount_usd"])

        bal = opening_cash
        out: list[tuple[int, int, int]] = []
        for w in range(1, 14):
            outflow = rec_out_map[w] + ap_by_w[w] + (payroll_amt if w in payroll_weeks else 0) + est_by_w[w]
            bal = bal + inflows_map[w] - outflow
            out.append((w, outflow, bal))
        return out

    base = _run_forecast(None)
    deferred = _run_forecast(2)

    assert base[0][2] == _cents(expected["weekly_forecast"]["baseline"]["week1_ending_cash"])
    assert base[1][2] == _cents(expected["weekly_forecast"]["baseline"]["week2_ending_cash"])
    assert base[-1][2] == _cents(expected["weekly_forecast"]["baseline"]["week13_ending_cash"])
    assert deferred[0][2] == _cents(expected["weekly_forecast"]["deferred_lone_star"]["week1_ending_cash"])
    assert deferred[1][2] == _cents(expected["weekly_forecast"]["deferred_lone_star"]["week2_ending_cash"])
    assert deferred[-1][2] == _cents(expected["weekly_forecast"]["deferred_lone_star"]["week13_ending_cash"])

    # Exact engineered deferral deltas
    assert deferred[0][2] - base[0][2] == 2_500_000  # Week 1 ending cash: +$25,000.00
    assert deferred[1][1] - base[1][1] == 2_500_000  # Week 2 outflow: +$25,000.00
    assert deferred[1][2] - base[1][2] == 0          # Week 2 ending cash: $0.00 delta
    assert deferred[-1][2] - base[-1][2] == 0        # Week 13 ending cash: $0.00 delta
    assert min(b for _, _, b in base) > cash_floor + 50_000_000
    assert min(b for _, _, b in deferred) > cash_floor + 50_000_000


def test_payroll_tieout_mapping_hard_stop_and_privacy() -> None:
    expected = _load_expected()
    wire_cfg = yaml.safe_load((SEED_ROOT / SEED_PATHS["payroll_funding_wire"]).read_text(encoding="utf-8"))
    map_cfg = yaml.safe_load((SEED_ROOT / SEED_PATHS["payroll_mappings"]).read_text(encoding="utf-8"))
    assumptions = yaml.safe_load((SEED_ROOT / SEED_PATHS["assumptions_config"]).read_text(encoding="utf-8"))
    feeds_cfg = yaml.safe_load((SEED_ROOT / SEED_PATHS["feeds_config"]).read_text(encoding="utf-8"))

    with (SEED_ROOT / SEED_PATHS["payroll_voucher"]).open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 18
    assert wire_cfg["employee_count"] == 18
    assert wire_cfg["pay_date"] == DEFAULT_PAYROLL_PERIOD

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
    for r in rows:
        reg = _cents(r["regular_pay"])
        ot = _cents(r["overtime_pay"])
        gross = _cents(r["gross_pay"])
        fica = _cents(r["employer_fica"])
        futa = _cents(r["employer_futa_suta"])
        er_tax = _cents(r["employer_taxes"])
        m401 = _cents(r["employer_401k_match"])
        med = _cents(r["employer_medical"])
        er_ben = _cents(r["employer_benefits"])
        wh = _cents(r["employee_withholdings"])
        net = _cents(r["net_pay"])

        assert reg + ot == gross
        assert fica + futa == er_tax
        assert m401 + med == er_ben
        assert gross - wh == net

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

    assert sums["overtime_wages"] == 1_148_025  # $11,480.25 seasonal overtime
    employer_cost = sums["gross_wages"] + sums["employer_taxes"] + sums["employer_benefits"]
    funding_wire = sums["net_pay"] + sums["employer_taxes"] + sums["employer_benefits"]
    assert funding_wire + sums["employee_withholdings"] == employer_cost

    # Tie every component to payroll-funding-wire-2026-11-15.yaml to $0.00
    wire_comp = wire_cfg["components"]
    for key, val_cents in sums.items():
        assert _cents(wire_comp[key]) == val_cents, f"Wire mismatch on {key}"
    assert _cents(wire_comp["employer_cost_total"]) == employer_cost
    assert _cents(wire_comp["total_funding_wire"]) == funding_wire
    assert _cents(expected["payroll"]["funding_wire_total"]) == funding_wire
    assert expected["payroll"]["variance_cents"] == 0

    # Verify exactly 1 unmapped employee: EMP-1048 (Marcus Delgado, TUL-03, TUL-INSTALL-NEW)
    mapped_depts = {m["dept_code"] for m in map_cfg["mappings"] if m.get("active", True)}
    unmapped = [r for r in rows if r["dept_code"] not in mapped_depts]
    assert len(unmapped) == 1
    assert unmapped[0]["employee_id"] == "EMP-1048"
    assert unmapped[0]["employee_name"] == "Marcus Delgado"
    assert unmapped[0]["branch"] == "TUL-03"
    assert unmapped[0]["dept_code"] == "TUL-INSTALL-NEW"

    # Verify independent production write gate & restricted privacy config
    assert assumptions["payroll"]["mapping_approval_ref"] is None
    assert assumptions["payroll"]["erp_posting_mode"] == "sandbox"
    pv_feed = [f for f in feeds_cfg["feeds"] if f["id"] == "payroll_voucher"][0]
    assert pv_feed["sensitivity"] == "restricted"
    assert pv_feed["attachable"] is False


def test_feeds_yaml_erp_cutover_and_outputs_config() -> None:
    feeds_text = (SEED_ROOT / SEED_PATHS["feeds_config"]).read_text(encoding="utf-8")
    assert "&id001" not in feeds_text and "*id001" not in feeds_text

    feeds_cfg = yaml.safe_load(feeds_text)
    feeds_by_id = {f["id"]: f for f in feeds_cfg["feeds"]}
    assert feeds_by_id["tulsa_qb_ar"]["active_until"] == "2026-11-30"
    assert feeds_by_id["tulsa_qb_ar"]["replaced_by_id"] == "unified_erp_ar"
    assert feeds_by_id["unified_erp_ar"]["active_from"] == "2026-12-01"

    outputs_cfg = yaml.safe_load((SEED_ROOT / SEED_PATHS["outputs_config"]).read_text(encoding="utf-8"))
    assert outputs_cfg["send_mode"] == "draft_only"
    assert outputs_cfg["approver"]["name"] == "Claire Vance"


def test_generator_byte_determinism(tmp_path: Path) -> None:
    run_a = tmp_path / "run_a"
    run_b = tmp_path / "run_b"
    generate_all(run_a)
    generate_all(run_b)

    for key, rel_path in SEED_PATHS.items():
        repo_file = SEED_ROOT / rel_path
        file_a = run_a / "seed" / rel_path
        file_b = run_b / "seed" / rel_path
        assert _sha256(file_a) == _sha256(file_b), f"Non-deterministic output across runs for {key}"
        assert _sha256(file_a) == _sha256(repo_file), f"Committed seed file out of sync with generator: {key}"

    exp_a = run_a / "tests" / "fixtures" / "expected_values.json"
    exp_b = run_b / "tests" / "fixtures" / "expected_values.json"
    assert _sha256(exp_a) == _sha256(exp_b)
    assert _sha256(exp_a) == _sha256(EXPECTED_VALUES_PATH)


def test_zero_forbidden_terms_across_all_generated_files() -> None:
    files_to_scan = [
        REPO_ROOT / "scripts" / "generate_demo_data.py",
        EXPECTED_VALUES_PATH,
        *(SEED_ROOT / rel for rel in SEED_PATHS.values()),
    ]

    for path in files_to_scan:
        if path.suffix.lower() == ".xlsx":
            # Scan both parsed cell text and raw XML inside the XLSX archive
            wb = load_workbook(path, data_only=True)
            cell_texts: list[str] = []
            for sheet in wb.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    for cell in row:
                        if cell is not None:
                            cell_texts.append(str(cell))
            with zipfile.ZipFile(path, "r") as zf:
                xml_texts = [zf.read(name).decode("utf-8", errors="ignore") for name in zf.namelist()]
            haystack = "\n".join(cell_texts) + "\n" + "\n".join(xml_texts)
        else:
            haystack = path.read_text(encoding="utf-8")

        hits = find_forbidden_matches(haystack)
        assert hits == [], f"Forbidden hygiene matches {hits} found in {path.relative_to(REPO_ROOT)}"
