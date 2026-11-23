# Rare Kaon Ops Demo

A clean-room, MIT-licensed **Portfolio Finance & Ops Co-Pilot** plugin that runs deterministic weekly cash forecasting, source-traced variance and A/R interrogation, stateless `what-if` modeling, and hard-stopped payroll controls against a bundled synthetic company (**Heartland Mechanical Services, LLC**).

Rare Kaon publishes this architecture in working code rather than hiding it behind slides: if this is what we give away as an open demonstration, evaluate what we build under contract against your real operating systems.

## Quick Start

### Fastest Path — One-Paste Self-Install Prompt (Claude Code, Claude Desktop Code Tab, OpenAI Codex)

Paste this single prompt into your agent (requires Python 3.11+; zero `pip install` or `git clone` needed):

```text
Install and run the Rare Kaon Ops Demo (v0.1.1): download and extract https://github.com/KnowHow-Software-Inc/rare-kaon-ops-demo/releases/download/v0.1.1/rare-kaon-ops-demo-v0.1.1.zip into ./rare-kaon-ops-demo-plugin (all pure-Python dependencies are already bundled in vendor/; stay in the session root folder rather than cd'ing into the plugin folder), read ./rare-kaon-ops-demo-plugin/skills/ops-copilot/SKILL.md, and immediately run Flow 1: check if Heartland Mechanical is ready to run this week's finance pack, show what systems feed it and what is still open, and deliver heartland-demo/Reports.html and heartland-demo/Setup.html.
```

### 60-Second Upload — Claude Desktop (Cowork, Recommended for Non-Technical Evaluators)

1. Download **`rare-kaon-ops-demo-v0.1.1.zip`** from [Releases](https://github.com/KnowHow-Software-Inc/rare-kaon-ops-demo/releases/latest).
2. Open **Claude Desktop** → **Customize** → **Plugins** → **Upload plugin**, and select the ZIP (or drag-and-drop the ZIP into a Cowork chat).
3. Open a new **Cowork** session (Python 3 is preinstalled inside Cowork; selecting a local folder is optional—pick one if you want to inspect the generated CSV, XLSX, YAML, PDF, and HTML files directly).
4. Paste Prompt 1:

```text
Are we ready to run this week's finance pack? Show me what systems feed this and what is still open.
```

On the first prompt, the engine automatically copies the read-only bundled `seed/` into a marked `heartland-demo/` workspace, delivers `Reports.html` and `Setup.html`, and suggests the next four prompts. See [quickstart/5-prompts-in-5-minutes.md](quickstart/5-prompts-in-5-minutes.md) for the full 1-page walkthrough card.

### Local Source Checkout — Developers & Technical Evaluators

```bash
git clone https://github.com/KnowHow-Software-Inc/rare-kaon-ops-demo.git
cd rare-kaon-ops-demo
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -v
```

Launch `claude` or `codex` in the repository root and paste Prompt 1. Both harnesses discover `skills/ops-copilot/SKILL.md` and materialize the working copy in gitignored `./heartland-demo/`.

## What the Five Flows Demonstrate

| Flow | Prompt focus | Engine behavior | Governance & engineering proof |
| --- | --- | --- | --- |
| **1. Readiness & System Map** | Weekly readiness, source feeds, open items | Reads `config/feeds.yaml` and local exports; renders `Setup.html` and `Reports.html`. | Works directly on messy exports from legacy systems with human-readable YAML config and cutover rules. |
| **2. Source-Traced Interrogation** | 13-week cash bridge and overdue A/R (`>60` days) | Queries `variance_bridge` and A/R aging index from `report-model.json`. | Every figure traces to source filenames, as-of dates, and invoice numbers; non-blocking open reviews stay separate from blockers. |
| **3. Stateless What-If & Revision** | Defer `$25,000` Lone Star payment (Week 1 → Week 2), then apply amendment | Clones SQLite into `:memory:` for zero-write preview; commits via `ingest` + `change confirm` + `prepare`. | Exploration writes zero bytes; committed revisions log to `config-audit.jsonl`, preserve Draft 1, and move cash once. |
| **4. Payroll Hard Stops & Privacy** | Prepare payroll journal for ERP posting and draft board email | Reconciles voucher to funding wire (`$0.00` variance), then blocks on unmapped branch code `TUL-INSTALL-NEW` (`PAY-MAP-001`) and unset `mapping_approval_ref`. | Code enforces hard stops and privacy walls (`attachable: false` on restricted payroll registers) independent of LLM wording. |
| **5. Transfer & Portability** | Adapt to non-finance workflows and Microsoft 365 Copilot | Inspects live `config/*.yaml` and bundled `docs/*.md`. | Shows what stays invariant across portfolio companies, how M365 Copilot wraps the same engine, and how a bounded pilot is measured. |

## Architecture Overview

```text
Agent Harness (Claude Desktop/Cowork, Claude Code, Codex, or M365 Copilot via MCP)
        │
        ▼
skills/ops-copilot/SKILL.md            (thin operating spec; zero math or file parsing)
        │
        ▼
skills/ops-copilot/scripts/run_cli.py  (12 structured JSON commands + uniform error envelopes)
        │
        ├── config/*.yaml              (declarative feeds, column aliases, assumptions, outputs)
        ├── heartland-demo/            (SQLite state, immutable draft folders, audit log)
        └── report-model.json          (single canonical typed ReportModel per run)
                │
                ├── engine/render/pdf.py   → Weekly Liquidity Update & 13-Week Cash Flow PDFs
                ├── engine/render/html.py  → Reports.html & Setup.html (zero external CDN deps)
                └── engine/render/xlsx.py  → Payroll Pre-Posting Review Workbook
```

### Key Architectural Invariants

- **Single canonical model:** Every run writes one `report-model.json` (`engine/model.py`). PDFs, HTML dashboards, Excel workbooks, and chat queries read that single file without recomputing totals.
- **Exact arithmetic and pure-Python portability:** Monetary math uses integer cents or `Decimal`. Runtime dependencies are strictly pure Python (`PyYAML`, `openpyxl`, `et_xmlfile`, `reportlab`, `charset-normalizer`)—no C extensions, NumPy, Pandas, or Pillow—so one vendored ZIP runs on macOS and Windows.
- **Controls in executable code:** `SourceRef.__post_init__` forces `attachable = False` on every `restricted` file; `CheckItem.__post_init__` separates `blocking` checks from `open_review` items; `ReportModel.can_approve` requires zero blockers and zero unconfirmed `PendingChange` items; `what-if` executes on an in-memory SQLite backup.
- **Read-only seed and safe resets:** Runtime commands never mutate `seed/`. They work exclusively inside `heartland-demo/` stamped with `.rare-kaon-demo-workspace.json`, and `reset` refuses any folder missing that marker.

### Repository Layout

```text
rare-kaon-ops-demo/
├── .claude-plugin/plugin.json         # Claude Desktop / Cowork / Code manifest
├── skills/ops-copilot/
│   ├── SKILL.md                       # Consolidated operating specification
│   └── scripts/run_cli.py             # Standalone CLI entry point
├── docs/                              # Bundled architecture, transfer, security & pilot guides
├── engine/                            # Pure-Python deterministic engine & renderers
│   ├── model.py                       # Frozen canonical ReportModel dataclasses
│   ├── contracts.py                   # Frozen CLI commands, topics, paths, KPI keys
│   ├── cli.py                         # Structured JSON CLI
│   └── render/                        # pdf.py, html.py, xlsx.py
├── seed/                              # Read-only synthetic Heartland Mechanical dataset
├── quickstart/
│   └── 5-prompts-in-5-minutes.md      # 1-page buyer walkthrough card
├── packaging/plugin-requirements.txt  # Pinned pure-Python runtime packages
├── scripts/                           # Deterministic data generator & plugin ZIP builder
└── tests/                             # Contract, math-invariant, privacy & packaging tests
```

## CLI Commands & Verification

All twelve CLI commands return uniform JSON envelopes (`ok_envelope` / `error_envelope`):

`init` · `reset` · `overview` · `readiness` · `explain` (`cash_bridge`, `ar_over_60`, `payroll`, `guardrails`, `architecture`, `transfer`) · `what-if` · `ingest` · `change` · `prepare` · `approve` · `post` · `config`

Run the automated test suite:

```bash
.venv/bin/python -m pytest -v
```

Build the standalone pure-Python plugin ZIP:

```bash
.venv/bin/python scripts/build_plugin.py
```

## Documentation

- **[5 Prompts in 5 Minutes](quickstart/5-prompts-in-5-minutes.md)** — 1-page install and buyer walkthrough card.
- **[Architecture Explainer](docs/architecture-explainer.md)** — How declarative YAML, a canonical `ReportModel`, deterministic Python, and a thin `SKILL.md` work together.
- **[Cross-Category Patterns](docs/cross-category-patterns.md)** — Applying the same architecture to customer retention, field capacity, credentialing, sponsor roll-ups, and commercial revenue leakage.
- **[Harness Portability & Security](docs/harness-and-security.md)** — Honest comparison of Claude Desktop/Cowork, Claude Code, OpenAI Codex, and Microsoft 365 Copilot / Copilot Studio, plus data residency and scaling path.
- **[Pilot Scoping Guide](docs/pilot-scoping-guide.md)** — How Rare Kaon bounds a first pilot and measures outcomes against the incumbent process.

## License & Clean-Room Guarantee

Released under the [MIT License](LICENSE). All data for **Heartland Mechanical Services, LLC** is 100% synthetic, deterministically generated, and verified by automated leakage checks.
