"""End-to-end engine & CLI parity tests against `tests/fixtures/expected_values.json`."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from engine.contracts import EXPLAIN_TOPICS, PAYROLL_KPI_KEYS, WEEKLY_KPI_KEYS
from engine.model import ReportModel

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_PATH = REPO_ROOT / "tests" / "fixtures" / "expected_values.json"
RUN_CLI = REPO_ROOT / "skills" / "ops-copilot" / "scripts" / "run_cli.py"


def _expected() -> dict[str, Any]:
    return json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))


def _cli(*args: str, cwd: Path | None = None) -> tuple[int, dict[str, Any]]:
    proc = subprocess.run(
        [sys.executable, str(RUN_CLI), *args],
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
    )
    payload = json.loads(proc.stdout)
    return proc.returncode, payload


def test_weekly_baseline_and_deferred_exact_parity(tmp_path: Path) -> None:
    expected = _expected()
    ws = tmp_path / "heartland-demo"

    # First command auto-materializes workspace and prepares baseline Weekly Draft 1
    rc, ov = _cli("overview", "--data", str(ws), "--refresh", "--json")
    assert rc == 0 and ov["ok"] is True
    assert (ws / "Reports.html").is_file()
    assert (ws / "Setup.html").is_file()

    draft1_model_path = ws / "runs/weekly/2026-11-20/draft-001/report-model.json"
    assert draft1_model_path.is_file()
    draft1_bytes_before = draft1_model_path.read_bytes()
    draft1 = ReportModel.from_dict(json.loads(draft1_model_path.read_text(encoding="utf-8")))

    # Verify all canonical weekly KPI keys match expected_values.json to $0.00
    kmap = draft1.kpi_map()
    assert tuple(k.key for k in draft1.kpis) == WEEKLY_KPI_KEYS
    base_exp = expected["weekly_forecast"]["baseline"]
    assert kmap["opening_cash"].value == base_exp["opening_cash"]
    assert kmap["week1_ending_cash"].value == base_exp["week1_ending_cash"]
    assert kmap["week13_ending_cash"].value == base_exp["week13_ending_cash"]
    assert kmap["min_cash_balance"].value == base_exp["min_cash_balance"]
    assert kmap["ar_total"].value == expected["ar_aging"]["total"]
    assert kmap["ar_over_60"].value == expected["ar_aging"]["ar_over_60"]

    # Bank account balances
    accts = {a["account_id"]: a["closing_balance"] for a in draft1.sections["cash_accounts"]}
    assert accts["frost_operating"] == expected["bank_accounts"]["as_of_2026_11_20"]["frost_operating"]
    assert accts["frost_payroll_sweep"] == expected["bank_accounts"]["as_of_2026_11_20"]["frost_payroll_sweep"]

    # Variance bridge exact match
    vb = draft1.variance_bridge
    assert vb is not None
    vb_exp = expected["variance_bridge"]
    assert vb.baseline_period == vb_exp["baseline_period"]
    assert vb.shared_date == vb_exp["shared_date"]
    assert vb.prior_value == vb_exp["prior_value"]
    assert vb.current_value == vb_exp["current_value"]
    assert vb.delta == vb_exp["delta"]
    assert len(vb.drivers) == len(vb_exp["drivers"])
    for drv, exp_d in zip(vb.drivers, vb_exp["drivers"]):
        assert drv.key == exp_d["key"]
        assert drv.amount == exp_d["amount"]
        assert drv.source_file == exp_d["source_file"]
        assert drv.source_date == exp_d["source_date"]
        assert drv.source_ref == exp_d["source_ref"]

    # A/R aging buckets & overdue items exact match
    ar = draft1.sections["ar_aging"]
    assert ar["as_of"] == expected["ar_aging"]["as_of"]
    assert ar["total"] == expected["ar_aging"]["total"]
    assert ar["buckets"] == expected["ar_aging"]["buckets"]
    assert ar["overdue_items"] == expected["ar_aging"]["overdue_items"]

    # All 13 baseline forecast weeks match expected_values.json to $0.00
    assert len(draft1.sections["forecast_weeks"]) == 13
    for w, exp_w in zip(draft1.sections["forecast_weeks"], base_exp["weeks"]):
        assert w["week_num"] == exp_w["week_num"]
        assert w["week_ending"] == exp_w["week_ending"]
        assert w["opening_cash"] == exp_w["opening_cash"]
        assert w["inflows"] == exp_w["inflows"]
        assert w["outflows"] == exp_w["outflows"]
        assert w["net_cash"] == exp_w["net_cash"]
        assert w["ending_cash"] == exp_w["ending_cash"]

    # Non-blocking open_review check WK-REV-001
    assert len(draft1.blocking_checks) == 0
    assert len(draft1.open_review_checks) == 1
    assert draft1.open_review_checks[0].id == "WK-REV-001"
    assert draft1.open_review_checks[0].amount == expected["weekly_forecast"]["open_review_fuel_estimate"]
    assert draft1.open_review_checks[0].blocks_approval is False
    assert draft1.can_approve is True

    # Single-model KPI parity in Reports.html
    reports_html = (ws / "Reports.html").read_text(encoding="utf-8")
    assert "$1,842,615.40" in reports_html
    assert "$2,178,115.40" in reports_html
    assert "$103,100.00" in reports_html

    # Flow 3 Commit: ingest amendment -> confirm -> prepare Draft 2
    rc, ing = _cli(
        "ingest",
        "--data",
        str(ws),
        "--period",
        "2026-11-20",
        "--file",
        "incoming/lone-star-payment-amendment-2026-11-20.yaml",
        "--json",
    )
    assert rc == 0 and ing["ok"] is True
    assert ing["data"]["pending_change"]["id"] == "CHG-LSS-4189"

    rc, chg = _cli(
        "change",
        "confirm",
        "--data",
        str(ws),
        "--id",
        "CHG-LSS-4189",
        "--by",
        "Claire Vance",
        "--json",
    )
    assert rc == 0 and chg["ok"] is True
    assert chg["data"]["status"] == "confirmed"

    rc, prep2 = _cli("prepare", "--data", str(ws), "--module", "weekly", "--period", "2026-11-20", "--json")
    assert rc == 0 and prep2["ok"] is True
    assert prep2["data"]["draft_number"] == 2

    # Draft 1 remains untouched & immutable
    assert draft1_model_path.read_bytes() == draft1_bytes_before

    # Draft 2 matches deferred_lone_star golden values to $0.00
    draft2_path = ws / "runs/weekly/2026-11-20/draft-002/report-model.json"
    draft2 = ReportModel.from_dict(json.loads(draft2_path.read_text(encoding="utf-8")))
    def_exp = expected["weekly_forecast"]["deferred_lone_star"]
    k2 = draft2.kpi_map()
    assert k2["week1_ending_cash"].value == def_exp["week1_ending_cash"]
    assert k2["week13_ending_cash"].value == def_exp["week13_ending_cash"]
    assert k2["min_cash_balance"].value == def_exp["min_cash_balance"]
    assert draft2.sections["forecast_weeks"][1]["outflows"] == def_exp["week2_outflows"]
    assert draft2.sections["forecast_weeks"][1]["ending_cash"] == def_exp["week2_ending_cash"]

    for w, exp_w in zip(draft2.sections["forecast_weeks"], def_exp["weeks"]):
        assert w["outflows"] == exp_w["outflows"]
        assert w["ending_cash"] == exp_w["ending_cash"]

    # Invoice LSS-2026-4189 moved strictly once (zero double-counting)
    lss_rows = [r for r in draft2.sections["scheduled_payments"] if r["invoice_ref"] == "LSS-2026-4189"]
    assert len(lss_rows) == 1
    assert lss_rows[0]["forecast_week"] == 2
    assert lss_rows[0]["due_date"] == "2026-12-02"
    assert len(draft2.sections["revisions"]) == 1
    assert draft2.sections["revisions"][0]["change_id"] == "CHG-LSS-4189"


def test_payroll_exact_parity_and_blocking_hard_stop(tmp_path: Path) -> None:
    expected = _expected()["payroll"]
    ws = tmp_path / "heartland-demo"

    rc, prep = _cli("prepare", "--data", str(ws), "--module", "payroll", "--period", "2026-11-15", "--json")
    assert rc == 0 and prep["ok"] is True
    assert prep["data"]["status"] == "blocked"
    assert prep["data"]["can_approve"] is False

    model_path = ws / "runs/payroll/2026-11-15/draft-001/report-model.json"
    model = ReportModel.from_dict(json.loads(model_path.read_text(encoding="utf-8")))

    assert tuple(k.key for k in model.kpis) == PAYROLL_KPI_KEYS
    kmap = model.kpi_map()
    assert kmap["gross_wages"].value == expected["gross_wages"]
    assert kmap["employer_cost_total"].value == expected["employer_cost_total"]
    assert kmap["funding_wire_total"].value == expected["funding_wire_total"]
    assert kmap["tieout_variance"].value == 0.0
    assert kmap["mapped_employees"].value == expected["mapped_employees"]
    assert kmap["blocking_exceptions"].value == expected["unmapped_employees"]

    summary = model.sections["summary"]
    assert summary["employee_count"] == expected["employee_count"]
    assert summary["regular_wages"] == expected["regular_wages"]
    assert summary["overtime_wages"] == expected["overtime_wages"]
    assert summary["employer_taxes"] == expected["employer_taxes"]
    assert summary["employer_benefits"] == expected["employer_benefits"]
    assert summary["employee_withholdings"] == expected["employee_withholdings"]
    assert summary["net_pay"] == expected["net_pay"]
    assert summary["total_funding_wire"] == expected["funding_wire_total"]
    assert summary["variance_cents"] == 0
    assert summary["mapping_approval_ref"] is None
    assert summary["journal_debits_total"] == expected["employer_cost_total"]
    assert summary["journal_credits_total"] == expected["employer_cost_total"]

    # All tie-out components pass with $0.00 difference
    assert all(row["status"] == "pass" and row["difference"] == 0.0 for row in model.sections["tieout"])

    # PAY-MAP-001 blocking check for EMP-1048
    assert len(model.blocking_checks) == 1
    assert model.blocking_checks[0].id == "PAY-MAP-001"
    assert model.blocking_checks[0].blocks_approval is True
    exc = model.sections["exceptions"]
    assert len(exc) == 1
    assert exc[0]["employee_id"] == expected["blocked_employee_id"]
    assert exc[0]["employee_name"] == expected["blocked_employee_name"]
    assert exc[0]["branch"] == expected["blocked_branch"]
    assert exc[0]["dept_code"] == expected["blocked_dept_code"]

    # Workbook rendered with 5 tabs and BLOCKED banner
    xlsx_path = ws / "runs/payroll/2026-11-15/draft-001/payroll-review.xlsx"
    assert xlsx_path.is_file()
    wb = load_workbook(xlsx_path)
    assert wb.sheetnames == ["Runs", "Tie-Out", "Checks", "Exceptions", "Draft Journal"]
    banner = str(wb["Draft Journal"].cell(row=4, column=1).value or "")
    assert "BLOCKED" in banner and "DO NOT POST" in banner


def test_all_explain_topics_and_readiness_and_invalid_ingest(tmp_path: Path) -> None:
    ws = tmp_path / "heartland-demo"

    rc, rdy = _cli("readiness", "--data", str(ws), "--period", "2026-11-20", "--json")
    assert rc == 0 and rdy["ok"] is True
    assert set(rdy["data"]["modules"].keys()) == {"weekly", "payroll", "configuration"}
    assert rdy["data"]["cutover"]["legacy_feed_id"] == "tulsa_qb_ar"
    assert rdy["data"]["cutover"]["replacement_feed_id"] == "unified_erp_ar"

    for topic in EXPLAIN_TOPICS:
        rc, exp = _cli("explain", "--data", str(ws), "--topic", topic, "--json")
        assert rc == 0 and exp["ok"] is True, f"explain failed for topic {topic}: {exp}"
        assert exp["data"]["topic"] == topic
        assert "attachable_files" in exp["data"]
        assert "cited_non_attachable_sources" in exp["data"]

    # Invalid CSV schema must fail without mutating workspace sources/
    bad_csv = tmp_path / "frost-operating-2026-11-27.csv"
    bad_csv.write_text("Header1,Header2\nfoo,bar\n", encoding="utf-8")
    sources_before = sorted(p.name for p in (ws / "sources").iterdir())
    rc, err = _cli("ingest", "--data", str(ws), "--period", "2026-11-20", "--file", str(bad_csv), "--json")
    assert rc != 0 and err["ok"] is False
    assert err["error_code"] == "INVALID_FEED_SCHEMA"
    assert sorted(p.name for p in (ws / "sources").iterdir()) == sources_before
