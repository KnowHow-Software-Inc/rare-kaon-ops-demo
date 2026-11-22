"""Packaging, pure-Python portability (`python3 -S`), and zero-leakage tests for the plugin ZIP."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

from engine import __version__
from engine.contracts import find_forbidden_matches
from scripts.build_plugin import NATIVE_EXTS, build_plugin

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_build_plugin_zip_unpacked_python3_isolated_execution_and_zero_forbidden_terms(tmp_path: Path) -> None:
    # Build plugin ZIP into build/ (or fresh test dir; build into REPO_ROOT/build so release artifact stays up to date)
    build_res = build_plugin(REPO_ROOT / "build")
    zip_path = Path(build_res["zip_path"])
    sha_path = Path(build_res["sha256_path"])

    assert zip_path.is_file()
    assert zip_path.name == f"rare-kaon-ops-demo-v{__version__}.zip"
    assert sha_path.is_file()

    # Verify SHA-256 checksum matches file bytes
    actual_sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    assert actual_sha == build_res["sha256"]
    assert sha_path.read_text(encoding="utf-8").strip().startswith(actual_sha)

    # Unpack into a clean temp directory
    unpacked = tmp_path / "unpacked_plugin"
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        # Zero native binaries in the archive
        native_hits = [n for n in names if Path(n).suffix.lower() in NATIVE_EXTS]
        assert native_hits == [], f"Native binaries found in plugin ZIP: {native_hits}"
        # Required top-level plugin structure present
        assert ".claude-plugin/plugin.json" in names
        assert "skills/ops-copilot/SKILL.md" in names
        assert "skills/ops-copilot/scripts/run_cli.py" in names
        assert "release.json" in names
        assert "LICENSE" in names
        assert "README.md" in names
        zf.extractall(unpacked)

    # Zero forbidden hygiene matches across EVERY non-vendor file in unpacked plugin
    text_exts = {".py", ".md", ".yaml", ".yml", ".json", ".csv", ".txt", ".html", ".toml"}
    for path in sorted(unpacked.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(unpacked)
        if rel.parts and rel.parts[0] == "vendor":
            continue
        if path.suffix.lower() not in text_exts and path.name != "LICENSE":
            continue
        haystack = path.read_text(encoding="utf-8", errors="ignore")
        hits = find_forbidden_matches(haystack)
        if rel.as_posix() == "engine/contracts.py":
            # contracts.py intentionally defines generic OS path patterns (`FORBIDDEN_PATH_PATTERNS`)
            hits = [h for h in hits if not h.startswith("path:")]
        assert hits == [], f"Forbidden hygiene matches {hits} found in unpacked plugin file {rel}"

    # Execute with system `python3 -S` (outside .venv, no site-packages!) in a clean user working directory
    system_python = shutil.which("python3")
    assert system_python is not None, "System python3 required for isolated smoke test"

    user_cwd = tmp_path / "clean_user_dir"
    user_cwd.mkdir()
    run_cli = unpacked / "skills" / "ops-copilot" / "scripts" / "run_cli.py"

    proc_ov = subprocess.run(
        [system_python, "-S", str(run_cli), "overview", "--json"],
        cwd=str(user_cwd),
        capture_output=True,
        text=True,
    )
    assert proc_ov.returncode == 0, f"overview failed under python3 -S:\nSTDOUT: {proc_ov.stdout}\nSTDERR: {proc_ov.stderr}"
    ov = json.loads(proc_ov.stdout)
    assert ov["ok"] is True
    assert ov["data"]["company_name"] == "Heartland Mechanical Services, LLC"
    assert (user_cwd / "heartland-demo" / "Reports.html").is_file()
    assert (user_cwd / "heartland-demo" / "Setup.html").is_file()

    proc_rdy = subprocess.run(
        [system_python, "-S", str(run_cli), "readiness", "--period", "2026-11-20", "--json"],
        cwd=str(user_cwd),
        capture_output=True,
        text=True,
    )
    assert proc_rdy.returncode == 0, f"readiness failed under python3 -S:\nSTDOUT: {proc_rdy.stdout}\nSTDERR: {proc_rdy.stderr}"
    rdy = json.loads(proc_rdy.stdout)
    assert rdy["ok"] is True
    assert rdy["data"]["modules"]["weekly"]["kpis"]["opening_cash"] == 1842615.4
    assert rdy["data"]["modules"]["payroll"]["status"] == "blocked"
