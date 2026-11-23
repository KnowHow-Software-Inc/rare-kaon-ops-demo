"""Workspace lifecycle, read-only seed isolation, SQLite state, and safe resets.

Guarantees:
- Bundled `seed/` is strictly read-only at runtime.
- Automatic workspace resolution when `--data` is omitted.
- Marked-folder safety: `reset` refuses any directory missing `.rare-kaon-demo-workspace.json`.
- Immutable draft folders under `runs/<module>/<period>/draft-NNN/` and approved editions
  under `editions/<module>/<period>/edition-NNN/`.
- First workspace materialization automatically prepares Weekly Draft 1 (`2026-11-20`)
  and renders `Reports.html` and `Setup.html`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from engine import __version__
from engine.config import EngineError, load_config, read_audit_entries, utc_now_iso
from engine.contracts import (
    COMPANY_NAME,
    DEFAULT_PAYROLL_PERIOD,
    DEFAULT_WEEKLY_PERIOD,
    FALLBACK_HOME_SUBDIR,
    SCHEMA_VERSION,
    WORKSPACE_DIR_NAME,
    WORKSPACE_MARKER,
)
from engine.model import PendingChange, ReportModel

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = PACKAGE_ROOT / "seed"
DB_FILENAME = "workspace.db"


# ---------------------------------------------------------------------------
# SQLite schema & connection helpers
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingested_inputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period TEXT NOT NULL,
    feed_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    as_of TEXT,
    sensitivity TEXT NOT NULL,
    attachable INTEGER NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    UNIQUE(period, feed_id, file_name)
);

CREATE TABLE IF NOT EXISTS pending_changes (
    id TEXT PRIMARY KEY,
    period TEXT NOT NULL,
    module TEXT NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    source_file TEXT NOT NULL,
    status TEXT NOT NULL,
    needs_decision INTEGER NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    recorded_by TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL DEFAULT '',
    decided_by TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    module TEXT NOT NULL,
    period TEXT NOT NULL,
    draft_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    run_dir TEXT NOT NULL,
    model_path TEXT NOT NULL,
    can_approve INTEGER NOT NULL,
    blocking_count INTEGER NOT NULL,
    open_review_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(module, period, draft_number)
);

CREATE TABLE IF NOT EXISTS editions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    module TEXT NOT NULL,
    period TEXT NOT NULL,
    edition_number INTEGER NOT NULL,
    source_draft_number INTEGER NOT NULL,
    edition_dir TEXT NOT NULL,
    model_path TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    UNIQUE(module, period, edition_number)
);
"""


def sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_marked_workspace(path: Path) -> bool:
    """Return True if `path` contains a valid `.rare-kaon-demo-workspace.json` marker."""
    marker = Path(path) / WORKSPACE_MARKER
    if not marker.is_file():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        return isinstance(data, dict) and data.get("marker") == "rare-kaon-ops-demo"
    except (OSError, json.JSONDecodeError):
        return False


def connect_db(workspace_dir: Path, *, readonly: bool = False) -> sqlite3.Connection:
    """Open `workspace.db` (optionally in strict read-only URI mode for zero-write ops)."""
    db_path = Path(workspace_dir) / DB_FILENAME
    if readonly:
        uri = f"file:{db_path.resolve().as_posix()}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def clone_db_to_memory(workspace_dir: Path) -> sqlite3.Connection:
    """Clone `workspace.db` into an isolated `:memory:` SQLite DB via `conn.backup()`."""
    src = connect_db(workspace_dir, readonly=True)
    try:
        mem = sqlite3.connect(":memory:")
        mem.row_factory = sqlite3.Row
        src.backup(mem)
        return mem
    finally:
        src.close()


def _init_db(workspace_dir: Path, config: dict[str, Any]) -> None:
    conn = connect_db(workspace_dir, readonly=False)
    try:
        conn.executescript(_SCHEMA_SQL)
        now = utc_now_iso()
        meta_rows = [
            ("schema_version", str(SCHEMA_VERSION)),
            ("engine_version", __version__),
            ("company_name", config.get("company_name", COMPANY_NAME)),
            ("created_at", now),
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            meta_rows,
        )

        # Register bundled seed source feeds in `ingested_inputs`
        feeds = (config.get("feeds") or {}).get("feeds") or []
        for feed in feeds:
            default_rel = feed.get("default_file")
            if not default_rel:
                continue
            src_path = workspace_dir / default_rel
            if not src_path.is_file():
                continue
            fid = feed["id"]
            module = feed["module"]
            period = DEFAULT_WEEKLY_PERIOD if module == "weekly" else DEFAULT_PAYROLL_PERIOD
            sensitivity = feed.get("sensitivity", "internal")
            attachable = 0 if sensitivity == "restricted" else (1 if feed.get("attachable", True) else 0)
            as_of = _infer_as_of_from_filename(src_path.name, period)
            conn.execute(
                """
                INSERT OR REPLACE INTO ingested_inputs(
                    period, feed_id, file_name, relative_path, as_of,
                    sensitivity, attachable, row_count, sha256, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    period,
                    fid,
                    src_path.name,
                    default_rel,
                    as_of,
                    sensitivity,
                    attachable,
                    0,
                    sha256_file(src_path),
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _infer_as_of_from_filename(filename: str, fallback: str) -> str:
    import re

    m = re.search(r"(\d{4}-\d{2}-\d{2})", filename)
    return m.group(1) if m else fallback


# ---------------------------------------------------------------------------
# Workspace path resolution
# ---------------------------------------------------------------------------


def _is_writable_dir(path: Path) -> bool:
    try:
        if not path.exists():
            parent = path.parent
            return parent.exists() and os.access(parent, os.W_OK | os.X_OK)
        if not path.is_dir():
            return False
        if not os.access(path, os.W_OK | os.X_OK):
            return False
        # Confirm real write capability with a probe only if needed; os.access is fast and safe
        return True
    except OSError:
        return False


def _is_inside_packaged_plugin(cwd: Path) -> bool:
    """Return True if `cwd` sits inside an installed/packaged read-only plugin directory."""
    try:
        cwd_res = cwd.resolve()
        pkg_res = PACKAGE_ROOT.resolve()
    except OSError:
        return False

    if cwd_res == pkg_res or pkg_res in cwd_res.parents:
        # Packaged plugin ZIPs contain `release.json` or `vendor/` (not `.git`).
        # A cloned git repo working tree is allowed to materialize `./heartland-demo/`
        # (which is in `.gitignore`), whereas a packaged plugin install dir should
        # never be mutated in place.
        if (pkg_res / "release.json").is_file() or (pkg_res / "vendor").is_dir():
            return True
        if not (pkg_res / ".git").exists() and not os.access(pkg_res, os.W_OK):
            return True
        # Also refuse writing inside `seed/` or `engine/` or `skills/` subdirectories
        if cwd_res != pkg_res:
            return True
    return False


def _is_plugin_cache_dir(path: Path) -> bool:
    """Return True if `path` sits inside a harness plugin install/cache tree."""
    lower_parts = {part.lower() for part in path.parts}
    if ".claude" in lower_parts and "plugins" in lower_parts:
        return True
    if "plugins" in lower_parts and ("claude" in lower_parts or "cache" in lower_parts):
        return True
    return False


def _cowork_session_root(path: Path) -> Path | None:
    """Return `/sessions/<id>` when running inside a sandboxed Cowork session tree."""
    parts = path.parts
    if len(parts) >= 3 and parts[0] == "/" and parts[1] == "sessions":
        return Path("/") / "sessions" / parts[2]
    return None


def _choose_cowork_workspace_candidate(cwd: Path, pkg_root: Path) -> Path | None:
    """Resolve an in-session workspace path when `cwd` is read-only inside a Cowork VM."""
    session_root = _cowork_session_root(cwd) or _cowork_session_root(pkg_root)
    if session_root is None or not session_root.is_dir():
        return None

    mnt_dir = session_root / "mnt"
    candidate_bases: list[Path] = []
    if mnt_dir.is_dir():
        try:
            entries = sorted(mnt_dir.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            entries = []
        # Prefer mounted user folders first, then `outputs`
        user_mounts = [
            p
            for p in entries
            if p.is_dir()
            and not p.name.startswith(".")
            and p.name not in {"outputs", "uploads"}
            and not _is_plugin_cache_dir(p)
            and _is_writable_dir(p)
        ]
        candidate_bases.extend(user_mounts)
        outputs_dir = mnt_dir / "outputs"
        if outputs_dir.is_dir() and _is_writable_dir(outputs_dir):
            candidate_bases.append(outputs_dir)

    if _is_writable_dir(session_root):
        candidate_bases.append(session_root)

    for base in candidate_bases:
        ws = base / WORKSPACE_DIR_NAME
        if is_marked_workspace(ws):
            return ws
    if candidate_bases:
        return candidate_bases[0] / WORKSPACE_DIR_NAME
    return None


def _choose_default_workspace_path() -> Path:
    cwd = Path.cwd().resolve()
    pkg_res = PACKAGE_ROOT.resolve()

    # 1. Reuse existing marked workspace in CWD or ./heartland-demo
    if is_marked_workspace(cwd):
        return cwd
    cwd_candidate = cwd / WORKSPACE_DIR_NAME
    if is_marked_workspace(cwd_candidate):
        return cwd_candidate

    # 2. Prefer materializing ./heartland-demo directly inside any writable session/user CWD.
    #    Never let a pre-existing ~/.rare-kaon-ops-demo/heartland-demo hijack a fresh session folder.
    if (
        cwd != Path("/")
        and _is_writable_dir(cwd)
        and not _is_inside_packaged_plugin(cwd)
        and not _is_plugin_cache_dir(cwd)
    ):
        return cwd_candidate

    # 3. If CWD is inside an unpacked plugin folder (e.g., `./rare-kaon-ops-demo-plugin/`),
    #    materialize `heartland-demo/` as a sibling in the writable parent session directory.
    if _is_inside_packaged_plugin(cwd):
        pkg_parent = pkg_res.parent
        if is_marked_workspace(pkg_parent):
            return pkg_parent
        sibling_candidate = pkg_parent / WORKSPACE_DIR_NAME
        if is_marked_workspace(sibling_candidate):
            return sibling_candidate
        if (
            pkg_parent != Path("/")
            and _is_writable_dir(pkg_parent)
            and not _is_plugin_cache_dir(pkg_parent)
        ):
            return sibling_candidate

    # 4. In sandboxed Cowork VMs where CWD is read-only or inside a plugin mount, keep
    #    the workspace inside the active `/sessions/<id>/...` mount tree.
    cowork_candidate = _choose_cowork_workspace_candidate(cwd, pkg_res)
    if cowork_candidate is not None:
        return cowork_candidate

    # 5. Last resort (read-only CWD outside a writable session directory): home or temp fallback
    home_candidate = Path.home() / FALLBACK_HOME_SUBDIR / WORKSPACE_DIR_NAME
    if is_marked_workspace(home_candidate):
        return home_candidate

    tmp_candidate = Path(tempfile.gettempdir()) / "rare-kaon-ops-demo" / WORKSPACE_DIR_NAME
    if is_marked_workspace(tmp_candidate):
        return tmp_candidate

    home_parent = home_candidate.parent
    try:
        home_parent.mkdir(parents=True, exist_ok=True)
        if _is_writable_dir(home_parent):
            return home_candidate
    except OSError:
        pass

    tmp_candidate.parent.mkdir(parents=True, exist_ok=True)
    return tmp_candidate


def resolve_workspace(
    explicit_data: str | Path | None = None,
    *,
    auto_init: bool = True,
) -> Path:
    """Resolve the active workspace directory, materializing from `seed/` on first use."""
    if explicit_data is not None:
        target = Path(explicit_data).expanduser().resolve()
        if is_marked_workspace(target):
            return target
        if not auto_init:
            return target
        # Safety check: never clobber an existing non-empty directory missing the marker
        if target.exists() and any(target.iterdir()):
            raise EngineError(
                "UNMARKED_WORKSPACE_REFUSED",
                f"Directory {target} exists and is not empty, but lacks `{WORKSPACE_MARKER}`.",
                "Specify an empty directory, a new `--dest` path, or an existing Heartland demo workspace.",
                details={"path": str(target)},
            )
        init_workspace(target)
        return target

    chosen = _choose_default_workspace_path()
    if not is_marked_workspace(chosen) and auto_init:
        init_workspace(chosen)
    return chosen


# ---------------------------------------------------------------------------
# Workspace initialization & marked-folder-safe reset
# ---------------------------------------------------------------------------


def _ensure_not_seed_dir(dest: Path) -> None:
    try:
        dest_res = dest.resolve()
        seed_res = SEED_DIR.resolve()
    except OSError:
        return
    if dest_res == seed_res or seed_res in dest_res.parents:
        raise EngineError(
            "SEED_DIR_READ_ONLY",
            "Refused to write inside the bundled read-only `seed/` directory.",
            "Choose a workspace directory outside `seed/` (for example `./heartland-demo/`).",
        )


def init_workspace(dest: Path) -> dict[str, Any]:
    """Materialize a fresh Heartland demo workspace from read-only `seed/`.

    Automatically prepares baseline Weekly Draft 1 (`2026-11-20`) and renders
    `Reports.html` and `Setup.html` so Prompt 1 immediately delivers live outputs.
    """
    dest = Path(dest).expanduser().resolve()
    _ensure_not_seed_dir(dest)

    if not SEED_DIR.is_dir():
        raise EngineError(
            "SEED_MISSING",
            f"Bundled seed directory not found at {SEED_DIR}.",
            "Reinstall or unpack the full plugin package including `seed/`.",
        )

    if dest.exists() and any(dest.iterdir()) and not is_marked_workspace(dest):
        raise EngineError(
            "UNMARKED_WORKSPACE_REFUSED",
            f"Target directory {dest} is non-empty and lacks `{WORKSPACE_MARKER}`.",
            "Use an empty directory or run `reset` on an already marked workspace.",
            details={"path": str(dest)},
        )

    dest.mkdir(parents=True, exist_ok=True)

    # Copy pristine read-only seed subdirectories into working copy
    for subdir in ("config", "sources", "incoming", "history"):
        src_sub = SEED_DIR / subdir
        dst_sub = dest / subdir
        if dst_sub.exists():
            shutil.rmtree(dst_sub)
        if src_sub.is_dir():
            shutil.copytree(src_sub, dst_sub)

    (dest / "runs").mkdir(exist_ok=True)
    (dest / "editions").mkdir(exist_ok=True)

    # Stamp workspace marker
    marker_payload = {
        "marker": "rare-kaon-ops-demo",
        "schema_version": SCHEMA_VERSION,
        "engine_version": __version__,
        "company_name": COMPANY_NAME,
        "created_at": utc_now_iso(),
    }
    (dest / WORKSPACE_MARKER).write_text(
        json.dumps(marker_payload, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    # Validate copied config and initialize SQLite state
    cfg = load_config(dest)
    _init_db(dest, cfg)

    # Prepare initial baseline Weekly Draft 1 (`2026-11-20`) & render HTML hubs
    from engine.weekly import prepare_weekly

    baseline_res = prepare_weekly(dest, period=DEFAULT_WEEKLY_PERIOD)

    return {
        "workspace_dir": str(dest),
        "marker_file": str(dest / WORKSPACE_MARKER),
        "company_name": cfg["company_name"],
        "initial_weekly_draft": baseline_res,
        "reports_html": str(dest / "Reports.html"),
        "setup_html": str(dest / "Setup.html"),
    }


def reset_workspace(workspace_dir: Path) -> dict[str, Any]:
    """Reset a marked demo workspace back to pristine seed state.

    Programmatically refuses any directory lacking `.rare-kaon-demo-workspace.json`.
    """
    workspace_dir = Path(workspace_dir).expanduser().resolve()
    _ensure_not_seed_dir(workspace_dir)

    if not workspace_dir.is_dir() or not is_marked_workspace(workspace_dir):
        raise EngineError(
            "UNMARKED_WORKSPACE_REFUSED",
            f"Refused to reset {workspace_dir}: missing valid `{WORKSPACE_MARKER}` safety marker.",
            "Only marked Heartland demo workspaces created by `init` or first-run auto-setup can be reset.",
            details={"path": str(workspace_dir), "required_marker": WORKSPACE_MARKER},
        )

    # Wipe managed contents inside the marked workspace directory
    for child in list(workspace_dir.iterdir()):
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()

    result = init_workspace(workspace_dir)
    result["reset"] = True
    return result


# ---------------------------------------------------------------------------
# State query helpers (runs, editions, pending changes, HTML refresh)
# ---------------------------------------------------------------------------


def next_draft_number(conn: sqlite3.Connection, module: str, period: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(draft_number), 0) AS max_d FROM runs WHERE module = ? AND period = ?",
        (module, period),
    ).fetchone()
    return int(row["max_d"]) + 1


def next_edition_number(conn: sqlite3.Connection, module: str, period: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(edition_number), 0) AS max_e FROM editions WHERE module = ? AND period = ?",
        (module, period),
    ).fetchone()
    return int(row["max_e"]) + 1


def get_pending_changes(
    conn: sqlite3.Connection,
    *,
    module: str | None = None,
    period: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """Query `pending_changes` rows from SQLite (disk or `:memory:`)."""
    clauses: list[str] = []
    params: list[Any] = []
    if module is not None:
        clauses.append("module = ?")
        params.append(module)
    if period is not None:
        clauses.append("period = ?")
        params.append(period)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM pending_changes{where} ORDER BY recorded_at ASC, id ASC",
        params,
    ).fetchall()
    result: list[dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        item["needs_decision"] = bool(item["needs_decision"])
        item["payload"] = json.loads(item.pop("payload_json"))
        result.append(item)
    return result


def pending_rows_to_dataclasses(rows: list[dict[str, Any]]) -> list[PendingChange]:
    """Convert SQLite pending-change dicts into `PendingChange` dataclasses."""
    out: list[PendingChange] = []
    for r in rows:
        out.append(
            PendingChange(
                id=r["id"],
                period=r["period"],
                module=r["module"],
                kind=r["kind"],
                summary=r["summary"],
                source_file=r["source_file"],
                needs_decision=bool(r["needs_decision"]),
                rationale=r.get("rationale", ""),
                recorded_by=r.get("recorded_by", ""),
                recorded_at=r.get("recorded_at", ""),
                payload=dict(r.get("payload") or {}),
            )
        )
    return out


def load_latest_model(
    workspace_dir: Path,
    *,
    module: str,
    period: str,
    prefer_edition: bool = False,
) -> tuple[ReportModel | None, Path | None]:
    """Load the latest draft (or approved edition) `ReportModel` for `(module, period)`."""
    workspace_dir = Path(workspace_dir)
    conn = connect_db(workspace_dir, readonly=True)
    try:
        if prefer_edition:
            ed = conn.execute(
                """
                SELECT model_path FROM editions
                WHERE module = ? AND period = ?
                ORDER BY edition_number DESC LIMIT 1
                """,
                (module, period),
            ).fetchone()
            if ed:
                p = workspace_dir / ed["model_path"]
                if p.is_file():
                    return ReportModel.from_dict(json.loads(p.read_text(encoding="utf-8"))), p

        run = conn.execute(
            """
            SELECT model_path FROM runs
            WHERE module = ? AND period = ?
            ORDER BY draft_number DESC LIMIT 1
            """,
            (module, period),
        ).fetchone()
        if run:
            p = workspace_dir / run["model_path"]
            if p.is_file():
                return ReportModel.from_dict(json.loads(p.read_text(encoding="utf-8"))), p
        return None, None
    finally:
        conn.close()


def collect_active_models(workspace_dir: Path) -> list[ReportModel]:
    """Collect the active `ReportModel` for each `(module, period)` to render in `Reports.html`.

    If an approved edition exists for the latest draft of `(module, period)`, displays
    the approved edition; if a newer unapproved draft exists, displays the newer draft.
    """
    workspace_dir = Path(workspace_dir)
    conn = connect_db(workspace_dir, readonly=True)
    try:
        pairs = conn.execute(
            """
            SELECT DISTINCT module, period FROM (
                SELECT module, period FROM runs
                UNION
                SELECT module, period FROM editions
            )
            ORDER BY CASE module WHEN 'weekly' THEN 0 ELSE 1 END, period DESC
            """
        ).fetchall()

        models: list[ReportModel] = []
        for pair in pairs:
            mod, per = pair["module"], pair["period"]
            latest_run = conn.execute(
                """
                SELECT draft_number, model_path FROM runs
                WHERE module = ? AND period = ?
                ORDER BY draft_number DESC LIMIT 1
                """,
                (mod, per),
            ).fetchone()
            latest_ed = conn.execute(
                """
                SELECT edition_number, source_draft_number, model_path FROM editions
                WHERE module = ? AND period = ?
                ORDER BY edition_number DESC LIMIT 1
                """,
                (mod, per),
            ).fetchone()

            chosen_rel: str | None = None
            if latest_ed and (not latest_run or int(latest_ed["source_draft_number"]) >= int(latest_run["draft_number"])):
                chosen_rel = latest_ed["model_path"]
            elif latest_run:
                chosen_rel = latest_run["model_path"]

            if chosen_rel:
                p = workspace_dir / chosen_rel
                if p.is_file():
                    models.append(ReportModel.from_dict(json.loads(p.read_text(encoding="utf-8"))))
        return models
    finally:
        conn.close()


def refresh_html_views(workspace_dir: Path) -> tuple[Path, Path]:
    """Re-render both `Reports.html` and `Setup.html` from canonical models and YAML config."""
    from engine.render.html import render_reports_html, render_setup_html

    workspace_dir = Path(workspace_dir)
    cfg = load_config(workspace_dir)
    audit = read_audit_entries(workspace_dir)
    models = collect_active_models(workspace_dir)
    reports_path = render_reports_html(models, cfg, workspace_dir)
    setup_path = render_setup_html(cfg, audit, workspace_dir)
    return reports_path, setup_path
