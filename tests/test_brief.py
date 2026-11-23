"""Tests for the 2-page Rare Kaon executive Session Brief (HTML + pure-Python PDF)."""

from __future__ import annotations

import json
from pathlib import Path

from engine.cli import main as cli_main
from engine.contracts import find_forbidden_matches
from engine.ingest import ingest_file
from engine.operations import decide_change
from engine.payroll import prepare_payroll
from engine.weekly import prepare_weekly
from engine.workspace import init_workspace


def _count_pdf_pages(pdf_path: Path) -> int:
    """Count ReportLab `/Type /Page` objects without requiring third-party PDF readers."""
    raw = pdf_path.read_bytes()
    return raw.count(b"/Type /Page\n") + raw.count(b"/Type /Page ")


def test_brief_baseline_and_customized_full_session_html_and_pdf(tmp_path: Path, capsys) -> None:
    ws = tmp_path / "heartland-demo"
    init_workspace(ws)

    # 1. Baseline brief right after Flow 1 (Draft 1 only)
    rc0 = cli_main(["brief", "--data", str(ws), "--json"])
    assert rc0 == 0
    env0 = json.loads(capsys.readouterr().out)
    assert env0["ok"] is True
    assert env0["data"]["page_count"] == 2
    html0 = Path(env0["data"]["brief_html"]).read_text(encoding="utf-8")
    pdf0 = Path(env0["data"]["brief_pdf"])
    assert pdf0.is_file() and pdf0.stat().st_size > 2000
    assert _count_pdf_pages(pdf0) == 2
    assert "http://" not in html0 and "https://" not in html0
    assert "Baseline Forecast &amp; Staged Revision" in html0
    assert "EMP-1048" not in html0
    assert "Marcus Delgado" not in html0

    # 2. Run Flow 3 (ingest + confirm + Draft 2) and Flow 4 (Payroll prepare)
    ingest_file(ws, period="2026-11-20", file_path="incoming/lone-star-payment-amendment-2026-11-20.yaml")
    decide_change(ws, action="confirm", change_id="CHG-LSS-4189", by="Claire Vance", reason="Approved deferral")
    prepare_weekly(ws, period="2026-11-20")
    prepare_payroll(ws, period="2026-11-15")

    # Generate customized 2-page brief for a target buyer on Microsoft 365 Copilot
    spec_payload = json.dumps(
        {
            "target_company": "Summit Dental Partners",
            "target_workflow": "weekly branch cash and clinic margin reporting",
            "target_harness": "m365_copilot",
            "target_owner": "the VP of Finance",
        }
    )
    rc1 = cli_main(["brief", "--data", str(ws), "--spec", spec_payload, "--json"])
    assert rc1 == 0
    env1 = json.loads(capsys.readouterr().out)
    assert env1["ok"] is True
    assert env1["data"]["page_count"] == 2
    assert env1["data"]["target_company"] == "Summit Dental Partners"
    assert set(env1["data"]["customized_fields"]) == {
        "target_company",
        "target_workflow",
        "target_harness",
        "target_owner",
    }

    html1 = Path(env1["data"]["brief_html"]).read_text(encoding="utf-8")
    pdf1 = Path(env1["data"]["brief_pdf"])
    assert _count_pdf_pages(pdf1) == 2

    # Verify live Draft 1 -> Draft 2 numbers, payroll hold, and customized Page 2 copy
    assert "$1,842,615.40" in html1
    assert "$1,888,865.40" in html1
    assert "$1,913,865.40" in html1
    assert "$69,738.69" in html1
    assert "Held (1 unmapped dept)" in html1
    assert "Excluded from email" in html1
    assert "Summit Dental Partners" in html1
    assert "weekly branch cash and clinic margin reporting" in html1
    assert "behind <strong>Microsoft 365 Copilot</strong>" in html1
    assert "3 preserved runs" in html1
    assert find_forbidden_matches(html1) == []

    # 3. Optional Page 3 audit appendix (`--include-log`)
    rc2 = cli_main(["brief", "--data", str(ws), "--include-log", "--json"])
    assert rc2 == 0
    env2 = json.loads(capsys.readouterr().out)
    assert env2["ok"] is True
    assert env2["data"]["page_count"] == 3
    pdf2 = Path(env2["data"]["brief_pdf"])
    assert _count_pdf_pages(pdf2) == 3
    html2 = Path(env2["data"]["brief_html"]).read_text(encoding="utf-8")
    assert "Session Run History &amp; Audit Trail" in html2
    assert "Page 3 of 3" in html2


def test_brief_rejects_invalid_spec_json(tmp_path: Path, capsys) -> None:
    ws = tmp_path / "heartland-demo"
    init_workspace(ws)

    rc = cli_main(["brief", "--data", str(ws), "--spec", "{not valid json", "--json"])
    assert rc == 1
    env = json.loads(capsys.readouterr().out)
    assert env["ok"] is False
    assert env["error_code"] == "INVALID_BRIEF_SPEC"
