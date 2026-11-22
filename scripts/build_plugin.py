"""Build the self-contained, pure-Python vendored Claude plugin ZIP + SHA-256 checksum.

Packages:
  .claude-plugin/
  skills/
  docs/
  quickstart/
  engine/          (pure-Python deterministic engine and single-model renderers)
  seed/            (read-only pristine Heartland Mechanical dataset)
  vendor/          (pure-Python deps from packaging/plugin-requirements.txt; native binaries stripped)
  release.json
  LICENSE
  README.md

Output:
  build/rare-kaon-ops-demo-v<version>.zip
  build/rare-kaon-ops-demo-v<version>.zip.sha256

Run:
  .venv/bin/python scripts/build_plugin.py
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine import __version__  # noqa: E402
from engine.contracts import find_forbidden_matches  # noqa: E402

NATIVE_EXTS = {".so", ".dylib", ".pyd", ".dll", ".exe", ".a", ".lib"}
SKIP_DIR_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "bin"}
SKIP_FILE_SUFFIXES = {".pyc", ".pyo", ".DS_Store"}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _install_vendor_deps(vendor_dir: Path, requirements_path: Path) -> list[str]:
    """Install pinned pure-Python dependencies into `vendor_dir` and strip native extensions."""
    vendor_dir.mkdir(parents=True, exist_ok=True)

    uv_bin = shutil.which("uv")
    if uv_bin:
        cmd = [
            uv_bin,
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(vendor_dir),
            "-r",
            str(requirements_path),
        ]
    else:
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(vendor_dir),
            "-r",
            str(requirements_path),
        ]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Failed to vendor dependencies with {' '.join(cmd)}:\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )

    stripped_files: list[str] = []
    # Remove native binaries, compiled caches, and CLI shims so one ZIP runs on macOS/Windows/Linux
    for root, dirs, files in os.walk(vendor_dir, topdown=True):
        root_p = Path(root)
        # Prune unwanted directories
        for dname in list(dirs):
            if dname in SKIP_DIR_NAMES or dname.endswith(".dist-info") or dname.endswith(".egg-info"):
                shutil.rmtree(root_p / dname)
                dirs.remove(dname)
        for fname in files:
            fpath = root_p / fname
            if (
                fname == ".lock"
                or fname == ".DS_Store"
                or fpath.suffix.lower() in NATIVE_EXTS
                or fpath.suffix.lower() in SKIP_FILE_SUFFIXES
            ):
                if fpath.suffix.lower() in NATIVE_EXTS:
                    stripped_files.append(str(fpath.relative_to(vendor_dir)))
                fpath.unlink()

    # Patch ReportLab's optional PIL check if any top-level module eagerly requires PIL
    _patch_reportlab_pil_optional(vendor_dir)

    return sorted(stripped_files)


def _patch_reportlab_pil_optional(vendor_dir: Path) -> None:
    """Ensure vendored ReportLab degrades gracefully without Pillow under `python3 -S`."""
    rl_utils = vendor_dir / "reportlab" / "lib" / "utils.py"
    if not rl_utils.is_file():
        return
    # ReportLab 4.2.5 already wraps PIL imports in try/except inside utils.py, and
    # engine/render/pdf.py installs a minimal PIL shim before importing ReportLab.
    # Verify no hard unguarded `import PIL` exists at top level of reportlab/__init__.py.
    rl_init = vendor_dir / "reportlab" / "__init__.py"
    if rl_init.is_file():
        text = rl_init.read_text(encoding="utf-8")
        if "import PIL" in text and "try:" not in text:
            rl_init.write_text(text.replace("import PIL", "PIL = None"), encoding="utf-8")


def _copy_tree_clean(src: Path, dst: Path) -> None:
    """Copy a directory tree skipping caches and system artifacts."""
    for root, dirs, files in os.walk(src, topdown=True):
        root_p = Path(root)
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES and not d.startswith(".pytest")]
        rel_dir = root_p.relative_to(src)
        target_dir = dst / rel_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        for fname in files:
            if fname == ".DS_Store" or Path(fname).suffix in SKIP_FILE_SUFFIXES:
                continue
            shutil.copy2(root_p / fname, target_dir / fname)


def _verify_zero_forbidden_terms(stage_dir: Path) -> None:
    """Fail the build if any forbidden hygiene match appears anywhere in the staged plugin bundle."""
    text_exts = {
        ".py",
        ".md",
        ".yaml",
        ".yml",
        ".json",
        ".csv",
        ".txt",
        ".html",
        ".toml",
        ".cfg",
        ".ini",
    }
    for path in sorted(stage_dir.rglob("*")):
        if not path.is_file():
            continue
        # Skip third-party vendored library internals
        rel = path.relative_to(stage_dir)
        if rel.parts and rel.parts[0] == "vendor":
            continue
        if path.suffix.lower() not in text_exts and path.name != "LICENSE":
            continue
        haystack = path.read_text(encoding="utf-8", errors="ignore")
        hits = find_forbidden_matches(haystack)
        if rel.as_posix() == "engine/contracts.py":
            hits = [h for h in hits if not h.startswith("path:")]
        if hits:
            raise RuntimeError(
                f"Build aborted: forbidden hygiene matches {hits} found in staged plugin file {rel}"
            )


def _verify_zero_native_binaries(stage_dir: Path) -> None:
    """Fail the build if any compiled native binary remains in the staged plugin."""
    offenders = [
        str(p.relative_to(stage_dir))
        for p in stage_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in NATIVE_EXTS
    ]
    if offenders:
        raise RuntimeError(f"Build aborted: native binary files remain in staged plugin: {offenders}")


def build_plugin(output_dir: Path | None = None) -> dict[str, Any]:
    """Build `build/rare-kaon-ops-demo-v<version>.zip` and `.sha256` checksum."""
    out_dir = Path(output_dir or (REPO_ROOT / "build")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    zip_name = f"rare-kaon-ops-demo-v{__version__}.zip"
    zip_path = out_dir / zip_name
    sha_path = out_dir / f"{zip_name}.sha256"

    req_path = REPO_ROOT / "packaging" / "plugin-requirements.txt"
    if not req_path.is_file():
        raise FileNotFoundError(f"Missing requirements file: {req_path}")

    with tempfile.TemporaryDirectory(prefix="rk_plugin_stage_") as tmp:
        stage = Path(tmp)

        # Copy all required directories
        for dirname in (".claude-plugin", "skills", "docs", "quickstart", "engine", "seed"):
            src = REPO_ROOT / dirname
            if not src.is_dir():
                raise FileNotFoundError(f"Required directory missing: {src}")
            _copy_tree_clean(src, stage / dirname)

        # Copy top-level files
        for fname in ("LICENSE", "README.md"):
            src = REPO_ROOT / fname
            if not src.is_file():
                raise FileNotFoundError(f"Required file missing: {src}")
            shutil.copy2(src, stage / fname)

        # Exclude marketplace manifest from single-plugin ZIP (keep plugin.json)
        mp_file = stage / ".claude-plugin" / "marketplace.json"
        if mp_file.exists():
            mp_file.unlink()

        # Vendor pure-Python dependencies
        stripped = _install_vendor_deps(stage / "vendor", req_path)

        # Build release.json manifest
        pinned_deps = [
            line.strip()
            for line in req_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        release_manifest = {
            "name": "rare-kaon-ops-demo",
            "version": __version__,
            "built_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "python_requires": ">=3.11",
            "pure_python_only": True,
            "vendored_packages": pinned_deps,
            "stripped_native_files_count": len(stripped),
            "license": "MIT",
        }
        (stage / "release.json").write_text(
            json.dumps(release_manifest, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

        # Pre-archive hygiene & portability checks
        _verify_zero_native_binaries(stage)
        _verify_zero_forbidden_terms(stage)

        # Create deterministic ZIP archive
        if zip_path.exists():
            zip_path.unlink()
        file_count = 0
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for path in sorted(stage.rglob("*")):
                if not path.is_file():
                    continue
                arcname = path.relative_to(stage).as_posix()
                zf.write(path, arcname=arcname)
                file_count += 1

    digest = _sha256_file(zip_path)
    sha_path.write_text(f"{digest}  {zip_name}\n", encoding="utf-8", newline="\n")

    return {
        "zip_path": str(zip_path),
        "sha256_path": str(sha_path),
        "sha256": digest,
        "size_bytes": zip_path.stat().st_size,
        "file_count": file_count,
        "stripped_native_files": stripped,
    }


def main() -> None:
    res = build_plugin()
    size_mb = res["size_bytes"] / (1024 * 1024)
    print(f"Built plugin ZIP: {res['zip_path']} ({size_mb:.2f} MB, {res['file_count']} files)")
    print(f"SHA-256: {res['sha256']}")
    print(f"Checksum file: {res['sha256_path']}")
    print(f"Stripped native binaries: {len(res['stripped_native_files'])}")


if __name__ == "__main__":
    main()
