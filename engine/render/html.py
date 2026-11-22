"""Single-model self-contained HTML dashboard renderers (zero external CDN/JS deps).

Implements the frozen signatures:
- `render_reports_html(models: list[ReportModel], config: dict[str, Any], workspace_dir: Path) -> Path`
- `render_setup_html(config: dict[str, Any], audit_entries: list[dict[str, Any]], workspace_dir: Path) -> Path`

All KPIs and governance fields are read directly from `ReportModel` and `config/*.yaml`
without recomputing business math.
"""

from __future__ import annotations

import html
import json
import os
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from engine.contracts import COMPANY_NAME
from engine.model import CheckItem, KpiField, ReportModel, SourceRef

TWO_PLACES = Decimal("0.01")


# ---------------------------------------------------------------------------
# Exact Decimal formatting helpers (parity with pdf.py)
# ---------------------------------------------------------------------------


def _to_decimal(value: float | int | str | Decimal | None) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def format_usd(value: float | int | str | Decimal | None, *, signed: bool = False) -> str:
    dec = _to_decimal(value)
    if dec is None:
        return "—"
    q = dec.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    if q < 0:
        return f"-${abs(q):,.2f}"
    if signed and q > 0:
        return f"+${q:,.2f}"
    return f"${q:,.2f}"


def format_kpi_value(kpi: KpiField) -> str:
    """Format a KpiField identically to the PDF surface."""
    if kpi.value is None:
        return "—"
    if kpi.unit == "text" or isinstance(kpi.value, str):
        return str(kpi.value)
    if kpi.unit == "USD":
        return format_usd(kpi.value)
    dec = _to_decimal(kpi.value)
    if dec is None:
        return str(kpi.value)
    if kpi.unit == "count":
        return f"{int(dec.quantize(Decimal('1'), rounding=ROUND_HALF_UP)):,}"
    if kpi.unit == "pct":
        q = dec.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        if (q * 100) % 10 == 0:
            return f"{q:.1f}%"
        return f"{q:.2f}%"
    return str(kpi.value)


def format_comparison(kpi: KpiField) -> str:
    if kpi.comparison_value is None and not kpi.comparison_label:
        return ""
    label = kpi.comparison_label or kpi.comparison_kind.title()
    if kpi.comparison_value is None:
        return label
    if kpi.unit == "USD":
        val_str = format_usd(kpi.comparison_value)
    elif kpi.unit == "count":
        dec = _to_decimal(kpi.comparison_value)
        val_str = f"{int(dec):,}" if dec is not None else str(kpi.comparison_value)
    elif kpi.unit == "pct":
        dec = _to_decimal(kpi.comparison_value)
        val_str = f"{dec.quantize(TWO_PLACES, rounding=ROUND_HALF_UP):.1f}%" if dec is not None else str(kpi.comparison_value)
    else:
        val_str = str(kpi.comparison_value)
    return f"{label}: {val_str}"


def _esc(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _to_relative_href(raw_path: str, workspace_dir: Path) -> str:
    """Convert an artifact path into a safe relative POSIX link within workspace_dir."""
    p = Path(raw_path)
    if not p.is_absolute():
        return p.as_posix()
    try:
        return p.resolve().relative_to(workspace_dir.resolve()).as_posix()
    except ValueError:
        return Path(os.path.relpath(p, workspace_dir)).as_posix()


def _find_nested(data: Any, paths: list[tuple[str, ...]]) -> tuple[bool, Any]:
    """Look up the first present key path in a nested dictionary."""
    for path in paths:
        cur = data
        found = True
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                found = False
                break
        if found:
            return True, cur
    return False, None


def _pretty_label(key: str) -> str:
    return key.replace("_", " ").replace("-", " ").strip().title()


def _format_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# ---------------------------------------------------------------------------
# Shared embedded CSS (zero external fonts, zero external URLs)
# ---------------------------------------------------------------------------

_BASE_CSS = """
:root {
  --navy: #162b4d;
  --navy-mid: #244270;
  --bg: #f4f6f9;
  --surface: #ffffff;
  --surface-alt: #f8fafc;
  --border: #d8e1ec;
  --border-strong: #94a3b8;
  --text: #1e293b;
  --muted: #64748b;
  --blue: #1e40af;
  --blue-bg: #eff6ff;
  --blue-border: #bfdbfe;
  --green: #166534;
  --green-bg: #dcfce7;
  --green-border: #86efac;
  --amber: #92400e;
  --amber-bg: #fef3c7;
  --amber-border: #fcd34d;
  --red: #991b1b;
  --red-bg: #fee2e2;
  --red-border: #fca5a5;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.5;
  font-size: 14px;
}
code, .mono {
  font-family: "SFMono-Regular", Menlo, Monaco, Consolas, "Liberation Mono", monospace;
  font-size: 12.5px;
}
.topbar {
  background: var(--navy);
  color: #ffffff;
  padding: 18px 28px;
  border-bottom: 3px solid var(--navy-mid);
}
.topbar-inner {
  max-width: 1220px;
  margin: 0 auto;
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: center;
  gap: 14px;
}
.brand-kicker {
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #cbd5e1;
  font-weight: 700;
}
.brand-title {
  margin: 2px 0 0 0;
  font-size: 21px;
  font-weight: 700;
  color: #ffffff;
}
.nav {
  display: flex;
  gap: 8px;
  align-items: center;
}
.nav a {
  color: #e2e8f0;
  text-decoration: none;
  padding: 7px 14px;
  border-radius: 6px;
  font-weight: 600;
  font-size: 13px;
  border: 1px solid rgba(255, 255, 255, 0.18);
}
.nav a.active {
  background: #ffffff;
  color: var(--navy);
  border-color: #ffffff;
}
.nav a:hover:not(.active) {
  background: rgba(255, 255, 255, 0.12);
}
.container {
  max-width: 1220px;
  margin: 22px auto 48px auto;
  padding: 0 24px;
}
.summary-strip {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
  margin-bottom: 22px;
}
.summary-tile {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 15px;
}
.summary-tile .label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--muted);
  font-weight: 700;
}
.summary-tile .val {
  font-size: 20px;
  font-weight: 700;
  color: var(--navy);
  margin-top: 2px;
}
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 20px 22px;
  margin-bottom: 22px;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}
.card-header {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 14px;
  margin-bottom: 16px;
}
.card-title {
  margin: 0;
  font-size: 18px;
  color: var(--navy);
}
.card-meta {
  font-size: 12.5px;
  color: var(--muted);
  margin-top: 3px;
}
.badges {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.badge {
  display: inline-block;
  padding: 3px 9px;
  border-radius: 999px;
  font-size: 11.5px;
  font-weight: 700;
  letter-spacing: 0.02em;
  border: 1px solid transparent;
  white-space: nowrap;
}
.badge-approved, .badge-pass, .badge-attachable {
  background: var(--green-bg);
  color: var(--green);
  border-color: var(--green-border);
}
.badge-draft, .badge-review, .badge-sandbox, .badge-internal {
  background: var(--amber-bg);
  color: var(--amber);
  border-color: var(--amber-border);
}
.badge-blocking, .badge-restricted, .badge-fail {
  background: var(--red-bg);
  color: var(--red);
  border-color: var(--red-border);
}
.badge-note, .badge-board, .badge-info {
  background: var(--blue-bg);
  color: var(--blue);
  border-color: var(--blue-border);
}
.badge-muted {
  background: var(--surface-alt);
  color: var(--muted);
  border-color: var(--border);
}
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 12px;
  margin-bottom: 18px;
}
.kpi-card {
  background: var(--surface-alt);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 13px 15px;
}
.kpi-label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--muted);
  font-weight: 700;
  display: flex;
  justify-content: space-between;
  gap: 6px;
}
.kpi-value {
  font-size: 21px;
  font-weight: 700;
  color: var(--navy);
  margin: 4px 0 3px 0;
  font-variant-numeric: tabular-nums;
}
.kpi-sub {
  font-size: 12px;
  color: var(--muted);
}
.callout {
  border-radius: 8px;
  padding: 11px 14px;
  margin-bottom: 14px;
  border: 1px solid var(--border);
  border-left-width: 4px;
  font-size: 13px;
}
.callout-blue {
  background: var(--blue-bg);
  border-color: var(--blue-border);
  border-left-color: var(--blue);
}
.callout-amber {
  background: var(--amber-bg);
  border-color: var(--amber-border);
  border-left-color: var(--amber);
}
.callout-red {
  background: var(--red-bg);
  border-color: var(--red-border);
  border-left-color: var(--red);
}
.callout-green {
  background: var(--green-bg);
  border-color: var(--green-border);
  border-left-color: var(--green);
}
h3.section-heading {
  font-size: 14px;
  color: var(--navy);
  margin: 18px 0 8px 0;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
h4.sub-heading {
  font-size: 13px;
  margin: 12px 0 6px 0;
}
.sub-blocking { color: var(--red); }
.sub-review { color: var(--amber); }
.sub-note { color: var(--blue); }
table {
  width: 100%;
  border-collapse: collapse;
  margin-bottom: 12px;
  font-size: 13px;
}
th, td {
  padding: 8px 10px;
  border: 1px solid var(--border);
  text-align: left;
  vertical-align: top;
}
th {
  background: var(--surface-alt);
  color: var(--navy);
  font-weight: 700;
  font-size: 12px;
}
td.num, th.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
tr:nth-child(even) td {
  background: #fcfdff;
}
.artifact-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 6px;
}
.artifact-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 12px;
  background: var(--blue-bg);
  color: var(--blue);
  border: 1px solid var(--blue-border);
  border-radius: 6px;
  text-decoration: none;
  font-weight: 600;
  font-size: 12.5px;
}
.artifact-link:hover {
  background: #dbeafe;
}
.chip-list {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.chip {
  display: inline-block;
  padding: 2px 6px;
  background: var(--surface-alt);
  border: 1px solid var(--border);
  border-radius: 4px;
  font-size: 11.5px;
  color: var(--text);
}
.gov-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
.gov-card {
  background: var(--surface-alt);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 13px 15px;
}
.gov-card .k {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--muted);
  font-weight: 700;
}
.gov-card .v {
  font-size: 16px;
  font-weight: 700;
  color: var(--navy);
  margin: 5px 0 4px 0;
}
.footer {
  text-align: center;
  color: var(--muted);
  font-size: 12px;
  padding-top: 10px;
  border-top: 1px solid var(--border);
}
"""


def _page_shell(
    *,
    title: str,
    company_name: str,
    active_tab: str,
    header_badge_html: str,
    body_html: str,
) -> str:
    rep_cls = "active" if active_tab == "reports" else ""
    set_cls = "active" if active_tab == "setup" else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>{_BASE_CSS}</style>
</head>
<body>
<header class="topbar">
  <div class="topbar-inner">
    <div>
      <div class="brand-kicker">{_esc(company_name)}</div>
      <h1 class="brand-title">{_esc(title)}</h1>
    </div>
    <nav class="nav" aria-label="Workspace navigation">
      {header_badge_html}
      <a href="Reports.html" class="{rep_cls}">Reports</a>
      <a href="Setup.html" class="{set_cls}">Setup &amp; Controls</a>
    </nav>
  </div>
</header>
<main class="container">
{body_html}
<footer class="footer">
  Single-model static HTML rendered directly from <code>report-model.json</code> and <code>config/*.yaml</code> &middot; Zero external dependencies.
</footer>
</main>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Reports.html rendering
# ---------------------------------------------------------------------------


def _render_checks_table_html(checks: list[CheckItem], empty_message: str) -> str:
    if not checks:
        return f'<p class="card-meta">{_esc(empty_message)}</p>'
    rows = []
    for chk in checks:
        sev_cls = {
            "blocking": "badge-blocking",
            "open_review": "badge-review",
            "note": "badge-note",
        }.get(chk.severity, "badge-muted")
        sev_label = {
            "blocking": "Blocking",
            "open_review": "Open Review · Non-Blocking",
            "note": "Note",
        }.get(chk.severity, chk.severity)
        blocks_badge = (
            '<span class="badge badge-blocking">YES — Blocks Approval</span>'
            if chk.blocks_approval
            else '<span class="badge badge-pass">NO — Non-Blocking</span>'
        )
        amt = format_usd(chk.amount) if chk.amount is not None else "—"
        next_html = f'<div class="card-meta"><strong>Next:</strong> {_esc(chk.next_step)}</div>' if chk.next_step else ""
        rows.append(
            f"<tr>"
            f'<td class="mono">{_esc(chk.id)}</td>'
            f'<td><span class="badge {sev_cls}">{_esc(sev_label)}</span></td>'
            f"<td>{blocks_badge}</td>"
            f"<td><strong>{_esc(chk.title)}</strong><div class=\"card-meta\">{_esc(chk.group)}</div></td>"
            f"<td>{_esc(chk.detail)}{next_html}</td>"
            f"<td>{_esc(chk.owner or '—')}</td>"
            f'<td class="num">{_esc(amt)}</td>'
            f"</tr>"
        )
    return (
        "<table>"
        "<thead><tr>"
        "<th>ID</th><th>Severity</th><th>Approval Gate</th><th>Check</th><th>Detail</th><th>Owner</th><th class=\"num\">Amount</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def _render_sources_html(model: ReportModel) -> str:
    if not model.sources:
        return '<p class="card-meta">No retained sources recorded.</p>'

    def _source_row(src: SourceRef) -> str:
        is_restricted = src.sensitivity == "restricted" or not src.attachable
        sens_cls = {
            "restricted": "badge-restricted",
            "internal": "badge-internal",
            "board": "badge-board",
        }.get(src.sensitivity, "badge-muted")
        attach_badge = (
            '<span class="badge badge-restricted">Non-Attachable · Citation Only</span>'
            if is_restricted
            else '<span class="badge badge-attachable">Attachable</span>'
        )
        # Programmatic privacy wall: restricted sources display filename and as_of date only.
        detail_col = (
            "Restricted source — cited by filename and date only; excluded from attachments."
            if is_restricted
            else _esc(src.detail or "Retained workspace input")
        )
        return (
            f"<tr>"
            f'<td class="mono">{_esc(src.feed_id)}</td>'
            f"<td><strong>{_esc(src.file_name)}</strong></td>"
            f"<td>{_esc(src.as_of or '—')}</td>"
            f'<td><span class="badge {sens_cls}">{_esc(src.sensitivity)}</span></td>'
            f"<td>{attach_badge}</td>"
            f"<td>{detail_col}</td>"
            f"</tr>"
        )

    rows = "".join(_source_row(s) for s in model.sources)
    return (
        "<table>"
        "<thead><tr>"
        "<th>Feed ID</th><th>File Name</th><th>As Of</th><th>Sensitivity</th><th>Attachment Policy</th><th>Notes</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
    )


def _render_model_card(model: ReportModel, workspace_dir: Path) -> str:
    meta = model.meta
    module_title = "Weekly Cash & 13-Week Liquidity" if meta.module == "weekly" else "Payroll Pre-Posting Review"

    # Draft vs Approved Edition badge
    if meta.is_approved or meta.status == "approved":
        ed_num = meta.edition_number if meta.edition_number is not None else 1
        status_badge = f'<span class="badge badge-approved">Approved &middot; Edition {ed_num}</span>'
        approval_meta = f" &middot; Approved by {_esc(meta.approved_by or meta.approver or 'Approver')} on {_esc(meta.approved_at or '')}"
    else:
        status_badge = f'<span class="badge badge-draft">Draft {meta.draft_number}</span>'
        approval_meta = f" &middot; Approver: {_esc(meta.approver)}" if meta.approver else ""

    # Check summary badges
    n_block = len(model.blocking_checks)
    n_rev = len(model.open_review_checks)
    open_pending = [p for p in model.pending_changes if p.needs_decision]
    if n_block > 0:
        gate_badge = f'<span class="badge badge-blocking">Approval Blocked ({n_block} blocking)</span>'
    elif open_pending:
        gate_badge = f'<span class="badge badge-draft">Pending Decision ({len(open_pending)})</span>'
    else:
        gate_badge = '<span class="badge badge-pass">Approval Gate Clear</span>'

    review_badge = (
        f'<span class="badge badge-review">{n_rev} Open Review (Non-Blocking)</span>'
        if n_rev > 0
        else '<span class="badge badge-muted">0 Open Review</span>'
    )

    parts: list[str] = [
        f'<section class="card" data-module="{_esc(meta.module)}" data-period="{_esc(meta.period)}" data-status="{_esc(meta.status)}">',
        '<div class="card-header">',
        "<div>",
        f'<h2 class="card-title">{_esc(module_title)} &mdash; {_esc(meta.period_label)}</h2>',
        f'<div class="card-meta">Period <span class="mono">{_esc(meta.period)}</span> &middot; Generated {_esc(meta.created_at)}{approval_meta} &middot; v{_esc(meta.version)}</div>',
        "</div>",
        f'<div class="badges">{status_badge}{gate_badge}{review_badge}</div>',
        "</div>",
    ]

    # Narrative summary
    headline = model.narrative.get("headline") or model.narrative.get("summary")
    if headline:
        parts.append(f'<div class="callout callout-blue">{_esc(headline)}</div>')

    # Revisions callout
    revisions = model.sections.get("revisions") or []
    if revisions:
        rev_items = []
        for rev in revisions:
            rev_items.append(
                f"<li><strong>{_esc(rev.get('change_id', 'REV'))}</strong> &mdash; "
                f"{_esc(rev.get('vendor', ''))} (<span class=\"mono\">{_esc(rev.get('invoice_ref', ''))}</span>, "
                f"<strong>{_esc(format_usd(rev.get('amount')))}</strong>): "
                f"moved from Week {rev.get('old_week', '?')} ({_esc(rev.get('old_due_date', ''))}) "
                f"to Week {rev.get('new_week', '?')} ({_esc(rev.get('new_due_date', ''))}); "
                f"confirmed by {_esc(rev.get('confirmed_by', 'operator'))} on {_esc(rev.get('confirmed_at', ''))}.</li>"
            )
        parts.append(
            '<div class="callout callout-blue">'
            "<strong>Confirmed Operator Revision(s) Applied</strong>"
            f"<ul style=\"margin:6px 0 0 18px;padding:0;\">{''.join(rev_items)}</ul>"
            "</div>"
        )

    # Pending changes callout
    if open_pending:
        p_items = "".join(
            f"<li><strong>{_esc(p.id)}</strong> ({_esc(p.kind)}): {_esc(p.summary)}</li>"
            for p in open_pending
        )
        parts.append(
            '<div class="callout callout-amber">'
            "<strong>Pending Change(s) Awaiting Operator Confirmation</strong>"
            f"<ul style=\"margin:6px 0 0 18px;padding:0;\">{p_items}</ul>"
            "</div>"
        )

    # KPI cards directly from model.kpis
    if model.kpis:
        kpi_cards = []
        for kpi in model.kpis:
            val_str = format_kpi_value(kpi)
            comp_str = format_comparison(kpi)
            sub_bits = []
            if comp_str:
                sub_bits.append(f"<div>{_esc(comp_str)}</div>")
            if kpi.note:
                sub_bits.append(f"<div>{_esc(kpi.note)}</div>")
            kpi_cards.append(
                f'<div class="kpi-card" data-kpi-key="{_esc(kpi.key)}" data-kpi-value="{_esc(kpi.value)}">'
                f'<div class="kpi-label"><span>{_esc(kpi.label)}</span><span>{_esc(kpi.status)}</span></div>'
                f'<div class="kpi-value">{_esc(val_str)}</div>'
                f'<div class="kpi-sub">{"".join(sub_bits)}</div>'
                f"</div>"
            )
        parts.append(f'<div class="kpi-grid">{"".join(kpi_cards)}</div>')

    # Variance bridge table if present
    bridge = model.variance_bridge
    if bridge is not None:
        b_rows = [
            f"<tr><td><strong>Prior Baseline &mdash; {_esc(bridge.baseline_label)}</strong></td>"
            f'<td class="mono">{_esc(bridge.baseline_period)}</td>'
            f"<td>Opening reference</td>"
            f'<td class="num"><strong>{_esc(format_usd(bridge.prior_value))}</strong></td></tr>'
        ]
        for drv in bridge.drivers:
            ref = " · ".join(x for x in (drv.source_file, drv.source_ref, drv.source_date) if x)
            b_rows.append(
                f"<tr><td>{_esc(drv.label)}</td>"
                f'<td class="mono">{_esc(ref)}</td>'
                f"<td>{_esc(drv.note)}</td>"
                f'<td class="num">{_esc(format_usd(drv.amount, signed=True))}</td></tr>'
            )
        b_rows.append(
            f"<tr><td><strong>Net Variance</strong></td><td></td><td>Model delta</td>"
            f'<td class="num"><strong>{_esc(format_usd(bridge.delta, signed=True))}</strong></td></tr>'
        )
        b_rows.append(
            f"<tr><td><strong>Current Balance ({_esc(bridge.shared_date)})</strong></td><td></td><td>Reconciled</td>"
            f'<td class="num"><strong>{_esc(format_usd(bridge.current_value))}</strong></td></tr>'
        )
        parts.append('<h3 class="section-heading">Cash Variance Bridge</h3>')
        parts.append(
            "<table><thead><tr><th>Driver</th><th>Source Reference</th><th>Note</th><th class=\"num\">Amount</th></tr></thead>"
            f"<tbody>{''.join(b_rows)}</tbody></table>"
        )

    # Artifacts (relative links to PDFs / Excel workbooks)
    if model.artifacts:
        links = []
        for art_key, art_path in model.artifacts.items():
            href = _to_relative_href(art_path, workspace_dir)
            label = _pretty_label(art_key)
            fname = Path(href).name
            links.append(
                f'<a class="artifact-link" href="{_esc(href)}">{_esc(label)} <span class="mono">({_esc(fname)})</span></a>'
            )
        parts.append('<h3 class="section-heading">Generated Artifacts</h3>')
        parts.append(f'<div class="artifact-list">{"".join(links)}</div>')

    # Control Checks — strictly separated Blocking vs. Open Review
    parts.append('<h3 class="section-heading">Control Checks &amp; Approval Gate</h3>')
    parts.append(f'<h4 class="sub-heading sub-blocking">Blocking Checks &mdash; Prevent Approval ({n_block})</h4>')
    parts.append(
        _render_checks_table_html(
            model.blocking_checks,
            "None — 0 blocking checks open.",
        )
    )
    parts.append(f'<h4 class="sub-heading sub-review">Open Review Items &mdash; Non-Blocking ({n_rev})</h4>')
    parts.append(
        _render_checks_table_html(
            model.open_review_checks,
            "No open review items.",
        )
    )
    if model.note_checks:
        parts.append(f'<h4 class="sub-heading sub-note">Informational Notes ({len(model.note_checks)})</h4>')
        parts.append(_render_checks_table_html(model.note_checks, ""))

    # Sources & privacy wall
    parts.append('<h3 class="section-heading">Retained Sources &amp; Privacy Boundary</h3>')
    parts.append(_render_sources_html(model))

    parts.append("</section>")
    return "\n".join(parts)


def render_reports_html(
    models: list[ReportModel],
    config: dict[str, Any],
    workspace_dir: Path,
) -> Path:
    """Render self-contained `<workspace_dir>/Reports.html` directly from ReportModel list."""
    workspace_dir = Path(workspace_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    out_path = workspace_dir / "Reports.html"

    company_name = (
        models[0].meta.company_name
        if models
        else str(config.get("company_name") or COMPANY_NAME)
    )
    outputs_cfg = config.get("outputs") if isinstance(config.get("outputs"), dict) else {}
    send_mode = outputs_cfg.get("send_mode")
    header_badge = (
        f'<span class="badge badge-draft">Send Mode: {_esc(send_mode)}</span>'
        if send_mode
        else ""
    )

    approved_count = sum(1 for m in models if m.meta.is_approved or m.meta.status == "approved")
    draft_count = len(models) - approved_count
    total_blocking = sum(len(m.blocking_checks) for m in models)
    total_review = sum(len(m.open_review_checks) for m in models)

    summary_html = f"""
<section class="summary-strip" aria-label="Workspace summary">
  <div class="summary-tile"><div class="label">Active Reports</div><div class="val">{len(models)}</div></div>
  <div class="summary-tile"><div class="label">Approved Editions</div><div class="val">{approved_count}</div></div>
  <div class="summary-tile"><div class="label">Working Drafts</div><div class="val">{draft_count}</div></div>
  <div class="summary-tile"><div class="label">Blocking Checks</div><div class="val" style="color:{'var(--red)' if total_blocking else 'var(--green)'};">{total_blocking}</div></div>
  <div class="summary-tile"><div class="label">Open Review (Non-Blocking)</div><div class="val" style="color:{'var(--amber)' if total_review else 'var(--navy)'};">{total_review}</div></div>
</section>
"""

    if models:
        cards_html = "\n".join(_render_model_card(m, workspace_dir) for m in models)
    else:
        cards_html = (
            '<section class="card"><h2 class="card-title">No reports prepared yet</h2>'
            '<p class="card-meta">Run a workflow preparation to populate this hub from <code>report-model.json</code>.</p></section>'
        )

    doc_html = _page_shell(
        title="Executive Reports Hub",
        company_name=company_name,
        active_tab="reports",
        header_badge_html=header_badge,
        body_html=summary_html + cards_html,
    )
    out_path.write_text(doc_html, encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# Setup.html rendering
# ---------------------------------------------------------------------------


def _extract_feeds_list(config: dict[str, Any]) -> list[dict[str, Any]]:
    raw = config.get("feeds")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        inner = raw.get("feeds")
        if isinstance(inner, list):
            return [item for item in inner if isinstance(item, dict)]
    return []


def _render_feeds_section(feeds: list[dict[str, Any]]) -> str:
    if not feeds:
        return '<p class="card-meta">No source feeds declared in <code>config/feeds.yaml</code>.</p>'

    rows: list[str] = []
    for f in feeds:
        sens = str(f.get("sensitivity", "internal"))
        sens_cls = {
            "restricted": "badge-restricted",
            "internal": "badge-internal",
            "board": "badge-board",
        }.get(sens, "badge-muted")
        sens_label = "restricted · non-attachable" if sens == "restricted" else sens

        # Column aliases
        aliases = f.get("column_aliases")
        if isinstance(aliases, dict) and aliases:
            alias_chips = []
            for canonical, alts in aliases.items():
                if isinstance(alts, list):
                    alt_str = ", ".join(str(a) for a in alts)
                else:
                    alt_str = str(alts)
                alias_chips.append(
                    f'<span class="chip"><strong class="mono">{_esc(canonical)}</strong> &larr; {_esc(alt_str)}</span>'
                )
            alias_html = f'<div class="chip-list">{"".join(alias_chips)}</div>'
        else:
            alias_html = '<span class="card-meta">—</span>'

        # Cutover lifecycle
        active_from = f.get("active_from")
        active_until = f.get("active_until")
        replaced_by = f.get("replaced_by_id")
        replaces = f.get("replaces_id")
        cutover_bits = []
        if active_from:
            cutover_bits.append(f'<div>From: <span class="mono">{_esc(active_from)}</span></div>')
        if active_until:
            cutover_bits.append(
                f'<div>Until: <span class="mono">{_esc(active_until)}</span> '
                f'<span class="badge badge-review">Cutover Sunset</span></div>'
            )
        if replaced_by:
            cutover_bits.append(f'<div>Replaced by: <span class="mono">{_esc(replaced_by)}</span></div>')
        if replaces:
            cutover_bits.append(
                f'<div>Replaces: <span class="mono">{_esc(replaces)}</span> '
                f'<span class="badge badge-info">Cutover Replacement</span></div>'
            )
        cutover_html = "".join(cutover_bits) if cutover_bits else '<span class="badge badge-pass">Active</span>'

        rows.append(
            f"<tr>"
            f'<td><div class="mono"><strong>{_esc(f.get("id", ""))}</strong></div><div>{_esc(f.get("name", ""))}</div></td>'
            f'<td><span class="badge badge-info">{_esc(f.get("module", ""))}</span></td>'
            f"<td><div>{_esc(f.get('provider', '—'))}</div><div class=\"card-meta\">Owner: {_esc(f.get('owner', '—'))}</div></td>"
            f"<td><div>{_esc(f.get('where', '—'))}</div><div class=\"card-meta\">Due: {_esc(f.get('due_rule', '—'))}</div></td>"
            f'<td><span class="badge {sens_cls}">{_esc(sens_label)}</span></td>'
            f"<td>{alias_html}</td>"
            f"<td>{cutover_html}</td>"
            f"</tr>"
        )

    return (
        "<table>"
        "<thead><tr>"
        "<th>Feed</th><th>Module</th><th>Provider &amp; Owner</th><th>Source &amp; Schedule</th>"
        "<th>Sensitivity</th><th>Column Aliases</th><th>Cutover Lifecycle</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def _flatten_config_rows(data: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for k, v in data.items():
        key_path = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict) and v:
            rows.extend(_flatten_config_rows(v, key_path))
        elif isinstance(v, list):
            if all(isinstance(x, dict) for x in v) and v:
                for idx, item in enumerate(v):
                    rows.extend(_flatten_config_rows(item, f"{key_path}[{idx}]"))
            else:
                rows.append((key_path, ", ".join(_format_scalar(x) for x in v) if v else "[]"))
        else:
            rows.append((key_path, _format_scalar(v)))
    return rows


def _render_assumptions_section(assumptions: dict[str, Any]) -> str:
    if not assumptions:
        return '<p class="card-meta">No assumptions loaded from <code>config/assumptions.yaml</code>.</p>'

    has_erp, erp_mode = _find_nested(
        assumptions,
        [
            ("erp_posting_mode",),
            ("payroll", "erp_posting_mode"),
            ("erp_mode",),
            ("payroll", "erp_mode"),
            ("erp", "mode"),
            ("erp", "erp_mode"),
        ],
    )
    has_ref, mapping_ref = _find_nested(
        assumptions,
        [
            ("mapping_approval_ref",),
            ("payroll", "mapping_approval_ref"),
            ("erp", "mapping_approval_ref"),
        ],
    )
    has_floor, cash_floor = _find_nested(
        assumptions,
        [
            ("cash_floor_usd",),
            ("weekly", "cash_floor_usd"),
            ("cash_floor",),
            ("min_cash_floor",),
            ("weekly", "cash_floor"),
            ("weekly", "min_cash_floor"),
        ],
    )
    has_tol, tol_cents = _find_nested(
        assumptions,
        [
            ("payroll_tolerance_cents",),
            ("tieout_tolerance_cents",),
            ("payroll", "tolerance_cents"),
            ("payroll", "tieout_tolerance_cents"),
        ],
    )

    gov_cards: list[str] = []

    erp_str = str(erp_mode) if has_erp and erp_mode is not None else "unset"
    erp_badge_cls = "badge-sandbox" if erp_str.lower() == "sandbox" else ("badge-approved" if erp_str.lower() == "production" else "badge-muted")
    gov_cards.append(
        '<div class="gov-card">'
        '<div class="k">ERP Posting Mode</div>'
        f'<div class="v"><span class="badge {erp_badge_cls}">{_esc(erp_str)}</span></div>'
        '<div class="card-meta">Controls whether payroll post runs dry-run preview or production gate.</div>'
        "</div>"
    )

    if has_ref and mapping_ref:
        ref_badge = f'<span class="badge badge-approved">{_esc(mapping_ref)}</span>'
        ref_note = "Signed-off department mapping approval reference recorded."
    else:
        ref_badge = '<span class="badge badge-blocking">null &mdash; Production Blocked</span>'
        ref_note = "Production ERP posting is programmatically refused until mapping_approval_ref is set."
    gov_cards.append(
        '<div class="gov-card">'
        '<div class="k">mapping_approval_ref</div>'
        f'<div class="v">{ref_badge}</div>'
        f'<div class="card-meta">{ref_note}</div>'
        "</div>"
    )

    if has_floor:
        gov_cards.append(
            '<div class="gov-card">'
            '<div class="k">Minimum Cash Floor</div>'
            f'<div class="v">{_esc(format_usd(cash_floor))}</div>'
            '<div class="card-meta">Weekly liquidity threshold tracked across all 13 forecast weeks.</div>'
            "</div>"
        )

    if has_tol:
        gov_cards.append(
            '<div class="gov-card">'
            '<div class="k">Payroll Tie-Out Tolerance</div>'
            f'<div class="v">{_esc(tol_cents)} cents</div>'
            '<div class="card-meta">Maximum allowed register vs. funding wire delta before hard stop.</div>'
            "</div>"
        )

    flat_rows = _flatten_config_rows(assumptions)
    tbl_rows = "".join(
        f'<tr><td class="mono">{_esc(k)}</td><td class="mono">{_esc(v)}</td></tr>'
        for k, v in flat_rows
    )
    return (
        f'<div class="gov-grid">{"".join(gov_cards)}</div>'
        "<table><thead><tr><th>Assumption Path</th><th>Configured Value</th></tr></thead>"
        f"<tbody>{tbl_rows}</tbody></table>"
    )


def _render_outputs_section(outputs: dict[str, Any]) -> str:
    if not outputs:
        return '<p class="card-meta">No output rules loaded from <code>config/outputs.yaml</code>.</p>'

    send_mode = outputs.get("send_mode", "draft_only")
    is_draft_only = str(send_mode).lower() == "draft_only"
    banner_cls = "callout-green" if is_draft_only else "callout-amber"
    banner_note = (
        "External auto-sending is disabled. All client and board distributions remain draft-only for human review."
        if is_draft_only
        else "Review send_mode policy before external distribution."
    )
    banner = (
        f'<div class="callout {banner_cls}">'
        f'<strong>Outbound Distribution Guardrail:</strong> '
        f'<span class="badge {"badge-pass" if is_draft_only else "badge-draft"}">{_esc(send_mode)}</span> '
        f"&mdash; {banner_note}"
        "</div>"
    )

    flat_rows = _flatten_config_rows(outputs)
    tbl_rows = "".join(
        f'<tr><td class="mono">{_esc(k)}</td><td class="mono">{_esc(v)}</td></tr>'
        for k, v in flat_rows
    )
    return (
        banner
        + "<table><thead><tr><th>Output Rule Path</th><th>Configured Value</th></tr></thead>"
        f"<tbody>{tbl_rows}</tbody></table>"
    )


def _render_audit_section(audit_entries: list[dict[str, Any]]) -> str:
    if not audit_entries:
        return '<p class="card-meta">No configuration or decision audit entries recorded in <code>config/config-audit.jsonl</code> yet.</p>'

    rows = []
    for entry in reversed(audit_entries):
        ts = entry.get("timestamp") or entry.get("recorded_at") or entry.get("ts") or "—"
        actor = entry.get("actor") or entry.get("by") or entry.get("recorded_by") or "—"
        field_path = entry.get("field_path") or entry.get("path") or entry.get("field") or entry.get("action") or "—"
        old_val = _format_scalar(entry.get("old_value") if "old_value" in entry else entry.get("old"))
        new_val = _format_scalar(entry.get("new_value") if "new_value" in entry else entry.get("new"))
        reason = entry.get("reason") or entry.get("summary") or entry.get("note") or ""
        rows.append(
            f"<tr>"
            f'<td class="mono">{_esc(ts)}</td>'
            f"<td>{_esc(actor)}</td>"
            f'<td class="mono">{_esc(field_path)}</td>'
            f'<td class="mono">{_esc(old_val)}</td>'
            f'<td class="mono"><strong>{_esc(new_val)}</strong></td>'
            f"<td>{_esc(reason)}</td>"
            f"</tr>"
        )

    return (
        "<table>"
        "<thead><tr>"
        "<th>Timestamp</th><th>Actor</th><th>Field / Action</th><th>Old Value</th><th>New Value</th><th>Reason</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def render_setup_html(
    config: dict[str, Any],
    audit_entries: list[dict[str, Any]],
    workspace_dir: Path,
) -> Path:
    """Render self-contained `<workspace_dir>/Setup.html` from YAML config and audit log."""
    workspace_dir = Path(workspace_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    out_path = workspace_dir / "Setup.html"

    assumptions = config.get("assumptions") if isinstance(config.get("assumptions"), dict) else {}
    _, nested_company = _find_nested(assumptions, [("company", "name")])
    company_name = str(config.get("company_name") or nested_company or COMPANY_NAME)
    feeds = _extract_feeds_list(config)
    outputs = config.get("outputs") if isinstance(config.get("outputs"), dict) else {}
    send_mode = outputs.get("send_mode")
    header_badge = (
        f'<span class="badge badge-draft">Send Mode: {_esc(send_mode)}</span>'
        if send_mode
        else ""
    )

    restricted_feeds = sum(1 for f in feeds if f.get("sensitivity") == "restricted")
    cutover_feeds = sum(1 for f in feeds if f.get("active_until") or f.get("replaced_by_id"))

    summary_html = f"""
<section class="summary-strip" aria-label="Configuration summary">
  <div class="summary-tile"><div class="label">Configured Feeds</div><div class="val">{len(feeds)}</div></div>
  <div class="summary-tile"><div class="label">Restricted Feeds</div><div class="val" style="color:var(--red);">{restricted_feeds}</div></div>
  <div class="summary-tile"><div class="label">Scheduled Cutovers</div><div class="val" style="color:var(--amber);">{cutover_feeds}</div></div>
  <div class="summary-tile"><div class="label">Audit Entries</div><div class="val">{len(audit_entries)}</div></div>
</section>
"""

    body_parts = [
        summary_html,
        '<section class="card">',
        '<div class="card-header"><div><h2 class="card-title">Source Feeds &amp; Cutover Rules (<code>config/feeds.yaml</code>)</h2>'
        '<div class="card-meta">Declarative input registry with privacy sensitivity walls, header alias normalization, and scheduled cutover windows.</div></div></div>',
        _render_feeds_section(feeds),
        "</section>",
        '<section class="card">',
        '<div class="card-header"><div><h2 class="card-title">Forecasting &amp; Control Assumptions (<code>config/assumptions.yaml</code>)</h2>'
        '<div class="card-meta">Explicit thresholds and ERP posting gates enforced deterministically by Python before agent execution.</div></div></div>',
        _render_assumptions_section(assumptions),
        "</section>",
        '<section class="card">',
        '<div class="card-header"><div><h2 class="card-title">Outputs &amp; Distribution Guardrails (<code>config/outputs.yaml</code>)</h2>'
        '<div class="card-meta">Artifact targets, approver configuration, and outbound send-mode controls.</div></div></div>',
        _render_outputs_section(outputs),
        "</section>",
        '<section class="card">',
        '<div class="card-header"><div><h2 class="card-title">Configuration &amp; Decision Audit Trail (<code>config/config-audit.jsonl</code>)</h2>'
        '<div class="card-meta">Append-only record of every schema-validated config change and human operator decision.</div></div></div>',
        _render_audit_section(audit_entries),
        "</section>",
    ]

    doc_html = _page_shell(
        title="Workspace Setup & Controls",
        company_name=company_name,
        active_tab="setup",
        header_badge_html=header_badge,
        body_html="\n".join(body_parts),
    )
    out_path.write_text(doc_html, encoding="utf-8")
    return out_path
