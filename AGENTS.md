# AGENTS.md — `rare-kaon-ops-demo`

This repository is a clean-room, MIT-licensed public demonstration plugin built by Rare Kaon.

## Non-negotiable rules

1. **Zero real client data or code:** Never copy code from client repositories or internal prototypes, and never include any real client, prospect, partner, personal identifier, real bank account number, or local home-directory path in any tracked file (verified via `find_forbidden_matches` in `engine/contracts.py`).
2. **Respect frozen contracts:** `engine/model.py` and `engine/contracts.py` are the shared interface contracts across all parallel workstreams. Do not break or rename frozen dataclass fields, YAML top-level keys, CLI command names, or renderer function signatures without coordinator approval.
3. **Respect file ownership during parallel waves:** Edit only the files assigned to your workstream so parallel agents never collide.
4. **Pure Python only for runtime dependencies:** Runtime code under `engine/` and `scripts/build_plugin.py` may depend only on Python 3.11+ standard library and the pure-Python packages pinned in `packaging/plugin-requirements.txt` (`PyYAML`, `openpyxl`, `et_xmlfile`, `reportlab`, `charset-normalizer`). Never depend on Pillow/PIL, numpy, pandas, or compiled C extensions.
5. **Exact integer-cent arithmetic:** Use `Decimal` or integer cents for all monetary math; never introduce floating-point rounding drift.
6. **Keep `seed/` read-only at runtime:** Runtime commands never mutate `seed/`. They operate exclusively on a materialized working copy (`heartland-demo/`) stamped with `.rare-kaon-demo-workspace.json`.

## File ownership by workstream

| Workstream | Owns | Must NOT edit |
| --- | --- | --- |
| **Stage 0 (Frozen contracts)** | `engine/model.py`, `engine/contracts.py`, `engine/__init__.py`, `engine/__main__.py`, `engine/render/__init__.py`, `pyproject.toml`, `LICENSE`, `.gitignore`, `.claude-plugin/plugin.json`, `packaging/plugin-requirements.txt`, `skills/ops-copilot/scripts/run_cli.py`, `tests/test_contracts.py` | — |
| **Wave 1 · A (Synthetic data)** | `scripts/generate_demo_data.py`, `seed/**`, `tests/fixtures/expected_values.json`, `tests/test_demo_data_invariants.py` | `engine/`, `skills/`, `docs/`, `quickstart/` |
| **Wave 1 · C (Renderers)** | `engine/render/pdf.py`, `engine/render/html.py`, `engine/render/xlsx.py`, `tests/test_renderers.py` | `seed/`, `skills/`, `docs/`, `engine/model.py`, `engine/contracts.py` |
| **Wave 1 · D (Skill & docs)** | `skills/ops-copilot/SKILL.md`, `docs/*.md`, `quickstart/5-prompts-in-5-minutes.md`, `README.md` | `engine/`, `seed/`, `scripts/`, `tests/` |
| **Wave 2 · B (Engine & CLI)** | `engine/config.py`, `engine/workspace.py`, `engine/ingest.py`, `engine/weekly.py`, `engine/payroll.py`, `engine/operations.py`, `engine/queries.py`, `engine/cli.py`, `scripts/build_plugin.py`, remaining `tests/test_*.py` | `seed/` (read-only), `docs/` |

## Running tests

```bash
.venv/bin/python -m pytest -v
```
