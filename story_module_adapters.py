from __future__ import annotations

import csv
import hashlib
import struct
import sys
import shutil
import tempfile
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from production_keying import (
    PRODUCTION_KEYING_FILTER_VERSION,
    production_keying_contract,
    production_keying_filter_chain,
    production_keying_filter_parts,
    production_keying_fingerprint,
    render_production_keyed_foreground,
)
from story_module_ports import (
    COMPOSITOR_PORT_VERSION,
    IMAGE_GENERATOR_PORT_VERSION,
    KEYER_PORT_VERSION,
    MUSIC_PROVIDER_PORT_VERSION,
    PRODUCT_PACKAGE_PORT_VERSION,
    RELEASE_LAYOUT_PORT_VERSION,
    STORY_SEMANTICS_COMPILER_VERSION,
    STORY_SEMANTICS_PORT_VERSION,
    STORY_SEMANTIC_KINDS,
    VIDEO_GENERATOR_PORT_VERSION,
    VISUAL_DESIGN_PORT_VERSION,
    CompositorExecutor,
    CompositorRequest,
    CompositorResult,
    ImageGeneratorExecutor,
    ImageGeneratorRequest,
    ImageGeneratorResult,
    KeyerRequest,
    KeyerResult,
    ModuleCapabilities,
    ModuleFailure,
    ModuleFailureCode,
    ModuleIdentity,
    ModuleUsageEvent,
    MusicProviderExecutor,
    MusicProviderRequest,
    MusicProviderResult,
    ProductPackageExecutor,
    ProductPackageRequest,
    ProductPackageResult,
    ReleaseLayoutExecutor,
    ReleaseLayoutRequest,
    ReleaseLayoutResult,
    StorySemanticsRequest,
    StorySemanticsResult,
    VideoGeneratorRequest,
    VideoGeneratorResult,
    VisualDesignRequest,
    VisualDesignResult,
)
from story_contracts import StoryContractValidationError, canonical_json_bytes, load_story_contract
from story_semantics import StoryOutput, classify_story, lines_for_output
from video_provider_adapter import VideoProviderAdapter, resolve_row_generation_seconds


VIDEO_ADAPTER_VERSION = "story-existing-video-adapter/v1"
KEYER_ADAPTER_VERSION = "story-production-ffmpeg-keyer/v1"
VIDEO_BATCH_INVOCATION_VERSION = "story-video-batch-invocation/v1"
STORY_SEMANTICS_ADAPTER_VERSION = "story-existing-semantics-adapter/v1"
VISUAL_DESIGN_ADAPTER_VERSION = "story-approved-contract-visual-design-adapter/v1"
IMAGE_GENERATOR_ADAPTER_VERSION = "story-codex-imagegen-adapter/v1"
MUSIC_PROVIDER_ADAPTER_VERSION = "story-suno-browser-adapter/v1"
PRODUCT_PACKAGE_ADAPTER_VERSION = "story-local-product-package-adapter/v1"
COMPOSITOR_ADAPTER_VERSION = "story-local-ffmpeg-compositor-adapter/v1"
RELEASE_LAYOUT_ADAPTER_VERSION = "story-local-release-layout-adapter/v1"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value.lower())


class ExistingStorySemanticsAdapter:
    """Thin deterministic adapter around the existing story_semantics module."""

    identity = ModuleIdentity(
        "story_semantics", STORY_SEMANTICS_PORT_VERSION, "story-semantics", STORY_SEMANTICS_ADAPTER_VERSION
    )
    capabilities = ModuleCapabilities(
        provider="local",
        model_or_tool="story_semantics.py",
        runner_or_tool="in-process",
        external=False,
        paid=False,
        deterministic=True,
        supported={
            "classify_full_story": True,
            "semantic_kind_by_source_line": True,
            "lines_for_output": True,
            "source_sha256_binding": True,
        },
    )

    def analyze(self, request: StorySemanticsRequest) -> StorySemanticsResult:
        issue = self._input_issue(request)
        if issue:
            return self._failure(request, ModuleFailureCode.INVALID_INPUT, issue)
        try:
            semantics = classify_story(request.normalized_lines)
            payload = semantics.to_dict()
            line_payload = tuple(
                {
                    **line,
                    "semantic_kind": (
                        semantics.kind_at(int(line["line_number"])).value
                        if semantics.kind_at(int(line["line_number"])) is not None
                        else ""
                    ),
                }
                for line in payload["lines"]
            )
            outputs = {
                output.value: tuple(line.line_number for line in lines_for_output(semantics, output))
                for output in StoryOutput
            }
        except Exception as exc:
            return self._failure(request, ModuleFailureCode.EXECUTION_FAILED, str(exc))
        return StorySemanticsResult(
            True,
            request.source_path,
            request.source_sha256,
            semantics.title or "",
            line_payload,
            tuple(payload["segments"]),
            outputs,
            STORY_SEMANTICS_ADAPTER_VERSION,
            STORY_SEMANTICS_COMPILER_VERSION,
            usage_events=(
                ModuleUsageEvent(
                    "local", "story_semantics.py", "story_semantics_analysis", actual_amount_status="not_applicable"
                ),
            ),
        )

    @staticmethod
    def _input_issue(request: StorySemanticsRequest) -> str:
        if not isinstance(request.source_path, Path) or not request.source_path.is_file():
            return "semantic source is missing"
        if not isinstance(request.source_sha256, str) or not _valid_sha256(request.source_sha256):
            return "semantic source hash is missing or stale"
        try:
            if file_sha256(request.source_path) != request.source_sha256:
                return "semantic source hash is missing or stale"
        except OSError:
            return "semantic source is unreadable"
        if (
            not isinstance(request.normalized_lines, tuple)
            or not request.normalized_lines
            or any(not isinstance(line, str) for line in request.normalized_lines)
        ):
            return "normalized semantic lines are missing or invalid"
        if not isinstance(request.compiler_version, str) or request.compiler_version != STORY_SEMANTICS_COMPILER_VERSION:
            return "semantic compiler version binding is unsupported"
        if not isinstance(request.attempt_id, str) or not request.attempt_id.strip():
            return "semantic attempt id is missing"
        return ""

    @staticmethod
    def _failure(
        request: StorySemanticsRequest, code: ModuleFailureCode, message: str
    ) -> StorySemanticsResult:
        return StorySemanticsResult(
            False,
            request.source_path,
            request.source_sha256,
            "",
            (),
            (),
            {},
            STORY_SEMANTICS_ADAPTER_VERSION,
            STORY_SEMANTICS_COMPILER_VERSION,
            failure=ModuleFailure(code, message),
        )


class MockStorySemanticsAdapter(ExistingStorySemanticsAdapter):
    """Deterministic fixture adapter; it is not an alternative classifier."""

    identity = ModuleIdentity(
        "story_semantics", STORY_SEMANTICS_PORT_VERSION, "mock-semantics", "story-mock-semantics/v1"
    )
    capabilities = ModuleCapabilities(
        provider="mock",
        model_or_tool="known-semantic-fixture",
        runner_or_tool="in-process",
        external=False,
        paid=False,
        deterministic=True,
        supported={"known_fixture": True, "network": False},
    )

    def __init__(
        self,
        *,
        kinds: Sequence[str] = (),
        output_line_numbers: Mapping[str, Sequence[int]] | None = None,
        mode: str = "success",
    ) -> None:
        self.kinds = tuple(str(kind) for kind in kinds)
        self.output_line_numbers = {
            str(output): tuple(int(number) for number in numbers)
            for output, numbers in (output_line_numbers or {}).items()
        }
        self.mode = mode

    def analyze(self, request: StorySemanticsRequest) -> StorySemanticsResult:
        issue = self._input_issue(request)
        if issue:
            return replace(
                self._failure(request, ModuleFailureCode.INVALID_INPUT, issue),
                adapter_version="story-mock-semantics/v1",
            )
        if self.mode == "failure":
            return replace(
                self._failure(request, ModuleFailureCode.EXECUTION_FAILED, "mock execution failure"),
                adapter_version="story-mock-semantics/v1",
            )
        if not self.kinds:
            return replace(super().analyze(request), adapter_version="story-mock-semantics/v1")
        if len(self.kinds) != len(request.normalized_lines) or any(
            kind not in STORY_SEMANTIC_KINDS for kind in self.kinds
        ):
            return replace(
                self._failure(request, ModuleFailureCode.INVALID_INPUT, "mock fixture does not match source lines"),
                adapter_version="story-mock-semantics/v1",
            )
        lines = tuple(
            {"line_number": index, "text": text, "semantic_kind": kind}
            for index, (text, kind) in enumerate(zip(request.normalized_lines, self.kinds), start=1)
        )
        segments: list[dict[str, Any]] = []
        for index, kind in enumerate(self.kinds, start=1):
            if segments and segments[-1]["kind"] == kind:
                segments[-1]["end_line"] = index
                segments[-1]["line_numbers"].append(index)
                segments[-1]["text"] += "\n" + request.normalized_lines[index - 1]
            else:
                segments.append(
                    {
                        "kind": kind,
                        "start_line": index,
                        "end_line": index,
                        "line_numbers": [index],
                        "text": request.normalized_lines[index - 1],
                    }
                )
        title = next((line["text"] for line in lines if line["semantic_kind"] == "title"), "")
        return StorySemanticsResult(
            True,
            request.source_path,
            request.source_sha256,
            title,
            lines,
            tuple(segments),
            self.output_line_numbers,
            "story-mock-semantics/v1",
            STORY_SEMANTICS_COMPILER_VERSION,
        )


class ApprovedStoryContractVisualDesignAdapter:
    """Resolve only the current reviewed-and-locked Story Contract projection."""

    identity = ModuleIdentity(
        "visual_design", VISUAL_DESIGN_PORT_VERSION, "approved-story-contract", VISUAL_DESIGN_ADAPTER_VERSION
    )
    capabilities = ModuleCapabilities(
        provider="local",
        model_or_tool="story-production-contract",
        runner_or_tool="story_contract_runtime.py",
        external=False,
        paid=False,
        deterministic=True,
        supported={
            "approved_projection": True,
            "review_lock_required": True,
            "preview_asset_references": True,
            "writes_visual_contract": False,
        },
    )

    def resolve(self, request: VisualDesignRequest) -> VisualDesignResult:
        issue = self._input_issue(request)
        if issue:
            return self._failure(request, ModuleFailureCode.INVALID_INPUT, issue)
        try:
            # Keep this import local: story_project's keying helpers import the
            # registry during startup, while story_contract_runtime imports
            # story_project for persistence.  The Port itself remains a thin
            # caller of the existing reviewed-lock fact source.
            from story_contract_runtime import CONTRACT_POLICY_LEGACY, contract_paths, locked_contract_binding

            binding = locked_contract_binding(request.project_root, request.consumer)
            projection = binding["contract_projection"]
            projection_sha = json_sha256(projection)
            if binding.get("mode") != CONTRACT_POLICY_LEGACY:
                if binding["story_contract_sha256"] != request.story_contract_sha256:
                    raise ValueError("story contract binding is stale")
                if projection_sha != request.projection_sha256:
                    raise ValueError("visual projection binding is stale")
                contract = load_story_contract(contract_paths(request.project_root)["contract"])
                references = tuple(
                    dict(item) for item in contract.get("preview_assets", []) if isinstance(item, Mapping)
                )
            else:
                if request.story_contract_sha256 or request.projection_sha256 != projection_sha:
                    raise ValueError("legacy visual projection binding is stale")
                references = ()
        except (OSError, ValueError, KeyError, TypeError, StoryContractValidationError) as exc:
            return self._failure(request, ModuleFailureCode.INVALID_INPUT, str(exc))
        return VisualDesignResult(
            True,
            request.operation,
            projection,
            references,
            projection_sha,
            str(binding["story_contract_sha256"]),
            projection_sha,
            request.story_semantics_sha256,
            request.attempt_id,
            VISUAL_DESIGN_ADAPTER_VERSION,
            usage_events=(
                ModuleUsageEvent(
                    "local", "story_contract_runtime.py", "approved_visual_projection", actual_amount_status="not_applicable"
                ),
            ),
        )

    @staticmethod
    def _input_issue(request: VisualDesignRequest) -> str:
        if not isinstance(request.project_root, Path) or not request.project_root.is_dir():
            return "visual design project root is missing"
        if not isinstance(request.consumer, str) or request.consumer != "storyboard_images":
            return "visual design consumer is unsupported"
        if not isinstance(request.operation, str) or request.operation != "approved_projection":
            return "visual design operation is unsupported"
        if not isinstance(request.story_contract_sha256, str) or (
            request.story_contract_sha256 and not _valid_sha256(request.story_contract_sha256)
        ):
            return "story contract hash is invalid"
        if not isinstance(request.projection_sha256, str) or not _valid_sha256(request.projection_sha256):
            return "visual projection hash is invalid"
        if not isinstance(request.story_semantics_sha256, str) or (
            request.story_semantics_sha256 and not _valid_sha256(request.story_semantics_sha256)
        ):
            return "story semantics binding is invalid"
        if not isinstance(request.current_artifact_references, tuple) or any(
            not isinstance(reference, Mapping) for reference in request.current_artifact_references
        ):
            return "visual artifact references are invalid"
        if not isinstance(request.attempt_id, str) or not request.attempt_id.strip():
            return "visual design attempt id is missing"
        return ""

    @staticmethod
    def _failure(
        request: VisualDesignRequest, code: ModuleFailureCode, message: str
    ) -> VisualDesignResult:
        return VisualDesignResult(
            False,
            request.operation,
            {},
            (),
            "",
            request.story_contract_sha256,
            request.projection_sha256,
            request.story_semantics_sha256,
            request.attempt_id,
            VISUAL_DESIGN_ADAPTER_VERSION,
            failure=ModuleFailure(code, message),
        )


class MockVisualDesignAdapter(ApprovedStoryContractVisualDesignAdapter):
    """Offline substitution that still resolves the sole approved contract fact source."""

    identity = ModuleIdentity(
        "visual_design", VISUAL_DESIGN_PORT_VERSION, "mock-visual-design", "story-mock-visual-design/v1"
    )
    capabilities = ModuleCapabilities(
        provider="mock",
        model_or_tool="approved-projection-fixture",
        runner_or_tool="in-process",
        external=False,
        paid=False,
        deterministic=True,
        supported={"approved_projection_passthrough": True, "network": False, "writes_visual_contract": False},
    )

    def __init__(self, mode: str = "success") -> None:
        self.mode = mode

    def resolve(self, request: VisualDesignRequest) -> VisualDesignResult:
        if self.mode == "failure":
            return replace(
                self._failure(request, ModuleFailureCode.EXECUTION_FAILED, "mock execution failure"),
                adapter_version="story-mock-visual-design/v1",
            )
        return replace(super().resolve(request), adapter_version="story-mock-visual-design/v1")


class CodexImageGeneratorAdapter:
    """Thin adapter around the current Codex/ImageGen execution envelope."""

    identity = ModuleIdentity(
        "image_generator", IMAGE_GENERATOR_PORT_VERSION, "codex-imagegen", IMAGE_GENERATOR_ADAPTER_VERSION
    )
    capabilities = ModuleCapabilities(
        provider="codex",
        model_or_tool="imagegen",
        runner_or_tool="codex exec",
        external=True,
        paid=True,
        deterministic=False,
        supported={
            "preplanned_execution_envelope": True,
            "owns_prompt_planning": False,
            "owns_batching": False,
            "owns_naming": False,
            "owns_quality_policy": False,
            "owns_currentness": False,
        },
    )

    def execute(
        self,
        request: ImageGeneratorRequest,
        *,
        executor: ImageGeneratorExecutor,
    ) -> ImageGeneratorResult:
        return executor(request)


class SunoMusicProviderAdapter:
    """Thin adapter around the current Codex/Ego Browser/Suno envelope."""

    identity = ModuleIdentity(
        "music_provider", MUSIC_PROVIDER_PORT_VERSION, "suno-browser", MUSIC_PROVIDER_ADAPTER_VERSION
    )
    capabilities = ModuleCapabilities(
        provider="suno",
        model_or_tool="current-browser-workflow",
        runner_or_tool="codex exec + Ego Browser",
        external=True,
        paid=True,
        deterministic=False,
        supported={
            "preplanned_execution_envelope": True,
            "owns_music_planning": False,
            "owns_target_naming": False,
            "owns_assembly": False,
            "owns_quality_policy": False,
            "owns_currentness": False,
        },
    )

    def execute(
        self,
        request: MusicProviderRequest,
        *,
        executor: MusicProviderExecutor,
    ) -> MusicProviderResult:
        return executor(request)


class MockImageGeneratorAdapter:
    identity = ModuleIdentity(
        "image_generator", IMAGE_GENERATOR_PORT_VERSION, "mock-image", "story-mock-image-generator/v1"
    )
    capabilities = ModuleCapabilities(
        provider="mock",
        model_or_tool="deterministic-image-fixture",
        runner_or_tool="in-process",
        external=False,
        paid=False,
        deterministic=True,
        supported={"offline": True, "network": False, "production_eligible": False},
    )
    # Fixed 256x256 RGB PNG: decodable by the unchanged visual-sample machine QA.
    _raw = b"".join(b"\x00" + bytes((80, 120, 160)) * 256 for _ in range(256))

    @staticmethod
    def _png_chunk(kind: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)

    _fixture = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk.__func__(b"IHDR", struct.pack(">IIBBBBB", 256, 256, 8, 2, 0, 0, 0))
        + _png_chunk.__func__(b"IDAT", zlib.compress(_raw))
        + _png_chunk.__func__(b"IEND", b"")
    )

    def __init__(self, mode: str = "success") -> None:
        self.mode = mode

    def execute(
        self,
        request: ImageGeneratorRequest,
        *,
        executor: ImageGeneratorExecutor,
    ) -> ImageGeneratorResult:
        del executor
        issue = _external_request_issue(request)
        if issue:
            return self._failure(request, ModuleFailureCode.INVALID_INPUT, issue)
        failure_code = {
            "unsupported": ModuleFailureCode.UNSUPPORTED_CAPABILITY,
            "failure": ModuleFailureCode.EXECUTION_FAILED,
            "invalid_output": ModuleFailureCode.INVALID_OUTPUT,
        }.get(self.mode)
        if failure_code is not None:
            return self._failure(request, failure_code, f"mock {self.mode}")
        artifacts = _write_mock_outputs(request.output_targets, self._fixture)
        return ImageGeneratorResult(
            True,
            request.operation,
            artifacts,
            "mock",
            "deterministic-image-fixture",
            "mock-image-request-0001",
            request.attempt_id,
            request.execution_request_sha256,
            "story-mock-image-generator/v1",
            False,
            usage_events=(
                ModuleUsageEvent(
                    "mock", "deterministic-image-fixture", request.operation,
                    unit_type="image", quantity=float(len(artifacts)), actual_amount_status="not_applicable",
                ),
            ),
        )

    @staticmethod
    def _failure(
        request: ImageGeneratorRequest, code: ModuleFailureCode, message: str
    ) -> ImageGeneratorResult:
        return ImageGeneratorResult(
            False, request.operation, (), "mock", "deterministic-image-fixture", "",
            request.attempt_id, request.execution_request_sha256, "story-mock-image-generator/v1",
            False,
            failure=ModuleFailure(code, message),
        )


class MockMusicProviderAdapter:
    identity = ModuleIdentity(
        "music_provider", MUSIC_PROVIDER_PORT_VERSION, "mock-music", "story-mock-music-provider/v1"
    )
    capabilities = ModuleCapabilities(
        provider="mock",
        model_or_tool="deterministic-music-fixture",
        runner_or_tool="in-process",
        external=False,
        paid=False,
        deterministic=True,
        supported={"offline": True, "network": False, "production_eligible": False},
    )
    _fixture = b"STORY_MODULE_MOCK_MUSIC\n"

    def __init__(self, mode: str = "success") -> None:
        self.mode = mode

    def execute(
        self,
        request: MusicProviderRequest,
        *,
        executor: MusicProviderExecutor,
    ) -> MusicProviderResult:
        del executor
        issue = _external_request_issue(request)
        if issue:
            return self._failure(request, ModuleFailureCode.INVALID_INPUT, issue)
        failure_code = {
            "unsupported": ModuleFailureCode.UNSUPPORTED_CAPABILITY,
            "failure": ModuleFailureCode.EXECUTION_FAILED,
            "invalid_output": ModuleFailureCode.INVALID_OUTPUT,
        }.get(self.mode)
        if failure_code is not None:
            return self._failure(request, failure_code, f"mock {self.mode}")
        artifacts = _write_mock_outputs(request.output_targets, self._fixture)
        return MusicProviderResult(
            True,
            request.operation,
            artifacts,
            "mock",
            "deterministic-music-fixture",
            "mock-music-request-0001",
            request.attempt_id,
            request.execution_request_sha256,
            "story-mock-music-provider/v1",
            False,
            usage_events=(
                ModuleUsageEvent(
                    "mock", "deterministic-music-fixture", request.operation,
                    unit_type="audio", quantity=float(len(artifacts)), actual_amount_status="not_applicable",
                ),
            ),
        )

    @staticmethod
    def _failure(
        request: MusicProviderRequest, code: ModuleFailureCode, message: str
    ) -> MusicProviderResult:
        return MusicProviderResult(
            False, request.operation, (), "mock", "deterministic-music-fixture", "",
            request.attempt_id, request.execution_request_sha256, "story-mock-music-provider/v1",
            False,
            failure=ModuleFailure(code, message),
        )


class LocalProductPackageAdapter:
    """Thin adapter around the caller-owned product filesystem executor."""

    identity = ModuleIdentity(
        "product_package", PRODUCT_PACKAGE_PORT_VERSION, "local-filesystem", PRODUCT_PACKAGE_ADAPTER_VERSION
    )
    capabilities = ModuleCapabilities(
        provider="local",
        model_or_tool="shutil.copy2",
        runner_or_tool="in-process",
        external=False,
        paid=False,
        deterministic=True,
        supported={
            "preplanned_source_destination_mapping": True,
            "copy2": True,
            "directory_backup": True,
            "owns_product_policy": False,
            "owns_naming": False,
            "owns_manifest": False,
            "owns_quality_policy": False,
            "owns_currentness": False,
        },
    )

    def execute(
        self,
        request: ProductPackageRequest,
        *,
        executor: ProductPackageExecutor,
    ) -> ProductPackageResult:
        issue = _product_package_request_issue(request)
        if issue:
            return self._failure(request, ModuleFailureCode.INVALID_INPUT, issue)
        try:
            result = executor(request)
        except Exception as exc:
            return self._failure(request, ModuleFailureCode.EXECUTION_FAILED, str(exc))
        if result.success:
            issue = _product_package_result_issue(request, result)
            if issue:
                return self._failure(request, ModuleFailureCode.INVALID_OUTPUT, issue)
        return result

    @staticmethod
    def _failure(
        request: ProductPackageRequest, code: ModuleFailureCode, message: str
    ) -> ProductPackageResult:
        return ProductPackageResult(
            False,
            request.operation,
            request.package_variant,
            (),
            request.attempt_id,
            PRODUCT_PACKAGE_ADAPTER_VERSION,
            True,
            failure=ModuleFailure(code, message),
        )


class LocalCompositorAdapter:
    """Thin adapter around caller-owned deterministic Python/FFmpeg execution."""

    identity = ModuleIdentity(
        "compositor", COMPOSITOR_PORT_VERSION, "local-ffmpeg", COMPOSITOR_ADAPTER_VERSION
    )
    capabilities = ModuleCapabilities(
        provider="local",
        model_or_tool="existing-python-ffmpeg-compositor",
        runner_or_tool="in-process",
        external=False,
        paid=False,
        deterministic=True,
        supported={
            "background_story": True,
            "presenter_demo": True,
            "a_only_background": True,
            "owns_timeline_policy": False,
            "owns_subtitle_policy": False,
            "owns_music_policy": False,
            "owns_geometry_policy": False,
            "owns_manifest": False,
            "owns_quality_policy": False,
            "owns_currentness": False,
        },
    )

    def execute(
        self,
        request: CompositorRequest,
        *,
        executor: CompositorExecutor,
    ) -> CompositorResult:
        issue = _compositor_request_issue(request)
        if issue:
            return self._failure(request, ModuleFailureCode.INVALID_INPUT, issue)
        try:
            result = executor(request)
        except Exception as exc:
            return self._failure(request, ModuleFailureCode.EXECUTION_FAILED, str(exc))
        if result.success:
            issue = _compositor_result_issue(request, result)
            if issue:
                return self._failure(request, ModuleFailureCode.INVALID_OUTPUT, issue)
        return result

    @staticmethod
    def _failure(
        request: CompositorRequest, code: ModuleFailureCode, message: str
    ) -> CompositorResult:
        return CompositorResult(
            False,
            request.operation,
            (),
            request.attempt_id,
            COMPOSITOR_ADAPTER_VERSION,
            True,
            failure=ModuleFailure(code, message),
        )


class MockCompositorAdapter:
    """Offline deterministic mock that never calls the production executor."""

    identity = ModuleIdentity(
        "compositor", COMPOSITOR_PORT_VERSION, "mock-compositor", "story-mock-compositor/v1"
    )
    capabilities = ModuleCapabilities(
        provider="mock",
        model_or_tool="deterministic-media-marker",
        runner_or_tool="in-process",
        external=False,
        paid=False,
        deterministic=True,
        supported={"offline": True, "network": False, "production_eligible": False},
    )

    def __init__(self, mode: str = "success") -> None:
        self.mode = mode

    def execute(
        self,
        request: CompositorRequest,
        *,
        executor: CompositorExecutor,
    ) -> CompositorResult:
        del executor
        issue = _compositor_request_issue(request)
        if issue:
            return self._failure(request, ModuleFailureCode.INVALID_INPUT, issue)
        failure_code = {
            "unsupported": ModuleFailureCode.UNSUPPORTED_CAPABILITY,
            "failure": ModuleFailureCode.EXECUTION_FAILED,
            "invalid_output": ModuleFailureCode.INVALID_OUTPUT,
        }.get(self.mode)
        if failure_code is not None:
            return self._failure(request, failure_code, f"mock {self.mode}")
        temporary_root = Path(tempfile.gettempdir()).resolve()
        for target in request.output_targets:
            try:
                target.resolve().relative_to(temporary_root)
            except ValueError:
                return self._failure(
                    request,
                    ModuleFailureCode.CONFIGURATION_ERROR,
                    "mock compositor targets must be inside the system temporary directory",
                )
        mock_root = request.output_targets[0].parent / "_mock_compositor"
        artifacts: list[Mapping[str, Any]] = []
        for index, requested in enumerate(request.output_targets, start=1):
            target = mock_root / f"{index:02d}_{requested.name}"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"story-compositor-mock/v1\n")
            artifacts.append(
                {"path": str(target), "sha256": file_sha256(target), "production_eligible": False}
            )
        return CompositorResult(
            True,
            request.operation,
            tuple(artifacts),
            request.attempt_id,
            "story-mock-compositor/v1",
            False,
            usage_events=(
                ModuleUsageEvent(
                    "mock", "deterministic-media-marker", request.operation,
                    unit_type="artifact", quantity=float(len(artifacts)), actual_amount_status="not_applicable",
                ),
            ),
        )

    @staticmethod
    def _failure(
        request: CompositorRequest, code: ModuleFailureCode, message: str
    ) -> CompositorResult:
        return CompositorResult(
            False,
            request.operation,
            (),
            request.attempt_id,
            "story-mock-compositor/v1",
            False,
            failure=ModuleFailure(code, message),
        )


class LocalReleaseLayoutAdapter:
    """Thin adapter around caller-owned release layout render execution."""

    identity = ModuleIdentity(
        "release_layout", RELEASE_LAYOUT_PORT_VERSION, "local-release-layout",
        RELEASE_LAYOUT_ADAPTER_VERSION,
    )
    capabilities = ModuleCapabilities(
        provider="local",
        model_or_tool="existing-release-python-ffmpeg-layout",
        runner_or_tool="in-process",
        external=False,
        paid=False,
        deterministic=True,
        supported={
            "preview_render": True,
            "main_wide_render": True,
            "vertical_package_render": True,
            "library_window_render": True,
            "plate_package_render": True,
            "owns_product_policy": False,
            "owns_geometry_policy": False,
            "owns_keying_policy": False,
            "owns_manifest": False,
            "owns_quality_policy": False,
            "owns_currentness": False,
        },
    )

    def execute(
        self,
        request: ReleaseLayoutRequest,
        *,
        executor: ReleaseLayoutExecutor,
    ) -> ReleaseLayoutResult:
        issue = _release_layout_request_issue(request)
        if issue:
            return self._failure(request, ModuleFailureCode.INVALID_INPUT, issue)
        try:
            result = executor(request)
        except Exception as exc:
            return self._failure(request, ModuleFailureCode.EXECUTION_FAILED, str(exc))
        if result.success:
            issue = _release_layout_result_issue(request, result)
            if issue:
                return self._failure(request, ModuleFailureCode.INVALID_OUTPUT, issue)
        return result

    @staticmethod
    def _failure(
        request: ReleaseLayoutRequest, code: ModuleFailureCode, message: str
    ) -> ReleaseLayoutResult:
        return ReleaseLayoutResult(
            False, request.operation, None, request.attempt_id,
            "local-release-layout", RELEASE_LAYOUT_ADAPTER_VERSION, True,
            failure=ModuleFailure(code, message),
        )


class MockReleaseLayoutAdapter:
    """Offline mock that never calls the production release renderer."""

    identity = ModuleIdentity(
        "release_layout", RELEASE_LAYOUT_PORT_VERSION, "mock-release-layout",
        "story-mock-release-layout/v1",
    )
    capabilities = ModuleCapabilities(
        provider="mock",
        model_or_tool="deterministic-release-marker",
        runner_or_tool="in-process",
        external=False,
        paid=False,
        deterministic=True,
        supported={"offline": True, "network": False, "production_eligible": False},
    )

    def __init__(self, mode: str = "success") -> None:
        self.mode = mode

    def execute(
        self,
        request: ReleaseLayoutRequest,
        *,
        executor: ReleaseLayoutExecutor,
    ) -> ReleaseLayoutResult:
        del executor
        issue = _release_layout_request_issue(request)
        if issue:
            return self._failure(request, ModuleFailureCode.INVALID_INPUT, issue)
        failure_code = {
            "unsupported": ModuleFailureCode.UNSUPPORTED_CAPABILITY,
            "failure": ModuleFailureCode.EXECUTION_FAILED,
            "invalid_output": ModuleFailureCode.INVALID_OUTPUT,
        }.get(self.mode)
        if failure_code is not None:
            return self._failure(request, failure_code, f"mock {self.mode}")
        try:
            request.output_target.resolve().relative_to(Path(tempfile.gettempdir()).resolve())
        except ValueError:
            return self._failure(
                request,
                ModuleFailureCode.CONFIGURATION_ERROR,
                "mock release layout target must be inside the system temporary directory",
            )
        target = request.output_target.parent / "_mock_release_layout" / request.output_target.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"story-release-layout-mock/v1\n")
        return ReleaseLayoutResult(
            True,
            request.operation,
            {"path": str(target), "sha256": file_sha256(target), "production_eligible": False},
            request.attempt_id,
            "mock-release-layout",
            "story-mock-release-layout/v1",
            False,
            usage_events=(
                ModuleUsageEvent(
                    "mock", "deterministic-release-marker", request.operation,
                    unit_type="render", quantity=1.0, actual_amount_status="not_applicable",
                ),
            ),
        )

    @staticmethod
    def _failure(
        request: ReleaseLayoutRequest, code: ModuleFailureCode, message: str
    ) -> ReleaseLayoutResult:
        return ReleaseLayoutResult(
            False, request.operation, None, request.attempt_id,
            "mock-release-layout", "story-mock-release-layout/v1", False,
            failure=ModuleFailure(code, message),
        )


class MockProductPackageAdapter:
    """Offline fixture adapter that never writes the requested customer targets."""

    identity = ModuleIdentity(
        "product_package", PRODUCT_PACKAGE_PORT_VERSION, "mock-product-package", "story-mock-product-package/v1"
    )
    capabilities = ModuleCapabilities(
        provider="mock",
        model_or_tool="copy2-fixture",
        runner_or_tool="in-process",
        external=False,
        paid=False,
        deterministic=True,
        supported={"offline": True, "network": False, "production_eligible": False},
    )

    def __init__(self, mode: str = "success") -> None:
        self.mode = mode

    def execute(
        self,
        request: ProductPackageRequest,
        *,
        executor: ProductPackageExecutor,
    ) -> ProductPackageResult:
        del executor
        issue = _product_package_request_issue(request)
        if issue:
            return self._failure(request, ModuleFailureCode.INVALID_INPUT, issue)
        failure_code = {
            "unsupported": ModuleFailureCode.UNSUPPORTED_CAPABILITY,
            "failure": ModuleFailureCode.EXECUTION_FAILED,
            "invalid_output": ModuleFailureCode.INVALID_OUTPUT,
        }.get(self.mode)
        if failure_code is not None:
            return self._failure(request, failure_code, f"mock {self.mode}")
        try:
            request.output_root.resolve().relative_to(Path(tempfile.gettempdir()).resolve())
        except ValueError:
            return self._failure(
                request,
                ModuleFailureCode.CONFIGURATION_ERROR,
                "mock product package output root must be inside the system temporary directory",
            )
        fixture_root = request.output_root / "_mock_product_package"
        artifacts: list[Mapping[str, Any]] = []
        for index, item in enumerate(request.source_artifacts, start=1):
            source = Path(str(item["source_path"]))
            variant = str(item["package_variant"])
            requested = Path(str(item["destination_path"]))
            target = fixture_root / variant / f"{index:02d}_{requested.name}"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            artifacts.append(
                {
                    "package_variant": variant,
                    "source_path": str(source),
                    "source_sha256": str(item["source_sha256"]),
                    "path": str(target),
                    "sha256": file_sha256(target),
                    "production_eligible": False,
                }
            )
        return ProductPackageResult(
            True,
            request.operation,
            request.package_variant,
            tuple(artifacts),
            request.attempt_id,
            "story-mock-product-package/v1",
            False,
            usage_events=(
                ModuleUsageEvent(
                    "mock", "copy2-fixture", request.operation,
                    unit_type="file", quantity=float(len(artifacts)), actual_amount_status="not_applicable",
                ),
            ),
        )

    @staticmethod
    def _failure(
        request: ProductPackageRequest, code: ModuleFailureCode, message: str
    ) -> ProductPackageResult:
        return ProductPackageResult(
            False,
            request.operation,
            request.package_variant,
            (),
            request.attempt_id,
            "story-mock-product-package/v1",
            False,
            failure=ModuleFailure(code, message),
        )


def _product_package_request_issue(request: ProductPackageRequest) -> str:
    if not request.artifact_id.strip() or not request.operation.strip() or not request.attempt_id.strip():
        return "product package execution identity is incomplete"
    if request.operation != "copy_product_packages" or request.package_variant != "base_and_advanced":
        return "product package execution operation or variant is unsupported"
    if not isinstance(request.output_root, Path) or not request.source_artifacts or not request.output_targets:
        return "product package filesystem mapping is missing"
    if len(request.source_artifacts) != len(request.output_targets):
        return "product package source and output counts differ"
    destinations: list[Path] = []
    output_root = request.output_root.resolve()
    for item in request.source_artifacts:
        if not isinstance(item, Mapping):
            return "product package source artifact is invalid"
        if str(item.get("package_variant") or "") not in {"base", "advanced"}:
            return "product package artifact variant is invalid"
        source = Path(str(item.get("source_path") or ""))
        source_sha = str(item.get("source_sha256") or "")
        destination = Path(str(item.get("destination_path") or ""))
        if not source.is_file():
            return f"product package source is missing: {source}"
        if not _valid_sha256(source_sha) or file_sha256(source) != source_sha:
            return f"product package source is stale: {source}"
        try:
            destination.resolve().relative_to(output_root)
        except ValueError:
            return f"product package destination escapes output root: {destination}"
        if destination.resolve() == source.resolve():
            return f"product package destination aliases source: {destination}"
        destinations.append(destination)
    if tuple(destinations) != request.output_targets:
        return "product package output targets do not match the planned mapping"
    if len(set(destinations)) != len(destinations):
        return "product package output targets are duplicated"
    return ""


def _product_package_result_issue(
    request: ProductPackageRequest, result: ProductPackageResult
) -> str:
    if (
        result.operation != request.operation
        or result.package_variant != request.package_variant
        or result.attempt_id != request.attempt_id
        or result.production_eligible is not True
    ):
        return "product package executor result binding mismatch"
    if len(result.output_artifacts) != len(request.output_targets):
        return "product package executor output count mismatch"
    observed: list[Path] = []
    for index, artifact in enumerate(result.output_artifacts):
        if not isinstance(artifact, Mapping):
            return "product package executor output artifact is invalid"
        source = request.source_artifacts[index]
        if (
            artifact.get("package_variant") != source.get("package_variant")
            or artifact.get("source_path") != source.get("source_path")
            or artifact.get("source_sha256") != source.get("source_sha256")
            or artifact.get("production_eligible") is not True
        ):
            return "product package executor source/output binding mismatch"
        path = Path(str(artifact.get("path") or ""))
        if not path.is_file() or artifact.get("sha256") != file_sha256(path):
            return f"product package executor output is missing or stale: {path}"
        observed.append(path)
    if tuple(observed) != request.output_targets:
        return "product package executor outputs do not match planned targets"
    return ""


def _compositor_request_issue(request: CompositorRequest) -> str:
    if not request.artifact_id.strip() or not request.operation.strip() or not request.attempt_id.strip():
        return "compositor execution identity is incomplete"
    if request.operation not in {"background_story", "presenter_demo", "a_only_background"}:
        return "compositor operation is unsupported"
    if not request.input_artifacts or not request.output_targets or not request.execution_binding:
        return "compositor execution binding is incomplete"
    if any(not isinstance(target, Path) for target in request.output_targets):
        return "compositor output targets are invalid"
    if len(set(request.output_targets)) != len(request.output_targets):
        return "compositor output targets are duplicated"
    for artifact in request.input_artifacts:
        if not isinstance(artifact, Mapping):
            return "compositor input artifact is invalid"
        role = str(artifact.get("role") or "")
        path = Path(str(artifact.get("path") or ""))
        expected_sha = str(artifact.get("sha256") or "")
        if not role:
            return "compositor input role is missing"
        if not path.is_file():
            return f"compositor input is missing: {path}"
        if not _valid_sha256(expected_sha) or file_sha256(path) != expected_sha:
            return f"compositor input is stale: {path}"
    return ""


def _compositor_result_issue(request: CompositorRequest, result: CompositorResult) -> str:
    if (
        result.operation != request.operation
        or result.attempt_id != request.attempt_id
        or result.production_eligible is not True
    ):
        return "compositor executor result binding mismatch"
    if len(result.output_artifacts) != len(request.output_targets):
        return "compositor executor output count mismatch"
    observed: list[Path] = []
    for artifact in result.output_artifacts:
        if not isinstance(artifact, Mapping):
            return "compositor executor output artifact is invalid"
        path = Path(str(artifact.get("path") or ""))
        if artifact.get("production_eligible") is not True:
            return "compositor executor output is not production eligible"
        if not path.is_file() or artifact.get("sha256") != file_sha256(path):
            return f"compositor executor output is missing or stale: {path}"
        observed.append(path)
    if tuple(observed) != request.output_targets:
        return "compositor executor outputs do not match planned targets"
    return ""


def _release_layout_request_issue(request: ReleaseLayoutRequest) -> str:
    if not request.artifact_id.strip() or not request.operation.strip() or not request.attempt_id.strip():
        return "release layout execution identity is incomplete"
    if request.operation not in {
        "preview_main", "preview_library", "main_wide_render", "main_vertical_render",
        "library_window_render", "library_vertical_render", "plate_package_render",
    }:
        return "release layout operation is unsupported"
    if not request.input_artifacts or not request.layout_binding or not isinstance(request.output_target, Path):
        return "release layout execution binding is incomplete"
    for artifact in request.input_artifacts:
        if not isinstance(artifact, Mapping):
            return "release layout input artifact is invalid"
        role = str(artifact.get("role") or "")
        path = Path(str(artifact.get("path") or ""))
        expected_sha = str(artifact.get("sha256") or "")
        if not role:
            return "release layout input role is missing"
        if not path.is_file():
            return f"release layout input is missing: {path}"
        if not _valid_sha256(expected_sha) or file_sha256(path) != expected_sha:
            return f"release layout input is stale: {path}"
    return ""


def _release_layout_result_issue(
    request: ReleaseLayoutRequest, result: ReleaseLayoutResult
) -> str:
    if (
        result.operation != request.operation
        or result.attempt_id != request.attempt_id
        or result.production_eligible is not True
    ):
        return "release layout executor result binding mismatch"
    artifact = result.output_artifact
    if not isinstance(artifact, Mapping) or artifact.get("production_eligible") is not True:
        return "release layout executor output artifact is invalid"
    path = Path(str(artifact.get("path") or ""))
    if path != request.output_target:
        return "release layout executor output does not match planned target"
    if not path.is_file() or artifact.get("sha256") != file_sha256(path):
        return f"release layout executor output is missing or stale: {path}"
    return ""


def _external_request_issue(request: ImageGeneratorRequest | MusicProviderRequest) -> str:
    if not request.artifact_id.strip() or not request.operation.strip() or not request.attempt_id.strip():
        return "external execution identity is incomplete"
    if not request.execution_request_path.is_file():
        return "external execution request is missing"
    if not _valid_sha256(request.execution_request_sha256):
        return "external execution request hash is invalid"
    if file_sha256(request.execution_request_path) != request.execution_request_sha256:
        return "external execution request is stale"
    if not request.output_targets or any(not isinstance(path, Path) for path in request.output_targets):
        return "external execution output targets are missing or invalid"
    if len(set(request.output_targets)) != len(request.output_targets):
        return "external execution output targets are duplicated"
    if any(not isinstance(item, Mapping) for item in request.input_artifacts):
        return "external execution input artifacts are invalid"
    return ""


def _write_mock_outputs(targets: Sequence[Path], content: bytes) -> tuple[Mapping[str, Any], ...]:
    artifacts: list[Mapping[str, Any]] = []
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        artifacts.append(
            {"path": str(target), "sha256": file_sha256(target), "production_eligible": False}
        )
    return tuple(artifacts)


class ExistingVideoGeneratorAdapter:
    """Thin Port adapter around the existing provider configuration/runner."""

    def __init__(
        self,
        provider: VideoProviderAdapter,
        *,
        executor: Callable[[VideoGeneratorRequest], VideoGeneratorResult] | None = None,
    ) -> None:
        self.provider_config = provider
        self._executor = executor
        self.identity = ModuleIdentity(
            "video_generator", VIDEO_GENERATOR_PORT_VERSION, provider.name, VIDEO_ADAPTER_VERSION
        )
        self.capabilities = ModuleCapabilities(
            provider=provider.name,
            model_or_tool=provider.model,
            runner_or_tool=str(provider.runner),
            external=True,
            paid=provider.estimated_cost_cny_per_clip > 0 or provider.estimated_cost_cny_per_second > 0,
            deterministic=False,
            supported={
                "image_to_video": True,
                "first_frame_image_count": 1,
                "reference_video": False,
                "reference_audio": False,
                "multi_image_conditioning": False,
                "min_duration": provider.min_seconds,
                "max_duration": provider.max_seconds,
                "default_duration": provider.default_seconds,
                "resolution": provider.default_resolution,
                "ratio": provider.default_ratio,
                "invocation_version": VIDEO_BATCH_INVOCATION_VERSION,
            },
        )

    def estimate_cost(self, seconds: float | None = None) -> float:
        return self.provider_config.estimate_cost(seconds)

    def runner_args(self) -> list[str]:
        return self.provider_config.runner_args()

    def invoke_batch(
        self,
        arguments: Sequence[str],
        *,
        executor: Callable[[Sequence[str]], None],
    ) -> None:
        """Invoke the unchanged batch runner behind the formal Port seam."""

        executor(
            [
                sys.executable,
                str(self.provider_config.runner),
                *self.provider_config.runner_args(),
                *(str(value) for value in arguments),
            ]
        )

    def resolve_request_seconds(self, row: Mapping[str, Any], fallback_seconds: str | int | float) -> str:
        return resolve_row_generation_seconds(
            dict(row), model=self.provider_config.model, fallback_seconds=fallback_seconds,
            min_seconds=self.provider_config.min_seconds, max_seconds=self.provider_config.max_seconds,
        )

    def generate(self, request: VideoGeneratorRequest) -> VideoGeneratorResult:
        if self._executor is None:
            return VideoGeneratorResult(
                False, None, "", self.provider_config.name, self.provider_config.model, "",
                request.requested_duration, None, request.attempt_id, request.source_image_sha256,
                request.prompt_sha256, VIDEO_ADAPTER_VERSION,
                failure=ModuleFailure(
                    ModuleFailureCode.CONFIGURATION_ERROR,
                    "Existing provider execution is owned by the unchanged batch runner.",
                ),
            )
        return self._executor(request)


class ProductionKeyerAdapter:
    """Port adapter delegating to the single production_keying.py fact source."""

    identity = ModuleIdentity("keyer", KEYER_PORT_VERSION, "ffmpeg-production-keyer", KEYER_ADAPTER_VERSION)
    capabilities = ModuleCapabilities(
        provider="local",
        model_or_tool="ffmpeg",
        runner_or_tool="production_keying.py",
        external=False,
        paid=False,
        deterministic=True,
        supported={"colorkey": True, "chromakey": True, "crop": True, "grade": True, "beauty": True},
    )

    def compile_contract(self, settings: Any) -> dict[str, Any]:
        return production_keying_contract(settings)

    def filter_chain(self, source: str, settings: Any, crop_filter: str = "") -> str:
        return production_keying_filter_chain(source, settings, crop_filter)

    def filter_parts(self, source: str, settings: Any, crop_filter: str = "") -> list[str]:
        return production_keying_filter_parts(source, settings, crop_filter)

    def fingerprint(self, settings: Any) -> str:
        return production_keying_fingerprint(settings)

    def render(self, request: KeyerRequest) -> KeyerResult:
        if not request.source_path.is_file() or file_sha256(request.source_path) != request.source_sha256:
            return self._failure(request, ModuleFailureCode.INVALID_INPUT, "Keying source is missing or stale.")
        try:
            render_production_keyed_foreground(request.source_path, request.output_target, request.settings)
        except Exception as exc:
            return self._failure(request, ModuleFailureCode.EXECUTION_FAILED, str(exc))
        if not request.output_target.is_file() or request.output_target.stat().st_size <= 0:
            return self._failure(request, ModuleFailureCode.INVALID_OUTPUT, "Keyer produced no usable output.")
        return KeyerResult(
            True, request.output_target, file_sha256(request.output_target),
            str(self.compile_contract(request.settings)["keyer"]), PRODUCTION_KEYING_FILTER_VERSION,
            self.fingerprint(request.settings), request.source_sha256, request.preset_sha256,
            KEYER_ADAPTER_VERSION,
            usage_events=(ModuleUsageEvent("local", "ffmpeg", "keyed_foreground_render", actual_amount_status="not_applicable"),),
        )

    def _failure(self, request: KeyerRequest, code: ModuleFailureCode, message: str) -> KeyerResult:
        return KeyerResult(
            False, None, "", str(request.settings.get("keyer", "colorkey")),
            PRODUCTION_KEYING_FILTER_VERSION, self.fingerprint(request.settings), request.source_sha256,
            request.preset_sha256, KEYER_ADAPTER_VERSION, failure=ModuleFailure(code, message),
        )


class MockVideoGeneratorAdapter:
    identity = ModuleIdentity("video_generator", VIDEO_GENERATOR_PORT_VERSION, "mock-video", "story-mock-video/v1")
    capabilities = ModuleCapabilities(
        provider="mock", model_or_tool="deterministic-fixture", runner_or_tool="in-process",
        deterministic=True,
        supported={
            "image_to_video": True,
            "first_frame_image_count": 1,
            "default_duration": 1,
            "min_duration": 1,
            "max_duration": 15,
            "invocation_version": VIDEO_BATCH_INVOCATION_VERSION,
        },
    )

    def __init__(self, mode: str = "success") -> None:
        self.mode = mode

    def estimate_cost(self, seconds: float | None = None) -> float:
        return 0.0

    def runner_args(self) -> list[str]:
        return []

    @staticmethod
    def _option(arguments: Sequence[str], name: str, default: str = "") -> str:
        values = [str(value) for value in arguments]
        try:
            return values[values.index(name) + 1]
        except (ValueError, IndexError):
            return default

    def invoke_batch(
        self,
        arguments: Sequence[str],
        *,
        executor: Callable[[Sequence[str]], None],
    ) -> None:
        """Produce deterministic offline fixtures without touching a runner/network."""

        del executor
        if self._option(arguments, "--execution-mode", "production") != "test":
            raise RuntimeError("mock-video is test-only and cannot run in production mode")
        jobs_csv = Path(self._option(arguments, "--jobs-csv")).expanduser()
        videos_dir = Path(self._option(arguments, "--videos-dir")).expanduser()
        if not jobs_csv.is_file() or not str(videos_dir):
            raise ValueError("mock-video invocation requires current jobs and videos paths")
        with jobs_csv.open(encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            rows = list(reader)
            fieldnames = list(reader.fieldnames or [])
        selected = {
            int(value.strip())
            for value in self._option(arguments, "--scenes").split(",")
            if value.strip().isdigit()
        }
        start = int(self._option(arguments, "--start-scene", "1"))
        end = int(self._option(arguments, "--end-scene", "9999"))
        limit = int(self._option(arguments, "--limit", "0"))
        candidates = [
            row for row in rows
            if (int(row.get("scene") or 0) in selected if selected else start <= int(row.get("scene") or 0) <= end)
        ]
        if limit:
            candidates = candidates[:limit]
        videos_dir.mkdir(parents=True, exist_ok=True)
        for name in ("status", "provider", "model", "execution_mode", "source_kind", "production_eligible"):
            if name not in fieldnames:
                fieldnames.append(name)
        for row in candidates:
            target = str(row.get("target_video_filename") or "").strip()
            if not target:
                continue
            (videos_dir / target).write_bytes(b"STORY_MODULE_MOCK_VIDEO\n")
            row.update(
                status="downloaded",
                provider="mock",
                model="deterministic-fixture",
                execution_mode="test",
                source_kind="mock_provider",
                production_eligible="false",
            )
        with jobs_csv.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def resolve_request_seconds(self, row: Mapping[str, Any], fallback_seconds: str | int | float) -> str:
        return str(fallback_seconds)

    def generate(self, request: VideoGeneratorRequest) -> VideoGeneratorResult:
        failure_code = {
            "unsupported": ModuleFailureCode.UNSUPPORTED_CAPABILITY,
            "failure": ModuleFailureCode.EXECUTION_FAILED,
            "invalid_output": ModuleFailureCode.INVALID_OUTPUT,
        }.get(self.mode)
        if failure_code is not None:
            return VideoGeneratorResult(
                False, None, "", "mock", "deterministic-fixture", "mock-request-0001",
                request.requested_duration, None, request.attempt_id, request.source_image_sha256,
                request.prompt_sha256, "story-mock-video/v1",
                failure=ModuleFailure(failure_code, self.mode),
            )
        request.output_target.parent.mkdir(parents=True, exist_ok=True)
        request.output_target.write_bytes(b"STORY_MODULE_MOCK_VIDEO\n")
        return VideoGeneratorResult(
            True, request.output_target, file_sha256(request.output_target), "mock", "deterministic-fixture",
            "mock-request-0001", request.requested_duration, request.requested_duration, request.attempt_id,
            request.source_image_sha256, request.prompt_sha256, "story-mock-video/v1",
        )


class MockKeyerAdapter(ProductionKeyerAdapter):
    identity = ModuleIdentity("keyer", KEYER_PORT_VERSION, "mock-keyer", "story-mock-keyer/v1")
    capabilities = ModuleCapabilities(
        provider="mock", model_or_tool="copy-fixture", runner_or_tool="in-process",
        deterministic=True, supported={"fixture_copy": True},
    )

    def __init__(self, mode: str = "success") -> None:
        self.mode = mode

    def compile_contract(self, settings: Any) -> dict[str, Any]:
        payload = dict(settings) if isinstance(settings, Mapping) else dict(vars(settings))
        return {
            "filter_version": "story-mock-filter/v1",
            "keyer": "mock",
            "settings": payload,
        }

    def filter_chain(self, source: str, settings: Any, crop_filter: str = "") -> str:
        del settings
        return f"{source}{crop_filter}null[person_keyed]"

    def filter_parts(self, source: str, settings: Any, crop_filter: str = "") -> list[str]:
        return [self.filter_chain(source, settings, crop_filter)]

    def fingerprint(self, settings: Any) -> str:
        serialized = repr(sorted(self.compile_contract(settings)["settings"].items())).encode("utf-8")
        return hashlib.sha256(b"story-mock-keyer/v1\0" + serialized).hexdigest()

    def render(self, request: KeyerRequest) -> KeyerResult:
        if not request.source_path.is_file() or file_sha256(request.source_path) != request.source_sha256:
            return self._failure(request, ModuleFailureCode.INVALID_INPUT, "mock invalid input")
        if self.mode == "failure":
            return self._failure(request, ModuleFailureCode.EXECUTION_FAILED, "mock execution failure")
        request.output_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(request.source_path, request.output_target)
        return KeyerResult(
            True, request.output_target, file_sha256(request.output_target), "mock", "story-mock-filter/v1",
            "0" * 64, request.source_sha256, request.preset_sha256, "story-mock-keyer/v1",
        )


__all__ = [
    "ApprovedStoryContractVisualDesignAdapter", "CodexImageGeneratorAdapter",
    "ExistingStorySemanticsAdapter", "ExistingVideoGeneratorAdapter", "IMAGE_GENERATOR_ADAPTER_VERSION",
    "COMPOSITOR_ADAPTER_VERSION", "KEYER_ADAPTER_VERSION", "LocalCompositorAdapter",
    "LocalProductPackageAdapter", "LocalReleaseLayoutAdapter", "MUSIC_PROVIDER_ADAPTER_VERSION",
    "MockImageGeneratorAdapter", "MockKeyerAdapter", "MockMusicProviderAdapter",
    "MockCompositorAdapter", "MockProductPackageAdapter", "MockReleaseLayoutAdapter", "MockStorySemanticsAdapter",
    "MockVideoGeneratorAdapter", "MockVisualDesignAdapter", "ProductionKeyerAdapter",
    "STORY_SEMANTICS_ADAPTER_VERSION", "SunoMusicProviderAdapter", "VIDEO_ADAPTER_VERSION",
    "PRODUCT_PACKAGE_ADAPTER_VERSION", "RELEASE_LAYOUT_ADAPTER_VERSION",
    "VIDEO_BATCH_INVOCATION_VERSION", "VISUAL_DESIGN_ADAPTER_VERSION",
]
