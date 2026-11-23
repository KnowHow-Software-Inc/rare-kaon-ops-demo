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

    # Reports.html should link to executive PDFs and omit internal report-model.json
    reports_html_text = (user_cwd / "heartland-demo" / "Reports.html").read_text(encoding="utf-8")
    assert 'href="runs/weekly/2026-11-20/draft-001/Weekly-Liquidity-Update.pdf"' in reports_html_text
    assert 'href="runs/weekly/2026-11-20/draft-001/13-Week-Cash-Flow.pdf"' in reports_html_text
    assert 'href="runs/weekly/2026-11-20/draft-001/report-model.json"' not in reports_html_text


def test_session_workspace_not_hijacked_by_preexisting_home_or_plugin_cwd(tmp_path: Path) -> None:
    """Regression test for v0.1.1: pre-existing ~/.rare-kaon-ops-demo and plugin-dir CWD."""
    import os

    zip_path = REPO_ROOT / "build" / f"rare-kaon-ops-demo-v{__version__}.zip"
    if not zip_path.is_file():
        build_plugin(REPO_ROOT / "build")

    system_python = shutil.which("python3")
    assert system_python is not None

    # Create a fake HOME that ALREADY contains a marked ~/.rare-kaon-ops-demo/heartland-demo
    fake_home = tmp_path / "fake_home"
    stale_home_ws = fake_home / ".rare-kaon-ops-demo" / "heartland-demo"
    stale_home_ws.mkdir(parents=True)
    (stale_home_ws / ".rare-kaon-demo-workspace.json").write_text(
        json.dumps({"marker": "rare-kaon-ops-demo", "schema_version": 1}),
        encoding="utf-8",
    )
    env = {**os.environ, "HOME": str(fake_home)}

    # Scenario 1: Fresh session folder with unpacked plugin inside it — must use ./heartland-demo
    session_a = tmp_path / "session_a"
    plugin_a = session_a / "rare-kaon-ops-demo-plugin"
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(plugin_a)

    run_cli_a = plugin_a / "skills" / "ops-copilot" / "scripts" / "run_cli.py"
    proc_a = subprocess.run(
        [system_python, "-S", str(run_cli_a), "overview", "--refresh", "--json"],
        cwd=str(session_a),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc_a.returncode == 0, f"Scenario 1 failed:\n{proc_a.stdout}\n{proc_a.stderr}"
    ov_a = json.loads(proc_a.stdout)
    assert Path(ov_a["data"]["workspace_dir"]).resolve() == (session_a / "heartland-demo").resolve()
    assert (session_a / "heartland-demo" / "Reports.html").is_file()
    assert (session_a / "heartland-demo" / "Setup.html").is_file()

    # Scenario 2: Agent cd'd into ./rare-kaon-ops-demo-plugin before invoking run_cli.py —
    # must materialize sibling <session_b>/heartland-demo, not ~/.rare-kaon-ops-demo/heartland-demo
    session_b = tmp_path / "session_b"
    plugin_b = session_b / "rare-kaon-ops-demo-plugin"
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(plugin_b)

    run_cli_b = plugin_b / "skills" / "ops-copilot" / "scripts" / "run_cli.py"
    proc_b = subprocess.run(
        [system_python, "-S", str(run_cli_b), "overview", "--json"],
        cwd=str(plugin_b),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc_b.returncode == 0, f"Scenario 2 failed:\n{proc_b.stdout}\n{proc_b.stderr}"
    ov_b = json.loads(proc_b.stdout)
    assert Path(ov_b["data"]["workspace_dir"]).resolve() == (session_b / "heartland-demo").resolve()
    assert (session_b / "heartland-demo" / "Reports.html").is_file()

    # Scenario 3: Plugin installed under ~/.claude/plugins/cache/... — must NOT write inside
    # .claude/plugins/, and instead fall back to ~/.rare-kaon-ops-demo/heartland-demo
    cache_plugin = fake_home / ".claude" / "plugins" / "cache" / "rare-kaon-ops-demo"
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(cache_plugin)

    # Remove the stub marker so real init runs cleanly into stale_home_ws
    shutil.rmtree(stale_home_ws)
    run_cli_c = cache_plugin / "skills" / "ops-copilot" / "scripts" / "run_cli.py"
    proc_c = subprocess.run(
        [system_python, "-S", str(run_cli_c), "overview", "--json"],
        cwd=str(cache_plugin),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc_c.returncode == 0, f"Scenario 3 failed:\n{proc_c.stdout}\n{proc_c.stderr}"
    ov_c = json.loads(proc_c.stdout)
    assert Path(ov_c["data"]["workspace_dir"]).resolve() == stale_home_ws.resolve()
    assert not (cache_plugin.parent / "heartland-demo").exists()
