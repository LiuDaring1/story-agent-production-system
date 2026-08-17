from __future__ import annotations

import csv
import hashlib
import sys
import shutil
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
    KEYER_PORT_VERSION,
    VIDEO_GENERATOR_PORT_VERSION,
    KeyerRequest,
    KeyerResult,
    ModuleCapabilities,
    ModuleFailure,
    ModuleFailureCode,
    ModuleIdentity,
    ModuleUsageEvent,
    VideoGeneratorRequest,
    VideoGeneratorResult,
)
from video_provider_adapter import VideoProviderAdapter, resolve_row_generation_seconds


VIDEO_ADAPTER_VERSION = "story-existing-video-adapter/v1"
KEYER_ADAPTER_VERSION = "story-production-ffmpeg-keyer/v1"
VIDEO_BATCH_INVOCATION_VERSION = "story-video-batch-invocation/v1"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    "ExistingVideoGeneratorAdapter", "KEYER_ADAPTER_VERSION", "MockKeyerAdapter",
    "MockVideoGeneratorAdapter", "ProductionKeyerAdapter", "VIDEO_ADAPTER_VERSION",
    "VIDEO_BATCH_INVOCATION_VERSION",
]
