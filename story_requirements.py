"""Small, hash-bound requirement projection shared by producer and reviewer."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "story-applicable-requirements/v1"
APPLICABILITY_KEYS = {"accounts", "shots", "artifacts"}


def file_sha256(path: Path) -> str:
    from story_hash_cache import sha256_file
    return sha256_file(path)


def binding(path: Path, *, version: str = "") -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    result: dict[str, Any] = {"path": str(resolved), "sha256": file_sha256(resolved)}
    if version:
        result["version"] = version
    return result


def write_projection(
    output: Path,
    *,
    scope: str,
    inputs: Mapping[str, Path],
    rule_sources: list[tuple[Path, str]],
    applicability: Mapping[str, list[str]],
    requirements: list[Mapping[str, Any]],
    executable_checks: list[Mapping[str, Any]],
    acceptance_evidence: list[str],
) -> dict[str, Any]:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "scope": scope,
        "inputs": {name: binding(path) for name, path in sorted(inputs.items())},
        "rule_sources": [binding(path, version=version) for path, version in rule_sources],
        "applicability": {key: sorted(set(values)) for key, values in applicability.items()},
        "requirements": [dict(item) for item in requirements],
        "executable_checks": [dict(item) for item in executable_checks],
        "acceptance_evidence": list(acceptance_evidence),
    }
    validate_projection_payload(payload)
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def _current_binding(item: Mapping[str, Any], label: str) -> None:
    path = Path(str(item.get("path") or "")).expanduser().resolve()
    expected = str(item.get("sha256") or "").lower()
    if len(expected) != 64 or not path.is_file() or file_sha256(path) != expected:
        raise ValueError(f"{label}缺失或证据过期：{path}")


def validate_projection_payload(
    payload: Mapping[str, Any], *, parameters: Mapping[str, Any] | None = None,
    consumer_scope: Mapping[str, list[str]] | None = None,
) -> dict[str, Any]:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"适用要求投影必须使用 {SCHEMA_VERSION}")
    if not str(payload.get("scope") or "").strip():
        raise ValueError("适用要求投影缺少 scope")
    inputs = payload.get("inputs")
    sources = payload.get("rule_sources")
    applicability = payload.get("applicability")
    if not isinstance(inputs, dict) or not inputs:
        raise ValueError("适用要求投影缺少输入绑定")
    if not isinstance(sources, list) or not sources:
        raise ValueError("适用要求投影缺少规则来源")
    if not isinstance(applicability, dict) or not any(applicability.values()):
        raise ValueError("适用要求投影缺少账号/镜头/产物范围")
    unknown_scope = set(applicability) - APPLICABILITY_KEYS
    if unknown_scope:
        raise ValueError("适用要求投影含未知范围：" + ", ".join(sorted(unknown_scope)))
    for key, values in applicability.items():
        if not isinstance(values, list) or not values or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise ValueError(f"适用要求投影 applicability.{key} 必须是非空字符串列表")
    if consumer_scope is not None:
        for key, requested in consumer_scope.items():
            if key not in APPLICABILITY_KEYS:
                raise ValueError(f"生产入口传入未知适用范围：{key}")
            allowed = set(applicability.get(key) or [])
            outside = sorted(set(requested) - allowed)
            if outside:
                raise ValueError(
                    f"适用要求投影范围错用：{key} " + ", ".join(outside)
                )
    for name, item in inputs.items():
        if not isinstance(item, Mapping):
            raise ValueError(f"输入绑定无效：{name}")
        _current_binding(item, f"输入 {name}")
    for index, item in enumerate(sources):
        if not isinstance(item, Mapping) or not str(item.get("version") or "").strip():
            raise ValueError(f"规则来源[{index}]缺少版本")
        _current_binding(item, f"规则来源[{index}]")
    requirements = payload.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise ValueError("适用要求投影缺少实际要求")
    for index, item in enumerate(requirements):
        if not isinstance(item, Mapping) or not all(
            str(item.get(key) or "").strip() for key in ("requirement_id", "source", "scope", "requirement")
        ):
            raise ValueError(f"requirements[{index}]缺少来源、范围或原要求")
    checks = payload.get("executable_checks")
    if not isinstance(checks, list):
        raise ValueError("适用要求投影的 executable_checks 无效")
    for check in checks:
        if (
            not isinstance(check, Mapping)
            or check.get("operator") != "equals"
            or not str(check.get("parameter") or "").strip()
            or "expected" not in check
        ):
            raise ValueError("只允许完整的 equals 确定性参数检查")
        name = str(check["parameter"])
        # One compact projection can serve multiple tools.  Each real entry
        # validates the parameters it actually owns; missing names belong to
        # another producer and are not guessed here.
        if parameters is not None and name in parameters:
            if parameters.get(name) != check.get("expected"):
                raise ValueError(
                    f"长时执行前参数不符合适用要求：{name}="
                    f"{parameters.get(name)!r}，要求 {check.get('expected')!r}"
                )
    evidence = payload.get("acceptance_evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("适用要求投影缺少验收证据类型")
    return dict(payload)


def validate_projection(
    path: Path, *, parameters: Mapping[str, Any] | None = None,
    consumer_scope: Mapping[str, list[str]] | None = None,
    registered_inputs: Mapping[str, Any] | None = None,
    registered_artifacts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"适用要求投影不是有效 JSON：{path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("适用要求投影顶层必须是对象")
    validated = validate_projection_payload(
        payload, parameters=parameters, consumer_scope=consumer_scope,
    )
    if registered_inputs is not None:
        current = {**(registered_artifacts or {}), **registered_inputs}
        for role, item in validated["inputs"].items():
            bound = current.get(role)
            if not isinstance(bound, Mapping):
                raise ValueError(f"投影输入角色未在当前运行登记：{role}")
            _current_binding(bound, f"当前运行输入 {role}")
            if (item["sha256"] != bound.get("sha256") or
                Path(item["path"]).resolve() != Path(str(bound.get("path") or "")).resolve()):
                raise ValueError(f"投影输入与当前运行错配：{role}")
    return validated


def validate_run_projection(
    run_file: Path, *, consumer_scope: Mapping[str, list[str]],
    parameters: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Resolve and validate the current ledger projection at a real consumer."""

    from story_run import file_sha256 as ledger_sha256, load_run

    ledger = load_run(run_file.expanduser().resolve())
    if ledger.get("requirements_policy") != SCHEMA_VERSION:
        raise ValueError("当前运行缺少适用要求策略；请显式补当前输入投影")
    record = ledger.get("artifacts", {}).get("requirements_projection")
    if not isinstance(record, dict):
        raise ValueError("当前生产运行缺少 requirements_projection，已在长时执行前阻断")
    projection = Path(str(record.get("path") or "")).expanduser().resolve()
    if not projection.is_file() or record.get("sha256") != ledger_sha256(projection):
        raise ValueError("当前 requirements_projection 缺失或哈希漂移，已在长时执行前阻断")
    return validate_projection(
        projection, parameters=parameters, consumer_scope=consumer_scope,
        registered_inputs=ledger["inputs"], registered_artifacts=ledger["artifacts"],
    )


__all__ = [
    "SCHEMA_VERSION", "binding", "validate_projection", "validate_run_projection",
    "write_projection",
]
