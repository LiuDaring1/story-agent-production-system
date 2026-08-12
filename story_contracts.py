"""Versioned, machine-verifiable production contracts for story projects.

This module is deliberately independent from the StoryAgent runtime.  It
defines the data boundary that later milestones can generate, review, lock,
and consume without changing any current production behaviour.

The contract keeps story-specific facts in data.  Production code only knows
generic section, relation, and provenance types; it never knows a particular
story title, character, state name, or project path.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


STORY_CONTRACT_SCHEMA_VERSION = "1.0"
SCHEMA_ROOT = Path(__file__).with_name("schemas") / "story_contract" / "v1"
ROOT_SCHEMA_PATH = SCHEMA_ROOT / "story_production_contract.schema.json"


class RuleSource(str, Enum):
    """Where a contract rule came from, ordered separately by SourcePriority."""

    TASK_INPUT = "task_input"
    PROJECT_CONFIG = "project_config"
    BRAND_OR_GLOBAL_DEFAULT = "brand_or_global_default"
    AGENT_INFERENCE = "agent_inference"


class SourcePriority(IntEnum):
    AGENT_INFERENCE = 100
    BRAND_OR_GLOBAL_DEFAULT = 200
    PROJECT_CONFIG = 300
    TASK_INPUT = 400


SOURCE_PRIORITIES: dict[RuleSource, SourcePriority] = {
    RuleSource.TASK_INPUT: SourcePriority.TASK_INPUT,
    RuleSource.PROJECT_CONFIG: SourcePriority.PROJECT_CONFIG,
    RuleSource.BRAND_OR_GLOBAL_DEFAULT: SourcePriority.BRAND_OR_GLOBAL_DEFAULT,
    RuleSource.AGENT_INFERENCE: SourcePriority.AGENT_INFERENCE,
}


class ContractSection(str, Enum):
    SEMANTIC_ARTIFACTS = "semantic_artifacts"
    VISUAL_STYLE = "visual_style"
    CHARACTERS = "characters"
    WORLD_SCALE = "world_scale"
    STORY_STATE = "story_state"
    BRAND = "brand"
    RELEASE_LAYOUT = "release_layout"


REQUIRED_CONTRACT_SECTIONS = tuple(section.value for section in ContractSection)
PREVIEW_KINDS = frozenset({"style_anchor", "character_sheet", "scale_anchor", "layout_preview"})
QUALITATIVE_SCALE_RELATIONS = frozenset(
    {
        "much_smaller",
        "slightly_smaller",
        "similar",
        "slightly_larger",
        "much_larger",
        "environment_reference",
    }
)


@dataclass(frozen=True)
class ContractIssue:
    path: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message}


class StoryContractValidationError(ValueError):
    """Raised when a production contract fails deterministic validation."""

    def __init__(self, issues: Sequence[ContractIssue]):
        self.issues = tuple(issues)
        detail = "; ".join(f"{issue.path}: {issue.message}" for issue in self.issues)
        super().__init__(detail or "Story Production Contract validation failed")


class RuleConflictError(ValueError):
    """Raised only when equal-precedence rules genuinely cannot be resolved."""


@dataclass(frozen=True)
class ResolvedRule:
    rule_id: str
    value: Any
    provenance: Mapping[str, Any]
    overridden: tuple[Mapping[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "value": self.value,
            "provenance": dict(self.provenance),
            "overridden": [dict(item) for item in self.overridden],
        }


@dataclass(frozen=True)
class StoryProductionContract:
    """Validated immutable-by-value view over a contract document."""

    _payload: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, contract: Mapping[str, Any]) -> "StoryProductionContract":
        # Canonical round-tripping detaches this value from caller-owned nested
        # dictionaries and rejects values JSON could not persist.
        try:
            detached = json.loads(canonical_json_bytes(contract).decode("utf-8"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StoryContractValidationError(
                [ContractIssue("$", "json_value", f"contract must contain JSON-compatible values: {exc}")]
            ) from exc
        validate_story_contract_or_raise(detached)
        return cls(detached)

    @classmethod
    def from_file(cls, path: Path | str) -> "StoryProductionContract":
        return cls.from_mapping(load_story_contract(path, validate=False))

    @property
    def schema_version(self) -> str:
        return str(self._payload["schema_version"])

    @property
    def contract_id(self) -> str:
        return str(self._payload["contract_id"])

    @property
    def sha256(self) -> str:
        return contract_sha256(self._payload)

    def section(self, section: ContractSection | str) -> Mapping[str, Any]:
        name = section.value if isinstance(section, ContractSection) else ContractSection(str(section)).value
        # Return another detached value so callers cannot mutate the model.
        return json.loads(canonical_json_bytes(self._payload["contracts"][name]).decode("utf-8"))

    def as_dict(self) -> dict[str, Any]:
        return json.loads(canonical_json_bytes(self._payload).decode("utf-8"))


def canonical_json_bytes(value: Any) -> bytes:
    """Stable JSON representation used by future review and lock hashes."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def contract_sha256(contract: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(contract)).hexdigest()


def source_priority(source: RuleSource | str) -> int:
    try:
        normalized = source if isinstance(source, RuleSource) else RuleSource(str(source))
    except ValueError as exc:
        raise ValueError(f"unknown rule source: {source!r}") from exc
    return int(SOURCE_PRIORITIES[normalized])


def resolve_rule_candidates(candidates: Iterable[Mapping[str, Any]]) -> dict[str, ResolvedRule]:
    """Resolve contract rules using the system-wide provenance precedence.

    Priority is task input > project config > brand/global default > Agent
    inference.  ``source_order`` resolves multiple statements from the same
    source (for example a later explicit task instruction).  A differing tie
    at the same source and source_order is the exceptional ambiguous case and
    raises instead of silently picking an arbitrary value.
    """

    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for candidate in candidates:
        rule_id = str(candidate.get("rule_id") or "").strip()
        if not rule_id:
            raise ValueError("rule candidate requires non-empty rule_id")
        _validate_provenance_or_raise(candidate.get("provenance"), f"rules.{rule_id}.provenance")
        grouped.setdefault(rule_id, []).append(candidate)

    resolved: dict[str, ResolvedRule] = {}
    for rule_id, items in grouped.items():
        ranked = sorted(
            items,
            key=lambda item: (
                source_priority(str(item["provenance"]["source"])),
                int(item["provenance"].get("source_order", 0)),
            ),
            reverse=True,
        )
        winner = ranked[0]
        winner_rank = (
            source_priority(str(winner["provenance"]["source"])),
            int(winner["provenance"].get("source_order", 0)),
        )
        tied = [
            item
            for item in ranked
            if (
                source_priority(str(item["provenance"]["source"])),
                int(item["provenance"].get("source_order", 0)),
            )
            == winner_rank
        ]
        if any(item.get("value") != winner.get("value") for item in tied[1:]):
            raise RuleConflictError(
                f"rule {rule_id!r} has conflicting values at equal source priority and source_order"
            )
        resolved[rule_id] = ResolvedRule(
            rule_id=rule_id,
            value=winner.get("value"),
            provenance=dict(winner["provenance"]),
            overridden=tuple(
                {
                    "value": item.get("value"),
                    "provenance": dict(item["provenance"]),
                }
                for item in ranked[len(tied) :]
            ),
        )
    return resolved


def load_story_contract(path: Path | str, *, validate: bool = True) -> dict[str, Any]:
    contract_path = Path(path).expanduser()
    try:
        payload = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StoryContractValidationError(
            [ContractIssue("$", "invalid_json", f"cannot read contract JSON: {exc}")]
        ) from exc
    if not isinstance(payload, dict):
        raise StoryContractValidationError([ContractIssue("$", "type", "contract root must be an object")])
    if validate:
        validate_story_contract_or_raise(payload)
    return payload


def load_contract_schema(section: ContractSection | str | None = None) -> dict[str, Any]:
    if section is None:
        path = ROOT_SCHEMA_PATH
    else:
        name = section.value if isinstance(section, ContractSection) else ContractSection(str(section)).value
        path = SCHEMA_ROOT / f"{name}.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def validate_story_contract_or_raise(contract: Mapping[str, Any]) -> None:
    issues = validate_story_contract(contract)
    if issues:
        raise StoryContractValidationError(issues)


def validate_story_contract(contract: Mapping[str, Any]) -> list[ContractIssue]:
    """Validate structure and cross-section invariants without extra packages."""

    issues: list[ContractIssue] = []
    if not isinstance(contract, Mapping):
        return [ContractIssue("$", "type", "contract root must be an object")]
    if contract.get("schema_version") != STORY_CONTRACT_SCHEMA_VERSION:
        issues.append(
            ContractIssue(
                "$.schema_version",
                "schema_version",
                f"must equal {STORY_CONTRACT_SCHEMA_VERSION!r}",
            )
        )
    _require_nonempty_string(contract, "contract_id", "$", issues)
    story = _require_mapping(contract, "story", "$", issues)
    if story is not None:
        _require_nonempty_string(story, "story_id", "$.story", issues)
        _require_nonempty_string(story, "title", "$.story", issues)
        source_hash = _require_nonempty_string(story, "source_sha256", "$.story", issues)
        if source_hash and not _is_sha256(source_hash):
            issues.append(ContractIssue("$.story.source_sha256", "sha256", "must be 64 lowercase hex characters"))

    contracts = _require_mapping(contract, "contracts", "$", issues)
    if contracts is None:
        return issues
    for section in REQUIRED_CONTRACT_SECTIONS:
        if section not in contracts:
            issues.append(ContractIssue(f"$.contracts.{section}", "required", "section is required"))
        elif not isinstance(contracts[section], Mapping):
            issues.append(ContractIssue(f"$.contracts.{section}", "type", "section must be an object"))

    for section in REQUIRED_CONTRACT_SECTIONS:
        value = contracts.get(section)
        if isinstance(value, Mapping):
            _validate_rules(value.get("rules", []), f"$.contracts.{section}.rules", issues)

    semantic = contracts.get(ContractSection.SEMANTIC_ARTIFACTS.value)
    if isinstance(semantic, Mapping):
        _validate_semantic_contract(semantic, issues)
    style = contracts.get(ContractSection.VISUAL_STYLE.value)
    if isinstance(style, Mapping):
        _validate_style_contract(style, issues)
    characters = contracts.get(ContractSection.CHARACTERS.value)
    character_ids: set[str] = set()
    if isinstance(characters, Mapping):
        character_ids = _validate_character_contract(characters, issues)
    scale = contracts.get(ContractSection.WORLD_SCALE.value)
    scale_relation_ids: set[str] = set()
    if isinstance(scale, Mapping):
        scale_relation_ids = _validate_scale_contract(scale, character_ids, issues)
    state = contracts.get(ContractSection.STORY_STATE.value)
    if isinstance(state, Mapping):
        _validate_state_contract(state, character_ids, issues)
    brand = contracts.get(ContractSection.BRAND.value)
    if isinstance(brand, Mapping):
        _validate_brand_contract(brand, issues)
    layout = contracts.get(ContractSection.RELEASE_LAYOUT.value)
    layout_variant_ids: set[str] = set()
    if isinstance(layout, Mapping):
        layout_variant_ids = _validate_layout_contract(layout, issues)

    _validate_preview_assets(
        contract.get("preview_assets", []),
        characters=characters if isinstance(characters, Mapping) else {},
        character_ids=character_ids,
        scale_relation_ids=scale_relation_ids,
        layout_variant_ids=layout_variant_ids,
        issues=issues,
    )
    return issues


def _validate_provenance_or_raise(value: object, path: str) -> None:
    issues: list[ContractIssue] = []
    _validate_provenance(value, path, issues)
    if issues:
        raise StoryContractValidationError(issues)


def _validate_provenance(value: object, path: str, issues: list[ContractIssue]) -> None:
    if not isinstance(value, Mapping):
        issues.append(ContractIssue(path, "provenance", "must be an object"))
        return
    source = value.get("source")
    try:
        RuleSource(str(source))
    except ValueError:
        issues.append(
            ContractIssue(path + ".source", "source", f"must be one of {[item.value for item in RuleSource]}")
        )
    source_ref = value.get("source_ref")
    if not isinstance(source_ref, str) or not source_ref.strip():
        issues.append(ContractIssue(path + ".source_ref", "required", "must explain where the rule came from"))
    source_order = value.get("source_order", 0)
    if isinstance(source_order, bool) or not isinstance(source_order, int) or source_order < 0:
        issues.append(ContractIssue(path + ".source_order", "type", "must be a non-negative integer"))


def _validate_rules(value: object, path: str, issues: list[ContractIssue]) -> None:
    if not isinstance(value, list):
        issues.append(ContractIssue(path, "type", "rules must be an array"))
        return
    seen: dict[str, list[Mapping[str, Any]]] = {}
    for index, rule in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(rule, Mapping):
            issues.append(ContractIssue(item_path, "type", "rule must be an object"))
            continue
        rule_id = _require_nonempty_string(rule, "rule_id", item_path, issues)
        if "value" not in rule:
            issues.append(ContractIssue(item_path + ".value", "required", "rule value is required"))
        _validate_provenance(rule.get("provenance"), item_path + ".provenance", issues)
        if rule_id:
            seen.setdefault(rule_id, []).append(rule)
    for rule_id, candidates in seen.items():
        try:
            resolve_rule_candidates(candidates)
        except (RuleConflictError, StoryContractValidationError, ValueError) as exc:
            issues.append(ContractIssue(path, "unresolved_rule_conflict", str(exc)))


def _validate_semantic_contract(value: Mapping[str, Any], issues: list[ContractIssue]) -> None:
    mappings = value.get("mappings")
    path = "$.contracts.semantic_artifacts.mappings"
    if not isinstance(mappings, list) or not mappings:
        issues.append(ContractIssue(path, "required", "must contain at least one semantic mapping"))
        return
    seen: set[tuple[str, str]] = set()
    for index, mapping in enumerate(mappings):
        item_path = f"{path}[{index}]"
        if not isinstance(mapping, Mapping):
            issues.append(ContractIssue(item_path, "type", "mapping must be an object"))
            continue
        kind = _require_nonempty_string(mapping, "semantic_kind", item_path, issues)
        artifact = _require_nonempty_string(mapping, "artifact", item_path, issues)
        action = mapping.get("action")
        if action not in {"include", "exclude", "visual_substitute"}:
            issues.append(ContractIssue(item_path + ".action", "enum", "invalid semantic action"))
        if action == "visual_substitute" and not str(mapping.get("visual_substitute") or "").strip():
            issues.append(
                ContractIssue(item_path + ".visual_substitute", "required", "visual_substitute action needs a target")
            )
        _validate_provenance(mapping.get("provenance"), item_path + ".provenance", issues)
        if kind and artifact:
            key = (kind, artifact)
            if key in seen:
                issues.append(ContractIssue(item_path, "duplicate", "semantic_kind/artifact mapping must be unique"))
            seen.add(key)


def _validate_style_contract(value: Mapping[str, Any], issues: list[ContractIssue]) -> None:
    profile = value.get("style_profile")
    path = "$.contracts.visual_style.style_profile"
    if not isinstance(profile, Mapping):
        issues.append(ContractIssue(path, "required", "style_profile must be an object"))
        return
    _require_nonempty_string(profile, "style_id", path, issues)
    _require_nonempty_string(profile, "description", path, issues)
    _validate_provenance(profile.get("provenance"), path + ".provenance", issues)
    for key in ("required_traits", "forbidden_traits"):
        field = profile.get(key, [])
        if not _is_string_list(field):
            issues.append(ContractIssue(path + f".{key}", "type", "must be an array of non-empty strings"))


def _validate_character_contract(value: Mapping[str, Any], issues: list[ContractIssue]) -> set[str]:
    mode = value.get("mode")
    path = "$.contracts.characters"
    if mode not in {"present", "none"}:
        issues.append(ContractIssue(path + ".mode", "enum", "must be 'present' or 'none'"))
    _validate_provenance(value.get("mode_provenance"), path + ".mode_provenance", issues)
    characters = value.get("characters")
    if not isinstance(characters, list):
        issues.append(ContractIssue(path + ".characters", "type", "must be an array"))
        return set()
    if mode == "none" and characters:
        issues.append(ContractIssue(path + ".characters", "conditional", "must be empty when mode is 'none'"))
    if mode == "none" and not str(value.get("no_character_reason") or "").strip():
        issues.append(
            ContractIssue(path + ".no_character_reason", "required", "must explain why this story has no characters")
        )
    if mode == "present" and not characters:
        issues.append(ContractIssue(path + ".characters", "conditional", "must not be empty when mode is 'present'"))
    ids: set[str] = set()
    for index, character in enumerate(characters):
        item_path = f"{path}.characters[{index}]"
        if not isinstance(character, Mapping):
            issues.append(ContractIssue(item_path, "type", "character must be an object"))
            continue
        character_id = _require_nonempty_string(character, "character_id", item_path, issues)
        _require_nonempty_string(character, "role", item_path, issues)
        _validate_provenance(character.get("provenance"), item_path + ".provenance", issues)
        for key in ("identity_anchors", "required_features", "forbidden_features"):
            if not _is_string_list(character.get(key, [])):
                issues.append(ContractIssue(item_path + f".{key}", "type", "must be an array of non-empty strings"))
        if character_id:
            if character_id in ids:
                issues.append(ContractIssue(item_path + ".character_id", "duplicate", "character_id must be unique"))
            ids.add(character_id)
    return ids


def _validate_scale_contract(
    value: Mapping[str, Any], character_ids: set[str], issues: list[ContractIssue]
) -> set[str]:
    relationships = value.get("relationships")
    path = "$.contracts.world_scale.relationships"
    if not isinstance(relationships, list):
        issues.append(ContractIssue(path, "type", "must be an array"))
        return set()
    relation_ids: set[str] = set()
    for index, relation in enumerate(relationships):
        item_path = f"{path}[{index}]"
        if not isinstance(relation, Mapping):
            issues.append(ContractIssue(item_path, "type", "relationship must be an object"))
            continue
        relation_id = _require_nonempty_string(relation, "relationship_id", item_path, issues)
        subject = _require_nonempty_string(relation, "subject", item_path, issues)
        reference = _require_nonempty_string(relation, "reference", item_path, issues)
        qualitative = relation.get("qualitative_relation")
        if qualitative not in QUALITATIVE_SCALE_RELATIONS:
            issues.append(
                ContractIssue(item_path + ".qualitative_relation", "enum", "invalid qualitative scale relation")
            )
        _validate_provenance(relation.get("provenance"), item_path + ".provenance", issues)
        if subject and subject.startswith("character:") and subject.split(":", 1)[1] not in character_ids:
            issues.append(ContractIssue(item_path + ".subject", "unknown_reference", "unknown character reference"))
        if reference and reference.startswith("character:") and reference.split(":", 1)[1] not in character_ids:
            issues.append(ContractIssue(item_path + ".reference", "unknown_reference", "unknown character reference"))
        numeric = relation.get("numeric_range")
        if numeric is not None:
            _validate_numeric_scale_range(numeric, item_path + ".numeric_range", issues)
        if relation_id:
            if relation_id in relation_ids:
                issues.append(ContractIssue(item_path + ".relationship_id", "duplicate", "relationship_id must be unique"))
            relation_ids.add(relation_id)
    return relation_ids


def _validate_numeric_scale_range(value: object, path: str, issues: list[ContractIssue]) -> None:
    if not isinstance(value, Mapping):
        issues.append(ContractIssue(path, "type", "numeric_range must be an object"))
        return
    numbers: dict[str, float] = {}
    for key in ("min_ratio", "target_ratio", "max_ratio"):
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)):
            issues.append(ContractIssue(path + f".{key}", "number", "must be a finite number"))
        else:
            numbers[key] = float(item)
    if len(numbers) == 3:
        minimum, target, maximum = numbers["min_ratio"], numbers["target_ratio"], numbers["max_ratio"]
        if minimum <= 0 or not minimum <= target <= maximum:
            issues.append(ContractIssue(path, "range", "must satisfy 0 < min_ratio <= target_ratio <= max_ratio"))
        elif target > 0 and (maximum - minimum) / target < 0.30:
            issues.append(
                ContractIssue(
                    path,
                    "false_precision",
                    "numeric scale needs a tolerance width of at least 30% of target_ratio",
                )
            )
    basis = value.get("basis")
    if not isinstance(basis, Mapping):
        issues.append(ContractIssue(path + ".basis", "required", "numeric scale requires an evidence basis"))
    else:
        if basis.get("kind") not in {
            "story_explicit",
            "project_config",
            "brand_default",
            "visual_derivation",
            "machine_qa",
        }:
            issues.append(ContractIssue(path + ".basis.kind", "enum", "invalid numeric scale basis"))
        _require_nonempty_string(basis, "evidence", path + ".basis", issues)


def _validate_state_contract(
    value: Mapping[str, Any], character_ids: set[str], issues: list[ContractIssue]
) -> None:
    machines = value.get("machines")
    path = "$.contracts.story_state.machines"
    if not isinstance(machines, list):
        issues.append(ContractIssue(path, "type", "must be an array"))
        return
    machine_ids: set[str] = set()
    for index, machine in enumerate(machines):
        item_path = f"{path}[{index}]"
        if not isinstance(machine, Mapping):
            issues.append(ContractIssue(item_path, "type", "state machine must be an object"))
            continue
        machine_id = _require_nonempty_string(machine, "machine_id", item_path, issues)
        entity_ref = _require_nonempty_string(machine, "entity_ref", item_path, issues)
        if entity_ref and entity_ref.startswith("character:") and entity_ref.split(":", 1)[1] not in character_ids:
            issues.append(ContractIssue(item_path + ".entity_ref", "unknown_reference", "unknown character reference"))
        _validate_provenance(machine.get("provenance"), item_path + ".provenance", issues)
        states = machine.get("states")
        transitions = machine.get("transitions", [])
        if not isinstance(states, list) or not states:
            issues.append(ContractIssue(item_path + ".states", "required", "state machine needs at least one state"))
            continue
        state_ids: set[str] = set()
        for state_index, state in enumerate(states):
            state_path = f"{item_path}.states[{state_index}]"
            if not isinstance(state, Mapping):
                issues.append(ContractIssue(state_path, "type", "state must be an object"))
                continue
            state_id = _require_nonempty_string(state, "state_id", state_path, issues)
            _validate_provenance(state.get("provenance"), state_path + ".provenance", issues)
            for key in ("required", "forbidden"):
                if not _is_string_list(state.get(key, [])):
                    issues.append(ContractIssue(state_path + f".{key}", "type", "must be an array of strings"))
            if state_id:
                if state_id in state_ids:
                    issues.append(ContractIssue(state_path + ".state_id", "duplicate", "state_id must be unique"))
                state_ids.add(state_id)
        initial = machine.get("initial_state")
        if initial not in state_ids:
            issues.append(ContractIssue(item_path + ".initial_state", "unknown_reference", "must name a declared state"))
        if not isinstance(transitions, list):
            issues.append(ContractIssue(item_path + ".transitions", "type", "must be an array"))
        else:
            for transition_index, transition in enumerate(transitions):
                transition_path = f"{item_path}.transitions[{transition_index}]"
                if not isinstance(transition, Mapping):
                    issues.append(ContractIssue(transition_path, "type", "transition must be an object"))
                    continue
                if transition.get("from") not in state_ids or transition.get("to") not in state_ids:
                    issues.append(
                        ContractIssue(transition_path, "unknown_reference", "transition states must be declared")
                    )
                _require_nonempty_string(transition, "trigger", transition_path, issues)
                _validate_provenance(transition.get("provenance"), transition_path + ".provenance", issues)
        if machine_id:
            if machine_id in machine_ids:
                issues.append(ContractIssue(item_path + ".machine_id", "duplicate", "machine_id must be unique"))
            machine_ids.add(machine_id)


def _validate_brand_contract(value: Mapping[str, Any], issues: list[ContractIssue]) -> None:
    assets = value.get("assets")
    path = "$.contracts.brand.assets"
    if not isinstance(assets, list):
        issues.append(ContractIssue(path, "type", "must be an array"))
        return
    ids: set[str] = set()
    for index, asset in enumerate(assets):
        item_path = f"{path}[{index}]"
        if not isinstance(asset, Mapping):
            issues.append(ContractIssue(item_path, "type", "brand asset must be an object"))
            continue
        asset_id = _require_nonempty_string(asset, "asset_id", item_path, issues)
        digest = _require_nonempty_string(asset, "sha256", item_path, issues)
        if digest and not _is_sha256(digest):
            issues.append(ContractIssue(item_path + ".sha256", "sha256", "must be 64 lowercase hex characters"))
        _validate_provenance(asset.get("provenance"), item_path + ".provenance", issues)
        if asset_id:
            if asset_id in ids:
                issues.append(ContractIssue(item_path + ".asset_id", "duplicate", "asset_id must be unique"))
            ids.add(asset_id)


def _validate_layout_contract(value: Mapping[str, Any], issues: list[ContractIssue]) -> set[str]:
    variants = value.get("variants")
    path = "$.contracts.release_layout.variants"
    if not isinstance(variants, list):
        issues.append(ContractIssue(path, "type", "must be an array"))
        return set()
    ids: set[str] = set()
    for index, variant in enumerate(variants):
        item_path = f"{path}[{index}]"
        if not isinstance(variant, Mapping):
            issues.append(ContractIssue(item_path, "type", "layout variant must be an object"))
            continue
        variant_id = _require_nonempty_string(variant, "variant_id", item_path, issues)
        _require_nonempty_string(variant, "aspect_ratio", item_path, issues)
        _validate_provenance(variant.get("provenance"), item_path + ".provenance", issues)
        regions = variant.get("regions", [])
        if not isinstance(regions, list):
            issues.append(ContractIssue(item_path + ".regions", "type", "must be an array"))
        else:
            region_ids: set[str] = set()
            for region_index, region in enumerate(regions):
                region_path = f"{item_path}.regions[{region_index}]"
                if not isinstance(region, Mapping):
                    issues.append(ContractIssue(region_path, "type", "region must be an object"))
                    continue
                region_id = _require_nonempty_string(region, "region_id", region_path, issues)
                for key in ("x", "y", "width", "height"):
                    number = region.get(key)
                    if isinstance(number, bool) or not isinstance(number, (int, float)):
                        issues.append(ContractIssue(region_path + f".{key}", "number", "must be a number"))
                    elif not 0 <= float(number) <= 1:
                        issues.append(ContractIssue(region_path + f".{key}", "bounds", "must be between 0 and 1"))
                if all(isinstance(region.get(key), (int, float)) for key in ("x", "y", "width", "height")):
                    if float(region["width"]) <= 0 or float(region["height"]) <= 0:
                        issues.append(ContractIssue(region_path, "bounds", "width and height must be positive"))
                    if float(region["x"]) + float(region["width"]) > 1.000001:
                        issues.append(ContractIssue(region_path, "bounds", "region exceeds horizontal canvas"))
                    if float(region["y"]) + float(region["height"]) > 1.000001:
                        issues.append(ContractIssue(region_path, "bounds", "region exceeds vertical canvas"))
                _validate_provenance(region.get("provenance"), region_path + ".provenance", issues)
                if region_id:
                    if region_id in region_ids:
                        issues.append(ContractIssue(region_path + ".region_id", "duplicate", "region_id must be unique"))
                    region_ids.add(region_id)
        if variant_id:
            if variant_id in ids:
                issues.append(ContractIssue(item_path + ".variant_id", "duplicate", "variant_id must be unique"))
            ids.add(variant_id)
    return ids


def _validate_preview_assets(
    value: object,
    *,
    characters: Mapping[str, Any],
    character_ids: set[str],
    scale_relation_ids: set[str],
    layout_variant_ids: set[str],
    issues: list[ContractIssue],
) -> None:
    path = "$.preview_assets"
    if not isinstance(value, list):
        issues.append(ContractIssue(path, "type", "preview_assets must be an array"))
        return
    ids: set[str] = set()
    for index, preview in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(preview, Mapping):
            issues.append(ContractIssue(item_path, "type", "preview asset must be an object"))
            continue
        preview_id = _require_nonempty_string(preview, "preview_id", item_path, issues)
        kind = preview.get("kind")
        if kind not in PREVIEW_KINDS:
            issues.append(ContractIssue(item_path + ".kind", "enum", "invalid preview kind"))
        _require_nonempty_string(preview, "need_reason", item_path, issues)
        _validate_provenance(preview.get("provenance"), item_path + ".provenance", issues)
        refs = preview.get("content_refs", [])
        if not _is_string_list(refs):
            issues.append(ContractIssue(item_path + ".content_refs", "type", "must be an array of strings"))
            refs = []
        if kind == "character_sheet":
            if characters.get("mode") != "present":
                issues.append(
                    ContractIssue(item_path, "conditional", "character_sheet is invalid when character mode is 'none'")
                )
            unknown = [ref for ref in refs if ref not in character_ids]
            if not refs or unknown:
                issues.append(
                    ContractIssue(item_path + ".content_refs", "unknown_reference", "must reference declared characters")
                )
        elif kind == "scale_anchor":
            unknown = [ref for ref in refs if ref not in scale_relation_ids]
            if not refs or unknown:
                issues.append(
                    ContractIssue(item_path + ".content_refs", "unknown_reference", "must reference declared scale relationships")
                )
        elif kind == "layout_preview":
            unknown = [ref for ref in refs if ref not in layout_variant_ids]
            if not refs or unknown:
                issues.append(
                    ContractIssue(item_path + ".content_refs", "unknown_reference", "must reference declared layout variants")
                )
        generated_path = preview.get("path")
        generated_hash = preview.get("sha256")
        if bool(generated_path) != bool(generated_hash):
            issues.append(ContractIssue(item_path, "paired_fields", "path and sha256 must appear together"))
        if generated_hash and not _is_sha256(str(generated_hash)):
            issues.append(ContractIssue(item_path + ".sha256", "sha256", "must be 64 lowercase hex characters"))
        if generated_path and Path(str(generated_path)).is_absolute():
            issues.append(ContractIssue(item_path + ".path", "portable_path", "must be a project-relative path"))
        if preview_id:
            if preview_id in ids:
                issues.append(ContractIssue(item_path + ".preview_id", "duplicate", "preview_id must be unique"))
            ids.add(preview_id)


def _require_mapping(
    value: Mapping[str, Any], key: str, parent_path: str, issues: list[ContractIssue]
) -> Mapping[str, Any] | None:
    item = value.get(key)
    if not isinstance(item, Mapping):
        issues.append(ContractIssue(parent_path + f".{key}", "type", "must be an object"))
        return None
    return item


def _require_nonempty_string(
    value: Mapping[str, Any], key: str, parent_path: str, issues: list[ContractIssue]
) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        issues.append(ContractIssue(parent_path + f".{key}", "required", "must be a non-empty string"))
        return ""
    return item.strip()


def _is_string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and bool(item.strip()) for item in value)


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


__all__ = [
    "ContractIssue",
    "ContractSection",
    "PREVIEW_KINDS",
    "QUALITATIVE_SCALE_RELATIONS",
    "ROOT_SCHEMA_PATH",
    "ResolvedRule",
    "RuleConflictError",
    "RuleSource",
    "SCHEMA_ROOT",
    "SOURCE_PRIORITIES",
    "STORY_CONTRACT_SCHEMA_VERSION",
    "StoryContractValidationError",
    "StoryProductionContract",
    "canonical_json_bytes",
    "contract_sha256",
    "load_contract_schema",
    "load_story_contract",
    "resolve_rule_candidates",
    "source_priority",
    "validate_story_contract",
    "validate_story_contract_or_raise",
]
