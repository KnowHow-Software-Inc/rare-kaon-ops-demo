"""Declarative YAML configuration loader, schema validator, and audit logger.

Owns:
- Exact integer-cent / Decimal arithmetic helpers shared across the engine
- Structured `EngineError` exception type for uniform CLI error envelopes
- Loading and schema-validating `config/feeds.yaml`, `config/assumptions.yaml`,
  `config/outputs.yaml`, and `config/payroll-mappings.yaml`
- `config show` and `config set` with append-only `config/config-audit.jsonl` logging
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import yaml

from engine.contracts import COMPANY_NAME, SCHEMA_VERSION

# ---------------------------------------------------------------------------
# Structured exception base for clean CLI error envelopes
# ---------------------------------------------------------------------------


class EngineError(Exception):
    """Structured domain error converted into an `error_envelope` by the CLI."""

    def __init__(
        self,
        error_code: str,
        cause: str,
        suggested_action: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(cause)
        self.error_code = error_code
        self.cause = cause
        self.suggested_action = suggested_action
        self.details = details or {}


# ---------------------------------------------------------------------------
# Exact integer-cent / Decimal arithmetic & timestamp helpers
# ---------------------------------------------------------------------------


def to_cents(value: str | int | float | Decimal | None) -> int:
    """Parse a currency/numeric scalar into exact integer cents (ROUND_HALF_UP)."""
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value * 100
    if isinstance(value, Decimal):
        dec = value
    else:
        raw = str(value).strip()
        if not raw or raw in {"-", "—", "N/A", "null", "None"}:
            return 0
        negative = False
        if raw.startswith("(") and raw.endswith(")"):
            negative = True
            raw = raw[1:-1].strip()
        cleaned = raw.replace("$", "").replace(",", "").replace(" ", "")
        if not cleaned:
            return 0
        try:
            dec = Decimal(cleaned)
        except InvalidOperation as exc:
            raise EngineError(
                "INVALID_MONETARY_VALUE",
                f"Cannot parse monetary amount {value!r}.",
                "Verify the source column contains numeric USD amounts.",
            ) from exc
        if negative:
            dec = -dec
    return int((dec * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cents_to_float(cents: int) -> float:
    """Convert integer cents to a 2-decimal float for canonical JSON serialization."""
    return float(Decimal(cents) / Decimal("100"))


def cents_to_currency(cents: int, *, signed: bool = False) -> str:
    """Format integer cents as exact USD currency string (`$1,234.56`)."""
    whole, frac = divmod(abs(cents), 100)
    if cents < 0:
        return f"-${whole:,}.{frac:02d}"
    if signed and cents > 0:
        return f"+${whole:,}.{frac:02d}"
    return f"${whole:,}.{frac:02d}"


def cents_to_str(cents: int) -> str:
    """Format integer cents as plain decimal string (`1234.56`)."""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{whole}.{frac:02d}"


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format (`YYYY-MM-DDTHH:MM:SSZ`)."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# YAML serialization helpers (anchor-free, deterministic key order)
# ---------------------------------------------------------------------------


class _NoAliasSafeDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


def dump_yaml(path: Path, data: dict[str, Any]) -> None:
    """Write anchor-free YAML preserving key insertion order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.dump(
        data,
        Dumper=_NoAliasSafeDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )
    path.write_text(rendered, encoding="utf-8", newline="\n")


def load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML mapping from disk with structured error handling."""
    if not path.is_file():
        raise EngineError(
            "CONFIG_FILE_MISSING",
            f"Configuration file not found: {path.name}.",
            "Ensure the demo workspace is initialized or run `reset` to restore seed configuration.",
            details={"path": str(path)},
        )
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise EngineError(
            "INVALID_YAML",
            f"YAML syntax error in {path.name}: {exc}",
            f"Fix the YAML syntax in {path.name} and retry.",
            details={"path": str(path)},
        ) from exc
    if not isinstance(loaded, dict):
        raise EngineError(
            "INVALID_CONFIG_SCHEMA",
            f"Expected a YAML mapping at top level of {path.name}.",
            f"Ensure {path.name} defines a valid key-value YAML structure.",
            details={"path": str(path)},
        )
    return loaded


# ---------------------------------------------------------------------------
# Config target registry & schema validation
# ---------------------------------------------------------------------------

CONFIG_TARGETS: dict[str, str] = {
    "feeds": "config/feeds.yaml",
    "assumptions": "config/assumptions.yaml",
    "outputs": "config/outputs.yaml",
    "payroll-mappings": "config/payroll-mappings.yaml",
}

AUDIT_LOG_REL = "config/config-audit.jsonl"

VALID_SENSITIVITIES = {"board", "internal", "restricted"}
VALID_MODULES = {"weekly", "payroll"}
VALID_ERP_MODES = {"sandbox", "production"}
VALID_SEND_MODES = {"draft_only"}


def validate_config_target(target: str, data: dict[str, Any]) -> None:
    """Validate one configuration document against strict schema rules."""
    if target not in CONFIG_TARGETS:
        raise EngineError(
            "UNKNOWN_CONFIG_TARGET",
            f"Unknown config target {target!r}.",
            f"Choose one of: {', '.join(CONFIG_TARGETS.keys())}.",
        )
    if not isinstance(data, dict):
        raise EngineError(
            "INVALID_CONFIG_SCHEMA",
            f"Config target {target!r} must be a YAML mapping.",
            "Provide a valid YAML mapping structure.",
        )

    sv = data.get("schema_version")
    if not isinstance(sv, int) or sv < 1:
        raise EngineError(
            "INVALID_CONFIG_SCHEMA",
            f"{target}: `schema_version` must be a positive integer (got {sv!r}).",
            f"Set `schema_version` to {SCHEMA_VERSION}.",
        )

    if target == "feeds":
        _validate_feeds(data)
    elif target == "assumptions":
        _validate_assumptions(data)
    elif target == "outputs":
        _validate_outputs(data)
    elif target == "payroll-mappings":
        _validate_payroll_mappings(data)


def _validate_feeds(data: dict[str, Any]) -> None:
    feeds = data.get("feeds")
    if not isinstance(feeds, list) or not feeds:
        raise EngineError(
            "INVALID_CONFIG_SCHEMA",
            "feeds.yaml: `feeds` must be a non-empty list.",
            "Declare at least one source feed entry under `feeds`.",
        )
    seen_ids: set[str] = set()
    for idx, feed in enumerate(feeds):
        if not isinstance(feed, dict):
            raise EngineError(
                "INVALID_CONFIG_SCHEMA",
                f"feeds.yaml: feeds[{idx}] must be a mapping.",
                "Ensure every entry in `feeds` is a YAML mapping.",
            )
        fid = feed.get("id")
        if not isinstance(fid, str) or not fid.strip():
            raise EngineError(
                "INVALID_CONFIG_SCHEMA",
                f"feeds.yaml: feeds[{idx}] is missing a non-empty `id`.",
                "Add a unique string `id` to every feed.",
            )
        if fid in seen_ids:
            raise EngineError(
                "INVALID_CONFIG_SCHEMA",
                f"feeds.yaml: duplicate feed id {fid!r}.",
                "Ensure every feed has a unique `id`.",
            )
        seen_ids.add(fid)

        module = feed.get("module")
        if module not in VALID_MODULES:
            raise EngineError(
                "INVALID_CONFIG_SCHEMA",
                f"feeds.yaml: feed {fid!r} has invalid module {module!r}.",
                "Use `weekly` or `payroll` for `module`.",
            )

        sens = feed.get("sensitivity")
        if sens not in VALID_SENSITIVITIES:
            raise EngineError(
                "INVALID_CONFIG_SCHEMA",
                f"feeds.yaml: feed {fid!r} has invalid sensitivity {sens!r}.",
                "Use `board`, `internal`, or `restricted` for `sensitivity`.",
            )
        if sens == "restricted" and feed.get("attachable") is True:
            raise EngineError(
                "PRIVACY_POLICY_VIOLATION",
                f"feeds.yaml: restricted feed {fid!r} cannot have `attachable: true`.",
                "Keep `attachable: false` on all restricted feeds.",
            )
        if "column_aliases" in feed and not isinstance(feed["column_aliases"], dict):
            raise EngineError(
                "INVALID_CONFIG_SCHEMA",
                f"feeds.yaml: feed {fid!r} `column_aliases` must be a mapping.",
                "Map canonical column names to lists of accepted header strings.",
            )


def _validate_assumptions(data: dict[str, Any]) -> None:
    company = data.get("company")
    if not isinstance(company, dict) or not company.get("name"):
        raise EngineError(
            "INVALID_CONFIG_SCHEMA",
            "assumptions.yaml: `company.name` is required.",
            "Set `company.name` to a non-empty string.",
        )

    weekly = data.get("weekly")
    if not isinstance(weekly, dict):
        raise EngineError(
            "INVALID_CONFIG_SCHEMA",
            "assumptions.yaml: `weekly` section must be a mapping.",
            "Provide the `weekly` forecast parameters block.",
        )
    cash_floor = weekly.get("cash_floor_usd")
    if isinstance(cash_floor, bool) or not isinstance(cash_floor, (int, float)) or cash_floor < 0:
        raise EngineError(
            "INVALID_CONFIG_SCHEMA",
            f"assumptions.yaml: `weekly.cash_floor_usd` must be a non-negative number (got {cash_floor!r}).",
            "Set `weekly.cash_floor_usd` to a numeric USD amount >= 0.",
        )
    horizon = weekly.get("horizon_weeks")
    if isinstance(horizon, bool) or not isinstance(horizon, int) or not (1 <= horizon <= 52):
        raise EngineError(
            "INVALID_CONFIG_SCHEMA",
            f"assumptions.yaml: `weekly.horizon_weeks` must be an integer between 1 and 52 (got {horizon!r}).",
            "Set `weekly.horizon_weeks` to an integer (default 13).",
        )
    payroll_base = weekly.get("biweekly_payroll_baseline_usd")
    if isinstance(payroll_base, bool) or not isinstance(payroll_base, (int, float)) or payroll_base < 0:
        raise EngineError(
            "INVALID_CONFIG_SCHEMA",
            f"assumptions.yaml: `weekly.biweekly_payroll_baseline_usd` must be >= 0 (got {payroll_base!r}).",
            "Set `weekly.biweekly_payroll_baseline_usd` to a numeric USD amount >= 0.",
        )

    payroll = data.get("payroll")
    if not isinstance(payroll, dict):
        raise EngineError(
            "INVALID_CONFIG_SCHEMA",
            "assumptions.yaml: `payroll` section must be a mapping.",
            "Provide the `payroll` control parameters block.",
        )
    tol = payroll.get("tieout_tolerance_cents")
    if isinstance(tol, bool) or not isinstance(tol, int) or tol < 0:
        raise EngineError(
            "INVALID_CONFIG_SCHEMA",
            f"assumptions.yaml: `payroll.tieout_tolerance_cents` must be a non-negative integer (got {tol!r}).",
            "Set `payroll.tieout_tolerance_cents` to an integer >= 0 (0 for exact cent matching).",
        )
    erp_mode = payroll.get("erp_posting_mode")
    if erp_mode not in VALID_ERP_MODES:
        raise EngineError(
            "INVALID_CONFIG_SCHEMA",
            f"assumptions.yaml: `payroll.erp_posting_mode` must be `sandbox` or `production` (got {erp_mode!r}).",
            "Use `sandbox` or `production` for `payroll.erp_posting_mode`.",
        )
    approval_ref = payroll.get("mapping_approval_ref")
    if approval_ref is not None and (not isinstance(approval_ref, str) or not approval_ref.strip()):
        raise EngineError(
            "INVALID_CONFIG_SCHEMA",
            f"assumptions.yaml: `payroll.mapping_approval_ref` must be null or a non-empty string (got {approval_ref!r}).",
            "Set `payroll.mapping_approval_ref` to null or a documented approval reference string.",
        )
    req_mapped = payroll.get("require_all_departments_mapped")
    if not isinstance(req_mapped, bool):
        raise EngineError(
            "INVALID_CONFIG_SCHEMA",
            f"assumptions.yaml: `payroll.require_all_departments_mapped` must be boolean (got {req_mapped!r}).",
            "Set `payroll.require_all_departments_mapped` to true or false.",
        )


def _validate_outputs(data: dict[str, Any]) -> None:
    send_mode = data.get("send_mode")
    if send_mode not in VALID_SEND_MODES:
        raise EngineError(
            "SEND_MODE_GUARDRAIL_VIOLATION",
            f"outputs.yaml: `send_mode` must remain `draft_only` (got {send_mode!r}).",
            "Outbound external auto-sending is prohibited by policy; keep `send_mode: draft_only`.",
        )
    approver = data.get("approver")
    if not isinstance(approver, dict) or not approver.get("name"):
        raise EngineError(
            "INVALID_CONFIG_SCHEMA",
            "outputs.yaml: `approver.name` is required.",
            "Set `approver.name` to the designated approver.",
        )
    if not isinstance(data.get("deliverables"), dict):
        raise EngineError(
            "INVALID_CONFIG_SCHEMA",
            "outputs.yaml: `deliverables` must be a mapping.",
            "Define `deliverables.weekly` and `deliverables.payroll` lists.",
        )


def _validate_payroll_mappings(data: dict[str, Any]) -> None:
    clearing = data.get("clearing_accounts")
    if not isinstance(clearing, dict) or not clearing.get("cash_sweep_account"):
        raise EngineError(
            "INVALID_CONFIG_SCHEMA",
            "payroll-mappings.yaml: `clearing_accounts.cash_sweep_account` is required.",
            "Specify clearing cash and withholdings accounts.",
        )
    mappings = data.get("mappings")
    if not isinstance(mappings, list):
        raise EngineError(
            "INVALID_CONFIG_SCHEMA",
            "payroll-mappings.yaml: `mappings` must be a list.",
            "Provide a list of department-to-GL mappings.",
        )
    required_keys = (
        "dept_code",
        "branch",
        "erp_department",
        "wages_account",
        "payroll_tax_account",
        "benefits_account",
    )
    for idx, entry in enumerate(mappings):
        if not isinstance(entry, dict):
            raise EngineError(
                "INVALID_CONFIG_SCHEMA",
                f"payroll-mappings.yaml: mappings[{idx}] must be a mapping.",
                "Ensure every department mapping is a YAML mapping.",
            )
        for key in required_keys:
            if not entry.get(key):
                raise EngineError(
                    "INVALID_CONFIG_SCHEMA",
                    f"payroll-mappings.yaml: mappings[{idx}] is missing required field `{key}`.",
                    f"Provide a non-empty `{key}` on every department mapping.",
                )


# ---------------------------------------------------------------------------
# Loading full workspace configuration & audit log
# ---------------------------------------------------------------------------


def load_config(workspace_dir: Path) -> dict[str, Any]:
    """Load and schema-validate all four declarative YAML files in the workspace."""
    workspace_dir = Path(workspace_dir)
    feeds = load_yaml(workspace_dir / CONFIG_TARGETS["feeds"])
    assumptions = load_yaml(workspace_dir / CONFIG_TARGETS["assumptions"])
    outputs = load_yaml(workspace_dir / CONFIG_TARGETS["outputs"])
    payroll_mappings = load_yaml(workspace_dir / CONFIG_TARGETS["payroll-mappings"])

    validate_config_target("feeds", feeds)
    validate_config_target("assumptions", assumptions)
    validate_config_target("outputs", outputs)
    validate_config_target("payroll-mappings", payroll_mappings)

    company_name = str((assumptions.get("company") or {}).get("name") or COMPANY_NAME)
    return {
        "company_name": company_name,
        "feeds": feeds,
        "assumptions": assumptions,
        "outputs": outputs,
        "payroll_mappings": payroll_mappings,
    }


def read_audit_entries(workspace_dir: Path) -> list[dict[str, Any]]:
    """Read all append-only audit records from `config/config-audit.jsonl`."""
    audit_path = Path(workspace_dir) / AUDIT_LOG_REL
    if not audit_path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    for raw_line in audit_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries


def append_audit_entry(
    workspace_dir: Path,
    *,
    actor: str,
    field_path: str,
    old_value: Any,
    new_value: Any,
    reason: str = "",
    target: str | None = None,
    action: str = "config_set",
) -> dict[str, Any]:
    """Append one structured JSON line to `config/config-audit.jsonl`."""
    audit_path = Path(workspace_dir) / AUDIT_LOG_REL
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    entry: dict[str, Any] = {
        "timestamp": utc_now_iso(),
        "action": action,
        "actor": actor,
        "target": target or "",
        "field_path": field_path,
        "old_value": old_value,
        "new_value": new_value,
        "reason": reason,
    }
    with audit_path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


# ---------------------------------------------------------------------------
# Dot-path navigation for `config set`
# ---------------------------------------------------------------------------

_SEGMENT_RE = re.compile(r"^([^\[\]]+)(?:\[(\d+)\])?$")


def _parse_path_tokens(dot_path: str) -> list[str | int]:
    if not dot_path or not dot_path.strip():
        raise EngineError(
            "INVALID_CONFIG_PATH",
            "Configuration path cannot be empty.",
            "Pass a dot-separated path such as `weekly.cash_floor_usd` or `payroll.mapping_approval_ref`.",
        )
    tokens: list[str | int] = []
    for raw in dot_path.strip().split("."):
        if not raw:
            raise EngineError(
                "INVALID_CONFIG_PATH",
                f"Malformed configuration path {dot_path!r}.",
                "Remove empty segments from the dot path.",
            )
        if raw.isdigit():
            tokens.append(raw)  # Resolve int-key vs list-index dynamically against parent
            continue
        m = _SEGMENT_RE.match(raw)
        if not m:
            raise EngineError(
                "INVALID_CONFIG_PATH",
                f"Invalid path segment {raw!r} in {dot_path!r}.",
                "Use dot notation such as `weekly.cash_floor_usd` or `feeds[0].owner`.",
            )
        tokens.append(m.group(1))
        if m.group(2) is not None:
            tokens.append(int(m.group(2)))
    return tokens


def _resolve_dict_key(mapping: dict[Any, Any], token: str | int) -> Any:
    if token in mapping:
        return token
    if isinstance(token, str) and token.isdigit() and int(token) in mapping:
        return int(token)
    if isinstance(token, int) and str(token) in mapping:
        return str(token)
    return None


def _set_by_path(doc: dict[str, Any], dot_path: str, new_value: Any) -> tuple[Any, dict[str, Any]]:
    """Return `(old_value, updated_doc)` without mutating the input document."""
    updated = copy.deepcopy(doc)
    tokens = _parse_path_tokens(dot_path)
    cur: Any = updated

    for tok in tokens[:-1]:
        if isinstance(cur, dict):
            resolved = _resolve_dict_key(cur, tok)
            if resolved is None:
                raise EngineError(
                    "INVALID_CONFIG_PATH",
                    f"Path segment {tok!r} does not exist in {dot_path!r}.",
                    "Inspect valid keys with `config show` before updating.",
                )
            cur = cur[resolved]
        elif isinstance(cur, list):
            idx = tok if isinstance(tok, int) else (int(tok) if str(tok).isdigit() else -1)
            if idx < 0 or idx >= len(cur):
                raise EngineError(
                    "INVALID_CONFIG_PATH",
                    f"List index {tok!r} out of bounds in {dot_path!r}.",
                    "Inspect list length with `config show`.",
                )
            cur = cur[idx]
        else:
            raise EngineError(
                "INVALID_CONFIG_PATH",
                f"Cannot traverse into scalar at segment {tok!r} of {dot_path!r}.",
                "Specify a valid nested mapping or list path.",
            )

    last = tokens[-1]
    if isinstance(cur, dict):
        resolved = _resolve_dict_key(cur, last)
        if resolved is None:
            # Allow appending a new leaf only if it is not a misspelled top-level key
            if len(tokens) == 1:
                raise EngineError(
                    "INVALID_CONFIG_PATH",
                    f"Unknown top-level configuration key {last!r}.",
                    "Inspect allowed top-level keys with `config show`.",
                )
            old_val = None
            cur[last] = new_value
        else:
            old_val = cur[resolved]
            cur[resolved] = new_value
    elif isinstance(cur, list):
        if str(last) == "append":
            old_val = None
            cur.append(new_value)
        else:
            idx = last if isinstance(last, int) else (int(last) if str(last).isdigit() else -1)
            if idx < 0 or idx >= len(cur):
                raise EngineError(
                    "INVALID_CONFIG_PATH",
                    f"List index {last!r} out of bounds in {dot_path!r}.",
                    "Use a valid index or `.append` to add an item to a list.",
                )
            old_val = cur[idx]
            cur[idx] = new_value
    else:
        raise EngineError(
            "INVALID_CONFIG_PATH",
            f"Cannot set {last!r} on non-container node in {dot_path!r}.",
            "Check the configuration path with `config show`.",
        )

    return old_val, updated


# ---------------------------------------------------------------------------
# Public CLI operations: `config show` & `config set`
# ---------------------------------------------------------------------------


def config_show(workspace_dir: Path, *, target: str | None = None) -> dict[str, Any]:
    """Return validated configuration documents and recent audit entries."""
    workspace_dir = Path(workspace_dir)
    cfg = load_config(workspace_dir)
    audit = read_audit_entries(workspace_dir)

    target_map = {
        "feeds": cfg["feeds"],
        "assumptions": cfg["assumptions"],
        "outputs": cfg["outputs"],
        "payroll-mappings": cfg["payroll_mappings"],
    }
    if target is not None:
        if target not in target_map:
            raise EngineError(
                "UNKNOWN_CONFIG_TARGET",
                f"Unknown config target {target!r}.",
                f"Choose one of: {', '.join(CONFIG_TARGETS.keys())}.",
            )
        return {
            "target": target,
            "file": CONFIG_TARGETS[target],
            "config": target_map[target],
            "audit_count": len(audit),
            "recent_audit_entries": audit[-10:],
        }

    return {
        "targets": list(CONFIG_TARGETS.keys()),
        "files": dict(CONFIG_TARGETS),
        "config": target_map,
        "audit_count": len(audit),
        "recent_audit_entries": audit[-10:],
    }


def config_set(
    workspace_dir: Path,
    *,
    target: str,
    path: str,
    raw_value: str,
    by: str,
    reason: str = "",
) -> dict[str, Any]:
    """Schema-validate and persist a YAML config mutation with append-only audit logging."""
    workspace_dir = Path(workspace_dir)
    if target not in CONFIG_TARGETS:
        raise EngineError(
            "UNKNOWN_CONFIG_TARGET",
            f"Unknown config target {target!r}.",
            f"Choose one of: {', '.join(CONFIG_TARGETS.keys())}.",
        )
    if not by or not by.strip():
        raise EngineError(
            "ACTOR_REQUIRED",
            "Configuration changes require `--by \"<name>\"` for audit attribution.",
            "Re-run `config set` with `--by \"<your name>\"`.",
        )

    try:
        parsed_value = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise EngineError(
            "INVALID_JSON_VALUE",
            f"Could not parse `--value` as JSON: {exc}",
            "Pass a valid JSON literal (e.g. `800000`, `\"APR-2026-1120\"`, `true`, `null`, or `{...}`).",
            details={"raw_value": raw_value},
        ) from exc

    cfg_path = workspace_dir / CONFIG_TARGETS[target]
    current_doc = load_yaml(cfg_path)
    old_value, updated_doc = _set_by_path(current_doc, path, parsed_value)

    # Validate BEFORE writing anything to disk
    validate_config_target(target, updated_doc)

    dump_yaml(cfg_path, updated_doc)
    audit_entry = append_audit_entry(
        workspace_dir,
        actor=by.strip(),
        field_path=f"{target}.{path}",
        old_value=old_value,
        new_value=parsed_value,
        reason=reason.strip() or f"Updated {target}.{path}",
        target=target,
        action="config_set",
    )

    # Refresh Setup.html so visual config & audit view stays synchronized
    from engine.render.html import render_setup_html

    full_cfg = load_config(workspace_dir)
    all_audit = read_audit_entries(workspace_dir)
    setup_html_path = render_setup_html(full_cfg, all_audit, workspace_dir)

    return {
        "target": target,
        "file": CONFIG_TARGETS[target],
        "path": path,
        "old_value": old_value,
        "new_value": parsed_value,
        "audit_entry": audit_entry,
        "setup_html": str(setup_html_path),
    }
