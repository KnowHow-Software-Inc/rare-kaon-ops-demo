# Pilot Scoping Guide

Operational AI pilots fail when they start with open-ended discovery across six departments, depend on a multi-month data-warehouse build, or declare success based on subjective demos rather than side-by-side operational evidence.

Rare Kaon scopes every initial engagement as a bounded, measurable pilot built directly on top of the exports a portfolio company already produces.

## Five Boundary Conditions for a First Pilot

1. **One portfolio company:** Prove the pattern inside a single operating entity before rolling out across a fund or multi-brand platform.
2. **One recurring operational workflow:** Pick a process that runs on a fixed weekly, biweekly, or monthly cadence—such as weekly 13-week cash forecasting, biweekly payroll tie-out and ERP journal prep, monthly A/R and retention triage, or staffing credentialing clearance.
3. **One accountable process owner:** Assign a single owner (Controller, VP Finance, or Operations Director) who runs the incumbent process today, validates tie-outs, and signs off on outputs.
4. **Representative real exports:** Start with two to three historical cycles of the actual messy CSV, XLSX, or structured exports the team already downloads from banks, ERPs, CRMs, or vertical software. No ERP migration, API project, or data-lake prerequisite is required.
5. **Parallel run against the incumbent process:** Run at least two live operating cycles side-by-side against the existing manual process and reconcile every figure before retiring legacy spreadsheets.

## Pilot Execution Phases

### Phase 1 — Discovery, Sample Exports, and Baseline Measurement
- Collect two to three prior completed cycles of raw input files, existing workbooks, and finished leadership packs.
- Document every existing reconciliation rule, mapping exception, sensitivity boundary, and approval step with the process owner.
- Record baseline metrics for the incumbent process using the rubric below so before-and-after comparisons use observed timings rather than estimates.

### Phase 2 — Declarative Build and Historical Replay
- Author the company's declarative YAML files (`config/feeds.yaml`, `config/assumptions.yaml`, `config/outputs.yaml`, and mapping tables) alongside the process owner.
- Implement deterministic export parsers, exact integer-cent calculations, check rules (`blocking` vs. `open_review`), privacy tags (`restricted` → `attachable: false`), and native renderers (PDF, HTML, XLSX) targeting the leadership team's review format.
- Replay historical cycles through the engine and verify cent-level tie-outs against previously issued packs via an automated `pytest` contract and math-invariant suite.

### Phase 3 — Live Parallel Run and Cutover Decision
- Execute at least two live cycles in parallel with the incumbent process inside the client's chosen harness (Claude Desktop Cowork, Claude Code, OpenAI Codex, or an Azure-hosted MCP wrapper for Microsoft 365 Copilot).
- Log every manual edit, caught exception, and elapsed-time measurement against the baseline rubric.
- Review results with the process owner and sponsor to decide whether to cut over to production use and replicate to workflow #2 or portfolio company #2.

## Measurement Rubric

Track all six metrics across the baseline cycles and every parallel-run cycle. Separating **external wait time** from internal processing time is essential: software cannot make a bank or branch upload a file faster, and lumping wait time into cycle time hides both automation gains and upstream data bottlenecks.

| Metric | Definition | How to capture | What success looks like |
| --- | --- | --- | --- |
| **1. Operator touch time** | Active hands-on minutes spent by analysts and the process owner downloading, cleaning, reconciling, and formatting the cycle. | Time-stamped activity log per cycle (baseline vs. pilot). | Material reduction in repetitive preparation and formatting minutes; remaining time focused on exception resolution. |
| **2. Elapsed time (complete inputs → reviewable pack)** | Wall-clock time from when the final required source file arrives to when a review-ready draft pack is delivered. | Timestamp of final source arrival vs. `ReportMeta.created_at` on the first reviewable draft. | Moves from hours or days of manual assembly to minutes after inputs land. |
| **3. Manual edits required** | Count of manual corrections made to spreadsheets, slides, or PDFs after generation before the approver signs off. | Tallied per cycle during parallel run. | Driven toward zero; any recurring edit becomes a rule in `config/*.yaml` or a parser rule. |
| **4. Exceptions caught before release** | Count and dollar/operational impact of tie-out discrepancies, unmapped codes, overdue concentrations, or missing approvals flagged deterministically before distribution or ERP posting. | Read directly from `blocking_checks` and `open_review_checks` in `report-model.json`. | Previously manual or missed exceptions caught consistently with exact source citations. |
| **5. Review and approval time** | Minutes spent by the approver (CFO, Controller, COO) verifying figures, tracing variances, and granting sign-off. | Approver log comparing baseline review vs. source-traced interrogation (`explain` + `Reports.html`). | Faster verification because variance bridges and overdue tables trace directly to filenames, dates, and invoice references. |
| **6. External wait time *(tracked separately)*** | Hours spent waiting for third-party portals, banks, branches, or vendors to deliver required exports. | Logged separately from processing time between cycle start and final source arrival. | Visibility into upstream bottlenecks (such as late branch exports) without distorting engine performance metrics. |

## Deliverables and Ownership

At the conclusion of a pilot, the client owns the complete operational artifact—with no proprietary black-box runtime or per-seat SaaS lock-in:

- Clean-room Python engine (`engine/`) and automated `pytest` guardrail suite.
- Human-readable YAML configuration (`config/*.yaml`) and append-only audit log (`config/config-audit.jsonl`).
- Native PDF, HTML, and Excel renderers matching leadership review standards.
- Thin operating specification (`SKILL.md`) packaged for Claude Desktop/Cowork, Claude Code, or OpenAI Codex—or wrapped in an Azure Container Apps / Functions MCP adapter for Microsoft 365 Copilot.
- Parallel-run reconciliation scorecard comparing baseline vs. pilot metrics across all six rubric dimensions.

## Scaling From Pilot #1 to a Portfolio Playbook

Once the first workflow passes parallel run:

- **Second workflow in the same company** reuses the workspace structure, `ReportModel` contract, approval and privacy gates, audit logging, and harness packaging—adding only the new module's YAML config, parser, checks, and output template.
- **Same workflow in a second portfolio company** reuses the entire deterministic engine and test framework—swapping only `config/*.yaml`, export column aliases, and entity mapping tables.

To generate a tailored 1-page pilot scope for a specific portfolio company right now, ask the co-pilot in this session to apply this rubric to your target workflow, systems, and process owner.
