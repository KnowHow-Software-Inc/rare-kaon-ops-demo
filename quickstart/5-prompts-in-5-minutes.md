# 5 Prompts in 5 Minutes — Buyer Quickstart

Evaluate Rare Kaon's **Portfolio Finance & Ops Co-Pilot** against a bundled synthetic company: **Heartland Mechanical Services, LLC** (a PE-backed commercial HVAC platform, ~$42M revenue, three branches: Dallas `DAL-01`, Austin `AUS-02`, and newly acquired Tulsa `TUL-03`).

No setup script or separate data download is needed. On your first prompt, the plugin automatically copies the read-only synthetic dataset into a local `heartland-demo/` workspace.

---

## Fastest Install — One-Paste Agent Prompt (Claude Code, Claude Desktop Code Tab, Codex)

Paste this into any agent with shell access (zero `pip install` or manual unzipping):

```text
Install and run the Rare Kaon Ops Demo (v0.1.0): download and extract https://github.com/KnowHow-Software-Inc/rare-kaon-ops-demo/releases/download/v0.1.0/rare-kaon-ops-demo-v0.1.0.zip into ./rare-kaon-ops-demo-plugin (all pure-Python dependencies are already bundled in vendor/), read ./rare-kaon-ops-demo-plugin/skills/ops-copilot/SKILL.md, and immediately run Flow 1: check if Heartland Mechanical is ready to run this week's finance pack, show what systems feed it and what is still open, and deliver Reports.html and Setup.html.
```

## 60-Second Upload (Claude Desktop — Cowork, Recommended for Non-Technical Evaluators)

1. Download **`rare-kaon-ops-demo-v0.1.0.zip`** from https://github.com/KnowHow-Software-Inc/rare-kaon-ops-demo/releases/latest.
2. Open **Claude Desktop** → **Customize** → **Plugins** → **Upload plugin**, and select the ZIP (or drag-and-drop it into a Cowork chat).
3. Open a new **Cowork** session (Python 3 is preinstalled inside Cowork). Selecting a local folder is optional—choose a folder if you want to inspect the raw CSV, XLSX, YAML, PDF, and HTML files directly in Finder or Explorer.
4. Paste **Prompt 1** below.

---

## Prompt 1 — Weekly Readiness & System Map *(Copy-Ready Outreach Starter)*

```text
Are we ready to run this week's finance pack? Show me what systems feed this and what is still open.
```

**What to look for:** `Reports.html` and `Setup.html` delivered at the top of the reply, a single readiness table across Weekly Cash, Payroll, and Configuration showing feed owners and the scheduled December 1 Tulsa ERP cutover, and a short "What to try next" list.

---

## Prompt 2 — Source-Traced Cash Bridge & Overdue A/R

```text
Why did our 13-week ending cash move from last week's pack, and which A/R balances over 60 days are driving liquidity risk?
```

**What to look for:** Period-over-period cash variance drivers traced to exact filenames, dates, and invoice numbers (`INV-2026-8814`), overdue A/R broken into `61–90` and `>90` day buckets with the mid-week as-of date caveat (`2026-11-18`), and non-blocking open review items separated from approval blockers.

---

## Prompt 3 — Stateless What-If & Controlled Forecast Revision

**Part A — Preview (zero disk writes):**
```text
Don't save anything yet—show me the cash impact if we defer the $25,000 Lone Star HVAC Supply payment from Week 1 to Week 2.
```

**Part B — Commit & regenerate:**
```text
Confirmed—apply the supplier payment amendment in the incoming folder and regenerate the weekly draft.
```

**What to look for:** Part A computes the exact shift (`+$25,000` in Week 1, `-$25,000` in Week 2, `$0` net change to Week 2 ending cash) inside an in-memory copy without altering any file on disk. Part B ingests the staged amendment, logs the confirmation to `config/config-audit.jsonl`, leaves Draft 1 intact, and issues Draft 2 PDFs and refreshed `Reports.html` with invoice `LSS-2026-4189` moved once.

---

## Prompt 4 — Hard Stops, Privacy Wall & Write Governance

```text
Prepare this period's payroll journal for ERP posting, and draft the board email with all supporting files attached.
```

**What to look for:** Payroll reconciles to **$0.00 variance** across every wage, tax, and benefit row—and still **blocks** approval and ERP posting (`PAY-MAP-001`) because bolt-on branch technician Marcus Delgado (`EMP-1048`, `TUL-INSTALL-NEW`) has no approved ERP department mapping and `mapping_approval_ref` is unset. The restricted payroll register (`payroll-voucher-2026-11-15.csv`) is cited by filename and date only and excluded from attachments. The board email is drafted in text, never sent.

---

## Prompt 5 — Portfolio Transfer, Copilot Portability & Pilot Scoping

```text
How would this adapt to our dental services, PEO, or firm back-office workflows—and what if a portfolio company only uses Microsoft Copilot?
```

**What to look for:** Direct inspection of the workspace's `config/*.yaml` files and bundled `docs/*.md`, a clear split between invariant engine code and company-specific YAML, an honest explanation of wrapping the same engine in a thin Azure MCP/HTTP adapter for Microsoft 365 Copilot, and an optional 1-page pilot scope using Rare Kaon's measurement rubric.

---

## 1-Line Reset

```text
Reset the Heartland demo workspace to its initial state.
```

Restores `heartland-demo/` to a byte-identical starting state in under a second so you can rerun any flow cleanly.
