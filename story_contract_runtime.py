"""Runtime persistence and hash gates for Story Production Contracts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
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
LEGACY_ELIGIBILITY_VERSION = 1
V3_BASELINE_TAG = "story-agent-v3-baseline-2026-08-12"
V3_BASELINE_COMMIT = "7e47efe9abd517fc999d3b0505ad9195c2d1e650"
V3_BASELINE_CREATED_AT = "2026-08-12 19:06:01"

# A consumer hash intentionally covers only the contract sections that can
# affect that output family.  The full contract hash is still recorded for
# audit, but an unrelated brand edit must not invalidate story images and an
# unrelated music/semantic edit must not invalidate a release layout.
CONTRACT_CONSUMER_SECTIONS: dict[str, tuple[str, ...]] = {
    "storyboard_images": ("semantic_artifacts", "visual_style", "characters", "world_scale", "story_state"),
    "image_video": ("characters", "story_state"),
    "music": ("semantic_artifacts", "story_state"),
    "cover": ("brand", "characters", "release_layout"),
    "release_video": ("brand", "release_layout"),
    "product_package": ("semantic_artifacts",),
}


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
        "consumers": contracts / "consumers",
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
    _write_crash_safe_json(
        paths["lock"],
        expected_contract_lock(project_root, bundle=bundle, review=review),
    )
    return paths["lock"]


def contract_lock_is_current(project_root: Path | str, *, bundle: Path, review: Path) -> bool:
    paths = contract_paths(project_root)
    try:
        current = json.loads(paths["lock"].read_text(encoding="utf-8"))
        expected = expected_contract_lock(project_root, bundle=bundle, review=review)
    except (OSError, ValueError, json.JSONDecodeError, StoryContractValidationError):
        return False
    return _lock_shape_is_complete(current) and current == expected


def legacy_eligibility_receipt(manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return a deterministic receipt only for a demonstrably pre-V3.5 run.

    Merely writing ``policy=legacy_passthrough`` is never enough.  The
    manifest must predate the frozen V3 baseline and contain a completed
    pre-contract stage.  The receipt binds those immutable migration facts.
    """

    created_at = str(manifest.get("created_at") or "").strip()
    if not created_at or created_at > V3_BASELINE_CREATED_AT:
        return None
    agent = manifest.get("agent") if isinstance(manifest.get("agent"), Mapping) else {}
    stages = agent.get("stages") if isinstance(agent.get("stages"), Mapping) else {}
    anchors: list[dict[str, str]] = []
    for stage, record in stages.items():
        if stage in {"story_contract", "story_contract_review"} or not isinstance(record, Mapping):
            continue
        status = str(record.get("status") or "")
        if status == "passed":
            anchors.append({"stage": str(stage), "status": status})
    if not anchors:
        return None
    anchors.sort(key=lambda item: item["stage"])
    facts = {
        "created_at": created_at,
        "story_name": str((manifest.get("story") or {}).get("name") or "")
        if isinstance(manifest.get("story"), Mapping)
        else "",
        "story_slug": str((manifest.get("story") or {}).get("slug") or "")
        if isinstance(manifest.get("story"), Mapping)
        else "",
        "anchors": anchors,
    }
    return {
        "version": LEGACY_ELIGIBILITY_VERSION,
        "baseline_tag": V3_BASELINE_TAG,
        "baseline_commit": V3_BASELINE_COMMIT,
        "facts": facts,
        "facts_sha256": _json_sha256(facts),
    }


def legacy_passthrough_allowed(manifest: Mapping[str, Any]) -> bool:
    agent = manifest.get("agent") if isinstance(manifest.get("agent"), Mapping) else {}
    runtime = agent.get("story_contract") if isinstance(agent.get("story_contract"), Mapping) else {}
    if runtime.get("policy") != CONTRACT_POLICY_LEGACY:
        return False
    receipt = runtime.get("legacy_eligibility")
    expected = legacy_eligibility_receipt(manifest)
    return isinstance(receipt, Mapping) and expected is not None and dict(receipt) == expected


def contract_consumer_path(project_root: Path | str, consumer: str) -> Path:
    if consumer not in CONTRACT_CONSUMER_SECTIONS:
        raise ValueError(f"unknown story contract consumer: {consumer}")
    return contract_paths(project_root)["consumers"] / f"{consumer}.json"


def contract_consumer_receipt_path(project_root: Path | str, consumer: str) -> Path:
    if consumer not in CONTRACT_CONSUMER_SECTIONS:
        raise ValueError(f"unknown story contract consumer: {consumer}")
    return contract_paths(project_root)["consumers"] / f"{consumer}.completed.json"


def locked_contract_binding(project_root: Path | str, consumer: str) -> dict[str, Any]:
    """Load a fully reviewed lock and return a fine-grained consumer view."""

    if consumer not in CONTRACT_CONSUMER_SECTIONS:
        raise ValueError(f"unknown story contract consumer: {consumer}")
    root = Path(project_root)
    manifest_path = root / "99_项目状态" / "project_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"project manifest unavailable: {exc}") from exc
    if legacy_passthrough_allowed(manifest):
        return {
            "version": 1,
            "mode": CONTRACT_POLICY_LEGACY,
            "consumer": consumer,
            "contract_schema_version": "legacy-v3",
            "story_contract_sha256": "",
            "story_contract_dependency_sha256": "legacy-v3",
            "contract_sections": list(CONTRACT_CONSUMER_SECTIONS[consumer]),
            "contract_projection": {},
        }
    runtime = manifest.get("agent", {}).get("story_contract", {})
    if runtime.get("policy") != CONTRACT_POLICY_REQUIRED:
        raise ValueError("manifest is neither an eligible V3 legacy project nor required_v1")
    issues = contract_runtime_issues(root)
    if issues:
        raise ValueError("story contract invalid: " + ";".join(issues[:12]))
    paths = contract_paths(root)
    if not _review_bundle_is_current(paths["bundle"]):
        raise ValueError("story contract review bundle is missing or stale")
    try:
        review_payload = json.loads(paths["review"].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"story contract review is invalid: {exc}") from exc
    if not _review_payload_passes(review_payload, paths["bundle"]):
        raise ValueError("story contract independent review is not approved/hash-bound")
    if contract_review_payload_issues(review_payload):
        raise ValueError("story contract independent review lacks seven-section evidence")
    if not contract_lock_is_current(root, bundle=paths["bundle"], review=paths["review"]):
        raise ValueError("story contract lock is missing, damaged, incomplete, or stale")
    contract = load_story_contract(paths["contract"])
    sections = CONTRACT_CONSUMER_SECTIONS[consumer]
    projection = {name: contract["contracts"][name] for name in sections}
    dependency_payload = {
        "contract_schema_version": str(contract["schema_version"]),
        "consumer": consumer,
        "contract_projection": projection,
    }
    return {
        "version": 1,
        "mode": CONTRACT_POLICY_REQUIRED,
        "consumer": consumer,
        "contract_schema_version": str(contract["schema_version"]),
        "story_contract_sha256": contract_sha256(contract),
        "story_contract_dependency_sha256": _json_sha256(dependency_payload),
        "contract_sections": list(sections),
        "contract_projection": projection,
    }


def write_contract_consumer_context(project_root: Path | str, consumer: str) -> Path:
    """Persist a request/plan binding without rewriting an equivalent output."""

    path = contract_consumer_path(project_root, consumer)
    expected = locked_contract_binding(project_root, consumer)
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        current = None
    # Preserve the original full-contract audit hash when this output family's
    # dependency projection is unchanged.  That records which reviewed
    # contract actually produced the artifact while avoiding unrelated redo.
    if (
        isinstance(current, Mapping)
        and _consumer_context_shape_is_complete(current, consumer)
        and current.get("contract_schema_version") == expected["contract_schema_version"]
        and current.get("story_contract_dependency_sha256") == expected["story_contract_dependency_sha256"]
        and current.get("contract_projection") == expected["contract_projection"]
    ):
        return path
    save_json(path, expected)
    return path


def contract_consumer_context_is_current(project_root: Path | str, consumer: str, path: Path | None = None) -> bool:
    target = path or contract_consumer_path(project_root, consumer)
    try:
        current = json.loads(target.read_text(encoding="utf-8"))
        expected = locked_contract_binding(project_root, consumer)
    except (OSError, ValueError, json.JSONDecodeError, StoryContractValidationError):
        return False
    return (
        _consumer_context_shape_is_complete(current, consumer)
        and current.get("contract_schema_version") == expected["contract_schema_version"]
        and current.get("story_contract_dependency_sha256") == expected["story_contract_dependency_sha256"]
        and current.get("contract_projection") == expected["contract_projection"]
    )


def mark_contract_consumer_completed(project_root: Path | str, consumer: str) -> Path:
    """Atomically bind a successfully produced output family to its request."""

    context = contract_consumer_path(project_root, consumer)
    if not contract_consumer_context_is_current(project_root, consumer, context):
        raise ValueError(f"cannot complete {consumer}: request context is missing or stale")
    payload = json.loads(context.read_text(encoding="utf-8"))
    receipt = {
        "version": 1,
        "consumer": consumer,
        "contract_schema_version": payload["contract_schema_version"],
        "story_contract_sha256": payload["story_contract_sha256"],
        "story_contract_dependency_sha256": payload["story_contract_dependency_sha256"],
        "request_manifest_sha256": _file_sha256(context),
    }
    target = contract_consumer_receipt_path(project_root, consumer)
    _write_crash_safe_json(target, receipt)
    return target


def contract_consumer_completion_is_current(project_root: Path | str, consumer: str) -> bool:
    context = contract_consumer_path(project_root, consumer)
    receipt = contract_consumer_receipt_path(project_root, consumer)
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        context_payload = json.loads(context.read_text(encoding="utf-8"))
        expected = locked_contract_binding(project_root, consumer)
    except (OSError, ValueError, json.JSONDecodeError, StoryContractValidationError):
        return False
    return (
        contract_consumer_context_is_current(project_root, consumer, context)
        and set(payload) == {
            "version", "consumer", "contract_schema_version", "story_contract_sha256",
            "story_contract_dependency_sha256", "request_manifest_sha256",
        }
        and payload.get("version") == 1
        and payload.get("consumer") == consumer
        and payload.get("contract_schema_version") == expected["contract_schema_version"]
        and payload.get("story_contract_sha256") == context_payload.get("story_contract_sha256")
        and payload.get("story_contract_dependency_sha256") == expected["story_contract_dependency_sha256"]
        and payload.get("request_manifest_sha256") == _file_sha256(context)
    )


def contract_diagnostics(
    project_root: Path | str, manifest: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Return a read-only, failure-tolerant view of contract and consumer state.

    This deliberately reuses the same validators as production gates.  It
    never repairs files, creates legacy receipts, or treats a recorded
    ``status=locked`` value as evidence that a lock is current.
    """

    root = Path(project_root)
    paths = contract_paths(root)
    if manifest is None:
        try:
            loaded = json.loads((root / "99_项目状态" / "project_manifest.json").read_text(encoding="utf-8"))
            manifest = loaded if isinstance(loaded, Mapping) else {}
        except (OSError, json.JSONDecodeError):
            manifest = {}
    agent = manifest.get("agent") if isinstance(manifest.get("agent"), Mapping) else {}
    runtime = agent.get("story_contract") if isinstance(agent.get("story_contract"), Mapping) else {}
    recorded_policy = str(runtime.get("policy") or CONTRACT_POLICY_REQUIRED)
    legacy = legacy_passthrough_allowed(manifest)

    contract: Mapping[str, Any] | None = None
    runtime_issues: list[str] = []
    try:
        contract = load_story_contract(paths["contract"])
        runtime_issues = contract_runtime_issues(root)
    except StoryContractValidationError as exc:
        runtime_issues = [f"{issue.path}:{issue.code}:{issue.message}" for issue in exc.issues]

    bundle_current = _review_bundle_is_current(paths["bundle"])
    review_payload: Mapping[str, Any] | None = None
    try:
        loaded_review = json.loads(paths["review"].read_text(encoding="utf-8"))
        review_payload = loaded_review if isinstance(loaded_review, Mapping) else None
    except (OSError, json.JSONDecodeError):
        review_payload = None
    review_evidence_issues = contract_review_payload_issues(review_payload or {})
    review_current = bool(
        bundle_current
        and review_payload is not None
        and _review_payload_passes(review_payload, paths["bundle"])
        and not review_evidence_issues
    )
    lock_current = bool(
        review_current
        and not runtime_issues
        and contract_lock_is_current(root, bundle=paths["bundle"], review=paths["review"])
    )

    consumers: dict[str, Any] = {}
    for consumer, sections in CONTRACT_CONSUMER_SECTIONS.items():
        request_path = contract_consumer_path(root, consumer)
        receipt_path = contract_consumer_receipt_path(root, consumer)
        if legacy:
            consumers[consumer] = {
                "sections": list(sections),
                "request_manifest": "legacy_passthrough",
                "completed_receipt": "legacy_passthrough",
                "changed_sections": [],
            }
            continue
        request_state, request_payload = _diagnostic_json_state(request_path)
        if request_state == "present":
            if not _consumer_context_shape_is_complete(request_payload, consumer):
                request_state = "damaged"
            elif contract_consumer_context_is_current(root, consumer, request_path):
                request_state = "current"
            else:
                request_state = "stale"
        receipt_state, receipt_payload = _diagnostic_json_state(receipt_path)
        if receipt_state == "present":
            if not _consumer_receipt_shape_is_complete(receipt_payload, consumer):
                receipt_state = "damaged"
            elif contract_consumer_completion_is_current(root, consumer):
                receipt_state = "current"
            else:
                receipt_state = "stale"
        changed_sections: list[str] = []
        old_projection = request_payload.get("contract_projection") if isinstance(request_payload, Mapping) else None
        current_contracts = contract.get("contracts") if isinstance(contract, Mapping) else None
        if isinstance(old_projection, Mapping) and isinstance(current_contracts, Mapping):
            changed_sections = [
                section
                for section in sections
                if old_projection.get(section) != current_contracts.get(section)
            ]
        consumers[consumer] = {
            "sections": list(sections),
            "request_manifest": request_state,
            "completed_receipt": receipt_state,
            "changed_sections": changed_sections,
        }

    contract_valid = bool(contract is not None and not runtime_issues)
    return {
        "policy": CONTRACT_POLICY_LEGACY if legacy else recorded_policy,
        "legacy_eligible": legacy,
        "contract": {
            "exists": paths["contract"].is_file(),
            "valid": contract_valid,
            "schema_version": str(contract.get("schema_version") or "") if contract else "",
            "sha256": contract_sha256(contract) if contract_valid and contract else "",
            "file_sha256": _file_sha256(paths["contract"]) if paths["contract"].is_file() else "",
            "issues": runtime_issues,
        },
        "review": {
            "bundle_exists": paths["bundle"].is_file(),
            "bundle_current": bundle_current,
            "review_exists": paths["review"].is_file(),
            "current_and_approved": review_current,
            "evidence_issues": review_evidence_issues,
        },
        "lock": {
            "exists": paths["lock"].is_file(),
            "valid": lock_current,
        },
        "consumers": consumers,
    }


def assert_request_contract_binding(project_root: Path | str, consumer: str, request: Mapping[str, Any]) -> dict[str, Any]:
    """Revalidate the live lock and a request row immediately before payment."""

    expected = locked_contract_binding(project_root, consumer)
    if expected["mode"] == CONTRACT_POLICY_LEGACY:
        return expected
    if str(request.get("contract_schema_version") or "") != expected["contract_schema_version"]:
        raise ValueError("request contract_schema_version does not match the current locked contract")
    if str(request.get("story_contract_dependency_sha256") or "") != expected["story_contract_dependency_sha256"]:
        raise ValueError("request story contract dependency hash is stale")
    context_path = contract_consumer_path(project_root, consumer)
    if not contract_consumer_context_is_current(project_root, consumer, context_path):
        raise ValueError("consumer request manifest is missing, damaged, or stale")
    context = json.loads(context_path.read_text(encoding="utf-8"))
    recorded_full = str(request.get("story_contract_sha256") or "")
    if recorded_full != context.get("story_contract_sha256"):
        raise ValueError("request story_contract_sha256 does not match its audited request manifest")
    return expected


def _write_crash_safe_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Durably replace a deterministic JSON lock in the same directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(encoded)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temp_path.unlink(missing_ok=True)


def _lock_shape_is_complete(payload: Any) -> bool:
    required = {
        "version",
        "schema_version",
        "contract_id",
        "contract_file_sha256",
        "contract_canonical_sha256",
        "trusted_inputs_sha256",
        "review_bundle_sha256",
        "review_sha256",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        return False
    return (
        payload.get("version") == LOCK_VERSION
        and all(str(payload.get(key) or "") for key in ("schema_version", "contract_id"))
        and all(
            len(str(payload.get(key) or "")) == 64
            for key in required
            if key.endswith("sha256")
        )
    )


def _consumer_context_shape_is_complete(payload: Any, consumer: str) -> bool:
    required = {
        "version", "mode", "consumer", "contract_schema_version",
        "story_contract_sha256", "story_contract_dependency_sha256",
        "contract_sections", "contract_projection",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        return False
    return (
        payload.get("version") == 1
        and payload.get("mode") == CONTRACT_POLICY_REQUIRED
        and payload.get("consumer") == consumer
        and payload.get("contract_sections") == list(CONTRACT_CONSUMER_SECTIONS[consumer])
        and isinstance(payload.get("contract_projection"), Mapping)
        and len(str(payload.get("story_contract_sha256") or "")) == 64
        and len(str(payload.get("story_contract_dependency_sha256") or "")) == 64
    )


def _consumer_receipt_shape_is_complete(payload: Any, consumer: str) -> bool:
    required = {
        "version", "consumer", "contract_schema_version", "story_contract_sha256",
        "story_contract_dependency_sha256", "request_manifest_sha256",
    }
    return (
        isinstance(payload, Mapping)
        and set(payload) == required
        and payload.get("version") == 1
        and payload.get("consumer") == consumer
        and len(str(payload.get("story_contract_sha256") or "")) == 64
        and len(str(payload.get("story_contract_dependency_sha256") or "")) == 64
        and len(str(payload.get("request_manifest_sha256") or "")) == 64
    )


def _diagnostic_json_state(path: Path) -> tuple[str, Mapping[str, Any]]:
    if not path.exists():
        return "missing", {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "damaged", {}
    return ("present", payload) if isinstance(payload, Mapping) else ("damaged", {})


def _review_bundle_is_current(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    artifacts = payload.get("artifacts") if isinstance(payload, Mapping) else None
    if not isinstance(artifacts, list) or not artifacts:
        return False
    for item in artifacts:
        if not isinstance(item, Mapping):
            return False
        target = Path(str(item.get("path") or ""))
        if not target.is_file() or item.get("sha256") != _file_sha256(target):
            return False
    return True


def _review_payload_passes(payload: Mapping[str, Any], bundle: Path) -> bool:
    try:
        score = float(payload.get("score") or 0)
    except (TypeError, ValueError):
        return False
    return (
        payload.get("approved") is True
        and score >= 85
        and not payload.get("critical_errors")
        and payload.get("artifact_sha256") == _file_sha256(bundle)
    )


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
    "CONTRACT_CONSUMER_SECTIONS",
    "V3_BASELINE_COMMIT",
    "V3_BASELINE_TAG",
    "assert_request_contract_binding",
    "build_trusted_input_chain",
    "contract_lock_is_current",
    "contract_consumer_context_is_current",
    "contract_consumer_completion_is_current",
    "contract_consumer_path",
    "contract_consumer_receipt_path",
    "contract_paths",
    "contract_review_artifacts",
    "contract_review_payload_issues",
    "contract_runtime_issues",
    "expected_contract_lock",
    "legacy_eligibility_receipt",
    "legacy_passthrough_allowed",
    "mark_contract_consumer_completed",
    "locked_contract_binding",
    "write_contract_lock",
    "write_contract_consumer_context",
    "write_trusted_input_chain",
]
