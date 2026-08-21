#!/usr/bin/env python3
"""Build and verify an isolated DeepSeek Harness migration package.

The package is created from the committed Git ``HEAD`` rather than by copying
the working directory.  This intentionally excludes Git metadata, ignored
runtime outputs, media, secrets and unrelated dirty changes.  No project code
is imported and no provider is called.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


PACKAGE_KIND = "story_agent_dsh_migration_package_v1"
MANIFEST_NAME = "MIGRATION_PACKAGE_MANIFEST.json"
MANIFEST_SHA_NAME = "MIGRATION_PACKAGE_MANIFEST.sha256"
CODEX_AGENTS_BASELINE = "docs/migration/AGENTS_CODEX_BASELINE.md"

AGENTS_OVERLAY = "docs/migration/DSH_AGENTS.md"
CONFIG_OVERLAY = "docs/migration/pipeline_config.dsh-template.json"

REQUIRED_PATHS = frozenset(
    {
        "AGENTS.md",
        "AI_CONTEXT.md",
        "story_agent.py",
        "story_agent_runtime.py",
        "story_module_ports.py",
        "story_module_registry.py",
        "story_module_adapters.py",
        "story_agent_observability.py",
        "story_agent_recovery.py",
        "story_agent_supervisor.py",
        "story_agent_dashboard.py",
        "story_agent_preflight.py",
        "pipeline_config.json",
        "docs/migration/DSH_MIGRATION_HANDOFF_V35_20260821.md",
        "docs/migration/DSH_FIRST_PROMPT.md",
        AGENTS_OVERLAY,
        CONFIG_OVERLAY,
        "docs/migration/dsh_settings.example.yaml",
        "tests/test_story_agent_runtime.py",
    }
)

FORBIDDEN_PARTS = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "auto-project",
        "output",
        "tmp",
        "backups",
        "version_backups",
    }
)
FORBIDDEN_MEDIA_SUFFIXES = frozenset(
    {
        ".aac",
        ".docx",
        ".flac",
        ".gif",
        ".jpeg",
        ".jpg",
        ".m4a",
        ".mov",
        ".mp3",
        ".mp4",
        ".pdf",
        ".png",
        ".wav",
        ".webp",
    }
)

SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9]{24,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"(?i)https?://[^\s]+/hook/[A-Za-z0-9_-]{20,}"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)"
        r"\s*[:=]\s*['\"]?([A-Za-z0-9._-]{24,})"
    ),
)

ABSOLUTE_REFERENCE_PATTERNS = {
    "user_home": re.compile(r"/Users/[^/\s`'\"]+"),
    "mounted_volume": re.compile(r"/Volumes/[^\s`'\"]+"),
}


class PackageError(RuntimeError):
    pass


def _run_git(source_root: Path, *args: str, check: bool = True) -> str:
    process = subprocess.run(
        ["git", *args],
        cwd=str(source_root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if check and process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "git command failed"
        raise PackageError(detail)
    return process.stdout.strip()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as target:
        json.dump(payload, target, ensure_ascii=False, indent=2, sort_keys=True)
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, path)


def _safe_tar_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members: list[tarfile.TarInfo] = []
    for member in archive.getmembers():
        pure = PurePosixPath(member.name)
        if pure.is_absolute() or ".." in pure.parts:
            raise PackageError(f"unsafe archive member: {member.name}")
        if member.isdev() or member.isfifo():
            raise PackageError(f"unsupported archive member: {member.name}")
        members.append(member)
    return members


def _extract_head(source_root: Path, payload_root: Path, archive_path: Path) -> None:
    process = subprocess.run(
        ["git", "archive", "--format=tar", f"--output={archive_path}", "HEAD"],
        cwd=str(source_root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if process.returncode != 0:
        raise PackageError(process.stderr.strip() or "git archive failed")
    with tarfile.open(archive_path, "r:") as archive:
        members = _safe_tar_members(archive)
        try:
            archive.extractall(payload_root, members=members, filter="fully_trusted")
        except TypeError:  # Python < 3.12 has no extraction filter argument.
            archive.extractall(payload_root, members=members)


def _forbidden_filename(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered == ".env"
        or lowered.startswith(".env.")
        or lowered.startswith("secrets.")
        or lowered == "pipeline_config.local.json"
    )


def _iter_regular_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise PackageError(f"migration package may not contain symlinks: {path.relative_to(root)}")
        if path.is_file():
            yield path


def _scan_secret_text(path: Path) -> None:
    if path.stat().st_size > 4 * 1024 * 1024:
        return
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            raise PackageError(f"high-confidence secret pattern found in {path.name}")


def _nonportable_reference_report(root: Path, files: Iterable[Path]) -> dict[str, Any]:
    by_kind: dict[str, set[str]] = {name: set() for name in ABSOLUTE_REFERENCE_PATTERNS}
    counts = {name: 0 for name in ABSOLUTE_REFERENCE_PATTERNS}
    for path in files:
        if path.stat().st_size > 4 * 1024 * 1024:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        relative = path.relative_to(root).as_posix()
        for name, pattern in ABSOLUTE_REFERENCE_PATTERNS.items():
            matches = pattern.findall(text)
            if matches:
                counts[name] += len(matches)
                by_kind[name].add(relative)
    return {
        "counts": counts,
        "files": {name: sorted(values) for name, values in by_kind.items()},
        "note": (
            "These are legacy/source references for migration audit. Active pipeline_config.json "
            "is overlaid with the portable template; do not execute historical one-off helpers."
        ),
    }


def validate_package_tree(root: Path, *, require_manifest: bool = False) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise PackageError(f"package directory does not exist: {root}")
    files = list(_iter_regular_files(root))
    relative_files = {path.relative_to(root).as_posix() for path in files}
    missing = sorted(REQUIRED_PATHS - relative_files)
    if missing:
        raise PackageError("required package files are missing: " + ", ".join(missing))
    if require_manifest:
        for name in (MANIFEST_NAME, MANIFEST_SHA_NAME):
            if name not in relative_files:
                raise PackageError(f"missing package integrity file: {name}")
    for path in files:
        relative = path.relative_to(root)
        if FORBIDDEN_PARTS.intersection(relative.parts):
            raise PackageError(f"forbidden runtime path in package: {relative}")
        if _forbidden_filename(path.name):
            raise PackageError(f"forbidden local secret/config file in package: {relative}")
        if path.suffix.lower() in FORBIDDEN_MEDIA_SUFFIXES:
            raise PackageError(f"forbidden media/customer artifact in package: {relative}")
        _scan_secret_text(path)
    return {
        "file_count": len(files),
        "total_bytes": sum(path.stat().st_size for path in files),
        "nonportable_references": _nonportable_reference_report(root, files),
    }


def _file_records(root: Path) -> list[dict[str, Any]]:
    excluded = {MANIFEST_NAME, MANIFEST_SHA_NAME}
    records: list[dict[str, Any]] = []
    for path in _iter_regular_files(root):
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        records.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    return records


def _source_git_info(source_root: Path) -> dict[str, Any]:
    top = Path(_run_git(source_root, "rev-parse", "--show-toplevel")).resolve()
    if top != source_root.resolve():
        raise PackageError(f"source root must be repository root: {top}")
    dirty = _run_git(source_root, "status", "--porcelain", "--untracked-files=all")
    if dirty:
        raise PackageError(
            "source worktree is dirty; commit or intentionally exclude changes before building the package"
        )
    branch = _run_git(source_root, "symbolic-ref", "--short", "-q", "HEAD", check=False)
    tracked = _run_git(source_root, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    return {
        "commit": _run_git(source_root, "rev-parse", "HEAD"),
        "tree": _run_git(source_root, "rev-parse", "HEAD^{tree}"),
        "branch": branch or "DETACHED",
        "tracked_file_count": len(tracked),
        "dirty": False,
    }


def _apply_overlays(payload_root: Path) -> dict[str, Any]:
    source_agents = payload_root / "AGENTS.md"
    source_config = payload_root / "pipeline_config.json"
    if not source_agents.is_file() or not source_config.is_file():
        raise PackageError("source snapshot lacks AGENTS.md or pipeline_config.json")
    original = {
        "AGENTS.md": _sha256_file(source_agents),
        "pipeline_config.json": _sha256_file(source_config),
    }
    baseline = payload_root / CODEX_AGENTS_BASELINE
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_bytes(source_agents.read_bytes())
    shutil.copy2(payload_root / AGENTS_OVERLAY, source_agents)
    shutil.copy2(payload_root / CONFIG_OVERLAY, source_config)
    return {
        "AGENTS.md": {
            "source": AGENTS_OVERLAY,
            "original_sha256": original["AGENTS.md"],
            "packaged_sha256": _sha256_file(source_agents),
            "original_preserved_as": CODEX_AGENTS_BASELINE,
        },
        "pipeline_config.json": {
            "source": CONFIG_OVERLAY,
            "original_sha256": original["pipeline_config.json"],
            "packaged_sha256": _sha256_file(source_config),
            "original_preserved": False,
            "reason": "remove machine-specific active paths while retaining provider env names",
        },
    }


def build_package(source_root: Path, destination: Path) -> dict[str, Any]:
    source_root = source_root.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise PackageError(f"destination already exists; refusing to overwrite: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        if os.path.commonpath((str(source_root), str(destination))) == str(source_root):
            raise PackageError("destination must be outside the source repository")
    except ValueError as exc:
        raise PackageError("source and destination paths are incompatible") from exc

    source_git = _source_git_info(source_root)
    build_root = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.building-", dir=str(destination.parent))
    )
    payload_root = build_root / "payload"
    payload_root.mkdir()
    archive_path = build_root / "source-head.tar"
    try:
        _extract_head(source_root, payload_root, archive_path)
        archive_path.unlink(missing_ok=True)
        overlays = _apply_overlays(payload_root)
        validation = validate_package_tree(payload_root)
        file_records = _file_records(payload_root)
        manifest = {
            "kind": PACKAGE_KIND,
            "schema_version": 1,
            "built_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source_git": source_git,
            "overlays": overlays,
            "exclusions": [
                "Git metadata and working-tree-only changes",
                "ignored runtime outputs, caches and build products",
                "all story/customer media and document artifacts",
                "environment files, local config overrides, keys and sessions",
                "Desktop Canary projects including all historical 17-image sets",
                "the original machine-specific active pipeline_config.json content",
            ],
            "provider_calls_made": False,
            "canary_started": False,
            "validation": {
                "file_count_before_manifest": validation["file_count"],
                "total_bytes_before_manifest": validation["total_bytes"],
                "secret_scan_passed": True,
                "symlink_count": 0,
                "forbidden_media_count": 0,
                "nonportable_references": validation["nonportable_references"],
            },
            "files": file_records,
        }
        manifest_path = payload_root / MANIFEST_NAME
        _write_json(manifest_path, manifest)
        manifest_sha = _sha256_file(manifest_path)
        (payload_root / MANIFEST_SHA_NAME).write_text(
            f"{manifest_sha}  {MANIFEST_NAME}\n", encoding="utf-8"
        )
        verify_package(payload_root)
        payload_root.rename(destination)
        shutil.rmtree(build_root)
        return {
            "package": str(destination),
            "manifest": str(destination / MANIFEST_NAME),
            "manifest_sha256": manifest_sha,
            "source_git": source_git,
            "file_count": len(file_records) + 2,
            "provider_calls_made": False,
            "canary_started": False,
        }
    except Exception:
        shutil.rmtree(build_root, ignore_errors=True)
        raise


def verify_package(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    validation = validate_package_tree(root, require_manifest=True)
    manifest_path = root / MANIFEST_NAME
    sidecar_path = root / MANIFEST_SHA_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageError(f"invalid package manifest: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("kind") != PACKAGE_KIND:
        raise PackageError("unexpected package manifest kind")
    sidecar_parts = sidecar_path.read_text(encoding="utf-8").strip().split()
    if len(sidecar_parts) < 2 or sidecar_parts[1] != MANIFEST_NAME:
        raise PackageError("invalid package manifest sidecar")
    actual_manifest_sha = _sha256_file(manifest_path)
    if sidecar_parts[0] != actual_manifest_sha:
        raise PackageError("package manifest SHA-256 mismatch")
    records = manifest.get("files")
    if not isinstance(records, list):
        raise PackageError("package manifest files must be an array")
    expected: set[str] = {MANIFEST_NAME, MANIFEST_SHA_NAME}
    for item in records:
        if not isinstance(item, dict):
            raise PackageError("invalid package file record")
        relative = str(item.get("path") or "")
        pure = PurePosixPath(relative)
        if not relative or pure.is_absolute() or ".." in pure.parts:
            raise PackageError(f"invalid manifest path: {relative}")
        path = root.joinpath(*pure.parts)
        if not path.is_file() or path.is_symlink():
            raise PackageError(f"manifest file is missing or not regular: {relative}")
        if path.stat().st_size != int(item.get("bytes") or -1):
            raise PackageError(f"file size mismatch: {relative}")
        if _sha256_file(path) != str(item.get("sha256") or ""):
            raise PackageError(f"file SHA-256 mismatch: {relative}")
        if relative in expected:
            raise PackageError(f"duplicate package file record: {relative}")
        expected.add(relative)
    actual = {path.relative_to(root).as_posix() for path in _iter_regular_files(root)}
    unexpected = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unexpected or missing:
        raise PackageError(
            f"package file set mismatch; unexpected={unexpected}, missing={missing}"
        )
    if manifest.get("provider_calls_made") is not False:
        raise PackageError("package manifest must state provider_calls_made=false")
    if manifest.get("canary_started") is not False:
        raise PackageError("package manifest must state canary_started=false")
    return {
        "package": str(root),
        "manifest_sha256": actual_manifest_sha,
        "file_count": validation["file_count"],
        "total_bytes": validation["total_bytes"],
        "source_git": manifest.get("source_git", {}),
        "provider_calls_made": False,
        "canary_started": False,
        "verified": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build or verify an isolated Story Agent V3.5 DSH migration package"
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--destination", type=Path, help="New package directory; must not exist")
    action.add_argument("--verify", type=Path, help="Existing package directory to verify")
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Clean Git repository root used for --destination",
    )
    args = parser.parse_args()
    try:
        result = (
            build_package(args.source_root, args.destination)
            if args.destination is not None
            else verify_package(args.verify)
        )
    except PackageError as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
