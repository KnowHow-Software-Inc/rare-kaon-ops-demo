# Harness Portability, Security, and Scaling

Buyers evaluating operational AI tools face two recurring concerns: vendor lock-in to a single model provider and enterprise security compliance across Microsoft-centric or regulated portfolio companies.

Because all parsing, exact arithmetic, state management, privacy enforcement, and document rendering live inside the standalone `engine/` Python package and readable `config/*.yaml` files, the agent harness acts as a replaceable natural-language shell. Switching from Claude Desktop to OpenAI Codex—or deploying behind Microsoft 365 Copilot—changes only the thin invocation layer, never the accounting or control logic.

## Agent Harness Comparison

```text
                ┌──────────────────────────────────────┐
                │         User's Agent Harness         │
                │      (Natural-language surface)      │
                └──────────────────┬───────────────────┘
         ┌─────────────────────────┼─────────────────────────┐
         ▼                         ▼                         ▼
Claude Desktop (Cowork)    Claude Code / OpenAI Codex   M365 Copilot Studio
Bundled plugin ZIP         Local repo + SKILL.md        Thin Azure MCP / HTTP
(Implemented today)        (Implemented today)          (Phase 2B wrapper)
         │                         │                         │
         └─────────────────────────┼─────────────────────────┘
                                   ▼
                 ┌────────────────────────────────────┐
                 │  Portable Deterministic Engine     │
                 │  engine/ + config/*.yaml + seed/   │
                 │  → report-model.json               │
                 │  → PDF / HTML / XLSX outputs       │
                 └────────────────────────────────────┘
```

| Harness | Current status | How the engine executes | Strengths | Practical tradeoffs |
| --- | --- | --- | --- | --- |
| **Claude Desktop — Cowork** *(recommended executive default)* | **Implemented today** (single vendored ZIP) | Runs bundled pure-Python engine inside Cowork's sandboxed Linux VM (Python 3 preinstalled). | Zero host Python setup; works with or without a selected local folder; delivers `Reports.html`, `Setup.html`, and PDFs inline. | Per-user desktop session; not a shared multi-user web portal. |
| **Claude Desktop — Code tab & Claude Code** | **Implemented today** (same ZIP or git repo) | Runs directly against a local folder via host `python3` (3.11+). | Full filesystem visibility into `heartland-demo/`, YAML config, SQLite state, and `pytest` suite. | Requires Python 3.11+ on the host machine (Windows machines without Python should use Cowork or install Python 3.11+). |
| **OpenAI Codex / CLI (Agent Skills)** | **Supported by repo layout** (zero changes to `engine/`) | Discovers the standard `skills/ops-copilot/SKILL.md` and executes `run_cli.py` locally with Python 3.11+. | Identical deterministic CLI, YAML config, and outputs on OpenAI models; avoids Anthropic lock-in. | Requires local CLI/desktop execution; standard ChatGPT web chat cannot invoke local host binaries directly. |
| **Microsoft 365 Copilot / Copilot Studio** | **Architectural extension** *(not bundled in the v0.1.0 local ZIP)* | Wraps the exact same `engine/` CLI handlers in a thin MCP server or OpenAPI HTTP adapter (~150 LOC) hosted in **Azure Container Apps** or **Azure Functions**. | Runs inside the company's existing Microsoft 365 and Azure tenant with Entra ID SSO and Purview governance; zero rewrite of engine logic. | M365 Copilot cannot run local Python binaries in chat; requires IT deployment of the container and Copilot Studio connector (days, not months). |

### How Microsoft 365 Copilot Connects Without Rewriting Logic

Microsoft 365 Copilot does not execute local Python scripts inside a user's chat session. For portfolio companies standardized exclusively on Microsoft 365, Rare Kaon packages the identical `engine/` package behind a thin Streamable HTTP MCP or OpenAPI adapter:

1. **Containerize `engine/` + `config/*.yaml`:** The adapter exposes the same operations (`overview`, `readiness`, `explain`, `what-if`, `ingest`, `change`, `prepare`, `approve`, `post`) as typed MCP tools or OpenAPI endpoints.
2. **Host inside the client's Azure tenant:** Deploy to Azure Container Apps or Azure Functions with Entra ID authentication, private VNet access to SharePoint/Blob storage, and existing enterprise logging.
3. **Bind Copilot Studio:** Register a Copilot Studio declarative agent pointing at the internal endpoint using the same operator instructions from `SKILL.md`.

Every calculation, `ReportModel` field, `SourceRef` privacy check, and `CheckItem` hard stop executes the exact same Python code verified by the `pytest` suite.

## Data Residency, Network Posture, and Privacy

### Zero Runtime Network Dependencies

The demo runtime uses only Python 3.11+ standard library modules and five pinned pure-Python packages (`PyYAML`, `openpyxl`, `et_xmlfile`, `reportlab`, `charset-normalizer`) vendored inside the plugin ZIP. The engine makes zero outbound network calls during execution—no telemetry, no third-party SaaS APIs, and no external JavaScript or CDN references in generated HTML dashboards.

### Where Files Live

In this demo, bundled `seed/` files remain read-only inside the plugin package and copy into a single marked working directory (`heartland-demo/`, containing `.rare-kaon-demo-workspace.json`) either inside the user's selected folder or inside a local fallback workspace (`~/.rare-kaon-ops-demo/heartland-demo/` or the Cowork sandbox).

In single-writer production pilots, the workspace folder sits on a managed local workstation or inside a synced **SharePoint / OneDrive** document library. Raw exports, YAML configs, SQLite run state, and generated PDF/HTML/XLSX packs remain inside the company's existing Microsoft 365 or file-storage boundary without provisioning a third-party cloud database.

### What the Model Sees vs. What Code Blocks

Be clear about the boundary between local deterministic execution and the language-model context window:

- **Parsed locally by Python:** Raw CSV and XLSX files (bank activity, A/R agings, payroll vouchers) are read and calculated locally by `engine/`. The model never scrapes raw spreadsheets directly.
- **Returned to the model in structured JSON:** Aggregated KPIs, variance bridge drivers, check summaries, overdue invoice references, and `attachable_files` paths return from `run_cli.py` so the model can summarize findings in executive English.
- **Programmatically blocked from distribution:** `SourceRef.__post_init__` forces `attachable = False` on every source tagged `sensitivity: restricted` (such as `sources/payroll-voucher-2026-11-15.csv`). `ReportModel.attachable_sources()` strips restricted files before constructing `attachable_files`, preventing row-level employee compensation from entering board packages or drafted emails.
- **Enterprise retention posture:** Production deployments pair this architecture with enterprise zero-data-retention (ZDR) agreements on Claude Enterprise, Azure OpenAI / Microsoft 365 Copilot, or OpenAI Enterprise so structured summaries returned to the model are never retained for model training.

### Execution Containment and Audit Trail

- **No silent mutations:** `what-if` runs against an in-memory SQLite copy (`conn.backup()`) and writes zero bytes to disk.
- **Draft preservation:** `prepare` writes numbered run folders (`draft-001`, `draft-002`) without overwriting prior drafts; `approve` mints immutable numbered editions only when `blocking_checks` and open `pending_changes` are both empty.
- **Write and send gates:** `post --target production` refuses execution if any blocking check is open or `mapping_approval_ref` is unset. `config/outputs.yaml` locks `send_mode: draft_only` so emails are drafted for human review rather than sent automatically.
- **Append-only configuration log:** Every `config set` and operator change confirmation appends a timestamped record (actor, field, prior value, new value, rationale) to `config/config-audit.jsonl`.

## Scale Path: Single-Writer to Multi-Writer Enterprise

Starting with local exports and SQLite removes infrastructure delays during a first pilot without creating a dead end when usage expands:

| Stage | Target operating profile | Storage & execution | Identity & integration | What changes in `engine/` |
| --- | --- | --- | --- | --- |
| **Stage 1 — Single-writer local / SharePoint-synced** *(this demo & first pilots)* | One accountable process owner (Controller, VP Finance, Ops Director) running weekly or biweekly cycles. | Local folder or SharePoint-synced library; embedded SQLite; Cowork, Claude Code, or Codex. | Operator name recorded in `approve` and `config-audit.jsonl`; manual export drops into `sources/`. | None — baseline architecture. |
| **Stage 2 — Shared team / M365 Copilot service** | Multiple reviewers, branch managers, or Microsoft-only environments. | Same Python `engine/` in Azure Container Apps / AWS ECS; swap SQLite state store for managed Postgres. | Entra ID / SSO identity bound to `approved_by` and audit entries; automated SharePoint/SFTP intake. | Replace SQLite adapter in `workspace.py` and add thin MCP/HTTP wrapper; keep `model.py`, checks, and renderers unchanged. |
| **Stage 3 — API-connected ERP & operational feeds** | High-frequency or multi-entity roll-ups eliminating manual export downloads. | Managed container service + scheduled ingestion triggers. | Read-only ERP/bank/CRM API pulls (NetSuite, Intacct, QuickBooks, PEO APIs) and gated write-back endpoints behind approval tokens. | Swap file parsers in `ingest.py` for API connectors; `ReportModel`, `config/*.yaml`, checks, and outputs stay identical. |
