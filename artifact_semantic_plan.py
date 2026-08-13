"""Deterministically compile per-artifact presentation from a locked Story Contract.

The Story Production Contract remains the sole source of truth.  This module
only compiles its ``semantic_artifacts`` mappings into an executable, hash-
bound plan.  Loading a plan always revalidates the lock and recompiles it, so
partial writes, hand edits, stale sources, and stale contracts are rejected.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from story_contract_consumers import write_json_atomic
from story_contract_runtime import CONTRACT_POLICY_LEGACY, contract_paths, locked_contract_binding
from story_contracts import canonical_json_bytes, contract_sha256, load_story_contract
from story_semantics import SemanticKind, classify_story


PLAN_SCHEMA_VERSION = "1.0"
PLAN_COMPILER_VERSION = "1"
PLAN_SCHEMA_PATH = Path(__file__).with_name("schemas") / "artifact_semantic_plan" / "v1" / "artifact_semantic_plan.schema.json"
ARTIFACTS = (
    "demo_subtitles", "background_visual", "background_subtitles",
    "sales_subtitles", "ppt", "customer_manuscript", "reading_annotation",
)
ARTIFACT_ALIASES = {"demo_subtitles": ("demo_subtitles", "demo")}
ACTIONS = frozenset({"include", "exclude", "visual_substitute"})
SUBTITLE_POLICIES = frozenset({"show", "hide", "inherit"})
PROVENANCE_SOURCES = frozenset({"task_input", "project_config", "brand_or_global_default", "agent_inference"})
CARD_KINDS = {SemanticKind.TITLE.value: "title_card", SemanticKind.MORAL.value: "moral_card"}


def semantic_plan_path(project_root: Path | str) -> Path:
    return Path(project_root) / "99_项目状态" / "story_contract" / "artifact_semantic_plan.json"


def semantic_plan_sha256(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compile_artifact_semantic_plan(
    project_root: Path | str,
    semantic_source: Path | str,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(semantic_source).resolve()
    binding = locked_contract_binding(root, "product_package")
    if binding.get("mode") == CONTRACT_POLICY_LEGACY:
        raise ValueError("legacy_passthrough projects do not create artifact semantic plans")
    contract = load_story_contract(contract_paths(root)["contract"])
    lines = _source_lines(source)
    semantics = classify_story(lines)
    present: dict[str, list[int]] = {}
    for line_number in range(1, len(lines) + 1):
        present.setdefault(semantics.kind_at(line_number).value, []).append(line_number)

    section = contract["contracts"]["semantic_artifacts"]
    mappings = section.get("mappings")
    if not isinstance(mappings, list):
        raise ValueError("semantic_artifacts.mappings must be an array")
    lookup: dict[tuple[str, str], tuple[int, Mapping[str, Any]]] = {}
    for index, raw in enumerate(mappings):
        if not isinstance(raw, Mapping):
            raise ValueError(f"semantic_artifacts.mappings[{index}] must be an object")
        key = (str(raw.get("semantic_kind") or ""), str(raw.get("artifact") or ""))
        if key in lookup:
            raise ValueError(f"ambiguous semantic mapping: {key[0]}/{key[1]}")
        lookup[key] = (index, raw)

    artifacts: dict[str, Any] = {}
    for artifact in ARTIFACTS:
        decisions = []
        for kind, line_numbers in present.items():
            names = ARTIFACT_ALIASES.get(artifact, (artifact,))
            candidates = [lookup[(kind, name)] for name in names if (kind, name) in lookup]
            if len(candidates) > 1:
                raise ValueError(f"ambiguous semantic mapping aliases: {kind}/{artifact}")
            pair = candidates[0] if candidates else None
            if pair is None:
                raise ValueError(f"missing required semantic mapping: {kind}/{artifact}")
            index, mapping = pair
            action = str(mapping.get("action") or "")
            policy = str(mapping.get("subtitle_policy") or "")
            if action not in ACTIONS or policy not in SUBTITLE_POLICIES:
                raise ValueError(f"invalid semantic mapping: {kind}/{artifact}")
            provenance = mapping.get("provenance")
            _validate_provenance(provenance, f"semantic_artifacts.mappings[{index}].provenance")
            decision: dict[str, Any] = {
                "semantic_kind": kind,
                "source_line_numbers": line_numbers,
                "action": action,
                "subtitle_policy": policy,
                "provenance": dict(provenance),
                "source_mapping_index": index,
            }
            if mapping.get("visual_substitute"):
                decision["visual_substitute"] = str(mapping["visual_substitute"])
            if mapping.get("mutual_exclusion_group"):
                decision["mutual_exclusion_group"] = str(mapping["mutual_exclusion_group"])
            if action == "visual_substitute":
                if kind not in CARD_KINDS:
                    raise ValueError(f"unsupported deterministic visual_substitute semantic kind: {kind}")
                if not decision.get("visual_substitute") or not decision.get("mutual_exclusion_group"):
                    raise ValueError(f"visual_substitute requires target and mutual_exclusion_group: {kind}/{artifact}")
            decisions.append(decision)
        artifacts[artifact] = {"decisions": decisions}

    _validate_mutual_exclusion(artifacts)
    cards = _compile_visual_cards(lines, artifacts["background_visual"]["decisions"])
    prefix = []
    for line_number in range(1, len(lines) + 1):
        kind = semantics.kind_at(line_number)
        if kind is SemanticKind.STORY_BODY:
            break
        prefix.append(line_number)

    projection = {
        "story": {"title": str(contract.get("story", {}).get("title") or "")},
        "semantic_artifacts": section,
    }
    dependency_payload = {
        "contract_schema_version": str(contract["schema_version"]),
        "consumer": "artifact_semantic_plan",
        "contract_projection": projection,
    }
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "compiler_version": PLAN_COMPILER_VERSION,
        "story_contract_sha256": contract_sha256(contract),
        "contract_schema_version": str(contract["schema_version"]),
        "story_contract_dependency_sha256": _sha_json(dependency_payload),
        "contract_projection_sha256": _sha_json(projection),
        "semantic_source": {
            "project_relative_path": _relative(source, root),
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "line_count": len(lines),
        },
        "artifacts": artifacts,
        "visual_cards": cards,
        "pre_roll_diagnostic": {
            "suspected": bool(prefix),
            "source_line_numbers": prefix,
            "action": "record_only_no_auto_trim",
        },
    }


def write_artifact_semantic_plan(project_root: Path | str, semantic_source: Path | str) -> Path:
    plan = compile_artifact_semantic_plan(project_root, semantic_source)
    validate_artifact_semantic_plan_or_raise(plan)
    return write_json_atomic(semantic_plan_path(project_root), plan)


def load_current_artifact_semantic_plan(
    project_root: Path | str,
    semantic_source: Path | str | None = None,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    path = semantic_plan_path(root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"artifact semantic plan missing or damaged: {exc}") from exc
    validate_artifact_semantic_plan_or_raise(payload)
    recorded = payload["semantic_source"]
    source = Path(semantic_source).resolve() if semantic_source else root / str(recorded["project_relative_path"])
    expected = compile_artifact_semantic_plan(root, source)
    if canonical_json_bytes(payload) != canonical_json_bytes(expected):
        raise ValueError("artifact semantic plan is stale or was modified")
    return payload


def artifact_semantic_plan_is_current(project_root: Path | str, semantic_source: Path | str | None = None) -> bool:
    try:
        load_current_artifact_semantic_plan(project_root, semantic_source)
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def plan_binding(plan_path: Path | str, payload: Mapping[str, Any] | None = None) -> dict[str, str]:
    plan = dict(payload) if payload is not None else json.loads(Path(plan_path).read_text(encoding="utf-8"))
    return {
        "artifact_semantic_plan_sha256": semantic_plan_sha256(plan_path),
        "artifact_semantic_plan_schema_version": str(plan["schema_version"]),
        "artifact_semantic_plan_dependency_sha256": str(plan["story_contract_dependency_sha256"]),
    }


def selected_line_indices(lines: Sequence[str], plan: Mapping[str, Any], artifact: str) -> list[int]:
    if artifact not in ARTIFACTS:
        raise ValueError(f"unknown semantic artifact: {artifact}")
    expected_count = int(plan["semantic_source"]["line_count"])
    if len(lines) != expected_count:
        raise ValueError(
            f"semantic artifact source line count mismatch: expected {expected_count}, got {len(lines)}"
        )
    decisions = plan["artifacts"][artifact]["decisions"]
    selected: set[int] = set()
    for decision in decisions:
        action = decision["action"]
        policy = decision["subtitle_policy"]
        if action == "include" and policy != "hide":
            selected.update(int(number) - 1 for number in decision["source_line_numbers"])
        elif action == "visual_substitute" and policy == "show":
            selected.update(int(number) - 1 for number in decision["source_line_numbers"])
    if any(index < 0 or index >= len(lines) for index in selected):
        raise ValueError(f"{artifact} selects a line outside the semantic source")
    return sorted(selected)


def select_timings_for_artifact(timings: Sequence[Any], plan: Mapping[str, Any], artifact: str) -> list[Any]:
    """Select timeline rows without altering their original timestamps."""

    indices = selected_line_indices([str(item.line) for item in timings], plan, artifact)
    return [timings[index] for index in indices]


def presentation_windows(timings: Sequence[Any], plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Compile deterministic title/moral card windows from current cue timing."""

    if not timings:
        return []
    if len(timings) != int(plan["semantic_source"]["line_count"]):
        raise ValueError("semantic presentation timing count does not match the current plan")
    by_line_number = {index: item for index, item in enumerate(timings, start=1)}
    visual_decisions = {
        str(decision["semantic_kind"]): decision
        for decision in plan["artifacts"]["background_visual"]["decisions"]
    }
    body_numbers = visual_decisions.get(SemanticKind.STORY_BODY.value, {}).get("source_line_numbers", [])
    windows = []
    for card in plan.get("visual_cards", []):
        kind = str(card["semantic_kind"])
        if kind == SemanticKind.TITLE.value:
            body = [by_line_number[number] for number in body_numbers if number in by_line_number]
            end = float(body[0].source_start if body else timings[-1].source_end)
            start = 0.0
        else:
            rows = [by_line_number[number] for number in card["source_line_numbers"] if number in by_line_number]
            if not rows:
                continue
            start = float(rows[0].source_start)
            end = float(rows[-1].source_end)
        if end > start:
            windows.append({**dict(card), "start": start, "end": end})
    return windows


def validate_artifact_semantic_plan_or_raise(payload: Mapping[str, Any]) -> None:
    required = {
        "schema_version", "compiler_version", "story_contract_sha256", "contract_schema_version",
        "story_contract_dependency_sha256", "contract_projection_sha256", "semantic_source",
        "artifacts", "visual_cards", "pre_roll_diagnostic",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise ValueError("artifact semantic plan has missing or unexpected top-level fields")
    if payload["schema_version"] != PLAN_SCHEMA_VERSION or payload["compiler_version"] != PLAN_COMPILER_VERSION:
        raise ValueError("artifact semantic plan schema/compiler version mismatch")
    if not isinstance(payload.get("contract_schema_version"), str) or not payload["contract_schema_version"]:
        raise ValueError("contract_schema_version invalid")
    for field in ("story_contract_sha256", "story_contract_dependency_sha256", "contract_projection_sha256"):
        _require_sha(payload.get(field), field)
    source = payload.get("semantic_source")
    if not isinstance(source, Mapping) or set(source) != {"project_relative_path", "sha256", "line_count"}:
        raise ValueError("semantic_source shape invalid")
    _require_sha(source.get("sha256"), "semantic_source.sha256")
    if not str(source.get("project_relative_path") or "") or not isinstance(source.get("line_count"), int) or source["line_count"] < 1:
        raise ValueError("semantic_source fields invalid")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != set(ARTIFACTS):
        raise ValueError("artifact plans must cover the complete artifact set")
    for artifact, artifact_plan in artifacts.items():
        if not isinstance(artifact_plan, Mapping) or set(artifact_plan) != {"decisions"} or not artifact_plan["decisions"]:
            raise ValueError(f"{artifact} decisions invalid")
        for decision in artifact_plan["decisions"]:
            required_decision = {
                "semantic_kind", "source_line_numbers", "action", "subtitle_policy",
                "provenance", "source_mapping_index",
            }
            optional_decision = {"visual_substitute", "mutual_exclusion_group"}
            if not isinstance(decision, Mapping) or not required_decision.issubset(decision) or not set(decision).issubset(required_decision | optional_decision):
                raise ValueError(f"{artifact} decision shape invalid")
            if not isinstance(decision.get("semantic_kind"), str) or not decision["semantic_kind"]:
                raise ValueError(f"{artifact} semantic_kind invalid")
            if decision.get("action") not in ACTIONS or decision.get("subtitle_policy") not in SUBTITLE_POLICIES:
                raise ValueError(f"{artifact} decision enum invalid")
            if (
                not isinstance(decision.get("source_line_numbers"), list)
                or not decision["source_line_numbers"]
                or any(not isinstance(number, int) or number < 1 for number in decision["source_line_numbers"])
            ):
                raise ValueError(f"{artifact} decision source lines invalid")
            if not isinstance(decision.get("source_mapping_index"), int) or decision["source_mapping_index"] < 0:
                raise ValueError(f"{artifact} source_mapping_index invalid")
            for optional in optional_decision:
                if optional in decision and (not isinstance(decision[optional], str) or not decision[optional]):
                    raise ValueError(f"{artifact} {optional} invalid")
            _validate_provenance(decision.get("provenance"), f"{artifact}.provenance")
    _validate_mutual_exclusion(artifacts)
    cards = payload.get("visual_cards")
    if not isinstance(cards, list):
        raise ValueError("visual_cards invalid")
    card_fields = {
        "semantic_kind", "card_kind", "text", "source_line_numbers",
        "mutual_exclusion_group", "timing_rule",
    }
    for card in cards:
        if not isinstance(card, Mapping) or set(card) != card_fields:
            raise ValueError("visual card shape invalid")
        kind = card.get("semantic_kind")
        if kind not in CARD_KINDS or card.get("card_kind") != CARD_KINDS[kind]:
            raise ValueError("visual card kind invalid")
        expected_timing = "until_first_story_body_cue" if kind == SemanticKind.TITLE.value else "semantic_cue_range"
        if card.get("timing_rule") != expected_timing:
            raise ValueError("visual card timing rule invalid")
        if not isinstance(card.get("text"), str) or not card["text"] or not isinstance(card.get("mutual_exclusion_group"), str) or not card["mutual_exclusion_group"]:
            raise ValueError("visual card content invalid")
        if not isinstance(card.get("source_line_numbers"), list) or not card["source_line_numbers"] or any(not isinstance(number, int) or number < 1 for number in card["source_line_numbers"]):
            raise ValueError("visual card source lines invalid")
    diagnostic = payload.get("pre_roll_diagnostic")
    if (
        not isinstance(diagnostic, Mapping)
        or set(diagnostic) != {"suspected", "source_line_numbers", "action"}
        or not isinstance(diagnostic.get("suspected"), bool)
        or not isinstance(diagnostic.get("source_line_numbers"), list)
        or any(not isinstance(number, int) or number < 1 for number in diagnostic["source_line_numbers"])
        or diagnostic.get("action") != "record_only_no_auto_trim"
    ):
        raise ValueError("pre_roll_diagnostic invalid")


def schema_python_parity() -> list[str]:
    schema = json.loads(PLAN_SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = []
    if set(schema.get("required", [])) != {
        "schema_version", "compiler_version", "story_contract_sha256", "contract_schema_version",
        "story_contract_dependency_sha256", "contract_projection_sha256", "semantic_source",
        "artifacts", "visual_cards", "pre_roll_diagnostic",
    }:
        errors.append("top-level required fields differ")
    artifact_schema = schema["properties"]["artifacts"]
    if set(artifact_schema.get("required", [])) != set(ARTIFACTS):
        errors.append("artifact enum differs")
    decision = schema["$defs"]["decision"]["properties"]
    if set(decision["action"]["enum"]) != set(ACTIONS):
        errors.append("action enum differs")
    if set(decision["subtitle_policy"]["enum"]) != set(SUBTITLE_POLICIES):
        errors.append("subtitle policy enum differs")
    if schema["properties"]["schema_version"].get("const") != PLAN_SCHEMA_VERSION:
        errors.append("schema version differs")
    if schema["properties"]["compiler_version"].get("const") != PLAN_COMPILER_VERSION:
        errors.append("compiler version differs")
    provenance = schema["$defs"]["provenance"]["properties"]
    if set(provenance["source"]["enum"]) != set(PROVENANCE_SOURCES):
        errors.append("provenance source enum differs")
    card = schema["$defs"]["visualCard"]["properties"]
    if set(card["semantic_kind"]["enum"]) != set(CARD_KINDS):
        errors.append("card semantic kind enum differs")
    if set(card["card_kind"]["enum"]) != set(CARD_KINDS.values()):
        errors.append("card kind enum differs")
    return errors


def _compile_visual_cards(lines: Sequence[str], decisions: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    cards = []
    for decision in decisions:
        if decision["action"] != "visual_substitute":
            continue
        kind = decision["semantic_kind"]
        line_numbers = decision["source_line_numbers"]
        cards.append({
            "semantic_kind": kind,
            "card_kind": CARD_KINDS[kind],
            "text": "\n".join(lines[number - 1] for number in line_numbers),
            "source_line_numbers": line_numbers,
            "mutual_exclusion_group": decision["mutual_exclusion_group"],
            "timing_rule": "until_first_story_body_cue" if kind == SemanticKind.TITLE.value else "semantic_cue_range",
        })
    return cards


def _validate_mutual_exclusion(artifacts: Mapping[str, Any]) -> None:
    visual = {d["semantic_kind"]: d for d in artifacts["background_visual"]["decisions"]}
    subtitles = {d["semantic_kind"]: d for d in artifacts["background_subtitles"]["decisions"]}
    for kind, visual_decision in visual.items():
        if visual_decision["action"] != "visual_substitute":
            continue
        subtitle = subtitles.get(kind)
        if subtitle is None:
            raise ValueError(f"missing background subtitle mutual exclusion member: {kind}")
        group = visual_decision.get("mutual_exclusion_group")
        if not group or subtitle.get("mutual_exclusion_group") != group or subtitle.get("subtitle_policy") != "hide":
            raise ValueError(f"visual card and subtitle are not uniquely mutually exclusive: {kind}")
        if subtitle.get("action") == "visual_substitute":
            raise ValueError(f"multiple visible candidates in mutual exclusion group: {group}")


def _source_lines(path: Path) -> list[str]:
    if not path.is_file():
        raise ValueError(f"semantic source missing: {path}")
    lines = [line.strip() for line in path.read_text(encoding="utf-8-sig", errors="strict").splitlines() if line.strip()]
    if not lines:
        raise ValueError("semantic source has no non-empty lines")
    return lines


def _validate_provenance(value: Any, path: str) -> None:
    if not isinstance(value, Mapping) or value.get("source") not in PROVENANCE_SOURCES:
        raise ValueError(f"{path} invalid source")
    if not str(value.get("source_ref") or ""):
        raise ValueError(f"{path} missing source_ref")
    if value.get("source") != "agent_inference":
        _require_sha(value.get("source_sha256"), f"{path}.source_sha256")


def _require_sha(value: Any, field: str) -> None:
    text = str(value or "")
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ValueError(f"{field} must be a lowercase SHA-256")


def _sha_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("semantic source must be inside the project") from exc
