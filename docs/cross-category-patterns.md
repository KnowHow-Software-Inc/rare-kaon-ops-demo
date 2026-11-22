# Cross-Category Patterns

Heartland Mechanical Services demonstrates weekly cash forecasting and biweekly payroll controls, yet nothing in the underlying architecture is specific to finance. The engine implements a general six-stage operational loop:

1. **Ingest messy recurring exports** from legacy systems using alias-aware parsers (`config/feeds.yaml`).
2. **Apply declarative business rules and thresholds** stored in readable, audit-logged YAML (`config/*.yaml`, `config/config-audit.jsonl`).
3. **Materialize one canonical typed model** (`report-model.json`) holding KPIs, period-over-period variance bridges, retained source citations, and pending decisions.
4. **Run deterministic checks** that separate non-blocking `open_review` findings from `blocking` hard stops (`blocks_approval: true`) and enforce `restricted` file privacy (`attachable: false`).
5. **Gate revisions and external actions** behind stateless `what-if` previews, explicit human confirmation, and immutable numbered editions.
6. **Render native outputs** (board PDFs, self-contained HTML hubs, Excel review workbooks, and grounded chat answers) from the single model.

The table below maps that identical loop onto five non-finance workflows common across middle-market private equity portfolios and multi-site services businesses.

## Five Non-Finance Operational Categories

| Category | Messy recurring inputs | Declarative YAML config | Deterministic hard stops & privacy walls | Native outputs |
| --- | --- | --- | --- | --- |
| **1. Customer Experience & Retention** | CRM account rosters, ticketing exports, SLA logs, billing/usage CSVs | Segment weights, renewal windows, risk thresholds, header aliases | Block outreach drafts on accounts in legal dispute or missing CSM mapping; mark PII/payroll ticket transcripts `restricted` | Weekly retention triage PDF, account-owner action hub (`Reports.html`), CSM review workbook |
| **2. Field Capacity & Branch Margin** | Dispatch/FSM work-order exports, fleet fuel cards, parts invoices, timecards | Branch utilization targets, overtime rules, acquisition cutover dates, job-code aliases | Block branch margin release when a bolt-on branch introduces unmapped service codes or job-cost tie-out fails | Regional margin & utilization PDF, branch dispatch dashboard, branch-manager exception XLSX |
| **3. Staffing & Healthcare Credentialing** | State license board exports, background-check reports, immunization logs, HRIS rosters | State and facility credential matrices, renewal grace rules, `restricted` PHI/PII tags | Block shift clearance (`blocks_approval: true`) if any required license or clearance is expired or missing; keep PHI/background PDFs `attachable: false` | Shift clearance packet, compliance audit log, recruiter exception workbook |
| **4. PE Sponsor & Firm Back-Office Roll-Ups** | Heterogeneous portfolio company trial balances, KPI packs, diligence data-room files | Entity chart-of-accounts crosswalks, fiscal calendar alignment, EBITDA adjustment definitions | Refuse consolidation when any entity trial balance fails `$0.00` tie-out, intercompany eliminations break, or an unmapped GL account appears | Portfolio board/LP tie-out PDF, sponsor portfolio hub (`Reports.html`), diligence variance bridge XLSX |
| **5. Commercial & Contract Revenue Leakage** | PSA timekeeping logs, signed contract registers, ERP billing batches, change-order emails | Customer rate cards, annual escalation clauses, SLA credit caps, billing-code aliases | Halt billing batch release when billed rates deviate from contract terms beyond tolerance or scope changes lack written sign-off | Pre-billing leakage review XLSX, commercial renewal tracker (HTML), CFO exception summary PDF |

---

## Detailed Pattern Mappings

### 1. Customer Experience & Retention (PEOs, B2B Services, Managed Services)

Operations teams at PEOs and B2B service platforms often discover churn risk only after a client sends a cancellation notice because warning signals sit across three disconnected systems: support ticket volume, billing disputes, and renewal dates.

- **Configuration and intake:** `feeds.yaml` maps inconsistent client names across CRM, Zendesk/ServiceNow exports, and billing CSVs onto a canonical client ID. `assumptions.yaml` defines deterministic risk-scoring rules (for example, ticket spike >40% over 30-day baseline combined with renewal inside 90 days) rather than letting an LLM invent subjective scores each week.
- **Controls:** `CheckItem` flags accounts missing an assigned account manager or active legal-hold status as `blocking` so retention outreach drafts cannot be approved until leadership clears them. Ticket transcripts containing employee SSNs or HR complaints carry `sensitivity: restricted` (`attachable: false`), preventing raw transcripts from leaking into sponsor summaries while preserving counts and dates.
- **Decision workflow:** Account leaders run `what-if` previews on proposed renewal concessions or SLA credits before committing changes to the weekly retention pack.

### 2. Field Capacity, Scheduling & Branch Margin (HVAC, Roofing, Dental/Vet Platforms)

Multi-site home-services, commercial-trades, and dental/veterinary platforms run on disparate practice-management or field-service software after acquisitions. Regional managers need branch margin and technician utilization visibility before systems finish migrating.

- **Configuration and intake:** `feeds.yaml` declares each branch's active export system and cutover dates (`active_until` / `replaced_by_id`), mirroring Heartland Mechanical's Tulsa ERP cutover. Mapping YAML normalizes branch job codes, parts categories, and overtime classes.
- **Controls:** When a newly acquired site introduces an unmapped job or procedure code, a `blocking` check stops branch gross-margin publication so costs are not silently miscategorized. Estimated monthly fuel or lab-supply accruals surface as `open_review` items so regional directors see known estimates without delaying Monday morning dispatch reviews.
- **Decision workflow:** Branch leaders interrogate `explain` bridges showing exactly which work orders, parts invoices, or overtime shifts drove week-over-week margin movement.

### 3. Staffing & Healthcare Credentialing Compliance (Healthcare Staffing, Allied Health)

In healthcare staffing and clinical services, placing one clinician with an expired state license or missing facility immunization check creates regulatory and contract-breach liability. Even a 99% accurate prompt-based review is unacceptable.

- **Configuration and intake:** Declarative YAML encodes mandatory credential sets by state, specialty, and hospital network alongside renewal lead times and source feed locations.
- **Controls:** The deterministic engine evaluates every clinician against the active facility matrix. If 19 credentials pass and 1 required license expires before the shift end date, `CheckItem` fires with `severity: "blocking"` (`blocks_approval: true`), preventing the placement packet from reaching `approved` status. Underlying background checks, drug screens, and immunization records are tagged `sensitivity: restricted` (`attachable: false`) so client hospital packets include verification status and expiration dates without attaching restricted medical records.
- **Decision workflow:** Recruiters work a prioritized exception workbook (`Exceptions` tab in XLSX) and cannot bypass a blocking item without updating the underlying verified source record.

### 4. PE Sponsor & Firm Back-Office Roll-Ups (Private Equity Firms, Operating Platforms)

Private equity operating teams and portfolio CFOs spend days every month reconciling inconsistent Excel packs emailed by 8–15 portfolio companies before investment-committee or LP meetings.

- **Configuration and intake:** Each portfolio company keeps its existing ERP export format. One YAML mapping file per company translates local accounts into the sponsor's standard reporting hierarchy, records fiscal calendar offsets, and documents approved pro-forma adjustments in `config-audit.jsonl`.
- **Controls:** Every company ingestion enforces integer-cent trial-balance footing (`debits == credits` to `$0.00`), checks intercompany balances, and blocks consolidated pack approval if a subsidiary adds a new account code not present in the crosswalk. Prior quarter editions stay immutable so `VarianceBridge` explains exact dollar movements between editions.
- **Decision workflow:** Operating partners interrogate `explain` queries during board prep ("Which three portfolio companies drove the Q3 working-capital variance?") and trace every answer back to the submitted entity file and date.

### 5. Commercial & Contract Revenue Leakage (Professional Services, Commercial Maintenance)

Services businesses routinely lose 2%–5% of margin to unbilled change orders, outdated rate cards applied after renewal dates, and unapproved write-offs hidden across timekeeping exports and ERP billing queues.

- **Configuration and intake:** Declarative YAML holds contracted rate cards, annual CPI/fixed escalation effective dates, included-hours caps, and billing-code aliases across timekeeping and invoicing exports.
- **Controls:** Before invoices release, the engine compares every unbilled time or materials line against the active contract YAML. Rate mismatches outside tolerance or unapproved out-of-scope entries trigger `blocking` checks; pending client confirmations on minor expenses surface as `open_review`. A stateless `what-if` simulates margin impact of renewal rate escalations or discount proposals without touching live billing state.
- **Decision workflow:** The controller or billing director reviews the pre-billing exception workbook, confirms approved adjustments with an audit entry, and releases clean billing batches with zero spreadsheet re-keying.

## Criteria for a Strong Fit

A workflow fits this architecture well when four conditions hold:

1. **Recurring cadence:** The team runs the process weekly, biweekly, or monthly and feels the repetitive preparation and reconciliation drag.
2. **Messy multi-system exports:** Inputs already exist as CSV, XLSX, YAML, or structured exports from bank portals, ERPs, CRMs, or vertical software—making a multi-month API integration unnecessary to start.
3. **Explicit rules and controls:** Tie-outs, thresholds, mappings, or privacy boundaries can be stated explicitly and verified deterministically in code.
4. **Human sign-off required:** An accountable operator or executive reviews exceptions and approves the output before external distribution or system write-back.
