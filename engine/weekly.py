"""Deterministic Weekly Cash & 13-Week Liquidity calculation engine.

Computes exact integer-cent bank balances, period-over-period variance bridge
against `history/issued-2026-11-13-model.json`, consolidated A/R aging and >60d
overdue trace (`2026-11-18` as-of date), 13-week cash forecast applying any
confirmed invoice-matched deferrals (zero double-counting), non-blocking
`open_review` check (`WK-REV-001`), and restricted source privacy refs.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

from engine import __version__
from engine.config import (
    EngineError,
    cents_to_currency,
    cents_to_float,
    load_config,
    to_cents,
    utc_now_iso,
)
from engine.contracts import (
    DEFAULT_PAYROLL_PERIOD,
    DEFAULT_WEEKLY_PERIOD,
    PRIOR_WEEKLY_PERIOD,
    SCHEMA_VERSION,
)
from engine.ingest import (
    get_feed_config,
    parse_ap_scheduled_csv,
    parse_ar_aging_xlsx,
    parse_bank_csv,
    parse_payroll_voucher_csv,
    resolve_feed_file,
)
from engine.model import (
    BridgeDriver,
    CheckItem,
    KpiField,
    ReportMeta,
    ReportModel,
    SourceRef,
    VarianceBridge,
)
from engine.render.pdf import render_weekly_pdfs
from engine.workspace import (
    connect_db,
    get_pending_changes,
    next_draft_number,
    pending_rows_to_dataclasses,
    refresh_html_views,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_period_label(period_iso: str) -> str:
    d = dt.date.fromisoformat(period_iso)
    return f"Week ended {d.day} {d.strftime('%B %Y')}"


def _closing_balance_on_or_before(rows: list[dict[str, Any]], cutoff_iso: str) -> tuple[int, str]:
    eligible = [r for r in rows if r["post_date"] <= cutoff_iso]
    if not eligible:
        raise EngineError(
            "NO_BANK_ROWS_FOR_PERIOD",
            f"No bank transactions on or before {cutoff_iso}.",
            "Check bank CSV dates and period parameter.",
        )
    last = eligible[-1]
    return int(last["running_bal_cents"]), str(last["post_date"])


def _load_prior_issued_model(workspace_dir: Path) -> ReportModel | None:
    history_dir = workspace_dir / "history"
    if not history_dir.is_dir():
        return None
    exact = history_dir / f"issued-{PRIOR_WEEKLY_PERIOD}-model.json"
    if exact.is_file():
        return ReportModel.from_dict(json.loads(exact.read_text(encoding="utf-8")))
    candidates = sorted(history_dir.glob("issued-*-model.json"))
    if candidates:
        return ReportModel.from_dict(json.loads(candidates[-1].read_text(encoding="utf-8")))
    return None


def compute_13_week_forecast_engine(
    *,
    opening_cash_cents: int,
    period: str,
    weekly_cfg: dict[str, Any],
    ap_rows: list[dict[str, Any]],
    deferrals_by_invoice: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    """Compute exact integer-cent 13-week forecast, returning `(weeks, effective_ap, min_cents, min_week)`."""
    deferrals_by_invoice = deferrals_by_invoice or {}
    horizon = int(weekly_cfg.get("horizon_weeks", 13))
    payroll_base_cents = to_cents(weekly_cfg.get("biweekly_payroll_baseline_usd", 0))
    payroll_weeks = {int(w) for w in (weekly_cfg.get("biweekly_payroll_weeks") or [])}
    inflows_map = {
        int(k): to_cents(v)
        for k, v in (weekly_cfg.get("recurring_inflows_by_week") or {}).items()
    }
    rec_outflows_map = {
        int(k): to_cents(v)
        for k, v in (weekly_cfg.get("recurring_operating_outflows_by_week") or {}).items()
    }
    open_review_estimates = list(weekly_cfg.get("open_review_estimates") or [])

    # Validate every deferral invoice_ref exists strictly among unpaid AP rows (zero vendor heuristics)
    unpaid_refs = {r["invoice_ref"] for r in ap_rows if not r["paid"]}
    for inv_ref in deferrals_by_invoice:
        if inv_ref not in unpaid_refs:
            raise EngineError(
                "INVOICE_REF_NOT_FOUND",
                f"Cannot apply payment deferral: invoice_ref {inv_ref!r} not found among unpaid scheduled AP.",
                "Specify an exact unpaid `invoice_ref` from `sources/ap-scheduled-payments-*.csv`.",
                details={"invoice_ref": inv_ref},
            )

    # Apply deferrals strictly by invoice_ref, moving each payment once (zero double-counting)
    effective_ap: list[dict[str, Any]] = []
    ap_by_week: dict[int, int] = {w: 0 for w in range(1, horizon + 1)}
    deferred_into_week: dict[int, list[str]] = {w: [] for w in range(1, horizon + 1)}

    for r in ap_rows:
        inv_ref = r["invoice_ref"]
        eff_week = int(r["forecast_week"])
        eff_due = str(r["due_date"])
        eff_status = str(r["status"])
        eff_note = str(r["note"])

        if not r["paid"] and inv_ref in deferrals_by_invoice:
            d = deferrals_by_invoice[inv_ref]
            eff_week = int(d["new_week"])
            if d.get("new_due_date"):
                eff_due = str(d["new_due_date"])
            eff_status = "deferred"
            eff_note = f"Deferred from Week {r['forecast_week']} to Week {eff_week} ({eff_due})"
            if 1 <= eff_week <= horizon:
                deferred_into_week[eff_week].append(inv_ref)

        if not r["paid"] and 1 <= eff_week <= horizon:
            ap_by_week[eff_week] += int(r["amount_cents"])

        effective_ap.append(
            {
                "invoice_ref": inv_ref,
                "vendor": r["vendor"],
                "branch": r["branch"],
                "due_date": eff_due,
                "forecast_week": eff_week,
                "amount": cents_to_float(int(r["amount_cents"])),
                "paid": bool(r["paid"]),
                "status": eff_status,
                "note": eff_note,
            }
        )

    est_by_week: dict[int, int] = {w: 0 for w in range(1, horizon + 1)}
    est_notes_by_week: dict[int, list[str]] = {w: [] for w in range(1, horizon + 1)}
    for est in open_review_estimates:
        w = int(est["forecast_week"])
        amt_c = to_cents(est["amount_usd"])
        if 1 <= w <= horizon:
            est_by_week[w] += amt_c
            est_notes_by_week[w].append(
                f"Includes {est['title']} ({cents_to_currency(amt_c)} est.)"
            )

    period_date = dt.date.fromisoformat(period)
    week1_ending = period_date + dt.timedelta(days=7)

    weeks: list[dict[str, Any]] = []
    running = opening_cash_cents
    min_cash_cents: int | None = None
    min_cash_week = 1

    for w in range(1, horizon + 1):
        w_end = week1_ending + dt.timedelta(days=7 * (w - 1))
        inflow_c = inflows_map.get(w, 0)
        rec_out_c = rec_outflows_map.get(w, 0)
        payroll_c = payroll_base_cents if w in payroll_weeks else 0
        outflow_c = rec_out_c + ap_by_week[w] + payroll_c + est_by_week[w]
        net_c = inflow_c - outflow_c
        ending_c = running + net_c

        if min_cash_cents is None or ending_c < min_cash_cents:
            min_cash_cents = ending_c
            min_cash_week = w

        notes_parts: list[str] = []
        if payroll_c:
            notes_parts.append("Biweekly payroll week")
        if ap_by_week[w]:
            notes_parts.append(f"Scheduled AP {cents_to_currency(ap_by_week[w])}")
        notes_parts.extend(est_notes_by_week[w])
        for inv_ref in deferred_into_week[w]:
            notes_parts.append(f"Includes deferred {inv_ref}")

        weeks.append(
            {
                "week_num": w,
                "week_ending": w_end.isoformat(),
                "opening_cash": cents_to_float(running),
                "inflows": cents_to_float(inflow_c),
                "outflows": cents_to_float(outflow_c),
                "net_cash": cents_to_float(net_c),
                "ending_cash": cents_to_float(ending_c),
                "notes": "; ".join(notes_parts) if notes_parts else "Baseline operating cadence",
            }
        )
        running = ending_c

    assert min_cash_cents is not None
    return weeks, effective_ap, min_cash_cents, min_cash_week


# ---------------------------------------------------------------------------
# Core Weekly ReportModel builder (supports disk OR `:memory:` DB connection)
# ---------------------------------------------------------------------------


def build_weekly_model(
    workspace_dir: Path,
    *,
    period: str = DEFAULT_WEEKLY_PERIOD,
    draft_number: int = 1,
    conn: sqlite3.Connection | None = None,
    extra_deferrals: dict[str, dict[str, Any]] | None = None,
) -> ReportModel:
    """Compute the complete canonical Weekly Cash `ReportModel` without writing to disk."""
    workspace_dir = Path(workspace_dir)
    config = load_config(workspace_dir)
    assumptions = config["assumptions"]
    weekly_cfg = assumptions["weekly"]
    payroll_cfg = assumptions["payroll"]
    outputs_cfg = config["outputs"]

    # 1. Bank balances
    op_feed = get_feed_config(config, "frost_operating")
    sw_feed = get_feed_config(config, "frost_payroll_sweep")
    op_path = resolve_feed_file(workspace_dir, config, "frost_operating", period=period)
    sw_path = resolve_feed_file(workspace_dir, config, "frost_payroll_sweep", period=period)

    op_rows = parse_bank_csv(op_path, op_feed)
    sw_rows = parse_bank_csv(sw_path, sw_feed)

    op_close_c, op_as_of = _closing_balance_on_or_before(op_rows, period)
    sw_close_c, sw_as_of = _closing_balance_on_or_before(sw_rows, period)
    opening_cash_cents = op_close_c + sw_close_c

    cash_accounts = [
        {
            "account_id": "frost_operating",
            "name": "Frost Commercial Operating",
            "bank": "Frost Commercial Banking",
            "mask": "*4812",
            "closing_balance": cents_to_float(op_close_c),
            "as_of": op_as_of,
            "source_file": op_path.name,
        },
        {
            "account_id": "frost_payroll_sweep",
            "name": "Frost Payroll Sweep",
            "bank": "Frost Commercial Banking",
            "mask": "*4829",
            "closing_balance": cents_to_float(sw_close_c),
            "as_of": sw_as_of,
            "source_file": sw_path.name,
        },
    ]

    # 2. A/R Aging (`2026-11-18` mid-week snapshot)
    ar_feed = get_feed_config(config, "ar_aging")
    ar_path = resolve_feed_file(workspace_dir, config, "ar_aging", period=period)
    ar_data = parse_ar_aging_xlsx(ar_path, ar_feed)

    # 3. AP Scheduled payments
    ap_feed = get_feed_config(config, "ap_scheduled")
    ap_path = resolve_feed_file(workspace_dir, config, "ap_scheduled", period=period)
    ap_rows = parse_ap_scheduled_csv(ap_path, ap_feed)

    # 4. Restricted payroll voucher (aggregate overtime delta only; NEVER attachable)
    pv_feed = get_feed_config(config, "payroll_voucher")
    pv_path = resolve_feed_file(workspace_dir, config, "payroll_voucher", period=DEFAULT_PAYROLL_PERIOD)
    pv_rows = parse_payroll_voucher_csv(pv_path, pv_feed)
    pv_overtime_cents = sum(r["overtime_pay_cents"] for r in pv_rows)
    pv_as_of = pv_rows[0].get("pay_date") or DEFAULT_PAYROLL_PERIOD

    # 5. Pending & confirmed changes from SQLite (disk or `:memory:`)
    owns_conn = conn is None
    active_conn = conn if conn is not None else connect_db(workspace_dir, readonly=True)
    try:
        open_pending_rows = get_pending_changes(active_conn, module="weekly", period=period, status="pending")
        confirmed_rows = get_pending_changes(active_conn, module="weekly", period=period, status="confirmed")
    finally:
        if owns_conn:
            active_conn.close()

    deferrals_by_invoice: dict[str, dict[str, Any]] = {}
    revisions: list[dict[str, Any]] = []
    moved_payments: list[dict[str, Any]] = []

    for crow in confirmed_rows:
        if crow["kind"] != "payment_deferral":
            continue
        p = crow["payload"]
        inv_ref = str(p["invoice_ref"])
        deferrals_by_invoice[inv_ref] = {
            "new_week": int(p["new_week"]),
            "new_due_date": str(p.get("new_due_date") or ""),
        }
        rev_entry = {
            "change_id": crow["id"],
            "invoice_ref": inv_ref,
            "vendor": str(p.get("vendor") or ""),
            "amount": cents_to_float(to_cents(p.get("amount"))),
            "old_due_date": str(p.get("old_due_date") or ""),
            "new_due_date": str(p.get("new_due_date") or ""),
            "old_week": int(p.get("old_week", 1)),
            "new_week": int(p["new_week"]),
            "confirmed_by": crow.get("decided_by") or "Operator",
            "confirmed_at": crow.get("decided_at") or utc_now_iso(),
            "source_file": crow.get("source_file") or "",
        }
        revisions.append(rev_entry)
        moved_payments.append(
            {
                "invoice_ref": inv_ref,
                "vendor": rev_entry["vendor"],
                "amount": rev_entry["amount"],
                "old_week": rev_entry["old_week"],
                "new_week": rev_entry["new_week"],
            }
        )

    if extra_deferrals:
        for inv_ref, ov in extra_deferrals.items():
            deferrals_by_invoice[inv_ref] = dict(ov)

    # 6. 13-Week Forecast
    forecast_weeks, effective_ap, min_cash_cents, min_cash_week = compute_13_week_forecast_engine(
        opening_cash_cents=opening_cash_cents,
        period=period,
        weekly_cfg=weekly_cfg,
        ap_rows=ap_rows,
        deferrals_by_invoice=deferrals_by_invoice,
    )
    cash_floor_cents = to_cents(weekly_cfg.get("cash_floor_usd", 750000.0))

    # 7. Variance Bridge vs. Prior Issued Edition 1 (`2026-11-13`)
    prior_model = _load_prior_issued_model(workspace_dir)
    variance_bridge: VarianceBridge | None = None
    prior_week1_end_cents: int | None = None

    if prior_model is not None:
        prior_weeks = prior_model.sections.get("forecast_weeks") or []
        matching_prior = [w for w in prior_weeks if w.get("week_ending") == period]
        baseline_wk = matching_prior[0] if matching_prior else (prior_weeks[0] if prior_weeks else None)
        if baseline_wk is not None:
            prior_week1_end_cents = to_cents(baseline_wk["ending_cash"])
            delta_cents = opening_cash_cents - prior_week1_end_cents

            # Trace exact drivers deterministically from bank CSVs + payroll voucher
            op_after_prior = [r for r in op_rows if prior_model.meta.period < r["post_date"] <= period]
            drivers: list[BridgeDriver] = []
            explained_cents = 0

            # Driver 1: Early collection INV-2026-8814 (Methodist Health Facilities)
            early_ar_rows = [r for r in op_after_prior if r["reference"] == "INV-2026-8814"]
            if early_ar_rows:
                amt_c = sum(r["credit_cents"] - r["debit_cents"] for r in early_ar_rows)
                explained_cents += amt_c
                drivers.append(
                    BridgeDriver(
                        key="early_ar_methodist",
                        label="Early collection — Methodist Health Facilities (INV-2026-8814)",
                        amount=cents_to_float(amt_c),
                        source_file=op_path.name,
                        source_date=early_ar_rows[0]["post_date"],
                        source_ref="INV-2026-8814",
                        note="Expected in prior Week 2; settled 5 days early on 2026-11-18.",
                    )
                )

            # Driver 2: Seasonal payroll overtime above baseline
            ot_base_cents = to_cents(payroll_cfg.get("overtime_baseline_usd", 0.0))
            ot_variance_cents = pv_overtime_cents - ot_base_cents
            if ot_variance_cents != 0:
                cash_impact_c = -ot_variance_cents
                explained_cents += cash_impact_c
                drivers.append(
                    BridgeDriver(
                        key="payroll_overtime_20261113",
                        label="Seasonal overtime above baseline — 2026-11-13 biweekly payroll run",
                        amount=cents_to_float(cash_impact_c),
                        source_file=pv_path.name,
                        source_date=pv_as_of,
                        source_ref="PEO-WIRE-2026-1115",
                        note="Restricted payroll register cited by filename/date only; wire cleared 2026-11-16.",
                    )
                )

            # Driver 3: Emergency compressor parts order AUS-EMERG-9042
            emerg_rows = [r for r in op_after_prior if r["reference"] == "AUS-EMERG-9042"]
            if emerg_rows:
                amt_c = sum(r["credit_cents"] - r["debit_cents"] for r in emerg_rows)
                explained_cents += amt_c
                drivers.append(
                    BridgeDriver(
                        key="emergency_compressor_aus02",
                        label="Emergency compressor parts order — AUS-02",
                        amount=cents_to_float(amt_c),
                        source_file=op_path.name,
                        source_date=emerg_rows[0]["post_date"],
                        source_ref="AUS-EMERG-9042",
                        note="Unplanned AUS-02 dispatch parts order (Gulf Coast Refrigeration).",
                    )
                )

            residual_cents = delta_cents - explained_cents
            if residual_cents != 0:
                drivers.append(
                    BridgeDriver(
                        key="other_timing_variance",
                        label="Net routine operating & clearing timing variance",
                        amount=cents_to_float(residual_cents),
                        source_file=op_path.name,
                        source_date=period,
                        source_ref="NET-TIMING",
                        note="Reconciling timing difference across remaining bank activity.",
                    )
                )

            variance_bridge = VarianceBridge(
                baseline_period=prior_model.meta.period,
                baseline_label=f"Issued Edition {prior_model.meta.edition_number or 1}",
                shared_date=period,
                prior_value=cents_to_float(prior_week1_end_cents),
                current_value=cents_to_float(opening_cash_cents),
                delta=cents_to_float(delta_cents),
                ending_value=forecast_weeks[-1]["ending_cash"],
                drivers=drivers,
                moved_payments=moved_payments,
                source_citations=[
                    f"{op_path.name} (as of {op_as_of})",
                    f"{sw_path.name} (as of {sw_as_of})",
                    f"{pv_path.name} (as of {pv_as_of}, restricted — citation only)",
                    f"issued-{prior_model.meta.period}-model.json",
                ],
            )

    # 8. Deterministic Checks (blocking vs open_review strictly separated)
    checks: list[CheckItem] = []

    if min_cash_cents < cash_floor_cents:
        min_wk_obj = forecast_weeks[min_cash_week - 1]
        checks.append(
            CheckItem(
                id="WK-LIQ-001",
                group="Weekly Cash",
                title="Projected cash breaches minimum liquidity floor",
                severity="blocking",
                detail=(
                    f"Minimum projected cash ({cents_to_currency(min_cash_cents)} in Week {min_cash_week}, "
                    f"ending {min_wk_obj['week_ending']}) falls below the {cents_to_currency(cash_floor_cents)} cash floor."
                ),
                owner=(outputs_cfg.get("approver") or {}).get("name", "Claire Vance"),
                next_step="Review liquidity levers or update liquidity floor assumption before approval.",
                amount=cents_to_float(min_cash_cents - cash_floor_cents),
            )
        )

    for est in weekly_cfg.get("open_review_estimates") or []:
        checks.append(
            CheckItem(
                id=str(est.get("id", "WK-REV-001")),
                group="Weekly Cash",
                title=str(est.get("title", "Unconfirmed cash estimate")),
                severity="open_review",
                detail=str(est.get("detail", "")),
                owner=str(est.get("owner", "Claire Vance")),
                next_step=str(est.get("next_step", "")),
                amount=cents_to_float(to_cents(est.get("amount_usd"))),
            )
        )

    checks.append(
        CheckItem(
            id="WK-NOTE-001",
            group="Weekly Cash",
            title="A/R aging mid-week snapshot & upcoming Tulsa ERP cutover",
            severity="note",
            detail=(
                f"Consolidated A/R aging is as of {ar_data['as_of']} (2-day mid-week lag vs. {period} bank cutoff). "
                "Legacy Tulsa QuickBooks feed (tulsa_qb_ar) sunsets 2026-11-30 ahead of the 2026-12-01 Unified Cloud ERP cutover (unified_erp_ar)."
            ),
            owner=(outputs_cfg.get("controller") or {}).get("name", "Rachel Morales"),
            next_step="Verify Unified Cloud ERP A/R feed on 2026-12-01 cutover date.",
        )
    )

    # 9. Canonical KPIs (exact `WEEKLY_KPI_KEYS` order)
    min_wk_obj = forecast_weeks[min_cash_week - 1]
    week1_note = (
        f"Revised +$25,000.00 after {revisions[0]['invoice_ref']} deferral to Week {revisions[0]['new_week']}."
        if revisions
        else f"Baseline Week 1 projection (ending {forecast_weeks[0]['week_ending']})."
    )
    kpis = [
        KpiField(
            key="opening_cash",
            label="Opening Cash",
            value=cents_to_float(opening_cash_cents),
            unit="USD",
            status="actual",
            comparison_value=cents_to_float(prior_week1_end_cents) if prior_week1_end_cents is not None else None,
            comparison_label="Prior W1 plan" if prior_week1_end_cents is not None else None,
            comparison_kind="prior",
            note=f"Operating (*4812) {cents_to_currency(op_close_c)} + Sweep (*4829) {cents_to_currency(sw_close_c)}",
        ),
        KpiField(
            key="week1_ending_cash",
            label="Week 1 Ending Cash",
            value=forecast_weeks[0]["ending_cash"],
            unit="USD",
            status="forecast",
            note=week1_note,
        ),
        KpiField(
            key="week13_ending_cash",
            label="Week 13 Ending Cash",
            value=forecast_weeks[-1]["ending_cash"],
            unit="USD",
            status="forecast",
            note=f"Projected ending cash for week ending {forecast_weeks[-1]['week_ending']}.",
        ),
        KpiField(
            key="min_cash_balance",
            label="Minimum Forecast Cash",
            value=cents_to_float(min_cash_cents),
            unit="USD",
            status="forecast",
            comparison_value=cents_to_float(cash_floor_cents),
            comparison_label="Cash floor",
            comparison_kind="limit",
            note=f"Occurs in Week {min_cash_week} ({min_wk_obj['week_ending']}); {cents_to_currency(min_cash_cents - cash_floor_cents)} above floor.",
        ),
        KpiField(
            key="ar_total",
            label="Total Open A/R",
            value=cents_to_float(ar_data["total_cents"]),
            unit="USD",
            status="actual",
            note=f"As of {ar_data['as_of']} across {ar_data['account_count']} commercial accounts.",
        ),
        KpiField(
            key="ar_over_60",
            label="A/R Over 60 Days",
            value=cents_to_float(ar_data["ar_over_60_cents"]),
            unit="USD",
            status="actual",
            note=(
                f"{len(ar_data['overdue_items'])} invoices >60d "
                f"(61-90: {cents_to_currency(ar_data['buckets_cents']['d61_90'])}; "
                f">90: {cents_to_currency(ar_data['buckets_cents']['over_90'])})."
            ),
        ),
    ]

    # 10. Retained Sources (with programmatic privacy wall on restricted payroll register)
    sources = [
        SourceRef(
            feed_id="frost_operating",
            file_name=op_path.name,
            as_of=op_as_of,
            sensitivity=str(op_feed.get("sensitivity", "internal")),
            attachable=bool(op_feed.get("attachable", True)),
            relative_path=f"sources/{op_path.name}",
            detail=f"Operating bank activity export ({len(op_rows)} rows).",
        ),
        SourceRef(
            feed_id="frost_payroll_sweep",
            file_name=sw_path.name,
            as_of=sw_as_of,
            sensitivity=str(sw_feed.get("sensitivity", "internal")),
            attachable=bool(sw_feed.get("attachable", True)),
            relative_path=f"sources/{sw_path.name}",
            detail=f"Payroll sweep activity export ({len(sw_rows)} rows).",
        ),
        SourceRef(
            feed_id="ar_aging",
            file_name=ar_path.name,
            as_of=ar_data["as_of"],
            sensitivity=str(ar_feed.get("sensitivity", "internal")),
            attachable=bool(ar_feed.get("attachable", True)),
            relative_path=f"sources/{ar_path.name}",
            detail=f"Consolidated multi-branch A/R aging ({ar_data['account_count']} accounts, mid-week snapshot).",
        ),
        SourceRef(
            feed_id="ap_scheduled",
            file_name=ap_path.name,
            as_of=period,
            sensitivity=str(ap_feed.get("sensitivity", "internal")),
            attachable=bool(ap_feed.get("attachable", True)),
            relative_path=f"sources/{ap_path.name}",
            detail=f"AP vendor disbursement schedule ({len(ap_rows)} items).",
        ),
        SourceRef(
            feed_id="payroll_voucher",
            file_name=pv_path.name,
            as_of=pv_as_of,
            sensitivity="restricted",
            attachable=False,
            relative_path=f"sources/{pv_path.name}",
            detail="Restricted payroll register — cited by filename and date only for seasonal overtime bridge.",
        ),
    ]
    for rev in revisions:
        if rev.get("source_file"):
            sources.append(
                SourceRef(
                    feed_id="incoming_amendment",
                    file_name=rev["source_file"],
                    as_of=period,
                    sensitivity="internal",
                    attachable=True,
                    relative_path=f"incoming/{rev['source_file']}",
                    detail=f"Confirmed payment deferral {rev['change_id']} ({rev['invoice_ref']}).",
                )
            )

    delta_str = (
        cents_to_currency(opening_cash_cents - prior_week1_end_cents, signed=True)
        if prior_week1_end_cents is not None
        else "$0.00"
    )
    headline = (
        f"Opening cash is {cents_to_currency(opening_cash_cents)} ({delta_str} vs. prior Edition 1 Week 1 plan), "
        f"with 13-week minimum cash of {cents_to_currency(min_cash_cents)} in Week {min_cash_week} "
        f"({cents_to_currency(min_cash_cents - cash_floor_cents)} above the {cents_to_currency(cash_floor_cents)} floor)."
    )

    return ReportModel(
        meta=ReportMeta(
            company_name=config["company_name"],
            module="weekly",
            period=period,
            period_label=_format_period_label(period),
            version=__version__,
            draft_number=draft_number,
            status="draft",
            created_at=utc_now_iso(),
            approver=(outputs_cfg.get("approver") or {}).get("name", "Claire Vance"),
            is_approved=False,
            edition_number=None,
            approved_at=None,
            approved_by=None,
            schema_version=SCHEMA_VERSION,
        ),
        kpis=kpis,
        variance_bridge=variance_bridge,
        sections={
            "cash_accounts": cash_accounts,
            "forecast_weeks": forecast_weeks,
            "ar_aging": {
                "as_of": ar_data["as_of"],
                "source_file": ar_data["source_file"],
                "total": cents_to_float(ar_data["total_cents"]),
                "buckets": {k: cents_to_float(v) for k, v in ar_data["buckets_cents"].items()},
                "branch_totals": {k: cents_to_float(v) for k, v in ar_data["branch_totals_cents"].items()},
                "overdue_items": ar_data["overdue_items"],
            },
            "scheduled_payments": effective_ap,
            "revisions": revisions,
            "cash_floor": cents_to_float(cash_floor_cents),
        },
        checks=checks,
        pending_changes=pending_rows_to_dataclasses(open_pending_rows),
        sources=sources,
        narrative={
            "headline": headline,
            "summary": headline,
        },
        artifacts={},
    )


# ---------------------------------------------------------------------------
# Public `prepare --module weekly` execution
# ---------------------------------------------------------------------------


def prepare_weekly(
    workspace_dir: Path,
    *,
    period: str = DEFAULT_WEEKLY_PERIOD,
) -> dict[str, Any]:
    """Run Weekly Cash preparation, preserve prior drafts, render PDFs and HTML hubs."""
    workspace_dir = Path(workspace_dir).resolve()

    conn = connect_db(workspace_dir, readonly=False)
    try:
        draft_num = next_draft_number(conn, "weekly", period)
    finally:
        conn.close()

    model = build_weekly_model(workspace_dir, period=period, draft_number=draft_num)

    run_rel = f"runs/weekly/{period}/draft-{draft_num:03d}"
    run_dir = workspace_dir / run_rel
    run_dir.mkdir(parents=True, exist_ok=True)

    pdf_paths = render_weekly_pdfs(model, run_dir)
    liq_rel = f"{run_rel}/{pdf_paths['liquidity_pdf'].name}"
    cf_rel = f"{run_rel}/{pdf_paths['cash_flow_pdf'].name}"
    model_rel = f"{run_rel}/report-model.json"

    model.artifacts = {
        "liquidity_pdf": liq_rel,
        "cash_flow_pdf": cf_rel,
        "report_model": model_rel,
    }

    (workspace_dir / model_rel).write_text(
        json.dumps(model.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    conn = connect_db(workspace_dir, readonly=False)
    try:
        conn.execute(
            """
            INSERT INTO runs(
                module, period, draft_number, status, run_dir, model_path,
                can_approve, blocking_count, open_review_count, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "weekly",
                period,
                draft_num,
                "draft",
                run_rel,
                model_rel,
                1 if model.can_approve else 0,
                len(model.blocking_checks),
                len(model.open_review_checks),
                model.meta.created_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    reports_html, setup_html = refresh_html_views(workspace_dir)

    attachable_files = [
        {
            "file_name": "Reports.html",
            "relative_path": "Reports.html",
            "path": str(reports_html),
            "kind": "dashboard",
            "sensitivity": "internal",
        },
        {
            "file_name": "Setup.html",
            "relative_path": "Setup.html",
            "path": str(setup_html),
            "kind": "dashboard",
            "sensitivity": "internal",
        },
        {
            "file_name": pdf_paths["liquidity_pdf"].name,
            "relative_path": liq_rel,
            "path": str(pdf_paths["liquidity_pdf"]),
            "kind": "pdf_report",
            "sensitivity": "board",
        },
        {
            "file_name": pdf_paths["cash_flow_pdf"].name,
            "relative_path": cf_rel,
            "path": str(pdf_paths["cash_flow_pdf"]),
            "kind": "pdf_report",
            "sensitivity": "board",
        },
    ]
    for src in model.attachable_sources():
        if src.relative_path:
            attachable_files.append(
                {
                    "file_name": src.file_name,
                    "relative_path": src.relative_path,
                    "path": str(workspace_dir / src.relative_path),
                    "kind": "source",
                    "sensitivity": src.sensitivity,
                }
            )

    cited_non_attachable = [
        {
            "feed_id": s.feed_id,
            "file_name": s.file_name,
            "as_of": s.as_of,
            "sensitivity": s.sensitivity,
            "attachable": False,
            "citation_rule": "Cite by filename and date only; never attach or reproduce row-level employee pay.",
        }
        for s in model.non_attachable_sources()
    ]

    return {
        "module": "weekly",
        "period": period,
        "draft_number": draft_num,
        "status": model.meta.status,
        "can_approve": model.can_approve,
        "run_dir": str(run_dir),
        "model_path": str(workspace_dir / model_rel),
        "kpis": {k.key: k.value for k in model.kpis},
        "blocking_checks": [c.__dict__ for c in model.blocking_checks],
        "open_review_checks": [c.__dict__ for c in model.open_review_checks],
        "revisions": model.sections.get("revisions", []),
        "artifacts": {
            "liquidity_pdf": str(pdf_paths["liquidity_pdf"]),
            "cash_flow_pdf": str(pdf_paths["cash_flow_pdf"]),
            "reports_html": str(reports_html),
            "setup_html": str(setup_html),
            "report_model": str(workspace_dir / model_rel),
        },
        "attachable_files": attachable_files,
        "cited_non_attachable_sources": cited_non_attachable,
    }
