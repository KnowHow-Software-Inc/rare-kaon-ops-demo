"""Rare Kaon branded 2-page Session Brief renderers (HTML + pure-Python ReportLab PDF).

Produces:
- `<workspace_dir>/Session-Brief.html`: self-contained Arial-on-white executive brief
  with official Rare Kaon geometric mark, balanced green/red/teal semantic accents,
  and exact 2-page US Letter print geometry (or 3 pages when `include_log=True`).
- `<workspace_dir>/Session-Brief.pdf`: matching pure-Python ReportLab vector/text PDF
  requiring zero Pillow, zero browser binaries, and zero external network calls.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import html
import importlib.util
from pathlib import Path
import sys
import types
from typing import Any
from xml.sax.saxutils import escape as xml_escape


def _ensure_reportlab_pillow_free() -> None:
    """Allow pure-vector/text ReportLab imports when Pillow is not installed."""
    if "PIL" not in sys.modules and importlib.util.find_spec("PIL") is None:
        pil_mod = types.ModuleType("PIL")
        pil_img = types.ModuleType("PIL.Image")
        setattr(pil_mod, "Image", pil_img)
        sys.modules["PIL"] = pil_mod
        sys.modules["PIL.Image"] = pil_img


_ensure_reportlab_pillow_free()

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import Paragraph, Table, TableStyle

pdfmetrics.registerFontFamily(
    "Helvetica",
    normal="Helvetica",
    bold="Helvetica-Bold",
    italic="Helvetica-Oblique",
    boldItalic="Helvetica-BoldOblique",
)

TWO_PLACES = Decimal("0.01")


# ---------------------------------------------------------------------------
# Shared formatting & text helpers (exact Decimal parity across HTML & PDF)
# ---------------------------------------------------------------------------


def _fmt_usd(value: float | int | str | Decimal | None, *, signed: bool = False) -> str:
    if value is None or isinstance(value, bool):
        return "—"
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    q = dec.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    if q < 0:
        return f"-${abs(q):,.2f}"
    if signed and q > 0:
        return f"+${q:,.2f}"
    return f"${q:,.2f}"


def _fmt_usd_whole(value: float | int | str | Decimal | None) -> str:
    if value is None or isinstance(value, bool):
        return "—"
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    q = dec.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"${int(q):,}"


def _esc(val: Any) -> str:
    return html.escape(str(val if val is not None else ""), quote=True)


def _xesc(val: Any) -> str:
    return xml_escape(str(val if val is not None else ""))


def _join_workflow_list(items: list[str]) -> str:
    cleaned = [x.strip() for x in items if x.strip()]
    if not cleaned:
        return "weekly cash, overdue A/R, branch margin, customer churn risk, credentialing compliance, or firm-level roll-ups"
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} or {cleaned[1]}"
    return ", ".join(cleaned[:-1]) + f", or {cleaned[-1]}"


def _harness_sentence_parts(target_harness: str) -> tuple[str, str, str]:
    """Return ordered harness emphasis for Point 04 (plain text, HTML, ReportLab XML)."""
    h = (target_harness or "").strip().lower()
    if "copilot" in h or "microsoft" in h or "m365" in h or "azure" in h:
        txt = (
            "The business owns the code and rules outright, with zero lock-in to a single model vendor: "
            "the same engine runs inside your Azure tenant behind Microsoft 365 Copilot, or locally in Claude or OpenAI Codex."
        )
        htm = (
            "The business owns the code and rules outright, with zero lock-in to a single model vendor: "
            "the same engine runs inside your Azure tenant behind <strong>Microsoft 365 Copilot</strong>, "
            "or locally in <strong>Claude</strong> or <strong>OpenAI Codex</strong>."
        )
        rl = (
            "The business owns the code and rules outright, with zero lock-in to a single model vendor: "
            "the same engine runs inside your Azure tenant behind <b>Microsoft 365 Copilot</b>, "
            "or locally in <b>Claude</b> or <b>OpenAI Codex</b>."
        )
        return txt, htm, rl
    if "codex" in h or "openai" in h:
        txt = (
            "The business owns the code and rules outright, with zero lock-in to a single model vendor: "
            "the same engine runs locally in OpenAI Codex or Claude, or inside your Azure tenant behind Microsoft 365 Copilot."
        )
        htm = (
            "The business owns the code and rules outright, with zero lock-in to a single model vendor: "
            "the same engine runs locally in <strong>OpenAI Codex</strong> or <strong>Claude</strong>, "
            "or inside your Azure tenant behind <strong>Microsoft 365 Copilot</strong>."
        )
        rl = (
            "The business owns the code and rules outright, with zero lock-in to a single model vendor: "
            "the same engine runs locally in <b>OpenAI Codex</b> or <b>Claude</b>, "
            "or inside your Azure tenant behind <b>Microsoft 365 Copilot</b>."
        )
        return txt, htm, rl

    txt = (
        "The business owns the code and rules outright, with zero lock-in to a single model vendor: "
        "the same engine runs locally in Claude or OpenAI Codex, or inside your Azure tenant behind Microsoft 365 Copilot."
    )
    htm = (
        "The business owns the code and rules outright, with zero lock-in to a single model vendor: "
        "the same engine runs locally in <strong>Claude</strong> or <strong>OpenAI Codex</strong>, "
        "or inside your Azure tenant behind <strong>Microsoft 365 Copilot</strong>."
    )
    rl = (
        "The business owns the code and rules outright, with zero lock-in to a single model vendor: "
        "the same engine runs locally in <b>Claude</b> or <b>OpenAI Codex</b>, "
        "or inside your Azure tenant behind <b>Microsoft 365 Copilot</b>."
    )
    return txt, htm, rl


def _build_copy_blocks(brief_data: dict[str, Any]) -> dict[str, Any]:
    """Build synchronized prose strings shared by both HTML and PDF renderers."""
    m1 = brief_data["moment1"]
    m2 = brief_data["moment2"]
    m3 = brief_data["moment3"]
    spec = brief_data["spec"]

    delta_abs = _fmt_usd(abs(m1["bridge_delta"]))
    ar60_whole = _fmt_usd_whole(m1["ar_over_60"])
    m1_lead_dir = "beat" if m1["bridge_delta"] >= 0 else "trailed"

    p1_lead = (
        "Heartland did not replace its bank, its spreadsheets, or the newly acquired Tulsa branch’s "
        "QuickBooks file. Running those existing exports through one lightweight workflow built this "
        "week’s leadership pack, answered follow-up questions directly from the transactions, handled "
        "a live payment change, and held an unapproved payroll posting—in minutes."
        if m2["mode"] == "revised"
        else (
            "Heartland did not replace its bank, its spreadsheets, or the newly acquired Tulsa branch’s "
            "QuickBooks file. Running those existing exports through one lightweight workflow built this "
            "week’s leadership pack, traced variances directly to underlying transactions, staged a live "
            "payment review, and enforced payroll posting controls—in minutes."
        )
    )

    m1_html = (
        f"When asked why opening cash {m1_lead_dir} last week’s plan by <strong>{_esc(delta_abs)}</strong>, "
        f"the system traced the gap to three exact transactions across Frost Banking and biweekly payroll—"
        f"while flagging {_esc(ar60_whole)} in A/R past 60 days by invoice. Because chat answers, the web "
        f"dashboard, and the PDFs all pull from one verified calculation, leadership can interrogate the "
        f"pack live without creating a second version of the truth."
    )
    m1_rl = (
        f"When asked why opening cash {m1_lead_dir} last week’s plan by <b>{_xesc(delta_abs)}</b>, "
        f"the system traced the gap to three exact transactions across Frost Banking and biweekly payroll—"
        f"while flagging {_xesc(ar60_whole)} in A/R past 60 days by invoice. Because chat answers, the web "
        f"dashboard, and the PDFs all pull from one verified calculation, leadership can interrogate the "
        f"pack live without creating a second version of the truth."
    )

    amt_str = _fmt_usd(m2["amount"])
    if m2["mode"] == "revised":
        m2_html = (
            f"Previewing a one-week deferral of {_esc(m2['vendor'])}’s <strong>{_esc(amt_str)}</strong> "
            f"invoice wrote nothing to disk. Once the operator confirmed, the system recorded the approval, "
            f"left {_esc(m2['baseline_label'])} intact for comparison, and regenerated the dashboard and PDFs "
            f"as {_esc(m2['latest_label'])} with the payment moved once. People keep judgment and sign-off; "
            f"software handles the rebuild."
        )
        m2_rl = (
            f"Previewing a one-week deferral of {_xesc(m2['vendor'])}’s <b>{_xesc(amt_str)}</b> "
            f"invoice wrote nothing to disk. Once the operator confirmed, the system recorded the approval, "
            f"left {_xesc(m2['baseline_label'])} intact for comparison, and regenerated the dashboard and PDFs "
            f"as {_xesc(m2['latest_label'])} with the payment moved once. People keep judgment and sign-off; "
            f"software handles the rebuild."
        )
        m2_box_title = f"{m2['vendor'].split()[0]} {m2['vendor'].split()[1]} Deferral (Before & After)" if len(m2["vendor"].split()) >= 2 else "Payment Deferral (Before & After)"
    else:
        m2_html = (
            f"Before saving any change, an operator can preview moving {_esc(m2['vendor'])}’s "
            f"<strong>{_esc(amt_str)}</strong> payment from Week {m2['old_week']} to Week {m2['new_week']} "
            f"in memory with zero files modified. Once confirmed, the system logs the decision, preserves "
            f"{_esc(m2['baseline_label'])} untouched, and issues a new numbered draft with the payment moved once."
        )
        m2_rl = (
            f"Before saving any change, an operator can preview moving {_xesc(m2['vendor'])}’s "
            f"<b>{_xesc(amt_str)}</b> payment from Week {m2['old_week']} to Week {m2['new_week']} "
            f"in memory with zero files modified. Once confirmed, the system logs the decision, preserves "
            f"{_xesc(m2['baseline_label'])} untouched, and issues a new numbered draft with the payment moved once."
        )
        m2_box_title = "Baseline Forecast & Staged Revision"

    wire_str = _fmt_usd(m3["funding_wire_total"])
    var_str = _fmt_usd(m3["tieout_variance"])
    m3_html = (
        f"Heartland’s payroll register tied to the <strong>{_esc(wire_str)}</strong> funding wire to the "
        f"cent ({_esc(var_str)} variance). The system still refused ERP posting because one newly transferred "
        f"Tulsa technician carried an unmapped department code and no written Controller sign-off was recorded. "
        f"When drafting the board email, it excluded the employee pay register automatically and cited it by "
        f"filename and date only."
    )
    m3_rl = (
        f"Heartland’s payroll register tied to the <b>{_xesc(wire_str)}</b> funding wire to the "
        f"cent ({_xesc(var_str)} variance). The system still refused ERP posting because one newly transferred "
        f"Tulsa technician carried an unmapped department code and no written Controller sign-off was recorded. "
        f"When drafting the board email, it excluded the employee pay register automatically and cited it by "
        f"filename and date only."
    )

    p2_target = spec["target_company"]
    p2_lead = spec["lead_override"] or (
        "Solving one recurring bottleneck this way delivers useful work from the first cycle—and leaves "
        "the business with an owned operational layer that traditional enterprise software promised for "
        "years and rarely delivered."
    )

    systems_phrase = spec["target_systems"]
    workflows_phrase = _join_workflow_list(spec["target_workflows"])
    _, h_html, h_rl = _harness_sentence_parts(spec["target_harness"])

    c1_text = (
        f"Consolidating numbers, enforcing approvals, and integrating bolt-on acquisitions used to require "
        f"a multi-quarter ERP rollout, a data lake, or rigid point software that still ended up exported to "
        f"Excel. Putting company-specific rules and deterministic calculations behind an agent delivers that "
        f"operating capability directly across {systems_phrase}—and adapts cleanly as acquired entities cut over "
        f"to new systems."
    )
    c2_text = (
        f"Making recurring operations legible to software makes them legible to an agent—and therefore to any "
        f"executive or operator in plain English. As a built-in byproduct of running the workflow, leadership "
        f"stops waiting for someone to pull a tab or reconcile two files. Whatever part of the business is "
        f"wired in—{workflows_phrase}—can be questioned live, with every answer traced to the underlying record."
    )
    c3_text = (
        "Handing out chat licenses rarely moves the P&L, and broad strategy decks stall before anyone changes "
        "a habit. When software takes over repetitive assembly and surfaces exceptions while operators keep "
        "judgment and sign-off, teams become AI-fluent because their actual job gets faster and sharper. Early "
        "operational wins build confidence on the ground and free leadership bandwidth for larger growth bets "
        "across the front office and portfolio."
    )
    c4_prefix = (
        "Every workflow added to the same foundation—or replicated at a second portfolio company—reuses the "
        "same control architecture, audit trail, and conversational layer, changing only the company’s rules "
        "and export mappings. "
    )

    start_target = p2_target if spec["target_company_is_custom"] else "a portfolio company or back office"
    start_workflow = spec["target_workflow"] if spec["target_workflow_is_custom"] else "one high-friction workflow"
    start_owner = spec["target_owner"]
    start_html = (
        f"Pick {'' if spec['target_workflow_is_custom'] else ''}<strong>{_esc(start_workflow)}</strong> at "
        f"<strong>{_esc(start_target)}</strong> with {_esc(start_owner)} and 2–3 prior cycles of real exports. "
        f"We configure the rules, replay historical cycles until outputs tie out to the cent, and run two live "
        f"cycles in parallel with your existing process—measuring hands-on time, turnaround from complete inputs, "
        f"manual edits, exceptions caught, and approver review time (with external waits tracked separately)."
    )
    start_rl = (
        f"Pick <b>{_xesc(start_workflow)}</b> at "
        f"<b>{_xesc(start_target)}</b> with {_xesc(start_owner)} and 2–3 prior cycles of real exports. "
        f"We configure the rules, replay historical cycles until outputs tie out to the cent, and run two live "
        f"cycles in parallel with your existing process—measuring hands-on time, turnaround from complete inputs, "
        f"manual edits, exceptions caught, and approver review time (with external waits tracked separately)."
    )

    next_step = spec["next_step_override"] or (
        "Identify the first workflow and its owner to review two recent export cycles and scope a bounded parallel run."
    )

    return {
        "p1_lead": p1_lead,
        "m1_html": m1_html,
        "m1_rl": m1_rl,
        "m2_html": m2_html,
        "m2_rl": m2_rl,
        "m2_box_title": m2_box_title,
        "m3_html": m3_html,
        "m3_rl": m3_rl,
        "p2_target": p2_target,
        "p2_lead": p2_lead,
        "c1_text": c1_text,
        "c2_text": c2_text,
        "c3_text": c3_text,
        "c4_html": _esc(c4_prefix) + h_html,
        "c4_rl": _xesc(c4_prefix) + h_rl,
        "start_html": start_html,
        "start_rl": start_rl,
        "next_step": next_step,
    }


# ---------------------------------------------------------------------------
# HTML Renderer (`Session-Brief.html`, exact v5 Rare Kaon Arial-on-white)
# ---------------------------------------------------------------------------

_RARE_KAON_MARK_SVG = """<svg width="26" height="26" viewBox="0 0 100 100" aria-hidden="true" focusable="false">
  <g fill="none" stroke="currentColor" stroke-width="7">
    <path d="M30 10V92"></path>
    <path d="M30 50L97.6 31.9"></path>
    <path d="M30 50L52 88"></path>
    <path d="M2 50H30"></path>
  </g>
  <circle cx="30" cy="50" r="8.4" fill="currentColor"></circle>
</svg>"""

_BRIEF_HTML_CSS = """
:root {
  --paper: #ffffff;
  --surface: #f7f9fb;
  --ink: #0a1524;
  --ink-body: #2d3748;
  --ink-muted: #64748b;
  --hair: #dce3ec;
  --rule: #0a1524;
  --teal: #0f6076;
  --green: #166534;
  --green-bar: #16a34a;
  --green-soft: #f0fdf4;
  --red: #991b1b;
  --red-bar: #dc2626;
  --red-soft: #fef2f2;
  --slate-bar: #475569;
  --font-sans: Arial, "Helvetica Neue", Helvetica, -apple-system, BlinkMacSystemFont, sans-serif;
}
* { box-sizing: border-box; }
html, body {
  margin: 0;
  padding: 0;
  background: #e8edf3;
  color: var(--ink-body);
  font-family: var(--font-sans);
  font-size: 10.8px;
  line-height: 1.5;
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
}
.sheet-stack {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 24px;
  padding: 24px 12px 44px;
}
.sheet {
  width: 8.5in;
  height: 11in;
  background: var(--paper);
  color: var(--ink);
  box-shadow: 0 12px 32px rgba(10, 21, 36, 0.14);
  display: flex;
  flex-direction: column;
  padding: 0.48in 0.64in 0.4in;
  position: relative;
}
.brand-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 11px;
}
.brand-lockup {
  display: inline-flex;
  align-items: center;
  gap: 9px;
  color: var(--ink);
}
.brand-wordmark {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--ink);
}
.brand-sep {
  color: #94a3b8;
  font-weight: 400;
  margin: 0 3px;
}
.brand-doc-type {
  font-size: 9.5px;
  font-weight: 600;
  letter-spacing: 0.11em;
  text-transform: uppercase;
  color: var(--ink-muted);
}
.meta-right {
  text-align: right;
  font-size: 9.5px;
  color: var(--ink-muted);
  line-height: 1.4;
}
.meta-right strong {
  display: block;
  color: var(--ink);
  font-weight: 700;
  font-size: 10px;
}
.title-band {
  border-top: 2px solid var(--rule);
  border-bottom: 1px solid var(--hair);
  padding: 11px 0;
  margin-bottom: 14px;
}
.title-band h1 {
  margin: 0;
  font-size: 19.5px;
  font-weight: 700;
  line-height: 1.18;
  letter-spacing: -0.015em;
  color: var(--ink);
}
.title-band .accent-slot { color: var(--teal); }
.lead {
  font-size: 11.4px;
  line-height: 1.54;
  color: var(--ink);
  margin: 0 0 15px;
}
.main-col {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 13px;
}
.moment {
  display: grid;
  grid-template-columns: 1.04fr 0.96fr;
  gap: 20px;
  align-items: start;
  padding-bottom: 13px;
  border-bottom: 1px solid var(--hair);
}
.moment:last-of-type {
  border-bottom: none;
  padding-bottom: 0;
}
.t-label {
  font-size: 8.8px;
  font-weight: 700;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--teal);
  margin-bottom: 4px;
}
.moment h2 {
  margin: 0 0 5px;
  font-size: 13px;
  font-weight: 700;
  line-height: 1.26;
  color: var(--ink);
}
.moment p {
  margin: 0;
  font-size: 10.6px;
  line-height: 1.52;
  color: var(--ink-body);
}
.moment p strong { color: var(--ink); }
.proof-box {
  background: var(--surface);
  border: 1px solid var(--hair);
  border-top: 2px solid var(--rule);
  padding: 9px 12px 10px;
}
.proof-box-title {
  font-size: 8.6px;
  font-weight: 700;
  letter-spacing: 0.09em;
  text-transform: uppercase;
  color: var(--ink-muted);
  margin-bottom: 6px;
}
.ledger {
  width: 100%;
  border-collapse: collapse;
  font-size: 10px;
}
.ledger td {
  padding: 3.5px 0;
  border-bottom: 1px solid var(--hair);
  color: var(--ink-body);
}
.ledger tr:last-child td {
  border-bottom: none;
  padding-top: 5.5px;
  font-weight: 700;
  color: var(--ink);
}
.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.ledger td.pos, .compare-tbl td.pos, .pos { color: var(--green); font-weight: 700; }
.ledger td.neg, .compare-tbl td.neg, .neg { color: var(--red); font-weight: 700; }
.source-note {
  margin-top: 6px;
  font-size: 8.8px;
  color: var(--ink-muted);
  line-height: 1.38;
}
.compare-tbl {
  width: 100%;
  border-collapse: collapse;
  font-size: 9.8px;
}
.compare-tbl th {
  text-align: left;
  font-size: 8.5px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--ink-muted);
  padding: 0 0 4.5px;
  border-bottom: 1px solid var(--rule);
}
.compare-tbl th.num { text-align: right; padding-left: 10px; }
.compare-tbl td {
  padding: 4px 0;
  border-bottom: 1px solid var(--hair);
  color: var(--ink-body);
}
.compare-tbl td.num { padding-left: 10px; }
.compare-tbl tr:last-child td { border-bottom: none; }
.status-stack {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.status-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  padding: 5.5px 8px;
  font-size: 9.8px;
  background: #ffffff;
  border: 1px solid var(--hair);
  border-left: 3px solid var(--slate-bar);
  color: var(--ink-body);
}
.status-row.ok {
  background: var(--green-soft);
  border-color: #bbf7d0;
  border-left-color: var(--green-bar);
  color: var(--green);
}
.status-row.stop {
  background: var(--red-soft);
  border-color: #fecaca;
  border-left-color: var(--red-bar);
  color: var(--red);
}
.status-row strong { font-weight: 700; white-space: nowrap; }
.session-rollup {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 7px 0 0;
  border-top: 1px solid var(--hair);
  font-size: 9.2px;
  color: var(--ink-muted);
}
.session-rollup strong { color: var(--ink); font-weight: 700; }
.bridge-band {
  background: var(--ink);
  color: #e2e8f0;
  padding: 12px 15px;
  margin-top: auto;
}
.bridge-band strong {
  font-size: 9.6px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: #ffffff;
}
.bridge-band p {
  margin: 0;
  font-size: 10.6px;
  line-height: 1.5;
}
.value-grid {
  display: flex;
  flex-direction: column;
  gap: 13px;
}
.value-row {
  display: grid;
  grid-template-columns: 32px 1fr;
  gap: 13px;
  align-items: start;
  padding-bottom: 13px;
  border-bottom: 1px solid var(--hair);
}
.value-row:last-child {
  border-bottom: none;
  padding-bottom: 0;
}
.value-idx {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.04em;
  color: var(--teal);
  border-top: 2px solid var(--teal);
  padding-top: 4px;
  line-height: 1;
}
.value-row h2 {
  margin: 0 0 5px;
  font-size: 13.6px;
  font-weight: 700;
  line-height: 1.25;
  color: var(--ink);
}
.value-row p {
  margin: 0;
  font-size: 10.7px;
  line-height: 1.54;
  color: var(--ink-body);
}
.value-row p strong { font-weight: 700; color: var(--ink); }
.start-band {
  margin-top: auto;
  background: var(--surface);
  border: 1px solid var(--hair);
  border-top: 2px solid var(--green-bar);
  padding: 12px 15px;
}
.start-band .t-label { color: var(--green); }
.start-band h3 {
  margin: 2px 0 5px;
  font-size: 12.2px;
  font-weight: 700;
  color: var(--ink);
}
.start-band p {
  margin: 0;
  font-size: 10.4px;
  line-height: 1.52;
  color: var(--ink-body);
}
.start-band p + p { margin-top: 6px; color: var(--ink); }
.start-band strong { font-weight: 700; color: var(--ink); }
.log-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 9.4px;
  margin-top: 4px;
}
.log-table th, .log-table td {
  padding: 5px 6px;
  border-bottom: 1px solid var(--hair);
  text-align: left;
  vertical-align: top;
}
.log-table th {
  font-size: 8.4px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--ink-muted);
  border-bottom: 1.5px solid var(--rule);
  background: var(--surface);
}
.sheet-footer {
  margin-top: 10px;
  padding-top: 7px;
  border-top: 1px solid var(--hair);
  display: flex;
  justify-content: space-between;
  font-size: 8.8px;
  color: var(--ink-muted);
}
@page { size: letter portrait; margin: 0; }
@media print {
  html, body { background: #ffffff; }
  .sheet-stack { padding: 0; gap: 0; }
  .sheet { box-shadow: none; page-break-after: always; break-after: page; }
  .sheet:last-child { page-break-after: auto; break-after: auto; }
}
"""


def render_session_brief_html(brief_data: dict[str, Any], workspace_dir: Path) -> Path:
    """Render `<workspace_dir>/Session-Brief.html` from live workspace data + optional target spec."""
    workspace_dir = Path(workspace_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    out_path = workspace_dir / "Session-Brief.html"

    copy = _build_copy_blocks(brief_data)
    m1 = brief_data["moment1"]
    m2 = brief_data["moment2"]
    m3 = brief_data["moment3"]
    total_pages = 3 if brief_data.get("include_log") else 2

    # Moment 1 bridge rows
    m1_rows = [
        f"<tr><td>Prior week’s plan ({_esc(m1['prior_date_label'].split(',')[0])} edition)</td>"
        f'<td class="num">{_esc(_fmt_usd(m1["prior_value"]))}</td></tr>'
    ]
    for drv in m1["drivers"]:
        cls = "pos" if drv["amount"] >= 0 else "neg"
        m1_rows.append(
            f"<tr><td>{_esc(drv['label'])}</td>"
            f'<td class="num {cls}">{_esc(_fmt_usd(drv["amount"], signed=True))}</td></tr>'
        )
    m1_rows.append(
        f"<tr><td>Actual opening cash ({_esc(m1['current_date_label'].split(',')[0])})</td>"
        f'<td class="num">{_esc(_fmt_usd(m1["opening_cash"]))}</td></tr>'
    )

    # Moment 2 compare rows
    m2_rows = []
    for r in m2["rows"]:
        if m2["mode"] == "revised":
            after_cls = "pos" if r["changed"] and (r["after"] or 0) > r["before"] else ""
            m2_rows.append(
                f"<tr><td>{_esc(r['label'])}</td>"
                f'<td class="num">{_esc(_fmt_usd(r["before"]))}</td>'
                f'<td class="num {after_cls}">{_esc(_fmt_usd(r["after"]))}</td></tr>'
            )
        else:
            st_cls = "pos" if r["changed"] else ""
            m2_rows.append(
                f"<tr><td>{_esc(r['label'])}</td>"
                f'<td class="num">{_esc(_fmt_usd(r["before"]))}</td>'
                f'<td class="num {st_cls}">{_esc(r.get("status_text", ""))}</td></tr>'
            )

    # Moment 3 status rows
    tieout_cls = "ok" if abs(m3["tieout_variance"]) < 0.005 else "stop"
    hold_cls = "stop" if m3["blocking_count"] > 0 else "ok"
    hold_val = (
        f"Held ({m3['blocking_count']} unmapped dept)"
        if m3["blocking_count"] > 0
        else "Clear for approval"
    )
    pay_date_short = m3["pay_date_label"].split(",")[0]

    # Page 3 appendix (only when include_log=True)
    page3_html = ""
    if brief_data.get("include_log"):
        run_trs = []
        for r in brief_data.get("runs") or []:
            run_trs.append(
                f"<tr><td>{_esc(str(r['module']).title())}</td>"
                f"<td>{_esc(r['period'])}</td>"
                f"<td>Draft {int(r['draft_number'])}</td>"
                f"<td>{_esc(r['status'])}</td>"
                f"<td>{int(r['blocking_count'])} blocking / {int(r['open_review_count'])} review</td>"
                f"<td>{_esc(str(r['created_at'])[:19])}</td></tr>"
            )
        audit_trs = []
        for a in brief_data.get("audit_entries") or []:
            audit_trs.append(
                f"<tr><td>{_esc(str(a.get('timestamp', ''))[:19])}</td>"
                f"<td>{_esc(a.get('actor', '—'))}</td>"
                f"<td>{_esc(a.get('field_path') or a.get('action') or '—')}</td>"
                f"<td>{_esc(a.get('reason') or a.get('summary') or '—')}</td></tr>"
            )
        if not audit_trs:
            audit_trs.append('<tr><td colspan="4" style="color:var(--ink-muted);">No configuration or operator decisions recorded yet.</td></tr>')

        page3_html = f"""
<article class="sheet">
  <header>
    <div class="brand-row">
      <div class="brand-lockup">
        {_RARE_KAON_MARK_SVG}
        <span class="brand-wordmark">Rare Kaon</span>
        <span class="brand-sep">/</span>
        <span class="brand-doc-type">Session Audit Appendix</span>
      </div>
      <div class="meta-right">
        <strong>{_esc(brief_data['period_display'])}</strong>
        Full run &amp; decision log
      </div>
    </div>
    <div class="title-band">
      <h1>Session Run History &amp; Audit Trail</h1>
    </div>
  </header>
  <main class="main-col">
    <section>
      <div class="t-label">Preserved Drafts &amp; Runs</div>
      <table class="log-table">
        <thead><tr><th>Module</th><th>Period</th><th>Run</th><th>Status</th><th>Checks</th><th>Created (UTC)</th></tr></thead>
        <tbody>{"".join(run_trs)}</tbody>
      </table>
    </section>
    <section>
      <div class="t-label">Logged Operator &amp; Configuration Decisions</div>
      <table class="log-table">
        <thead><tr><th>Timestamp (UTC)</th><th>Actor</th><th>Target / Action</th><th>Summary</th></tr></thead>
        <tbody>{"".join(audit_trs)}</tbody>
      </table>
    </section>
  </main>
  <footer class="sheet-footer">
    <span>Rare Kaon &middot; Appendix generated directly from workspace SQLite &amp; audit trail</span>
    <span>Page 3 of {total_pages}</span>
  </footer>
</article>
"""

    doc_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rare Kaon — {_esc(brief_data['short_company_name'])} Session Brief</title>
<style>{_BRIEF_HTML_CSS}</style>
</head>
<body>
<div class="sheet-stack">

<article class="sheet">
  <header>
    <div class="brand-row">
      <div class="brand-lockup">
        {_RARE_KAON_MARK_SVG}
        <span class="brand-wordmark">Rare Kaon</span>
        <span class="brand-sep">/</span>
        <span class="brand-doc-type">Session Brief</span>
      </div>
      <div class="meta-right">
        <strong>{_esc(brief_data['period_display'])}</strong>
        {_esc(brief_data['header_status'])}
      </div>
    </div>
    <div class="title-band">
      <h1>{_esc(brief_data['short_company_name'])}: Weekly Cash &amp; Payroll</h1>
    </div>
  </header>

  <p class="lead">{_esc(copy['p1_lead'])}</p>

  <main class="main-col">
    <section class="moment">
      <div>
        <div class="t-label">01 &middot; One Reconciled Record</div>
        <h2>The conversation and the board pack read the exact same numbers</h2>
        <p>{copy['m1_html']}</p>
      </div>
      <div class="proof-box">
        <div class="proof-box-title">Opening Cash Variance Bridge</div>
        <table class="ledger">{"".join(m1_rows)}</table>
        <div class="source-note">{_esc(m1['source_note'])}</div>
      </div>
    </section>

    <section class="moment">
      <div>
        <div class="t-label">02 &middot; Safe Decisions &amp; Audit History</div>
        <h2>{_esc(m2['title'])}</h2>
        <p>{copy['m2_html']}</p>
      </div>
      <div class="proof-box">
        <div class="proof-box-title">{_esc(copy['m2_box_title'])}</div>
        <table class="compare-tbl">
          <thead>
            <tr><th>Cash Position</th><th class="num">{_esc(m2['baseline_label'])}</th><th class="num">{_esc(m2['latest_label'])}</th></tr>
          </thead>
          <tbody>{"".join(m2_rows)}</tbody>
        </table>
        <div class="source-note">{_esc(m2['source_note'])}</div>
      </div>
    </section>

    <section class="moment">
      <div>
        <div class="t-label">03 &middot; Controls Enforced in Software</div>
        <h2>Accounting holds and privacy walls held even when the math balanced</h2>
        <p>{copy['m3_html']}</p>
      </div>
      <div class="proof-box">
        <div class="proof-box-title">Payroll Control Summary ({_esc(pay_date_short)})</div>
        <div class="status-stack">
          <div class="status-row {tieout_cls}">
            <span>Register-to-wire tie-out</span>
            <strong>{_esc(_fmt_usd(m3['tieout_variance']))} variance</strong>
          </div>
          <div class="status-row {hold_cls}">
            <span>ERP posting &amp; approval</span>
            <strong>{_esc(hold_val)}</strong>
          </div>
          <div class="status-row">
            <span>Employee pay register</span>
            <strong>Excluded from email</strong>
          </div>
        </div>
        <div class="source-note">{_esc(m3['open_review_note'])}</div>
      </div>
    </section>

    <div class="session-rollup">
      <span><strong>Session activity:</strong> {_esc(brief_data['rollup_summary'])}</span>
      <span>Full log available on request</span>
    </div>

    <section class="bridge-band">
      <p>
        <strong>What ran in this session:</strong> the equivalent of a custom operational application—data intake across fragmented systems, exact calculations, exception holds, privacy boundaries, and executive reporting—delivered as a lightweight agent plugin rather than a multi-month software project.
      </p>
    </section>
  </main>

  <footer class="sheet-footer">
    <span>Rare Kaon &middot; Live demonstration on synthetic {_esc(brief_data['company_name'])} records</span>
    <span>Page 1 of {total_pages}</span>
  </footer>
</article>

<article class="sheet">
  <header>
    <div class="brand-row">
      <div class="brand-lockup">
        {_RARE_KAON_MARK_SVG}
        <span class="brand-wordmark">Rare Kaon</span>
        <span class="brand-sep">/</span>
        <span class="brand-doc-type">What This Means for Your Business</span>
      </div>
      <div class="meta-right">
        <strong>Immediate operational return</strong>
        Foundational long-term asset
      </div>
    </div>
    <div class="title-band">
      <h1>If You Run <span class="accent-slot">{_esc(copy['p2_target'])}</span> This Way</h1>
    </div>
  </header>

  <p class="lead">{_esc(copy['p2_lead'])}</p>

  <main class="main-col">
    <section class="value-grid">
      <div class="value-row">
        <div class="value-idx">01</div>
        <div>
          <h2>Enterprise operating capability at a fraction of ERP cost and complexity</h2>
          <p>{_esc(copy['c1_text'])}</p>
        </div>
      </div>

      <div class="value-row">
        <div class="value-idx">02</div>
        <div>
          <h2>Every configured slice of the business becomes directly queryable</h2>
          <p>{_esc(copy['c2_text'])}</p>
        </div>
      </div>

      <div class="value-row">
        <div class="value-idx">03</div>
        <div>
          <h2>AI adoption happens through real work, raising the company’s clock speed</h2>
          <p>{_esc(copy['c3_text'])}</p>
        </div>
      </div>

      <div class="value-row">
        <div class="value-idx">04</div>
        <div>
          <h2>You build an owned, vendor-neutral asset that compounds across workflows</h2>
          <p>{copy['c4_html']}</p>
        </div>
      </div>
    </section>

    <section class="start-band">
      <div class="t-label">How to Start Without a Big-Bang Commitment</div>
      <h3>One company, one recurring workflow, proven side-by-side</h3>
      <p>{copy['start_html']}</p>
      <p><strong>Next step:</strong> {_esc(copy['next_step'])}</p>
    </section>
  </main>

  <footer class="sheet-footer">
    <span>Rare Kaon &middot; rarekaon.com &middot; Clean-room, client-owned operational software</span>
    <span>Page 2 of {total_pages}</span>
  </footer>
</article>
{page3_html}
</div>
</body>
</html>
"""
    out_path.write_text(doc_html, encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# Pure-Python ReportLab PDF Renderer (`Session-Brief.pdf`, exact v5 parity)
# ---------------------------------------------------------------------------

_C_WHITE = colors.white
_C_SURFACE = colors.HexColor("#F7F9FB")
_C_INK = colors.HexColor("#0A1524")
_C_BODY = colors.HexColor("#2D3748")
_C_MUTED = colors.HexColor("#64748B")
_C_HAIR = colors.HexColor("#DCE3EC")
_C_TEAL = colors.HexColor("#0F6076")
_C_GREEN = colors.HexColor("#166534")
_C_GREEN_BAR = colors.HexColor("#16A34A")
_C_GREEN_SOFT = colors.HexColor("#F0FDF4")
_C_GREEN_BORDER = colors.HexColor("#BBF7D0")
_C_RED = colors.HexColor("#991B1B")
_C_RED_BAR = colors.HexColor("#DC2626")
_C_RED_SOFT = colors.HexColor("#FEF2F2")
_C_RED_BORDER = colors.HexColor("#FECACA")
_C_SLATE_BAR = colors.HexColor("#475569")
_C_INVERT_BODY = colors.HexColor("#E2E8F0")


def _draw_rare_kaon_mark(c: rl_canvas.Canvas, x: float, y_bottom: float, size: float = 20.0) -> None:
    """Draw the official Rare Kaon geometric mark (`Logo.astro`, 100x100 viewBox) in vector ink."""
    scale = size / 100.0

    def pt(sx: float, sy: float) -> tuple[float, float]:
        return (x + sx * scale, y_bottom + (100.0 - sy) * scale)

    c.saveState()
    c.setStrokeColor(_C_INK)
    c.setFillColor(_C_INK)
    c.setLineWidth(7.0 * scale)
    c.setLineCap(0)

    for (x1, y1), (x2, y2) in (
        ((30.0, 10.0), (30.0, 92.0)),
        ((30.0, 50.0), (97.6, 31.9)),
        ((30.0, 50.0), (52.0, 88.0)),
        ((2.0, 50.0), (30.0, 50.0)),
    ):
        p1 = pt(x1, y1)
        p2 = pt(x2, y2)
        c.line(p1[0], p1[1], p2[0], p2[1])

    cx, cy = pt(30.0, 50.0)
    c.circle(cx, cy, 8.4 * scale, stroke=0, fill=1)
    c.restoreState()


def _draw_masthead(
    c: rl_canvas.Canvas,
    *,
    page_w: float,
    page_h: float,
    margin_x: float,
    doc_type: str,
    meta_top: str,
    meta_sub: str,
    title_rl: str,
) -> float:
    """Draw Rare Kaon masthead + title band and return the next y cursor below the band."""
    content_w = page_w - 2 * margin_x
    top_y = page_h - 36.0

    # Mark + wordmark lockup
    mark_size = 19.5
    _draw_rare_kaon_mark(c, margin_x, top_y - mark_size, size=mark_size)

    baseline = top_y - 13.0
    c.setFont("Helvetica-Bold", 8.8)
    c.setFillColor(_C_INK)
    c.drawString(margin_x + mark_size + 7.0, baseline, "R A R E   K A O N")

    c.setFont("Helvetica", 8.5)
    c.setFillColor(_C_MUTED)
    sep_x = margin_x + mark_size + 89.0
    c.drawString(sep_x, baseline, "/")
    c.setFont("Helvetica-Bold", 7.8)
    c.drawString(sep_x + 9.0, baseline, doc_type.upper())

    # Right metadata
    c.setFont("Helvetica-Bold", 8.2)
    c.setFillColor(_C_INK)
    c.drawRightString(page_w - margin_x, top_y - 7.0, meta_top)
    c.setFont("Helvetica", 7.8)
    c.setFillColor(_C_MUTED)
    c.drawRightString(page_w - margin_x, top_y - 17.5, meta_sub)

    # Top 2pt ink rule
    rule_y = top_y - mark_size - 9.0
    c.setStrokeColor(_C_INK)
    c.setLineWidth(1.6)
    c.line(margin_x, rule_y, page_w - margin_x, rule_y)

    # Title paragraph (supports inline teal accent span)
    title_style = ParagraphStyle(
        "BriefTitle",
        fontName="Helvetica-Bold",
        fontSize=15.5,
        leading=18.5,
        textColor=_C_INK,
    )
    p_title = Paragraph(title_rl, title_style)
    _, th = p_title.wrap(content_w, 60)
    title_y = rule_y - 9.0 - th
    p_title.drawOn(c, margin_x, title_y)

    # Hairline below title
    hair_y = title_y - 9.0
    c.setStrokeColor(_C_HAIR)
    c.setLineWidth(0.75)
    c.line(margin_x, hair_y, page_w - margin_x, hair_y)
    return hair_y - 12.0


def _draw_footer(
    c: rl_canvas.Canvas,
    *,
    page_w: float,
    margin_x: float,
    left_text: str,
    page_num: int,
    total_pages: int,
) -> None:
    y = 28.0
    c.setStrokeColor(_C_HAIR)
    c.setLineWidth(0.7)
    c.line(margin_x, y + 11.0, page_w - margin_x, y + 11.0)
    c.setFont("Helvetica", 7.4)
    c.setFillColor(_C_MUTED)
    c.drawString(margin_x, y, left_text)
    c.drawRightString(page_w - margin_x, y, f"Page {page_num} of {total_pages}")


def _draw_p1_moment(
    c: rl_canvas.Canvas,
    *,
    x: float,
    y_top: float,
    content_w: float,
    kicker: str,
    heading: str,
    body_rl: str,
    box_title: str,
    box_kind: str,
    box_payload: Any,
    source_note: str,
    draw_bottom_rule: bool,
) -> float:
    """Draw one 2-column Page 1 proof row and return next y cursor."""
    gap = 16.0
    left_w = content_w * 0.505
    right_w = content_w - left_w - gap
    right_x = x + left_w + gap

    # Left column paragraphs
    st_kicker = ParagraphStyle("K", fontName="Helvetica-Bold", fontSize=7.2, leading=9.5, textColor=_C_TEAL)
    st_h2 = ParagraphStyle("H2", fontName="Helvetica-Bold", fontSize=10.4, leading=13.2, textColor=_C_INK)
    st_body = ParagraphStyle("B", fontName="Helvetica", fontSize=8.6, leading=12.8, textColor=_C_BODY)

    p_k = Paragraph(_xesc(kicker.upper()), st_kicker)
    p_h = Paragraph(_xesc(heading), st_h2)
    p_b = Paragraph(body_rl, st_body)

    _, kh = p_k.wrap(left_w, 30)
    _, hh = p_h.wrap(left_w, 40)
    _, bh = p_b.wrap(left_w, 160)
    left_h = kh + 3.0 + hh + 4.0 + bh

    # Measure right proof box contents
    box_pad_x = 9.5
    box_pad_top = 8.0
    box_pad_bot = 8.0
    inner_w = right_w - 2 * box_pad_x

    st_box_title = ParagraphStyle("BT", fontName="Helvetica-Bold", fontSize=7.0, leading=9.0, textColor=_C_MUTED)
    st_note = ParagraphStyle("SN", fontName="Helvetica", fontSize=7.2, leading=9.8, textColor=_C_MUTED)
    p_bt = Paragraph(_xesc(box_title.upper()), st_box_title)
    p_sn = Paragraph(_xesc(source_note), st_note)
    _, bth = p_bt.wrap(inner_w, 20)
    _, snh = p_sn.wrap(inner_w, 30)

    # Build inner table or status stack
    tbl_obj: Table | None = None
    status_items: list[tuple[str, str, str]] = []
    if box_kind == "ledger":
        st_lbl = ParagraphStyle("TL", fontName="Helvetica", fontSize=8.0, leading=10.2, textColor=_C_BODY)
        st_lbl_b = ParagraphStyle("TLB", fontName="Helvetica-Bold", fontSize=8.0, leading=10.2, textColor=_C_INK)
        st_num = ParagraphStyle("TN", fontName="Helvetica", fontSize=8.0, leading=10.2, textColor=_C_BODY, alignment=TA_RIGHT)
        st_num_b = ParagraphStyle("TNB", fontName="Helvetica-Bold", fontSize=8.0, leading=10.2, textColor=_C_INK, alignment=TA_RIGHT)
        st_pos = ParagraphStyle("TP", fontName="Helvetica-Bold", fontSize=8.0, leading=10.2, textColor=_C_GREEN, alignment=TA_RIGHT)
        st_neg = ParagraphStyle("TNG", fontName="Helvetica-Bold", fontSize=8.0, leading=10.2, textColor=_C_RED, alignment=TA_RIGHT)

        rows_data = []
        for idx, (lbl, val_str, tone) in enumerate(box_payload):
            is_last = idx == len(box_payload) - 1
            ls = st_lbl_b if is_last else st_lbl
            ns = st_num_b if is_last else (st_pos if tone == "pos" else (st_neg if tone == "neg" else st_num))
            rows_data.append([Paragraph(_xesc(lbl), ls), Paragraph(_xesc(val_str), ns)])

        tbl_obj = Table(rows_data, colWidths=[inner_w * 0.66, inner_w * 0.34])
        tbl_obj.setStyle(
            TableStyle(
                [
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 3.0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3.0),
                    ("LINEBELOW", (0, 0), (-1, -2), 0.5, _C_HAIR),
                    ("TOPPADDING", (0, -1), (-1, -1), 4.5),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ]
            )
        )
        _, mid_h = tbl_obj.wrap(inner_w, 200)
    elif box_kind == "compare":
        hdr_l, hdr_c1, hdr_c2, row_specs = box_payload
        st_th = ParagraphStyle("TH", fontName="Helvetica-Bold", fontSize=6.9, leading=9.0, textColor=_C_MUTED)
        st_thr = ParagraphStyle("THR", fontName="Helvetica-Bold", fontSize=6.9, leading=9.0, textColor=_C_MUTED, alignment=TA_RIGHT)
        st_td = ParagraphStyle("TD", fontName="Helvetica", fontSize=8.0, leading=10.2, textColor=_C_BODY)
        st_tdr = ParagraphStyle("TDR", fontName="Helvetica", fontSize=8.0, leading=10.2, textColor=_C_BODY, alignment=TA_RIGHT)
        st_pos = ParagraphStyle("TDP", fontName="Helvetica-Bold", fontSize=8.0, leading=10.2, textColor=_C_GREEN, alignment=TA_RIGHT)

        rows_data = [[Paragraph(_xesc(hdr_l.upper()), st_th), Paragraph(_xesc(hdr_c1.upper()), st_thr), Paragraph(_xesc(hdr_c2.upper()), st_thr)]]
        for lbl, c1_s, c2_s, highlight in row_specs:
            rows_data.append(
                [
                    Paragraph(_xesc(lbl), st_td),
                    Paragraph(_xesc(c1_s), st_tdr),
                    Paragraph(_xesc(c2_s), st_pos if highlight else st_tdr),
                ]
            )
        tbl_obj = Table(rows_data, colWidths=[inner_w * 0.42, inner_w * 0.29, inner_w * 0.29])
        tbl_obj.setStyle(
            TableStyle(
                [
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 3.2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3.2),
                    ("LINEBELOW", (0, 0), (-1, 0), 0.9, _C_INK),
                    ("LINEBELOW", (0, 1), (-1, -2), 0.5, _C_HAIR),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ]
            )
        )
        _, mid_h = tbl_obj.wrap(inner_w, 200)
    else:
        status_items = list(box_payload)
        mid_h = len(status_items) * 21.0 + max(0, len(status_items) - 1) * 4.5

    right_h = box_pad_top + bth + 5.0 + mid_h + 6.0 + snh + box_pad_bot

    # Draw left column
    cur_ly = y_top - kh
    p_k.drawOn(c, x, cur_ly)
    cur_ly -= 3.0 + hh
    p_h.drawOn(c, x, cur_ly)
    cur_ly -= 4.0 + bh
    p_b.drawOn(c, x, cur_ly)

    # Draw right shaded proof box
    box_bottom = y_top - right_h
    c.saveState()
    c.setFillColor(_C_SURFACE)
    c.setStrokeColor(_C_HAIR)
    c.setLineWidth(0.6)
    c.rect(right_x, box_bottom, right_w, right_h, stroke=1, fill=1)
    # 2pt ink top bar
    c.setStrokeColor(_C_INK)
    c.setLineWidth(1.6)
    c.line(right_x, y_top, right_x + right_w, y_top)
    c.restoreState()

    cur_ry = y_top - box_pad_top - bth
    p_bt.drawOn(c, right_x + box_pad_x, cur_ry)
    cur_ry -= 5.0

    if tbl_obj is not None:
        cur_ry -= mid_h
        tbl_obj.drawOn(c, right_x + box_pad_x, cur_ry)
    else:
        row_h = 21.0
        for lbl, val_s, tone in status_items:
            cur_ry -= row_h
            bg = _C_GREEN_SOFT if tone == "ok" else (_C_RED_SOFT if tone == "stop" else _C_WHITE)
            bd = _C_GREEN_BORDER if tone == "ok" else (_C_RED_BORDER if tone == "stop" else _C_HAIR)
            bar = _C_GREEN_BAR if tone == "ok" else (_C_RED_BAR if tone == "stop" else _C_SLATE_BAR)
            fg = _C_GREEN if tone == "ok" else (_C_RED if tone == "stop" else _C_BODY)

            c.saveState()
            c.setFillColor(bg)
            c.setStrokeColor(bd)
            c.setLineWidth(0.6)
            c.rect(right_x + box_pad_x, cur_ry, inner_w, row_h, stroke=1, fill=1)
            c.setFillColor(bar)
            c.rect(right_x + box_pad_x, cur_ry, 2.5, row_h, stroke=0, fill=1)
            c.setFillColor(fg)
            c.setFont("Helvetica", 7.9)
            c.drawString(right_x + box_pad_x + 7.5, cur_ry + 7.2, lbl)
            c.setFont("Helvetica-Bold", 7.9)
            c.drawRightString(right_x + box_pad_x + inner_w - 6.5, cur_ry + 7.2, val_s)
            c.restoreState()
            cur_ry -= 4.5
        cur_ry += 4.5

    p_sn.drawOn(c, right_x + box_pad_x, box_bottom + box_pad_bot)

    row_bottom = y_top - max(left_h, right_h)
    if draw_bottom_rule:
        rule_y = row_bottom - 11.0
        c.setStrokeColor(_C_HAIR)
        c.setLineWidth(0.65)
        c.line(x, rule_y, x + content_w, rule_y)
        return rule_y - 11.0
    return row_bottom - 10.0


def render_session_brief_pdf(brief_data: dict[str, Any], workspace_dir: Path) -> Path:
    """Render pure-Python ReportLab `<workspace_dir>/Session-Brief.pdf` matching the v5 design."""
    workspace_dir = Path(workspace_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    out_path = workspace_dir / "Session-Brief.pdf"

    page_w, page_h = letter  # 612 x 792 pt
    margin_x = 46.0
    content_w = page_w - 2 * margin_x
    total_pages = 3 if brief_data.get("include_log") else 2

    copy = _build_copy_blocks(brief_data)
    m1 = brief_data["moment1"]
    m2 = brief_data["moment2"]
    m3 = brief_data["moment3"]

    c = rl_canvas.Canvas(str(out_path), pagesize=letter)
    c.setTitle(f"Rare Kaon — {brief_data['short_company_name']} Session Brief")
    c.setAuthor("Rare Kaon")

    # ======================================================================
    # PAGE 1: WHAT HAPPENED AND WHY IT'S GOOD
    # ======================================================================
    y = _draw_masthead(
        c,
        page_w=page_w,
        page_h=page_h,
        margin_x=margin_x,
        doc_type="Session Brief",
        meta_top=brief_data["period_display"],
        meta_sub=brief_data["header_status"],
        title_rl=f"{_xesc(brief_data['short_company_name'])}: Weekly Cash &amp; Payroll",
    )

    st_lead = ParagraphStyle("Lead", fontName="Helvetica", fontSize=9.2, leading=13.8, textColor=_C_INK)
    p_lead = Paragraph(_xesc(copy["p1_lead"]), st_lead)
    _, lh = p_lead.wrap(content_w, 80)
    y -= lh
    p_lead.drawOn(c, margin_x, y)
    y -= 13.0

    # Moment 1
    m1_rows: list[tuple[str, str, str]] = [
        (f"Prior week’s plan ({m1['prior_date_label'].split(',')[0]} edition)", _fmt_usd(m1["prior_value"]), "base")
    ]
    for drv in m1["drivers"]:
        m1_rows.append(
            (drv["label"], _fmt_usd(drv["amount"], signed=True), "pos" if drv["amount"] >= 0 else "neg")
        )
    m1_rows.append((f"Actual opening cash ({m1['current_date_label'].split(',')[0]})", _fmt_usd(m1["opening_cash"]), "total"))

    y = _draw_p1_moment(
        c,
        x=margin_x,
        y_top=y,
        content_w=content_w,
        kicker="01 · One Reconciled Record",
        heading="The conversation and the board pack read the exact same numbers",
        body_rl=copy["m1_rl"],
        box_title="Opening Cash Variance Bridge",
        box_kind="ledger",
        box_payload=m1_rows,
        source_note=m1["source_note"],
        draw_bottom_rule=True,
    )

    # Moment 2
    m2_rows = []
    for r in m2["rows"]:
        if m2["mode"] == "revised":
            hl = bool(r["changed"] and (r["after"] or 0) > r["before"])
            m2_rows.append((r["label"], _fmt_usd(r["before"]), _fmt_usd(r["after"]), hl))
        else:
            m2_rows.append((r["label"], _fmt_usd(r["before"]), str(r.get("status_text", "")), bool(r["changed"])))

    y = _draw_p1_moment(
        c,
        x=margin_x,
        y_top=y,
        content_w=content_w,
        kicker="02 · Safe Decisions & Audit History",
        heading=m2["title"],
        body_rl=copy["m2_rl"],
        box_title=copy["m2_box_title"],
        box_kind="compare",
        box_payload=("Cash Position", m2["baseline_label"], m2["latest_label"], m2_rows),
        source_note=m2["source_note"],
        draw_bottom_rule=True,
    )

    # Moment 3
    tieout_tone = "ok" if abs(m3["tieout_variance"]) < 0.005 else "stop"
    hold_tone = "stop" if m3["blocking_count"] > 0 else "ok"
    hold_val = f"Held ({m3['blocking_count']} unmapped dept)" if m3["blocking_count"] > 0 else "Clear for approval"
    pay_date_short = m3["pay_date_label"].split(",")[0]

    y = _draw_p1_moment(
        c,
        x=margin_x,
        y_top=y,
        content_w=content_w,
        kicker="03 · Controls Enforced in Software",
        heading="Accounting holds and privacy walls held even when the math balanced",
        body_rl=copy["m3_rl"],
        box_title=f"Payroll Control Summary ({pay_date_short})",
        box_kind="status",
        box_payload=[
            ("Register-to-wire tie-out", f"{_fmt_usd(m3['tieout_variance'])} variance", tieout_tone),
            ("ERP posting & approval", hold_val, hold_tone),
            ("Employee pay register", "Excluded from email", "neutral"),
        ],
        source_note=m3["open_review_note"],
        draw_bottom_rule=False,
    )

    # Session rollup line
    rollup_y = max(y - 6.0, 114.0)
    c.setStrokeColor(_C_HAIR)
    c.setLineWidth(0.65)
    c.line(margin_x, rollup_y + 11.0, page_w - margin_x, rollup_y + 11.0)
    c.setFont("Helvetica-Bold", 7.6)
    c.setFillColor(_C_INK)
    c.drawString(margin_x, rollup_y, "Session activity:")
    c.setFont("Helvetica", 7.6)
    c.setFillColor(_C_MUTED)
    c.drawString(margin_x + 67.0, rollup_y, brief_data["rollup_summary"])
    c.drawRightString(page_w - margin_x, rollup_y, "Full log available on request")

    # Bottom-anchored inverted dark-ink bridge band
    st_bridge = ParagraphStyle("Br", fontName="Helvetica", fontSize=8.6, leading=12.6, textColor=_C_INVERT_BODY)
    p_bridge = Paragraph(
        '<font name="Helvetica-Bold" color="#FFFFFF">WHAT RAN IN THIS SESSION:</font> '
        "the equivalent of a custom operational application—data intake across fragmented systems, "
        "exact calculations, exception holds, privacy boundaries, and executive reporting—delivered "
        "as a lightweight agent plugin rather than a multi-month software project.",
        st_bridge,
    )
    _, brh = p_bridge.wrap(content_w - 24.0, 80)
    band_h = brh + 18.0
    band_y = 48.0
    c.saveState()
    c.setFillColor(_C_INK)
    c.rect(margin_x, band_y, content_w, band_h, stroke=0, fill=1)
    c.restoreState()
    p_bridge.drawOn(c, margin_x + 12.0, band_y + 9.0)

    _draw_footer(
        c,
        page_w=page_w,
        margin_x=margin_x,
        left_text=f"Rare Kaon · Live demonstration on synthetic {brief_data['company_name']} records",
        page_num=1,
        total_pages=total_pages,
    )
    c.showPage()

    # ======================================================================
    # PAGE 2: WHAT THIS MEANS FOR YOUR COMPANY IF YOU DO THIS
    # ======================================================================
    y = _draw_masthead(
        c,
        page_w=page_w,
        page_h=page_h,
        margin_x=margin_x,
        doc_type="What This Means for Your Business",
        meta_top="Immediate operational return",
        meta_sub="Foundational long-term asset",
        title_rl=f'If You Run <font color="#0F6076">{_xesc(copy["p2_target"])}</font> This Way',
    )

    p_lead2 = Paragraph(_xesc(copy["p2_lead"]), st_lead)
    _, l2h = p_lead2.wrap(content_w, 80)
    y -= l2h
    p_lead2.drawOn(c, margin_x, y)
    y -= 16.0

    consequences = [
        ("01", "Enterprise operating capability at a fraction of ERP cost and complexity", _xesc(copy["c1_text"])),
        ("02", "Every configured slice of the business becomes directly queryable", _xesc(copy["c2_text"])),
        ("03", "AI adoption happens through real work, raising the company’s clock speed", _xesc(copy["c3_text"])),
        ("04", "You build an owned, vendor-neutral asset that compounds across workflows", copy["c4_rl"]),
    ]

    st_vh = ParagraphStyle("VH", fontName="Helvetica-Bold", fontSize=11.0, leading=13.8, textColor=_C_INK)
    st_vp = ParagraphStyle("VP", fontName="Helvetica", fontSize=8.7, leading=13.2, textColor=_C_BODY)
    idx_col_w = 26.0
    idx_gap = 11.0
    txt_w = content_w - idx_col_w - idx_gap

    for i, (idx_str, title_s, body_rl) in enumerate(consequences):
        p_vh = Paragraph(_xesc(title_s), st_vh)
        p_vp = Paragraph(body_rl, st_vp)
        _, vhh = p_vh.wrap(txt_w, 40)
        _, vph = p_vp.wrap(txt_w, 120)

        # Teal tick + index number on left
        c.saveState()
        c.setStrokeColor(_C_TEAL)
        c.setLineWidth(1.6)
        c.line(margin_x, y - 2.0, margin_x + idx_col_w, y - 2.0)
        c.setFont("Helvetica-Bold", 9.0)
        c.setFillColor(_C_TEAL)
        c.drawString(margin_x, y - 14.0, idx_str)
        c.restoreState()

        p_vh.drawOn(c, margin_x + idx_col_w + idx_gap, y - vhh)
        p_vp.drawOn(c, margin_x + idx_col_w + idx_gap, y - vhh - 4.0 - vph)

        y = y - vhh - 4.0 - vph
        if i < len(consequences) - 1:
            y -= 11.0
            c.setStrokeColor(_C_HAIR)
            c.setLineWidth(0.65)
            c.line(margin_x, y, page_w - margin_x, y)
            y -= 12.0

    # Bottom-anchored green-topped starting-point box
    st_sk = ParagraphStyle("SK", fontName="Helvetica-Bold", fontSize=7.2, leading=9.5, textColor=_C_GREEN)
    st_sh = ParagraphStyle("SH", fontName="Helvetica-Bold", fontSize=10.0, leading=12.8, textColor=_C_INK)
    st_sp = ParagraphStyle("SP", fontName="Helvetica", fontSize=8.4, leading=12.4, textColor=_C_BODY)
    st_snx = ParagraphStyle("SNX", fontName="Helvetica", fontSize=8.4, leading=12.2, textColor=_C_INK)

    sb_pad = 12.0
    sb_w = content_w - 2 * sb_pad
    p_sk = Paragraph("HOW TO START WITHOUT A BIG-BANG COMMITMENT", st_sk)
    p_sh = Paragraph("One company, one recurring workflow, proven side-by-side", st_sh)
    p_sp = Paragraph(copy["start_rl"], st_sp)
    p_snx = Paragraph(f"<b>Next step:</b> {_xesc(copy['next_step'])}", st_snx)

    _, skh = p_sk.wrap(sb_w, 20)
    _, shh = p_sh.wrap(sb_w, 25)
    _, sph = p_sp.wrap(sb_w, 90)
    _, snxh = p_snx.wrap(sb_w, 40)

    sb_h = 10.0 + skh + 3.0 + shh + 4.0 + sph + 6.0 + snxh + 10.0
    sb_y = 48.0

    c.saveState()
    c.setFillColor(_C_SURFACE)
    c.setStrokeColor(_C_HAIR)
    c.setLineWidth(0.65)
    c.rect(margin_x, sb_y, content_w, sb_h, stroke=1, fill=1)
    c.setStrokeColor(_C_GREEN_BAR)
    c.setLineWidth(1.8)
    c.line(margin_x, sb_y + sb_h, margin_x + content_w, sb_y + sb_h)
    c.restoreState()

    cur_sy = sb_y + sb_h - 10.0 - skh
    p_sk.drawOn(c, margin_x + sb_pad, cur_sy)
    cur_sy -= 3.0 + shh
    p_sh.drawOn(c, margin_x + sb_pad, cur_sy)
    cur_sy -= 4.0 + sph
    p_sp.drawOn(c, margin_x + sb_pad, cur_sy)
    cur_sy -= 6.0 + snxh
    p_snx.drawOn(c, margin_x + sb_pad, cur_sy)

    _draw_footer(
        c,
        page_w=page_w,
        margin_x=margin_x,
        left_text="Rare Kaon · rarekaon.com · Clean-room, client-owned operational software",
        page_num=2,
        total_pages=total_pages,
    )
    c.showPage()

    # ======================================================================
    # OPTIONAL PAGE 3: SESSION AUDIT LOG & RUN HISTORY (`include_log=True`)
    # ======================================================================
    if brief_data.get("include_log"):
        y = _draw_masthead(
            c,
            page_w=page_w,
            page_h=page_h,
            margin_x=margin_x,
            doc_type="Session Audit Appendix",
            meta_top=brief_data["period_display"],
            meta_sub="Full run & decision log",
            title_rl="Session Run History &amp; Audit Trail",
        )

        st_sec = ParagraphStyle("Sec", fontName="Helvetica-Bold", fontSize=7.4, leading=10.0, textColor=_C_TEAL)
        st_th = ParagraphStyle("ATH", fontName="Helvetica-Bold", fontSize=7.0, leading=9.2, textColor=_C_MUTED)
        st_td = ParagraphStyle("ATD", fontName="Helvetica", fontSize=7.8, leading=10.4, textColor=_C_BODY)

        p_s1 = Paragraph("PRESERVED DRAFTS &amp; RUNS", st_sec)
        _, s1h = p_s1.wrap(content_w, 20)
        y -= s1h
        p_s1.drawOn(c, margin_x, y)
        y -= 6.0

        run_table_data = [
            [Paragraph(h, st_th) for h in ("MODULE", "PERIOD", "RUN", "STATUS", "CHECKS", "CREATED (UTC)")]
        ]
        for r in (brief_data.get("runs") or [])[:12]:
            run_table_data.append(
                [
                    Paragraph(_xesc(str(r["module"]).title()), st_td),
                    Paragraph(_xesc(r["period"]), st_td),
                    Paragraph(f"Draft {int(r['draft_number'])}", st_td),
                    Paragraph(_xesc(r["status"]), st_td),
                    Paragraph(f"{int(r['blocking_count'])} blocking / {int(r['open_review_count'])} review", st_td),
                    Paragraph(_xesc(str(r["created_at"])[:19]), st_td),
                ]
            )
        t_runs = Table(
            run_table_data,
            colWidths=[content_w * 0.13, content_w * 0.14, content_w * 0.11, content_w * 0.13, content_w * 0.25, content_w * 0.24],
        )
        t_runs.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), _C_SURFACE),
                    ("LINEBELOW", (0, 0), (-1, 0), 1.0, _C_INK),
                    ("LINEBELOW", (0, 1), (-1, -1), 0.5, _C_HAIR),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        _, rh = t_runs.wrap(content_w, 260)
        y -= rh
        t_runs.drawOn(c, margin_x, y)
        y -= 20.0

        p_s2 = Paragraph("LOGGED OPERATOR &amp; CONFIGURATION DECISIONS", st_sec)
        _, s2h = p_s2.wrap(content_w, 20)
        y -= s2h
        p_s2.drawOn(c, margin_x, y)
        y -= 6.0

        audit_table_data = [
            [Paragraph(h, st_th) for h in ("TIMESTAMP (UTC)", "ACTOR", "TARGET / ACTION", "SUMMARY")]
        ]
        entries = brief_data.get("audit_entries") or []
        if entries:
            for a in entries[:14]:
                audit_table_data.append(
                    [
                        Paragraph(_xesc(str(a.get("timestamp", ""))[:19]), st_td),
                        Paragraph(_xesc(a.get("actor", "—")), st_td),
                        Paragraph(_xesc(a.get("field_path") or a.get("action") or "—"), st_td),
                        Paragraph(_xesc(a.get("reason") or a.get("summary") or "—"), st_td),
                    ]
                )
        else:
            audit_table_data.append([Paragraph("No configuration or operator decisions recorded yet.", st_td), "", "", ""])

        t_audit = Table(
            audit_table_data,
            colWidths=[content_w * 0.21, content_w * 0.17, content_w * 0.22, content_w * 0.40],
        )
        t_audit.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), _C_SURFACE),
                    ("LINEBELOW", (0, 0), (-1, 0), 1.0, _C_INK),
                    ("LINEBELOW", (0, 1), (-1, -1), 0.5, _C_HAIR),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        _, ah = t_audit.wrap(content_w, 320)
        y -= ah
        t_audit.drawOn(c, margin_x, y)

        _draw_footer(
            c,
            page_w=page_w,
            margin_x=margin_x,
            left_text="Rare Kaon · Appendix generated directly from workspace SQLite & audit trail",
            page_num=3,
            total_pages=total_pages,
        )
        c.showPage()

    c.save()
    return out_path
