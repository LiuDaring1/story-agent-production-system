from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable


MODULE_PORT_SCHEMA_VERSION = "story-module-ports/v1"
VIDEO_GENERATOR_PORT_VERSION = "story-video-generator-port/v1"
KEYER_PORT_VERSION = "story-keyer-port/v1"
STORY_SEMANTICS_PORT_VERSION = "story-semantics-port/v1"
VISUAL_DESIGN_PORT_VERSION = "story-visual-design-port/v1"
IMAGE_GENERATOR_PORT_VERSION = "story-image-generator-port/v1"
MUSIC_PROVIDER_PORT_VERSION = "story-music-provider-port/v1"
PRODUCT_PACKAGE_PORT_VERSION = "story-product-package-port/v1"
COMPOSITOR_PORT_VERSION = "story-compositor-port/v1"
RELEASE_LAYOUT_PORT_VERSION = "story-release-layout-port/v1"
PUBLISH_ASSET_PORT_VERSION = "story-publish-asset-port/v1"
STORY_SEMANTICS_COMPILER_VERSION = "story-semantics-classifier/v1"
STORY_SEMANTIC_KINDS = frozenset(
    {"title", "host_intro", "story_announcement", "story_body", "moral", "outro"}
)


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


@dataclass(frozen=True)
class StorySemanticsRequest:
    source_path: Path
    source_sha256: str
    normalized_lines: tuple[str, ...]
    story_id: str
    compiler_version: str
    attempt_id: str


@dataclass(frozen=True)
class StorySemanticsResult:
    success: bool
    source_path: Path
    source_sha256: str
    title: str
    lines: tuple[Mapping[str, Any], ...]
    segments: tuple[Mapping[str, Any], ...]
    output_line_numbers: Mapping[str, tuple[int, ...]]
    adapter_version: str
    compiler_version: str
    usage_events: tuple[ModuleUsageEvent, ...] = ()
    failure: ModuleFailure | None = None


@dataclass(frozen=True)
class VisualDesignRequest:
    project_root: Path
    consumer: str
    operation: str
    story_contract_sha256: str
    projection_sha256: str
    story_semantics_sha256: str
    current_artifact_references: tuple[Mapping[str, Any], ...]
    attempt_id: str


@dataclass(frozen=True)
class VisualDesignResult:
    success: bool
    operation: str
    approved_projection: Mapping[str, Any]
    artifact_references: tuple[Mapping[str, Any], ...]
    output_sha256: str
    story_contract_sha256: str
    projection_sha256: str
    story_semantics_sha256: str
    attempt_id: str
    adapter_version: str
    usage_events: tuple[ModuleUsageEvent, ...] = ()
    failure: ModuleFailure | None = None


@dataclass(frozen=True)
class ImageGeneratorRequest:
    """One already-planned external image execution envelope."""

    artifact_id: str
    operation: str
    execution_request_path: Path
    execution_request_sha256: str
    input_artifacts: tuple[Mapping[str, Any], ...]
    output_targets: tuple[Path, ...]
    attempt_id: str


@dataclass(frozen=True)
class ImageGeneratorResult:
    success: bool
    operation: str
    output_artifacts: tuple[Mapping[str, Any], ...]
    provider: str
    model_or_tool: str
    request_id: str
    attempt_id: str
    execution_request_sha256: str
    adapter_version: str
    production_eligible: bool
    usage_events: tuple[ModuleUsageEvent, ...] = ()
    failure: ModuleFailure | None = None


@dataclass(frozen=True)
class MusicProviderRequest:
    """One already-planned external music execution envelope."""

    artifact_id: str
    operation: str
    execution_request_path: Path
    execution_request_sha256: str
    input_artifacts: tuple[Mapping[str, Any], ...]
    output_targets: tuple[Path, ...]
    attempt_id: str


@dataclass(frozen=True)
class MusicProviderResult:
    success: bool
    operation: str
    output_artifacts: tuple[Mapping[str, Any], ...]
    provider: str
    model_or_tool: str
    request_id: str
    attempt_id: str
    execution_request_sha256: str
    adapter_version: str
    production_eligible: bool
    usage_events: tuple[ModuleUsageEvent, ...] = ()
    failure: ModuleFailure | None = None


ImageGeneratorExecutor = Callable[[ImageGeneratorRequest], ImageGeneratorResult]
MusicProviderExecutor = Callable[[MusicProviderRequest], MusicProviderResult]


@dataclass(frozen=True)
class ProductPackageRequest:
    """One caller-planned local filesystem packaging invocation."""

    artifact_id: str
    operation: str
    package_variant: str
    output_root: Path
    source_artifacts: tuple[Mapping[str, Any], ...]
    output_targets: tuple[Path, ...]
    attempt_id: str


@dataclass(frozen=True)
class ProductPackageResult:
    success: bool
    operation: str
    package_variant: str
    output_artifacts: tuple[Mapping[str, Any], ...]
    attempt_id: str
    adapter_version: str
    production_eligible: bool
    usage_events: tuple[ModuleUsageEvent, ...] = ()
    failure: ModuleFailure | None = None


ProductPackageExecutor = Callable[[ProductPackageRequest], ProductPackageResult]


@dataclass(frozen=True)
class CompositorRequest:
    """One caller-resolved local compositor invocation."""

    artifact_id: str
    operation: str
    input_artifacts: tuple[Mapping[str, Any], ...]
    output_targets: tuple[Path, ...]
    execution_binding: Mapping[str, Any]
    attempt_id: str


@dataclass(frozen=True)
class CompositorResult:
    success: bool
    operation: str
    output_artifacts: tuple[Mapping[str, Any], ...]
    attempt_id: str
    adapter_version: str
    production_eligible: bool
    usage_events: tuple[ModuleUsageEvent, ...] = ()
    failure: ModuleFailure | None = None


CompositorExecutor = Callable[[CompositorRequest], CompositorResult]


@dataclass(frozen=True)
class ReleaseLayoutRequest:
    """One caller-resolved local release layout render invocation."""

    artifact_id: str
    operation: str
    input_artifacts: tuple[Mapping[str, Any], ...]
    layout_binding: Mapping[str, Any]
    output_target: Path
    attempt_id: str


@dataclass(frozen=True)
class ReleaseLayoutResult:
    success: bool
    operation: str
    output_artifact: Mapping[str, Any] | None
    attempt_id: str
    adapter_name: str
    adapter_version: str
    production_eligible: bool
    usage_events: tuple[ModuleUsageEvent, ...] = ()
    failure: ModuleFailure | None = None


ReleaseLayoutExecutor = Callable[[ReleaseLayoutRequest], ReleaseLayoutResult]


@dataclass(frozen=True)
class PublishAssetRequest:
    """One caller-resolved deterministic local publish asset invocation."""

    artifact_id: str
    operation: str
    input_artifacts: tuple[Mapping[str, Any], ...]
    execution_binding: Mapping[str, Any]
    output_targets: tuple[Path, ...]
    attempt_id: str


@dataclass(frozen=True)
class PublishAssetResult:
    success: bool
    operation: str
    output_artifacts: tuple[Mapping[str, Any], ...]
    attempt_id: str
    adapter_name: str
    adapter_version: str
    production_eligible: bool
    usage_events: tuple[ModuleUsageEvent, ...] = ()
    failure: ModuleFailure | None = None


PublishAssetExecutor = Callable[[PublishAssetRequest], PublishAssetResult]


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


@runtime_checkable
class StorySemanticsPort(Protocol):
    identity: ModuleIdentity
    capabilities: ModuleCapabilities

    def analyze(self, request: StorySemanticsRequest) -> StorySemanticsResult: ...


@runtime_checkable
class VisualDesignPort(Protocol):
    identity: ModuleIdentity
    capabilities: ModuleCapabilities

    def resolve(self, request: VisualDesignRequest) -> VisualDesignResult: ...


@runtime_checkable
class ImageGeneratorPort(Protocol):
    identity: ModuleIdentity
    capabilities: ModuleCapabilities

    def execute(
        self,
        request: ImageGeneratorRequest,
        *,
        executor: ImageGeneratorExecutor,
    ) -> ImageGeneratorResult: ...


@runtime_checkable
class MusicProviderPort(Protocol):
    identity: ModuleIdentity
    capabilities: ModuleCapabilities

    def execute(
        self,
        request: MusicProviderRequest,
        *,
        executor: MusicProviderExecutor,
    ) -> MusicProviderResult: ...


@runtime_checkable
class ProductPackagePort(Protocol):
    identity: ModuleIdentity
    capabilities: ModuleCapabilities

    def execute(
        self,
        request: ProductPackageRequest,
        *,
        executor: ProductPackageExecutor,
    ) -> ProductPackageResult: ...


@runtime_checkable
class CompositorPort(Protocol):
    identity: ModuleIdentity
    capabilities: ModuleCapabilities

    def execute(
        self,
        request: CompositorRequest,
        *,
        executor: CompositorExecutor,
    ) -> CompositorResult: ...


@runtime_checkable
class ReleaseLayoutPort(Protocol):
    identity: ModuleIdentity
    capabilities: ModuleCapabilities

    def execute(
        self,
        request: ReleaseLayoutRequest,
        *,
        executor: ReleaseLayoutExecutor,
    ) -> ReleaseLayoutResult: ...


@runtime_checkable
class PublishAssetPort(Protocol):
    identity: ModuleIdentity
    capabilities: ModuleCapabilities

    def execute(
        self,
        request: PublishAssetRequest,
        *,
        executor: PublishAssetExecutor,
    ) -> PublishAssetResult: ...


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


def story_semantics_payload(kind: str, value: Any) -> dict[str, Any]:
    return _versioned_payload(STORY_SEMANTICS_PORT_VERSION, kind, value)


def visual_design_payload(kind: str, value: Any) -> dict[str, Any]:
    return _versioned_payload(VISUAL_DESIGN_PORT_VERSION, kind, value)


def image_generator_payload(kind: str, value: Any) -> dict[str, Any]:
    return _versioned_payload(IMAGE_GENERATOR_PORT_VERSION, kind, value)


def music_provider_payload(kind: str, value: Any) -> dict[str, Any]:
    return _versioned_payload(MUSIC_PROVIDER_PORT_VERSION, kind, value)


def product_package_payload(kind: str, value: Any) -> dict[str, Any]:
    return _versioned_payload(PRODUCT_PACKAGE_PORT_VERSION, kind, value)


def compositor_payload(kind: str, value: Any) -> dict[str, Any]:
    return _versioned_payload(COMPOSITOR_PORT_VERSION, kind, value)


def release_layout_payload(kind: str, value: Any) -> dict[str, Any]:
    return _versioned_payload(RELEASE_LAYOUT_PORT_VERSION, kind, value)


def publish_asset_payload(kind: str, value: Any) -> dict[str, Any]:
    return _versioned_payload(PUBLISH_ASSET_PORT_VERSION, kind, value)


def validate_story_semantics_payload(payload: Mapping[str, Any]) -> list[str]:
    return _validate_front_half_payload(
        payload,
        schema_version=STORY_SEMANTICS_PORT_VERSION,
        required_by_kind={
            "semantics_request": (
                "source_path", "source_sha256", "normalized_lines", "story_id", "compiler_version", "attempt_id",
            ),
            "semantics_result": (
                "success", "source_path", "source_sha256", "title", "lines", "segments",
                "output_line_numbers", "adapter_version", "compiler_version", "usage_events", "failure",
            ),
        },
        string_fields={
            "semantics_request": ("source_path", "source_sha256", "story_id", "compiler_version", "attempt_id"),
            "semantics_result": (
                "source_path", "source_sha256", "title", "adapter_version", "compiler_version",
            ),
        },
        array_fields={
            "semantics_request": ("normalized_lines",),
            "semantics_result": ("lines", "segments", "usage_events"),
        },
        array_item_types={
            "semantics_request": {"normalized_lines": str},
            "semantics_result": {"lines": Mapping, "segments": Mapping, "usage_events": Mapping},
        },
        object_fields={"semantics_result": ("output_line_numbers",)},
        object_array_value_fields={"semantics_result": ("output_line_numbers",)},
        boolean_fields={"semantics_result": ("success",)},
        nullable_object_fields={"semantics_result": ("failure",)},
    )


def validate_visual_design_payload(payload: Mapping[str, Any]) -> list[str]:
    return _validate_front_half_payload(
        payload,
        schema_version=VISUAL_DESIGN_PORT_VERSION,
        required_by_kind={
            "visual_design_request": (
                "project_root", "consumer", "operation", "story_contract_sha256", "projection_sha256",
                "story_semantics_sha256", "current_artifact_references", "attempt_id",
            ),
            "visual_design_result": (
                "success", "operation", "approved_projection", "artifact_references", "output_sha256",
                "story_contract_sha256", "projection_sha256", "story_semantics_sha256", "attempt_id",
                "adapter_version", "usage_events", "failure",
            ),
        },
        string_fields={
            "visual_design_request": (
                "project_root", "consumer", "operation", "story_contract_sha256", "projection_sha256",
                "story_semantics_sha256", "attempt_id",
            ),
            "visual_design_result": (
                "operation", "output_sha256", "story_contract_sha256", "projection_sha256",
                "story_semantics_sha256", "attempt_id", "adapter_version",
            ),
        },
        array_fields={
            "visual_design_request": ("current_artifact_references",),
            "visual_design_result": ("artifact_references", "usage_events"),
        },
        array_item_types={
            "visual_design_request": {"current_artifact_references": Mapping},
            "visual_design_result": {"artifact_references": Mapping, "usage_events": Mapping},
        },
        object_fields={"visual_design_result": ("approved_projection",)},
        object_array_value_fields={},
        boolean_fields={"visual_design_result": ("success",)},
        nullable_object_fields={"visual_design_result": ("failure",)},
    )


def validate_image_generator_payload(payload: Mapping[str, Any]) -> list[str]:
    return _validate_external_execution_payload(
        payload,
        schema_version=IMAGE_GENERATOR_PORT_VERSION,
        request_kind="image_generator_request",
        result_kind="image_generator_result",
    )


def validate_music_provider_payload(payload: Mapping[str, Any]) -> list[str]:
    return _validate_external_execution_payload(
        payload,
        schema_version=MUSIC_PROVIDER_PORT_VERSION,
        request_kind="music_provider_request",
        result_kind="music_provider_result",
    )


def validate_product_package_payload(payload: Mapping[str, Any]) -> list[str]:
    issues = _validate_front_half_payload(
        payload,
        schema_version=PRODUCT_PACKAGE_PORT_VERSION,
        required_by_kind={
            "product_package_request": (
                "artifact_id", "operation", "package_variant", "output_root", "source_artifacts",
                "output_targets", "attempt_id",
            ),
            "product_package_result": (
                "success", "operation", "package_variant", "output_artifacts", "attempt_id",
                "adapter_version", "production_eligible", "usage_events", "failure",
            ),
        },
        string_fields={
            "product_package_request": (
                "artifact_id", "operation", "package_variant", "output_root", "attempt_id",
            ),
            "product_package_result": (
                "operation", "package_variant", "attempt_id", "adapter_version",
            ),
        },
        array_fields={
            "product_package_request": ("source_artifacts", "output_targets"),
            "product_package_result": ("output_artifacts", "usage_events"),
        },
        array_item_types={
            "product_package_request": {"source_artifacts": Mapping, "output_targets": str},
            "product_package_result": {"output_artifacts": Mapping, "usage_events": Mapping},
        },
        object_fields={},
        object_array_value_fields={},
        boolean_fields={"product_package_result": ("success", "production_eligible")},
        nullable_object_fields={"product_package_result": ("failure",)},
    )
    kind = payload.get("kind")
    field_name = "source_artifacts" if kind == "product_package_request" else "output_artifacts"
    required = (
        ("package_variant", "source_path", "source_sha256", "destination_path")
        if kind == "product_package_request"
        else ("package_variant", "source_path", "source_sha256", "path", "sha256", "production_eligible")
    )
    artifacts = payload.get(field_name)
    if isinstance(artifacts, list):
        for index, artifact in enumerate(artifacts):
            if not isinstance(artifact, Mapping):
                continue
            for name in required:
                if name not in artifact:
                    issues.append(f"missing:{field_name}[{index}].{name}")
                elif name == "production_eligible":
                    if type(artifact[name]) is not bool:
                        issues.append(f"type:{field_name}[{index}].{name}")
                elif not isinstance(artifact[name], str):
                    issues.append(f"type:{field_name}[{index}].{name}")
    return issues


def validate_compositor_payload(payload: Mapping[str, Any]) -> list[str]:
    issues = _validate_front_half_payload(
        payload,
        schema_version=COMPOSITOR_PORT_VERSION,
        required_by_kind={
            "compositor_request": (
                "artifact_id", "operation", "input_artifacts", "output_targets",
                "execution_binding", "attempt_id",
            ),
            "compositor_result": (
                "success", "operation", "output_artifacts", "attempt_id", "adapter_version",
                "production_eligible", "usage_events", "failure",
            ),
        },
        string_fields={
            "compositor_request": ("artifact_id", "operation", "attempt_id"),
            "compositor_result": ("operation", "attempt_id", "adapter_version"),
        },
        array_fields={
            "compositor_request": ("input_artifacts", "output_targets"),
            "compositor_result": ("output_artifacts", "usage_events"),
        },
        array_item_types={
            "compositor_request": {"input_artifacts": Mapping, "output_targets": str},
            "compositor_result": {"output_artifacts": Mapping, "usage_events": Mapping},
        },
        object_fields={"compositor_request": ("execution_binding",)},
        object_array_value_fields={},
        boolean_fields={"compositor_result": ("success", "production_eligible")},
        nullable_object_fields={"compositor_result": ("failure",)},
    )
    kind = payload.get("kind")
    field_name = "input_artifacts" if kind == "compositor_request" else "output_artifacts"
    required = (
        ("role", "path", "sha256")
        if kind == "compositor_request"
        else ("path", "sha256", "production_eligible")
    )
    artifacts = payload.get(field_name)
    if isinstance(artifacts, list):
        for index, artifact in enumerate(artifacts):
            if not isinstance(artifact, Mapping):
                continue
            for name in required:
                if name not in artifact:
                    issues.append(f"missing:{field_name}[{index}].{name}")
                elif name == "production_eligible":
                    if type(artifact[name]) is not bool:
                        issues.append(f"type:{field_name}[{index}].{name}")
                elif not isinstance(artifact[name], str):
                    issues.append(f"type:{field_name}[{index}].{name}")
    return issues


def validate_release_layout_payload(payload: Mapping[str, Any]) -> list[str]:
    issues = _validate_front_half_payload(
        payload,
        schema_version=RELEASE_LAYOUT_PORT_VERSION,
        required_by_kind={
            "release_layout_request": (
                "artifact_id", "operation", "input_artifacts", "layout_binding",
                "output_target", "attempt_id",
            ),
            "release_layout_result": (
                "success", "operation", "output_artifact", "attempt_id", "adapter_name", "adapter_version",
                "production_eligible", "usage_events", "failure",
            ),
        },
        string_fields={
            "release_layout_request": ("artifact_id", "operation", "output_target", "attempt_id"),
            "release_layout_result": ("operation", "attempt_id", "adapter_name", "adapter_version"),
        },
        array_fields={
            "release_layout_request": ("input_artifacts",),
            "release_layout_result": ("usage_events",),
        },
        array_item_types={
            "release_layout_request": {"input_artifacts": Mapping},
            "release_layout_result": {"usage_events": Mapping},
        },
        object_fields={"release_layout_request": ("layout_binding",)},
        object_array_value_fields={},
        boolean_fields={"release_layout_result": ("success", "production_eligible")},
        nullable_object_fields={"release_layout_result": ("output_artifact", "failure")},
    )
    kind = payload.get("kind")
    if kind == "release_layout_request":
        artifacts = payload.get("input_artifacts")
        if isinstance(artifacts, list):
            for index, artifact in enumerate(artifacts):
                if not isinstance(artifact, Mapping):
                    continue
                for name in ("role", "path", "sha256"):
                    if name not in artifact:
                        issues.append(f"missing:input_artifacts[{index}].{name}")
                    elif not isinstance(artifact[name], str):
                        issues.append(f"type:input_artifacts[{index}].{name}")
    elif kind == "release_layout_result":
        artifact = payload.get("output_artifact")
        if isinstance(artifact, Mapping):
            for name in ("path", "sha256", "production_eligible"):
                if name not in artifact:
                    issues.append(f"missing:output_artifact.{name}")
                elif name == "production_eligible":
                    if type(artifact[name]) is not bool:
                        issues.append(f"type:output_artifact.{name}")
                elif not isinstance(artifact[name], str):
                    issues.append(f"type:output_artifact.{name}")
    return issues


def validate_publish_asset_payload(payload: Mapping[str, Any]) -> list[str]:
    issues = _validate_front_half_payload(
        payload,
        schema_version=PUBLISH_ASSET_PORT_VERSION,
        required_by_kind={
            "publish_asset_request": (
                "artifact_id", "operation", "input_artifacts", "execution_binding",
                "output_targets", "attempt_id",
            ),
            "publish_asset_result": (
                "success", "operation", "output_artifacts", "attempt_id", "adapter_name",
                "adapter_version", "production_eligible", "usage_events", "failure",
            ),
        },
        string_fields={
            "publish_asset_request": ("artifact_id", "operation", "attempt_id"),
            "publish_asset_result": ("operation", "attempt_id", "adapter_name", "adapter_version"),
        },
        array_fields={
            "publish_asset_request": ("input_artifacts", "output_targets"),
            "publish_asset_result": ("output_artifacts", "usage_events"),
        },
        array_item_types={
            "publish_asset_request": {"input_artifacts": Mapping, "output_targets": str},
            "publish_asset_result": {"output_artifacts": Mapping, "usage_events": Mapping},
        },
        object_fields={"publish_asset_request": ("execution_binding",)},
        object_array_value_fields={},
        boolean_fields={"publish_asset_result": ("success", "production_eligible")},
        nullable_object_fields={"publish_asset_result": ("failure",)},
    )
    kind = payload.get("kind")
    field_name = "input_artifacts" if kind == "publish_asset_request" else "output_artifacts"
    required = (
        ("role", "path", "sha256")
        if kind == "publish_asset_request"
        else ("path", "sha256", "production_eligible")
    )
    artifacts = payload.get(field_name)
    if isinstance(artifacts, list):
        for index, artifact in enumerate(artifacts):
            if not isinstance(artifact, Mapping):
                continue
            for name in required:
                if name not in artifact:
                    issues.append(f"missing:{field_name}[{index}].{name}")
                elif name == "production_eligible":
                    if type(artifact[name]) is not bool:
                        issues.append(f"type:{field_name}[{index}].{name}")
                elif not isinstance(artifact[name], str):
                    issues.append(f"type:{field_name}[{index}].{name}")
    return issues


def _validate_external_execution_payload(
    payload: Mapping[str, Any],
    *,
    schema_version: str,
    request_kind: str,
    result_kind: str,
) -> list[str]:
    return _validate_front_half_payload(
        payload,
        schema_version=schema_version,
        required_by_kind={
            request_kind: (
                "artifact_id", "operation", "execution_request_path", "execution_request_sha256",
                "input_artifacts", "output_targets", "attempt_id",
            ),
            result_kind: (
                "success", "operation", "output_artifacts", "provider", "model_or_tool", "request_id",
                "attempt_id", "execution_request_sha256", "adapter_version", "production_eligible",
                "usage_events", "failure",
            ),
        },
        string_fields={
            request_kind: (
                "artifact_id", "operation", "execution_request_path", "execution_request_sha256", "attempt_id",
            ),
            result_kind: (
                "operation", "provider", "model_or_tool", "request_id", "attempt_id",
                "execution_request_sha256", "adapter_version",
            ),
        },
        array_fields={
            request_kind: ("input_artifacts", "output_targets"),
            result_kind: ("output_artifacts", "usage_events"),
        },
        array_item_types={
            request_kind: {"input_artifacts": Mapping, "output_targets": str},
            result_kind: {"output_artifacts": Mapping, "usage_events": Mapping},
        },
        object_fields={},
        object_array_value_fields={},
        boolean_fields={result_kind: ("success", "production_eligible")},
        nullable_object_fields={result_kind: ("failure",)},
    )


def _versioned_payload(schema_version: str, kind: str, value: Any) -> dict[str, Any]:
    payload = _json_value(asdict(value))
    payload["kind"] = kind
    payload["schema_version"] = schema_version
    return payload


def _validate_front_half_payload(
    payload: Mapping[str, Any],
    *,
    schema_version: str,
    required_by_kind: Mapping[str, Sequence[str]],
    string_fields: Mapping[str, Sequence[str]],
    array_fields: Mapping[str, Sequence[str]],
    array_item_types: Mapping[str, Mapping[str, type]],
    object_fields: Mapping[str, Sequence[str]],
    object_array_value_fields: Mapping[str, Sequence[str]],
    boolean_fields: Mapping[str, Sequence[str]],
    nullable_object_fields: Mapping[str, Sequence[str]],
) -> list[str]:
    if not isinstance(payload, Mapping):
        return ["payload_not_object"]
    issues: list[str] = []
    kind = str(payload.get("kind") or "")
    if payload.get("schema_version") != schema_version:
        issues.append("schema_version")
    if kind not in required_by_kind:
        return issues + ["kind"]
    for field_name in required_by_kind[kind]:
        if field_name not in payload:
            issues.append(f"missing:{field_name}")
    for field_name in string_fields.get(kind, ()):
        if field_name in payload and not isinstance(payload[field_name], str):
            issues.append(f"type:{field_name}")
    for field_name in array_fields.get(kind, ()):
        if field_name in payload and not isinstance(payload[field_name], list):
            issues.append(f"type:{field_name}")
        elif field_name in payload:
            expected = array_item_types.get(kind, {}).get(field_name)
            if expected is not None and any(not isinstance(item, expected) for item in payload[field_name]):
                issues.append(f"items:{field_name}")
    for field_name in object_fields.get(kind, ()):
        if field_name in payload and not isinstance(payload[field_name], Mapping):
            issues.append(f"type:{field_name}")
    for field_name in object_array_value_fields.get(kind, ()):
        value = payload.get(field_name)
        if isinstance(value, Mapping) and any(
            not isinstance(items, list)
            or any(isinstance(item, bool) or not isinstance(item, int) for item in items)
            for items in value.values()
        ):
            issues.append(f"values:{field_name}")
    for field_name in boolean_fields.get(kind, ()):
        if field_name in payload and type(payload[field_name]) is not bool:
            issues.append(f"type:{field_name}")
    for field_name in nullable_object_fields.get(kind, ()):
        value = payload.get(field_name)
        if value is not None and not isinstance(value, Mapping):
            issues.append(f"type:{field_name}")
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
    "COMPOSITOR_PORT_VERSION", "IMAGE_GENERATOR_PORT_VERSION", "KEYER_PORT_VERSION", "MODULE_PORT_SCHEMA_VERSION",
    "PUBLISH_ASSET_PORT_VERSION", "RELEASE_LAYOUT_PORT_VERSION",
    "MUSIC_PROVIDER_PORT_VERSION", "PRODUCT_PACKAGE_PORT_VERSION", "STORY_SEMANTICS_COMPILER_VERSION",
    "STORY_SEMANTIC_KINDS",
    "STORY_SEMANTICS_PORT_VERSION", "VIDEO_GENERATOR_PORT_VERSION", "VISUAL_DESIGN_PORT_VERSION",
    "CompositorExecutor", "CompositorPort", "CompositorRequest", "CompositorResult",
    "ImageGeneratorExecutor", "ImageGeneratorPort", "ImageGeneratorRequest", "ImageGeneratorResult",
    "KeyerPort", "KeyerRequest", "KeyerResult", "ModuleCapabilities", "ModuleFailure",
    "ModuleFailureCode", "ModuleIdentity", "ModuleUsageEvent", "MusicProviderExecutor",
    "MusicProviderPort", "MusicProviderRequest", "MusicProviderResult", "ProductPackageExecutor",
    "ProductPackagePort", "ProductPackageRequest", "ProductPackageResult", "StorySemanticsPort",
    "PublishAssetExecutor", "PublishAssetPort", "PublishAssetRequest", "PublishAssetResult",
    "ReleaseLayoutExecutor", "ReleaseLayoutPort", "ReleaseLayoutRequest", "ReleaseLayoutResult",
    "StorySemanticsRequest", "StorySemanticsResult", "VideoGeneratorPort", "VideoGeneratorRequest",
    "VideoGeneratorResult", "VisualDesignPort", "VisualDesignRequest", "VisualDesignResult",
    "compositor_payload", "image_generator_payload", "module_payload", "music_provider_payload", "product_package_payload",
    "publish_asset_payload", "release_layout_payload",
    "story_semantics_payload",
    "validate_image_generator_payload", "validate_module_payload", "validate_music_provider_payload",
    "validate_compositor_payload", "validate_product_package_payload", "validate_publish_asset_payload", "validate_release_layout_payload",
    "validate_story_semantics_payload", "validate_visual_design_payload",
    "visual_design_payload",
]
