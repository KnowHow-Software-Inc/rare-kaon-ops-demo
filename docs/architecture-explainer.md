# Architecture Explainer

Recurring operational workflows fail in generic AI chat windows because language models approximate arithmetic, lose track of column mappings across messy exports, and treat governance instructions as soft suggestions. Custom web applications solve those failures at the cost of months of frontend, database, and integration engineering for every portfolio company.

This plugin demonstrates a third architecture: a **deterministic Python engine** governed by **declarative YAML configuration**, writing one **canonical `ReportModel`**, operated through a **thin operating specification (`SKILL.md`)** inside the user's existing agent harness. Rare Kaon publishes this architecture openly in working code rather than hiding it behind slides—if this is what we give away as an open demonstration, evaluate what we build under contract against your real operating systems.

## Four Layers and Their Responsibilities

```text
┌──────────────────────────────────────────────────────────────────────┐
│ 1. Agent Harness + Thin Operating Spec (skills/ops-copilot/SKILL.md) │
│    Translates natural-language questions into CLI calls and plain    │
│    executive summaries. Performs zero arithmetic or file parsing.    │
└──────────────────────────────────┬───────────────────────────────────┘
                                   │ JSON stdin/stdout
┌──────────────────────────────────▼───────────────────────────────────┐
│ 2. Deterministic Engine & CLI (engine/ via run_cli.py)               │
│    12 structured commands returning uniform JSON envelopes.          │
│    Exact integer-cent / Decimal arithmetic and SQLite run state.     │
└──────────────┬───────────────────────────────────────┬───────────────┘
               │ reads & validates                     │ writes one file
┌──────────────▼──────────────────┐     ┌──────────────▼───────────────┐
│ 3. Declarative YAML Config      │     │ 4. Canonical ReportModel     │
│    config/feeds.yaml            │     │    report-model.json         │
│    config/assumptions.yaml      │     │       ├── pdf.py   (PDFs)    │
│    config/outputs.yaml          │     │       ├── html.py  (HTML)    │
│    config/payroll-mappings.yaml │     │       ├── xlsx.py  (Excel)   │
│    config/config-audit.jsonl    │     │       └── explain  (JSON)    │
└─────────────────────────────────┘     └──────────────────────────────┘
```

### 1. Declarative Configuration (`config/*.yaml`)

Company-specific rules live in four readable YAML files inside the workspace rather than inside prompt text or database tables:

- **`config/feeds.yaml`** catalogs every recurring source across Weekly Cash and Payroll, including feed owner, portal or email location, due cadence, `sensitivity` (`board`, `internal`, or `restricted`), `column_aliases` that map messy bank or ERP export headers onto canonical fields, and cutover dates (`active_from`, `active_until`, `replaced_by_id`). In the Heartland Mechanical dataset, `tulsa_qb_ar` transitions automatically to `unified_erp_ar` on December 1, 2026 without code changes.
- **`config/assumptions.yaml`** stores explicit calculation and control parameters: minimum cash floor, recurring weekly baselines, payroll tie-out tolerance in cents, ERP posting mode (`sandbox` or `production`), and `mapping_approval_ref` (which defaults to `null` so production ERP posting stays locked until written approval is recorded).
- **`config/outputs.yaml`** defines generated artifacts, the named approver, distribution recipients, and the `send_mode: draft_only` boundary.
- **`config/payroll-mappings.yaml`** maps branch and department codes to ERP general-ledger accounts and departments.

When an operator updates a setting through `config set`, the engine validates the change against the schema before writing the YAML file and appends one structured JSON record (timestamp, actor, field path, old value, new value, and reason) to `config/config-audit.jsonl`.

### 2. Deterministic Engine and Structured CLI (`engine/`)

All parsing, reconciliation, forecasting, and control evaluation execute in pure Python (`engine/`), invoked through `skills/ops-copilot/scripts/run_cli.py`. Monetary calculations use integer cents or `Decimal` rather than binary floating point, preventing penny rounding drift across bank tie-outs and journal lines.

The engine exposes twelve commands—`init`, `reset`, `overview`, `readiness`, `explain`, `what-if`, `ingest`, `change`, `prepare`, `approve`, `post`, and `config`—each returning a uniform JSON envelope (`ok_envelope` or `error_envelope` in `engine/contracts.py`). The `explain` command serves six grounded evidence topics (`cash_bridge`, `ar_over_60`, `payroll`, `guardrails`, `architecture`, and `transfer`) directly from saved run models and configuration rather than asking the language model to re-read source spreadsheets.

Runtime dependencies are restricted to Python 3.11+ standard library modules and pure-Python packages pinned in `packaging/plugin-requirements.txt` (`PyYAML`, `openpyxl`, `et_xmlfile`, `reportlab`, `charset-normalizer`). Because no compiled C extensions are required, one vendored plugin ZIP executes on macOS and Windows without virtual environments or external network calls.

### 3. Single Canonical `ReportModel` (`engine/model.py`)

Every `prepare` run writes one serialized `ReportModel` to `report-model.json`. Its frozen dataclasses hold every fact needed across all output channels:

- `ReportMeta`: company identity, module (`weekly` or `payroll`), period, draft number, approval status, approver, edition number, and schema version.
- `KpiField`: typed KPI key, label, value, unit (`USD`, `pct`, `count`, `text`), status (`actual`, `forecast`, `provisional`), and comparison baseline.
- `VarianceBridge` and `BridgeDriver`: reconciliation from the prior issued pack to the current draft, matching shifted vendor payments by invoice reference rather than vendor-name string matching.
- `CheckItem`: deterministic control finding with explicit `severity` (`blocking`, `open_review`, or `note`), owner, dollar amount, and remediation step.
- `PendingChange`: operator decision awaiting explicit confirmation or decline before approval can proceed.
- `SourceRef`: retained input citation carrying `feed_id`, `file_name`, `as_of`, `sensitivity`, `attachable`, and row references.

Every renderer reads `report-model.json` without recomputing business totals:

- **`engine/render/pdf.py`** produces the Weekly Liquidity Update PDF and the landscape 13-Week Cash Flow PDF via ReportLab.
- **`engine/render/html.py`** renders self-contained `Reports.html` and `Setup.html` dashboards with zero external JavaScript or CDN dependencies.
- **`engine/render/xlsx.py`** produces `payroll-review.xlsx` (`Runs`, `Tie-Out`, `Checks`, `Exceptions`, and `Draft Journal`—watermarked when blocking checks remain open).
- **`explain` CLI queries** serve the exact same KPI values, variance drivers, and citations into the chat window.

Because no downstream surface recalculates totals, the numbers in chat, the HTML hub, the board PDFs, and the Excel workpaper match to the cent.

### 4. Thin Operating Specification (`skills/ops-copilot/SKILL.md`)

`SKILL.md` is a compact operating specification (under 80 lines) rather than a multi-page prompt library. It directs the model to call `run_cli.py`, prohibits ad-hoc Python snippets or direct spreadsheet scraping, routes operator questions to the exact CLI command or `explain` topic, and keeps operator responses in plain finance and operations English.

## Controls Enforced in Executable Code

Governance does not depend on whether the language model remembers a polite prompt reminder. Five controls execute inside `engine/model.py`, `engine/workspace.py`, and CLI handlers before the model receives a response:

1. **Programmatic privacy wall:** `SourceRef.__post_init__` sets `attachable = False` unconditionally whenever `sensitivity == "restricted"`. `ReportModel.attachable_sources()` filters out restricted inputs before returning `attachable_files`, so raw employee compensation files (`sources/payroll-voucher-2026-11-15.csv`) can be cited by filename and date without ever being attached to a board email.
2. **Blocking versus non-blocking checks:** `CheckItem.__post_init__` sets `blocks_approval = (severity == "blocking")`. Non-blocking `open_review` findings (such as the unconfirmed `$14,200` fleet fuel estimate in Weekly Cash) surface in every output without halting pack issuance. Blocking findings (such as unmapped technician department `TUL-INSTALL-NEW` in `PAY-MAP-001`) set `ReportModel.can_approve` to `False` even when the payroll journal ties out to `$0.00` variance.
3. **Stateless in-memory `what-if` simulation:** The `what-if` command copies the workspace SQLite database into `:memory:` via `conn.backup()`, recalculates the 13-week forecast with the proposed override, and returns baseline versus scenario deltas without writing a byte to disk.
4. **Immutable draft history and write gates:** Running `prepare` writes each draft to a new numbered run folder (`draft-001`, `draft-002`) so prior runs remain inspectable. `approve` refuses execution while any `blocking` check or unconfirmed `PendingChange` remains open. `post --target production` refuses execution unless blocking checks are clear and `mapping_approval_ref` is populated. `config/outputs.yaml` locks `send_mode: draft_only`.
5. **Read-only `seed/` and marked workspace resets:** Runtime commands never write to `seed/`. On first use, the workspace resolver copies `seed/` into `heartland-demo/` and stamps `.rare-kaon-demo-workspace.json`. The `reset` command refuses to modify any directory missing that marker file.

## Comparison With Prompts and Custom Web Apps

| Dimension | Generic chat prompts | Custom web application | Declarative CLI + Agent Harness |
| --- | --- | --- | --- |
| **Calculation accuracy** | Model estimates sums and rollforwards in prose or ad-hoc scripts. | Deterministic backend code. | Deterministic pure-Python engine using exact integer-cent math. |
| **Cross-report consistency** | Chat answers and exported slides drift whenever numbers are restated. | Shared backend API, high frontend maintenance. | Single `report-model.json` feeds PDFs, HTML, XLSX, and chat answers. |
| **Governance & privacy** | Enforced only by prompt wording. | Enforced in application code. | Enforced in typed dataclasses (`SourceRef`, `CheckItem`) and CLI handlers. |
| **Onboarding a new company** | Copy and manually edit fragile prompt documents. | Months of schema, UI, and ETL work. | Edit readable YAML config (`feeds.yaml`, `assumptions.yaml`, mappings) and add export parsers. |
| **UI and model lock-in** | Tied to one chat product; outputs are unstructured prose. | High ongoing UI engineering cost. | Native PDF/HTML/XLSX outputs; thin `SKILL.md` or MCP wrapper swaps across harnesses. |

## What Stays Invariant vs. What Changes Per Company

When Rare Kaon deploys this pattern for a portfolio company or a second operating entity, the core architecture remains unchanged while business rules stay isolated in configuration:

- **Stays invariant:** `ReportModel` schema, exact integer-cent math helpers, `SourceRef` privacy enforcement, `CheckItem` blocking semantics, immutable draft and edition lifecycle, stateless in-memory `what-if`, `config-audit.jsonl` logging, CLI JSON envelope contract, HTML/PDF/XLSX rendering pipeline, and the automated contract test structure.
- **Configured per company:** `config/feeds.yaml` (actual bank, ERP, A/R, AP, or operational export schemas and column aliases), `config/assumptions.yaml` (thresholds, tolerances, recurring rules, write gates), `config/outputs.yaml` (approvers, recipients, artifact templates), entity-specific mapping tables (such as `config/payroll-mappings.yaml`), and any source-specific parser adapters for non-standard exports.
