"""Shared hash-bound review evidence for the Codex-native story pipeline.

This module deliberately contains no stage graph, scheduler, supervisor, retry
policy, or provider routing.  It is safe for deterministic production tools to
use without importing the legacy Story Agent runtime.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any


PASS_SCORE = 85


def now() -> str:
    """Return the shared human-readable timestamp used in evidence records."""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    from story_hash_cache import sha256_file
    return sha256_file(path, chunk_size=chunk_size)


def record_derived_input(
    manifest: dict[str, Any],
    *,
    role: str,
    path: Path,
    source_sha256: str,
    decisions_sha256: str,
    producer: str = "source_video_pipeline",
) -> None:
    """Record a source-edit derivative without importing the legacy runtime."""
    contract = manifest.get("agent", {}).get("input_contract")
    if not isinstance(contract, dict) or contract.get("mode") != "single_greenscreen" or not path.is_file():
        return
    derived = contract.setdefault("derived_inputs", {})
    derived[role] = {
        "path": str(path),
        "sha256": file_sha256(path),
        "bytes": path.stat().st_size,
        "producer": producer,
        "source_sha256": source_sha256,
        "decisions_sha256": decisions_sha256,
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def review_passes(
    payload: dict[str, Any],
    *,
    artifact: Path | None = None,
    threshold: int = PASS_SCORE,
) -> bool:
    if payload.get("approved") is not True:
        return False
    try:
        score = float(payload.get("score", 0))
    except (TypeError, ValueError):
        return False
    if not math.isfinite(score) or score < threshold or payload.get("critical_errors"):
        return False
    if artifact is not None:
        expected = str(payload.get("artifact_sha256") or "")
        if not expected or not artifact.is_file() or expected != file_sha256(artifact):
            return False
    return True


def write_review_bundle(path: Path, artifacts: list[Path]) -> Path:
    files: list[Path] = []
    for artifact in artifacts:
        if artifact.is_dir():
            files.extend(sorted(item for item in artifact.rglob("*") if item.is_file()))
        elif artifact.is_file():
            files.append(artifact)
    payload = {
        "version": 1,
        "artifacts": [
            {"path": str(item), "sha256": file_sha256(item), "bytes": item.stat().st_size}
            for item in files
        ],
    }
    _atomic_write_json(path, payload)
    return path


def review_bundle_is_current(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return False
    for item in artifacts:
        if not isinstance(item, dict):
            return False
        target = Path(str(item.get("path") or ""))
        if not target.is_file() or str(item.get("sha256") or "") != file_sha256(target):
            return False
    return True
