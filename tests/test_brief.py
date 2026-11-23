"""Tests for the 2-page Rare Kaon executive Session Brief (HTML + pure-Python PDF)."""

from __future__ import annotations

import json
from pathlib import Path

from engine.cli import main as cli_main
from engine.contracts import find_forbidden_matches
from engine.workspace import init_workspace


def _count_pdf_pages(pdf_path: Path) -> int:
    """Count ReportLab `/Type /Page` objects without requiring third-party PDF readers."""
    raw = pdf_path.read_bytes()
    return raw.count(b"/Type /Page\n") + raw.count(b"/Type /Page ")


def _run_cli(capsys, *args: str) -> tuple[int, dict]:
    rc = cli_main(list(args))
    out = capsys.readouterr().out
    return rc, json.loads(out)


def test_brief_flow1_only_is_grounded_and_never_hallucinates_past_actions(tmp_path: Path, capsys) -> None:
    ws = tmp_path / "heartland-demo"
    init_workspace(ws)

    rc0, env0 = _run_cli(capsys, "brief", "--data", str(ws), "--json")
    assert rc0 == 0
    assert env0["ok"] is True
    assert env0["data"]["page_count"] == 2
    assert env0["data"]["target_company"] == "Your Business"
    assert env0["data"]["brief_html_rel"]
    assert env0["data"]["brief_pdf_rel"]

    html0 = Path(env0["data"]["brief_html"]).read_text(encoding="utf-8")
    pdf0 = Path(env0["data"]["brief_pdf"])
    assert pdf0.is_file() and pdf0.stat().st_size > 2000
    assert _count_pdf_pages(pdf0) == 2
    assert "http://" not in html0 and "https://" not in html0

    # Flow 1 only: capability/present framing, never claiming unexecuted actions
    assert "When asked why" not in html0
    assert "refused ERP posting" not in html0
    assert "board email" not in html0
    assert "Baseline Forecast &amp; Staged Amendment" in html0
    assert "Held (PAY-MAP-001)" in html0
    assert "Withheld from attachments" in html0
    assert "1 confidential file withheld from sharing" in html0
    assert "*4812" in html0 and "*4829" in html0
    assert "EMP-1048" not in html0
    assert "Marcus Delgado" not in html0

    # Brief outputs must NOT leak into Flow 4 board email attachable_files
    rc_ov, env_ov = _run_cli(capsys, "overview", "--data", str(ws), "--json")
    assert rc_ov == 0
    ov_paths = [f["path"] for f in env_ov["data"]["attachable_files"]]
    assert not any("Session-Brief" in p for p in ov_paths)

    rc_rdy, env_rdy = _run_cli(capsys, "readiness", "--data", str(ws), "--period", "2026-11-20", "--json")
    assert rc_rdy == 0
    rdy_paths = [f["path"] for f in env_rdy["data"]["attachable_files"]]
    assert not any("Session-Brief" in p for p in rdy_paths)


def test_brief_full_session_events_customization_prior_draft_links_and_page3_appendix(
    tmp_path: Path, capsys
) -> None:
    ws = tmp_path / "heartland-demo"
    init_workspace(ws)

    # Flow 2 interrogation (logged to session-events.jsonl)
    rc, env = _run_cli(capsys, "explain", "--data", str(ws), "--topic", "cash_bridge", "--period", "2026-11-20", "--json")
    assert rc == 0 and env["ok"] is True

    # Flow 3 ingest + confirm + prepare Draft 2
    rc, env = _run_cli(
        capsys,
        "ingest",
        "--data",
        str(ws),
        "--period",
        "2026-11-20",
        "--file",
        "incoming/lone-star-payment-amendment-2026-11-20.yaml",
        "--json",
    )
    assert rc == 0 and env["ok"] is True

    rc, env = _run_cli(
        capsys,
        "change",
        "confirm",
        "--data",
        str(ws),
        "--id",
        "CHG-LSS-4189",
        "--by",
        "Claire Vance",
        "--reason",
        "Approved deferral",
        "--json",
    )
    assert rc == 0 and env["ok"] is True

    rc, env = _run_cli(capsys, "prepare", "--data", str(ws), "--module", "weekly", "--period", "2026-11-20", "--json")
    assert rc == 0 and env["ok"] is True

    # Reports.html must preserve clickable links to Draft 1 PDFs alongside Draft 2
    reports_html = (ws / "Reports.html").read_text(encoding="utf-8")
    assert "Prior Preserved Drafts (Intact)" in reports_html
    assert "runs/weekly/2026-11-20/draft-001/Weekly-Liquidity-Update.pdf" in reports_html
    assert "runs/weekly/2026-11-20/draft-002/Weekly-Liquidity-Update.pdf" in reports_html

    # Flow 4 prepare payroll + refused production post (logged to session-events.jsonl)
    rc, env = _run_cli(capsys, "prepare", "--data", str(ws), "--module", "payroll", "--period", "2026-11-15", "--json")
    assert rc == 0 and env["ok"] is True

    rc_post, env_post = _run_cli(
        capsys,
        "post",
        "--data",
        str(ws),
        "--module",
        "payroll",
        "--period",
        "2026-11-15",
        "--target",
        "production",
        "--json",
    )
    assert rc_post == 1 and env_post["ok"] is False

    # Generate customized 2-page brief for a target buyer on Microsoft 365 Copilot
    spec_payload = json.dumps(
        {
            "target_company": "Summit Dental Partners",
            "target_workflow": "weekly branch cash and clinic margin reporting",
            "target_harness": "m365_copilot",
            "target_owner": "the VP of Finance",
            "user_focus": "Tightening weekly cash visibility across newly acquired clinics.",
        }
    )
    rc1, env1 = _run_cli(capsys, "brief", "--data", str(ws), "--spec", spec_payload, "--json")
    assert rc1 == 0 and env1["ok"] is True
    assert env1["data"]["page_count"] == 2
    assert env1["data"]["target_company"] == "Summit Dental Partners"
    assert set(env1["data"]["customized_fields"]) == {
        "target_company",
        "target_workflow",
        "target_harness",
        "target_owner",
        "user_focus",
    }

    html1 = Path(env1["data"]["brief_html"]).read_text(encoding="utf-8")
    pdf1 = Path(env1["data"]["brief_pdf"])
    assert _count_pdf_pages(pdf1) == 2

    # Engine-verified past-tense phrasing now active because explain + post refusal actually ran
    assert "When asked why opening cash beat" in html1
    assert "refused ERP posting" in html1
    assert "$1,842,615.40" in html1
    assert "$1,888,865.40" in html1
    assert "$1,913,865.40" in html1
    assert "$69,738.69" in html1
    assert "Held (PAY-MAP-001)" in html1
    assert "Withheld from attachments" in html1
    assert "Summit Dental Partners" in html1
    assert "weekly branch cash and clinic margin reporting" in html1
    assert "thin Azure adapter to run behind <strong>Microsoft 365 Copilot</strong>" in html1
    assert "Tightening weekly cash visibility across newly acquired clinics." in html1
    assert "3 preserved runs" in html1
    assert find_forbidden_matches(html1) == []

    # Optional Page 3 audit appendix (`--include-log`) shows refused post event & audit trail
    rc2, env2 = _run_cli(capsys, "brief", "--data", str(ws), "--include-log", "--json")
    assert rc2 == 0 and env2["ok"] is True
    assert env2["data"]["page_count"] == 3
    pdf2 = Path(env2["data"]["brief_pdf"])
    assert _count_pdf_pages(pdf2) == 3
    html2 = Path(env2["data"]["brief_html"]).read_text(encoding="utf-8")
    assert "Session Audit Appendix" in html2
    assert "Control &amp; Query Events" in html2
    assert '<td class="neg">BLOCKED</td>' in html2
    assert "<td>post</td>" in html2
    assert "Full log on page 3" in html2
    assert "Page 3 of 3" in html2


def test_brief_declined_amendment_and_approval_ref_and_previewed_deferral(tmp_path: Path, capsys) -> None:
    ws = tmp_path / "heartland-demo"
    init_workspace(ws)

    # 1. Stateless previewed_deferral via --spec re-verifies in memory without mutating runs/DB
    db_before = (ws / "workspace.db").read_bytes()
    preview_spec = json.dumps(
        {
            "previewed_deferral": {
                "invoice_ref": "LSS-2026-4189",
                "new_week": 2,
                "new_due_date": "2026-12-02",
            }
        }
    )
    rc_p, env_p = _run_cli(capsys, "brief", "--data", str(ws), "--spec", preview_spec, "--json")
    assert rc_p == 0 and env_p["ok"] is True
    assert (ws / "workspace.db").read_bytes() == db_before
    html_p = Path(env_p["data"]["brief_html"]).read_text(encoding="utf-8")
    assert "Stateless What-If Preview (In Memory)" in html_p
    assert "$1,913,865.40" in html_p
    assert "modified zero workspace files" in html_p

    # 2. Declined amendment renders "Amendment declined" (never "staged")
    rc, _ = _run_cli(
        capsys,
        "ingest",
        "--data",
        str(ws),
        "--period",
        "2026-11-20",
        "--file",
        "incoming/lone-star-payment-amendment-2026-11-20.yaml",
        "--json",
    )
    assert rc == 0
    rc, _ = _run_cli(
        capsys,
        "change",
        "decline",
        "--data",
        str(ws),
        "--id",
        "CHG-LSS-4189",
        "--by",
        "Claire Vance",
        "--reason",
        "Keep Week 1 schedule",
        "--json",
    )
    assert rc == 0

    # 3. Setting mapping_approval_ref removes "no written Controller sign-off" from payroll prose
    rc, _ = _run_cli(
        capsys,
        "config",
        "set",
        "--data",
        str(ws),
        "--target",
        "assumptions",
        "--path",
        "payroll.mapping_approval_ref",
        "--value",
        '"CTRL-2026-11-15-OK"',
        "--by",
        "Rachel Morales",
        "--reason",
        "Signed off",
        "--json",
    )
    assert rc == 0

    rc_d, env_d = _run_cli(capsys, "brief", "--data", str(ws), "--json")
    assert rc_d == 0 and env_d["ok"] is True
    html_d = Path(env_d["data"]["brief_html"]).read_text(encoding="utf-8")
    assert "Amendment declined" in html_d
    assert "1 amendment staged" not in html_d
    assert "no written Controller sign-off" not in html_d


def test_brief_spec_validation_and_long_text_pdf_fit(tmp_path: Path, capsys) -> None:
    ws = tmp_path / "heartland-demo"
    init_workspace(ws)

    # Invalid JSON rejected
    rc, env = _run_cli(capsys, "brief", "--data", str(ws), "--spec", "{not valid json", "--json")
    assert rc == 1 and env["ok"] is False and env["error_code"] == "INVALID_BRIEF_SPEC"

    # Numeric/currency claims in user_focus rejected
    rc_num, env_num = _run_cli(
        capsys,
        "brief",
        "--data",
        str(ws),
        "--spec",
        json.dumps({"user_focus": "Save $500,000 in Q1 across 12 branches"}),
        "--json",
    )
    assert rc_num == 1 and env_num["ok"] is False and env_num["error_code"] == "INVALID_BRIEF_SPEC"

    # Very long spec strings truncate safely and stay strictly 2 pages in PDF
    long_spec = json.dumps(
        {
            "target_company": "Northstar Multi-Site Commercial Services Holdings LLC Extended Name",
            "target_workflow": "consolidated multi-branch weekly liquidity forecasting and supplier timing governance",
            "target_owner": "the Chief Financial Officer and Regional Controller team",
            "target_systems": "legacy ERP exports, regional QuickBooks ledgers, bank CSV files, and payroll summaries across newly acquired entities",
            "target_workflows": [
                "weekly cash forecasting",
                "branch margin reporting",
                "technician utilization",
                "vendor rebate tracking",
            ],
            "lead_override": (
                "Deploying controlled automation on one recurring operational workflow produces auditable outputs "
                "immediately while building a durable queryable foundation across all connected operating systems "
                "without waiting on a multi-year enterprise software replacement."
            ),
            "next_step_override": (
                "Pick one high-friction recurring workflow and its single accountable owner to replay recent cycles "
                "side-by-side against current exports and measure turnaround, manual edits, and exceptions caught."
            ),
            "user_focus": (
                "Standardizing weekly liquidity reviews and acquisition onboarding without forcing acquired branches "
                "onto a single accounting system on day one."
            ),
        }
    )
    rc_long, env_long = _run_cli(capsys, "brief", "--data", str(ws), "--spec", long_spec, "--json")
    assert rc_long == 0 and env_long["ok"] is True
    assert env_long["data"]["page_count"] == 2
    assert _count_pdf_pages(Path(env_long["data"]["brief_pdf"])) == 2
