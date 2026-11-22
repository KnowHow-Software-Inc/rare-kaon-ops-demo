"""Tests for `config show`, schema-validated `config set`, `config-audit.jsonl`, and `Setup.html` refresh."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_CLI = REPO_ROOT / "skills" / "ops-copilot" / "scripts" / "run_cli.py"


def _cli(*args: str) -> tuple[int, dict[str, Any]]:
    proc = subprocess.run(
        [sys.executable, str(RUN_CLI), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    return proc.returncode, json.loads(proc.stdout)


def test_config_show_set_audit_and_setup_html_refresh(tmp_path: Path) -> None:
    ws = tmp_path / "heartland-demo"
    rc, _ = _cli("init", "--dest", str(ws), "--json")
    assert rc == 0

    # config show returns all 4 targets
    rc, show_all = _cli("config", "show", "--data", str(ws), "--json")
    assert rc == 0 and show_all["ok"] is True
    assert set(show_all["data"]["targets"]) == {"feeds", "assumptions", "outputs", "payroll-mappings"}

    # config set updates weekly.cash_floor_usd, writes audit log, refreshes Setup.html
    rc, set_ok = _cli(
        "config",
        "set",
        "--data",
        str(ws),
        "--target",
        "assumptions",
        "--path",
        "weekly.cash_floor_usd",
        "--value",
        "850000.0",
        "--by",
        "Claire Vance",
        "--reason",
        "Q4 board covenant floor increase",
        "--json",
    )
    assert rc == 0 and set_ok["ok"] is True
    assert set_ok["data"]["old_value"] == 750000.0
    assert set_ok["data"]["new_value"] == 850000.0

    # Verify config-audit.jsonl append-only trail
    audit_path = ws / "config/config-audit.jsonl"
    assert audit_path.is_file()
    lines = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["actor"] == "Claire Vance"
    assert lines[0]["field_path"] == "assumptions.weekly.cash_floor_usd"
    assert lines[0]["old_value"] == 750000.0
    assert lines[0]["new_value"] == 850000.0
    assert lines[0]["reason"] == "Q4 board covenant floor increase"

    # Verify Setup.html refreshed with both new value and audit entry
    setup_html = (ws / "Setup.html").read_text(encoding="utf-8")
    assert "$850,000.00" in setup_html
    assert "Q4 board covenant floor increase" in setup_html
    assert "assumptions.weekly.cash_floor_usd" in setup_html

    # Verify target-filtered config show reflects updated value
    rc, show_assump = _cli("config", "show", "--data", str(ws), "--target", "assumptions", "--json")
    assert rc == 0 and show_assump["data"]["config"]["weekly"]["cash_floor_usd"] == 850000.0
    assert show_assump["data"]["audit_count"] == 1


def test_config_set_rejects_invalid_schema_without_mutating_files(tmp_path: Path) -> None:
    ws = tmp_path / "heartland-demo"
    rc, _ = _cli("init", "--dest", str(ws), "--json")
    assert rc == 0

    outputs_before = (ws / "config/outputs.yaml").read_bytes()
    assumptions_before = (ws / "config/assumptions.yaml").read_bytes()
    feeds_before = (ws / "config/feeds.yaml").read_bytes()

    # 1. Cannot weaken send_mode from draft_only
    rc, err1 = _cli(
        "config",
        "set",
        "--data",
        str(ws),
        "--target",
        "outputs",
        "--path",
        "send_mode",
        "--value",
        '"auto_send"',
        "--by",
        "Claire Vance",
        "--json",
    )
    assert rc != 0 and err1["ok"] is False
    assert err1["error_code"] == "SEND_MODE_GUARDRAIL_VIOLATION"
    assert (ws / "config/outputs.yaml").read_bytes() == outputs_before

    # 2. Cannot set negative cash_floor_usd
    rc, err2 = _cli(
        "config",
        "set",
        "--data",
        str(ws),
        "--target",
        "assumptions",
        "--path",
        "weekly.cash_floor_usd",
        "--value",
        "-5000",
        "--by",
        "Claire Vance",
        "--json",
    )
    assert rc != 0 and err2["ok"] is False
    assert err2["error_code"] == "INVALID_CONFIG_SCHEMA"
    assert (ws / "config/assumptions.yaml").read_bytes() == assumptions_before

    # 3. Cannot mark restricted payroll feed attachable=true
    rc, err3 = _cli(
        "config",
        "set",
        "--data",
        str(ws),
        "--target",
        "feeds",
        "--path",
        "feeds[6].attachable",
        "--value",
        "true",
        "--by",
        "Claire Vance",
        "--json",
    )
    assert rc != 0 and err3["ok"] is False
    assert err3["error_code"] == "PRIVACY_POLICY_VIOLATION"
    assert (ws / "config/feeds.yaml").read_bytes() == feeds_before

    # 4. Missing --by refused
    rc, err4 = _cli(
        "config",
        "set",
        "--data",
        str(ws),
        "--target",
        "assumptions",
        "--path",
        "weekly.cash_floor_usd",
        "--value",
        "800000",
        "--json",
    )
    assert rc != 0 and err4["ok"] is False
    assert err4["error_code"] == "ACTOR_REQUIRED"

    # Zero audit entries written for rejected mutations
    assert not (ws / "config/config-audit.jsonl").exists()
