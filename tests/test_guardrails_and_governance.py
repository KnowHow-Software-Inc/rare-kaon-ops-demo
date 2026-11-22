"""Governance, privacy wall, hard stops, write gates, and marked-folder reset tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = REPO_ROOT / "seed"
RUN_CLI = REPO_ROOT / "skills" / "ops-copilot" / "scripts" / "run_cli.py"


def _cli(*args: str) -> tuple[int, dict[str, Any]]:
    proc = subprocess.run(
        [sys.executable, str(RUN_CLI), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    return proc.returncode, json.loads(proc.stdout)


def _hash_dir(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_restricted_payroll_privacy_wall_across_all_surfaces(tmp_path: Path) -> None:
    ws = tmp_path / "heartland-demo"
    rc, prep = _cli("prepare", "--data", str(ws), "--module", "payroll", "--period", "2026-11-15", "--json")
    assert rc == 0 and prep["ok"] is True

    responses = [prep]
    for cmd in (
        ("overview", "--data", str(ws), "--json"),
        ("readiness", "--data", str(ws), "--period", "2026-11-20", "--json"),
        ("explain", "--data", str(ws), "--topic", "payroll", "--period", "2026-11-15", "--json"),
        ("explain", "--data", str(ws), "--topic", "cash_bridge", "--period", "2026-11-20", "--json"),
        ("explain", "--data", str(ws), "--topic", "guardrails", "--period", "2026-11-15", "--json"),
    ):
        rc, res = _cli(*cmd)
        assert rc == 0 and res["ok"] is True
        responses.append(res)

    for res in responses:
        attachable = res["data"]["attachable_files"]
        non_attachable = res["data"]["cited_non_attachable_sources"]
        attachable_names = {f["file_name"] for f in attachable}
        non_attachable_names = {f["file_name"] for f in non_attachable}

        assert "payroll-voucher-2026-11-15.csv" not in attachable_names
        assert "payroll-voucher-2026-11-15.csv" in non_attachable_names
        assert all(f.get("sensitivity") != "restricted" for f in attachable)


def test_payroll_blocking_check_and_mapping_approval_ref_gates(tmp_path: Path) -> None:
    ws = tmp_path / "heartland-demo"
    rc, _ = _cli("prepare", "--data", str(ws), "--module", "payroll", "--period", "2026-11-15", "--json")
    assert rc == 0

    # 1. Blocking check PAY-MAP-001 refuses approve, sandbox post, and production post
    rc, app_err = _cli(
        "approve", "--data", str(ws), "--module", "payroll", "--period", "2026-11-15", "--by", "Claire Vance", "--json"
    )
    assert rc != 0 and app_err["ok"] is False
    assert app_err["error_code"] == "BLOCKING_CHECKS_OPEN"

    rc, sb_err = _cli("post", "--data", str(ws), "--module", "payroll", "--period", "2026-11-15", "--target", "sandbox", "--json")
    assert rc != 0 and sb_err["ok"] is False
    assert sb_err["error_code"] == "PAYROLL_BLOCKING_CHECKS_OPEN"

    rc, prod_err = _cli("post", "--data", str(ws), "--module", "payroll", "--period", "2026-11-15", "--target", "production", "--json")
    assert rc != 0 and prod_err["ok"] is False
    assert prod_err["error_code"] == "PAYROLL_BLOCKING_CHECKS_OPEN"
    assert prod_err["details"]["mapping_approval_ref_unset"] is True

    # 2. Add missing mapping for TUL-INSTALL-NEW via config set, keep mapping_approval_ref == null
    new_mapping = json.dumps(
        {
            "dept_code": "TUL-INSTALL-NEW",
            "branch": "TUL-03",
            "erp_department": "310-TUL-INSTALL",
            "wages_account": "5110-30",
            "wages_account_name": "Install Field Labor - Tulsa",
            "payroll_tax_account": "5120-30",
            "payroll_tax_account_name": "Employer Payroll Taxes - Tulsa",
            "benefits_account": "5130-30",
            "benefits_account_name": "Employer Benefits & 401(k) - Tulsa",
            "active": True,
        }
    )
    rc, cfg_res = _cli(
        "config",
        "set",
        "--data",
        str(ws),
        "--target",
        "payroll-mappings",
        "--path",
        "mappings.append",
        "--value",
        new_mapping,
        "--by",
        "Rachel Morales",
        "--json",
    )
    assert rc == 0 and cfg_res["ok"] is True

    # Re-prepare payroll: PAY-MAP-001 clears!
    rc, prep2 = _cli("prepare", "--data", str(ws), "--module", "payroll", "--period", "2026-11-15", "--json")
    assert rc == 0 and prep2["ok"] is True
    assert prep2["data"]["blocking_checks"] == []
    assert prep2["data"]["can_approve"] is True

    # Sandbox post now succeeds with dry-run payload preview and zero network calls
    rc, sb_ok = _cli("post", "--data", str(ws), "--module", "payroll", "--period", "2026-11-15", "--target", "sandbox", "--json")
    assert rc == 0 and sb_ok["ok"] is True
    assert sb_ok["data"]["dry_run"] is True
    assert sb_ok["data"]["live_network_call"] is False
    assert sb_ok["data"]["erp_payload_preview"]["total_debits"] == 90016.31
    assert sb_ok["data"]["erp_payload_preview"]["total_credits"] == 90016.31

    # Production post is STILL refused because mapping_approval_ref is null!
    rc, prod_ref_err = _cli("post", "--data", str(ws), "--module", "payroll", "--period", "2026-11-15", "--target", "production", "--json")
    assert rc != 0 and prod_ref_err["ok"] is False
    assert prod_ref_err["error_code"] == "MAPPING_APPROVAL_REF_REQUIRED"

    # 3. Record mapping_approval_ref -> production gate passes (still dry-run, zero live network call)
    rc, ref_set = _cli(
        "config",
        "set",
        "--data",
        str(ws),
        "--target",
        "assumptions",
        "--path",
        "payroll.mapping_approval_ref",
        "--value",
        '"CTRL-MAP-2026-1120-RM"',
        "--by",
        "Rachel Morales",
        "--json",
    )
    assert rc == 0 and ref_set["ok"] is True

    rc, prod_ok = _cli("post", "--data", str(ws), "--module", "payroll", "--period", "2026-11-15", "--target", "production", "--json")
    assert rc == 0 and prod_ok["ok"] is True
    assert prod_ok["data"]["dry_run"] is True
    assert prod_ok["data"]["live_network_call"] is False


def test_open_review_does_not_block_weekly_approve_and_missing_by_refused(tmp_path: Path) -> None:
    ws = tmp_path / "heartland-demo"
    rc, _ = _cli("init", "--dest", str(ws), "--json")
    assert rc == 0

    # Missing `--by` is refused
    rc, no_by = _cli("approve", "--data", str(ws), "--module", "weekly", "--period", "2026-11-20", "--json")
    assert rc != 0 and no_by["ok"] is False
    assert no_by["error_code"] == "APPROVER_REQUIRED"

    # Open pending change blocks approval until decided
    rc, _ = _cli(
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
    rc, pend_err = _cli(
        "approve", "--data", str(ws), "--module", "weekly", "--period", "2026-11-20", "--by", "Claire Vance", "--json"
    )
    assert rc != 0 and pend_err["ok"] is False
    assert pend_err["error_code"] == "PENDING_CHANGES_OPEN"

    # Decline pending change -> weekly approve succeeds EVEN THOUGH WK-REV-001 (open_review) is open!
    rc, _ = _cli("change", "decline", "--data", str(ws), "--id", "CHG-LSS-4189", "--by", "Claire Vance", "--json")
    assert rc == 0
    rc, app_ok = _cli(
        "approve", "--data", str(ws), "--module", "weekly", "--period", "2026-11-20", "--by", "Claire Vance", "--json"
    )
    assert rc == 0 and app_ok["ok"] is True
    assert app_ok["data"]["edition_number"] == 1
    assert app_ok["data"]["approved_by"] == "Claire Vance"
    assert len(app_ok["data"]["open_review_checks_carried"]) == 1
    assert app_ok["data"]["open_review_checks_carried"][0]["id"] == "WK-REV-001"
    assert (ws / "editions/weekly/2026-11-20/edition-001/Weekly-Liquidity-Update.pdf").is_file()
    assert (ws / "runs/weekly/2026-11-20/draft-001/Weekly-Liquidity-Update.pdf").is_file()


def test_reset_marked_folder_safety_and_seed_read_only(tmp_path: Path) -> None:
    seed_before = _hash_dir(SEED_DIR)

    # Unmarked directory MUST be refused without deleting its contents
    unmarked = tmp_path / "random-folder"
    unmarked.mkdir()
    sentinel = unmarked / "important.txt"
    sentinel.write_text("do not delete", encoding="utf-8")

    rc, err = _cli("reset", "--data", str(unmarked), "--json")
    assert rc != 0 and err["ok"] is False
    assert err["error_code"] == "UNMARKED_WORKSPACE_REFUSED"
    assert sentinel.read_text(encoding="utf-8") == "do not delete"

    # Marked workspace resets cleanly to pristine state
    ws = tmp_path / "heartland-demo"
    rc, _ = _cli("init", "--dest", str(ws), "--json")
    assert rc == 0
    rc, _ = _cli(
        "config",
        "set",
        "--data",
        str(ws),
        "--target",
        "assumptions",
        "--path",
        "weekly.cash_floor_usd",
        "--value",
        "900000",
        "--by",
        "Claire Vance",
        "--json",
    )
    assert rc == 0
    assert (ws / "config/config-audit.jsonl").is_file()

    rc, rst = _cli("reset", "--data", str(ws), "--json")
    assert rc == 0 and rst["ok"] is True
    assert rst["data"]["reset"] is True
    assert not (ws / "config/config-audit.jsonl").exists()

    rc, ov = _cli("overview", "--data", str(ws), "--json")
    assert rc == 0 and ov["data"]["governance"]["cash_floor_usd"] == 750000.0
    assert ov["data"]["run_count"] == 1
    assert ov["data"]["edition_count"] == 0

    # Bundled seed/ stayed strictly read-only throughout
    assert _hash_dir(SEED_DIR) == seed_before
