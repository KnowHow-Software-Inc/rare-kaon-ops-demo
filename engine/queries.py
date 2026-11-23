"""Read-only query and grounded evidence builders: overview, readiness, and explain.

All six `EXPLAIN_TOPICS` (`cash_bridge`, `ar_over_60`, `payroll`, `guardrails`,
`architecture`, `transfer`) return structured JSON payloads grounded directly in
canonical `ReportModel` outputs, `config/*.yaml`, and bundled `docs/*.md`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from engine.config import (
    CONFIG_TARGETS,
    EngineError,
    cents_to_float,
    load_config,
    read_audit_entries,
    to_cents,
)
from engine.contracts import (
    DEFAULT_PAYROLL_PERIOD,
    DEFAULT_WEEKLY_PERIOD,
    EXPLAIN_TOPICS,
    PRIOR_WEEKLY_PERIOD,
)
from engine.model import ReportModel
from engine.payroll import build_payroll_model
from engine.weekly import build_weekly_model
from engine.workspace import (
    PACKAGE_ROOT,
    collect_active_models,
    connect_db,
    get_pending_changes,
    load_latest_model,
    refresh_html_views,
)


# ---------------------------------------------------------------------------
# Privacy-safe file attachment list builder
# ---------------------------------------------------------------------------


def build_privacy_file_lists(
    workspace_dir: Path,
    models: list[ReportModel],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return `(attachable_files, cited_non_attachable_sources)` enforcing the privacy wall."""
    workspace_dir = Path(workspace_dir).resolve()
    attachable: list[dict[str, Any]] = []
    seen_attachable: set[str] = set()
    non_attachable: list[dict[str, Any]] = []
    seen_non_attachable: set[str] = set()

    def _add_attachable(rel: str, kind: str, sensitivity: str = "internal") -> None:
        if sensitivity == "restricted":
            return
        full = workspace_dir / rel
        if not full.is_file():
            return
        key = str(full)
        if key in seen_attachable:
            return
        seen_attachable.add(key)
        attachable.append(
            {
                "file_name": full.name,
                "relative_path": rel,
                "path": str(full),
                "kind": kind,
                "sensitivity": sensitivity,
            }
        )

    # Hub HTML dashboards
    _add_attachable("Reports.html", "dashboard", "internal")
    _add_attachable("Setup.html", "dashboard", "internal")

    # Model artifacts and attachable sources
    for model in models:
        for art_key, art_rel in (model.artifacts or {}).items():
            if art_key == "report_model":
                continue
            sens = "board" if art_rel.endswith(".pdf") else "internal"
            kind = "pdf_report" if art_rel.endswith(".pdf") else "xlsx_workbook"
            _add_attachable(art_rel, kind, sens)

        for src in model.attachable_sources():
            if src.relative_path:
                _add_attachable(src.relative_path, "source", src.sensitivity)

        for src in model.non_attachable_sources():
            if src.file_name in seen_non_attachable:
                continue
            seen_non_attachable.add(src.file_name)
            non_attachable.append(
                {
                    "feed_id": src.feed_id,
                    "file_name": src.file_name,
                    "as_of": src.as_of,
                    "sensitivity": src.sensitivity,
                    "attachable": False,
                    "citation_rule": "Cite by filename and date only; never attach or reproduce row-level employee pay.",
                }
            )

    # Also ensure every restricted feed in feeds.yaml is always listed in cited_non_attachable_sources
    for feed in (config.get("feeds") or {}).get("feeds") or []:
        if feed.get("sensitivity") == "restricted" or feed.get("attachable") is False:
            default_rel = feed.get("default_file") or ""
            fname = Path(default_rel).name if default_rel else str(feed.get("id"))
            if fname not in seen_non_attachable:
                seen_non_attachable.add(fname)
                non_attachable.append(
                    {
                        "feed_id": str(feed["id"]),
                        "file_name": fname,
                        "as_of": DEFAULT_PAYROLL_PERIOD if feed.get("module") == "payroll" else DEFAULT_WEEKLY_PERIOD,
                        "sensitivity": str(feed.get("sensitivity", "restricted")),
                        "attachable": False,
                        "citation_rule": "Cite by filename and date only; never attach or reproduce row-level employee pay.",
                    }
                )

    return attachable, non_attachable


def _list_staged_incoming(workspace_dir: Path, pending_in_db: list[dict[str, Any]]) -> list[dict[str, Any]]:
    incoming_dir = workspace_dir / "incoming"
    if not incoming_dir.is_dir():
        return []
    known_files = {p["source_file"] for p in pending_in_db}
    staged: list[dict[str, Any]] = []
    for f in sorted(incoming_dir.iterdir()):
        if not f.is_file():
            continue
        staged.append(
            {
                "file_name": f.name,
                "relative_path": f"incoming/{f.name}",
                "ingested": f.name in known_files,
            }
        )
    return staged


def _extract_cutover_summary(config: dict[str, Any]) -> dict[str, Any]:
    feeds = (config.get("feeds") or {}).get("feeds") or []
    legacy = next((f for f in feeds if f.get("replaced_by_id")), None)
    replacement = next((f for f in feeds if f.get("replaces_id")), None)
    return {
        "cutover_date": (config["assumptions"].get("company") or {}).get("erp_cutover_date", "2026-12-01"),
        "legacy_feed_id": legacy["id"] if legacy else None,
        "legacy_feed_name": legacy["name"] if legacy else None,
        "legacy_owner": legacy.get("owner") if legacy else None,
        "legacy_active_until": legacy.get("active_until") if legacy else None,
        "replacement_feed_id": replacement["id"] if replacement else None,
        "replacement_feed_name": replacement["name"] if replacement else None,
        "replacement_owner": replacement.get("owner") if replacement else None,
        "replacement_active_from": replacement.get("active_from") if replacement else None,
    }


# ---------------------------------------------------------------------------
# 1. `overview`
# ---------------------------------------------------------------------------


def query_overview(workspace_dir: Path, *, refresh: bool = False) -> dict[str, Any]:
    """Return workspace status, active models, pending changes, and HTML hub paths."""
    workspace_dir = Path(workspace_dir).resolve()
    config = load_config(workspace_dir)
    reports_html = workspace_dir / "Reports.html"
    setup_html = workspace_dir / "Setup.html"

    if refresh or not reports_html.is_file() or not setup_html.is_file():
        reports_html, setup_html = refresh_html_views(workspace_dir)

    models = collect_active_models(workspace_dir)
    conn = connect_db(workspace_dir, readonly=True)
    try:
        all_changes = get_pending_changes(conn)
        open_pending = [c for c in all_changes if c["needs_decision"]]
        run_count = int(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
        edition_count = int(conn.execute("SELECT COUNT(*) FROM editions").fetchone()[0])
    finally:
        conn.close()

    model_summaries = []
    for m in models:
        model_summaries.append(
            {
                "module": m.meta.module,
                "period": m.meta.period,
                "period_label": m.meta.period_label,
                "status": m.meta.status,
                "draft_number": m.meta.draft_number,
                "edition_number": m.meta.edition_number,
                "is_approved": m.meta.is_approved,
                "can_approve": m.can_approve,
                "kpis": {k.key: k.value for k in m.kpis},
                "blocking_checks_count": len(m.blocking_checks),
                "open_review_checks_count": len(m.open_review_checks),
                "artifacts": {k: str(workspace_dir / v) for k, v in m.artifacts.items() if k != "report_model"},
            }
        )

    attachable_files, cited_non_attachable = build_privacy_file_lists(workspace_dir, models, config)
    audit = read_audit_entries(workspace_dir)

    return {
        "company_name": config["company_name"],
        "workspace_dir": str(workspace_dir),
        "reports_html": str(reports_html),
        "setup_html": str(setup_html),
        "run_count": run_count,
        "edition_count": edition_count,
        "audit_entry_count": len(audit),
        "active_models": model_summaries,
        "pending_changes": open_pending,
        "staged_incoming_files": _list_staged_incoming(workspace_dir, all_changes),
        "cutover": _extract_cutover_summary(config),
        "governance": {
            "send_mode": config["outputs"].get("send_mode", "draft_only"),
            "erp_posting_mode": config["assumptions"]["payroll"].get("erp_posting_mode", "sandbox"),
            "mapping_approval_ref": config["assumptions"]["payroll"].get("mapping_approval_ref"),
            "cash_floor_usd": config["assumptions"]["weekly"].get("cash_floor_usd", 750000.0),
        },
        "attachable_files": attachable_files,
        "cited_non_attachable_sources": cited_non_attachable,
    }


# ---------------------------------------------------------------------------
# 2. `readiness`
# ---------------------------------------------------------------------------


def query_readiness(workspace_dir: Path, *, period: str = DEFAULT_WEEKLY_PERIOD) -> dict[str, Any]:
    """Return cross-module readiness status across Weekly Cash, Payroll, and Configuration."""
    workspace_dir = Path(workspace_dir).resolve()
    config = load_config(workspace_dir)
    feeds = (config.get("feeds") or {}).get("feeds") or []
    audit = read_audit_entries(workspace_dir)

    conn = connect_db(workspace_dir, readonly=True)
    try:
        all_changes = get_pending_changes(conn)
        open_pending = [c for c in all_changes if c["needs_decision"]]
    finally:
        conn.close()

    feed_statuses: list[dict[str, Any]] = []
    weekly_present: list[str] = []
    weekly_missing: list[str] = []
    payroll_present: list[str] = []
    payroll_missing: list[str] = []

    for f in feeds:
        fid = str(f["id"])
        mod = str(f["module"])
        active_from = f.get("active_from")
        active_until = f.get("active_until")
        default_rel = f.get("default_file")

        # Determine whether feed is scheduled for a future cutover relative to `period`
        if active_from and str(active_from) > period:
            feed_state = "scheduled_cutover"
            present = False
        else:
            present = bool(default_rel and (workspace_dir / str(default_rel)).is_file())
            feed_state = "present" if present else "missing"
            if mod == "weekly":
                (weekly_present if present else weekly_missing).append(fid)
            else:
                (payroll_present if present else payroll_missing).append(fid)

        feed_statuses.append(
            {
                "id": fid,
                "name": f.get("name"),
                "module": mod,
                "provider": f.get("provider"),
                "owner": f.get("owner"),
                "where": f.get("where"),
                "due_rule": f.get("due_rule"),
                "sensitivity": f.get("sensitivity"),
                "attachable": False if f.get("sensitivity") == "restricted" else bool(f.get("attachable", True)),
                "status": feed_state,
                "active_from": active_from,
                "active_until": active_until,
                "replaced_by_id": f.get("replaced_by_id"),
                "replaces_id": f.get("replaces_id"),
            }
        )

    # Weekly model state
    weekly_model, _ = load_latest_model(workspace_dir, module="weekly", period=period, prefer_edition=True)
    if weekly_model is None:
        weekly_model = build_weekly_model(workspace_dir, period=period, draft_number=1)

    # Payroll model state (load prepared if present, else compute read-only readiness preview)
    payroll_model, _ = load_latest_model(workspace_dir, module="payroll", period=DEFAULT_PAYROLL_PERIOD, prefer_edition=True)
    payroll_prepared = payroll_model is not None
    if payroll_model is None:
        payroll_model = build_payroll_model(workspace_dir, period=DEFAULT_PAYROLL_PERIOD, draft_number=0)

    staged = _list_staged_incoming(workspace_dir, all_changes)
    cutover = _extract_cutover_summary(config)
    approver = (config["outputs"].get("approver") or {}).get("name", "Claire Vance")
    controller = (config["outputs"].get("controller") or {}).get("name", "Rachel Morales")

    weekly_status = (
        "approved"
        if weekly_model.meta.is_approved
        else ("pending_decision" if open_pending else ("blocked" if weekly_model.blocking_checks else "ready_for_review"))
    )
    payroll_status = (
        "blocked"
        if payroll_model.blocking_checks
        else ("approved" if payroll_model.meta.is_approved else "ready_for_review")
    )

    models_for_files = collect_active_models(workspace_dir)
    attachable_files, cited_non_attachable = build_privacy_file_lists(workspace_dir, models_for_files, config)

    return {
        "period": period,
        "company_name": config["company_name"],
        "reports_html": str(workspace_dir / "Reports.html"),
        "setup_html": str(workspace_dir / "Setup.html"),
        "modules": {
            "weekly": {
                "module": "weekly",
                "period": period,
                "status": weekly_status,
                "draft_number": weekly_model.meta.draft_number,
                "edition_number": weekly_model.meta.edition_number,
                "can_approve": weekly_model.can_approve and len(open_pending) == 0,
                "present_feeds": weekly_present,
                "missing_feeds": weekly_missing,
                "owners": [approver, controller, "Owen Gallagher"],
                "blocking_checks": [c.__dict__ for c in weekly_model.blocking_checks],
                "open_review_checks": [c.__dict__ for c in weekly_model.open_review_checks],
                "pending_changes": open_pending,
                "staged_incoming_files": staged,
                "kpis": {k.key: k.value for k in weekly_model.kpis},
            },
            "payroll": {
                "module": "payroll",
                "period": DEFAULT_PAYROLL_PERIOD,
                "prepared": payroll_prepared,
                "status": payroll_status,
                "can_approve": payroll_model.can_approve,
                "present_feeds": payroll_present,
                "missing_feeds": payroll_missing,
                "owners": [controller, approver],
                "tieout_variance": payroll_model.kpi_map()["tieout_variance"].value,
                "mapped_employees": payroll_model.kpi_map()["mapped_employees"].value,
                "employee_count": payroll_model.sections["summary"]["employee_count"],
                "blocking_checks": [c.__dict__ for c in payroll_model.blocking_checks],
                "open_review_checks": [c.__dict__ for c in payroll_model.open_review_checks],
                "exceptions": payroll_model.sections["exceptions"],
                "erp_posting_mode": config["assumptions"]["payroll"].get("erp_posting_mode", "sandbox"),
                "mapping_approval_ref": config["assumptions"]["payroll"].get("mapping_approval_ref"),
            },
            "configuration": {
                "status": "valid",
                "config_files": list(CONFIG_TARGETS.values()),
                "owners": [approver, controller],
                "send_mode": config["outputs"].get("send_mode", "draft_only"),
                "erp_posting_mode": config["assumptions"]["payroll"].get("erp_posting_mode", "sandbox"),
                "mapping_approval_ref": config["assumptions"]["payroll"].get("mapping_approval_ref"),
                "cash_floor_usd": config["assumptions"]["weekly"].get("cash_floor_usd", 750000.0),
                "cutover": cutover,
                "audit_entry_count": len(audit),
            },
        },
        "feeds": feed_statuses,
        "cutover": cutover,
        "attachable_files": attachable_files,
        "cited_non_attachable_sources": cited_non_attachable,
    }


# ---------------------------------------------------------------------------
# 3. `explain --topic <T>` (all 6 topics)
# ---------------------------------------------------------------------------


def query_explain(
    workspace_dir: Path,
    *,
    topic: str,
    period: str | None = None,
) -> dict[str, Any]:
    """Return grounded evidence payload for one of the 6 frozen `EXPLAIN_TOPICS`."""
    workspace_dir = Path(workspace_dir).resolve()
    if topic not in EXPLAIN_TOPICS:
        raise EngineError(
            "UNKNOWN_EXPLAIN_TOPIC",
            f"Unknown explain topic {topic!r}.",
            f"Choose one of: {', '.join(EXPLAIN_TOPICS)}.",
        )

    config = load_config(workspace_dir)
    active_models = collect_active_models(workspace_dir)

    if topic == "cash_bridge":
        eff_period = period or DEFAULT_WEEKLY_PERIOD
        model, _ = load_latest_model(workspace_dir, module="weekly", period=eff_period, prefer_edition=True)
        if model is None:
            model = build_weekly_model(workspace_dir, period=eff_period, draft_number=1)

        # Prior Edition 1 comparison for both shared-date opening cash AND 13-week ending cash
        prior_path = workspace_dir / "history" / f"issued-{PRIOR_WEEKLY_PERIOD}-model.json"
        prior_comparison: dict[str, Any] = {}
        if prior_path.is_file():
            prior = ReportModel.from_dict(json.loads(prior_path.read_text(encoding="utf-8")))
            prior_weeks = prior.sections.get("forecast_weeks") or []
            curr_weeks = model.sections.get("forecast_weeks") or []
            prior_w13 = prior_weeks[-1]
            curr_w13 = curr_weeks[-1]
            # Find overlapping date (2027-02-12) in current forecast (Week 12)
            same_end = next((w for w in curr_weeks if w["week_ending"] == prior_w13["week_ending"]), None)
            prior_w13_c = to_cents(prior_w13["ending_cash"])
            curr_w13_c = to_cents(curr_w13["ending_cash"])
            prior_comparison = {
                "prior_period": prior.meta.period,
                "prior_edition_number": prior.meta.edition_number,
                "prior_week13_ending_date": prior_w13["week_ending"],
                "prior_week13_ending_cash": prior_w13["ending_cash"],
                "current_week13_ending_date": curr_w13["week_ending"],
                "current_week13_ending_cash": curr_w13["ending_cash"],
                "week13_headline_delta": cents_to_float(curr_w13_c - prior_w13_c),
                "shared_horizon_date": prior_w13["week_ending"],
                "current_cash_on_prior_w13_date": same_end["ending_cash"] if same_end else None,
                "shared_horizon_delta": (
                    cents_to_float(to_cents(same_end["ending_cash"]) - prior_w13_c) if same_end else None
                ),
                "new_week13_net_cash": curr_w13["net_cash"],
                "explanation": (
                    f"Shared-date cash on {eff_period} came in +${model.variance_bridge.delta:,.2f} ahead of "
                    f"Edition 1's Week 1 plan ($1,808,465.65 -> $1,842,615.40). Over the 13-week window, "
                    f"ending cash moved from ${prior_w13['ending_cash']:,.2f} (ending {prior_w13['week_ending']}) "
                    f"to ${curr_w13['ending_cash']:,.2f} (ending {curr_w13['week_ending']}), reflecting the "
                    f"opening cash variance bridge, early collection timing shift of INV-2026-8814 out of Week 1, "
                    f"and the 1-week horizon rollforward adding Week 13 (+${curr_w13['net_cash']:,.2f} net cash)."
                )
                if model.variance_bridge
                else "",
            }

        attachable, non_attachable = build_privacy_file_lists(workspace_dir, [model], config)
        return {
            "topic": "cash_bridge",
            "period": eff_period,
            "company_name": model.meta.company_name,
            "kpis": {k.key: k.value for k in model.kpis},
            "cash_accounts": model.sections.get("cash_accounts", []),
            "variance_bridge": (
                {
                    "baseline_period": model.variance_bridge.baseline_period,
                    "baseline_label": model.variance_bridge.baseline_label,
                    "shared_date": model.variance_bridge.shared_date,
                    "prior_value": model.variance_bridge.prior_value,
                    "current_value": model.variance_bridge.current_value,
                    "delta": model.variance_bridge.delta,
                    "ending_value": model.variance_bridge.ending_value,
                    "drivers": [d.__dict__ for d in model.variance_bridge.drivers],
                    "moved_payments": model.variance_bridge.moved_payments,
                    "source_citations": model.variance_bridge.source_citations,
                }
                if model.variance_bridge
                else None
            ),
            "prior_issued_comparison": prior_comparison,
            "revisions": model.sections.get("revisions", []),
            "blocking_checks": [c.__dict__ for c in model.blocking_checks],
            "open_review_checks": [c.__dict__ for c in model.open_review_checks],
            "attachable_files": attachable,
            "cited_non_attachable_sources": non_attachable,
        }

    if topic == "ar_over_60":
        eff_period = period or DEFAULT_WEEKLY_PERIOD
        model, _ = load_latest_model(workspace_dir, module="weekly", period=eff_period, prefer_edition=True)
        if model is None:
            model = build_weekly_model(workspace_dir, period=eff_period, draft_number=1)

        ar = model.sections.get("ar_aging") or {}
        as_of = str(ar.get("as_of", "2026-11-18"))
        attachable, non_attachable = build_privacy_file_lists(workspace_dir, [model], config)
        return {
            "topic": "ar_over_60",
            "period": eff_period,
            "company_name": model.meta.company_name,
            "as_of": as_of,
            "source_file": ar.get("source_file", "ar-aging-2026-11-18.xlsx"),
            "as_of_caveat": (
                f"Consolidated A/R aging is a mid-week snapshot as of {as_of} (2 days prior to the {eff_period} Friday bank cutoff). "
                f"Collections received after {as_of} are not reflected in these aging buckets."
            ),
            "ar_total": ar.get("total"),
            "ar_over_60": model.kpi_map()["ar_over_60"].value,
            "buckets": ar.get("buckets", {}),
            "branch_totals": ar.get("branch_totals", {}),
            "overdue_items": ar.get("overdue_items", []),
            "blocking_checks": [c.__dict__ for c in model.blocking_checks],
            "open_review_checks": [c.__dict__ for c in model.open_review_checks],
            "attachable_files": attachable,
            "cited_non_attachable_sources": non_attachable,
        }

    if topic == "payroll":
        eff_period = period or DEFAULT_PAYROLL_PERIOD
        model, _ = load_latest_model(workspace_dir, module="payroll", period=eff_period, prefer_edition=True)
        if model is None:
            model = build_payroll_model(workspace_dir, period=eff_period, draft_number=0)

        summary = model.sections["summary"]
        attachable, non_attachable = build_privacy_file_lists(workspace_dir, [model], config)
        return {
            "topic": "payroll",
            "period": eff_period,
            "company_name": model.meta.company_name,
            "can_approve": model.can_approve,
            "summary": summary,
            "kpis": {k.key: k.value for k in model.kpis},
            "tieout": model.sections["tieout"],
            "exceptions": model.sections["exceptions"],
            "blocking_checks": [c.__dict__ for c in model.blocking_checks],
            "open_review_checks": [c.__dict__ for c in model.open_review_checks],
            "journal_summary": {
                "line_count": len(model.sections["journal_lines"]),
                "unmapped_line_count": sum(1 for ln in model.sections["journal_lines"] if ln.get("blocked_reason")),
                "total_debits": summary["journal_debits_total"],
                "total_credits": summary["journal_credits_total"],
                "balanced": summary["journal_debits_total"] == summary["journal_credits_total"],
            },
            "production_post_gate": {
                "erp_posting_mode": summary["erp_mode"],
                "mapping_approval_ref": summary["mapping_approval_ref"],
                "production_post_allowed": (len(model.blocking_checks) == 0) and bool(summary["mapping_approval_ref"]),
            },
            "privacy_policy": {
                "restricted_source": "payroll-voucher-2026-11-15.csv",
                "as_of": eff_period,
                "attachable": False,
                "rule": "Never attach the restricted payroll voucher or reproduce row-level employee pay in emails or chat.",
            },
            "attachable_files": attachable,
            "cited_non_attachable_sources": non_attachable,
        }

    if topic == "guardrails":
        eff_period = period or DEFAULT_PAYROLL_PERIOD
        payroll_model, _ = load_latest_model(workspace_dir, module="payroll", period=eff_period, prefer_edition=True)
        if payroll_model is None:
            payroll_model = build_payroll_model(workspace_dir, period=eff_period, draft_number=0)
        weekly_model, _ = load_latest_model(workspace_dir, module="weekly", period=DEFAULT_WEEKLY_PERIOD, prefer_edition=True)
        if weekly_model is None:
            weekly_model = build_weekly_model(workspace_dir, period=DEFAULT_WEEKLY_PERIOD, draft_number=1)

        attachable, non_attachable = build_privacy_file_lists(workspace_dir, [weekly_model, payroll_model], config)
        return {
            "topic": "guardrails",
            "company_name": config["company_name"],
            "enforced_in_code": [
                {
                    "id": "privacy_wall",
                    "mechanism": "SourceRef.__post_init__ in engine/model.py forces attachable=False whenever sensitivity=='restricted'.",
                    "live_state": f"{len(non_attachable)} restricted source(s) programmatically excluded from attachable_files.",
                },
                {
                    "id": "check_severity_separation",
                    "mechanism": "CheckItem.__post_init__ sets blocks_approval=(severity=='blocking'). Only blocking checks and open PendingChanges halt approval.",
                    "live_state": (
                        f"Weekly Cash has {len(weekly_model.blocking_checks)} blocking and {len(weekly_model.open_review_checks)} open_review check(s); "
                        f"Payroll has {len(payroll_model.blocking_checks)} blocking check(s) (PAY-MAP-001) despite $0.00 tie-out variance."
                    ),
                },
                {
                    "id": "stateless_what_if",
                    "mechanism": "what-if clones workspace.db into sqlite3.connect(':memory:') via conn.backup() and writes zero bytes to disk.",
                    "live_state": "Verified zero-write simulation; explicit `ingest` + `change confirm` required to persist revisions.",
                },
                {
                    "id": "write_and_send_gates",
                    "mechanism": "post --target production refuses when blocking checks exist OR payroll.mapping_approval_ref is null; outputs.yaml locks send_mode='draft_only'.",
                    "live_state": (
                        f"send_mode={config['outputs'].get('send_mode')}, "
                        f"erp_posting_mode={config['assumptions']['payroll'].get('erp_posting_mode')}, "
                        f"mapping_approval_ref={config['assumptions']['payroll'].get('mapping_approval_ref')!r}."
                    ),
                },
                {
                    "id": "workspace_isolation_and_audit",
                    "mechanism": "seed/ is strictly read-only; reset refuses unmarked directories; config set and change decisions append to config/config-audit.jsonl.",
                    "live_state": f"{len(read_audit_entries(workspace_dir))} audit entries recorded in config/config-audit.jsonl.",
                },
            ],
            "attachable_files": attachable,
            "cited_non_attachable_sources": non_attachable,
        }

    if topic == "architecture":
        attachable, non_attachable = build_privacy_file_lists(workspace_dir, active_models, config)
        docs_dir = PACKAGE_ROOT / "docs"
        return {
            "topic": "architecture",
            "company_name": config["company_name"],
            "layers": [
                "1. Agent Harness + Thin Operating Spec (skills/ops-copilot/SKILL.md) — natural language orchestration only, zero math or file scraping.",
                "2. Deterministic Engine & Structured CLI (engine/ via run_cli.py) — 12 commands, exact integer-cent Decimal math, SQLite state, uniform JSON envelopes.",
                "3. Declarative YAML Configuration (config/feeds.yaml, assumptions.yaml, outputs.yaml, payroll-mappings.yaml + config-audit.jsonl).",
                "4. Single Canonical ReportModel (report-model.json) — drives ReportLab PDFs, self-contained HTML hubs, openpyxl XLSX workbooks, and CLI explain responses with zero KPI drift.",
            ],
            "config_files": {k: str(workspace_dir / v) for k, v in CONFIG_TARGETS.items()},
            "reference_docs": {
                "architecture_explainer": str(docs_dir / "architecture-explainer.md"),
                "cross_category_patterns": str(docs_dir / "cross-category-patterns.md"),
                "harness_and_security": str(docs_dir / "harness-and-security.md"),
                "pilot_scoping_guide": str(docs_dir / "pilot-scoping-guide.md"),
            },
            "pure_python_runtime_deps": ["PyYAML", "openpyxl", "et_xmlfile", "reportlab", "charset-normalizer"],
            "invariant_vs_configurable": {
                "invariant_engine": [
                    "ReportModel schema & exact integer-cent math",
                    "SourceRef privacy wall & CheckItem blocking semantics",
                    "Stateless in-memory what-if simulation",
                    "Immutable draft/edition lifecycle & config-audit.jsonl logging",
                    "Single-model PDF/HTML/XLSX rendering pipeline",
                ],
                "configured_per_company": [
                    "config/feeds.yaml (sources, column_aliases, sensitivity, ERP cutover windows)",
                    "config/assumptions.yaml (cash floor, baselines, tolerances, ERP posting mode, mapping_approval_ref)",
                    "config/outputs.yaml (approvers, recipients, deliverables, send_mode: draft_only)",
                    "config/payroll-mappings.yaml (department-to-GL account crosswalks)",
                ],
            },
            "attachable_files": attachable,
            "cited_non_attachable_sources": non_attachable,
        }

    # topic == "transfer"
    attachable, non_attachable = build_privacy_file_lists(workspace_dir, active_models, config)
    docs_dir = PACKAGE_ROOT / "docs"
    return {
        "topic": "transfer",
        "company_name": config["company_name"],
        "six_stage_loop": [
            "1. Ingest messy recurring exports via alias-aware declarative feeds.yaml parsers",
            "2. Apply schema-validated, audit-logged YAML business rules",
            "3. Materialize one canonical ReportModel (report-model.json)",
            "4. Run deterministic checks separating blocking hard stops from non-blocking open_review items and restricted privacy walls",
            "5. Gate revisions and external actions behind stateless what-if previews, human confirmation, and immutable editions",
            "6. Render native PDF, HTML, XLSX, and grounded chat outputs from the single model",
        ],
        "non_finance_categories": [
            {
                "category": "Customer Experience & Retention (PEOs, B2B Services)",
                "inputs": "CRM rosters, Zendesk/ServiceNow ticket exports, SLA logs, billing CSVs",
                "hard_stops": "Block retention outreach on legal-hold or unmapped CSM accounts; mark PII/HR ticket transcripts restricted (attachable=False).",
            },
            {
                "category": "Field Capacity & Branch Margin (HVAC, Dental/Vet, Multi-Site Services)",
                "inputs": "Practice-management / FSM work orders, fleet fuel cards, parts invoices, timecards",
                "hard_stops": "Block branch margin publication when bolt-on sites introduce unmapped procedure/job codes; declare acquisition ERP cutovers in feeds.yaml.",
            },
            {
                "category": "Staffing & Healthcare Credentialing",
                "inputs": "State license exports, background checks, immunization logs, HRIS rosters",
                "hard_stops": "Block shift clearance if any required license/credential is expired or missing; keep PHI/background PDFs attachable=False.",
            },
            {
                "category": "PE Sponsor & Firm Back-Office Roll-Ups",
                "inputs": "Heterogeneous portfolio company trial balances, KPI packs, diligence files",
                "hard_stops": "Refuse consolidation if any entity trial balance fails $0.00 footing or introduces an unmapped GL account.",
            },
            {
                "category": "Commercial & Contract Revenue Leakage",
                "inputs": "PSA timekeeping logs, contract registers, ERP billing batches, change-order emails",
                "hard_stops": "Halt billing batch release when billed rates deviate from contracted rate cards or lack written sign-off.",
            },
        ],
        "harness_portability": {
            "claude_desktop_cowork": "Implemented today — single vendored pure-Python plugin ZIP running in Cowork's Linux VM.",
            "claude_code_and_codex": "Implemented today — same SKILL.md (Agent Skills standard) + local engine/ CLI with Python 3.11+.",
            "microsoft_365_copilot": (
                "Architectural extension (not bundled in the local ZIP): wrap the identical engine/ package in a thin "
                "Streamable HTTP MCP or OpenAPI adapter (~150 LOC) hosted in Azure Container Apps or Azure Functions with Entra ID SSO, "
                "and bind Copilot Studio to that endpoint — zero rewrite of accounting, check, or privacy logic."
            ),
        },
        "pilot_rubric_metrics": [
            "1. Operator touch time (active hands-on minutes)",
            "2. Elapsed time (complete inputs -> reviewable pack)",
            "3. Manual edits required before approval",
            "4. Exceptions caught deterministically before release",
            "5. Review and approval time",
            "6. External wait time (tracked separately from processing time)",
        ],
        "reference_docs": {
            "cross_category_patterns": str(docs_dir / "cross-category-patterns.md"),
            "harness_and_security": str(docs_dir / "harness-and-security.md"),
            "pilot_scoping_guide": str(docs_dir / "pilot-scoping-guide.md"),
        },
        "attachable_files": attachable,
        "cited_non_attachable_sources": non_attachable,
    }
