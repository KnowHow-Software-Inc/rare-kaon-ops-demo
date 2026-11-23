"""Structured JSON CLI implementing all 13 `CLI_COMMANDS`.

Every command returns a uniform `ok_envelope` or `error_envelope` JSON document
(from `engine/contracts.py`) so agent harnesses never scrape prose or improvise
ad-hoc scripts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from engine.brief import generate_session_brief
from engine.config import CONFIG_TARGETS, EngineError, config_set, config_show
from engine.contracts import (
    DEFAULT_PAYROLL_PERIOD,
    DEFAULT_WEEKLY_PERIOD,
    EXPLAIN_TOPICS,
    error_envelope,
    ok_envelope,
)
from engine.ingest import ingest_file
from engine.operations import approve_run, decide_change, post_payroll, run_what_if
from engine.payroll import prepare_payroll
from engine.queries import query_explain, query_overview, query_readiness
from engine.weekly import prepare_weekly
from engine.workspace import (
    append_session_event,
    init_workspace,
    reset_workspace,
    resolve_workspace,
)

# Commands logged to `session-events.jsonl` for grounded Session Brief evidence.
# `what-if` is strictly excluded so it preserves zero-byte workspace immutability.
_LOGGED_SESSION_COMMANDS = frozenset(
    {"readiness", "explain", "ingest", "change", "prepare", "approve", "post"}
)


class _JsonArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that raises `EngineError` instead of printing raw stderr text."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise EngineError(
            "CLI_ARGUMENT_ERROR",
            message,
            "Check command syntax and required flags.",
        )


def _add_common_flags(parser: argparse.ArgumentParser, *, include_data: bool = True) -> None:
    if include_data:
        parser.add_argument(
            "--data",
            default=None,
            help="Explicit path to the demo workspace directory (auto-resolved if omitted).",
        )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit structured JSON envelope output.",
    )


def build_parser() -> _JsonArgumentParser:
    """Build the CLI parser for all 13 `CLI_COMMANDS`."""
    parser = _JsonArgumentParser(
        prog="rare-kaon-demo",
        description="Rare Kaon Ops Demo — deterministic single-model finance & ops CLI.",
    )
    parser.add_argument("--json", action="store_true", default=False, help="Emit structured JSON output.")

    sub = parser.add_subparsers(dest="command")

    # 1. init
    p_init = sub.add_parser("init", help="Materialize a fresh Heartland demo workspace from read-only seed/.")
    p_init.add_argument("--dest", default=None, help="Destination workspace directory (defaults to ./heartland-demo).")
    _add_common_flags(p_init, include_data=False)

    # 2. reset
    p_reset = sub.add_parser("reset", help="Reset a marked demo workspace back to pristine seed state.")
    _add_common_flags(p_reset)

    # 3. overview
    p_over = sub.add_parser("overview", help="Summarize workspace status, models, pending changes, and HTML hubs.")
    p_over.add_argument("--refresh", action="store_true", default=False, help="Regenerate Reports.html and Setup.html.")
    _add_common_flags(p_over)

    # 4. readiness
    p_ready = sub.add_parser("readiness", help="Cross-module readiness across Weekly Cash, Payroll, and Configuration.")
    p_ready.add_argument("--period", default=DEFAULT_WEEKLY_PERIOD, help="Weekly period YYYY-MM-DD.")
    _add_common_flags(p_ready)

    # 5. explain
    p_exp = sub.add_parser("explain", help="Return grounded evidence payloads for a frozen explain topic.")
    p_exp.add_argument("--topic", required=True, help=f"One of: {', '.join(EXPLAIN_TOPICS)}.")
    p_exp.add_argument("--period", default=None, help="Optional period YYYY-MM-DD.")
    _add_common_flags(p_exp)

    # 6. what-if
    p_wi = sub.add_parser("what-if", help="Stateless in-memory 13-week cash forecast simulation (zero disk writes).")
    p_wi.add_argument("--period", default=DEFAULT_WEEKLY_PERIOD, help="Weekly period YYYY-MM-DD.")
    p_wi.add_argument("--override", required=True, help="JSON deferral override payload.")
    _add_common_flags(p_wi)

    # 7. ingest
    p_ing = sub.add_parser("ingest", help="Validate and ingest a feed file or structured incoming amendment.")
    p_ing.add_argument("--period", default=DEFAULT_WEEKLY_PERIOD, help="Target period YYYY-MM-DD.")
    p_ing.add_argument("--file", default=None, help="Path to source feed file or incoming amendment YAML.")
    p_ing.add_argument("--feed", default=None, help="Optional explicit feed_id from config/feeds.yaml.")
    _add_common_flags(p_ing)

    # 8. change
    p_chg = sub.add_parser("change", help="Confirm or decline a pending operator change.")
    p_chg.add_argument("action", choices=("confirm", "decline"), help="Decision action.")
    p_chg.add_argument("--id", required=True, dest="change_id", help="Pending change identifier (e.g. CHG-LSS-4189).")
    p_chg.add_argument("--by", default=None, help="Name of operator confirming/declining the change.")
    p_chg.add_argument("--reason", default="", help="Optional audit rationale.")
    _add_common_flags(p_chg)

    # 9. prepare
    p_prep = sub.add_parser("prepare", help="Run deterministic module calculation, checks, and native rendering.")
    p_prep.add_argument("--module", required=True, choices=("weekly", "payroll"), help="Workflow module.")
    p_prep.add_argument("--period", default=None, help="Target period YYYY-MM-DD.")
    _add_common_flags(p_prep)

    # 10. approve
    p_app = sub.add_parser("approve", help="Approve the latest unblocked draft into an immutable numbered edition.")
    p_app.add_argument("--module", required=True, choices=("weekly", "payroll"), help="Workflow module.")
    p_app.add_argument("--period", default=None, help="Target period YYYY-MM-DD.")
    p_app.add_argument("--by", default=None, help="Required approver name.")
    _add_common_flags(p_app)

    # 11. post
    p_post = sub.add_parser("post", help="Enforce payroll ERP posting gates or return sandbox dry-run preview.")
    p_post.add_argument("--module", default="payroll", help="Workflow module (payroll).")
    p_post.add_argument("--period", default=DEFAULT_PAYROLL_PERIOD, help="Payroll pay date YYYY-MM-DD.")
    p_post.add_argument("--target", required=True, choices=("sandbox", "production"), help="ERP posting target.")
    _add_common_flags(p_post)

    # 12. config
    p_cfg = sub.add_parser("config", help="Inspect or schema-validate and update declarative YAML configuration.")
    p_cfg.add_argument("action", choices=("show", "set"), help="Configuration action.")
    p_cfg.add_argument("--target", default=None, help=f"Target YAML ({', '.join(CONFIG_TARGETS.keys())}).")
    p_cfg.add_argument("--path", default=None, help="Dot-separated path to update (for `set`).")
    p_cfg.add_argument("--value", default=None, help="JSON value literal (for `set`).")
    p_cfg.add_argument("--by", default=None, help="Actor name for audit logging (for `set`).")
    p_cfg.add_argument("--reason", default="", help="Optional audit rationale (for `set`).")
    _add_common_flags(p_cfg)

    # 13. brief
    p_brief = sub.add_parser(
        "brief",
        help="Render a 2-page Rare Kaon executive Session Brief (HTML + PDF) from live workspace state.",
    )
    p_brief.add_argument(
        "--spec",
        default=None,
        help="Optional JSON object customizing Page 2 (target_company, target_workflow, target_harness, target_owner, target_systems, target_workflows).",
    )
    p_brief.add_argument("--target-company", default=None, help="Target company or portfolio label for Page 2.")
    p_brief.add_argument("--target-workflow", default=None, help="Named first workflow for Page 2 pilot scope.")
    p_brief.add_argument("--target-harness", default=None, help="Buyer preferred AI harness (claude, codex, m365_copilot).")
    p_brief.add_argument(
        "--include-log",
        action="store_true",
        default=False,
        help="Append an optional Page 3 audit log and run history appendix.",
    )
    _add_common_flags(p_brief)

    return parser


def _annotate_workspace_scope(envelope: dict[str, Any], ws: Path) -> dict[str, Any]:
    """Flag when the resolved workspace lies outside the active session CWD."""
    try:
        cwd = Path.cwd().resolve()
        ws_res = ws.resolve()
        inside = ws_res == cwd or cwd in ws_res.parents or ws_res in cwd.parents
    except OSError:
        return envelope
    if not inside:
        envelope["workspace_outside_session"] = True
        envelope["cwd"] = str(cwd)
    return envelope


def _log_cli_event(ws: Path | None, args: argparse.Namespace, *, ok: bool, error_code: str | None = None) -> None:
    cmd = getattr(args, "command", None)
    if ws is None or cmd not in _LOGGED_SESSION_COMMANDS:
        return
    append_session_event(
        ws,
        command=str(cmd),
        ok=ok,
        error_code=error_code,
        topic=getattr(args, "topic", None),
        module=getattr(args, "module", None),
        period=getattr(args, "period", None),
        target=getattr(args, "target", None),
        change_id=getattr(args, "change_id", None),
    )


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    cmd = args.command
    if not cmd:
        raise EngineError(
            "COMMAND_REQUIRED",
            "No CLI subcommand provided.",
            "Specify one of: init, reset, overview, readiness, explain, what-if, ingest, change, prepare, approve, post, config, brief.",
        )

    if cmd == "init":
        if args.dest:
            dest = Path(args.dest).expanduser().resolve()
            data = init_workspace(dest)
            ws = dest
        else:
            ws = resolve_workspace(None, auto_init=True)
            data = {
                "workspace_dir": str(ws),
                "reports_html": str(ws / "Reports.html"),
                "setup_html": str(ws / "Setup.html"),
                "initialized": True,
            }
        return _annotate_workspace_scope(ok_envelope("init", data, workspace=data["workspace_dir"]), ws)

    if cmd == "reset":
        # Resolve without auto_init so unmarked explicit directories are cleanly refused
        ws = resolve_workspace(args.data, auto_init=False)
        data = reset_workspace(ws)
        return _annotate_workspace_scope(ok_envelope("reset", data, workspace=str(ws)), ws)

    # All remaining commands automatically resolve/materialize the workspace on first run
    ws = resolve_workspace(args.data, auto_init=True)

    try:
        if cmd == "overview":
            data = query_overview(ws, refresh=bool(args.refresh))
            return _annotate_workspace_scope(ok_envelope("overview", data, workspace=str(ws)), ws)

        if cmd == "readiness":
            data = query_readiness(ws, period=args.period)
            _log_cli_event(ws, args, ok=True)
            return _annotate_workspace_scope(ok_envelope("readiness", data, workspace=str(ws)), ws)

        if cmd == "explain":
            data = query_explain(ws, topic=args.topic, period=args.period)
            _log_cli_event(ws, args, ok=True)
            return _annotate_workspace_scope(ok_envelope("explain", data, workspace=str(ws)), ws)

        if cmd == "what-if":
            data = run_what_if(ws, period=args.period, override_json=args.override)
            return _annotate_workspace_scope(ok_envelope("what-if", data, workspace=str(ws)), ws)

        if cmd == "ingest":
            data = ingest_file(ws, period=args.period, file_path=args.file, feed_id=args.feed)
            _log_cli_event(ws, args, ok=True)
            return _annotate_workspace_scope(ok_envelope("ingest", data, workspace=str(ws)), ws)

        if cmd == "change":
            data = decide_change(
                ws,
                action=args.action,
                change_id=args.change_id,
                by=args.by,
                reason=args.reason,
            )
            _log_cli_event(ws, args, ok=True)
            return _annotate_workspace_scope(ok_envelope("change", data, workspace=str(ws)), ws)

        if cmd == "prepare":
            period = args.period or (DEFAULT_WEEKLY_PERIOD if args.module == "weekly" else DEFAULT_PAYROLL_PERIOD)
            if args.module == "weekly":
                data = prepare_weekly(ws, period=period)
            else:
                data = prepare_payroll(ws, period=period)
            _log_cli_event(ws, args, ok=True)
            return _annotate_workspace_scope(ok_envelope("prepare", data, workspace=str(ws)), ws)

        if cmd == "approve":
            period = args.period or (DEFAULT_WEEKLY_PERIOD if args.module == "weekly" else DEFAULT_PAYROLL_PERIOD)
            data = approve_run(ws, module=args.module, period=period, by=args.by)
            _log_cli_event(ws, args, ok=True)
            return _annotate_workspace_scope(ok_envelope("approve", data, workspace=str(ws)), ws)

        if cmd == "post":
            data = post_payroll(ws, module=args.module, period=args.period, target=args.target)
            _log_cli_event(ws, args, ok=True)
            return _annotate_workspace_scope(ok_envelope("post", data, workspace=str(ws)), ws)

        if cmd == "config":
            if args.action == "show":
                data = config_show(ws, target=args.target)
            else:
                if not args.target:
                    raise EngineError(
                        "CONFIG_TARGET_REQUIRED",
                        "`config set` requires `--target <feeds|assumptions|outputs|payroll-mappings>`.",
                        "Re-run `config set` with `--target`, `--path`, `--value`, and `--by`.",
                    )
                if not args.path:
                    raise EngineError(
                        "CONFIG_PATH_REQUIRED",
                        "`config set` requires `--path <dot.path>`.",
                        "Specify the configuration dot path to update.",
                    )
                if args.value is None:
                    raise EngineError(
                        "CONFIG_VALUE_REQUIRED",
                        "`config set` requires `--value '<JSON>'`.",
                        "Provide a JSON value literal for `--value`.",
                    )
                data = config_set(
                    ws,
                    target=args.target,
                    path=args.path,
                    raw_value=args.value,
                    by=args.by or "",
                    reason=args.reason or "",
                )
            return _annotate_workspace_scope(ok_envelope("config", data, workspace=str(ws)), ws)

        if cmd == "brief":
            data = generate_session_brief(
                ws,
                spec_json=args.spec,
                target_company=args.target_company,
                target_workflow=args.target_workflow,
                target_harness=args.target_harness,
                include_log=bool(args.include_log),
            )
            return _annotate_workspace_scope(ok_envelope("brief", data, workspace=str(ws)), ws)

        raise EngineError(
            "UNKNOWN_COMMAND",
            f"Unsupported CLI command {cmd!r}.",
            "Use one of the supported CLI commands.",
        )
    except EngineError as err:
        _log_cli_event(ws, args, ok=False, error_code=err.error_code)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint emitting uniform JSON envelopes to stdout."""
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    cmd_hint = next((tok for tok in raw_argv if not tok.startswith("-")), "cli")

    parser = build_parser()
    try:
        args = parser.parse_args(raw_argv)
        envelope = _dispatch(args)
        print(json.dumps(envelope, indent=2, ensure_ascii=False))
        return 0
    except EngineError as err:
        envelope = error_envelope(
            cmd_hint,
            err.error_code,
            err.cause,
            err.suggested_action,
            details=err.details or None,
        )
        print(json.dumps(envelope, indent=2, ensure_ascii=False))
        return 1
    except Exception as exc:  # noqa: BLE001
        envelope = error_envelope(
            cmd_hint,
            "UNEXPECTED_ENGINE_ERROR",
            f"{type(exc).__name__}: {exc}",
            "Inspect the workspace state or run `reset` to restore the demo workspace.",
        )
        print(json.dumps(envelope, indent=2, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
