#!/usr/bin/env python3
"""Standalone plugin CLI launcher wiring bundled `vendor/` (or local `.venv`) + package root."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

# 1. Bundled pure-Python dependencies (Release ZIP) and package root
for candidate in (ROOT / "vendor", ROOT):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

# 2. Local `.venv` fallback when invoked from a git/marketplace clone with system python3
venv_dir = ROOT / ".venv"
if venv_dir.is_dir():
    for sp in sorted(venv_dir.glob("lib/python*/site-packages")) + [venv_dir / "Lib" / "site-packages"]:
        if sp.is_dir() and str(sp) not in sys.path:
            sys.path.append(str(sp))

try:
    import openpyxl  # noqa: F401
    import reportlab  # noqa: F401
    import yaml  # noqa: F401
except ImportError as exc:
    print(
        json.dumps(
            {
                "ok": False,
                "command": "bootstrap",
                "error_code": "MISSING_RUNTIME_DEPENDENCIES",
                "cause": f"Required pure-Python runtime package missing: {exc.name}",
                "suggested_action": (
                    "Use the self-contained Release ZIP (`rare-kaon-ops-demo-v0.1.2.zip`, which bundles `vendor/`), "
                    "or install dependencies once via `pip install -r packaging/plugin-requirements.txt`."
                ),
            }
        )
    )
    raise SystemExit(1)

from engine.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
