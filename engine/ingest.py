"""Alias-aware CSV/XLSX/YAML feed parsers and incoming amendment intake.

All parsing behavior (`skip_rows`, `header_row`, `skip_subtotal_labels`,
`strip_currency_symbols`, `column_aliases`) is driven declaratively by
`config/feeds.yaml`. Invalid schemas raise `EngineError` before mutating any
workspace file or SQLite table.
"""

from __future__ import annotations

import csv
import datetime as dt
import fnmatch
import json
import re
import shutil
from pathlib import Path
from typing import Any, Sequence

import yaml
from openpyxl import load_workbook

from engine.config import (
    EngineError,
    cents_to_float,
    load_config,
    to_cents,
    utc_now_iso,
)
from engine.workspace import connect_db, sha256_file

# ---------------------------------------------------------------------------
# Header alias matching
# ---------------------------------------------------------------------------


def _norm_header(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "").strip()).lower()


def resolve_header_map(
    headers: Sequence[str],
    column_aliases: dict[str, list[str]],
    *,
    required_fields: Sequence[str] = (),
    source_name: str = "input",
) -> dict[str, str]:
    """Map canonical field names to actual file column headers using `column_aliases`."""
    norm_to_actual: dict[str, str] = {}
    for h in headers:
        nh = _norm_header(h)
        if nh and nh not in norm_to_actual:
            norm_to_actual[nh] = str(h)

    resolved: dict[str, str] = {}
    for canonical, aliases in column_aliases.items():
        candidates = [canonical, *(aliases if isinstance(aliases, list) else [aliases])]
        for cand in candidates:
            nc = _norm_header(str(cand))
            if nc in norm_to_actual:
                resolved[canonical] = norm_to_actual[nc]
                break

    missing = [f for f in required_fields if f not in resolved]
    if missing:
        raise EngineError(
            "INVALID_FEED_SCHEMA",
            f"Missing required column(s) in {source_name}: {', '.join(missing)}.",
            "Check `config/feeds.yaml` column_aliases or provide an export containing the required columns.",
            details={"missing_fields": missing, "observed_headers": list(headers)},
        )
    return resolved


def get_feed_config(config: dict[str, Any], feed_id: str) -> dict[str, Any]:
    """Retrieve one feed specification from loaded config by `id`."""
    feeds = (config.get("feeds") or {}).get("feeds") or []
    for f in feeds:
        if f.get("id") == feed_id:
            return f
    raise EngineError(
        "UNKNOWN_FEED_ID",
        f"Feed {feed_id!r} is not configured in `config/feeds.yaml`.",
        "Inspect configured feeds via `config show --target feeds`.",
    )


def resolve_feed_file(workspace_dir: Path, config: dict[str, Any], feed_id: str, period: str | None = None) -> Path:
    """Resolve the active file path for a feed (preferring latest ingested input for period, else default_file)."""
    workspace_dir = Path(workspace_dir)
    feed = get_feed_config(config, feed_id)

    # Check ingested_inputs table first (if workspace.db exists)
    db_path = workspace_dir / "workspace.db"
    if db_path.is_file():
        conn = connect_db(workspace_dir, readonly=True)
        try:
            if period:
                row = conn.execute(
                    """
                    SELECT relative_path FROM ingested_inputs
                    WHERE feed_id = ? AND period = ?
                    ORDER BY ingested_at DESC, id DESC LIMIT 1
                    """,
                    (feed_id, period),
                ).fetchone()
                if row:
                    cand = workspace_dir / row["relative_path"]
                    if cand.is_file():
                        return cand
            row = conn.execute(
                """
                SELECT relative_path FROM ingested_inputs
                WHERE feed_id = ?
                ORDER BY ingested_at DESC, id DESC LIMIT 1
                """,
                (feed_id,),
            ).fetchone()
            if row:
                cand = workspace_dir / row["relative_path"]
                if cand.is_file():
                    return cand
        finally:
            conn.close()

    default_rel = feed.get("default_file")
    if default_rel:
        cand = workspace_dir / default_rel
        if cand.is_file():
            return cand

    raise EngineError(
        "FEED_FILE_MISSING",
        f"Source file for feed `{feed_id}` is missing from the workspace.",
        f"Place the source export in `sources/` and run `ingest --file <path>`.",
        details={"feed_id": feed_id, "expected_pattern": feed.get("file_pattern")},
    )


# ---------------------------------------------------------------------------
# Feed-specific parsers
# ---------------------------------------------------------------------------


def parse_bank_csv(path: Path, feed_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse a Frost bank activity CSV honoring `parser.skip_rows` and `column_aliases`."""
    parser_cfg = feed_cfg.get("parser") or {}
    skip_rows = int(parser_cfg.get("skip_rows", 0))
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) <= skip_rows + 1:
        raise EngineError(
            "INVALID_FEED_SCHEMA",
            f"Bank CSV {path.name} has insufficient rows after skipping {skip_rows} preamble line(s).",
            "Provide a valid bank export with header and transaction rows.",
        )

    reader = csv.DictReader(lines[skip_rows:])
    if not reader.fieldnames:
        raise EngineError(
            "INVALID_FEED_SCHEMA",
            f"Bank CSV {path.name} has no header row.",
            "Verify `parser.skip_rows` in `config/feeds.yaml`.",
        )

    col_map = resolve_header_map(
        reader.fieldnames,
        feed_cfg.get("column_aliases") or {},
        required_fields=("post_date", "reference", "description", "debit", "credit", "running_balance"),
        source_name=path.name,
    )

    rows: list[dict[str, Any]] = []
    for idx, raw in enumerate(reader, start=skip_rows + 2):
        post_date = str(raw.get(col_map["post_date"]) or "").strip()
        if not post_date:
            continue
        try:
            dt.date.fromisoformat(post_date)
        except ValueError as exc:
            raise EngineError(
                "INVALID_FEED_SCHEMA",
                f"Invalid ISO date {post_date!r} at row {idx} of {path.name}.",
                "Ensure bank export dates use YYYY-MM-DD format.",
            ) from exc

        debit_c = to_cents(raw.get(col_map["debit"]))
        credit_c = to_cents(raw.get(col_map["credit"]))
        bal_c = to_cents(raw.get(col_map["running_balance"]))
        rows.append(
            {
                "post_date": post_date,
                "reference": str(raw.get(col_map["reference"]) or "").strip(),
                "description": str(raw.get(col_map["description"]) or "").strip(),
                "debit_cents": debit_c,
                "credit_cents": credit_c,
                "running_bal_cents": bal_c,
            }
        )

    if not rows:
        raise EngineError(
            "INVALID_FEED_SCHEMA",
            f"Bank CSV {path.name} contained zero transaction rows.",
            "Provide a bank activity export with at least one transaction.",
        )
    return rows


def parse_ar_aging_xlsx(path: Path, feed_cfg: dict[str, Any]) -> dict[str, Any]:
    """Parse consolidated commercial A/R aging XLSX with banner skip, currency stripping, and subtotal filtering."""
    parser_cfg = feed_cfg.get("parser") or {}
    header_row = int(parser_cfg.get("header_row", 4))
    skip_labels = {str(lbl).strip().lower() for lbl in (parser_cfg.get("skip_subtotal_labels") or ["total", "subtotal", "grand total"])}

    try:
        wb = load_workbook(path, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise EngineError(
            "INVALID_FEED_SCHEMA",
            f"Could not open XLSX file {path.name}: {exc}",
            "Provide a valid `.xlsx` workbook matching `config/feeds.yaml`.",
        ) from exc

    ws = wb.active
    if ws is None or ws.max_row < header_row + 1:
        raise EngineError(
            "INVALID_FEED_SCHEMA",
            f"A/R aging workbook {path.name} lacks data rows below header row {header_row}.",
            "Verify `parser.header_row` in `config/feeds.yaml`.",
        )

    # Extract as_of date from banner rows (1..header_row-1), falling back to filename date
    as_of: str | None = None
    for r_idx in range(1, header_row):
        for c_idx in range(1, ws.max_column + 1):
            val = ws.cell(row=r_idx, column=c_idx).value
            if val:
                m = re.search(r"as\s+of\s*:?\s*(\d{4}-\d{2}-\d{2})", str(val), flags=re.IGNORECASE)
                if m:
                    as_of = m.group(1)
                    break
        if as_of:
            break
    if not as_of:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
        as_of = m.group(1) if m else ""

    raw_headers = [
        str(ws.cell(row=header_row, column=c).value or "").strip()
        for c in range(1, ws.max_column + 1)
    ]
    col_map = resolve_header_map(
        raw_headers,
        feed_cfg.get("column_aliases") or {},
        required_fields=(
            "invoice_id",
            "customer",
            "branch",
            "invoice_date",
            "due_date",
            "days_past_due",
            "current",
            "d1_30",
            "d31_60",
            "d61_90",
            "over_90",
            "total",
        ),
        source_name=path.name,
    )
    header_idx = {h: idx for idx, h in enumerate(raw_headers)}

    def _cell(vals: list[Any], canonical: str) -> Any:
        actual = col_map.get(canonical)
        if actual is None:
            return None
        return vals[header_idx[actual]]

    buckets_cents = {"current": 0, "d1_30": 0, "d31_60": 0, "d61_90": 0, "over_90": 0}
    branch_totals_cents: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    overdue_items: list[dict[str, Any]] = []

    for r_idx in range(header_row + 1, ws.max_row + 1):
        vals = [ws.cell(row=r_idx, column=c).value for c in range(1, ws.max_column + 1)]
        if all(v is None or str(v).strip() == "" for v in vals):
            continue
        inv_id = str(_cell(vals, "invoice_id") or "").strip()
        first_cell = str(vals[0] or "").strip().lower()
        if not inv_id or inv_id.lower() in skip_labels or first_cell in skip_labels:
            continue

        customer = str(_cell(vals, "customer") or "").strip()
        branch = str(_cell(vals, "branch") or "").strip()
        invoice_date = str(_cell(vals, "invoice_date") or "").strip()
        due_date = str(_cell(vals, "due_date") or "").strip()
        dpd = int(float(_cell(vals, "days_past_due") or 0))

        c_cur = to_cents(_cell(vals, "current"))
        c_1_30 = to_cents(_cell(vals, "d1_30"))
        c_31_60 = to_cents(_cell(vals, "d31_60"))
        c_61_90 = to_cents(_cell(vals, "d61_90"))
        c_over_90 = to_cents(_cell(vals, "over_90"))
        c_tot = to_cents(_cell(vals, "total"))
        note = str(_cell(vals, "note") or "").strip() if "note" in col_map else ""

        if c_cur + c_1_30 + c_31_60 + c_61_90 + c_over_90 != c_tot:
            raise EngineError(
                "AR_ROW_FOOTING_MISMATCH",
                f"A/R aging row {inv_id} buckets do not sum to Total Open in {path.name}.",
                "Verify the A/R aging export balances before ingesting.",
                details={"invoice_id": inv_id},
            )

        buckets_cents["current"] += c_cur
        buckets_cents["d1_30"] += c_1_30
        buckets_cents["d31_60"] += c_31_60
        buckets_cents["d61_90"] += c_61_90
        buckets_cents["over_90"] += c_over_90
        branch_totals_cents[branch] = branch_totals_cents.get(branch, 0) + c_tot

        row_item = {
            "invoice_id": inv_id,
            "customer": customer,
            "branch": branch,
            "invoice_date": invoice_date,
            "due_date": due_date,
            "days_past_due": dpd,
            "amount_cents": c_tot,
            "note": note,
        }
        rows.append(row_item)

        if c_61_90 > 0 or c_over_90 > 0:
            overdue_items.append(
                {
                    "invoice_id": inv_id,
                    "customer": customer,
                    "branch": branch,
                    "invoice_date": invoice_date,
                    "due_date": due_date,
                    "days_past_due": dpd,
                    "bucket": ">90" if c_over_90 > 0 else "61-90",
                    "amount": cents_to_float(c_tot),
                    "note": note,
                }
            )

    if not rows:
        raise EngineError(
            "INVALID_FEED_SCHEMA",
            f"A/R aging workbook {path.name} contained zero open invoice rows.",
            "Provide an A/R aging export containing invoice detail rows.",
        )

    overdue_items.sort(key=lambda item: (-int(item["days_past_due"]), str(item["invoice_id"])))
    total_cents = sum(buckets_cents.values())
    ar_over_60_cents = buckets_cents["d61_90"] + buckets_cents["over_90"]

    return {
        "as_of": as_of,
        "source_file": path.name,
        "account_count": len(rows),
        "total_cents": total_cents,
        "ar_over_60_cents": ar_over_60_cents,
        "buckets_cents": buckets_cents,
        "branch_totals_cents": branch_totals_cents,
        "overdue_items": overdue_items,
        "rows": rows,
    }


def parse_ap_scheduled_csv(path: Path, feed_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse AP scheduled vendor disbursements CSV using `column_aliases`."""
    parser_cfg = feed_cfg.get("parser") or {}
    skip_rows = int(parser_cfg.get("skip_rows", 0))
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) <= skip_rows + 1:
        raise EngineError(
            "INVALID_FEED_SCHEMA",
            f"AP schedule CSV {path.name} has no data rows.",
            "Provide a valid AP scheduled payments CSV.",
        )

    reader = csv.DictReader(lines[skip_rows:])
    if not reader.fieldnames:
        raise EngineError("INVALID_FEED_SCHEMA", f"AP schedule CSV {path.name} has no headers.", "Verify CSV headers.")

    col_map = resolve_header_map(
        reader.fieldnames,
        feed_cfg.get("column_aliases") or {},
        required_fields=("invoice_ref", "vendor", "branch", "due_date", "forecast_week", "amount", "paid"),
        source_name=path.name,
    )

    rows: list[dict[str, Any]] = []
    for raw in reader:
        inv_ref = str(raw.get(col_map["invoice_ref"]) or "").strip()
        if not inv_ref:
            continue
        paid_raw = str(raw.get(col_map["paid"]) or "false").strip().lower()
        paid = paid_raw in {"true", "1", "yes", "y"}
        try:
            f_week = int(str(raw.get(col_map["forecast_week"]) or "0").strip())
        except ValueError as exc:
            raise EngineError(
                "INVALID_FEED_SCHEMA",
                f"Invalid forecast_week value for invoice {inv_ref} in {path.name}.",
                "Ensure `forecast_week` contains integer week numbers.",
            ) from exc

        rows.append(
            {
                "invoice_ref": inv_ref,
                "vendor": str(raw.get(col_map["vendor"]) or "").strip(),
                "branch": str(raw.get(col_map["branch"]) or "").strip(),
                "due_date": str(raw.get(col_map["due_date"]) or "").strip(),
                "forecast_week": f_week,
                "amount_cents": to_cents(raw.get(col_map["amount"])),
                "paid": paid,
                "status": str(raw.get(col_map["status"]) or ("paid" if paid else "scheduled")).strip()
                if "status" in col_map
                else ("paid" if paid else "scheduled"),
                "note": str(raw.get(col_map["note"]) or "").strip() if "note" in col_map else "",
            }
        )

    if not rows:
        raise EngineError("INVALID_FEED_SCHEMA", f"AP schedule CSV {path.name} has zero rows.", "Check AP CSV content.")
    return rows


def parse_payroll_voucher_csv(path: Path, feed_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse restricted biweekly PEO payroll register CSV in exact integer cents."""
    parser_cfg = feed_cfg.get("parser") or {}
    skip_rows = int(parser_cfg.get("skip_rows", 0))
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) <= skip_rows + 1:
        raise EngineError(
            "INVALID_FEED_SCHEMA",
            f"Payroll voucher CSV {path.name} has no employee rows.",
            "Provide a valid payroll voucher CSV.",
        )

    reader = csv.DictReader(lines[skip_rows:])
    if not reader.fieldnames:
        raise EngineError("INVALID_FEED_SCHEMA", f"Payroll voucher CSV {path.name} has no headers.", "Verify headers.")

    col_map = resolve_header_map(
        reader.fieldnames,
        feed_cfg.get("column_aliases") or {},
        required_fields=(
            "employee_id",
            "employee_name",
            "branch",
            "dept_code",
            "regular_pay",
            "overtime_pay",
            "gross_pay",
            "employer_fica",
            "employer_futa_suta",
            "employer_taxes",
            "employer_401k_match",
            "employer_medical",
            "employer_benefits",
            "employee_withholdings",
            "net_pay",
        ),
        source_name=path.name,
    )

    rows: list[dict[str, Any]] = []
    for raw in reader:
        emp_id = str(raw.get(col_map["employee_id"]) or "").strip()
        if not emp_id:
            continue
        rows.append(
            {
                "employee_id": emp_id,
                "employee_name": str(raw.get(col_map["employee_name"]) or "").strip(),
                "branch": str(raw.get(col_map["branch"]) or "").strip(),
                "dept_code": str(raw.get(col_map["dept_code"]) or "").strip(),
                "job_title": str(raw.get(col_map["job_title"]) or "").strip() if "job_title" in col_map else "",
                "pay_period_start": str(raw.get("pay_period_start") or "").strip(),
                "pay_period_end": str(raw.get("pay_period_end") or "").strip(),
                "pay_date": str(raw.get("pay_date") or "").strip(),
                "regular_pay_cents": to_cents(raw.get(col_map["regular_pay"])),
                "overtime_pay_cents": to_cents(raw.get(col_map["overtime_pay"])),
                "gross_pay_cents": to_cents(raw.get(col_map["gross_pay"])),
                "employer_fica_cents": to_cents(raw.get(col_map["employer_fica"])),
                "employer_futa_suta_cents": to_cents(raw.get(col_map["employer_futa_suta"])),
                "employer_taxes_cents": to_cents(raw.get(col_map["employer_taxes"])),
                "employer_401k_match_cents": to_cents(raw.get(col_map["employer_401k_match"])),
                "employer_medical_cents": to_cents(raw.get(col_map["employer_medical"])),
                "employer_benefits_cents": to_cents(raw.get(col_map["employer_benefits"])),
                "employee_withholdings_cents": to_cents(raw.get(col_map["employee_withholdings"])),
                "net_pay_cents": to_cents(raw.get(col_map["net_pay"])),
            }
        )

    if not rows:
        raise EngineError("INVALID_FEED_SCHEMA", f"Payroll voucher {path.name} has zero rows.", "Check CSV contents.")
    return rows


def parse_payroll_funding_wire_yaml(path: Path) -> dict[str, Any]:
    """Parse and validate a payroll funding wire confirmation YAML."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise EngineError(
            "INVALID_FEED_SCHEMA",
            f"YAML parse error in {path.name}: {exc}",
            "Provide a valid payroll funding wire YAML file.",
        ) from exc

    if not isinstance(data, dict) or not isinstance(data.get("components"), dict):
        raise EngineError(
            "INVALID_FEED_SCHEMA",
            f"Funding wire {path.name} must contain a `components` mapping.",
            "Verify the funding wire YAML schema.",
        )
    required_comp = (
        "gross_wages",
        "employer_taxes",
        "employer_benefits",
        "employee_withholdings",
        "net_pay",
        "total_funding_wire",
    )
    missing = [k for k in required_comp if k not in data["components"]]
    if missing:
        raise EngineError(
            "INVALID_FEED_SCHEMA",
            f"Funding wire {path.name} is missing component(s): {', '.join(missing)}.",
            "Include all required payroll funding wire components.",
        )
    return data


def parse_amendment_yaml(path: Path) -> dict[str, Any]:
    """Parse and validate a structured payment amendment YAML file."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise EngineError(
            "INVALID_AMENDMENT_SCHEMA",
            f"YAML syntax error in amendment {path.name}: {exc}",
            "Fix the YAML syntax in the amendment file and retry `ingest`.",
        ) from exc

    if not isinstance(data, dict):
        raise EngineError(
            "INVALID_AMENDMENT_SCHEMA",
            f"Amendment file {path.name} must be a YAML mapping.",
            "Provide a structured YAML payment amendment.",
        )

    required = (
        "change_id",
        "period",
        "module",
        "kind",
        "invoice_ref",
        "vendor",
        "amount",
        "old_due_date",
        "new_due_date",
        "old_week",
        "new_week",
        "summary",
    )
    missing = [k for k in required if k not in data or data[k] in (None, "")]
    if missing:
        raise EngineError(
            "INVALID_AMENDMENT_SCHEMA",
            f"Amendment {path.name} is missing required field(s): {', '.join(missing)}.",
            "Provide all required amendment fields before ingesting.",
            details={"missing_fields": missing},
        )
    if data["module"] not in ("weekly", "payroll"):
        raise EngineError(
            "INVALID_AMENDMENT_SCHEMA",
            f"Amendment {path.name} has invalid module {data['module']!r}.",
            "Use `weekly` or `payroll`.",
        )
    old_w = int(data["old_week"])
    new_w = int(data["new_week"])
    if not (1 <= old_w <= 13 and 1 <= new_w <= 13):
        raise EngineError(
            "INVALID_AMENDMENT_SCHEMA",
            f"Amendment weeks must be between 1 and 13 (got old_week={old_w}, new_week={new_w}).",
            "Specify valid forecast week numbers (1..13).",
        )
    to_cents(data["amount"])  # Validate numeric amount
    return data


# ---------------------------------------------------------------------------
# Unified `ingest` CLI handler
# ---------------------------------------------------------------------------


def _resolve_input_path(workspace_dir: Path, raw_file: str | None) -> Path:
    workspace_dir = Path(workspace_dir)
    if raw_file is None or not str(raw_file).strip():
        incoming_dir = workspace_dir / "incoming"
        yamls = sorted(incoming_dir.glob("*.yaml")) if incoming_dir.is_dir() else []
        if len(yamls) == 1:
            return yamls[0]
        raise EngineError(
            "INGEST_FILE_REQUIRED",
            "No `--file` path was provided and could not unambiguously auto-select an incoming file.",
            "Pass `--file <path>` (e.g. `incoming/lone-star-payment-amendment-2026-11-20.yaml`).",
            details={"available_incoming": [p.name for p in yamls]},
        )

    p = Path(raw_file).expanduser()
    candidates = [
        p,
        workspace_dir / p,
        workspace_dir / "incoming" / p.name,
        workspace_dir / "sources" / p.name,
    ]
    for cand in candidates:
        if cand.is_file():
            return cand.resolve()

    raise EngineError(
        "INGEST_FILE_NOT_FOUND",
        f"Input file not found: {raw_file}.",
        "Check the file path or place the file inside the workspace `incoming/` or `sources/` folder.",
        details={"requested_file": raw_file},
    )


def _detect_feed_id(path: Path, config: dict[str, Any], explicit_feed_id: str | None) -> str | None:
    if explicit_feed_id:
        get_feed_config(config, explicit_feed_id)  # Validate exists
        return explicit_feed_id

    name = path.name
    feeds = (config.get("feeds") or {}).get("feeds") or []
    for feed in feeds:
        pattern = str(feed.get("file_pattern") or "")
        base_pat = Path(pattern).name if pattern else ""
        if base_pat and fnmatch.fnmatch(name, base_pat):
            # Prefer canonical `ar_aging` over `tulsa_qb_ar` when both match `ar-aging-*.xlsx`
            if feed["id"] == "tulsa_qb_ar":
                continue
            return str(feed["id"])
    return None


def _is_amendment_yaml(path: Path) -> bool:
    if path.suffix.lower() not in {".yaml", ".yml"}:
        return False
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return False
    return isinstance(loaded, dict) and ("change_id" in loaded or loaded.get("kind") == "payment_deferral")


def ingest_file(
    workspace_dir: Path,
    *,
    period: str,
    file_path: str | None = None,
    feed_id: str | None = None,
) -> dict[str, Any]:
    """Validate and ingest a feed file or structured payment amendment into workspace state.

    Never mutates workspace files or SQLite state when schema validation fails.
    """
    workspace_dir = Path(workspace_dir).resolve()
    config = load_config(workspace_dir)
    resolved = _resolve_input_path(workspace_dir, file_path)

    # Branch 1: Structured payment amendment YAML -> queue as PendingChange
    if feed_id is None and _is_amendment_yaml(resolved):
        amend = parse_amendment_yaml(resolved)
        # Verify invoice_ref exists in scheduled AP before mutating workspace
        ap_cfg = get_feed_config(config, "ap_scheduled")
        ap_path = resolve_feed_file(workspace_dir, config, "ap_scheduled", period=period)
        ap_rows = parse_ap_scheduled_csv(ap_path, ap_cfg)
        matching = [r for r in ap_rows if r["invoice_ref"] == amend["invoice_ref"] and not r["paid"]]
        if not matching:
            raise EngineError(
                "INVOICE_REF_NOT_FOUND",
                f"Invoice {amend['invoice_ref']!r} not found in unpaid scheduled AP payments.",
                "Verify the invoice reference against `sources/ap-scheduled-payments-*.csv`.",
                details={"invoice_ref": amend["invoice_ref"]},
            )

        incoming_dst = workspace_dir / "incoming" / resolved.name
        if resolved != incoming_dst.resolve():
            incoming_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(resolved, incoming_dst)

        now = utc_now_iso()
        change_id = str(amend["change_id"])
        amend_period = str(amend.get("period") or period)
        payload = {
            "invoice_ref": str(amend["invoice_ref"]),
            "vendor": str(amend["vendor"]),
            "branch": str(amend.get("branch") or matching[0]["branch"]),
            "amount": cents_to_float(to_cents(amend["amount"])),
            "old_due_date": str(amend["old_due_date"]),
            "new_due_date": str(amend["new_due_date"]),
            "old_week": int(amend["old_week"]),
            "new_week": int(amend["new_week"]),
            "requested_by": str(amend.get("requested_by") or ""),
            "vendor_contact": str(amend.get("vendor_contact") or ""),
            "companion_memo": str(amend.get("companion_memo") or ""),
        }

        conn = connect_db(workspace_dir, readonly=False)
        try:
            existing = conn.execute("SELECT status FROM pending_changes WHERE id = ?", (change_id,)).fetchone()
            if existing and existing["status"] == "confirmed":
                raise EngineError(
                    "PENDING_CHANGE_ALREADY_CONFIRMED",
                    f"Change {change_id} has already been confirmed and applied.",
                    "Run `prepare --module weekly` to regenerate drafts or `reset` to replay the demo.",
                    details={"change_id": change_id, "status": "confirmed"},
                )

            conn.execute(
                """
                INSERT OR REPLACE INTO pending_changes(
                    id, period, module, kind, summary, source_file,
                    status, needs_decision, rationale, recorded_by,
                    recorded_at, decided_by, decided_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 1, ?, ?, ?, '', '', ?)
                """,
                (
                    change_id,
                    amend_period,
                    str(amend["module"]),
                    str(amend["kind"]),
                    str(amend["summary"]),
                    resolved.name,
                    str(amend.get("rationale") or ""),
                    str(amend.get("requested_by") or "incoming_amendment"),
                    now,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        return {
            "ingested_kind": "pending_change",
            "pending_change": {
                "id": change_id,
                "period": amend_period,
                "module": str(amend["module"]),
                "kind": str(amend["kind"]),
                "status": "pending",
                "needs_decision": True,
                "summary": str(amend["summary"]),
                "source_file": resolved.name,
                "rationale": str(amend.get("rationale") or ""),
                "payload": payload,
            },
            "next_step": f"Confirm with `change confirm --id {change_id} --by \"<name>\"` or decline with `change decline --id {change_id} --by \"<name>\"`.",
            "attachable_files": [
                {
                    "file_name": resolved.name,
                    "relative_path": f"incoming/{resolved.name}",
                    "path": str(incoming_dst),
                    "sensitivity": "internal",
                }
            ],
            "cited_non_attachable_sources": [],
        }

    # Branch 2: Feed source file -> validate parser first, then copy & record
    detected_id = _detect_feed_id(resolved, config, feed_id)
    if not detected_id:
        raise EngineError(
            "UNRECOGNIZED_INPUT_FILE",
            f"Could not match {resolved.name} to a configured feed in `config/feeds.yaml` or a structured amendment.",
            "Pass `--feed <feed_id>` explicitly or check `file_pattern` rules in `config/feeds.yaml`.",
            details={"file_name": resolved.name},
        )

    feed_cfg = get_feed_config(config, detected_id)
    fmt = str(feed_cfg.get("format") or "").lower()
    row_count = 0
    as_of: str | None = None

    if detected_id in ("frost_operating", "frost_payroll_sweep"):
        parsed_rows = parse_bank_csv(resolved, feed_cfg)
        row_count = len(parsed_rows)
        as_of = max(r["post_date"] for r in parsed_rows)
    elif detected_id in ("ar_aging", "tulsa_qb_ar", "unified_erp_ar"):
        parsed_ar = parse_ar_aging_xlsx(resolved, feed_cfg)
        row_count = parsed_ar["account_count"]
        as_of = parsed_ar["as_of"]
    elif detected_id == "ap_scheduled":
        parsed_ap = parse_ap_scheduled_csv(resolved, feed_cfg)
        row_count = len(parsed_ap)
        as_of = period
    elif detected_id == "payroll_voucher":
        parsed_pv = parse_payroll_voucher_csv(resolved, feed_cfg)
        row_count = len(parsed_pv)
        as_of = parsed_pv[0].get("pay_date") or period
    elif detected_id == "payroll_funding_wire":
        parsed_wire = parse_payroll_funding_wire_yaml(resolved)
        row_count = int(parsed_wire.get("employee_count") or 0)
        as_of = str(parsed_wire.get("pay_date") or period)
    else:
        raise EngineError(
            "UNSUPPORTED_FEED_PARSER",
            f"No parser registered for feed {detected_id!r} (format={fmt!r}).",
            "Use one of the configured weekly or payroll feeds.",
        )

    # Validation succeeded — now safely copy into workspace `sources/` & record in SQLite
    dst = workspace_dir / "sources" / resolved.name
    if resolved != dst.resolve():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(resolved, dst)

    sensitivity = str(feed_cfg.get("sensitivity") or "internal")
    attachable = False if sensitivity == "restricted" else bool(feed_cfg.get("attachable", True))
    rel_path = f"sources/{resolved.name}"
    now = utc_now_iso()

    conn = connect_db(workspace_dir, readonly=False)
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO ingested_inputs(
                period, feed_id, file_name, relative_path, as_of,
                sensitivity, attachable, row_count, sha256, ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                period,
                detected_id,
                resolved.name,
                rel_path,
                as_of,
                sensitivity,
                1 if attachable else 0,
                row_count,
                sha256_file(dst),
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    attachable_files = (
        [
            {
                "file_name": resolved.name,
                "relative_path": rel_path,
                "path": str(dst),
                "sensitivity": sensitivity,
            }
        ]
        if attachable
        else []
    )
    cited_non_attachable = (
        [
            {
                "feed_id": detected_id,
                "file_name": resolved.name,
                "as_of": as_of,
                "sensitivity": sensitivity,
                "attachable": False,
                "citation_rule": "Cite by filename and date only; never attach or reproduce row-level employee pay.",
            }
        ]
        if not attachable
        else []
    )

    return {
        "ingested_kind": "feed",
        "feed_id": detected_id,
        "period": period,
        "file_name": resolved.name,
        "relative_path": rel_path,
        "as_of": as_of,
        "row_count": row_count,
        "sensitivity": sensitivity,
        "attachable": attachable,
        "attachable_files": attachable_files,
        "cited_non_attachable_sources": cited_non_attachable,
    }
