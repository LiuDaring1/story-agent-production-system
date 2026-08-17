from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable


MODULE_PORT_SCHEMA_VERSION = "story-module-ports/v1"
VIDEO_GENERATOR_PORT_VERSION = "story-video-generator-port/v1"
KEYER_PORT_VERSION = "story-keyer-port/v1"


class ModuleFailureCode(str, Enum):
    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    CONFIGURATION_ERROR = "configuration_error"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    EXECUTION_FAILED = "execution_failed"
    INVALID_OUTPUT = "invalid_output"


@dataclass(frozen=True)
class ModuleIdentity:
    port_name: str
    port_version: str
    adapter_name: str
    adapter_version: str


@dataclass(frozen=True)
class ModuleCapabilities:
    provider: str = ""
    model_or_tool: str = ""
    runner_or_tool: str = ""
    external: bool = False
    paid: bool = False
    deterministic: bool = False
    supported: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModuleFailure:
    code: ModuleFailureCode
    message: str
    retryable: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModuleUsageEvent:
    provider: str
    model_or_tool: str
    operation: str
    unit_type: str = ""
    quantity: float | None = None
    currency: str = ""
    estimated_amount: float | None = None
    actual_amount_status: str = "unknown"


@dataclass(frozen=True)
class VideoGeneratorRequest:
    artifact_id: str
    scene_id: str
    source_image_path: Path
    source_image_sha256: str
    prompt: str
    prompt_sha256: str
    requested_duration: float
    requested_ratio: str
    requested_resolution: str
    output_target: Path
    attempt_id: str
    story_contract_sha256: str = ""
    story_contract_dependency_sha256: str = ""
    motion_plan_sha256: str = ""


@dataclass(frozen=True)
class VideoGeneratorResult:
    success: bool
    output_path: Path | None
    output_sha256: str
    provider: str
    model: str
    request_id: str
    requested_duration: float
    produced_duration: float | None
    attempt_id: str
    source_image_sha256: str
    prompt_sha256: str
    adapter_version: str
    usage_events: tuple[ModuleUsageEvent, ...] = ()
    failure: ModuleFailure | None = None


@dataclass(frozen=True)
class KeyerRequest:
    artifact_id: str
    source_path: Path
    source_sha256: str
    settings: Mapping[str, Any]
    output_target: Path
    attempt_id: str
    preset_sha256: str = ""
    keying_lock_sha256: str = ""


@dataclass(frozen=True)
class KeyerResult:
    success: bool
    output_path: Path | None
    output_sha256: str
    keyer: str
    filter_version: str
    filter_fingerprint: str
    source_sha256: str
    preset_sha256: str
    adapter_version: str
    usage_events: tuple[ModuleUsageEvent, ...] = ()
    failure: ModuleFailure | None = None


@runtime_checkable
class VideoGeneratorPort(Protocol):
    identity: ModuleIdentity
    capabilities: ModuleCapabilities

    def estimate_cost(self, seconds: float | None = None) -> float: ...

    def runner_args(self) -> list[str]: ...

    def invoke_batch(
        self,
        arguments: Sequence[str],
        *,
        executor: Callable[[Sequence[str]], None],
    ) -> None: ...

    def resolve_request_seconds(self, row: Mapping[str, Any], fallback_seconds: str | int | float) -> str: ...

    def generate(self, request: VideoGeneratorRequest) -> VideoGeneratorResult: ...


@runtime_checkable
class KeyerPort(Protocol):
    identity: ModuleIdentity
    capabilities: ModuleCapabilities

    def compile_contract(self, settings: Any) -> dict[str, Any]: ...

    def filter_chain(self, source: str, settings: Any, crop_filter: str = "") -> str: ...

    def filter_parts(self, source: str, settings: Any, crop_filter: str = "") -> list[str]: ...

    def fingerprint(self, settings: Any) -> str: ...

    def render(self, request: KeyerRequest) -> KeyerResult: ...


def module_payload(kind: str, value: Any) -> dict[str, Any]:
    payload = _json_value(asdict(value))
    payload["kind"] = kind
    payload["schema_version"] = MODULE_PORT_SCHEMA_VERSION
    return payload


def validate_module_payload(payload: Mapping[str, Any]) -> list[str]:
    """Small dependency-free validator kept in parity with the JSON Schema."""

    if not isinstance(payload, Mapping):
        return ["payload_not_object"]
    issues: list[str] = []
    kind = payload.get("kind")
    required_by_kind = {
        "identity": ("port_name", "port_version", "adapter_name", "adapter_version"),
        "capabilities": ("provider", "model_or_tool", "runner_or_tool", "external", "paid", "deterministic", "supported"),
        "failure": ("code", "message", "retryable", "details"),
        "usage_event": ("provider", "model_or_tool", "operation", "actual_amount_status"),
        "video_request": (
            "artifact_id", "scene_id", "source_image_path", "source_image_sha256", "prompt", "prompt_sha256",
            "requested_duration", "requested_ratio", "requested_resolution", "output_target", "attempt_id",
        ),
        "video_result": (
            "success", "output_path", "output_sha256", "provider", "model", "request_id", "requested_duration",
            "produced_duration", "attempt_id", "source_image_sha256", "prompt_sha256", "adapter_version",
        ),
        "keyer_request": ("artifact_id", "source_path", "source_sha256", "settings", "output_target", "attempt_id"),
        "keyer_result": (
            "success", "output_path", "output_sha256", "keyer", "filter_version", "filter_fingerprint",
            "source_sha256", "preset_sha256", "adapter_version",
        ),
    }
    if payload.get("schema_version") != MODULE_PORT_SCHEMA_VERSION:
        issues.append("schema_version")
    if kind not in required_by_kind:
        return issues + ["kind"]
    for field_name in required_by_kind[kind]:
        if field_name not in payload:
            issues.append(f"missing:{field_name}")
    string_fields = {
        "identity": ("port_name", "port_version", "adapter_name", "adapter_version"),
        "capabilities": ("provider", "model_or_tool", "runner_or_tool"),
        "failure": ("message",),
        "usage_event": ("provider", "model_or_tool", "operation", "unit_type", "currency", "actual_amount_status"),
        "video_request": (
            "artifact_id", "scene_id", "source_image_path", "source_image_sha256", "prompt", "prompt_sha256",
            "requested_ratio", "requested_resolution", "output_target", "attempt_id", "story_contract_sha256",
            "story_contract_dependency_sha256", "motion_plan_sha256",
        ),
        "video_result": (
            "output_sha256", "provider", "model", "request_id", "attempt_id", "source_image_sha256",
            "prompt_sha256", "adapter_version",
        ),
        "keyer_request": (
            "artifact_id", "source_path", "source_sha256", "output_target", "attempt_id", "preset_sha256",
            "keying_lock_sha256",
        ),
        "keyer_result": (
            "output_sha256", "keyer", "filter_version", "filter_fingerprint", "source_sha256",
            "preset_sha256", "adapter_version",
        ),
    }
    for field_name in string_fields.get(str(kind), ()):
        if field_name in payload and not isinstance(payload[field_name], str):
            issues.append(f"type:{field_name}")
    object_fields = {
        "capabilities": ("supported",),
        "failure": ("details",),
        "keyer_request": ("settings",),
    }
    for field_name in object_fields.get(str(kind), ()):
        if field_name in payload and not isinstance(payload[field_name], Mapping):
            issues.append(f"type:{field_name}")
    boolean_fields = {
        "capabilities": ("external", "paid", "deterministic"),
        "failure": ("retryable",),
        "video_result": ("success",),
        "keyer_result": ("success",),
    }
    for field_name in boolean_fields.get(str(kind), ()):
        if field_name in payload and type(payload[field_name]) is not bool:
            issues.append(f"type:{field_name}")
    numeric_fields = {
        "usage_event": ("quantity", "estimated_amount"),
        "video_request": ("requested_duration",),
        "video_result": ("requested_duration", "produced_duration"),
    }
    for field_name in numeric_fields.get(str(kind), ()):
        value = payload.get(field_name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            issues.append(f"type:{field_name}")
    nullable_string_fields = {
        "video_result": ("output_path",),
        "keyer_result": ("output_path",),
    }
    for field_name in nullable_string_fields.get(str(kind), ()):
        value = payload.get(field_name)
        if value is not None and not isinstance(value, str):
            issues.append(f"type:{field_name}")
    if kind == "failure" and payload.get("code") not in {item.value for item in ModuleFailureCode}:
        issues.append("code")
    return issues


def _json_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


__all__ = [
    "KEYER_PORT_VERSION", "MODULE_PORT_SCHEMA_VERSION", "VIDEO_GENERATOR_PORT_VERSION",
    "KeyerPort", "KeyerRequest", "KeyerResult", "ModuleCapabilities", "ModuleFailure",
    "ModuleFailureCode", "ModuleIdentity", "ModuleUsageEvent", "VideoGeneratorPort",
    "VideoGeneratorRequest", "VideoGeneratorResult", "module_payload", "validate_module_payload",
]
