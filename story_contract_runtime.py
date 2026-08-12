"""Runtime persistence and hash gates for Story Production Contracts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from story_contracts import (
    REQUIRED_CONTRACT_SECTIONS,
    SCHEMA_ROOT,
    StoryContractValidationError,
    canonical_json_bytes,
    contract_sha256,
    load_story_contract,
    validate_preview_asset_files,
    validate_trusted_provenance,
)
from story_project import load_config, save_json


CONTRACT_POLICY_REQUIRED = "required_v1"
CONTRACT_POLICY_LEGACY = "legacy_passthrough"
TRUST_CHAIN_VERSION = 1
LOCK_VERSION = 1


def contract_paths(project_root: Path | str) -> dict[str, Path]:
    root = Path(project_root)
    status = root / "99_项目状态"
    contracts = status / "contracts"
    reviews = status / "reviews"
    return {
        "directory": contracts,
        "contract": contracts / "story_production_contract.json",
        "summary": contracts / "story_production_contract.md",
        "trusted_inputs": contracts / "story_contract_trusted_inputs.json",
        "handoff": contracts / "story_contract_handoff.md",
        "lock": contracts / "story_contract.lock.json",
        "bundle": reviews / "story_contract_bundle.json",
        "review": reviews / "story_contract_review_review.json",
    }


def build_trusted_input_chain(
    project_root: Path | str, manifest: Mapping[str, Any], config: Mapping[str, Any]
) -> dict[str, Any]:
    """Build Runtime-owned receipts; never accept generator-provided receipts."""

    root = Path(project_root).resolve()
    story_text_value = manifest.get("inputs", {}).get("story_text") if isinstance(manifest.get("inputs"), Mapping) else ""
    story_text = Path(str(story_text_value or ""))
    if not story_text.is_file():
        raise ValueError("story contract requires a readable manifest inputs.story_text")
    text = story_text.read_text(encoding="utf-8-sig", errors="strict")
    sources: list[dict[str, Any]] = [
        {
            "source": "task_input",
            "source_ref": "task_input.story_text",
            "sha256": _file_sha256(story_text),
            "project_relative_path": _relative_path(story_text, root),
            "text": text,
        }
    ]
    story = manifest.get("story") if isinstance(manifest.get("story"), Mapping) else {}
    manual_overrides = story.get("manual_overrides") if isinstance(story.get("manual_overrides"), Mapping) else {}
    project_rules = {
        str(key): story.get(str(key))
        for key, enabled in manual_overrides.items()
        if bool(enabled) and str(key) in story
    }
    if project_rules:
        sources.append(
            {
                "source": "project_config",
                "source_ref": "project_config.story.manual_overrides",
                "sha256": _json_sha256(project_rules),
                "json": json.loads(canonical_json_bytes(project_rules).decode("utf-8")),
            }
        )
    global_defaults = {
        key: config[key]
        for key in (
            "default_story_type",
            "default_image_style",
            "default_age_range",
            "brand_assets",
            "release_defaults",
            "product_defaults",
        )
        if key in config
    }
    sources.append(
        {
            "source": "brand_or_global_default",
            "source_ref": "brand_or_global_default.pipeline",
            "sha256": _json_sha256(global_defaults),
            "json": json.loads(canonical_json_bytes(global_defaults).decode("utf-8")),
        }
    )
    return {
        "version": TRUST_CHAIN_VERSION,
        "story_id": str(story.get("slug") or story.get("name") or "story"),
        "sources": sources,
    }


def write_trusted_input_chain(path: Path, payload: Mapping[str, Any]) -> Path:
    save_json(path, dict(payload))
    return path


def contract_runtime_issues(project_root: Path | str) -> list[str]:
    root = Path(project_root)
    paths = contract_paths(root)
    try:
        contract = load_story_contract(paths["contract"])
    except StoryContractValidationError as exc:
        return [f"{issue.path}:{issue.code}:{issue.message}" for issue in exc.issues]
    try:
        trusted = json.loads(paths["trusted_inputs"].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"trusted_inputs:invalid:{exc}"]
    try:
        manifest = json.loads((root / "99_项目状态" / "project_manifest.json").read_text(encoding="utf-8"))
        expected_trusted = build_trusted_input_chain(root, manifest, load_config())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [f"trusted_inputs:source_unavailable:{exc}"]
    if canonical_json_bytes(trusted) != canonical_json_bytes(expected_trusted):
        return ["trusted_inputs:source_drift:Runtime 可信来源链与当前输入/配置不一致"]
    issues = validate_trusted_provenance(contract, trusted)
    issues.extend(validate_preview_asset_files(contract, project_root))
    return [f"{issue.path}:{issue.code}:{issue.message}" for issue in issues]


def contract_review_payload_issues(payload: Mapping[str, Any]) -> list[str]:
    matrix = payload.get("evidence_matrix")
    if not isinstance(matrix, list):
        return ["evidence_matrix 必须是数组并逐节覆盖七类合同"]
    covered: set[str] = set()
    issues: list[str] = []
    for index, item in enumerate(matrix):
        if not isinstance(item, Mapping):
            issues.append(f"evidence_matrix[{index}] 必须是对象")
            continue
        section = str(item.get("section") or "")
        evidence = item.get("evidence")
        if section not in REQUIRED_CONTRACT_SECTIONS:
            issues.append(f"evidence_matrix[{index}].section 非法：{section or '<empty>'}")
        else:
            covered.add(section)
        if not (
            isinstance(evidence, str)
            and evidence.strip()
            or isinstance(evidence, list)
            and any(str(value).strip() for value in evidence)
        ):
            issues.append(f"evidence_matrix[{index}].evidence 不能为空")
    missing = sorted(set(REQUIRED_CONTRACT_SECTIONS) - covered)
    if missing:
        issues.append("evidence_matrix 缺少合同节：" + ",".join(missing))
    return issues


def contract_review_artifacts(project_root: Path | str) -> tuple[list[Path], list[Path]]:
    root = Path(project_root)
    paths = contract_paths(root)
    contract = load_story_contract(paths["contract"])
    artifacts = [paths["contract"], paths["summary"], paths["trusted_inputs"]]
    story_text_value = _load_manifest_story_text(root)
    if story_text_value is not None:
        artifacts.append(story_text_value)
    artifacts.extend(sorted(SCHEMA_ROOT.glob("*.schema.json")))
    images: list[Path] = []
    for preview in contract.get("preview_assets", []):
        if isinstance(preview, Mapping) and preview.get("path"):
            target = root / str(preview["path"])
            if target.is_file():
                artifacts.append(target)
                images.append(target)
    return artifacts, images


def expected_contract_lock(
    project_root: Path | str, *, bundle: Path, review: Path
) -> dict[str, Any]:
    paths = contract_paths(project_root)
    contract = load_story_contract(paths["contract"])
    return {
        "version": LOCK_VERSION,
        "schema_version": str(contract["schema_version"]),
        "contract_id": str(contract["contract_id"]),
        "contract_file_sha256": _file_sha256(paths["contract"]),
        "contract_canonical_sha256": contract_sha256(contract),
        "trusted_inputs_sha256": _file_sha256(paths["trusted_inputs"]),
        "review_bundle_sha256": _file_sha256(bundle),
        "review_sha256": _file_sha256(review),
    }


def write_contract_lock(project_root: Path | str, *, bundle: Path, review: Path) -> Path:
    paths = contract_paths(project_root)
    save_json(paths["lock"], expected_contract_lock(project_root, bundle=bundle, review=review))
    return paths["lock"]


def contract_lock_is_current(project_root: Path | str, *, bundle: Path, review: Path) -> bool:
    paths = contract_paths(project_root)
    try:
        current = json.loads(paths["lock"].read_text(encoding="utf-8"))
        expected = expected_contract_lock(project_root, bundle=bundle, review=review)
    except (OSError, ValueError, json.JSONDecodeError, StoryContractValidationError):
        return False
    return current == expected


def _load_manifest_story_text(project_root: Path) -> Path | None:
    manifest_path = project_root / "99_项目状态" / "project_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    candidate = Path(str(manifest.get("inputs", {}).get("story_text") or ""))
    return candidate if candidate.is_file() else None


def _relative_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return path.name


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


__all__ = [
    "CONTRACT_POLICY_LEGACY",
    "CONTRACT_POLICY_REQUIRED",
    "build_trusted_input_chain",
    "contract_lock_is_current",
    "contract_paths",
    "contract_review_artifacts",
    "contract_review_payload_issues",
    "contract_runtime_issues",
    "expected_contract_lock",
    "write_contract_lock",
    "write_trusted_input_chain",
]
