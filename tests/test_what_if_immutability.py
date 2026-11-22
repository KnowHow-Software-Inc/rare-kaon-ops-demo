"""Immutability tests: zero disk writes on `what-if` and data/forecast preservation on `change decline`."""

from __future__ import annotations

import hashlib
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


def _snapshot_tree(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def test_what_if_writes_zero_bytes_to_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "heartland-demo"
    rc, init_res = _cli("init", "--dest", str(ws), "--json")
    assert rc == 0 and init_res["ok"] is True

    before = _snapshot_tree(ws)

    # 1. Canonical nested override JSON
    canonical_override = json.dumps(
        {
            "defer_payment": {
                "invoice_ref": "LSS-2026-4189",
                "new_week": 2,
                "new_due_date": "2026-12-02",
            }
        }
    )
    rc, wi1 = _cli("what-if", "--data", str(ws), "--period", "2026-11-20", "--override", canonical_override, "--json")
    assert rc == 0 and wi1["ok"] is True
    assert wi1["data"]["stateless"] is True
    assert wi1["data"]["files_modified"] == 0
    assert wi1["data"]["bytes_written"] == 0
    assert wi1["data"]["deltas"]["week1_ending_cash"] == 25000.0
    assert wi1["data"]["deltas"]["week2_outflows"] == 25000.0
    assert wi1["data"]["deltas"]["week2_ending_cash"] == 0.0
    assert wi1["data"]["deltas"]["week13_ending_cash"] == 0.0

    # Every single file in workspace (including workspace.db) is byte-identical
    assert _snapshot_tree(ws) == before

    # 2. Shorthand override JSON also works and writes zero bytes
    shorthand_override = json.dumps({"invoice_ref": "LSS-2026-4189", "new_week": 2})
    rc, wi2 = _cli("what-if", "--data", str(ws), "--period", "2026-11-20", "--override", shorthand_override, "--json")
    assert rc == 0 and wi2["ok"] is True
    assert wi2["data"]["deltas"] == wi1["data"]["deltas"]
    assert _snapshot_tree(ws) == before


def test_change_decline_preserves_sources_configs_drafts_and_baseline_forecast(tmp_path: Path) -> None:
    ws = tmp_path / "heartland-demo"
    rc, _ = _cli("init", "--dest", str(ws), "--json")
    assert rc == 0

    # Ingest staged amendment
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

    # Snapshot all business data, YAML configs, history, incoming, and prior draft runs
    def _business_snapshot() -> dict[str, str]:
        snap = _snapshot_tree(ws)
        # Exclude only workspace.db (status transition), config-audit.jsonl (decision log), and Setup.html (audit view)
        return {
            k: v
            for k, v in snap.items()
            if k not in {"workspace.db", "config/config-audit.jsonl", "Setup.html"}
        }

    before_decline = _business_snapshot()

    rc, dec = _cli(
        "change",
        "decline",
        "--data",
        str(ws),
        "--id",
        "CHG-LSS-4189",
        "--by",
        "Claire Vance",
        "--reason",
        "Keep Week 1 payment on original schedule.",
        "--json",
    )
    assert rc == 0 and dec["ok"] is True
    assert dec["data"]["status"] == "declined"
    assert dec["data"]["needs_decision"] is False

    # All sources, YAML configs, prior drafts, PDFs, and Reports.html are byte-identical
    assert _business_snapshot() == before_decline

    # Audit log recorded the decline
    audit_lines = (ws / "config/config-audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(audit_lines) == 1
    entry = json.loads(audit_lines[0])
    assert entry["field_path"] == "pending_change.CHG-LSS-4189"
    assert entry["new_value"] == "declined"

    # Subsequent prepare reproduces exact baseline Week 1 ending cash (not deferred!)
    rc, prep = _cli("prepare", "--data", str(ws), "--module", "weekly", "--period", "2026-11-20", "--json")
    assert rc == 0 and prep["ok"] is True
    assert prep["data"]["kpis"]["week1_ending_cash"] == 1888865.4
    assert prep["data"]["revisions"] == []
