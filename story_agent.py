from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import re
import signal
import shutil
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from PIL import Image, ImageDraw
from story_module_registry import (
    ALLOWED_MODULE_EXECUTION_MODES,
    ALLOWED_MODULE_PROFILES,
    ModuleRegistry,
    build_registry_for_profile,
    module_selection_environment,
)
from story_module_ports import (
    ImageGeneratorPort,
    ImageGeneratorRequest,
    ImageGeneratorResult,
    MusicProviderPort,
    MusicProviderRequest,
    MusicProviderResult,
    ModuleFailure,
    ModuleFailureCode,
)
from story_video_synthesizer.image_video import validate_image_video_jobs
from semantic_card_motion import (
    semantic_card_motion_receipt_issues,
)
from story_contract_runtime import (
    CONTRACT_POLICY_LEGACY,
    CONTRACT_POLICY_REQUIRED,
    bind_contract_visual_style_to_trusted_default,
    build_trusted_input_chain,
    contract_consumer_context_is_current,
    contract_consumer_completion_is_current,
    contract_consumer_path,
    contract_diagnostics,
    contract_lock_is_current,
    contract_paths,
    contract_review_artifacts,
    contract_review_payload_issues,
    contract_review_retry_sections,
    contract_runtime_issues,
    defer_contract_visual_samples,
    enforce_targeted_contract_revision,
    legacy_passthrough_allowed,
    mark_contract_consumer_completed,
    normalize_contract_policy_provenance,
    write_contract_consumer_context,
    write_contract_lock,
    write_trusted_input_chain,
)
from story_contract_consumers import compile_cover_spec
from artifact_semantic_plan import (
    artifact_semantic_plan_is_current,
    load_current_artifact_semantic_plan,
    plan_binding as artifact_semantic_plan_binding,
    reconcile_semantic_mappings,
    semantic_plan_path,
    write_artifact_semantic_plan,
)
from visual_sample_gate import (
    ANATOMICAL_COHERENCE_REVIEW_RULE,
    load_current_visual_sample_plan,
    product_quality_review_issues,
    visual_sample_asset_paths,
    visual_sample_binding,
    visual_sample_generation_jobs,
    visual_sample_lock_is_current,
    visual_sample_machine_issues,
    visual_sample_paths,
    visual_sample_plan_is_current,
    visual_sample_review_payload_issues,
    write_visual_sample_lock,
    write_visual_sample_machine_qa,
    write_visual_sample_plan,
    write_visual_sample_supplemental_request,
)
from video_motion import (
    VIDEO_REVIEW_HARD_DEFECT_CODES,
    VIDEO_REVIEW_POLICY_VERSION,
    formal_source_issues,
    review_semantic_issues,
    video_review_policy_issues,
    video_receipt_issues,
    write_video_receipt,
)
from storyboard_continuity import storyboard_continuity_issues
from keying_quality import (
    keying_preset_lock_issues,
    keying_review_images,
    lock_keying_preset,
    refresh_keying_quality_from_preset,
)
from demo_quality import demo_render_manifest_issues
from cover_quality import (
    cover_review_image_paths,
    cover_review_payload_issues,
    expand_retry_files as expand_cover_retry_files,
    integrated_cover_issues,
    required_cover_issues,
)

from story_codex_tasks import (
    build_children_story_handoff,
    build_children_story_image_request,
    build_product_annotation_agent_prompt,
    build_publish_package_agent_prompt,
    build_release_assets_agent_prompt,
    build_release_preview_agent_prompt,
)
from story_agent_runtime import (
    AgentRuntimeError,
    BudgetExceeded,
    BudgetLedger,
    JobCancelled,
    JobRegistry,
    DEFAULT_RESOURCE_CAPACITIES,
    STAGE_BRANCHES,
    STAGE_RESOURCES,
    STAGE_WRITE_SETS,
    STORY_STAGE_DEPENDENCIES,
    STORY_STAGE_SEQUENCE,
    STAGE_ESTIMATES_MINUTES,
    STORY_AGENT_RELEASE_VERSION,
    assert_runnable,
    accept_current_outputs,
    deliver_best_valid_at_deadline,
    ensure_manifest_v2,
    existing_artifact_hashes,
    file_sha256,
    job_lock,
    load_control,
    mark_stage,
    manifest_context_sha256,
    migrate_code_binding,
    process_is_alive,
    render_job_report,
    request_cancel,
    record_contract_derivative,
    prepared_input_contract_errors,
    refresh_prepared_inputs,
    resume_job,
    review_bundle_is_current,
    review_passes,
    freeze_runtime,
    runtime_elapsed_seconds,
    runtime_deadline_state,
    normalized_subprocess_environment,
    start_runtime,
    submit_video_job,
    supervisor_start_lock,
    update_control,
    write_review_bundle,
)
from story_project import (
    AUDIO_EXTENSIONS,
    TEXT_EXTENSIONS,
    VIDEO_EXTENSIONS,
    ensure_project_dirs,
    apply_fixed_cover_branding,
    first_existing,
    init_project,
    load_config,
    load_main_package_reference,
    load_manifest,
    project_paths,
    refresh_project_outputs,
    save_json,
    short_slug,
    slugify,
    write_internal_agent_reports,
)
from story_qualification import build_promotion_report, record_human_signoff, record_unattended_launch, render_promotion_markdown
from story_agent_observability import (
    acknowledge_notification,
    age_seconds,
    append_ndjson,
    append_agent_event,
    emit_notification,
    event_log_path,
    load_agent_events,
    load_notifications,
    notification_sinks as build_notification_sinks,
    notification_log_path,
    read_ndjson,
    recovery_log_path,
    supervisor_state_path,
)


ROOT = Path(__file__).resolve().parent
AGENT_STATE_NAME = "story_agent_state.json"

# Review-only stages that attach visual evidence to the Codex prompt.  These
# stages use GPT's native multimodal input; bypassing user config prevents a
# user-level Claude/GLM vision bridge from being injected into the review
# process.  Keep production/imagegen/browser/Suno stages out of this set.
NATIVE_VISION_REVIEW_STAGES = frozenset(
    {
        "story_contract_review",
        "visual_sample_review",
        "story_images_review",
        "video_prompt_review",
        "video_review",
        "video_review_bulk_confirmation",
        "release_preview",
        "release_video_review",
        "publish_package_review",
        "product_annotation_review",
    }
)


def resolve_agent_runtime_python() -> str:
    """Choose an interpreter that can run the real media pipeline.

    Codex Desktop may launch this entrypoint with Homebrew Python while the
    workspace's Whisper/torch runtime lives in Miniconda.  The background
    supervisor must inherit the dependency-complete interpreter, otherwise it
    can start successfully and then fail every transcription attempt.
    """
    if importlib.util.find_spec("whisper") is not None:
        return sys.executable
    candidates = [
        os.environ.get("STORY_AGENT_PYTHON", ""),
        "/opt/miniconda3/bin/python3",
        "/opt/miniconda3/bin/python",
    ]
    for value in candidates:
        if not value:
            continue
        candidate = Path(value).expanduser()
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        return str(candidate)
    return sys.executable


def release_render_expected_bindings(
    manifest_path: Path,
    common_bindings: dict[str, Any],
) -> dict[str, Any]:
    """Return the variant-specific bindings expected from a render receipt."""
    expected = dict(common_bindings)
    if manifest_path.stem.endswith("_library"):
        expected.update(
            {
                "demo_render_manifest_sha256": "not_applicable:library_variant",
                "approved_demo_geometry_sha256": "not_applicable:library_variant",
            }
        )
    return expected


@dataclass(frozen=True)
class StageResult:
    status: str
    message: str
    handoff: Path | None = None


@dataclass(frozen=True)
class AgentContext:
    project_dir: Path
    inbox: Path | None
    story_name: str
    slug: str
    execute: bool
    update_latest_episode: bool
    codex_mode: str
    codex_model: str
    codex_sandbox: str
    codex_approval: str
    codex_path: str
    codex_timeout: int
    codex_worker_model: str = ""
    codex_reasoning_effort: str = ""
    codex_worker_reasoning_effort: str = ""
    codex_story_image_batch_size: int = 15
    scheduler: str = "linear"
    max_parallel: int = 1
    notification_sinks: str = "project"
    stop_after_stage: str = ""

    @property
    def paths(self):
        return project_paths(self.project_dir)

    def codex_route(self, stage: str) -> tuple[str, str, str]:
        """Return the fixed two-role route: commander for judgment, worker otherwise."""
        commander_stages = {
            "recovery_controller",
            "source_edit_review",
            "story_contract_review",
            "visual_sample_review",
            "story_images_review",
            "video_prompt_review",
            "video_review",
            "video_review_bulk_confirmation",
            "release_preview",
            "release_video_review",
            "product_annotation_review",
            "product_package_review",
            "publish_package_review",
        }
        if stage in commander_stages or not self.codex_worker_model:
            return "commander", self.codex_model, self.codex_reasoning_effort
        return "worker", self.codex_worker_model, self.codex_worker_reasoning_effort


@dataclass
class WorkerAttempt:
    stage: str
    attempt_id: str
    run_epoch: int
    work_dir: Path
    base_manifest: Path
    shadow_manifest: Path
    result_file: Path
    log_file: Path
    process: subprocess.Popen[str]


def canonical_video_prompt_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        rows = list(reader)
        has_continuity = bool(
            set(reader.fieldnames or []).intersection(
                {"continuity_state", "continuity_required", "continuity_forbidden", "visual_continuity_state"}
            )
        )
    result: list[dict[str, str]] = []
    for row in rows:
        raw_scene = (row.get("scene") or "").strip()
        try:
            scene = str(int(raw_scene)).zfill(2)
        except ValueError:
            scene = raw_scene
        item = {
            "scene": scene,
            "image_filename": (row.get("image_filename") or "").strip(),
            "story_text": (row.get("story_text") or "").strip(),
            "prompt": (row.get("prompt") or "").strip(),
        }
        if has_continuity:
            item.update(
                {
                    "continuity_state": (row.get("continuity_state") or row.get("visual_continuity_state") or "").strip(),
                    "continuity_required": (row.get("continuity_required") or row.get("visual_continuity_required") or "").strip(),
                    "continuity_forbidden": (row.get("continuity_forbidden") or row.get("visual_continuity_forbidden") or "").strip(),
                }
            )
        result.append(item)
    return sorted(result, key=lambda item: item["scene"])


def video_prompt_review_matches_current(jobs: Path, snapshot: Path, decisions: Path) -> bool:
    try:
        current = canonical_video_prompt_rows(jobs)
        snapshot_payload = json.loads(snapshot.read_text(encoding="utf-8"))
        original = snapshot_payload.get("rows")
        if not isinstance(original, list) or not original:
            return False
        if current == original:
            return True
        with decisions.open(encoding="utf-8-sig", newline="") as file:
            decision_rows = list(csv.DictReader(file))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    original_by_scene = {str(row.get("scene", "")): row for row in original if isinstance(row, dict)}
    approved: list[dict[str, str]] = []
    for row in decision_rows:
        raw_scene = (row.get("scene") or "").strip()
        try:
            scene = str(int(raw_scene)).zfill(2)
        except ValueError:
            scene = raw_scene
        source = original_by_scene.get(scene)
        if source is None or (row.get("review_status") or "").strip() != "approved":
            return False
        item = {
            "scene": scene,
            "image_filename": (row.get("image_filename") or source.get("image_filename") or "").strip(),
            "story_text": (row.get("story_text") or source.get("story_text") or "").strip(),
            "prompt": (row.get("prompt") or source.get("prompt") or "").strip(),
        }
        for key in ("continuity_state", "continuity_required", "continuity_forbidden"):
            if key in source:
                item[key] = (row.get(key) or source.get(key) or "").strip()
        approved.append(item)
    approved.sort(key=lambda item: item["scene"])
    return len(approved) == len(original) and current == approved


def classify_command_failure(output: str) -> str:
    """Return blocked for failures that require external user/account action."""
    text = output.lower()
    external_markers = (
        "captcha",
        "验证码",
        "未登录",
        "login required",
        "sign in",
        "payment required",
        "支付失败",
        "余额不足",
        "insufficient credits",
        "permission denied",
        "权限不足",
        "usage limit",
        "用量限制",
        "api key",
        "missing credential",
        "账号风控",
        "no browser is available",
        "browser discovery returned an empty",
        "没有可用浏览器",
    )
    return "blocked" if any(marker in text for marker in external_markers) else "failed"


class StoryAgent:
    def __init__(
        self,
        context: AgentContext,
        *,
        read_only: bool = False,
        module_registry: ModuleRegistry | None = None,
    ) -> None:
        self.context = context
        self.read_only = read_only
        self._module_registry = module_registry
        if not read_only:
            ensure_project_dirs(context.paths)
        worker_dir = os.environ.get("STORY_AGENT_WORKER_DIR", "").strip()
        self.state_path = Path(worker_dir) / AGENT_STATE_NAME if worker_dir else context.paths.status / AGENT_STATE_NAME
        self.state: dict[str, Any] = self._load_state()
        self._stage_cost_baselines: dict[str, float] = {}

    def _modules(self) -> ModuleRegistry:
        if self._module_registry is None:
            self._module_registry = build_registry_for_profile("", load_config(), ROOT)
        return self._module_registry

    def _module_subprocess_env(self) -> dict[str, str]:
        profile = self._modules().selection_profile()
        execution_mode = self._modules().selection_execution_mode()
        return module_selection_environment(profile, execution_mode)

    def run(self, max_steps: int) -> int:
        if self.context.scheduler == "dag" and self.context.execute:
            return self._run_dag(max_steps=max_steps)
        return self._run_linear(max_steps=max_steps)

    def _postcondition_checked_result(self, stage: str, result: StageResult) -> StageResult:
        if result.status != "done" or not self.context.execute:
            return result
        manifest = self._manifest()
        predicate = next((done for name, done, _action in self._stage_checks() if name == stage), None)
        if predicate is None:
            return StageResult("blocked", f"producer_postcondition_failed: 未找到阶段完成条件 {stage}", result.handoff)
        try:
            passed = bool(predicate(manifest))
        except Exception as exc:
            return StageResult(
                "blocked",
                f"producer_postcondition_failed: {stage} 返回 done，但后置条件检查异常：{type(exc).__name__}: {exc}",
                result.handoff,
            )
        if not passed:
            return StageResult(
                "blocked",
                f"producer_postcondition_failed: {stage} 返回 done，但当前产物/哈希后置条件仍为 false；"
                "已停止同输入热循环，只允许明确的局部修复。",
                result.handoff,
            )
        return result

    def _deadline_exit_if_needed(self, manifest: dict[str, Any]) -> int | None:
        state = runtime_deadline_state(manifest.get("agent", {}))
        if not state["reached"]:
            return None
        try:
            _updated, receipt = deliver_best_valid_at_deadline(
                self.context.project_dir,
                notes="达到项目运行时限；停止新增审美返工并冻结当前最佳哈希有效版本。",
            )
        except AgentRuntimeError as exc:
            freeze_runtime(manifest["agent"])
            manifest["agent"]["status"] = "blocked"
            manifest["agent"]["blocked_reason"] = f"deadline_reached_without_valid_delivery: {exc}"
            from story_project import write_manifest
            write_manifest(self.context.paths, manifest)
            return 2
        print(f"DEADLINE: 已冻结最佳有效版本：{receipt}")
        return 0

    def _pause_if_stop_stage_reached(self, manifest: dict[str, Any]) -> int | None:
        """Pause a bounded canary immediately after one verified stage gate."""

        target = self.context.stop_after_stage.strip()
        if not target or not self.context.execute:
            return None
        predicate = next((done for name, done, _action in self._stage_checks() if name == target), None)
        if predicate is None:
            raise AgentRuntimeError(f"未知 stop-after-stage：{target}")
        try:
            reached = bool(predicate(manifest))
        except Exception:
            reached = False
        if not reached:
            return None
        from story_project import write_manifest

        freeze_runtime(manifest["agent"])
        manifest["agent"]["status"] = "pending"
        manifest["agent"]["blocked_reason"] = ""
        manifest["agent"]["pause"] = {
            "reason": "stop_after_stage",
            "stage": target,
            "paused_at": now(),
        }
        write_manifest(self.context.paths, manifest)
        self._record_event(
            "requested_stage_pause",
            {"stage": target, "status": "pending", "message": "已在验证阶段门后暂停。"},
        )
        render_job_report(self.context.project_dir)
        print(f"PAUSED: 已通过 {target} 门禁，未启动下游阶段。")
        return 0

    def _bounded_stage_names(self) -> set[str]:
        """Return the target stage and its transitive prerequisites.

        A bounded DAG canary must not start an independent branch merely
        because that branch happens to be ready before the requested gate.
        """

        target = self.context.stop_after_stage.strip()
        if not target:
            return set(STORY_STAGE_SEQUENCE)
        if target not in STORY_STAGE_DEPENDENCIES:
            raise AgentRuntimeError(f"未知 stop-after-stage：{target}")
        allowed: set[str] = set()

        def include(stage: str) -> None:
            if stage in allowed:
                return
            allowed.add(stage)
            for dependency in STORY_STAGE_DEPENDENCIES[stage]:
                include(dependency)

        include(target)
        return allowed

    def _run_linear(self, max_steps: int) -> int:
        with job_lock(self.context.project_dir):
            manifest = self._manifest()
            ensure_manifest_v2(manifest)
            assert_runnable(manifest, self.context.project_dir)
            from story_project import write_manifest

            if self.context.execute:
                manifest["agent"]["status"] = "running"
                manifest["agent"].pop("pause", None)
                start_runtime(manifest["agent"])
                write_manifest(self.context.paths, manifest)
            self._record_event("agent_start", {"execute": self.context.execute, "codex_mode": self.context.codex_mode})
            for _ in range(max_steps):
                manifest = self._manifest()
                assert_runnable(manifest, self.context.project_dir)
                deadline_exit = self._deadline_exit_if_needed(manifest)
                if deadline_exit is not None:
                    return deadline_exit
                requested_pause = self._pause_if_stop_stage_reached(manifest)
                if requested_pause is not None:
                    return requested_pause
                stage_name, action = self._next_stage(manifest)
                if self.context.execute:
                    self._reconcile_completed_stage_records(manifest, stage_name)
                if stage_name == "done":
                    if self.context.execute:
                        freeze_runtime(manifest["agent"])
                        manifest["agent"]["status"] = "completed"
                        manifest["agent"]["blocked_reason"] = ""
                        manifest["agent"]["finished_at"] = now()
                        write_manifest(self.context.paths, manifest)
                        render_job_report(self.context.project_dir)
                    self._record_event("done", {"message": "故事生产 Agent 已完成全部阶段。"})
                    print("DONE: 故事生产 Agent 已完成全部阶段。")
                    return 0
                print(f"NEXT: {stage_name}")
                if self.context.execute:
                    self._stage_cost_baselines[stage_name] = float(manifest.get("agent", {}).get("budget", {}).get("spent", 0.0))
                    mark_stage(
                        manifest,
                        stage_name,
                        "running",
                        message="阶段开始",
                        input_hashes={"manifest_context": manifest_context_sha256(manifest)},
                        provider=self._provider_for_stage(stage_name),
                    )
                    write_manifest(self.context.paths, manifest)
                    self._record_event(
                        "stage_started",
                        {
                            "stage": stage_name,
                            "status": "running",
                            "message": "线性调度阶段开始",
                        },
                    )
                result = self._postcondition_checked_result(stage_name, action(manifest))
                if result.status == "failed":
                    critical_stage = stage_name in {
                        "source_edit",
                        "codex_story_images",
                        "generate_videos",
                        "video_review",
                        "suno_generate",
                        "assemble_final",
                        "package_release",
                        "product_package",
                    }
                    if self._can_retry_stage(stage_name, critical=critical_stage):
                        result = StageResult("retrying", f"{result.message}；将在新尝试中自动重跑。", result.handoff)
                self._record_stage(stage_name, result)
                print(f"{result.status.upper()}: {result.message}")
                if result.handoff is not None:
                    print(f"HANDOFF: {result.handoff}")
                if result.status in {"blocked", "failed", "cancelled"}:
                    render_job_report(self.context.project_dir)
                    return 2 if result.status == "blocked" else (3 if result.status == "cancelled" else 1)
            if self.context.execute:
                manifest = self._manifest()
                freeze_runtime(manifest["agent"])
                manifest["agent"]["status"] = "pending"
                write_manifest(self.context.paths, manifest)
        print(f"PAUSED: 已达到本轮最大步数 {max_steps}，可再次运行继续。")
        render_job_report(self.context.project_dir)
        return 0

    def _run_dag(self, max_steps: int) -> int:
        with job_lock(self.context.project_dir):
            manifest = self._manifest()
            ensure_manifest_v2(manifest)
            assert_runnable(manifest, self.context.project_dir)
            if self._recover_interrupted_dag_workers(manifest):
                from story_project import write_manifest

                write_manifest(self.context.paths, manifest)
            control = update_control(self.context.project_dir, cancel_requested=False, increment_epoch=True)
            run_epoch = int(control["run_epoch"])
            run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
            agent = manifest["agent"]
            agent["status"] = "running"
            agent["cancel_requested"] = False
            agent["control_epoch"] = run_epoch
            agent["scheduler"].update(
                {
                    "schema_version": 1,
                    "mode": "dag",
                    "max_parallel": max(1, self.context.max_parallel),
                    "run_epoch": run_epoch,
                    "run_id": run_id,
                    "running": {},
                }
            )
            start_runtime(agent)
            from story_project import write_manifest

            write_manifest(self.context.paths, manifest)
            self._record_event("dag_agent_start", {"run_id": run_id, "run_epoch": run_epoch, "max_parallel": self.context.max_parallel})
            blocked_this_run: set[str] = set()
            launched_attempts = 0
            while launched_attempts < max_steps:
                manifest = self._manifest()
                assert_runnable(manifest, self.context.project_dir)
                deadline_exit = self._deadline_exit_if_needed(manifest)
                if deadline_exit is not None:
                    return deadline_exit
                self._reconcile_all_completed_stage_records(manifest)
                manifest = load_manifest(self.context.paths) or manifest
                requested_pause = self._pause_if_stop_stage_reached(manifest)
                if requested_pause is not None:
                    return requested_pause
                completed = self._completed_stage_names(manifest)
                if self._recover_stale_stage_blockers(manifest, completed, blocked_this_run):
                    write_manifest(self.context.paths, manifest)
                if len(completed) == len(STORY_STAGE_SEQUENCE):
                    freeze_runtime(manifest["agent"])
                    manifest["agent"]["status"] = "completed"
                    manifest["agent"]["blocked_reason"] = ""
                    manifest["agent"]["branch_blockers"] = {}
                    manifest["agent"]["finished_at"] = now()
                    manifest["agent"]["scheduler"]["running"] = {}
                    write_manifest(self.context.paths, manifest)
                    render_job_report(self.context.project_dir)
                    self._record_event("done", {"message": "DAG 故事生产 Agent 已完成全部阶段。"})
                    print("DONE: DAG 故事生产 Agent 已完成全部阶段。")
                    return 0
                ready = self._ready_dag_stages(manifest, completed, blocked_this_run)
                selected = self._select_parallel_batch(ready, max_count=min(self.context.max_parallel, max_steps - launched_attempts))
                if not selected:
                    freeze_runtime(manifest["agent"])
                    blockers = manifest["agent"].get("branch_blockers", {})
                    if blocked_this_run or blockers:
                        manifest["agent"]["status"] = "blocked"
                        messages = [str(item.get("message", "")) for item in blockers.values() if isinstance(item, dict)]
                        manifest["agent"]["blocked_reason"] = "；".join(dict.fromkeys(item for item in messages if item)) or "一个或多个并行分支阻塞"
                        exit_code = 2
                    else:
                        manifest["agent"]["status"] = "failed"
                        manifest["agent"]["blocked_reason"] = "DAG 没有可运行节点且仍有未完成阶段；请检查依赖或产物哈希。"
                        exit_code = 1
                    manifest["agent"]["scheduler"]["running"] = {}
                    write_manifest(self.context.paths, manifest)
                    render_job_report(self.context.project_dir)
                    return exit_code
                print("READY: " + ", ".join(selected))
                attempts = self._launch_worker_batch(selected, manifest, run_id=run_id, run_epoch=run_epoch)
                launched_attempts += len(attempts)
                results = self._wait_worker_batch(attempts, run_epoch=run_epoch)
                for attempt, result in results:
                    if result.status == "failed" and self._can_retry_stage(attempt.stage, critical=self._critical_stage(attempt.stage)):
                        result = StageResult("retrying", f"{result.message}；将在新 attempt 中自动重跑。", result.handoff)
                    merge_error = self._merge_worker_shadow(attempt)
                    if merge_error:
                        result = StageResult("failed", f"worker manifest 合并冲突：{merge_error}", attempt.result_file)
                    result = self._postcondition_checked_result(attempt.stage, result)
                    self._record_stage(attempt.stage, result, terminal=False)
                    current = load_manifest(self.context.paths) or self._manifest()
                    current["agent"]["scheduler"].setdefault("running", {}).pop(attempt.stage, None)
                    current["agent"]["status"] = "running"
                    current["agent"]["blocked_reason"] = ""
                    start_runtime(current["agent"])
                    write_manifest(self.context.paths, current)
                    print(f"{attempt.stage}: {result.status.upper()} {result.message}")
                    if result.status in {"blocked", "failed", "cancelled"}:
                        blocked_this_run.add(attempt.stage)
                if load_control(self.context.project_dir).get("cancel_requested"):
                    raise JobCancelled("并行任务已按取消请求停止。")
            manifest = load_manifest(self.context.paths) or self._manifest()
            freeze_runtime(manifest["agent"])
            manifest["agent"]["status"] = "pending"
            manifest["agent"]["scheduler"]["running"] = {}
            write_manifest(self.context.paths, manifest)
        print(f"PAUSED: DAG 已启动 {launched_attempts} 个阶段 attempt，达到本轮上限 {max_steps}。")
        render_job_report(self.context.project_dir)
        return 0

    def _completed_stage_names(self, manifest: dict[str, Any]) -> set[str]:
        completed: set[str] = set()
        for name, done, _action in self._stage_checks():
            try:
                if done(manifest) and all(
                    dependency in completed for dependency in STORY_STAGE_DEPENDENCIES[name]
                ):
                    completed.add(name)
            except Exception:
                continue
        return completed

    def _ready_dag_stages(self, manifest: dict[str, Any], completed: set[str], blocked: set[str]) -> list[str]:
        stages = manifest.get("agent", {}).get("stages", {})
        ready: list[str] = []
        allowed = self._bounded_stage_names()
        for name in STORY_STAGE_SEQUENCE:
            if name not in allowed:
                continue
            if name in completed or name in blocked:
                continue
            record = stages.get(name, {}) if isinstance(stages, dict) else {}
            if isinstance(record, dict) and record.get("status") in {"running", "blocked", "failed", "cancelled"}:
                continue
            if all(dependency in completed for dependency in STORY_STAGE_DEPENDENCIES[name]):
                ready.append(name)
        return ready

    def _recover_stale_stage_blockers(
        self,
        manifest: dict[str, Any],
        completed: set[str],
        blocked_this_run: set[str],
    ) -> bool:
        """Requeue blockers whose recorded prerequisite failure is now obsolete.

        Recovery is keyed by a hash of the prerequisite artifacts. A given
        prerequisite state is retried at most once, preventing infinite loops
        while allowing another DAG branch to repair the inputs.
        """

        agent = manifest.get("agent", {})
        stages = agent.get("stages", {}) if isinstance(agent, dict) else {}
        blockers = agent.get("branch_blockers", {}) if isinstance(agent, dict) else {}
        if not isinstance(stages, dict) or not isinstance(blockers, dict):
            return False

        prepare_record = stages.get("prepare_jobs", {})
        prepare_message = str(prepare_record.get("message", "")) if isinstance(prepare_record, dict) else ""
        prepare_paths = contract_paths(self.context.project_dir)
        recoveries: dict[str, tuple[bool, tuple[Path, ...], str]] = {
            "prepare_jobs": (
                (
                    not self._legacy_contract_policy(manifest)
                    and "合同" in prepare_message
                    and self._has_story_contract_review(manifest)
                    and self._has_story_images_review(manifest)
                ),
                (prepare_paths["contract"], prepare_paths["lock"], prepare_paths["review"]),
                "已审核合同与图片审核现已有效，自动清除旧 prepare_jobs 阻塞并重新排队。",
            ),
            "assemble_music": (
                "suno_generate" in completed and self._has_suno_audio(manifest),
                (self._music_plan(), *self._expected_suno_audio_targets(manifest)),
                "音乐计划所需片段现已齐全，自动清除旧 assemble_music 失败并重新排队。",
            ),
        }

        changed = False
        for stage, (prerequisites_ready, paths, message) in recoveries.items():
            record = stages.get(stage, {})
            if not isinstance(record, dict) or record.get("status") not in {"blocked", "failed", "cancelled"}:
                continue
            if not prerequisites_ready or not all(path.is_file() for path in paths):
                continue
            fingerprint = hashlib.sha256(
                json.dumps(
                    {str(path): file_sha256(path) for path in paths},
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            if record.get("auto_recovery_fingerprint") == fingerprint:
                continue
            record["status"] = "pending"
            record["message"] = message
            record["finished_at"] = ""
            record["retry_reason"] = message
            record["auto_recovery_fingerprint"] = fingerprint
            blockers.pop(stage, None)
            blocked_this_run.discard(stage)
            agent["events"] = [
                *agent.get("events", []),
                {"time": now(), "event": "stale_blocker_recovered", "stage": stage, "message": message},
            ][-500:]
            changed = True
        return changed

    def _recover_interrupted_dag_workers(self, manifest: dict[str, Any]) -> bool:
        agent = manifest.get("agent", {}) if isinstance(manifest.get("agent"), dict) else {}
        scheduler = agent.get("scheduler", {}) if isinstance(agent.get("scheduler"), dict) else {}
        running = scheduler.get("running", {}) if isinstance(scheduler.get("running"), dict) else {}
        changed = False
        for stage, item in list(running.items()):
            pid = int(item.get("pid") or 0) if isinstance(item, dict) else 0
            if process_is_alive(pid):
                raise AgentRuntimeError(
                    f"orphan worker still alive: stage={stage}, pid={pid}; supervisor must monitor it"
                )
            record = agent.get("stages", {}).get(stage, {})
            if isinstance(record, dict) and record.get("status") == "running":
                record["status"] = "pending"
                record["finished_at"] = ""
                record["retry_reason"] = "supervisor restart recovered an interrupted worker"
                record["infrastructure_attempts"] = int(
                    record.get("infrastructure_attempts") or 0
                ) + 1
            running.pop(stage, None)
            agent.get("branch_blockers", {}).pop(stage, None)
            agent["events"] = [
                *agent.get("events", []),
                {
                    "time": now(),
                    "event": "interrupted_worker_requeued",
                    "stage": stage,
                    "pid": pid,
                },
            ][-500:]
            append_agent_event(
                self.context.project_dir,
                event_type="interrupted_worker_requeued",
                status="pending",
                summary=f"dead worker PID {pid} was requeued after supervisor restart",
                job_id=str(agent.get("job_id") or ""),
                run_id=str(scheduler.get("run_id") or ""),
                stage=stage,
                attempt_id=str(item.get("attempt_id") or "") if isinstance(item, dict) else "",
                blocker_category="interrupted_worker",
                recovery_decision="repair_and_retry",
            )
            changed = True
        return changed

    def _select_parallel_batch(self, ready: list[str], *, max_count: int) -> list[str]:
        selected: list[str] = []
        used_resources: dict[str, int] = {}
        used_writes: list[str] = []

        def overlaps(left: str, right: str) -> bool:
            return left == right or left.startswith(right + "/") or right.startswith(left + "/")

        for stage in ready:
            if len(selected) >= max(1, max_count):
                break
            writes = STAGE_WRITE_SETS.get(stage, ())
            if any(overlaps(left, right) for left in writes for right in used_writes):
                continue
            resources = STAGE_RESOURCES.get(stage, ())
            if any(used_resources.get(resource, 0) + 1 > DEFAULT_RESOURCE_CAPACITIES.get(resource, 1) for resource in resources):
                continue
            selected.append(stage)
            used_writes.extend(writes)
            for resource in resources:
                used_resources[resource] = used_resources.get(resource, 0) + 1
        return selected

    def _launch_worker_batch(
        self,
        stages: list[str],
        manifest: dict[str, Any],
        *,
        run_id: str,
        run_epoch: int,
    ) -> list[WorkerAttempt]:
        from story_project import write_manifest

        for stage in stages:
            self._stage_cost_baselines[stage] = float(manifest.get("agent", {}).get("budget", {}).get("spent", 0.0))
            mark_stage(
                manifest,
                stage,
                "running",
                message="DAG worker 已启动",
                input_hashes={"manifest_context": manifest_context_sha256(manifest)},
                provider=self._provider_for_stage(stage),
            )
        write_manifest(self.context.paths, manifest)
        canonical_snapshot = load_manifest(self.context.paths) or manifest
        scheduler_root = self.context.paths.status / "scheduler" / "runs" / run_id
        attempts: list[WorkerAttempt] = []
        for stage in stages:
            attempt_id = f"{stage}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
            work_dir = scheduler_root / attempt_id
            work_dir.mkdir(parents=True, exist_ok=True)
            base_manifest = work_dir / "base_manifest.json"
            shadow_manifest = work_dir / "shadow_manifest.json"
            result_file = work_dir / "result.json"
            log_file = work_dir / "worker.log"
            save_json(base_manifest, canonical_snapshot)
            save_json(shadow_manifest, canonical_snapshot)
            command = [
                resolve_agent_runtime_python(),
                str(Path(__file__).resolve()),
                "run-stage",
                "--project-dir",
                str(self.context.project_dir),
                "--story-name",
                self.context.story_name,
                "--slug",
                self.context.slug,
                "--stage",
                stage,
                "--result-file",
                str(result_file),
                "--run-epoch",
                str(run_epoch),
                "--attempt-id",
                attempt_id,
                "--codex-mode",
                self.context.codex_mode,
                "--codex-sandbox",
                self.context.codex_sandbox,
                "--codex-approval",
                self.context.codex_approval,
                "--codex-path",
                self.context.codex_path,
                "--codex-timeout",
                str(self.context.codex_timeout),
                "--codex-story-image-batch-size",
                str(self.context.codex_story_image_batch_size),
                "--module-profile",
                self._modules().selection_profile(),
                "--module-execution-mode",
                self._modules().selection_execution_mode(),
                "--notification-sinks",
                self.context.notification_sinks,
            ]
            if self.context.codex_model:
                command.extend(["--codex-model", self.context.codex_model])
            if self.context.codex_worker_model:
                command.extend(["--codex-worker-model", self.context.codex_worker_model])
            if self.context.codex_reasoning_effort:
                command.extend(["--codex-reasoning-effort", self.context.codex_reasoning_effort])
            if self.context.codex_worker_reasoning_effort:
                command.extend(["--codex-worker-reasoning-effort", self.context.codex_worker_reasoning_effort])
            env = normalized_subprocess_environment()
            env["STORY_AGENT_MANIFEST_OVERRIDE"] = str(shadow_manifest)
            env["STORY_AGENT_PROJECT_ROOT"] = str(self.context.project_dir.expanduser().resolve())
            env["STORY_AGENT_WORKER_DIR"] = str(work_dir)
            env["STORY_AGENT_RUN_EPOCH"] = str(run_epoch)
            env["STORY_AGENT_ATTEMPT_ID"] = attempt_id
            env.update(self._module_subprocess_env())
            with log_file.open("w", encoding="utf-8") as worker_log:
                process = subprocess.Popen(
                    command,
                    cwd=str(ROOT),
                    env=env,
                    text=True,
                    stdout=worker_log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            attempts.append(
                WorkerAttempt(stage, attempt_id, run_epoch, work_dir, base_manifest, shadow_manifest, result_file, log_file, process)
            )
            self._record_event(
                "stage_started",
                {
                    "stage": stage,
                    "status": "running",
                    "attempt_id": attempt_id,
                    "message": f"DAG worker 已启动，PID {process.pid}",
                },
            )
            manifest["agent"]["scheduler"].setdefault("running", {})[stage] = {
                "attempt_id": attempt_id,
                "pid": process.pid,
                "branch": STAGE_BRANCHES.get(stage, "other"),
                "started_at": now(),
                "run_epoch": run_epoch,
            }
        write_manifest(self.context.paths, manifest)
        return attempts

    def _wait_worker_batch(self, attempts: list[WorkerAttempt], *, run_epoch: int) -> list[tuple[WorkerAttempt, StageResult]]:
        pending = {attempt.attempt_id: attempt for attempt in attempts}
        cancelled = False
        while pending:
            control = load_control(self.context.project_dir)
            if control.get("cancel_requested") or int(control.get("run_epoch", 0)) != run_epoch:
                cancelled = True
                for attempt in pending.values():
                    try:
                        os.killpg(attempt.process.pid, signal.SIGTERM)
                    except OSError:
                        pass
            for attempt_id, attempt in list(pending.items()):
                if attempt.process.poll() is not None:
                    pending.pop(attempt_id)
            manifest = load_manifest(self.context.paths)
            if manifest is not None:
                manifest["agent"]["heartbeat_at"] = now()
                from story_project import write_manifest

                write_manifest(self.context.paths, manifest)
            if pending:
                time.sleep(1)
        results: list[tuple[WorkerAttempt, StageResult]] = []
        for attempt in attempts:
            if cancelled:
                results.append((attempt, StageResult("cancelled", "worker 已被 run epoch/cancel 控制面终止", attempt.log_file)))
                continue
            try:
                payload = json.loads(attempt.result_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            if (
                attempt.process.returncode != 0
                or payload.get("attempt_id") != attempt.attempt_id
                or int(payload.get("run_epoch", -1)) != run_epoch
            ):
                results.append((attempt, StageResult("failed", f"stage worker 异常退出或结果信封无效：{attempt.log_file}", attempt.log_file)))
                continue
            handoff_text = str(payload.get("handoff") or "")
            results.append(
                (
                    attempt,
                    StageResult(str(payload.get("status") or "failed"), str(payload.get("message") or "worker 未提供说明"), Path(handoff_text) if handoff_text else None),
                )
            )
        return results

    def _merge_worker_shadow(self, attempt: WorkerAttempt) -> str:
        try:
            base = json.loads(attempt.base_manifest.read_text(encoding="utf-8"))
            shadow = json.loads(attempt.shadow_manifest.read_text(encoding="utf-8"))
            current = load_manifest(self.context.paths) or {}
        except (OSError, json.JSONDecodeError) as exc:
            return str(exc)
        conflicts: list[str] = []

        def merge(base_value: Any, shadow_value: Any, current_value: Any, path: str) -> Any:
            if path in {"updated_at", "agent.heartbeat_at"}:
                return current_value
            if shadow_value == base_value:
                return current_value
            if isinstance(base_value, dict) and isinstance(shadow_value, dict) and isinstance(current_value, dict):
                result = dict(current_value)
                for key in set(base_value) | set(shadow_value):
                    child = f"{path}.{key}" if path else key
                    if key not in shadow_value:
                        if key in base_value and current_value.get(key) == base_value.get(key):
                            result.pop(key, None)
                        continue
                    result[key] = merge(base_value.get(key), shadow_value.get(key), current_value.get(key), child)
                return result
            if current_value == base_value or current_value == shadow_value:
                return shadow_value
            conflicts.append(path or "<root>")
            return current_value

        merged = merge(base, shadow, current, "")
        if conflicts:
            return "、".join(sorted(set(conflicts))[:12])
        from story_project import write_manifest

        write_manifest(self.context.paths, ensure_manifest_v2(merged))
        return ""

    def _reconcile_all_completed_stage_records(self, manifest: dict[str, Any]) -> None:
        changed = False
        for name, done, _action in self._stage_checks():
            try:
                complete = done(manifest)
            except Exception:
                complete = False
            record = manifest.get("agent", {}).get("stages", {}).get(name, {})
            if complete and record.get("status") != "passed":
                mark_stage(manifest, name, "passed", message="当前产物与哈希满足 DAG 节点完成条件。")
                changed = True
        if changed:
            from story_project import write_manifest

            write_manifest(self.context.paths, manifest)

    @staticmethod
    def _critical_stage(stage: str) -> bool:
        return stage in {
            "source_edit",
            "codex_story_images",
            "generate_videos",
            "video_review",
            "suno_generate",
            "assemble_final",
            "package_release",
            "product_package",
        }

    def reconcile_state(
        self,
        *,
        archive_stale_story_images: bool = False,
        legacy_story_image_staging: list[Path] | None = None,
    ) -> dict[str, Any]:
        """Normalize recorded stage state against current deterministic gates.

        This maintenance operation is deliberately provider-free. It requires
        an idle supervisor/job, preserves attempts and prior evidence, archives
        stale images instead of deleting them, and records a JSON decision in
        ``99_项目状态``.
        """

        if self.read_only:
            raise AgentRuntimeError("只读 StoryAgent 不能整理持久状态")
        for state_file in (
            supervisor_state_path(self.context.project_dir),
            self.context.paths.status / "story_agent_supervisor.json",
        ):
            payload = self._read_json_object(state_file)
            try:
                pid = int(payload.get("pid") or 0)
            except (TypeError, ValueError):
                pid = 0
            if process_is_alive(pid):
                raise AgentRuntimeError(f"supervisor 仍在运行，拒绝并发整理状态：pid={pid}")

        normalized_legacy_roots: list[Path] = []
        for raw in legacy_story_image_staging or []:
            path = raw.expanduser().resolve()
            if path.exists() and not path.is_dir():
                raise AgentRuntimeError(f"旧图片 staging 不是目录：{path}")
            if path.name != "images" or path.parent.name != "codex_story_images":
                raise AgentRuntimeError(
                    f"旧图片 staging 必须是 codex_story_images/images 目录：{path}"
                )
            if path not in normalized_legacy_roots:
                normalized_legacy_roots.append(path)

        with job_lock(self.context.project_dir):
            manifest_path = self.context.paths.manifest
            manifest = load_manifest(self.context.paths)
            if manifest is None:
                raise AgentRuntimeError(f"项目 manifest 缺失或损坏：{manifest_path}")
            before_manifest_sha256 = file_sha256(manifest_path)
            manifest = ensure_manifest_v2(manifest)
            agent = manifest["agent"]
            before_status = str(agent.get("status") or "pending")
            before_stage_statuses = {
                name: str(agent.get("stages", {}).get(name, {}).get("status") or "pending")
                for name in STORY_STAGE_SEQUENCE
            }

            storyboard = self._storyboard_path(manifest)
            image_dir = self._image_dir()
            staging_images = self._codex_stage_dir("codex_story_images") / "images"
            lineage_before = self._story_image_lineage_status(
                manifest,
                storyboard=storyboard,
                image_dir=image_dir,
                staging_images=staging_images,
            )
            archived_story_images = ""
            if archive_stale_story_images and not lineage_before["stage_gate_complete"]:
                has_legacy_files = any(
                    self._expected_named_story_image_count(root, manifest, storyboard) > 0
                    for root in normalized_legacy_roots
                )
                if int(lineage_before["physical_expected_named_count"]) > 0 or has_legacy_files:
                    archived_story_images = str(
                        self._invalidate_story_image_derivatives(
                            staging_images,
                            additional_staging_images=normalized_legacy_roots,
                            reason="reconcile_stale_or_unbound_story_images",
                        )
                    )

            completed: set[str] = set()
            predicate_results: dict[str, dict[str, Any]] = {}
            checks = self._stage_checks()
            for name, done, _action in checks:
                error = ""
                try:
                    own_gate = bool(done(manifest))
                except Exception as exc:
                    own_gate = False
                    error = f"{type(exc).__name__}: {exc}"
                dependencies_current = all(
                    dependency in completed for dependency in STORY_STAGE_DEPENDENCIES[name]
                )
                effective_complete = own_gate and dependencies_current
                if effective_complete:
                    completed.add(name)
                predicate_results[name] = {
                    "own_gate": own_gate,
                    "dependencies_current": dependencies_current,
                    "effective_complete": effective_complete,
                    "error": error,
                }

            changed_stages: list[dict[str, Any]] = []
            reconciled_at = now()
            stages = agent.get("stages", {})
            for name in STORY_STAGE_SEQUENCE:
                record = stages[name]
                previous = str(record.get("status") or "pending")
                desired = "passed" if name in completed else "pending"
                if previous == desired:
                    continue
                history = record.setdefault("reconciliation_history", [])
                if not isinstance(history, list):
                    history = []
                    record["reconciliation_history"] = history
                history.append(
                    {
                        "time": reconciled_at,
                        "from": previous,
                        "to": desired,
                        "message": str(record.get("message") or ""),
                        "retry_reason": str(record.get("retry_reason") or ""),
                        "input_hashes": record.get("input_hashes", {}),
                        "output_hashes": record.get("output_hashes", {}),
                    }
                )
                record["reconciliation_history"] = history[-20:]
                record["status"] = desired
                record["started_at"] = ""
                if desired == "passed":
                    record["finished_at"] = reconciled_at
                    record["message"] = "状态整理：当前产物、哈希与依赖门禁重新核验通过。"
                    record["retry_reason"] = ""
                else:
                    record["finished_at"] = ""
                    record["retry_reason"] = "状态整理：旧记录不再满足当前产物/哈希/依赖门禁，已安全回到 pending。"
                changed_stages.append(
                    {
                        "stage": name,
                        "from": previous,
                        "to": desired,
                        **predicate_results[name],
                    }
                )

            control = update_control(
                self.context.project_dir,
                cancel_requested=bool(agent.get("cancel_requested")),
                increment_epoch=True,
            )
            freeze_runtime(agent)
            agent["control_epoch"] = int(control["run_epoch"])
            scheduler = agent.get("scheduler", {})
            scheduler["run_epoch"] = int(control["run_epoch"])
            scheduler["run_id"] = ""
            scheduler["running"] = {}
            agent["branch_blockers"] = {}
            agent["blocked_reason"] = ""
            agent["last_checkpoint"] = next(
                (name for name in reversed(STORY_STAGE_SEQUENCE) if name in completed),
                "",
            )
            all_completed = len(completed) == len(STORY_STAGE_SEQUENCE)
            if agent.get("cancel_requested"):
                agent["status"] = "cancelled"
            elif all_completed:
                agent["status"] = "completed"
                agent["finished_at"] = reconciled_at
            else:
                agent["status"] = "pending"
                agent["finished_at"] = ""
                manifest.pop("completed_at", None)
            agent["heartbeat_at"] = reconciled_at
            agent["events"] = [
                *agent.get("events", []),
                {
                    "time": reconciled_at,
                    "event": "state_reconciled",
                    "changed_stage_count": len(changed_stages),
                    "archived_story_images": archived_story_images,
                },
            ][-500:]

            from story_project import write_manifest

            write_manifest(self.context.paths, manifest)
            after_manifest_sha256 = file_sha256(manifest_path)
            report_dir = (
                self.context.paths.status
                / "reconciliation"
                / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
            )
            report = {
                "kind": "story_agent_state_reconciliation_v1",
                "created_at": reconciled_at,
                "project_dir": str(self.context.project_dir.resolve()),
                "job_id": str(agent.get("job_id") or ""),
                "provider_calls_made": False,
                "before_manifest_sha256": before_manifest_sha256,
                "after_manifest_sha256": after_manifest_sha256,
                "before_agent_status": before_status,
                "after_agent_status": str(agent.get("status") or ""),
                "before_stage_statuses": before_stage_statuses,
                "completed_stages": [name for name in STORY_STAGE_SEQUENCE if name in completed],
                "ready_stages": self._ready_dag_stages(manifest, completed, set()),
                "changed_stages": changed_stages,
                "predicate_results": predicate_results,
                "story_image_lineage_before": lineage_before,
                "archived_story_images": archived_story_images,
                "control_run_epoch": int(control["run_epoch"]),
            }
            report_path = report_dir / "state_reconciliation.json"
            save_json(report_path, report)
            report["report"] = str(report_path)
            append_agent_event(
                self.context.project_dir,
                event_type="state_reconciled",
                status=str(agent.get("status") or ""),
                summary=(
                    f"状态整理完成：{len(changed_stages)} 个阶段记录更新；"
                    f"旧图片归档={archived_story_images or '无'}"
                ),
                job_id=str(agent.get("job_id") or ""),
                output_evidence_hashes={str(report_path): file_sha256(report_path)},
                metadata={"provider_calls_made": False, "control_run_epoch": int(control["run_epoch"])},
            )
            emit_notification(
                self.context.project_dir,
                category="state_reconciled",
                severity="info",
                message=f"状态已按当前产物与哈希重新整理；更新 {len(changed_stages)} 个阶段。",
                dedupe_key=f"{agent.get('job_id')}:state-reconciled:{after_manifest_sha256}",
                recovery_mode="none",
                job_id=str(agent.get("job_id") or ""),
                sinks=[],
            )
            return report

    def status_payload(self) -> dict[str, Any]:
        manifest = self._manifest()
        stage_name, _ = self._next_stage(manifest)
        agent_data = manifest.get("agent", {})
        scheduler_data = agent_data.get("scheduler", {}) if isinstance(agent_data.get("scheduler"), dict) else {}
        scheduler_mode = str(scheduler_data.get("mode") or "linear")
        completed = self._completed_stage_names(manifest) if scheduler_mode == "dag" else {
            name for name in STORY_STAGE_SEQUENCE[: STORY_STAGE_SEQUENCE.index(stage_name)]
        } if stage_name != "done" else set(STORY_STAGE_SEQUENCE)
        remaining_work: list[dict[str, Any]] = []
        for name in STORY_STAGE_SEQUENCE:
            if name not in completed:
                remaining_work.append({"stage": name, "estimated_minutes": STAGE_ESTIMATES_MINUTES.get(name, 10)})
        estimate_total_work = sum(int(item["estimated_minutes"]) for item in remaining_work)
        critical_minutes: dict[str, int] = {}
        for name in STORY_STAGE_SEQUENCE:
            if name in completed:
                critical_minutes[name] = 0
                continue
            upstream = max((critical_minutes.get(dependency, 0) for dependency in STORY_STAGE_DEPENDENCIES[name]), default=0)
            critical_minutes[name] = upstream + int(STAGE_ESTIMATES_MINUTES.get(name, 10))
        estimate_total = max(critical_minutes.values(), default=0) if scheduler_mode == "dag" else estimate_total_work
        dag_ready = self._ready_dag_stages(manifest, completed, set()) if scheduler_mode == "dag" else ([] if stage_name == "done" else [stage_name])
        branch_status: dict[str, list[dict[str, str]]] = {}
        for name in STORY_STAGE_SEQUENCE:
            recorded_status = str(agent_data.get("stages", {}).get(name, {}).get("status", "pending"))
            effective_status = (
                "passed"
                if name in completed
                else ("stale" if recorded_status == "passed" else recorded_status)
            )
            branch_status.setdefault(STAGE_BRANCHES.get(name, "other"), []).append(
                {"stage": name, "status": effective_status}
            )
        timing = self._timing_status(agent_data)
        stage_records = agent_data.get("stages", {}) if isinstance(agent_data.get("stages"), dict) else {}
        retries = {
            name: {
                "status": record.get("status", "pending"),
                "attempts": int(record.get("attempts", 0)),
                "provider": record.get("provider", ""),
                "actual_cost": float(record.get("actual_cost", 0.0)),
                "retry_reason": record.get("retry_reason", ""),
            }
            for name, record in stage_records.items()
            if isinstance(record, dict) and (int(record.get("attempts", 0)) > 1 or record.get("status") in {"retrying", "blocked", "failed"})
        }
        storyboard = self._storyboard_path(manifest)
        image_dir = self._image_dir()
        staging_images = self._codex_stage_dir("codex_story_images") / "images"
        story_image_lineage = self._story_image_lineage_status(
            manifest,
            storyboard=storyboard,
            image_dir=image_dir,
            staging_images=staging_images,
        )
        supervisor: dict[str, Any] = {"running": False}
        supervisor_path = self.context.paths.status / "story_agent_supervisor.json"
        if supervisor_path.exists():
            try:
                supervisor = json.loads(supervisor_path.read_text(encoding="utf-8"))
                pid = int(supervisor.get("pid", 0))
                supervisor["running"] = process_is_alive(pid)
            except (ValueError, json.JSONDecodeError):
                supervisor["running"] = False
        payload = {
            "story_agent_version": STORY_AGENT_RELEASE_VERSION,
            "job_id": manifest.get("agent", {}).get("job_id", ""),
            "project_dir": str(self.context.project_dir),
            "next_stage": stage_name,
            "agent_status": agent_data.get("status", "pending"),
            "last_checkpoint": agent_data.get("last_checkpoint", ""),
            "heartbeat_at": agent_data.get("heartbeat_at", ""),
            "blocked_reason": agent_data.get("blocked_reason", ""),
            "external_blockers": agent_data.get("external_blockers", []),
            "branch_blockers": agent_data.get("branch_blockers", {}),
            "scheduler": scheduler_data,
            "ready_stages": dag_ready,
            "branches": branch_status,
            "recovery_action": self._recovery_action(manifest, stage_name),
            "budget": agent_data.get("budget", {}),
            "timing": timing,
            "retries_and_failures": retries,
            "remaining_work": remaining_work,
            "estimated_remaining_minutes": {
                "optimistic": round(estimate_total * 0.65),
                "nominal": estimate_total,
                "conservative": round(estimate_total * 1.8),
                "total_work_minutes": estimate_total_work,
                "note": "DAG 模式 nominal 为依赖关系的剩余关键路径；外部排队、登录阻塞和重试不在确定性承诺内。",
            },
            "supervisor": supervisor,
            "state_file": str(self.state_path),
            "codex_mode": self.context.codex_mode,
            "model_routing": {
                "roles": 2,
                "commander": {
                    "model": self.context.codex_model,
                    "reasoning_effort": self.context.codex_reasoning_effort,
                },
                "worker": {
                    "model": self.context.codex_worker_model,
                    "reasoning_effort": self.context.codex_worker_reasoning_effort,
                },
            },
            "story_contract": contract_diagnostics(self.context.project_dir, manifest),
            "story_images": {
                "actual": self._story_image_count(image_dir),
                "expected_named_actual": self._expected_named_story_image_count(image_dir, manifest, storyboard),
                "expected": self._expected_story_image_count(manifest, storyboard),
                "staging_actual": self._story_image_count(staging_images),
                "staging_expected_named_actual": self._expected_named_story_image_count(staging_images, manifest, storyboard),
                "staging_dir": str(staging_images),
                "image_dir": str(image_dir or ""),
                "storyboard": str(storyboard or ""),
                **story_image_lineage,
            },
            "final_delivery": manifest.get("outputs", {}).get("final_delivery_checklist", ""),
            "completed_at": manifest.get("completed_at", ""),
            "completion_valid": stage_name == "done" and bool(manifest.get("completed_at")),
        }
        payload.update(
            self._observability_status(
                manifest,
                stage_name=stage_name,
                completed=completed,
                supervisor=supervisor,
                stage_records=stage_records,
                storyboard=storyboard,
                image_dir=image_dir,
                staging_images=staging_images,
                story_image_lineage=story_image_lineage,
            )
        )
        return payload

    def status(self) -> None:
        print(json.dumps(self.status_payload(), ensure_ascii=False, indent=2))

    @staticmethod
    def _record_duration_seconds(record: dict[str, Any], *, running: bool) -> int | None:
        started = str(record.get("started_at") or "")
        finished = str(record.get("finished_at") or "")
        if not started:
            return None
        try:
            start_value = time.mktime(time.strptime(started, "%Y-%m-%d %H:%M:%S"))
            end_value = (
                time.time()
                if running or not finished
                else time.mktime(time.strptime(finished, "%Y-%m-%d %H:%M:%S"))
            )
        except ValueError:
            return None
        return max(0, round(end_value - start_value))

    @staticmethod
    def _read_json_object(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _observability_status(
        self,
        manifest: dict[str, Any],
        *,
        stage_name: str,
        completed: set[str],
        supervisor: dict[str, Any],
        stage_records: dict[str, Any],
        storyboard: Path | None,
        image_dir: Path | None,
        staging_images: Path,
        story_image_lineage: dict[str, Any],
    ) -> dict[str, Any]:
        agent = manifest.get("agent", {}) if isinstance(manifest.get("agent"), dict) else {}
        scheduler = agent.get("scheduler", {}) if isinstance(agent.get("scheduler"), dict) else {}
        running_attempts = scheduler.get("running", {}) if isinstance(scheduler.get("running"), dict) else {}
        ready = self._ready_dag_stages(manifest, completed, set()) if scheduler.get("mode") == "dag" else []
        current_stage = next(iter(running_attempts), "") or (ready[0] if ready else stage_name)

        mutable_supervisor = self._read_json_object(supervisor_state_path(self.context.project_dir))
        merged_supervisor = {**supervisor, **mutable_supervisor}
        try:
            supervisor_pid = int(mutable_supervisor.get("pid") or supervisor.get("pid") or 0)
        except (TypeError, ValueError):
            supervisor_pid = 0
        supervisor_alive = process_is_alive(supervisor_pid)
        heartbeat_at = str(mutable_supervisor.get("heartbeat_at") or "")
        heartbeat_age = age_seconds(heartbeat_at)
        heartbeat_limit = int(mutable_supervisor.get("heartbeat_timeout_seconds") or 120)
        recorded_supervisor_status = str(mutable_supervisor.get("status") or "")
        if supervisor_alive and heartbeat_age is not None and heartbeat_age > heartbeat_limit:
            effective_supervisor_status = "running_heartbeat_stale"
        elif supervisor_alive:
            effective_supervisor_status = recorded_supervisor_status or "running"
        elif recorded_supervisor_status in {"completed", "cancelled", "terminal_bug"}:
            effective_supervisor_status = recorded_supervisor_status
        else:
            effective_supervisor_status = "stopped"
        child = (
            mutable_supervisor.get("child")
            if isinstance(mutable_supervisor.get("child"), dict)
            else {}
        )
        try:
            child_pid = int(child.get("pid") or 0)
        except (TypeError, ValueError):
            child_pid = 0
        merged_supervisor.update(
            {
                "pid": supervisor_pid,
                "running": supervisor_alive,
                "heartbeat_at": heartbeat_at,
                "heartbeat_age_seconds": round(heartbeat_age, 1) if heartbeat_age is not None else None,
                "effective_status": effective_supervisor_status,
                "child": {**child, "running": process_is_alive(child_pid)},
                "state_file": str(supervisor_state_path(self.context.project_dir)),
            }
        )

        stage_rows: list[dict[str, Any]] = []
        provider_receipts: list[dict[str, Any]] = []
        for name in STORY_STAGE_SEQUENCE:
            raw_record = stage_records.get(name, {})
            record = raw_record if isinstance(raw_record, dict) else {}
            recorded = str(record.get("status") or "pending")
            effective = "passed" if name in completed else ("stale" if recorded == "passed" else recorded)
            running = name in running_attempts or effective in {"running", "reviewing"}
            attempt = running_attempts.get(name, {}) if isinstance(running_attempts.get(name), dict) else {}
            review = record.get("review") if isinstance(record.get("review"), dict) else {}
            request_id = str(record.get("request_id") or attempt.get("attempt_id") or "")
            duration_seconds = self._record_duration_seconds(record, running=running)
            baseline_estimate = int(
                record.get("estimated_remaining_seconds")
                or STAGE_ESTIMATES_MINUTES.get(name, 0) * 60
            )
            if running and duration_seconds is not None:
                dynamic_estimate = max(0, baseline_estimate - duration_seconds)
                estimate_status = "overdue" if duration_seconds > baseline_estimate else "counting_down"
            elif effective == "passed":
                dynamic_estimate = 0
                estimate_status = "complete"
            else:
                dynamic_estimate = baseline_estimate
                estimate_status = "baseline"
            status_explanation = ""
            if effective == "stale":
                status_explanation = (
                    "上次通过的产物已被后续修订或当前哈希/后置条件取代；看板保留旧记录作审计，"
                    "但不会把它当成当前有效结果。"
                )
            row = {
                "stage": name,
                "branch": STAGE_BRANCHES.get(name, "other"),
                "recorded_status": recorded,
                "effective_status": effective,
                "attempts": int(record.get("attempts") or 0),
                "infrastructure_attempts": int(record.get("infrastructure_attempts") or 0),
                "quality_attempts": int(record.get("quality_attempts") or 0),
                "attempt_id": str(attempt.get("attempt_id") or ""),
                "duration_seconds": duration_seconds,
                "provider": str(record.get("provider") or ""),
                "request_id": request_id,
                "actual_cost": float(record.get("actual_cost") or 0.0),
                "message": str(record.get("message") or ""),
                "why_running": str(
                    attempt.get("why_running") or record.get("why_running") or record.get("message") or ""
                ),
                "retry_scope": str(record.get("retry_scope") or name),
                "retry_files": list(
                    record.get("retry_files")
                    if isinstance(record.get("retry_files"), list)
                    else review.get("retry_files") if isinstance(review.get("retry_files"), list) else []
                ),
                "estimated_remaining_seconds": dynamic_estimate,
                "estimate_status": estimate_status,
                "status_explanation": status_explanation,
                "postconditions": record.get("postconditions", {}),
                "input_hashes": record.get("input_hashes", {}),
                "output_hashes": record.get("output_hashes", {}),
                "input_artifact_hashes": record.get("input_artifact_hashes", record.get("input_hashes", {})),
                "output_artifact_hashes": record.get("output_artifact_hashes", record.get("output_hashes", {})),
                "artifacts": record.get("artifacts", []),
            }
            stage_rows.append(row)
            if row["provider"] or request_id:
                provider_receipts.append(
                    {
                        "stage": name,
                        "provider": row["provider"],
                        "request_id": request_id,
                        "attempt_id": row["attempt_id"],
                        "output_hashes": row["output_hashes"],
                        "actual_cost": row["actual_cost"],
                    }
                )

        expected_images = int(story_image_lineage.get("expected") or 0)
        valid_images = int(story_image_lineage.get("current_lineage_valid_count") or 0)
        physical_images = int(story_image_lineage.get("physical_expected_named_count") or 0)
        physical_staging = int(
            story_image_lineage.get("physical_staging_expected_named_count") or 0
        )
        stale_images = int(story_image_lineage.get("stale_or_unbound_count") or 0)

        jobs = self._jobs_csv(manifest)
        video_targets: list[str] = []
        invalid_video_targets: list[str] = []
        if jobs and jobs.is_file():
            try:
                with jobs.open(encoding="utf-8-sig", newline="") as source:
                    for row in csv.DictReader(source):
                        name = str(row.get("target_video_filename") or "").strip()
                        if not name:
                            invalid_video_targets.append("<empty>")
                        elif Path(name).name != name or Path(name).suffix.lower() != ".mp4":
                            invalid_video_targets.append(name)
                        elif name not in video_targets:
                            video_targets.append(name)
                        request_id = str(
                            row.get("provider_request_id")
                            or row.get("request_id")
                            or row.get("task_id")
                            or ""
                        ).strip()
                        if request_id:
                            provider_receipts.append(
                                {
                                    "stage": "generate_videos",
                                    "provider": str(row.get("provider") or ""),
                                    "request_id": request_id,
                                    "attempt_id": str(row.get("attempt_id") or ""),
                                    "target": name,
                                    "receipt": str(row.get("receipt_path") or ""),
                                }
                            )
            except (OSError, csv.Error):
                invalid_video_targets.append("<unreadable jobs CSV>")
        videos_dir = self.context.paths.video_jobs / "videos"
        actual_videos = sum(
            1
            for name in video_targets
            if (videos_dir / name).is_file() and (videos_dir / name).stat().st_size > 0
        )

        music_names: list[str] = []
        invalid_music_targets: list[str] = []
        plan = self._music_plan()
        if plan.is_file():
            try:
                with plan.open(encoding="utf-8-sig", newline="") as source:
                    for row in csv.DictReader(source):
                        name = str(row.get("target_audio_filename") or "").strip()
                        suffix = Path(name).suffix.lower()
                        if (
                            not name
                            or Path(name).name != name
                            or suffix not in AUDIO_EXTENSIONS
                            or name in music_names
                        ):
                            invalid_music_targets.append(name or "<empty>")
                        else:
                            music_names.append(name)
            except (OSError, csv.Error):
                invalid_music_targets.append("<unreadable music plan>")
        downloads = self._suno_downloads_dir()
        actual_music = sum(
            1
            for name in music_names
            if (downloads / name).is_file() and (downloads / name).stat().st_size > 0
        )

        events = load_agent_events(self.context.project_dir, limit=100)
        if not events:
            legacy_events = self.state.get("events", []) if isinstance(self.state.get("events"), list) else []
            events = [
                {
                    "kind": "legacy_agent_event_v1",
                    "timestamp": item.get("time", ""),
                    "event_type": item.get("event", ""),
                    "stage": item.get("stage", item.get("event", "")),
                    "status": item.get("status", ""),
                    "summary": item.get("message", ""),
                }
                for item in legacy_events[-100:]
                if isinstance(item, dict)
            ]
        for item in events:
            if item.get("event_type") != "provider_receipt":
                continue
            provider = item.get("provider", {}) if isinstance(item.get("provider"), dict) else {}
            provider_receipts.append(
                {
                    "stage": str(item.get("stage") or ""),
                    "provider": str(provider.get("name") or ""),
                    "request_id": str(provider.get("request_id") or ""),
                    "receipt_id": str(provider.get("receipt_id") or ""),
                    "attempt_id": str(item.get("attempt_id") or ""),
                    "output_hashes": item.get("output_evidence_hashes", {}),
                    "actual_cost": item.get("cost_cny"),
                }
            )
        notifications = load_notifications(self.context.project_dir, limit=100)
        recovery_records = [
            item
            for item in read_ndjson(recovery_log_path(self.context.project_dir), limit=100)
            if str(item.get("kind") or "").startswith("recovery_decision")
        ]
        recovery = recovery_records[-1] if recovery_records else {}
        if not recovery and isinstance(mutable_supervisor.get("recovery"), dict):
            recovery = dict(mutable_supervisor["recovery"])

        agent_log_dir = self.context.paths.status / "agent_logs"
        recent_logs = (
            sorted(
                (path for path in agent_log_dir.iterdir() if path.is_file()),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )[:12]
            if agent_log_dir.is_dir()
            else []
        )
        immutable_supervisor_path = self.context.paths.status / "story_agent_supervisor.json"
        supervisor_log = self.context.paths.status / "story_agent_supervisor.log"
        evidence = {
            "manifest": str(self.context.paths.manifest),
            "legacy_state": str(self.state_path),
            "events": str(event_log_path(self.context.project_dir)),
            "notifications": str(notification_log_path(self.context.project_dir)),
            "recovery_decisions": str(recovery_log_path(self.context.project_dir)),
            "supervisor_record": str(immutable_supervisor_path),
            "supervisor_state": str(supervisor_state_path(self.context.project_dir)),
            "supervisor_log": str(supervisor_log),
        }
        if recent_logs:
            evidence["latest_stage_log"] = str(recent_logs[0])
        reviews_dir = self.context.paths.status / "reviews"
        contact_sheets = (
            sorted(
                (
                    path
                    for path in reviews_dir.glob("*contact_sheet*")
                    if path.is_file()
                ),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            if reviews_dir.is_dir()
            else []
        )
        if contact_sheets:
            evidence["latest_contact_sheet"] = str(contact_sheets[0])
        release_preview = self.context.paths.status / "release_preview_frames" / "preview_contact_sheet.png"
        if release_preview.is_file():
            evidence["release_preview_contact_sheet"] = str(release_preview)
        dashboard_state = self._read_json_object(self.context.paths.status / "story_agent_dashboard.json")
        codex_usage_records = read_ndjson(self.context.paths.status / "codex_usage.ndjson", limit=5000)
        known_codex_tokens = sum(
            int(item.get("total_tokens") or 0)
            for item in codex_usage_records
            if isinstance(item.get("total_tokens"), int)
        )
        provider_cost = sum(float(row.get("actual_cost") or 0.0) for row in stage_rows)
        render_stages = {
            "assemble_final", "release_preview", "package_release",
            "product_preflight", "product_package",
        }
        render_time_seconds = sum(
            int(row.get("duration_seconds") or 0)
            for row in stage_rows
            if row.get("stage") in render_stages
        )
        formal_encode_time_seconds = sum(
            int(row.get("duration_seconds") or 0)
            for row in stage_rows
            if row.get("stage") == "package_release"
        )
        video_generation_cost = sum(
            float(row.get("actual_cost") or 0.0)
            for row in stage_rows
            if row.get("stage") == "generate_videos"
        )
        music_cost = sum(
            float(row.get("actual_cost") or 0.0)
            for row in stage_rows
            if row.get("stage") in {"suno_generate", "assemble_music"}
        )
        budget = agent.get("budget") if isinstance(agent.get("budget"), dict) else {}
        current_record = stage_records.get(current_stage) if isinstance(stage_records.get(current_stage), dict) else {}
        current_attempt = running_attempts.get(current_stage) if isinstance(running_attempts.get(current_stage), dict) else {}
        why_running = str(
            current_attempt.get("why_running")
            or current_record.get("why_running")
            or current_record.get("message")
            or (f"正在执行 {current_stage}" if current_stage and current_stage != "done" else "已完成")
        )

        job_lock_path = self.context.paths.status / "story_agent.lock"
        job_lock_payload = self._read_json_object(job_lock_path)
        try:
            job_lock_pid = int(job_lock_payload.get("pid") or 0)
        except (TypeError, ValueError):
            job_lock_pid = 0

        return {
            "current_stage": current_stage,
            "agent_heartbeat_age_seconds": age_seconds(str(agent.get("heartbeat_at") or "")),
            "supervisor": merged_supervisor,
            "stage_rows": stage_rows,
            "dag": {
                "nodes": STORY_STAGE_SEQUENCE,
                "edges": [
                    {"from": dependency, "to": stage}
                    for stage in STORY_STAGE_SEQUENCE
                    for dependency in STORY_STAGE_DEPENDENCIES[stage]
                ],
            },
            "artifact_progress": {
                "故事图片（当前有效血缘）": {
                    "actual": valid_images,
                    "expected": expected_images,
                    "detail": (
                        f"generation_context_current={bool(story_image_lineage.get('generation_context_current'))}; "
                        f"disk_present={physical_images}; stale_or_unbound={stale_images}; "
                        f"manifest={story_image_lineage.get('generation_manifest') or ''}"
                    ),
                },
                "故事图片（磁盘现有，含旧版）": {
                    "actual": physical_images,
                    "expected": expected_images,
                    "detail": str(image_dir or ""),
                },
                "图片 staging（磁盘现有，含旧版）": {
                    "actual": physical_staging,
                    "expected": expected_images,
                    "detail": str(staging_images),
                },
                "图生视频片段": {
                    "actual": actual_videos,
                    "expected": len(video_targets),
                    "detail": (
                        "存在非法 target_video_filename：" + "、".join(invalid_video_targets[:5])
                        if invalid_video_targets
                        else str(videos_dir)
                    ),
                },
                "Suno 音乐分段": {
                    "actual": actual_music,
                    "expected": len(music_names) + len(invalid_music_targets),
                    "detail": (
                        "音乐计划含非法目标：" + "、".join(invalid_music_targets[:5])
                        if invalid_music_targets
                        else str(downloads)
                    ),
                },
            },
            "provider_receipts": provider_receipts,
            "dashboard": dashboard_state,
            "why_running": why_running,
            "retry_scope": str(current_record.get("retry_scope") or current_stage or ""),
            "delivery_state": str(agent.get("delivery_state") or ""),
            "cost_and_usage": {
                "provider_cost_cny": round(provider_cost, 4),
                "budget_spent_cny": round(float(budget.get("spent") or 0.0), 4),
                "codex_total_tokens_reported": known_codex_tokens,
                "codex_calls": len(codex_usage_records),
                "codex_calls_without_cli_usage": sum(1 for item in codex_usage_records if item.get("total_tokens") is None),
                "video_generation_cost_cny": round(video_generation_cost, 4),
                "music_cost_cny": round(music_cost, 4) if music_cost else None,
                "music_cost_status": "reported" if music_cost else "not_reported_by_local_runtime",
                "imagegen_cost_cny": None,
                "imagegen_cost_status": "not_reported_by_local_runtime",
                "render_time_seconds": render_time_seconds,
                "formal_encode_time_seconds": formal_encode_time_seconds,
                "render_time_source": "stage_rows.duration_seconds",
            },
            "notifications": notifications,
            "unacknowledged_notifications": sum(
                1 for item in notifications if not item.get("acknowledged_at")
            ),
            "events": events,
            "recovery": recovery,
            "locks": {
                "job_lock": {
                    "path": str(job_lock_path),
                    "exists": job_lock_path.exists(),
                    "pid": job_lock_pid,
                    "owner_alive": process_is_alive(job_lock_pid),
                },
                "control": load_control(self.context.project_dir),
            },
            "evidence": evidence,
            "recent_logs": [str(path) for path in recent_logs],
        }

    def _timing_status(self, agent_data: dict[str, Any]) -> dict[str, Any]:
        started_text = str(agent_data.get("started_at", ""))
        elapsed_hours = runtime_elapsed_seconds(agent_data) / 3600
        deadline = runtime_deadline_state(agent_data)
        return {
            "started_at": started_text,
            "elapsed_hours": round(elapsed_hours, 3),
            "active_elapsed_seconds": round(runtime_elapsed_seconds(agent_data), 3),
            "runtime_deadline_enabled": deadline["enabled"],
            "deadline_policy": deadline["deadline_behavior"],
            "deadline_hours": deadline["deadline_hours"],
            "target_delivery_seconds": deadline["target_delivery_seconds"],
            "remaining_deadline_seconds": deadline["remaining_seconds"],
            "remaining_deadline_hours": (
                round(float(deadline["remaining_seconds"]) / 3600, 3)
                if deadline["remaining_seconds"] is not None else None
            ),
            "deadline_reached": deadline["reached"],
        }

    def _recovery_action(self, manifest: dict[str, Any], next_stage: str) -> str:
        agent_data = manifest.get("agent", {})
        status = str(agent_data.get("status", ""))
        reason = str(agent_data.get("blocked_reason", ""))
        combined = f"{next_stage} {reason}".lower()
        job_id = str(agent_data.get("job_id", "<job_id>"))
        if status == "cancelled" or agent_data.get("cancel_requested"):
            return f"依次运行 `python3 story_agent.py resume --job {job_id}` 和 `python3 story_agent.py start --job {job_id}`。"
        if next_stage.startswith("codex_") and ("用量" in reason or "usage" in combined or "额度" in reason):
            return f"等待报告中的 Codex 用量恢复时间，再执行 python3 story_agent.py resume --job {job_id}；已通过检查点不会重做。"
        if "suno" in combined or "browser" in combined or "登录" in reason or "captcha" in combined:
            return "在 Codex 主任务恢复 Suno 登录/浏览器能力，完成 handoff 下载后执行 resume 和 start。"
        if "磁盘" in reason:
            return "释放项目磁盘空间至最低阈值以上，再执行 resume 和 start。"
        if "api key" in combined or "credential" in combined or "鉴权" in reason:
            return "在环境变量中配置对应供应商密钥，确认不写入 manifest 后执行 resume 和 start。"
        if "预算" in reason or "上限" in reason:
            return "查看成本报告；只有用户明确调整预算或改用零费用供应商后才能 resume。"
        if status in {"blocked", "failed"}:
            return f"查看最新阶段日志和 agent_morning_report.md，处理原因后执行 python3 story_agent.py resume --job {job_id}。"
        return "无需人工恢复；后台 supervisor 会继续推进。"

    def _manifest(self) -> dict[str, Any]:
        if self.read_only:
            return ensure_manifest_v2(load_manifest(self.context.paths) or {})
        init_project(self.context.project_dir, story_name=self.context.story_name, slug=self.context.slug)
        try:
            manifest = refresh_project_outputs(self.context.project_dir)
        except Exception:
            manifest = load_manifest(self.context.paths) or init_project(
                self.context.project_dir,
                story_name=self.context.story_name,
                slug=self.context.slug,
            )
        return ensure_manifest_v2(manifest)

    def _next_stage(self, manifest: dict[str, Any]) -> tuple[str, Callable[[dict[str, Any]], StageResult]]:
        for name, done, action in self._stage_checks():
            if not done(manifest):
                return name, action
        return "done", lambda _: StageResult("done", "完成")

    def _stage_checks(self) -> list[tuple[str, Callable[[dict[str, Any]], bool], Callable[[dict[str, Any]], StageResult]]]:
        checks: list[tuple[str, Callable[[dict[str, Any]], bool], Callable[[dict[str, Any]], StageResult]]] = [
            ("import_inbox", self._has_imported_inbox, self._stage_import_inbox),
            ("source_edit", self._has_source_edit, self._stage_source_edit),
            ("source_text_correction", self._has_source_text_correction, self._stage_source_text_correction),
            ("source_edit_review", self._has_source_edit_review, self._stage_source_edit_review),
            ("setup_project", self._has_setup_project, self._stage_setup_project),
            ("story_contract", self._has_story_contract, self._stage_story_contract),
            ("story_contract_review", self._has_story_contract_review, self._stage_story_contract_review),
            ("artifact_semantic_plan", self._has_artifact_semantic_plan, self._stage_artifact_semantic_plan),
            ("visual_samples", self._has_visual_samples, self._stage_visual_samples),
            ("visual_sample_review", self._has_visual_sample_review, self._stage_visual_sample_review),
            ("codex_story_images", self._has_story_images, self._stage_codex_story_images),
            ("story_images_review", self._has_story_images_review, self._stage_story_images_review),
            ("prepare_jobs", self._has_jobs_csv, self._stage_prepare_jobs),
            ("timing", self._has_timing, self._stage_timing),
            ("video_prompt_review", self._has_video_prompt_review, self._stage_video_prompt_review),
            ("generate_videos", self._has_generated_videos, self._stage_generate_videos),
            ("video_qa", self._has_video_qa, self._stage_video_qa),
            ("video_review", self._has_video_review, self._stage_video_review),
            ("apply_review", self._has_applied_review, self._stage_apply_review),
            ("music_request", self._has_music_request, self._stage_music_request),
            ("suno_generate", self._has_suno_audio, self._stage_suno_generate),
            ("assemble_music", self._has_background_music, self._stage_assemble_music),
            ("music_qa", self._has_music_qa, self._stage_music_qa),
            ("assemble_final", self._has_background_assembly, self._stage_assemble_final),
            ("release_assets", self._has_release_assets, self._stage_release_assets),
            ("product_preflight", self._has_product_preflight, self._stage_product_preflight),
            ("product_annotation", self._has_product_annotation, self._stage_product_annotation),
            ("product_annotation_review", self._has_product_annotation_review, self._stage_product_annotation_review),
            ("release_preview", self._has_release_preview, self._stage_release_preview),
            ("package_release", self._has_release_videos, self._stage_package_release),
            ("release_qa", self._has_release_qa, self._stage_release_qa),
            ("release_video_review", self._has_release_video_review, self._stage_release_video_review),
            ("publish_package", self._has_publish_package, self._stage_publish_package),
            ("publish_package_review", self._has_publish_package_review, self._stage_publish_package_review),
            ("product_package", self._has_product_package, self._stage_product_package),
            ("product_package_review", self._has_product_package_review, self._stage_product_package_review),
            ("final_delivery", self._has_final_delivery, self._stage_final_delivery),
            ("doctor", self._has_doctor_report, self._stage_doctor),
        ]
        names = tuple(item[0] for item in checks)
        if names != STORY_STAGE_SEQUENCE:
            raise AgentRuntimeError("状态机阶段顺序与 runtime 清单不一致")
        return checks

    def _reconcile_completed_stage_records(self, manifest: dict[str, Any], next_stage: str) -> None:
        from story_project import write_manifest

        limit = len(STORY_STAGE_SEQUENCE) if next_stage == "done" else STORY_STAGE_SEQUENCE.index(next_stage)
        checks = {name: done for name, done, _action in self._stage_checks()}
        changed = False
        for name in STORY_STAGE_SEQUENCE[:limit]:
            record = manifest.get("agent", {}).get("stages", {}).get(name, {})
            try:
                complete = checks[name](manifest)
            except Exception:
                complete = False
            # A stage can be recorded as blocked/failed even after the user or a
            # recovery path supplies the missing verified artifacts (for example,
            # native cover generation timing out and being completed by the parent
            # Agent).  The current completion predicate is the source of truth; do
            # not leave a completed job with a stale blocked stage record.
            if record.get("status") != "passed" and complete:
                mark_stage(manifest, name, "passed", message="由当前输入/已验证产物满足，或在中断后按当前产物重新核验通过。")
                changed = True
        if changed:
            write_manifest(self.context.paths, manifest)

    def _stage_import_inbox(self, manifest: dict[str, Any]) -> StageResult:
        if self.context.inbox is None:
            return StageResult("done", "未指定投喂区，跳过导入。")
        inbox = self.context.inbox.expanduser()
        if not inbox.exists():
            return StageResult("failed", f"投喂区不存在：{inbox}")
        if not self.context.execute:
            return StageResult("done", f"dry-run: 将从 {inbox} 导入素材到 00_输入素材。")
        inputs = self.context.paths.inputs
        inputs.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        for path in sorted(item for item in inbox.iterdir() if item.is_file()):
            if path.suffix.lower() not in TEXT_EXTENSIONS | AUDIO_EXTENSIONS | VIDEO_EXTENSIONS:
                continue
            target = inputs / path.name
            if path.resolve() != target.resolve() and not target.exists():
                shutil.copy2(path, target)
                copied.append(path.name)
        self.state["inbox_imported_at"] = now()
        self._save_state()
        return StageResult("done", "已导入投喂区素材：" + ("、".join(copied) if copied else "没有新文件"))

    def _stage_setup_project(self, manifest: dict[str, Any]) -> StageResult:
        command = [
            "setup-project",
            "--project-dir",
            str(self.context.project_dir),
            "--story-name",
            self.context.story_name,
            "--slug",
            self.context.slug,
            "--extract-audio",
        ]
        result = self._workflow(command, "保存故事信息并识别素材")
        contract = manifest.get("agent", {}).get("input_contract", {})
        if result.status == "done" and contract.get("mode") == "prepared_greenscreen_confirmed_text":
            current = load_manifest(self.context.paths) or manifest
            narration = first_existing(
                current.get("inputs", {}).get("extracted_narration"),
                current.get("inputs", {}).get("narration"),
            )
            user_inputs = contract.get("user_inputs", []) if isinstance(contract.get("user_inputs"), list) else []
            video_record = next(
                (item for item in user_inputs if isinstance(item, dict) and item.get("role") == "prepared_greenscreen_video"),
                {},
            )
            if narration is not None:
                record_contract_derivative(
                    current,
                    role="extracted_narration",
                    path=narration,
                    source_sha256=str(video_record.get("sha256") or ""),
                    producer="setup_project_audio_extraction",
                )
                from story_project import write_manifest

                write_manifest(self.context.paths, current)
        return result

    def _normalize_contract_for_runtime(
        self,
        manifest: dict[str, Any],
        *,
        invalidate_current_review: bool = False,
    ) -> dict[str, Any]:
        """Apply deterministic contract normalization before independent QA.

        Runtime reconciles its own semantic defaults with the actual line
        source and corrects elevated provenance that cannot support composite
        product policy.  Model-authored substantive choices are not rewritten.
        """

        source = self._artifact_semantic_source(manifest)
        if source is None:
            raise ValueError("缺少可读的逐行语义源，不能补齐合同语义映射")
        paths = contract_paths(self.context.project_dir)
        old_contract = paths["contract"].read_bytes()
        old_summary = paths["summary"].read_bytes() if paths["summary"].is_file() else b""
        semantics_port = self._modules().story_semantics()
        reconciliation = reconcile_semantic_mappings(paths["contract"], source, semantics_port)
        trusted = json.loads(paths["trusted_inputs"].read_text(encoding="utf-8"))
        provenance_changes = normalize_contract_policy_provenance(paths["contract"], trusted)
        added = list(reconciliation["added_mappings"])
        removed = list(reconciliation["removed_mappings"])
        if not added and not removed and not provenance_changes:
            return {
                "semantic_kinds": list(reconciliation["semantic_kinds"]),
                "added_mappings": [],
                "removed_mappings": [],
                "provenance_changes": [],
            }

        archive: Path | None = None
        if invalidate_current_review:
            archive = (
                self.context.paths.status
                / "rejected"
                / "story_contract"
                / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}-runtime-normalization"
            )
            archive.mkdir(parents=True, exist_ok=True)
            (archive / "story_production_contract.json").write_bytes(old_contract)
            if old_summary:
                (archive / "story_production_contract.md").write_bytes(old_summary)
            for name, path in (
                ("story_contract.lock.json", paths["lock"]),
                ("story_contract_bundle.json", paths["bundle"]),
                ("story_contract_review_review.json", paths["review"]),
            ):
                if path.exists():
                    shutil.move(str(path), str(archive / name))
            (archive / "archive_reason.txt").write_text(
                "Runtime 对齐实际语义源并修正不受证据支持的策略来源；旧审核哈希失效，必须重新独立审核。\n",
                encoding="utf-8",
            )

        note = (
            "\n## Runtime 合同规范化\n\n"
            f"- 实际逐行语义源：`{source}`。\n"
            f"- 实际检测语义类型：{', '.join(reconciliation['semantic_kinds'])}。\n"
            f"- 补齐 {len(added)} 个固定映射，移除 {len(removed)} 个已不适用的 Runtime 默认映射。\n"
            f"- 将 {len(provenance_changes)} 项不能由原证据支持的产品策略来源降为 `agent_inference`。\n"
            "- 模型已有的故事事实、角色、状态和视觉选择保持不变。\n"
        )
        with paths["summary"].open("a", encoding="utf-8") as summary:
            summary.write(note)
        receipt_dir = paths["directory"] / "runtime_normalizations"
        receipt = receipt_dir / f"{int(time.time())}-{uuid.uuid4().hex[:8]}.json"
        save_json(
            receipt,
            {
                "schema_version": "story-contract-runtime-normalization/v2",
                "source": str(source),
                "source_sha256": file_sha256(source),
                "contract_sha256_before": hashlib.sha256(old_contract).hexdigest(),
                "contract_sha256_after": file_sha256(paths["contract"]),
                "added_mappings": [
                    {
                        "semantic_kind": item["semantic_kind"],
                        "artifact": item["artifact"],
                        "action": item["action"],
                        "subtitle_policy": item["subtitle_policy"],
                    }
                    for item in added
                ],
                "removed_mappings": [
                    {
                        "semantic_kind": item["semantic_kind"],
                        "artifact": item["artifact"],
                    }
                    for item in removed
                ],
                "provenance_changes": provenance_changes,
                "invalidated_review": invalidate_current_review,
                "archived_review": str(archive) if archive else "",
                "completed_at": now(),
            },
        )
        return {
            "semantic_kinds": list(reconciliation["semantic_kinds"]),
            "added_mappings": added,
            "removed_mappings": removed,
            "provenance_changes": provenance_changes,
        }

    def _stage_story_contract(self, manifest: dict[str, Any]) -> StageResult:
        if self._legacy_contract_policy(manifest):
            return StageResult("done", "V3 冻结项目采用 legacy_passthrough，不补写或重做合同。")
        paths = contract_paths(self.context.project_dir)
        paths["directory"].mkdir(parents=True, exist_ok=True)
        if paths["contract"].is_file() and contract_runtime_issues(self.context.project_dir):
            self._archive_story_contract_attempt("runtime_invalid_or_trusted_input_drift")
        # A regeneration attempt immediately revokes any prior approval lock.
        # If the worker crashes or emits an invalid draft, stale approval must
        # not remain visually or mechanically plausible.
        paths["lock"].unlink(missing_ok=True)
        trusted = build_trusted_input_chain(self.context.project_dir, manifest, load_config())
        write_trusted_input_chain(paths["trusted_inputs"], trusted)
        story_text = Path(str(manifest.get("inputs", {}).get("story_text") or ""))
        if not story_text.is_file():
            return StageResult("blocked", "缺少可信故事文本，无法生成 Story Production Contract。")
        semantic_source = self._artifact_semantic_source(manifest)
        paths["handoff"].write_text(
            "\n".join(
                [
                    "# Story Production Contract 生成任务",
                    "",
                    f"- 故事文本：`{story_text}`",
                    f"- 实际逐行语义源：`{semantic_source or story_text}`",
                    f"- Runtime 可信来源链：`{paths['trusted_inputs']}`",
                    f"- 根 Schema：`{ROOT / 'schemas/story_contract/v1/story_production_contract.schema.json'}`",
                    f"- 输出 JSON：`{paths['contract']}`",
                    f"- 输出说明：`{paths['summary']}`",
                    "",
                    "本阶段只生成文本/JSON 规则，preview_assets 必须是空数组。",
                    "禁止在合同阶段调用 ImageGen、生成视觉小样或要求审核像素级状态；视觉证据统一由后续 visual_samples 阶段按预算生成。",
                    "无角色不得编造角色；无可靠尺度依据不得编造数值比例。",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        prompt = "\n".join(
            [
                "以执行工人身份，为这个新故事生成通用 Story Production Contract 草案。",
                f"完整读取 handoff：`{paths['handoff']}`。",
                "严格遵守根 Schema 及其七个 section Schema；不要修改 Schema 或生产代码。",
                "规则冲突自动采用 task_input > project_config > brand_or_global_default > agent_inference。",
                "可信高优先级来源只能使用 Runtime 可信来源链中已有的 source/source_ref/sha256：",
                "task_input 必须附逐字存在的 evidence_quote；配置/品牌默认必须附可解析的 evidence_pointer。",
                "你自己的分析、视觉补全和推断一律标记 agent_inference；禁止伪装成高优先级来源。",
                "视觉风格只能由用户/项目配置的明确选择决定；必须把可信来源链 /resolved_image_style 中的 key、label 和 prompt 原样绑定到 visual_style.style_profile，不得根据故事类型、标题或文本关键词换风格。",
                "角色身份锚点只锁定原文明确说明或用户明确指定的特征。未指定的外观不写成合同硬约束，由图像模型按已选整体风格完成角色设计。",
                "合同只声明后续需要验证的角色、尺度、状态和布局规则；preview_assets 必须写 []，本阶段不得调用 ImageGen 或生成任何小样。",
                "story_state 必须覆盖会被消耗、撕下、打碎、修复、交付或逐步减少的关键道具。每个状态要精确写明仍存在的数量、成员、颜色或完整性，并把不应再出现的旧状态写入 forbidden，不能只写‘发生变化’。",
                "每个不可逆的实体状态迁移必须在 transition 写 must_show_action=true、action_subject 和 action_object；触发动作必须来自原文，不得编造。只有瞬间跳切、明确离屏事件或抽象状态才可写 must_show_action=false。",
                "尺度优先 qualitative_relation；只有可信依据或机器布局需要时才写宽容数值区间及 numeric_basis。",
                "如果多个角色会在同一画面出现，world_scale.relationships 只覆盖有故事或布局必要的宽松视觉层级；不得为所有角色两两建立无依据的总排序。儿童卡通允许为了表演和可读性适度夸张小角色，不能按现实物种厘米比例机械判定。已有用户批准母版时，定性关系应忠于母版实际画面，不得仅凭现实常识写成 much_smaller。环境或动作参照不是角色尺度证明，不要求为其另画尺度图。",
                "release_layout 只能登记可信来源链能复核的画布或布局默认；没有画布收据时不要凭经验补写精确比例变体。",
                "semantic_artifacts 以 handoff 中的实际逐行语义源为准。只写有明确非默认选择或事实依据的映射；不要耗费推理枚举重复的固定笛卡尔积。Runtime 会在本阶段末补齐缺失默认映射，并移除因语义分类修正而不再适用的 Runtime 默认映射，再做确定性完整性校验。",
                "语义源中若有‘故事告诉我们’、对受众的总结性教训或独立寓意，必须拆成 semantic_kind=moral 并单独建立七类产物 mapping，不得并入 story_body。",
                "标题或道理若用 visual_substitute，必须给出唯一 mutual_exclusion_group；同组 background_subtitles 必须 action=exclude 且 subtitle_policy=hide。Demo 必须按自身 mapping 决定，不借用销售版规则。",
                "所有视觉验证需求只写进对应合同 section；不要把多状态道具画成一张总表，也不要创建 contracts/previews 文件。",
                f"写入 `{paths['contract']}` 和便于人读的 `{paths['summary']}`。不要写合同锁，锁只能由 Runtime 在独立审核通过后生成。",
            ]
        )
        result = self._codex_task(
            stage="story_contract",
            label="Story Production Contract 草案生成",
            handoff=paths["handoff"],
            prompt=prompt,
        )
        if result.status != "done":
            return result
        if not self.context.execute:
            return result
        if not paths["contract"].is_file() or not paths["summary"].is_file():
            return StageResult("blocked", "合同生成任务未写入 JSON 与说明文档。", paths["handoff"])
        trusted = json.loads(paths["trusted_inputs"].read_text(encoding="utf-8"))
        bind_contract_visual_style_to_trusted_default(paths["contract"], trusted)
        defer_contract_visual_samples(paths["contract"], paths["deferred_previews"])
        try:
            self._normalize_contract_for_runtime(manifest)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return StageResult("blocked", f"合同 Runtime 规范化失败：{exc}", paths["contract"])
        issues = contract_runtime_issues(self.context.project_dir)
        if issues:
            return StageResult("blocked", "合同机器校验或可信来源校验失败：" + "；".join(issues[:12]), paths["contract"])
        return StageResult("done", "合同草案通过 Schema、Python Validator 与可信来源链校验。", paths["contract"])

    def _stage_story_contract_review(self, manifest: dict[str, Any]) -> StageResult:
        if self._legacy_contract_policy(manifest):
            return StageResult("done", "V3 冻结项目采用 legacy_passthrough，不新增合同审核。")
        paths = contract_paths(self.context.project_dir)
        try:
            self._normalize_contract_for_runtime(manifest, invalidate_current_review=True)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return StageResult("blocked", f"独立审核前合同 Runtime 规范化失败：{exc}", paths["contract"])
        issues = contract_runtime_issues(self.context.project_dir)
        if issues:
            paths["lock"].unlink(missing_ok=True)
            return StageResult("blocked", "独立审核前合同校验失败：" + "；".join(issues[:12]), paths["contract"])
        try:
            artifacts, images = contract_review_artifacts(self.context.project_dir)
        except Exception as exc:
            return StageResult("blocked", f"无法构建合同审核证据：{exc}", paths["contract"])
        if not paths["summary"].is_file():
            return StageResult("blocked", "合同缺少人类可读说明，不能独立审核。", paths["contract"])
        bundle = write_review_bundle(paths["bundle"], artifacts)
        try:
            cached_review = json.loads(paths["review"].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached_review = None
        if isinstance(cached_review, dict):
            try:
                cached_score = float(cached_review.get("score") or 0)
            except (TypeError, ValueError):
                cached_score = 0
            if (
                not contract_review_payload_issues(cached_review)
                and cached_review.get("approved") is True
                and cached_score >= 85
                and not cached_review.get("critical_errors")
                and cached_review.get("artifact_sha256") == file_sha256(bundle)
            ):
                lock = write_contract_lock(self.context.project_dir, bundle=bundle, review=paths["review"])
                return StageResult(
                    "done",
                    f"复用当前哈希的独立合同审核并由 Runtime 锁定：{cached_score:g} 分",
                    lock,
                )
        result, payload = self._structured_review(
            stage="story_contract_review",
            label="Story Production Contract 独立审核",
            bundle=bundle,
            images=images,
            rubric=(
                "逐项审核七类合同是否忠于可信输入、是否把推断错误伪装成高优先级来源、语义/角色/状态/尺度/品牌/布局是否互相一致；"
                "本阶段只审核文本合同；preview_assets 必须为空，视觉质量与像素级状态留给后续 visual_samples 独立审核。"
                "视觉风格只核对其声明是否原样绑定可信来源链 /resolved_image_style/prompt；本阶段没有图片，缺少视觉证据不得作为拒绝理由，也不得审核角色美感。"
                "world_scale 只审核宽松层级声明是否有故事或布局必要性、来源优先级是否诚实；尺度在实际画面中是否可读留给 visual_samples，不得在本阶段索要尺度图。"
                "Schema 与可信来源格式已由 Runtime 确定性校验，不要重复审 Schema 文件。必须提供 evidence_matrix，逐节引用合同 JSON 路径与可信来源记录。"
                "若 approved=false，还必须输出 retry_contract_sections，只列需要修订的七类顶层合同节；未列出的节由 Runtime 强制恢复，不能随整份文档一起重写。"
            ),
        )
        if payload is None:
            paths["lock"].unlink(missing_ok=True)
            return result
        review_issues = contract_review_payload_issues(payload)
        if review_issues:
            paths["lock"].unlink(missing_ok=True)
            return StageResult(
                "blocked",
                "合同独立审核缺少可验证的逐节证据：" + "；".join(review_issues),
                paths["review"],
            )
        if result.status != "done":
            paths["lock"].unlink(missing_ok=True)
            quality_attempts = self._story_contract_quality_attempts(paths["review"])
            if self._can_retry_contract_review(quality_attempts):
                revision = self._revise_story_contract_after_review(manifest, payload)
                if revision.status == "done":
                    return StageResult(
                        "retrying",
                        "已按独立审核意见完成唯一一次定向文本修订，将使用新哈希重新审核。",
                        revision.handoff,
                    )
                return revision
            return result
        lock = write_contract_lock(self.context.project_dir, bundle=bundle, review=paths["review"])
        return StageResult("done", f"合同独立审核通过并由 Runtime 确定性锁定：{payload.get('score')} 分", lock)

    def _archive_story_contract_attempt(self, reason: str) -> Path:
        """Move a rejected/stale contract attempt aside without deleting evidence."""

        paths = contract_paths(self.context.project_dir)
        archive = (
            self.context.paths.status
            / "rejected"
            / "story_contract"
            / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        )
        candidates = {
            "story_production_contract.json": paths["contract"],
            "story_production_contract.md": paths["summary"],
            "story_contract_trusted_inputs.json": paths["trusted_inputs"],
            "story_contract_handoff.md": paths["handoff"],
            "deferred_visual_samples.json": paths["deferred_previews"],
            "contract_revision_scope.json": paths["revision_scope"],
            "story_contract.lock.json": paths["lock"],
            "story_contract_bundle.json": paths["bundle"],
            "story_contract_review_review.json": paths["review"],
        }
        existing = [(name, source) for name, source in candidates.items() if source.exists()]
        previews = paths["directory"] / "previews"
        if not existing and not previews.exists():
            return archive
        archive.mkdir(parents=True, exist_ok=True)
        for name, source in existing:
            shutil.move(str(source), str(archive / name))
        if previews.exists():
            approved_manifest = (
                self.context.paths.status / "style_references" / "approved_preview_manifest.json"
            )
            preserve_approved = False
            try:
                approved_payload = json.loads(approved_manifest.read_text(encoding="utf-8"))
                preserve_approved = approved_payload.get("replace_allowed") is False
            except (OSError, json.JSONDecodeError):
                pass
            if preserve_approved:
                shutil.copytree(previews, archive / "previews")
            else:
                shutil.move(str(previews), str(archive / "previews"))
        (archive / "archive_reason.txt").write_text(reason.strip() + "\n", encoding="utf-8")
        return archive

    def _story_contract_quality_attempts(self, current_review: Path) -> int:
        candidates = [current_review]
        rejected_root = self.context.paths.status / "rejected" / "story_contract"
        if rejected_root.exists():
            candidates.extend(rejected_root.glob("*/story_contract_review_review.json"))
        current_trusted = contract_paths(self.context.project_dir)["trusted_inputs"]
        try:
            current_trusted_sha = file_sha256(current_trusted)
        except OSError:
            current_trusted_sha = ""
        reviewed_hashes: set[str] = set()
        for candidate in candidates:
            bundle_path = (
                contract_paths(self.context.project_dir)["bundle"]
                if candidate == current_review
                else candidate.parent / "story_contract_bundle.json"
            )
            try:
                bundle_payload = json.loads(bundle_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            trusted_hashes = {
                str(item.get("sha256") or "")
                for item in bundle_payload.get("artifacts", [])
                if isinstance(item, dict)
                and Path(str(item.get("path") or "")).name == "story_contract_trusted_inputs.json"
            }
            if current_trusted_sha not in trusted_hashes:
                continue
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            artifact_sha = str(payload.get("artifact_sha256", "")) if isinstance(payload, dict) else ""
            if len(artifact_sha) == 64:
                reviewed_hashes.add(artifact_sha)
        return max(1, len(reviewed_hashes))

    def _revise_story_contract_after_review(
        self,
        manifest: dict[str, Any],
        review_payload: dict[str, Any],
    ) -> StageResult:
        paths = contract_paths(self.context.project_dir)
        old_contract_sha = file_sha256(paths["contract"])
        archive = self._archive_story_contract_attempt("independent_review_rejected")
        paths["directory"].mkdir(parents=True, exist_ok=True)
        trusted = build_trusted_input_chain(self.context.project_dir, manifest, load_config())
        write_trusted_input_chain(paths["trusted_inputs"], trusted)
        archived_contract = archive / "story_production_contract.json"
        archived_review = archive / "story_contract_review_review.json"
        allowed_sections = contract_review_retry_sections(review_payload)
        if not allowed_sections:
            return StageResult(
                "blocked",
                "合同审核拒绝但未声明 retry_contract_sections，拒绝扩大为整份合同重写。",
                archived_review,
            )
        paths["handoff"].write_text(
            "\n".join(
                [
                    "# Story Production Contract 自动修订任务",
                    "",
                    f"- 被拒绝合同（只读）：`{archived_contract}`",
                    f"- 独立审核（只读）：`{archived_review}`",
                    f"- 当前可信来源链：`{paths['trusted_inputs']}`",
                    f"- 根 Schema：`{ROOT / 'schemas/story_contract/v1/story_production_contract.schema.json'}`",
                    f"- 新合同 JSON：`{paths['contract']}`",
                    f"- 新说明：`{paths['summary']}`",
                    f"- 本轮唯一允许修改的合同节：`{', '.join(allowed_sections)}`",
                    "- Runtime 会把未列入上述清单的合同节原样恢复；不要借修订机会重写整份合同。",
                    "- 只修正审核指出的问题及其直接一致性影响，不得篡改可信故事事实。",
                    "- visual_style.style_profile 必须原样继承可信来源链 /resolved_image_style 的 key、label 和 prompt；故事类型和文本关键词不得改变风格。",
                    "- 角色只锁定可信来源明确特征；未指定的外观不写成合同硬约束，由图像模型按已选整体风格完成。",
                    "- world_scale 只保留故事和构图确有必要的宽松层级。儿童卡通可为表演与可读性适度夸张小角色；不得按现实厘米比例硬判，也不得为了环境/动作参照再生成尺度图。用户批准母版是当前视觉关系依据。",
                    "- preview_assets 必须保持 []；本阶段不得调用 ImageGen、生成或修改视觉小样。",
                    "- 状态、角色、尺度与布局只修订 JSON 规则；视觉证据由后续 visual_samples 阶段生成。",
                    "- 不要修改独立审核文件，不要生成合同锁。",
                    "",
                    "## 审核结构化意见",
                    "```json",
                    json.dumps(review_payload, ensure_ascii=False, indent=2, sort_keys=True),
                    "```",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        result = self._codex_task(
            stage="story_contract",
            label="Story Production Contract 自动修订",
            handoff=paths["handoff"],
            prompt=(
                "你是合同修订生产者。完整读取 handoff、被拒绝合同、当前可信来源链与独立审核，"
                "只对审核指出的 JSON 路径及直接一致性影响做一次定向修订，并生成新合同和说明。"
                "禁止调用 ImageGen，preview_assets 必须为 []。"
            ),
        )
        if result.status != "done":
            return result
        if not paths["contract"].is_file() or not paths["summary"].is_file():
            return StageResult("blocked", "合同自动修订未写入 JSON 与说明文档。", paths["handoff"])
        try:
            enforce_targeted_contract_revision(
                archived_contract,
                paths["contract"],
                allowed_sections=allowed_sections,
                receipt_path=paths["revision_scope"],
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            return StageResult("blocked", f"合同定向修订边界校验失败：{exc}", paths["contract"])
        paths["summary"].write_text(
            paths["summary"].read_text(encoding="utf-8")
            + "\n## Runtime 定向修订边界\n\n"
            + f"- 允许修改：{', '.join(allowed_sections)}\n"
            + "- 其余合同节已从被拒绝版本逐值恢复，防止整份合同漂移。\n",
            encoding="utf-8",
        )
        bind_contract_visual_style_to_trusted_default(paths["contract"], trusted)
        defer_contract_visual_samples(paths["contract"], paths["deferred_previews"])
        try:
            self._normalize_contract_for_runtime(manifest)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return StageResult("blocked", f"合同定向修订后的 Runtime 规范化失败：{exc}", paths["contract"])
        if file_sha256(paths["contract"]) == old_contract_sha:
            return StageResult("blocked", "合同自动修订没有改变被拒绝的合同哈希。", paths["contract"])
        issues = contract_runtime_issues(self.context.project_dir)
        if issues:
            return StageResult("blocked", "合同自动修订后机器校验失败：" + "；".join(issues[:12]), paths["contract"])
        return StageResult("done", "合同自动修订完成。", paths["contract"])

    def _stage_artifact_semantic_plan(self, manifest: dict[str, Any]) -> StageResult:
        if self._legacy_contract_policy(manifest):
            return StageResult("done", "V3 冻结项目沿用 story_semantics，不补造语义呈现计划。")
        source = self._artifact_semantic_source(manifest)
        if source is None:
            return StageResult("blocked", "缺少可读的逐行语义源，无法编译 artifact_semantic_plan。")
        if not self.context.execute:
            return StageResult("done", "dry-run：将从已锁定合同确定性编译逐产物语义呈现计划。")
        try:
            normalized = self._normalize_contract_for_runtime(
                manifest,
                invalidate_current_review=True,
            )
            change_count = (
                len(normalized["added_mappings"])
                + len(normalized["removed_mappings"])
                + len(normalized["provenance_changes"])
            )
            if change_count:
                return StageResult(
                    "retrying",
                    f"Runtime 完成 {change_count} 项确定性合同规范化并撤销旧合同锁；将重新独立审核，不重生成整份合同。",
                    contract_paths(self.context.project_dir)["contract"],
                )
            semantics_port = self._modules().story_semantics()
            path = write_artifact_semantic_plan(self.context.project_dir, source, semantics_port)
            load_current_artifact_semantic_plan(self.context.project_dir, source, semantics_port)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return StageResult("blocked", f"逐产物语义呈现计划编译失败：{exc}")
        return StageResult("done", "逐产物语义呈现计划已确定性编译并绑定当前合同与语义源。", path)

    def _stage_visual_samples(self, manifest: dict[str, Any]) -> StageResult:
        if self._legacy_contract_policy(manifest):
            return StageResult("done", "V3 冻结项目沿用既有视觉控制，不补造 V3.5 条件式小样。")
        context = self._prepare_contract_consumer(manifest, "storyboard_images")
        if isinstance(context, StageResult):
            return context
        if context is None:
            return StageResult("blocked", "缺少 storyboard_images 合同投影，无法编译视觉小样。")
        paths = visual_sample_paths(self.context.project_dir)
        style_references = self._approved_style_reference_paths()
        paths["lock"].unlink(missing_ok=True)
        if not self.context.execute:
            return StageResult("done", "dry-run：将优先复用合同预览，并只生成故事实际缺少的视觉小样。")
        try:
            visual_design_port = self._modules().visual_design()
            plan_path = write_visual_sample_plan(self.context.project_dir, context, visual_design_port)
            plan = load_current_visual_sample_plan(self.context.project_dir, context, visual_design_port)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return StageResult("blocked", f"条件式视觉小样计划编译失败：{exc}")
        missing = [item for item in plan["requirements"] if not isinstance(item.get("asset"), dict)]
        if missing:
            paths["assets"].mkdir(parents=True, exist_ok=True)
            paths["handoff"].parent.mkdir(parents=True, exist_ok=True)
            projection = json.loads(context.read_text(encoding="utf-8"))["contract_projection"]
            generation_jobs = visual_sample_generation_jobs(plan, projection, missing)
            paths["handoff"].write_text(
                "\n".join(
                    [
                        "# 条件式视觉小样生产任务",
                        "",
                        "只生成计划中 fulfillment=supplemental_sample 且 asset=null 的小样；禁止生成正式分镜图片。",
                        f"- 小样计划：`{plan_path}`",
                        f"- 输出目录：`{paths['assets']}`",
                        "- 每张图只读取下方对应 sample_id 的 relevant_contract；完整合同仅作 Runtime 哈希绑定，不是要求一张小样同时展示全部内容。",
                        "- 仅当计划确实要求角色间尺度小样时，检查宽松的画面层级和透视可读性；儿童卡通允许为表演适度放大小角色，不按现实厘米比例机械处理。环境/动作参照不生成尺度小样。",
                        "- 不得擅自新增会成为跨镜头身份锚点的特殊标记、固定配饰、徽记，或违反合同/角色设定的非意图结构。",
                        "- 允许不违背合同的正常人体/动物结构、时代和场景合理的普通服饰及非身份性自然细节；这些推断细节不得升级为永久身份锚点。合同 required/forbidden 始终优先。",
                        "- 不得生成文字、标题、字幕、水印或 Logo。",
                        "- 每张只验证该 sample_id 的合同约束；不要扩展故事事实。",
                        "- style_anchor 同时作为风格与角色身份母版，不要再为每个角色分别画一张重复参考。",
                        "- 每个 state_anchor 必须是一张只呈现一个指定状态的独立图片；严禁把 F0～Fn 或所有状态挤在一张接触表里。",
                        "- strategy=targeted_regeneration 时只修审核指出的缺陷；strategy=single_state_single_asset 或 simplify_to_single_subject_contract_evidence 时必须减少同图约束并改变实现方法，禁止照搬上一轮提示词和版式。",
                        *( ["- 以下图片是用户确认的整体风格参考，必须用作 ImageGen 参考图，不复制具体角色：", *[f"  - `{path}`" for path in style_references]] if style_references else [] ),
                        "",
                        "## 身份扩展禁令",
                        "```json",
                        json.dumps(plan["identity_expansion_policy"], ensure_ascii=False, indent=2, sort_keys=True),
                        "```",
                        "",
                        "## 本轮逐图最小生成任务",
                        "```json",
                        json.dumps(generation_jobs, ensure_ascii=False, indent=2, sort_keys=True),
                        "```",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            sample_prompt = (
                "严格执行 handoff。使用 ImageGen 仅补齐其中列出的 supplemental_sample；"
                "不要修改合同、计划或正式故事图片。完成前逐文件确认可解码且路径精确。"
            )
            result = self._execute_image_generation(
                artifact_id="visual-samples-supplemental",
                operation="generate_visual_samples",
                stage="visual_samples",
                label="条件式视觉小样生成",
                handoff=paths["handoff"],
                prompt=sample_prompt,
                output_targets=tuple(
                    self.context.project_dir / item["expected_path"] for item in missing
                ),
                input_artifacts=(
                    {"role": "visual_sample_plan", "path": str(plan_path), "sha256": file_sha256(plan_path)},
                    {"role": "storyboard_contract_context", "path": str(context), "sha256": file_sha256(context)},
                    *(
                        {"role": "approved_style_reference", "path": str(path), "sha256": file_sha256(path)}
                        for path in style_references
                    ),
                ),
                attempt_id=f"visual-samples-attempt-{int(manifest.get('agent', {}).get('stages', {}).get('visual_samples', {}).get('attempts', 0))}",
            )
            if result.status != "done":
                return result
            try:
                plan_path = write_visual_sample_plan(self.context.project_dir, context, visual_design_port)
                plan = load_current_visual_sample_plan(self.context.project_dir, context, visual_design_port)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return StageResult("retrying", f"小样生成后无法重新绑定计划：{exc}", paths["handoff"])
        issues = visual_sample_machine_issues(self.context.project_dir, plan)
        qa = write_visual_sample_machine_qa(self.context.project_dir, plan)
        if issues:
            return StageResult("retrying", "视觉小样机器完整性未通过：" + "；".join(issues), qa)
        reused = sum(1 for item in plan["requirements"] if item["fulfillment"] == "contract_preview")
        supplemental = len(plan["requirements"]) - reused
        return StageResult(
            "done",
            f"条件式视觉小样已就绪：复用合同预览 {reused} 项，补充生产小样 {supplemental} 项。",
            qa,
        )

    def _stage_visual_sample_review(self, manifest: dict[str, Any]) -> StageResult:
        if self._legacy_contract_policy(manifest):
            return StageResult("done", "V3 冻结项目不补造 V3.5 视觉小样审核。")
        context = contract_consumer_path(self.context.project_dir, "storyboard_images")
        paths = visual_sample_paths(self.context.project_dir)
        paths["lock"].unlink(missing_ok=True)
        try:
            plan = load_current_visual_sample_plan(self.context.project_dir, context)
            machine_issues = visual_sample_machine_issues(self.context.project_dir, plan)
            machine_qa = write_visual_sample_machine_qa(self.context.project_dir, plan)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return StageResult("blocked", f"视觉小样或合同绑定无效：{exc}")
        if machine_issues:
            return StageResult("retrying", "视觉小样机器完整性未通过：" + "；".join(machine_issues), machine_qa)
        assets = visual_sample_asset_paths(self.context.project_dir, plan)
        style_references = self._approved_style_reference_paths()
        bundle = write_review_bundle(
            paths["bundle"],
            [paths["plan"], machine_qa, context, *style_references, *assets],
        )
        result, payload = self._structured_review(
            stage="visual_sample_review",
            label="条件式视觉小样独立审核",
            bundle=bundle,
            images=[*style_references, *assets],
            rubric=(
                "这是批量生图前门禁，审核必须分三层并在 JSON 中分别写 machine_completeness、contract_adherence、product_quality。"
                "machine_completeness 必须 passed=true 且引用文件/哈希证据；contract_adherence.checks 只覆盖各 sample_id 的 relevant_contract，不要求单张图或同一张总表展现完整状态序列；完整状态序列仍由正式 JSON 合同约束。"
                "product_quality.dimensions 必须逐项覆盖计划中的风格中性维度；有角色时覆盖 character_design_fit、identity_coherence、anatomical_coherence。"
                "如 product_quality 包含 scale_readability，逐 relationship_id 检查宽松画面层级、落点、前后景与透视是否清楚；儿童卡通为表演和可读性适度放大小角色是允许的，不使用现实厘米比例作为门禁。"
                f"{ANATOMICAL_COHERENCE_REVIEW_RULE}"
                "product_quality.style_contract 必须原样引用并逐条审核计划中 visual_style.style_profile 的 description、required_traits、forbidden_traits；整体判断角色设计与渲染是否真正符合该风格和已提供参考图，不得仅因画面明亮、安全、物种可辨就判定风格通过。"
                "style_contract 输出 description、description_fit=true、description_evidence，并分别用 required_traits[{trait,passed,evidence}] 和 forbidden_traits[{trait,absent,evidence}] 逐条举证。"
                "JSON 还必须写 p0_errors、retry_sample_ids 和逐 sample_id 的 evidence_matrix。"
                "任何擅自新增的身份定义性特殊标记、固定配饰、徽记、违反合同/角色设定的结构，或非意图性的多肢、缺肢、器官错位、结构崩坏及跨镜头身份锚点，均可构成 anatomy_or_organ_error 等 P0；"
                "不违背合同的正常结构、时代/场景合理普通服饰和非身份性自然细节不是 P0，但不得被升级为永久身份锚点。"
                "只要 p0_errors 非空就必须 approved=false，不能被总分平均。"
            ),
        )
        special_issues = visual_sample_review_payload_issues(payload, plan) if payload else ["missing review payload"]
        if result.status == "done" and not special_issues:
            lock = write_visual_sample_lock(self.context.project_dir)
            return StageResult("done", f"视觉小样三层审核通过并锁定：{payload.get('score')} 分", lock)
        request_payload: dict[str, Any] = {}
        can_retry = False
        if payload is not None:
            try:
                request = write_visual_sample_supplemental_request(self.context.project_dir, plan, payload)
                request_payload = json.loads(request.read_text(encoding="utf-8"))
                can_retry = self._can_retry_visual_sample_review(request_payload)
                retry_ids = set(request_payload.get("sample_ids", []))
                if can_retry:
                    quarantine = paths["directory"] / "rejected" / time.strftime("%Y%m%d-%H%M%S")
                    for item in plan["requirements"]:
                        if item["sample_id"] not in retry_ids or item["fulfillment"] != "supplemental_sample":
                            continue
                        source = self.context.project_dir / item["expected_path"]
                        if source.is_file():
                            quarantine.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(source), str(quarantine / source.name))
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                pass
        message = "视觉小样审核未通过"
        if special_issues:
            message += "：" + "；".join(special_issues)
        if can_retry:
            strategies = request_payload.get("generation_strategy_by_sample", {})
            return StageResult(
                "retrying",
                message + f"；只重做失败小样，并切换生成策略：{strategies}。",
                paths["review"],
            )
        return StageResult("blocked", message, paths["review"])

    def _stage_source_edit(self, manifest: dict[str, Any]) -> StageResult:
        contract = manifest.get("agent", {}).get("input_contract", {})
        if contract.get("mode") == "prepared_greenscreen_confirmed_text":
            errors = prepared_input_contract_errors(self.context.project_dir, manifest)
            if errors:
                return StageResult("blocked", "prepared 加速入口契约失效：" + "；".join(errors))
            return StageResult("done", "prepared 视频与人工确认文本哈希通过，跳过转写、自动剪口和重复还原。")
        inputs = manifest.get("inputs", {})
        video = first_existing(inputs.get("greenscreen_video_original"), inputs.get("greenscreen_video"))
        if video is None:
            return StageResult("blocked", "缺少绿幕原片，无法生成故事文本和清洁视频。")
        agent_defaults = load_config().get("agent_defaults", {})
        whisper_model = str(agent_defaults.get("whisper_model", "small"))
        working_max_width = int(agent_defaults.get("working_video_max_width", 0))
        proxy_max_width = int(agent_defaults.get("proxy_video_max_width", 1280))
        command = [
            resolve_agent_runtime_python(),
            str(ROOT / "source_video_pipeline.py"),
            "--project-dir",
            str(self.context.project_dir),
            "--video",
            str(video),
            "--whisper-model",
            whisper_model,
            "--language",
            "zh",
            "--working-max-width",
            str(max(0, working_max_width)),
            "--proxy-max-width",
            str(max(0, proxy_max_width)),
        ]
        model_dir = ROOT / "models" / "whisper"
        if model_dir.exists():
            command.extend(["--whisper-model-dir", str(model_dir)])
        color_lut = first_existing(inputs.get("color_lut"))
        if color_lut is not None:
            command.extend(["--lut", str(color_lut)])
        return self._run_command(command, "绿幕原片转写与可追溯自动剪辑", log_name="source_edit")

    def _stage_source_text_correction(self, manifest: dict[str, Any]) -> StageResult:
        decisions = first_existing(manifest.get("outputs", {}).get("source_edit_decisions"))
        if decisions is None:
            return StageResult("done", "用户提供了人工文本，跳过自动听辨校对。")
        source_dir = self.context.paths.status / "source_edit"
        raw_transcript = source_dir / "raw_transcript.json"
        if not raw_transcript.exists():
            return StageResult("blocked", "缺少原始转写 JSON，无法进行上下文校对。")
        correction = source_dir / "corrected_transcript.json"
        raw_sha = file_sha256(raw_transcript)
        original_video = first_existing(
            manifest.get("inputs", {}).get("greenscreen_video_original"),
            manifest.get("inputs", {}).get("greenscreen_video"),
        )
        clean_audio = first_existing(manifest.get("inputs", {}).get("extracted_narration"))
        prompt = "\n".join(
            [
                "你是中文故事听辨校对生产者。必须以原音为证据修正 ASR，不能只凭上下文把句子改顺。不要改变已有 segment 编号、时间范围或保留/删除决定。",
                f"必须实际听辨的绿幕原片：`{original_video}`",
                f"清洁音轨（若存在可优先逐段听）：`{clean_audio}`",
                f"原始 Whisper 转写：`{raw_transcript}`",
                f"当前剪辑决定和上下文：`{decisions}`",
                "逐段对照原音修正同音字、繁简体、他/她/它、的/地/得、专有名词、歌词和句尾。上下文只帮助定位疑点，不能充当听辨证据。必须逐字忠实，不得润色、浓缩、同义替换或补写连接句。",
                "对低置信度片段、歌唱、拟声词、重复感叹词、代词和结尾，必须截取对应音频复听；必要时使用另一个 ASR 模型交叉验证。仅因语义更通顺不得修改。",
                "检查相邻 ASR segment 之间的无字区：若原音中确有被 Whisper 整段漏掉的台词、歌声或拟声词，写入 insertions，给出精确原片 start/end/text 和 audio_evidence；insertions 不得与已有 segment 时间重叠。纯停顿、笑容或无声表演不要写成文字。",
                "完整保留开场、自我介绍、所有对白、引号、段落、重复感叹词、拟声词（如啊呜/哎哟）和表演性停顿；不确定时维持原转写并在 notes 标疑，禁止猜写。",
                "本系列固定品牌开场通常是“大家好，我是绵羊姐姐。”；当 Whisper 把低置信度的‘绵羊’听成‘明儿/绵阳’时应据此纠正，但若原音清楚显示不同说法则以原音为准。",
                f"把 JSON 写入：`{correction}`",
                "格式必须为 {\"raw_transcript_sha256\":\"...\",\"segments\":[{\"segment\":1,\"corrected_text\":\"...\",\"confidence\":0.0,\"evidence\":\"audio|asr-consensus|unchanged\",\"notes\":\"...\"}],\"insertions\":[{\"start\":1.2,\"end\":2.4,\"text\":\"原音确有但 ASR 漏掉的文字\",\"confidence\":0.0,\"audio_evidence\":\"听辨说明\"}]}。没有整段漏听时 insertions 写空数组。",
                f"raw_transcript_sha256 必须原样写为：{raw_sha}",
                "segments 必须覆盖原始转写里的每一个 segment；即使无需修改也要写出 corrected_text。",
            ]
        )
        result = self._codex_task(stage="source_text_correction", label="绿幕口播上下文校对", handoff=raw_transcript, prompt=prompt)
        if result.status != "done":
            return result
        try:
            payload = json.loads(correction.read_text(encoding="utf-8"))
            raw_payload = json.loads(raw_transcript.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return StageResult("blocked", f"上下文校对没有写入有效 JSON：{correction}", raw_transcript)
        expected_count = len(raw_payload.get("segments", []))
        corrected_segments = payload.get("segments", [])
        insertions = payload.get("insertions", [])
        insertions_valid = isinstance(insertions, list)
        if insertions_valid:
            for item in insertions:
                try:
                    valid_item = (
                        isinstance(item, dict)
                        and float(item.get("start", -1)) >= 0
                        and float(item.get("end", -1)) > float(item.get("start", -1))
                        and bool(str(item.get("text", "")).strip())
                        and bool(str(item.get("audio_evidence", "")).strip())
                    )
                except (TypeError, ValueError):
                    valid_item = False
                if not valid_item:
                    insertions_valid = False
                    break
        if (
            payload.get("raw_transcript_sha256") != raw_sha
            or not isinstance(corrected_segments, list)
            or len(corrected_segments) != expected_count
            or not insertions_valid
        ):
            return StageResult("blocked", f"上下文校对未覆盖全部 segment 或源哈希不匹配：{correction}", correction)
        original = first_existing(manifest.get("inputs", {}).get("greenscreen_video_original"))
        if original is None:
            return StageResult("blocked", "缺少绿幕原片，无法应用听辨校对。", correction)
        self._archive_source_edit_attempt(preserve_media=True)
        rerun_command = [
                sys.executable,
                str(ROOT / "source_video_pipeline.py"),
                "--project-dir",
                str(self.context.project_dir),
                "--video",
                str(original),
                "--transcript-json",
                str(raw_transcript),
                "--text-overrides-json",
                str(correction),
                "--no-render",
            ]
        color_lut = first_existing(manifest.get("inputs", {}).get("color_lut"))
        if color_lut is not None:
            rerun_command.extend(["--lut", str(color_lut)])
        rerun = self._run_command(
            rerun_command,
            "应用上下文校对并重建清洁文本/字幕/绿幕视频",
            log_name="source_text_correction",
        )
        if rerun.status != "done":
            return rerun
        refreshed = self._manifest()
        refreshed.setdefault("outputs", {})["corrected_transcript"] = str(correction)
        from story_project import write_manifest

        write_manifest(self.context.paths, refreshed)
        return StageResult("done", "已应用逐 segment 上下文校对，时间轴和删改决定保持不变。", correction)

    def _stage_source_edit_review(self, manifest: dict[str, Any]) -> StageResult:
        decisions = first_existing(manifest.get("outputs", {}).get("source_edit_decisions"))
        story_text = first_existing(manifest.get("inputs", {}).get("story_text"))
        if decisions is None or story_text is None:
            return StageResult("blocked", "缺少自动剪辑决定或清洁文本，无法独立审核。")
        source_qa_path = self.context.paths.status / "qa_source_report.json"
        if not self._hashed_qa_report_passes(source_qa_path):
            return StageResult("blocked", f"源视频机器 QA 缺失、未通过或媒体哈希已变化：{source_qa_path}", source_qa_path)
        review_path = self.context.paths.status / "source_edit" / "source_edit_review.json"
        artifact_sha = file_sha256(decisions)
        try:
            decision_payload = json.loads(decisions.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            decision_payload = {}
        rhythm_guard_passed = bool(decision_payload.get("rhythm_guard", {}).get("passed", False))
        corrected_transcript = self.context.paths.status / "source_edit" / "corrected_transcript.json"
        corrected_transcript_line = (
            f"经上下文校对且已绑定哈希的转写：`{corrected_transcript}`"
            if corrected_transcript.exists()
            else "本项目没有上下文校对转写；文本准确性以原始转写为基线。"
        )
        prompt = "\n".join(
            [
                "你是独立文本剪辑审核员。不要延续生产者的判断，也不要修改任何生产文件。",
                f"原始转写：`{self.context.paths.status / 'source_edit' / 'raw_transcript.json'}`",
                corrected_transcript_line,
                f"自动剪辑决定：`{decisions}`",
                f"清洁分镜文本：`{story_text}`",
                f"源视频/决策/LUT/清洁媒体机器 QA：`{source_qa_path}`",
                f"必须实际抽听核对的原片：`{first_existing(manifest.get('inputs', {}).get('greenscreen_video_original'))}`",
                f"可用于逐段复听的清洁音轨：`{first_existing(manifest.get('inputs', {}).get('extracted_narration'))}`",
                "检查是否误删语义、拟声词、自然呼吸、角色切换停顿或句尾；是否仍保留失败口误/重复；只允许删除可确认的失败重录。",
                "不能只比 JSON 文本：必须抽听所有低置信度、歌词、拟声词、代词、句尾，以及相邻 ASR segment 之间的无字区。发现原音存在但文字缺失、文字与原音不符或出现无音频依据的顺写/润色，均为关键错误。",
                "读取 edit_decisions.json 的 rhythm_guard：若 cut_count 超阈值、删除了表演词，或形成密集跳切，approved 必须为 false，并要求恢复最小必要片段。",
                "审核必须区分‘剪辑删改’与‘ASR 听辨纠错’：若 corrected_transcript.json 存在、其 raw_transcript_sha256 正确，且 edit_decisions.json 的 text_overrides_source_sha256 与它匹配，则 segment 文本应与 corrected_text/insertions 比对；但哈希匹配不代表听辨正确，仍须以原音独立核验。",
                "原始转写用于核对 segment 数量、时间范围和保留/删除决定；经哈希绑定的校对转写用于核对文字。逐字检查开场、自我介绍、对白引号、重复感叹词和结尾最后一个字；真正的缺句、无依据补写、润色或残句仍是关键错误。",
                "评分必须为 0–100；总分低于 85 或存在语义误删、故事缺段等关键错误时 approved 必须为 false。",
                f"把 JSON 写入：`{review_path}`",
                "JSON 必须包含 approved、score、critical_errors、issues、retry_instructions、artifact_sha256。",
                f"artifact_sha256 必须原样写为：{artifact_sha}",
                "只做审核，不要读取任何生产者推理或旧审核结论。",
            ]
        )
        payload: dict[str, Any] | None = None
        try:
            existing_review = json.loads(review_path.read_text(encoding="utf-8"))
            if isinstance(existing_review, dict) and existing_review.get("artifact_sha256") == artifact_sha:
                payload = existing_review
        except (OSError, json.JSONDecodeError):
            pass
        if payload is None:
            result = self._codex_task(stage="source_edit_review", label="独立文本剪辑审核", handoff=decisions, prompt=prompt)
            if result.status != "done":
                return result
            try:
                loaded_review = json.loads(review_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return StageResult("blocked", f"独立审核未写入有效 JSON：{review_path}", decisions)
            if not isinstance(loaded_review, dict):
                return StageResult("blocked", f"独立审核 JSON 顶层必须是对象：{review_path}", decisions)
            payload = loaded_review
        quality_attempts = self._source_edit_quality_attempts(review_path)
        if not review_passes(payload, artifact=decisions) or not rhythm_guard_passed:
            if self._can_retry_stage("source_edit_review", critical=True, attempts_override=quality_attempts):
                revision_path = self.context.paths.status / "source_edit" / "source_edit_keep_overrides.json"
                revision_delta_path = self.context.paths.status / "source_edit" / "source_edit_keep_overrides_delta.json"
                revision_prompt = "\n".join(
                    [
                        "你是文本剪辑修订生产者。根据独立审核意见提出最小 segment 保留/删除修正，不要自行改写故事。",
                        f"原始转写：`{self.context.paths.status / 'source_edit' / 'raw_transcript.json'}`",
                        f"当前剪辑决定：`{decisions}`",
                        corrected_transcript_line,
                        f"独立审核：`{review_path}`",
                        f"已有累计覆盖（只读参考，可能不存在）：`{revision_path}`",
                        f"只把本轮新增或替换的覆盖 JSON 写入：`{revision_delta_path}`",
                        "格式：{\"overrides\":[{\"segment\":1,\"keep\":true,\"trim_start\":1.23,\"trim_end\":4.56,\"text\":\"精确区间对应的清洁文本\",\"reason\":\"...\"}]}。",
                        "trim_start/trim_end 为可选原片绝对秒数，只能落在该 segment 原始区间内；用于保留半段、删除半段或去掉段首语气词。",
                        "使用精确区间时必须给出与区间严格对应的 text，不能包含已经裁掉的词。整段保留/删除时可省略 trim_start、trim_end、text。",
                        "只列出必须改变的 segment；不得用改写文本掩盖误删或口误。",
                        "keep-overrides 只负责剪辑区间和保留/删除，不能撤销或替换 corrected_transcript.json 中的 ASR 纠错；不得把‘小老虎’等已校对文字恢复成 Whisper 的‘小老鼠’等错字。若审核意见只涉及听辨文本而不涉及剪辑区间，不要伪造 segment 剪辑覆盖。",
                    ]
                )
                revision = self._codex_task(
                    stage=f"source_edit_revision_{int(self._manifest().get('agent', {}).get('stages', {}).get('source_edit_review', {}).get('attempts', 1))}",
                    label="文本剪辑自动修订",
                    handoff=review_path,
                    prompt=revision_prompt,
                )
                if revision.status != "done":
                    return revision
                try:
                    revision_payload = json.loads(revision_delta_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    return StageResult("blocked", f"文本修订未写入有效增量覆盖 JSON：{revision_delta_path}", review_path)
                if not isinstance(revision_payload.get("overrides"), list) or not revision_payload["overrides"]:
                    return StageResult("blocked", f"审核未通过且修订器没有给出可执行 segment 覆盖：{revision_delta_path}", review_path)
                self._merge_source_edit_overrides(revision_path, revision_delta_path)
                original = first_existing(manifest.get("inputs", {}).get("greenscreen_video_original"))
                raw_transcript = self.context.paths.status / "source_edit" / "raw_transcript.json"
                if original is None or not raw_transcript.exists():
                    return StageResult("blocked", "缺少原片或原始转写，无法执行文本剪辑自动修订。", review_path)
                self._archive_source_edit_attempt()
                rerun_command = [
                        sys.executable,
                        str(ROOT / "source_video_pipeline.py"),
                        "--project-dir",
                        str(self.context.project_dir),
                        "--video",
                        str(original),
                        "--transcript-json",
                        str(raw_transcript),
                        "--keep-overrides-json",
                        str(revision_path),
                        "--working-max-width",
                        str(max(0, int(load_config().get("agent_defaults", {}).get("working_video_max_width", 0)))),
                        "--proxy-max-width",
                        str(max(0, int(load_config().get("agent_defaults", {}).get("proxy_video_max_width", 1280)))),
                    ] + (["--text-overrides-json", str(self.context.paths.status / "source_edit" / "corrected_transcript.json")] if (self.context.paths.status / "source_edit" / "corrected_transcript.json").exists() else [])
                color_lut = first_existing(manifest.get("inputs", {}).get("color_lut"))
                if color_lut is not None:
                    rerun_command.extend(["--lut", str(color_lut)])
                rerun = self._run_command(
                    rerun_command,
                    "按独立审核意见重新剪辑绿幕原片",
                    log_name="source_edit_retry",
                )
                if rerun.status != "done":
                    return rerun
                return StageResult("retrying", "已按独立审核意见重新剪辑，将使用新上下文再次审核。", revision_path)
            return StageResult("blocked", f"文本剪辑审核未通过（{payload.get('score', 0)} 分）：{review_path}", review_path)
        return StageResult("done", f"文本剪辑独立审核通过：{payload.get('score')} 分", review_path)

    def _source_edit_quality_attempts(self, current_review: Path) -> int:
        """Count distinct reviewed decision hashes, excluding infrastructure-only retries."""
        candidates = [current_review]
        rejected_root = self.context.paths.status / "rejected" / "source_edit"
        if rejected_root.exists():
            candidates.extend(rejected_root.glob("*/source_edit_review.json"))
        reviewed_hashes: set[str] = set()
        for candidate in candidates:
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            artifact_sha = str(payload.get("artifact_sha256", "")) if isinstance(payload, dict) else ""
            if len(artifact_sha) == 64:
                reviewed_hashes.add(artifact_sha)
        return len(reviewed_hashes)

    def _merge_source_edit_overrides(self, cumulative_path: Path, delta_path: Path) -> dict[str, Any]:
        """Merge incremental review fixes without reviving earlier rejected segments."""
        cumulative: dict[str, Any] = {"overrides": []}
        if cumulative_path.exists():
            try:
                loaded = json.loads(cumulative_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("overrides"), list):
                    cumulative = loaded
            except (OSError, json.JSONDecodeError):
                pass
        delta = json.loads(delta_path.read_text(encoding="utf-8"))
        by_segment: dict[int, dict[str, Any]] = {}
        for source in (cumulative.get("overrides", []), delta.get("overrides", [])):
            for item in source:
                if not isinstance(item, dict):
                    continue
                try:
                    segment = int(item["segment"])
                except (KeyError, TypeError, ValueError):
                    continue
                by_segment[segment] = dict(item)
                by_segment[segment]["segment"] = segment
        if not by_segment:
            raise ValueError("增量修订合并后没有有效 segment 覆盖")
        merged = {"version": 2, "merge_policy": "latest_delta_wins_by_segment", "overrides": [by_segment[key] for key in sorted(by_segment)]}
        cumulative_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return merged

    def _stage_codex_story_images(self, manifest: dict[str, Any]) -> StageResult:
        if not self._legacy_contract_policy(manifest):
            semantic_source = self._artifact_semantic_source(manifest)
            if semantic_source is None or not artifact_semantic_plan_is_current(
                self.context.project_dir, semantic_source
            ):
                return StageResult("blocked", "逐产物语义呈现计划缺失或已过期，禁止开始批量生图。")
            sample_context = contract_consumer_path(self.context.project_dir, "storyboard_images")
            if not visual_sample_lock_is_current(self.context.project_dir, sample_context):
                return StageResult("blocked", "条件式视觉小样尚未通过三层独立审核或锁已失效，禁止开始批量生图。")
        contract_context = self._prepare_contract_consumer(manifest, "storyboard_images")
        if isinstance(contract_context, StageResult):
            return contract_context
        staging = self._codex_stage_dir("codex_story_images")
        staging_images = staging / "images"
        staging_storyboard = staging / f"{self.context.slug}_storyboard_lines.txt"
        staging_images.mkdir(parents=True, exist_ok=True)
        story_lines = self._story_lines(manifest)
        if not story_lines:
            return StageResult("blocked", "缺少可用故事镜头行，无法生成故事图片。")
        storyboard_changed = self._ensure_storyboard_from_lines(staging_storyboard, story_lines)
        if storyboard_changed:
            self._invalidate_story_image_derivatives(staging_images)
        authoritative_storyboard_sha = file_sha256(staging_storyboard)
        handoff = self._write_story_image_handoff(manifest, staging_images=staging_images, staging_storyboard=staging_storyboard)
        if contract_context is not None:
            self._append_contract_handoff(
                handoff,
                contract_context,
                "分镜/生图的产物语义、视觉风格、角色身份、世界尺度与故事状态",
            )
        if (
            contract_context is not None
            and not self._story_image_generation_context_current(contract_context, staging_storyboard)
        ):
            final_images = self.context.paths.images / "images"
            has_unbound_images = any(
                directory.exists()
                and any(directory.glob(f"{self.context.slug}_scene_*.png"))
                for directory in (staging_images, final_images)
            )
            if has_unbound_images:
                self._invalidate_story_image_derivatives(staging_images)
        self._sync_story_images_from_staging(staging, staging_storyboard, staging_images)
        missing = self._missing_story_image_indices(story_lines)
        if not missing:
            if contract_context is not None and not self._story_image_generation_complete(
                manifest, contract_context, staging_storyboard
            ):
                return StageResult("retrying", "现有故事图片缺少当前合同/视觉小样的生产血缘，必须重新生成。", handoff)
            self._complete_contract_consumer(manifest, "storyboard_images")
            return StageResult("done", f"故事图片已完整：{len(story_lines)}/{len(story_lines)} 张。", handoff)

        batch_size = max(1, self.context.codex_story_image_batch_size)
        batch = missing[:batch_size]
        batch_prompt = self._story_images_batch_prompt(
            handoff=handoff,
            staging_images=staging_images,
            staging_storyboard=staging_storyboard,
            story_lines=story_lines,
            indices=batch,
        )
        result = self._execute_image_generation(
            artifact_id=f"story-images-{batch[0]:02d}-{batch[-1]:02d}",
            operation="generate_story_images",
            stage=f"codex_story_images_{batch[0]:02d}_{batch[-1]:02d}",
            label=f"Codex 原生故事批量出图 {len(batch)} 张",
            handoff=handoff,
            prompt=batch_prompt,
            output_targets=tuple(
                staging_images / self._story_image_filename(index) for index in batch
            ),
            input_artifacts=(
                {
                    "role": "authoritative_storyboard",
                    "path": str(staging_storyboard),
                    "sha256": authoritative_storyboard_sha,
                },
                *(
                    (
                        {
                            "role": "storyboard_contract_context",
                            "path": str(contract_context),
                            "sha256": file_sha256(contract_context),
                        },
                        {
                            "role": "visual_sample_lock",
                            "path": str(visual_sample_paths(self.context.project_dir)["lock"]),
                            "sha256": file_sha256(visual_sample_paths(self.context.project_dir)["lock"]),
                        },
                    )
                    if contract_context is not None
                    else ()
                ),
            ),
            attempt_id=f"codex-story-images-attempt-{int(manifest.get('agent', {}).get('stages', {}).get('codex_story_images', {}).get('attempts', 0))}",
        )
        if result.status != "done":
            for index in batch:
                self._record_story_image_status(index, "failed", result.message)
            return result
        if not staging_storyboard.exists() or file_sha256(staging_storyboard) != authoritative_storyboard_sha:
            self._ensure_storyboard_from_lines(staging_storyboard, story_lines)
            quarantine = self._invalidate_story_image_derivatives(staging_images)
            return StageResult(
                "retrying",
                f"图片生产者改写了只读权威分镜；本批已归档并恢复原 SHA：{quarantine}",
                handoff,
            )
        self._sync_story_images_from_staging(staging, staging_storyboard, staging_images)
        if contract_context is not None:
            self._write_story_image_generation_manifest(
                contract_context,
                staging_storyboard,
                story_lines,
            )
        completed = []
        for index in batch:
            target = self.context.paths.images / "images" / self._story_image_filename(index)
            if target.exists():
                self._record_story_image_status(index, "done", str(target))
                completed.append(index)
            else:
                self._record_story_image_status(index, "missing_after_batch", str(target))
        if not completed:
            return StageResult("blocked", f"Codex CLI 批量子任务已返回，但本批 {len(batch)} 张图片都没有落盘。", handoff)

        if not self._has_story_visual_control():
            return StageResult(
                "retrying",
                "图片已落盘，但视觉圣经/风格锚点/机器可读分镜计划不完整或未逐行绑定权威分镜，将在下一轮补齐后再放行。",
                handoff,
            )

        manifest = self._manifest()
        if self._has_story_image_files(manifest) and (
            contract_context is None
            or self._story_image_generation_complete(manifest, contract_context, staging_storyboard)
        ):
            self._complete_contract_consumer(manifest, "storyboard_images")
            return StageResult("done", f"故事图片已完整生成：{len(story_lines)}/{len(story_lines)} 张。", handoff)
        image_dir = self._image_dir()
        current = self._expected_named_story_image_count(image_dir, manifest, self._storyboard_path(manifest))
        return StageResult("retrying", f"已逐张生成 {len(completed)} 张，本阶段进度：{current}/{len(story_lines)}。将在下一批继续。", handoff)

    def _stage_prepare_jobs(self, manifest: dict[str, Any]) -> StageResult:
        storyboard = self._storyboard_path(manifest)
        image_dir = self._image_dir()
        if storyboard is None or image_dir is None:
            return StageResult("blocked", "缺少分镜文本或图片目录，无法准备图生视频任务。")
        contract_context = self._prepare_contract_consumer(manifest, "image_video")
        if isinstance(contract_context, StageResult):
            return contract_context
        continuity_contract = self._visual_continuity_contract_path()
        storyboard_plan = self.context.paths.images / f"{self.context.slug}_storyboard_plan.json"
        if continuity_contract is not None:
            continuity_errors = self._visual_continuity_storyboard_errors(continuity_contract, storyboard_plan)
            if continuity_errors:
                return StageResult(
                    "blocked",
                    "视觉连续性合同校验失败，未准备/付费：" + "；".join(continuity_errors),
                    continuity_contract,
                )
        command = [
            "prepare",
            "--image-dir",
            str(image_dir),
            "--storyboard",
            str(storyboard),
            "--output-dir",
            str(self.context.paths.video_jobs),
            "--slug",
            self.context.slug,
            "--short-slug",
            short_slug(self.context.slug),
        ]
        if continuity_contract is not None:
            command.extend(["--continuity-contract", str(continuity_contract)])
        if storyboard_plan.is_file():
            command.extend(["--storyboard-plan", str(storyboard_plan)])
        if contract_context is not None:
            command.extend(["--story-contract-context", str(contract_context)])
        result = self._workflow(command, "准备图生视频任务")
        return result

    def _stage_story_images_review(self, manifest: dict[str, Any]) -> StageResult:
        image_dir = self._image_dir()
        storyboard = self._storyboard_path(manifest)
        if image_dir is None or storyboard is None:
            return StageResult("blocked", "缺少故事图片或分镜文本，无法独立审核。")
        story_lines = [line for line in storyboard.read_text(encoding="utf-8-sig", errors="ignore").splitlines() if line.strip()]
        scene_images = [image_dir / self._story_image_filename(index) for index in range(1, len(story_lines) + 1)]
        missing = [path.name for path in scene_images if not path.is_file()]
        if missing:
            return StageResult("blocked", f"故事图片独立审核缺少目标文件：{', '.join(missing)}")
        contact_sheets: list[Path] = []
        for offset in range(0, len(scene_images), 6):
            group = scene_images[offset : offset + 6]
            contact_sheets.append(
                self._make_contact_sheet(
                    group,
                    self.context.paths.status / "reviews" / f"story_images_contact_sheet_{offset + 1:02d}_{offset + len(group):02d}.jpg",
                    columns=3,
                )
            )
        control_files = [
            self.context.paths.images / f"{self.context.slug}_visual_bible.md",
            self.context.paths.images / f"{self.context.slug}_storyboard_plan.json",
        ]
        semantic_contract = first_existing(manifest.get("inputs", {}).get("story_semantics"))
        if semantic_contract is not None:
            control_files.append(semantic_contract)
        continuity_contract = self._visual_continuity_contract_path()
        if continuity_contract is not None:
            control_files.append(continuity_contract)
        storyboard_plan = control_files[1]
        continuity_errors = self._visual_continuity_storyboard_errors(continuity_contract, storyboard_plan)
        if continuity_errors:
            return StageResult("blocked", "视觉连续性合同校验失败（关键错误）：" + "；".join(continuity_errors), continuity_contract)
        bundle = write_review_bundle(
            self.context.paths.status / "reviews" / "story_images_bundle.json",
            [
                storyboard,
                *(path for path in control_files if path.exists()),
                *(path for path in [contract_consumer_path(self.context.project_dir, "storyboard_images")] if path.exists()),
                *scene_images,
            ],
        )
        required_quality_dimensions: list[str] = []
        if not self._legacy_contract_policy(manifest):
            try:
                sample_plan = load_current_visual_sample_plan(
                    self.context.project_dir,
                    contract_consumer_path(self.context.project_dir, "storyboard_images"),
                )
                required_quality_dimensions = [
                    str(value)
                    for value in sample_plan.get("review_profile", {}).get("product_quality", [])
                    if str(value)
                ]
            except (OSError, ValueError, KeyError, TypeError):
                required_quality_dimensions = []
        result, payload = self._structured_review(
            stage="story_images_review",
            label="故事图片独立审核",
            bundle=bundle,
            images=contact_sheets,
            rubric=(
                "逐镜头对照分镜检查角色和服装一致性、故事语义、构图和相邻连续性；按当前合同的物种/身份、角色定义、visual_style 和本镜头设计检查肢体、五官、器官及整体结构是否内部一致。"
                "还要检查物种/颜色身份、该镜头应出现与明确不应出现的角色，以及角色是否提前知道尚未发生的信息。"
                "逐镜核对 storyboard_plan.json：每个唱歌/关键发言/关键动作/受挫反应角色是否有自己的焦点镜头；连续段是否有建立镜头、表演者中近景和反应镜头，而不是全程同一种双人中景。"
                "按 appearance_id 逐项比较脸部花纹、服装主色/款式和饰品；虎妈妈等跨镜角色无剧情依据换衣服属于关键连续性错误。"
                "若 bundle 包含 story_semantics.json，必须按其语义分段检查：主持人开场/故事预告不得被生成为第二个片名镜头，片名只能出现一次。"
                "若 bundle 包含 visual_continuity_contract.json，必须将其作为逐镜硬约束：按 allowed_states、transitions、story_boundaries 和 state_rules 核对每镜当前状态、事件转折与在场证据；状态越界或违反 required/forbidden 均为 critical_errors，不能用整体印象放行。"
                "若合同的 storyboard_requirements 指定 required_field，机器可读 storyboard_plan 必须逐镜提供该字段且值必须属于合同 allowed_states；缺失或枚举无效是关键错误。"
                "审核 JSON 的 evidence_matrix 必须逐镜写明：角色数量、身份/服装、关键物体数量、角色应在场/不应在场及画面证据；不得用“整体正常”代替逐项核对。"
                "V3.5 required_v1 项目还必须分层写 contract_adherence 和 product_quality，并写 p0_errors。"
                "contract_adherence 必须包含 passed=true 和非空 evidence。"
                "product_quality 必须包含 passed=true 和 dimensions 数组；dimensions 每项必须是"
                "{dimension,passed,score,evidence}，不得改写成以维度名为键的对象。"
                f"本项目 dimensions 必须逐项覆盖：{', '.join(required_quality_dimensions)}。"
                f"{ANATOMICAL_COHERENCE_REVIEW_RULE}"
                "product_quality.style_contract 必须原样逐条审核当前视觉小样计划中的风格 description、required_traits、forbidden_traits；整体判断角色设计与渲染是否真正符合该风格和已通过参考图，不得仅因画面明亮、安全、物种可辨就判定风格通过。"
                "style_contract 输出 description、description_fit=true、description_evidence，并分别用 required_traits[{trait,passed,evidence}] 和 forbidden_traits[{trait,absent,evidence}] 逐条举证。"
                "擅自新增身份定义性特殊标记、固定配饰、徽记、违反合同/角色设定的结构，或非意图性的多肢、缺肢、器官错位、结构崩坏及跨镜头身份锚点，均可构成 anatomy_or_organ_error 等 P0；符合合同的非写实结构本身不是 P0。身份错、尺度或状态矛盾、儿童不适、不可用构图仍为 P0；普通合理服饰和非身份自然细节不自动构成 P0。P0 非空时无论总分多高都不得通过。"
                "输出 retry_indices（需要重做的镜头编号整数数组）。角色身份或在场关系错、肢体/五官崩坏、错误文字、漏镜头属于关键错误。"
            ),
        )
        if result.status == "done":
            if self._legacy_contract_policy(manifest):
                return result
            quality_issues = self._story_image_quality_review_issues(payload)
            if not quality_issues:
                return result
            result = StageResult("blocked", "故事图片产品质量/P0 门禁未通过：" + "；".join(quality_issues), result.handoff)
        if payload and self._can_retry_stage("story_images_review", critical=True):
            indices = self._review_retry_indices(payload)
            if indices:
                moved = self._quarantine_story_images(indices)
                if moved:
                    return StageResult("retrying", f"图片审核未通过，已保留失败版本并排队重做镜头：{', '.join(map(str, moved))}", result.handoff)
        return result

    def _story_image_quality_review_issues(self, payload: dict[str, Any] | None) -> list[str]:
        if not isinstance(payload, dict):
            return ["missing review payload"]
        issues: list[str] = []
        p0 = payload.get("p0_errors")
        if not isinstance(p0, list):
            issues.append("p0_errors must be an array")
        elif p0:
            issues.append("P0 hard gate failed: " + ",".join(str(item) for item in p0))
        adherence = payload.get("contract_adherence")
        if not isinstance(adherence, dict) or adherence.get("passed") is not True or not adherence.get("evidence"):
            issues.append("contract_adherence must pass with evidence")
        context = contract_consumer_path(self.context.project_dir, "storyboard_images")
        try:
            plan = load_current_visual_sample_plan(self.context.project_dir, context)
        except (OSError, ValueError, KeyError, TypeError):
            issues.append("visual sample quality profile unreadable")
        else:
            issues.extend(product_quality_review_issues(payload.get("product_quality"), plan["review_profile"]))
        return issues

    def _stage_timing(self, manifest: dict[str, Any]) -> StageResult:
        narration = first_existing(manifest.get("inputs", {}).get("narration"), manifest.get("inputs", {}).get("extracted_narration"))
        jobs = self._jobs_csv(manifest)
        if narration is None or jobs is None:
            return StageResult("blocked", "缺少旁白或 jobs CSV，无法写入时长。")
        command = ["timing", "--jobs-csv", str(jobs), "--narration", str(narration), "--whisper-model", "base", "--language", "zh"]
        model_dir = ROOT / "models" / "whisper"
        if model_dir.exists():
            command.extend(["--whisper-model-dir", str(model_dir)])
        # Timing strategy follows the selected provider adapter.  Only the
        # Provider adapters advertise whole-second, per-second
        # generation bounds; legacy providers retain fixed-duration behavior.
        try:
            provider = self._modules().video_generator()
        except Exception:
            provider = None
        supported = provider.capabilities.supported if provider is not None else {}
        min_seconds = supported.get("min_duration")
        max_seconds = supported.get("max_duration")
        duration_choices = tuple(supported.get("duration_choices") or ())
        if provider is not None and min_seconds is not None and max_seconds is not None:
            command.extend(
                [
                    "--duration-mode",
                    "adaptive-seconds",
                    "--min-generation-seconds",
                    str(int(min_seconds)),
                    "--max-generation-seconds",
                    str(int(max_seconds)),
                ]
            )
            if duration_choices:
                command.extend(
                    [
                        "--generation-duration-choices",
                        ",".join(f"{float(item):g}" for item in duration_choices),
                    ]
                )
        return self._workflow(command, "写入旁白时长")

    def _stage_generate_videos(self, manifest: dict[str, Any]) -> StageResult:
        jobs = self._jobs_csv(manifest)
        image_dir = self._image_dir()
        if jobs is None or image_dir is None:
            return StageResult("blocked", "缺少 jobs CSV 或图片目录，无法生成视频片段。")
        if not self._legacy_contract_policy(manifest) and not contract_consumer_context_is_current(self.context.project_dir, "image_video"):
            return StageResult("blocked", "image_video 合同请求清单已失效，未发起付费调用。")
        continuity_errors = validate_image_video_jobs(jobs)
        if continuity_errors:
            return StageResult(
                "blocked",
                "视觉连续性合同/任务校验失败，未发起付费调用：" + "；".join(continuity_errors),
                jobs,
            )
        videos_dir = self.context.paths.video_jobs / "videos"
        actual = len(list(videos_dir.glob("*.mp4"))) if videos_dir.exists() else 0
        pending = max(0, self._job_count(jobs) - actual)
        config = load_config()
        video_api = config.get("video_api", {}) if isinstance(config.get("video_api"), dict) else {}
        provider = self._modules().video_generator()
        try:
            with jobs.open(encoding="utf-8-sig", newline="") as file:
                job_rows = list(csv.DictReader(file))
        except (OSError, csv.Error):
            job_rows = []
        before_targets = {
            str(row.get("target_video_filename") or "").strip()
            for row in job_rows
            if str(row.get("target_video_filename") or "").strip()
            and (videos_dir / str(row.get("target_video_filename") or "").strip()).is_file()
        }
        pending_rows = [
            row
            for row in job_rows
            if str(row.get("target_video_filename") or "").strip() not in before_targets
            and str(row.get("status") or "").strip() not in {"downloaded", "approved"}
        ]
        quality_provider_overrides = {
            str(row.get("video_quality_provider_override") or "").strip()
            for row in pending_rows
            if str(row.get("video_quality_provider_override") or "").strip()
        }
        if len(quality_provider_overrides) > 1:
            return StageResult("blocked", "同一生成批次出现多个质量恢复 provider，无法可靠计费和提交。", jobs)
        quality_provider_override = next(iter(quality_provider_overrides), "")
        if quality_provider_override and quality_provider_override != provider.identity.adapter_name:
            try:
                quality_registry = build_registry_for_profile(
                    self._modules().selection_profile(),
                    config,
                    ROOT,
                    video_provider_override=quality_provider_override,
                    execution_mode=self._modules().selection_execution_mode(),
                )
                provider = quality_registry.video_generator()
            except Exception as exc:
                return StageResult("blocked", f"视频质量恢复 provider 配置不可用：{exc}", jobs)
        supported = provider.capabilities.supported
        default_seconds = supported.get("default_duration") or 0
        estimated_per_clip = provider.estimate_cost(float(default_seconds or 0))

        def estimate_row_cost(row: dict[str, str]) -> float:
            seconds = float(provider.resolve_request_seconds(row, default_seconds or 0))
            return provider.estimate_cost(seconds)

        estimated_rows = pending_rows or [{"duration": str(default_seconds or 0)} for _ in range(pending)]
        estimate = round(sum(estimate_row_cost(row) for row in estimated_rows), 2)
        ledger = BudgetLedger(manifest)
        try:
            reservation = ledger.authorize(estimate, label=f"图生视频 {pending} 个镜头", critical=True)
        except BudgetExceeded as exc:
            return StageResult("blocked", str(exc))
        from story_project import write_manifest

        write_manifest(self.context.paths, manifest)
        write_internal_agent_reports(self.context.project_dir)
        generate_command = [
            "generate",
            "--jobs-csv", str(jobs),
            "--images-dir", str(image_dir),
            "--videos-dir", str(self.context.paths.video_jobs / "videos"),
            "--project-dir", str(self.context.project_dir),
            "--execution-mode", "test" if self._modules().selection_profile() == "mock-video" else "production",
        ]
        if quality_provider_override:
            generate_command.extend(["--provider", quality_provider_override])
        if bool(video_api.get("submit_all_first", False)):
            generate_command.append("--submit-all-first")
            generate_command.extend(["--max-submit-first", str(int(video_api.get("max_submit_first", 20)))])
        result = self._workflow(generate_command, "调用图生视频 API 生成片段")
        manifest = self._manifest()
        ledger = BudgetLedger(manifest)
        after = len(list(videos_dir.glob("*.mp4"))) if videos_dir.exists() else 0
        generated_by_api = max(0, after - actual)
        if result.status == "done" or generated_by_api > 0:
            try:
                with jobs.open(encoding="utf-8-sig", newline="") as file:
                    settled_rows = list(csv.DictReader(file))
            except (OSError, csv.Error):
                settled_rows = []
            newly_generated = [
                row
                for row in settled_rows
                if str(row.get("target_video_filename") or "").strip()
                and str(row.get("target_video_filename") or "").strip() not in before_targets
                and (videos_dir / str(row.get("target_video_filename") or "").strip()).is_file()
            ]
            actual_cost = round(sum(estimate_row_cost(row) for row in newly_generated), 2)
            if actual_cost <= 0 and generated_by_api > 0:
                actual_cost = round(generated_by_api * estimated_per_clip, 2)
            ledger.settle(
                reservation,
                actual_cost,
                provider=provider.capabilities.provider or provider.identity.adapter_name,
            )
        else:
            ledger.release(reservation, reason=result.message)
        write_manifest(self.context.paths, manifest)
        write_internal_agent_reports(self.context.project_dir)
        if result.status == "done" and self._has_generated_video_files(manifest):
            self._complete_contract_consumer(manifest, "image_video")
        fallback_provider_name = str(video_api.get("fallback_provider", "")).strip()
        if (
            result.status != "done"
            and fallback_provider_name
            and fallback_provider_name.lower() != "browser"
            and fallback_provider_name != provider.identity.adapter_name
        ):
            try:
                fallback_registry = build_registry_for_profile(
                    self._modules().selection_profile(),
                    config,
                    ROOT,
                    video_provider_override=fallback_provider_name,
                    execution_mode=self._modules().selection_execution_mode(),
                )
                fallback_provider = fallback_registry.video_generator()
            except Exception as exc:
                return StageResult("blocked", f"图生视频备用 provider 配置不可用：{exc}", jobs)

            try:
                with jobs.open(encoding="utf-8-sig", newline="") as file:
                    fallback_rows = list(csv.DictReader(file))
            except (OSError, csv.Error):
                fallback_rows = []
            fallback_before_targets = {
                str(row.get("target_video_filename") or "").strip()
                for row in fallback_rows
                if str(row.get("target_video_filename") or "").strip()
                and (videos_dir / str(row.get("target_video_filename") or "").strip()).is_file()
            }
            remaining_rows = [
                row
                for row in fallback_rows
                if str(row.get("target_video_filename") or "").strip()
                and str(row.get("target_video_filename") or "").strip() not in fallback_before_targets
            ]
            if remaining_rows:
                fallback_supported = fallback_provider.capabilities.supported
                fallback_default_seconds = fallback_supported.get("default_duration") or 0

                def estimate_fallback_row_cost(row: dict[str, str]) -> float:
                    seconds = float(
                        fallback_provider.resolve_request_seconds(row, fallback_default_seconds or 0)
                    )
                    return fallback_provider.estimate_cost(seconds)

                fallback_estimate = round(
                    sum(estimate_fallback_row_cost(row) for row in remaining_rows), 2
                )
                manifest = self._manifest()
                fallback_ledger = BudgetLedger(manifest)
                try:
                    fallback_reservation = fallback_ledger.authorize(
                        fallback_estimate,
                        label=f"图生视频备用 {len(remaining_rows)} 个镜头",
                        critical=True,
                    )
                except BudgetExceeded as exc:
                    return StageResult("blocked", str(exc), jobs)
                write_manifest(self.context.paths, manifest)
                write_internal_agent_reports(self.context.project_dir)

                fallback_command = [
                    *generate_command,
                    "--provider", fallback_provider_name,
                ]
                fallback_result = self._workflow(
                    fallback_command,
                    f"主模型失败，切换 {fallback_provider.capabilities.model_or_tool} 生成剩余片段",
                )
                manifest = self._manifest()
                fallback_ledger = BudgetLedger(manifest)
                try:
                    with jobs.open(encoding="utf-8-sig", newline="") as file:
                        fallback_settled_rows = list(csv.DictReader(file))
                except (OSError, csv.Error):
                    fallback_settled_rows = []
                fallback_new_rows = [
                    row
                    for row in fallback_settled_rows
                    if str(row.get("target_video_filename") or "").strip()
                    and str(row.get("target_video_filename") or "").strip() not in fallback_before_targets
                    and (videos_dir / str(row.get("target_video_filename") or "").strip()).is_file()
                ]
                if fallback_result.status == "done" or fallback_new_rows:
                    fallback_actual = round(
                        sum(estimate_fallback_row_cost(row) for row in fallback_new_rows), 2
                    )
                    fallback_ledger.settle(
                        fallback_reservation,
                        fallback_actual,
                        provider=(
                            fallback_provider.capabilities.provider
                            or fallback_provider.identity.adapter_name
                        ),
                    )
                else:
                    fallback_ledger.release(fallback_reservation, reason=fallback_result.message)
                write_manifest(self.context.paths, manifest)
                write_internal_agent_reports(self.context.project_dir)
                if fallback_result.status == "done" and self._has_generated_video_files(manifest):
                    self._complete_contract_consumer(manifest, "image_video")
                    return StageResult(
                        "done",
                        f"主模型未完成，已由 {fallback_provider.capabilities.model_or_tool} 补齐视频。",
                        jobs,
                    )
                result = fallback_result

        if result.status != "done" and fallback_provider_name.lower() == "browser":
            remaining = max(0, self._job_count(jobs) - after)
            provider_name = str(video_api.get("browser_provider_name", "Flow"))
            handoff = self.context.paths.video_jobs / "browser_video_fallback.md"
            handoff.write_text(
                "\n".join(
                    [
                        "# 图生视频浏览器备用任务",
                        "",
                        f"- 供应商：{provider_name}",
                        f"- jobs CSV：`{jobs}`",
                        f"- 图片目录：`{image_dir}`",
                        f"- 视频保存目录：`{videos_dir}`",
                        f"- 尚缺镜头数：{remaining}",
                        "",
                        "只处理缺失镜头，按 jobs CSV 的 scene、prompt 和 target_video_filename 操作。",
                        "不得覆盖已经通过的 MP4。遇到登录、验证码、积分不足或平台风控时写 blocker，不得绕过。",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fallback = self._codex_task(
                stage="browser_video_fallback",
                label=f"{provider_name} 浏览器图生视频备用路径",
                handoff=handoff,
                prompt=f"请使用可用的浏览器控制工具完整执行：`{handoff}`。完成后逐个确认目标 MP4 已下载并可读取。",
            )
            if fallback.status == "done" and self._has_generated_videos(self._manifest()):
                return StageResult("done", f"API 路径未完成，已由 {provider_name} 浏览器备用路径补齐视频。", handoff)
            return fallback
        return result

    def _stage_video_prompt_review(self, manifest: dict[str, Any]) -> StageResult:
        jobs = self._jobs_csv(manifest)
        image_dir = self._image_dir()
        if jobs is None or image_dir is None:
            return StageResult("blocked", "缺少图生视频任务或分镜图片，无法审核动作提示词。")
        review_dir = self.context.paths.status / "reviews"
        snapshot = review_dir / "video_prompt_inputs.json"
        save_json(snapshot, {"version": 1, "rows": canonical_video_prompt_rows(jobs)})
        control_files = [
            self.context.paths.images / f"{self.context.slug}_storyboard_plan.json",
            self._visual_continuity_contract_path(),
            contract_consumer_path(self.context.project_dir, "image_video"),
        ]
        with jobs.open(encoding="utf-8-sig", newline="") as handle:
            first_job = next(csv.DictReader(handle), {})
        motion_plan = first_existing(first_job.get("motion_plan_path"))
        if motion_plan is not None:
            control_files.append(motion_plan)
        bundle = write_review_bundle(
            review_dir / "video_prompt_bundle.json",
            [snapshot, image_dir, *(path for path in control_files if path is not None and path.exists())],
        )
        decisions_csv = self.context.paths.video_jobs / "prompt_review_decisions.csv"
        review_path = review_dir / "video_prompt_review.json"
        bundle_sha = file_sha256(bundle)
        contact_sheet = self._make_contact_sheet(
            sorted(path for path in image_dir.iterdir() if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}),
            review_dir / "video_prompt_contact_sheet.jpg",
        )
        prompt = "\n".join(
            [
                "你是独立图生视频动作提示词审核员。不要读取生产者推理，也不要修改 jobs CSV。",
                f"当前任务及图片哈希清单：`{bundle}`",
                f"读取任务 CSV：`{jobs}`",
                "如果任务 CSV 含 continuity_state/continuity_required/continuity_forbidden，必须逐字段保留并审核；这些字段和最终提示词中的视觉连续性硬约束不可删改，不能以审核 CSV 覆盖。",
                "逐镜核对 motion_shot 中的 subject_action、environment_motion、camera_motion、entry_state、exit_state、screen_direction、adjacent_handoff、expected_motion；动作职责必须具体且与故事状态一致。",
                "连续性只锁身份、状态、尺度和故事逻辑，不得用完全静止/几乎不动逃避动作；但 expected_motion.primary=quiet 时允许有意义的低运动，不能强迫无意义的大动作。",
                "逐相邻镜头检查出入状态、视线和移动方向；无解释方向反转、瞬移或状态跳变不得 approved。",
                "逐镜头检查动作是否具体、是否符合当前图片和故事、是否过度文学化、是否可能触发眼睛发光/肢体畸变/新增角色。",
                "每个镜头的 prompt 必须显式锁定起始图的角色数量、物体数量和核心外观，禁止凭空新增/删除/融合物体，禁止改变太阳、月亮等核心物体的实心/空心拓扑。",
                f"把最终确认 CSV 写入：`{decisions_csv}`",
                "CSV 必须包含 scene,image_filename,story_text,review_status,prompt,notes；如果任务 CSV 含 continuity_* 或 motion_* 字段，也原样写回每镜的机器字段，不得覆盖或丢弃动作计划；每个镜头一行，review_status 必须是 approved，必要时直接在 prompt 列给出修正后的最终动作提示。",
                f"把结构化审核 JSON 写入：`{review_path}`",
                "JSON 必须包含 approved、score、critical_errors、issues、retry_indices、retry_files、retry_instructions、artifact_sha256。",
                f"artifact_sha256 必须原样写为：{bundle_sha}",
                "只有全部镜头都有可直接提交的视频动作提示时才能 approved=true 且 score>=85。",
            ]
        )
        result = self._codex_task(
            stage="video_prompt_review",
            label="图生视频提示词独立审核",
            handoff=bundle,
            prompt=prompt,
            images=[contact_sheet],
        )
        if result.status != "done":
            return result
        try:
            payload = json.loads(review_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return StageResult("blocked", f"提示词审核未写入有效 JSON：{review_path}", bundle)
        if not decisions_csv.exists() or not review_passes(payload, artifact=bundle):
            return StageResult("blocked", f"提示词审核未通过或缺少确认 CSV：{review_path}", review_path)
        return StageResult("done", f"图生视频提示词独立审核通过：{payload.get('score')} 分", review_path)

    def _stage_video_qa(self, manifest: dict[str, Any]) -> StageResult:
        result = self._workflow([
            "qa-videos", "--project-dir", str(self.context.project_dir),
            "--videos-dir", str(self.context.paths.video_jobs / "videos"),
            "--execution-mode", "production",
        ], "视频片段 QA")
        if result.status != "done":
            return result
        evidence = self.context.paths.status / "qa_videos_report.json"
        try:
            payload = json.loads(evidence.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return StageResult("blocked", "视频 QA 未写入有效运动证据 JSON。", evidence)
        if payload.get("passed") is True:
            return result
        indices = sorted({int(value) for value in payload.get("retry_indices", []) if str(value).isdigit()})
        jobs = self._jobs_csv(manifest)
        if indices and jobs is not None and self._can_retry_stage("video_qa", critical=True):
            reasons = {
                str(row.get("scene")): "；".join(str(item) for item in row.get("issues", []))
                for row in payload.get("clips", []) if isinstance(row, dict) and row.get("issues")
            }
            attempts = self._video_provider_attempt_counts(indices, jobs)
            provider_indices = [scene for scene in indices if attempts.get(scene, 0) < 1]
            editorial_indices = [scene for scene in indices if scene not in provider_indices]
            instructions = {
                str(scene): {
                    "instruction": reasons.get(str(scene)) or "机器 QA 硬伤，仅局部修复该镜头",
                    "provider_prompt": "保持同一角色和场景，动作简化、结构稳定、无文字水印",
                    "hard_defect_code": "machine_qa_failure",
                }
                for scene in indices
            }
            moved = self._quarantine_story_videos(
                provider_indices,
                jobs,
                {str(scene): instructions[str(scene)] for scene in provider_indices},
            ) if provider_indices else []
            editorial_done = self._editorial_fallback_story_videos(
                editorial_indices,
                jobs,
                {str(scene): instructions[str(scene)] for scene in editorial_indices},
            ) if editorial_indices else []
            if moved or editorial_done:
                parts = []
                if moved:
                    parts.append("仅排队一次付费修复镜头：" + ", ".join(map(str, moved)))
                if editorial_done:
                    parts.append("付费额度已用尽，采用相邻镜头延展：" + ", ".join(map(str, editorial_done)))
                return StageResult("retrying", "视频运动/来源 QA 局部恢复：" + "；".join(parts), evidence)
        return StageResult("blocked", "视频运动/来源 QA 未通过：" + ", ".join(map(str, indices)), evidence)

    def _stage_video_review(self, manifest: dict[str, Any]) -> StageResult:
        frames_dir = self.context.paths.status / "video_review_frames"
        frame_paths = sorted(path for path in frames_dir.rglob("*.jpg")) + sorted(path for path in frames_dir.rglob("*.png"))
        jobs = self._jobs_csv(manifest)
        qa_report = first_existing(manifest.get("qa", {}).get("videos"), self.context.paths.status / "qa_videos_report.md")
        videos_dir = self.context.paths.video_jobs / "videos"
        if jobs is None or qa_report is None or not frame_paths:
            return StageResult("blocked", "缺少视频任务、QA 报告或多帧抽样，无法独立审核。")
        try:
            with jobs.open(encoding="utf-8-sig", newline="") as handle:
                expected_scenes = [int(row["scene"]) for row in csv.DictReader(handle)]
        except (OSError, ValueError, KeyError):
            return StageResult("blocked", "视频 jobs CSV 缺少有效 scene，无法独立审核。", jobs)
        review_dir = self.context.paths.status / "reviews"
        policy_path = review_dir / "video_review_policy.json"
        save_json(policy_path, {
            "version": VIDEO_REVIEW_POLICY_VERSION,
            "blocking_hard_defect_codes": sorted(VIDEO_REVIEW_HARD_DEFECT_CODES),
            "non_blocking_soft_deviations": [
                "screen_direction_or_gaze_mismatch",
                "minor_action_order_or_gesture_mismatch",
                "camera_motion_amount_mismatch",
                "minor_state_boundary_timing",
                "small_prop_count_or_persistence_mismatch",
                "decorative_continuity_or_minor_crop_difference",
            ],
            "rule": "软偏差只记录，不得降低到 85 分以下、写入 critical_errors 或触发付费重做。",
        })
        frame_order = {name: index for index, name in enumerate(("start", "q1", "mid", "q3", "end"))}
        clip_dirs = sorted(path for path in frames_dir.iterdir() if path.is_dir())
        contact_sheets: list[Path] = []
        for offset in range(0, len(clip_dirs), 5):
            group_dirs = clip_dirs[offset : offset + 5]
            group_frames = sorted(
                (path for directory in group_dirs for path in directory.glob("*.jpg")),
                key=lambda path: (path.parent.name, frame_order.get(path.stem, 99)),
            )
            if not group_frames:
                continue
            first_scene = group_dirs[0].name.split("_")[0]
            last_scene = group_dirs[-1].name.split("_")[0]
            contact_sheets.append(
                self._make_contact_sheet(
                    group_frames,
                    self.context.paths.status / "reviews" / f"video_contact_sheet_{first_scene}_{last_scene}.jpg",
                    columns=5,
                )
            )
        bundle = write_review_bundle(
            self.context.paths.status / "reviews" / "video_bundle.json",
            [
                jobs,
                qa_report,
                self.context.paths.status / "qa_videos_report.json",
                videos_dir,
                frames_dir,
                policy_path,
                *(path for path in [contract_consumer_path(self.context.project_dir, "image_video")] if path.exists()),
                *(item for item in [self._visual_continuity_contract_path()] if item is not None),
            ],
        )
        result, payload = self._structured_review(
            stage="video_review",
            label="图生视频独立审核",
            bundle=bundle,
            images=contact_sheets,
            rubric=(
                f"必须遵守 {policy_path}，quality_policy 必须写为 {VIDEO_REVIEW_POLICY_VERSION}。"
                "交付标准是批量故事视频可看可用，不是逐字逐动作复刻拍摄脚本。只有黑帧/损坏、水印文字、严重肢体五官崩坏、角色融合消失或身份物种错误、抽搐鬼畜、严重反物理，以及核心故事事件被反转或缺失到镜头不可用，才是硬伤。"
                "左右方向或视线不精确、动作顺序或手势小偏差、镜头运动量差异、状态早晚少量切换、小道具数量或全程可见性、装饰连续性等均为软偏差；软偏差只能写入 issues，不得写入 hard_defects/critical_errors/retry_indices，也不得令可看镜头低于 85 分。"
                "结合机器证据和 start/q1/mid/q3/end 抽帧逐镜输出 per_scene_reviews；story_state_consistent 和 adjacent_handoff_consistent 仍须给布尔值用于审计，但单独为 false 不构成付费重做理由。"
                "hard_defects 与 critical_errors 必须是对象数组，每项包含 scene、hard_defect_code、evidence，代码只能取 policy 文件列出的值；retry_indices 必须与硬伤镜头完全一致。"
                "retry_instructions 必须是对象数组，每项包含 scene、hard_defect_code、instruction、provider_prompt；provider_prompt 是真正提交供应商的完整精简修正版，必须针对硬伤改变原输入、保留镜头核心动作，UTF-16 字符数不超过 120。"
                "若 jobs/receipt 标记 video_source_kind=editorial_adjacent_extension，这是为保证无人值守完片而采用的相邻镜头延展：应按剪辑后的整体叙事是否仍可理解来审核；原镜头动作未复现本身不是硬伤，只有造成核心故事反转或整段不可理解时才可判硬伤。"
                "若只是无法确认细节或证据不足，应列为 issues 并放行，不得把推测升级为硬伤。"
            ),
        )
        if payload:
            policy_issues = video_review_policy_issues(payload, expected_scenes=expected_scenes)
            if policy_issues:
                return StageResult(
                    "blocked",
                    "视频审核输出未满足硬伤策略，已禁止付费重做：" + "；".join(policy_issues),
                    result.handoff,
                )
        if result.status == "done":
            return result
        if payload:
            indices = self._review_retry_indices(payload)
            if indices:
                defaults = load_config().get("agent_defaults", {})
                systemic_fraction = float(defaults.get("video_review_systemic_failure_fraction", 1 / 3))
                systemic_failure = bool(expected_scenes and len(indices) / len(expected_scenes) > systemic_fraction)
                if systemic_failure:
                    confirmation_bundle = write_review_bundle(
                        review_dir / "video_review_bulk_confirmation_bundle.json",
                        [bundle, result.handoff, policy_path],
                    )
                    confirmation_result, confirmation_payload = self._structured_review(
                        stage="video_review_bulk_confirmation",
                        label="图生视频大批量硬伤二次独立复核",
                        bundle=confirmation_bundle,
                        images=contact_sheets,
                        rubric=(
                            f"这是超过全片 {systemic_fraction:.0%} 的系统性硬伤候选，必须从零复核，不得照抄首次审核。"
                            f"严格遵守 {policy_path}，quality_policy={VIDEO_REVIEW_POLICY_VERSION}。"
                            "软语义偏差一律放行；只有画面不可用的白名单硬伤才能进入 hard_defects、critical_errors 和 retry_indices。"
                            "输出完整 per_scene_reviews、hard_defects、critical_errors、retry_indices、retry_instructions 和 evidence_matrix；重试提示必须重写共同的提示词策略，减少并发动作和易畸变描述，并且是不超过120字符的完整供应商提示。"
                        ),
                    )
                    if not confirmation_payload:
                        return StageResult("blocked", "大批量硬伤二次复核没有形成有效结论，未发起付费重做。", confirmation_bundle)
                    confirmation_issues = video_review_policy_issues(
                        confirmation_payload, expected_scenes=expected_scenes,
                    )
                    if confirmation_issues:
                        return StageResult(
                            "blocked",
                            "大批量硬伤二次复核不满足硬伤策略，未发起付费重做：" + "；".join(confirmation_issues),
                            confirmation_result.handoff,
                        )
                    payload = confirmation_payload
                    result = confirmation_result
                    indices = self._review_retry_indices(payload)
                    systemic_failure = bool(expected_scenes and len(indices) / len(expected_scenes) > systemic_fraction)
                    if result.status == "done":
                        canonical_payload = dict(payload)
                        canonical_payload["artifact_sha256"] = file_sha256(bundle)
                        save_json(review_dir / "video_review_review.json", canonical_payload)
                        return StageResult("done", "大批量硬伤候选经二次独立复核后全部判为软偏差，现有视频通过。", review_dir / "video_review_review.json")
                retry_counts = self._video_review_retry_counts(indices, jobs)
                provider_attempts = self._video_provider_attempt_counts(indices, jobs)
                fallback_after = int(defaults.get("video_review_editorial_fallback_after_retries", 3))
                editorial_indices = [
                    scene for scene in indices
                    if retry_counts.get(scene, 0) >= fallback_after or provider_attempts.get(scene, 0) >= 1
                ]
                provider_indices = [scene for scene in indices if scene not in editorial_indices]
                instructions = self._normalize_video_retry_instructions(payload.get("retry_instructions", []))
                moved: list[int] = []
                if provider_indices:
                    provider_instructions = {
                        str(scene): instructions[str(scene)] for scene in provider_indices if str(scene) in instructions
                    }
                    provider_override = str(load_config().get("video_api", {}).get("fallback_provider") or "").strip()
                    should_switch_provider = systemic_failure or any(
                        retry_counts.get(scene, 0) >= 1 for scene in provider_indices
                    )
                    moved = self._quarantine_story_videos(
                        provider_indices,
                        jobs,
                        provider_instructions,
                        count_review_retry=True,
                        provider_override=provider_override if should_switch_provider else "",
                    )
                editorial_done: list[int] = []
                if editorial_indices:
                    editorial_instructions = {
                        str(scene): instructions[str(scene)] for scene in editorial_indices if str(scene) in instructions
                    }
                    editorial_done = self._editorial_fallback_story_videos(
                        editorial_indices, jobs, editorial_instructions,
                    )
                if moved or editorial_done:
                    parts = []
                    if moved:
                        parts.append("已改写提示并切换备用模型重做：" + ", ".join(map(str, moved)))
                    if editorial_done:
                        parts.append("已用相邻镜头延展完成无人值守剪辑降级：" + ", ".join(map(str, editorial_done)))
                    return StageResult("retrying", "视频硬伤自动恢复：" + "；".join(parts), result.handoff)
                return StageResult("blocked", "视频硬伤重试没有形成可证明变化的供应商提示词，未移动文件、未发起付费调用。", result.handoff)
        return result

    def _stage_apply_review(self, manifest: dict[str, Any]) -> StageResult:
        jobs = self._jobs_csv(manifest)
        if jobs is None:
            return StageResult("blocked", "缺少 jobs CSV，无法应用审核。")
        command = [
            "apply-review",
            "--jobs-csv",
            str(jobs),
            "--videos-dir",
            str(self.context.paths.video_jobs / "videos"),
            "--output-dir",
            str(self.context.paths.assembly),
            "--short-slug",
            short_slug(self.context.slug),
        ]
        decisions = self.context.paths.video_jobs / "review_decisions.csv"
        if decisions.exists():
            command.extend(["--decisions-csv", str(decisions)])
        return self._workflow(command, "整理最终合成素材")

    def _stage_music_request(self, manifest: dict[str, Any]) -> StageResult:
        provided_music = self._provided_music(manifest)
        if provided_music is not None:
            return StageResult("done", f"已检测到用户提供的背景音乐，跳过 Suno 配乐任务：{provided_music}")
        story_text = first_existing(manifest.get("inputs", {}).get("story_text"))
        if story_text is None:
            return StageResult("blocked", "缺少故事正文，无法生成配乐任务。")
        contract_context = self._prepare_contract_consumer(manifest, "music")
        if isinstance(contract_context, StageResult):
            return contract_context
        command = [
            "music-request",
            "--story-file",
            str(story_text),
            "--output-dir",
            str(self.context.paths.video_jobs),
            "--slug",
            self.context.slug,
            "--story-title",
            self.context.story_name,
        ]
        narration = first_existing(manifest.get("inputs", {}).get("narration"), manifest.get("inputs", {}).get("extracted_narration"))
        jobs = self._jobs_csv(manifest)
        if narration is not None:
            command.extend(["--narration", str(narration)])
        if jobs is not None:
            command.extend(["--jobs-csv", str(jobs)])
        if contract_context is not None:
            command.extend(["--story-contract-context", str(contract_context)])
        result = self._workflow(command, "生成配乐任务")
        return result

    def _stage_suno_generate(self, manifest: dict[str, Any]) -> StageResult:
        provided_music = self._provided_music(manifest)
        if provided_music is not None:
            return StageResult("done", f"已检测到用户提供的背景音乐，跳过 Suno 生成：{provided_music}")
        if not self._legacy_contract_policy(manifest) and not contract_consumer_context_is_current(self.context.project_dir, "music"):
            return StageResult("blocked", "music 合同请求清单已失效，未打开 Suno 付费生产。")
        handoff = self._write_suno_handoff()
        output_targets = self._expected_suno_audio_targets(manifest)
        if not output_targets:
            return StageResult("blocked", f"音乐分段 CSV 没有可执行的 target_audio_filename：{self._music_plan()}", self._music_plan())
        suno_prompt = (
            f"请读取并执行这份 Suno 浏览器自动化任务：\n{handoff}\n\n"
            f"目标是逐行生成并下载音乐分段 CSV 中全部 {len(output_targets)} 段音乐；每一段都必须按 "
            "target_audio_filename 精确重命名，全部保存到指定 suno_downloads 目录。"
            "只有所有目标文件均已落盘才可报告完成。若当前 CLI 无浏览器控制能力、Suno 未登录、遇到验证码或付费弹窗，"
            f"请写入 `{self._music_dir() / 'suno_cli_blocker.md'}` 说明原因，不要假装完成。"
        )
        request_path = self._music_dir() / f"{self.context.slug}_suno_music_request.md"
        request_manifest = self._music_dir() / f"{self.context.slug}_music_request_manifest.json"
        input_artifacts = tuple(
            {"role": role, "path": str(path), "sha256": file_sha256(path)}
            for role, path in (
                ("suno_music_request", request_path),
                ("music_request_manifest", request_manifest),
                ("music_segment_plan", self._music_plan()),
            )
            if path.is_file()
        )
        result = self._execute_music_provider(
            artifact_id="suno-music-execution",
            operation="generate_music_segments",
            stage="suno_generate",
            label="Codex/Suno 浏览器音乐生成",
            handoff=handoff,
            prompt=suno_prompt,
            output_targets=output_targets,
            input_artifacts=input_artifacts,
            attempt_id=f"suno-generate-attempt-{int(manifest.get('agent', {}).get('stages', {}).get('suno_generate', {}).get('attempts', 0))}",
        )
        if result.status == "blocked":
            blocker = self._music_dir() / "suno_cli_blocker.md"
            blocker.write_text(
                "\n".join(
                    [
                        "# Suno 外部阻塞",
                        "",
                        f"- 时间：{now()}",
                        f"- 子任务结果：{result.message}",
                        "- 原因：后台 Codex 没有可用 Browser 工具，或 Suno 登录态不可用。",
                        "- 恢复：在 Codex 主任务中登录 Suno（或明确批准使用已登录 Chrome），按 handoff 生成并下载音乐到 suno_downloads，然后执行 resume/start。",
                        "- 安全边界：不得绕过验证码、付费弹窗或账号风控。",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            return StageResult("blocked", f"Suno 需要在 Codex 主任务恢复浏览器能力：{blocker}", blocker)
        if result.status != "done":
            return result
        if not self._music_plan_contract_bound(manifest):
            return StageResult("blocked", "Suno 音乐计划缺少当前合同绑定字段。", self._music_plan())
        if self._has_suno_audio(self._manifest()):
            self._complete_contract_consumer(manifest, "music")
            return result
        blocker = self._music_dir() / "suno_cli_blocker.md"
        if blocker.exists():
            return StageResult("blocked", f"Suno 需要外部恢复动作：{blocker}", blocker)
        missing = [path.name for path in output_targets if not path.is_file()]
        return StageResult("failed", f"Suno 子任务返回但仍缺少 {len(missing)} 个目标音频：{'、'.join(missing)}", handoff)

    def _stage_assemble_music(self, manifest: dict[str, Any]) -> StageResult:
        provided_music = self._provided_music(manifest)
        if provided_music is not None:
            return StageResult("done", f"已检测到用户提供的背景音乐，跳过音乐拼接：{provided_music}")
        plan = self._music_plan()
        clips = self._suno_downloads_dir()
        if not plan.exists() or not clips.exists():
            return StageResult("blocked", "缺少音乐分段 CSV 或 Suno 下载目录。")
        return self._workflow([
            "assemble-music",
            "--plan-csv",
            str(plan),
            "--clips-dir",
            str(clips),
            "--output",
            str(self._background_music()),
        ], "拼接背景音乐")

    def _stage_music_qa(self, manifest: dict[str, Any]) -> StageResult:
        music = self._music_for_assembly(manifest)
        narration = first_existing(manifest.get("inputs", {}).get("narration"), manifest.get("inputs", {}).get("extracted_narration"))
        if narration is None or not music.exists():
            return StageResult("blocked", "缺少配乐或旁白，无法执行配乐 QA。")
        command = [
            "qa-music",
            "--project-dir",
            str(self.context.project_dir),
            "--music",
            str(music),
            "--narration",
            str(narration),
        ]
        plan = self._music_plan()
        if plan.exists() and self._provided_music(manifest) is None:
            command.extend(["--plan-csv", str(plan)])
        result = self._workflow(command, "配乐覆盖、响度、削波、静音与分段 QA")
        report = self.context.paths.status / "qa_music_report.json"
        if result.status != "done":
            return result
        if not self._music_qa_report_passes(report):
            return StageResult("blocked", f"配乐 QA 未通过：{report}", report)
        return StageResult("done", "配乐 QA 通过。", report)

    def _stage_assemble_final(self, manifest: dict[str, Any]) -> StageResult:
        narration = first_existing(manifest.get("inputs", {}).get("narration"), manifest.get("inputs", {}).get("extracted_narration"))
        if narration is None:
            return StageResult("blocked", "缺少旁白，无法合成背景成片。")
        command = [
            "assemble",
            "--video-dir",
            str(self.context.paths.assembly / "clips"),
            "--script",
            str(self.context.paths.assembly / "script_lines.txt"),
            "--narration",
            str(narration),
            "--music",
            str(self._music_for_assembly(manifest)),
            "--output-dir",
            str(self.context.paths.assembly),
            "--whisper-model",
            "base",
            "--language",
            "zh",
            "--subtitle-style",
            "clean",
        ]
        if not self._legacy_contract_policy(manifest):
            source = self._artifact_semantic_source(manifest)
            if source is None:
                return StageResult("blocked", "缺少语义源，不能验证逐产物语义呈现计划。")
            try:
                semantic_plan = load_current_artifact_semantic_plan(self.context.project_dir, source)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return StageResult("blocked", f"逐产物语义呈现计划缺失或过期，禁止最终合成：{exc}")
            semantic_cards = self._ensure_semantic_card_assets(semantic_plan)
            if semantic_cards.status != "done":
                return semantic_cards
            command.extend([
                "--project-dir", str(self.context.project_dir),
                "--artifact-semantic-plan", str(self.context.paths.status / "story_contract" / "artifact_semantic_plan.json"),
                "--subtitle-script", str(source),
            ])
        model_dir = ROOT / "models" / "whisper"
        if model_dir.exists():
            command.extend(["--whisper-model-dir", str(model_dir)])
        confirmed_subtitles = first_existing(manifest.get("inputs", {}).get("confirmed_subtitles"))
        if confirmed_subtitles is not None and self._legacy_contract_policy(manifest):
            # Keep semantic story_text/storyboard_text as the video timing
            # script.  The optional user-confirmed stream is only the subtitle
            # script, aligned independently by the existing synthesizer.
            command.extend(["--subtitle-script", str(confirmed_subtitles)])
        return self._workflow(command, "合成背景成片")

    def _semantic_card_receipt_issues(self, plan: dict[str, Any]) -> list[str]:
        card_dir = self.context.paths.images / "semantic_cards"
        receipt_path = card_dir / "semantic_card_generation_receipt.json"
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ["semantic_card_generation_receipt_missing_or_invalid"]
        issues: list[str] = []
        plan_path = semantic_plan_path(self.context.project_dir)
        if receipt.get("schema_version") != "story-semantic-card-generation/v1":
            issues.append("semantic_card_receipt_schema_invalid")
        if receipt.get("artifact_semantic_plan_sha256") != file_sha256(plan_path):
            issues.append("semantic_card_plan_binding_mismatch")
        if receipt.get("imagegen_native") is not True:
            issues.append("semantic_cards_not_imagegen_native")
        if receipt.get("post_render_text_overlay") is not False:
            issues.append("semantic_cards_use_post_render_text_overlay")
        try:
            attempt_count = int(receipt.get("attempt_count") or 0)
        except (TypeError, ValueError):
            attempt_count = 0
        if not 1 <= attempt_count <= 3:
            issues.append("semantic_card_generation_attempt_count_invalid")
        items = receipt.get("cards") if isinstance(receipt.get("cards"), list) else []
        by_kind = {str(item.get("card_kind") or ""): item for item in items if isinstance(item, dict)}
        expected = {
            str(card["card_kind"]): card
            for card in plan.get("visual_cards", [])
            if isinstance(card, dict)
        }
        for kind, card in expected.items():
            item = by_kind.get(kind)
            if item is None or str(item.get("text") or "") != str(card.get("text") or ""):
                issues.append(f"semantic_card_text_binding_mismatch:{kind}")
                continue
            path = Path(str(item.get("path") or "")).expanduser().resolve()
            try:
                path.relative_to(card_dir.resolve())
            except ValueError:
                issues.append(f"semantic_card_path_outside_project:{kind}")
                continue
            if not path.is_file() or item.get("sha256") != file_sha256(path):
                issues.append(f"semantic_card_file_binding_mismatch:{kind}")
                continue
            try:
                with Image.open(path) as image:
                    if image.size != (1920, 1080):
                        issues.append(f"semantic_card_size_invalid:{kind}")
            except OSError:
                issues.append(f"semantic_card_unreadable:{kind}")
            if item.get("ocr_passed") is not True:
                issues.append(f"semantic_card_ocr_not_passed:{kind}")
        if set(by_kind) != set(expected):
            issues.append("semantic_card_set_mismatch")
        return sorted(set(issues))

    def _ensure_semantic_card_assets(self, plan: dict[str, Any]) -> StageResult:
        cards = [card for card in plan.get("visual_cards", []) if isinstance(card, dict)]
        if not cards:
            return StageResult("done", "当前语义计划不需要片头/寓意卡。")
        issues = self._semantic_card_receipt_issues(plan)
        if issues and not self.context.execute:
            return StageResult("done", "dry-run：将生成 ImageGen 一体化片头/寓意卡。")
        card_dir = self.context.paths.images / "semantic_cards"
        card_dir.mkdir(parents=True, exist_ok=True)
        if issues:
            handoff = card_dir / "semantic_cards_imagegen_handoff.md"
            card_rows = []
            for card in cards:
                path = card_dir / f"{card['card_kind']}.png"
                card_rows.append({
                    "card_kind": card["card_kind"],
                    "semantic_kind": card["semantic_kind"],
                    "text": card["text"],
                    "source_line_numbers": card["source_line_numbers"],
                    "output": str(path),
                })
            handoff.write_text(
                "\n".join([
                    "# ImageGen 一体化片头与寓意卡",
                    "",
                    "必须直接调用原生 ImageGen 生成下列 1920×1080 最终图卡。准确中文文字、画面与装饰必须在同一次生成/编辑结果中一体成型；禁止 Pillow、Canvas、HTML、SVG、FFmpeg drawtext 或任何程序后期叠字。",
                    "主账号竖屏包装参考图不适用于这些图卡，禁止引用或混用。视觉应依据本故事与已审核视觉合同自行设计。",
                    "OCR 错字时只重生成对应图卡，最多两轮定向修正。",
                    "",
                    "```json",
                    json.dumps(card_rows, ensure_ascii=False, indent=2),
                    "```",
                    "",
                    f"完成后写 `{card_dir / 'semantic_card_generation_receipt.json'}`，schema_version=story-semantic-card-generation/v1，artifact_semantic_plan_sha256={file_sha256(semantic_plan_path(self.context.project_dir))}，imagegen_native=true，post_render_text_overlay=false，attempt_count 记录实际生成轮次（1–3，即首次加最多两轮定向修正）。cards 逐项记录 card_kind、text、path、sha256、ocr_passed=true。",
                ]) + "\n",
                encoding="utf-8",
            )
            result = self._codex_task(
                stage="semantic_cards",
                label="ImageGen 一体化片头/寓意卡生成",
                handoff=handoff,
                prompt="严格执行 handoff，直接生成并保存最终图卡与哈希/OCR 回执；不要只总结。",
            )
            if result.status != "done":
                return result
            remaining = self._semantic_card_receipt_issues(plan)
            if remaining:
                return StageResult("blocked", "ImageGen 片头/寓意卡未通过机器绑定：" + "；".join(remaining), handoff)

        # Static ImageGen cards are only the first half of the production
        # contract.  The formal compositor builds the provider request from
        # real aligned audio windows; do not create a fake four-second request
        # here.  If a timing-bound request already exists, accept only its
        # current text-stable provider receipt. Otherwise assembly will write
        # the exact 6s/10s request and stop with the generated handoff.
        motion_request = card_dir / "semantic_card_motion_request.json"
        motion_receipt = card_dir / "semantic_card_motion_receipt.json"
        if motion_request.is_file():
            motion_issues = semantic_card_motion_receipt_issues(motion_request, motion_receipt)
        else:
            motion_issues = ["semantic_card_motion_timing_request_pending"]
        if motion_request.is_file() and not motion_issues:
            return StageResult(
                "done",
                "ImageGen 片头/寓意卡及文字锁定微动版已绑定当前语义计划。",
                motion_receipt,
            )
        if not self.context.execute:
            return StageResult("done", "dry-run：正式合成将按真实音频窗口生成 6/10 秒微动请求。", motion_request)
        # Let the compositor calculate the real title/moral windows.  It will
        # either consume the current receipt or fail closed after writing the
        # exact provider request and handoff.
        return StageResult("done", "静态语义卡已通过；微动请求将在正式音频对齐后生成。", motion_request)

    def _stage_release_assets(self, manifest: dict[str, Any]) -> StageResult:
        if not self._legacy_contract_policy(manifest):
            semantic_source = self._artifact_semantic_source(manifest)
            if semantic_source is None or not artifact_semantic_plan_is_current(
                self.context.project_dir, semantic_source
            ):
                return StageResult("blocked", "逐产物语义呈现计划缺失或已过期，禁止生成发布视觉素材。")
        contract_context = self._prepare_contract_consumer(manifest, "release_video")
        if isinstance(contract_context, StageResult):
            return contract_context
        result = self._workflow(["prepare-release-assets-project", "--project-dir", str(self.context.project_dir)], "生成发布视觉任务书")
        if result.status != "done":
            return result
        handoff = self.context.paths.release / "theme_assets" / "theme_assets_codex_handoff.txt"
        if contract_context is not None and handoff.exists():
            self._append_contract_handoff(handoff, contract_context, "品牌、发布布局、真人与故事画面安全区")
        reference_images: list[Path] = []
        if not self._legacy_contract_policy(manifest):
            try:
                reference = load_main_package_reference()
            except (OSError, ValueError) as exc:
                return StageResult("blocked", f"package_reference_missing: {exc}", handoff)
            reference_images = [Path(str(reference["asset_path"]))]
        result = self._codex_task(
            stage="release_assets",
            label="Codex 原生发布视觉素材生成",
            handoff=handoff if handoff.exists() else None,
            prompt=build_release_assets_agent_prompt(handoff),
            images=reference_images,
        )
        if result.status == "done" and not self._has_release_assets(self._manifest()):
            return StageResult("blocked", "Codex CLI 子任务已返回，但发布视觉素材/keying 参数没有完整落盘。", handoff if handoff.exists() else None)
        return result

    def _stage_release_preview(self, manifest: dict[str, Any]) -> StageResult:
        preset = self.context.paths.release / "keying" / "keying_preset.json"
        try:
            refresh_keying_quality_from_preset(preset)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return StageResult("blocked", f"无法为当前抠像 candidate 生成分区机器证据：{exc}", preset)
        if not self._legacy_contract_policy(manifest):
            # The annotation pass may have finalized crop/alignment parameters
            # after the first product preflight. Re-render only the short Demo
            # evidence here so Release and the later full Demo share the same
            # real source, transform and keying inputs without making Release
            # depend on the complete product package.
            product_context = contract_consumer_path(self.context.project_dir, "product_package")
            if not product_context.is_file():
                return StageResult("blocked", "缺少 product_package 合同上下文，无法刷新共用 Demo 短预演。")
            demo_preview_command = [
                "product-package-preflight-project",
                "--project-dir", str(self.context.project_dir),
                "--story-contract-context", str(product_context),
            ]
            demo_params = self.context.paths.status / "product_package_work" / "demo_params.json"
            if demo_params.is_file():
                try:
                    data = json.loads(demo_params.read_text(encoding="utf-8"))
                    for key, flag in (
                        ("demo_person_crop_mode", "--demo-person-crop-mode"),
                        ("demo_person_vertical_align", "--demo-person-vertical-align"),
                        ("demo_person_crop_bottom_ratio", "--demo-person-crop-bottom-ratio"),
                    ):
                        if key in data:
                            demo_preview_command.extend([flag, str(data[key])])
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    return StageResult("blocked", f"Demo 最终布局参数损坏：{exc}", demo_params)
            demo_preview = self._workflow(demo_preview_command, "按最终参数刷新 Demo/发布共用短预演")
            if demo_preview.status != "done":
                return demo_preview
        command = ["preview-release-project", "--project-dir", str(self.context.project_dir)]
        if not self._legacy_contract_policy(manifest):
            command.extend([
                "--story-contract-context",
                str(contract_consumer_path(self.context.project_dir, "release_video")),
            ])
        result = self._workflow(command, "生成发布预览")
        if result.status != "done":
            return result
        preview_dir = self.context.paths.status / "release_preview_frames"
        handoff = preview_dir / "release_preview_feedback_to_codex.md"
        preview_images = self._release_preview_images()
        keying_search = self.context.paths.release / "keying" / "keying_search.json"
        keying_candidates = self.context.paths.release / "keying" / "keying_candidates.jpg"
        keying_machine_qa = self.context.paths.release / "keying" / "keying_machine_qa.json"
        keying_evidence = self.context.paths.release / "keying" / "evidence"
        try:
            qa_payload = json.loads(keying_machine_qa.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return StageResult("blocked", "抠像机器 QA 缺失或损坏，禁止独立审核。", keying_machine_qa)
        if qa_payload.get("passed") is not True or qa_payload.get("critical_errors"):
            return StageResult("blocked", f"抠像机器 QA 未通过：{keying_machine_qa}", keying_machine_qa)
        evidence_manifest = Path(str(qa_payload.get("evidence_manifest") or ""))
        try:
            images = keying_review_images(keying_candidates, evidence_manifest, preview_images)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return StageResult("blocked", f"抠像分区审核图片无法加载：{exc}", evidence_manifest)
        bundle = write_review_bundle(
            self.context.paths.status / "reviews" / "release_preview_bundle.json",
            [
                preset, keying_search, keying_candidates, keying_machine_qa, keying_evidence, preview_dir,
                *(path for path in [contract_consumer_path(self.context.project_dir, "release_video")] if path.exists()),
            ],
        )
        result, payload = self._structured_review(
            stage="release_preview",
            label="发布预览独立视觉审核",
            bundle=bundle,
            images=images,
            rubric=(
                "先比较 keying_candidates.jpg：RVM 项目比较当前素材同一时序 Alpha 的 0/1/2 像素内收候选，"
                "颜色键项目比较站立帧与大手势帧的 3×3 参数候选；并逐项引用 evidence 下的"
                "head_hair、左右 shoulder_forearm_hand、garment_outline、适用时的 hem/legs、full_body 和 high_contrast_edges 证据。"
                "检查发丝自然度、肩膀/手臂/手部边缘、衣服与下摆完整性、绿色溢出、灰黑/亮色 halo、锯齿、透明孔洞、背景透漏，"
                "以及人物是否像贴纸、人物与背景光感是否割裂。再检查主账号和宝库号预览中的人物比例与位置、故事框覆盖、"
                "字幕安全区以及 A（人物+故事框）、B（故事框）、C（人物+主题背景）三种构图。人物必须保留拍摄原构图和原始大小，禁止因自动检测框被缩小或切手。"
                "A 镜只在人物首次出现、手臂自然放下的校准帧检查基础 X 轴：躯干主体应落在故事框右侧留白，不得居中大面积压框。"
                "后续伸手、转身等自然动作与框相交允许，不得因此判失败；只有基础位置跳动或异常重定位才是错误。"
                "必须核对主账号上下包装图实际被引用、与稳定参考图的简洁层级一致，且没有泄漏参考图中的“历史故事/煮酒论英雄/4分50秒/8岁以上”。"
                "同时检查 LUT 是否只应用一次、肤色是否自然、画面是否灰暗或过饱和，以及模糊背景是否有人眼可见的矩形拼接块。"
                "抠像截断、主体内部误透明、明显 spill/halo、初始躯干因错误锚点大面积压框、框体露缝、明显矩形背景块均属于 P0/Critical，不能被总分抵消；后续手势的自然相交不属于 P0。"
                "失败时在 retry_instructions 中明确给出候选 id 或可执行的 keying_preset 参数修订建议。"
            ),
        )
        if result.status == "done":
            review_path = self.context.paths.status / "reviews" / "release_preview_review.json"
            try:
                lock_keying_preset(
                    preset,
                    machine_qa_path=keying_machine_qa,
                    evidence_manifest_path=Path(str(qa_payload["evidence_manifest"])),
                    review_bundle_path=bundle,
                    review_path=review_path,
                )
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                return StageResult("blocked", f"独立审核虽通过，但抠像 preset 无法确定性锁定：{exc}", review_path)
            return result
        if payload and preset.exists() and self._can_retry_stage("release_preview", critical=True):
            previous_sha = file_sha256(preset)
            review_path = self.context.paths.status / "reviews" / "release_preview_review.json"
            revision_prompt = "\n".join(
                [
                    "你是发布布局与抠像参数修订生产者。根据独立审核意见最小化修改 keying_preset.json。",
                    f"参数文件：`{preset}`",
                    f"候选搜索记录：`{keying_search}`",
                    f"候选对照图：`{keying_candidates}`",
                    f"独立审核：`{review_path}`",
                    f"预览说明：`{handoff}`",
                    "如果 keyer=rvm，必须从当前素材的 0/1/2 像素内收候选中选择最能兼顾发丝/手部保留与色边清理的候选，"
                    "同步写入 keying_candidate 与 rvm_alpha_choke_pixels；如果是颜色键，才调整 similarity/blend。"
                    "只修改参数文件，不得伪造批准文件；完成后下一轮会重新渲染并由新上下文审核。",
                ]
            )
            revision = self._codex_task(
                stage=f"release_preview_revision_{int(self._manifest().get('agent', {}).get('stages', {}).get('release_preview', {}).get('attempts', 1))}",
                label="发布预览参数自动修订",
                handoff=review_path,
                prompt=revision_prompt,
                images=images,
            )
            if revision.status != "done":
                return revision
            if file_sha256(preset) == previous_sha:
                return StageResult("blocked", "发布预览未通过，但自动修订没有改变 keying_preset.json。", review_path)
            return StageResult("retrying", "已修改抠像/布局参数，将重新渲染并独立复审。", review_path)
        return result

    def _stage_package_release(self, manifest: dict[str, Any]) -> StageResult:
        if not self._consumer_request_current(manifest, "release_video"):
            return StageResult("blocked", "release_video 合同请求清单已失效，拒绝渲染发布视频。")
        if not self._legacy_contract_policy(manifest):
            preset = self.context.paths.release / "keying" / "keying_preset.json"
            issues = keying_preset_lock_issues(preset)
            if issues:
                return StageResult("blocked", "抠像审核锁无效，拒绝渲染全片：" + "；".join(issues), preset)
        command = ["package-release-project", "--project-dir", str(self.context.project_dir)]
        if not self._legacy_contract_policy(manifest):
            command.extend([
                "--story-contract-context",
                str(contract_consumer_path(self.context.project_dir, "release_video")),
            ])
        result = self._workflow(command, "生成主账号/宝库号发布视频")
        if result.status == "done":
            self._complete_contract_consumer(manifest, "release_video")
        return result

    def _stage_release_qa(self, manifest: dict[str, Any]) -> StageResult:
        result = self._workflow(["qa-release", "--project-dir", str(self.context.project_dir)], "发布终片编码与音画同步 QA")
        report = self.context.paths.status / "qa_release_report.json"
        if result.status != "done":
            return result
        if not self._json_qa_report_passes(report):
            return StageResult("failed", f"发布终片编码或音画同步 QA 未通过：{report}", report)
        return StageResult("done", "发布终片编码与音画同步 QA 通过。", report)

    def _stage_release_video_review(self, manifest: dict[str, Any]) -> StageResult:
        from release_video import tail_probe_timestamps

        videos = [self.context.paths.release / "主账号发布视频.mp4", self.context.paths.release / "宝库号发布视频.mp4"]
        if not all(path.exists() for path in videos):
            return StageResult("blocked", "主账号或宝库号发布视频缺失，无法终片独立审核。")
        frame_dir = self.context.paths.status / "release_video_review_frames"
        if frame_dir.exists():
            shutil.rmtree(frame_dir)
        frames: list[Path] = []
        tail_sheets: list[Path] = []
        boundary_sheets: list[Path] = []
        for video in videos:
            duration = self._probe_duration(video)
            timestamps = [0.5, 5.0, 10.0, 14.5, duration * 0.25, duration * 0.5, duration * 0.75]
            timestamps.extend(max(0.0, duration - offset) for offset in (14.5, 10.0, 5.0, 0.5))
            if video.name == "宝库号发布视频.mp4":
                # The library edition must have at least 30 seconds of a blurred,
                # watermark-free tail.  Sparse quarter-point sampling cannot prove
                # that duration and previously caused a reviewer to infer a false
                # start time.  Include boundary evidence on both sides of 30s.
                timestamps.extend(
                    [
                        max(0.0, duration - 36.0),
                        max(0.0, duration - 31.0),
                        max(0.0, duration - 30.0),
                        max(0.0, duration - 29.0),
                    ]
                )
            timestamps.append(max(0.0, duration - 0.5))
            timestamps.extend(tail_probe_timestamps(duration, fps=6, window_seconds=2.0))
            timestamps = list(dict.fromkeys(round(timestamp, 3) for timestamp in timestamps))
            for index, timestamp in enumerate(timestamps, start=1):
                target = frame_dir / video.stem / f"frame_{index:02d}_{timestamp:.1f}s.jpg"
                target.parent.mkdir(parents=True, exist_ok=True)
                command = ["ffmpeg", "-y", "-v", "error", "-ss", f"{timestamp:.3f}", "-i", str(video), "-frames:v", "1", "-q:v", "2", str(target)]
                process = subprocess.run(
                    command,
                    text=True,
                    capture_output=True,
                    env=normalized_subprocess_environment(),
                )
                if process.returncode == 0 and target.exists():
                    frames.append(target)
            video_tail_frames = sorted((frame_dir / video.stem).glob("*.jpg"))[-14:]
            if video_tail_frames:
                tail_sheets.append(
                    self._make_contact_sheet(
                        video_tail_frames,
                        self.context.paths.status / "reviews" / f"{video.stem}_tail_2s_contact_sheet.jpg",
                        columns=7,
                    )
                )
            video_frames = sorted((frame_dir / video.stem).glob("*.jpg"))
            if video_frames:
                boundary_sheets.append(self._make_contact_sheet(
                    video_frames[:4] + video_frames[-4:],
                    self.context.paths.status / "reviews" / f"{video.stem}_first_last_15s.jpg",
                    columns=4,
                ))
        if not frames:
            return StageResult("blocked", "发布终片无法抽帧，拒绝仅凭文件存在放行。")
        contact_sheet = self._make_contact_sheet(frames, self.context.paths.status / "reviews" / "release_videos_contact_sheet.jpg", columns=5)
        bundle = write_review_bundle(
            self.context.paths.status / "reviews" / "release_video_bundle.json",
            [
                *videos, frame_dir,
                *(path for path in sorted(self.context.paths.release.glob("release_geometry_manifest_*.json")) if path.is_file()),
                *(path for path in sorted(self.context.paths.release.glob("release_render_manifest_*.json")) if path.is_file()),
                *(path for path in [contract_consumer_path(self.context.project_dir, "release_video")] if path.exists()),
            ],
        )
        result, payload = self._structured_review(
            stage="release_video_review",
            label="发布终片独立审核",
            bundle=bundle,
            images=[contact_sheet, *boundary_sheets, *self._release_preview_images(), *tail_sheets],
            rubric=(
                "检查主账号和宝库号最终竖版视频抽帧：必须逐项引用首15秒、末15秒、A/B/C代表帧、切换帧、最大手势、字幕、Logo、上下确定性条带证据；"
                "A镜人物必须继承审核通过的Demo/source-native尺度，仅允许合同要求的水平位移，不得二次fit-to-box缩小。"
                "同时检查人物抠像、字幕、标题信息、故事框、Logo、水印、尾部提示、"
                "黑帧和安全区。主账号必须是 2160×2880 且不得出现销售联系尾卡；宝库号应为 1080×1440，尾部模糊至少 30 秒且模糊阶段不显示移动水印。"
                "宝库号接近片尾的连续抽帧包含片尾前 36、31、30、29 秒及最后 0.5 秒；请用这些带时间戳的边界帧核验尾部时长，"
                "不要根据稀疏整十秒采样推测模糊起点。若片尾前 31 秒的帧已经模糊且无移动水印，即满足至少 30 秒。"
                "人物肤色应自然，不灰、不脏、不过饱和。任何人物截断、错误标题、画面露缝、黑帧或缺少账号版本都是关键错误。"
                "另外必须逐张检查两个版本最后 2 秒 6fps 高频证据和安全终帧，审核 JSON 的 evidence_matrix 要写明时间戳、文件名、是否有大黑矩形/透明人形变黑块/边缘断裂；"
                "只看片尾前 0.5 秒或仅凭音画时长数字不能判定通过。"
            ),
        )
        if result.status == "done":
            return result
        review_path = self.context.paths.status / "reviews" / "release_video_review_review.json"
        return StageResult(
            "blocked",
            "正式编码后独立视觉审核未通过；现有正式视频和已锁定参数均已保留，"
            "不会自动修改抠像/布局，也不会触发整片重编码。可执行局部技术修复，或由用户接受当前版本。",
            review_path if review_path.exists() else result.handoff,
        )

    def _stage_publish_package(self, manifest: dict[str, Any]) -> StageResult:
        contract_context = self._prepare_contract_consumer(manifest, "cover")
        if isinstance(contract_context, StageResult):
            return contract_context
        result = self._workflow(["publish-package-project", "--project-dir", str(self.context.project_dir)], "生成发布物料任务包")
        if result.status != "done":
            return result
        handoff = self.context.paths.publish / "publish_package_codex_handoff.md"
        if contract_context is not None and handoff.exists():
            self._append_contract_handoff(handoff, contract_context, "品牌、角色身份与封面布局安全区")
            payload = json.loads(contract_context.read_text(encoding="utf-8"))
            with handoff.open("a", encoding="utf-8") as handle:
                handle.write("\n## required_v1 ImageGen 一体成型封面覆盖指令\n\n")
                handle.write("本节覆盖 handoff 中所有无字底图、Runtime 叠字和后期 Logo 指令。最终六张 `cover_*.png` 的画面、准确中文标题、信息和装饰必须由 Codex ImageGen 一体成型；禁止任何本地脚本后期叠字。\n\n")
                handle.write("以下已审核投影必须完整进入最终 ImageGen handoff，不得丢弃或自行扩展身份锚点：\n\n```json\n")
                handle.write(json.dumps(payload.get("contract_projection", {}), ensure_ascii=False, sort_keys=True, indent=2))
                handle.write("\n```\n")
        if handoff.exists():
            result = self._codex_task(
                stage="publish_package",
                label="Codex 4:3 封面衍生",
                handoff=handoff,
                prompt=build_publish_package_agent_prompt(
                    handoff,
                    self.context.project_dir,
                    required_v1=contract_context is not None,
                ),
            )
            if result.status == "done" and contract_context is None and not self._has_publish_package_files():
                return StageResult("blocked", "Codex CLI 子任务已返回，但主账号/宝库号封面没有完整落盘。", handoff)
            if result.status == "done":
                if contract_context is not None:
                    receipt = self.context.paths.publish / "cover_integrated_generation.json"
                    issues, _ = integrated_cover_issues(
                        self.context.paths.publish,
                        receipt_path=receipt,
                        expected_title=str(manifest.get("story", {}).get("name") or ""),
                    )
                    if issues:
                        return StageResult("blocked", "一体成型 ImageGen 封面收据未通过：" + "；".join(issues), handoff)
                    if not self._has_publish_package_files():
                        return StageResult("blocked", "ImageGen 已返回，但六张最终封面或文案不完整。", receipt)
                    self._complete_contract_consumer(manifest, "cover")
                    return StageResult("done", "六张最终封面已由 Codex ImageGen 一体成型生成。", receipt)
                try:
                    receipt = apply_fixed_cover_branding(self.context.project_dir)
                except (OSError, ValueError) as exc:
                    return StageResult("blocked", f"封面品牌定版失败：{exc}", handoff)
                if not self._has_publish_package_files():
                    return StageResult("blocked", "封面定版结束，但六张最终封面或文案不完整。", receipt)
                return StageResult("done", "legacy 六张封面已完成。", receipt)
            return result
        return result

    def _stage_publish_package_review(self, manifest: dict[str, Any]) -> StageResult:
        publish = self.context.paths.publish
        required_v1 = not self._legacy_contract_policy(manifest)
        review_images = cover_review_image_paths(publish, required_v1=required_v1)
        covers = review_images[:6]
        creative_bases = review_images[6:]
        copy_files = [publish / "main" / "copy.md", publish / "library" / "copy.md"]
        if not all(path.exists() for path in [*review_images, *copy_files]):
            return StageResult("blocked", "发布物料不完整，无法开始独立审核。")
        qa = self._workflow(["qa-publish", "--project-dir", str(self.context.project_dir)], "发布物料比例、尺寸与文案结构 QA")
        if qa.status != "done":
            return qa
        qa_report = self.context.paths.status / "qa_publish_report.md"
        qa_json = self.context.paths.status / "qa_publish_report.json"
        if not self._json_qa_report_passes(qa_json):
            try:
                qa_payload = json.loads(qa_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                qa_payload = {}
            retry_files = qa_payload.get("retry_files", [])
            if isinstance(retry_files, list) and self._can_retry_stage("publish_package_review", critical=True):
                moved = self._quarantine_publish_files(expand_cover_retry_files([str(item) for item in retry_files]))
                if moved:
                    return StageResult("retrying", f"发布物料机器 QA 未通过，已保留并排队重做 {len(moved)} 个文件。", qa_json)
            return StageResult("blocked", f"发布物料机器 QA 未通过或已达到重做上限：{qa_json}", qa_json)
        contact_sheet = self._make_contact_sheet(covers, self.context.paths.status / "reviews" / "publish_covers_contact_sheet.jpg", columns=3)
        lineage = publish / "cover_lineage.json"
        bundle = write_review_bundle(
            self.context.paths.status / "reviews" / "publish_package_bundle.json",
            [
                *review_images, *copy_files, qa_report, qa_json, lineage,
                publish / "cover_integrated_generation.json",
                publish / "cover_render_manifest.json",
                publish / "cover_creative_lineage.json",
                publish / "publish_asset_manifest.json",
                *(path for path in [contract_consumer_path(self.context.project_dir, "cover")] if path.exists()),
            ],
        )
        result, payload = self._structured_review(
            stage="publish_package_review",
            label="发布物料独立审核",
            bundle=bundle,
            images=[*review_images, contact_sheet],
            rubric=(
                "检查两个账号文案定位、标题准确性、敏感承诺和话题相关性；检查六张封面标题文字、真人一致性、故事角色、"
                "比例构图和安全区。结合 cover_lineage.json 检查：主账号 4:3 是唯一主母版，主账号另外两比例由它编辑衍生；"
                "宝库号 4:3 由主母版移除真人得到，另两比例由宝库号母版衍生。六张必须保持同一故事角色、服装、字体、色彩和装饰语言，不能像六次随机生成，也不能只是机械裁切。"
                "版式应遵循历史样例的扁平简洁信息层级，标题与时长/年龄集中，底部适用说明克制，不能自创复杂木框、嵌套框或多层装饰。"
                "必须对照 cover.compiled.json 与 cover_integrated_generation.json 确认六张最终封面均由 Codex ImageGen 一体成型生成，标题、信息、装饰和画面属于同一次生成/编辑结果，不得使用 Pillow、Canvas、HTML 或脚本后期叠字。official_assets 为空时保持零 Logo，生成的花朵/仿写字样不得冒充品牌 Logo。"
                "必须逐张审核六个实际高分辨率文件（contact sheet 只能辅助总览），evidence_matrix 为六张逐一写结论，"
                "并使用 main/covers/cover_*.png 或 library/covers/cover_*.png 的发布目录相对路径标识，不能只写同名文件名。"
                + (
                    "还必须逐张查看六张 creative_base_*.png 原始高分辨率底图，不能用成品标题覆盖区域或 contact sheet 代替。"
                    "evidence_matrix 必须使用 main/covers/creative_base_*.png 与 library/covers/creative_base_*.png 相对路径逐张举证。"
                    "创意底图出现可读正式标题、AI 假汉字/乱码、年龄/时长/用途文字、官方 Logo、仿 Logo、未授权品牌字样或明显第二品牌标记，"
                    "必须写入 p0_errors/critical_errors；正常世界场景中的非品牌元素不得仅因形似文字而机械误杀。"
                    if required_v1 else ""
                )
                + "产品质量使用风格中性的 audience_fit、style_suitability、composition、color、lighting、character_design_fit、identity_coherence、anatomical_coherence；"
                "不得把可爱度作为所有故事默认标准。以下任一项必须列入 p0_errors/critical_errors，不能被总分抵消：标题错误或缺失、底图残留假文字、Logo 数量不符合合同/重复/伪造、"
                "关键角色或真人被裁切、角色身份漂移、母版血缘失效、比例/安全区/受保护区域碰撞。"
                "失败时输出 retry_files，使用相对发布物料目录的路径；Runtime 会只扩展真正的 lineage 后代。"
            ),
        )
        if required_v1:
            expected_assets = [str(path.relative_to(publish)) for path in [*covers, *creative_bases]]
            review_issues = cover_review_payload_issues(payload, expected_assets)
            if review_issues:
                result = StageResult(
                    "blocked",
                    "发布物料独立审核证据不完整或存在 P0：" + "；".join(review_issues),
                    result.handoff,
                )
        if result.status == "done":
            return result
        if payload and self._can_retry_stage("publish_package_review", critical=True):
            retry_files = payload.get("retry_files", [])
            if isinstance(retry_files, list):
                moved = self._quarantine_publish_files(expand_cover_retry_files([str(item) for item in retry_files]))
                if moved:
                    return StageResult("retrying", f"发布物料审核未通过，已保留失败版本并排队重做 {len(moved)} 个文件。", result.handoff)
        return result

    def _stage_product_preflight(self, manifest: dict[str, Any]) -> StageResult:
        contract_context = self._prepare_contract_consumer(manifest, "product_package")
        if isinstance(contract_context, StageResult):
            return contract_context
        command = ["product-package-preflight-project", "--project-dir", str(self.context.project_dir)]
        if contract_context is not None:
            command.extend(["--story-contract-context", str(contract_context)])
        result = self._workflow(command, "资料包前置审查")
        handoff = self.context.paths.status / "product_package_work" / "第16步资料包_Codex前置审查.md"
        if result.status == "done" and contract_context is not None and handoff.exists():
            self._append_contract_handoff(handoff, contract_context, "PPT、文稿、朗读标注、示范视频的产物语义矩阵")
        return result

    def _stage_product_annotation(self, manifest: dict[str, Any]) -> StageResult:
        handoff = self.context.paths.status / "product_package_work" / "第16步资料包_Codex前置审查.md"
        images = self._product_preview_images()
        result = self._codex_task(
            stage="product_annotation",
            label="Codex 资料包示范预览审查与朗读标注精修",
            handoff=handoff if handoff.exists() else None,
            prompt=build_product_annotation_agent_prompt(
                handoff,
                self.context.paths.status / "product_package_work" / "annotation.json",
                self.context.paths.status / "product_package_work" / "demo_params.json",
            ),
            images=images,
        )
        if result.status == "done" and not self._has_product_annotation(self._manifest()):
            return StageResult("blocked", "Codex CLI 子任务已返回，但没有写入精修 annotation.json/docx。", handoff if handoff.exists() else None)
        return result

    def _stage_product_package(self, manifest: dict[str, Any]) -> StageResult:
        if not self._consumer_request_current(manifest, "product_package"):
            return StageResult("blocked", "product_package 合同请求清单已失效，拒绝生成 PPT/资料包。")
        annotation = self._product_annotation()
        if annotation is None:
            return StageResult("blocked", "缺少精修朗读标注。")
        command = ["product-package-project", "--project-dir", str(self.context.project_dir)]
        if not self._legacy_contract_policy(manifest):
            command.extend([
                "--story-contract-context",
                str(contract_consumer_path(self.context.project_dir, "product_package")),
            ])
        if annotation.suffix.lower() == ".json":
            command.extend(["--annotation-json", str(annotation)])
        else:
            command.extend(["--annotation-docx", str(annotation)])
        demo_params = self.context.paths.status / "product_package_work" / "demo_params.json"
        if demo_params.exists():
            try:
                data = json.loads(demo_params.read_text(encoding="utf-8"))
                for key, flag in (
                    ("demo_person_crop_mode", "--demo-person-crop-mode"),
                    ("demo_person_vertical_align", "--demo-person-vertical-align"),
                    ("demo_person_crop_bottom_ratio", "--demo-person-crop-bottom-ratio"),
                ):
                    if key in data:
                        command.extend([flag, str(data[key])])
            except Exception:
                pass
        result = self._workflow(command, "正式打包资料包")
        if result.status == "done":
            self._complete_contract_consumer(manifest, "product_package")
        return result

    def _stage_product_annotation_review(self, manifest: dict[str, Any]) -> StageResult:
        annotation = self._product_annotation()
        if annotation is None:
            return StageResult("blocked", "缺少朗读标注，无法独立审核。")
        work = self.context.paths.status / "product_package_work"
        demo_params = work / "demo_params.json"
        handoff = work / "第16步资料包_Codex前置审查.md"
        artifacts = [annotation]
        if demo_params.exists():
            artifacts.append(demo_params)
        if handoff.exists():
            artifacts.append(handoff)
        consumer_manuscript = first_existing(manifest.get("outputs", {}).get("consumer_manuscript"))
        if consumer_manuscript is not None:
            artifacts.append(consumer_manuscript)
        contract_context = contract_consumer_path(self.context.project_dir, "product_package")
        if contract_context.exists():
            artifacts.append(contract_context)
        annotation_receipt = work / "reading_annotation_receipt.json"
        content_manifest = work / "product_content_manifest.json"
        for path in (annotation_receipt, content_manifest):
            if path.exists():
                artifacts.append(path)
        bundle = write_review_bundle(self.context.paths.status / "reviews" / "product_annotation_bundle.json", artifacts)
        images = self._product_preview_images()
        result, payload = self._structured_review(
            stage="product_annotation_review",
            label="朗读标注与示范参数独立审核",
            bundle=bundle,
            images=images,
            rubric=(
                "检查朗读标注是否逐段覆盖 consumer_manuscript 的故事正文；消费者文稿已移除主持人开场，因此朗读标注不得补回主持人自我介绍。"
                "marked_text 必须逐字忠实，不得改代词、对白、形容词或句尾。重音、停连、语气和动作提示应适合儿童表演。结合预览检查示范视频裁切参数是否会截断手部或身体。"
                "notes 必须像有经验的幼儿园故事老师当面提醒朗读者：温和、自然、短句，先给角色当下的心情或画面，再落到声音/目光/上半身动作；出现‘内容重点、节奏落点、情绪层次、完成收束’等干硬分析术语必须退回。"
                "长段落的重音必须覆盖动作、情绪、反差和转折，只有一两个重音或只标人物名词必须退回。示范视频应保留原片构图、自然停顿、拟声词和完整句尾。"
                "示范视频是原主持人的表演参考，允许原声和字幕保留‘我是绵羊姐姐’，并应保留一个经品牌合同批准的原 Logo；不得删除、伪造或重复叠加 Logo。不要把主持人口播误判为对外文稿泄漏。"
                "漏掉 consumer_manuscript 中的故事正文、错重音导致语义改变属于关键错误。"
                "required_v1 审核必须逐 block 写 evidence_matrix，每项必须包含 source_line_indices、original_text、fidelity、emotion_fit、"
                "pause_emphasis_quality、performance_guidance_quality 和 passed=true/false。"
                "任何漏字、增字、改写、主持人身份泄漏、占位符、伪造 source_line_indices 或 notes 改变原意均为 P0/Critical，不能被总分抵消。"
            ),
        )
        if result.status == "done" and not self._legacy_contract_policy(manifest):
            from product_quality import annotation_review_payload_issues

            evidence_issues = annotation_review_payload_issues(
                payload or {},
                annotation_json=annotation,
                content_manifest=content_manifest,
            )
            if evidence_issues:
                result = StageResult(
                    "blocked",
                    "朗读标注独立审核结构证据不完整：" + "；".join(evidence_issues),
                    result.handoff,
                )
        if result.status == "done":
            return result
        if payload and self._can_retry_stage("product_annotation_review", critical=True):
            archive = self.context.paths.status / "rejected" / "product_annotation" / time.strftime("%Y%m%d-%H%M%S")
            archive.mkdir(parents=True, exist_ok=True)
            moved = False
            for path in (annotation, demo_params):
                if path.exists():
                    shutil.move(str(path), str(archive / path.name))
                    moved = True
            if moved:
                return StageResult("retrying", "朗读标注审核未通过，已保留失败版本并排队重新精修。", result.handoff)
        return result

    def _stage_final_delivery(self, manifest: dict[str, Any]) -> StageResult:
        command = ["final-delivery", "--project-dir", str(self.context.project_dir)]
        if self.context.update_latest_episode:
            command.append("--update-latest-episode")
        result = self._workflow(command, "生成总交付清单")
        if result.status != "done":
            return result
        if self.context.execute and not self._manifest().get("completed_at"):
            return StageResult("blocked", f"总交付清单已生成，但完成门槛未满足：{self.context.project_dir / '总交付清单.md'}")
        return result

    def _stage_product_package_review(self, manifest: dict[str, Any]) -> StageResult:
        outputs = manifest.get("outputs", {})
        base = first_existing(outputs.get("product_base"))
        advanced = first_existing(outputs.get("product_advanced"))
        if base is None or advanced is None:
            return StageResult("blocked", "基础版或进阶版资料包缺失，无法独立审核。")
        qa = self._workflow(["qa-product", "--project-dir", str(self.context.project_dir)], "资料包结构 QA")
        if qa.status != "done":
            return qa
        report = self.context.paths.status / "qa_product_report.md"
        report_json = self.context.paths.status / "qa_product_report.json"
        if not self._json_qa_report_passes(report_json):
            if self._can_retry_stage("product_package_review", critical=True):
                archive = self.context.paths.status / "rejected" / "product_packages" / time.strftime("%Y%m%d-%H%M%S")
                archive.mkdir(parents=True, exist_ok=True)
                for directory in (base, advanced):
                    if directory.exists():
                        shutil.move(str(directory), str(archive / directory.name))
                return StageResult("retrying", f"资料包机器 QA 未通过，已保留失败包并排队重新打包：{report_json}", report_json)
            return StageResult("blocked", f"资料包机器 QA 未通过且已达到重做上限：{report_json}", report_json)
        bundle = write_review_bundle(
            self.context.paths.status / "reviews" / "product_package_bundle.json",
            [
                base, advanced, report, report_json,
                *(path for path in [contract_consumer_path(self.context.project_dir, "product_package")] if path.exists()),
                *(path for path in [
                    self.context.paths.status / "product_package_work" / "product_content_manifest.json",
                    self.context.paths.status / "product_package_work" / "customer_manuscript_receipt.json",
                    self.context.paths.status / "product_package_work" / "reading_annotation_receipt.json",
                    self.context.paths.status / "product_package_work" / "ppt_with_subtitles_render_manifest.json",
                    self.context.paths.status / "product_package_work" / "ppt_without_subtitles_render_manifest.json",
                    self.context.paths.status / "product_package_work" / "product_package_manifest.json",
                ] if path.exists()),
            ],
        )
        ppt_evidence = sorted(
            (self.context.paths.status / "product_package_work" / "ppt_evidence").glob("**/*.png")
        )
        result, payload = self._structured_review(
            stage="product_package_review",
            label="资料包独立审核",
            bundle=bundle,
            images=ppt_evidence,
            rubric=(
                "核对基础版必须包含故事文稿、朗读标注、音乐、示范视频、背景图片；进阶版必须包含故事文稿、朗读标注、音乐、"
                "示范视频、背景图片、含/无字幕 PPT、含/无字幕背景视频、A镜无人物背景视频。检查文件名、重复/缺失、对外禁用口吻和 QA 报告。"
                "06_资料包 对外层只能包含基础版与进阶版两个客户目录，内部过程文件必须位于 99_项目状态。缺少任一必备文件属于关键错误。"
                "逐张检查含字幕/无字幕 PPT 的首张、中间、末张、最长字幕和边界证据；字幕默认使用小字号单行，只有合同明确记录的例外才允许两行；黑条不得越界，无字幕版不得残留字幕。"
                "核对客户文稿与朗读标注只包含各自 semantic plan 选择的正文，标题唯一，无主持人自我介绍、占位符、内部路径或 Agent/Codex 痕迹。"
                "required_v1 必须写 p0_errors 和 evidence_matrix；每张传入的 PPT 证据必须用相对 ppt_evidence 目录的路径逐项引用。"
                "缺页、重复页、字幕污染/越界、图片拉伸、旧 BGM、漏/增/改客户正文、伪造 source_line_indices 属于 P0，不得被总分抵消。"
            ),
        )
        if result.status == "done" and payload and not self._legacy_contract_policy(manifest):
            from product_quality import product_package_review_payload_issues

            evidence_issues = product_package_review_payload_issues(
                payload,
                evidence_root=self.context.paths.status / "product_package_work" / "ppt_evidence",
                required_evidence=ppt_evidence,
            )
            if evidence_issues:
                return StageResult(
                    "blocked",
                    f"资料包独立审核证据不完整：{'; '.join(evidence_issues)}",
                    result.handoff,
                )
        if result.status == "done":
            return result
        if payload and self._can_retry_stage("product_package_review", critical=True):
            archive = self.context.paths.status / "rejected" / "product_packages" / time.strftime("%Y%m%d-%H%M%S")
            archive.mkdir(parents=True, exist_ok=True)
            for directory in (base, advanced):
                if directory.exists():
                    shutil.move(str(directory), str(archive / directory.name))
            return StageResult("retrying", "资料包审核未通过，已保留失败包并排队重新打包。", result.handoff)
        return result

    def _stage_doctor(self, manifest: dict[str, Any]) -> StageResult:
        return self._workflow(["doctor-project", "--project-dir", str(self.context.project_dir)], "生成工程体检报告")

    def _workflow(self, args: list[str], label: str) -> StageResult:
        profile = self._modules().selection_profile()
        execution_mode = self._modules().selection_execution_mode()
        command = [
            sys.executable,
            str(ROOT / "story_workflow.py"),
            "--module-profile",
            profile,
            "--module-execution-mode",
            execution_mode,
            *args,
        ]
        return self._run_command(
            command,
            label,
            log_name=args[0],
            env_overrides=self._module_subprocess_env(),
        )

    def _run_command(
        self,
        command: list[str],
        label: str,
        *,
        log_name: str,
        env_overrides: dict[str, str] | None = None,
    ) -> StageResult:
        if not self.context.execute:
            return StageResult("done", "dry-run: " + " ".join(str(part) for part in command))
        started = time.time()
        log_dir = self.context.paths.status / "agent_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{int(started)}_{log_name}.log"
        env = normalized_subprocess_environment(overrides=env_overrides)
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout = ""
        stderr = ""
        while True:
            try:
                stdout, stderr = process.communicate(timeout=5)
                break
            except subprocess.TimeoutExpired:
                if self._heartbeat_and_cancelled():
                    process.terminate()
                    try:
                        stdout, stderr = process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        stdout, stderr = process.communicate()
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    log_path.write_text((stdout or "") + ("\n[stderr]\n" + stderr if stderr else "") + "\n[cancelled]\n", encoding="utf-8")
                    return StageResult("cancelled", f"{label}已按取消请求停止，日志：{log_path}", log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text((stdout or "") + ("\n[stderr]\n" + stderr if stderr else ""), encoding="utf-8")
        if process.returncode != 0:
            status = classify_command_failure((stdout or "") + "\n" + (stderr or ""))
            return StageResult(status, f"{label}失败，日志：{log_path}", log_path)
        return StageResult("done", f"{label}完成，日志：{log_path}")

    def _heartbeat_and_cancelled(self) -> bool:
        from story_project import write_manifest

        manifest = load_manifest(self.context.paths)
        if manifest is None:
            return False
        ensure_manifest_v2(manifest)
        manifest["agent"]["heartbeat_at"] = now()
        write_manifest(self.context.paths, manifest)
        return bool(manifest["agent"].get("cancel_requested"))

    def _record_provider_receipt(self, stage: str, result: Any) -> None:
        manifest = ensure_manifest_v2(load_manifest(self.context.paths) or {})
        agent = manifest.get("agent", {}) if isinstance(manifest.get("agent"), dict) else {}
        scheduler = agent.get("scheduler", {}) if isinstance(agent.get("scheduler"), dict) else {}
        output_hashes: dict[str, str] = {}
        for artifact in getattr(result, "output_artifacts", ()) or ():
            if not isinstance(artifact, dict):
                continue
            path = str(artifact.get("path") or "")
            digest = str(artifact.get("sha256") or "")
            if path and digest:
                output_hashes[path] = digest
        failure = getattr(result, "failure", None)
        append_agent_event(
            self.context.project_dir,
            event_type="provider_receipt",
            status="passed" if bool(getattr(result, "success", False)) else "failed",
            summary=(
                str(getattr(failure, "message", ""))
                if failure is not None
                else f"{stage} provider receipt 已记录"
            ),
            job_id=str(agent.get("job_id") or ""),
            run_id=str(scheduler.get("run_id") or ""),
            stage=stage,
            attempt_id=str(getattr(result, "attempt_id", "") or ""),
            output_evidence_hashes=output_hashes,
            provider=str(getattr(result, "provider", "") or ""),
            request_id=str(getattr(result, "request_id", "") or ""),
            receipt_id=str(getattr(result, "execution_request_sha256", "") or ""),
            metadata={
                "model_or_tool": str(getattr(result, "model_or_tool", "") or ""),
                "adapter_version": str(getattr(result, "adapter_version", "") or ""),
                "production_eligible": bool(getattr(result, "production_eligible", False)),
                "usage_events": [
                    {
                        "provider": getattr(item, "provider", ""),
                        "model_or_tool": getattr(item, "model_or_tool", ""),
                        "operation": getattr(item, "operation", ""),
                        "quantity": getattr(item, "quantity", None),
                        "currency": getattr(item, "currency", ""),
                        "estimated_amount": getattr(item, "estimated_amount", None),
                        "actual_amount_status": getattr(item, "actual_amount_status", ""),
                    }
                    for item in (getattr(result, "usage_events", ()) or ())
                ],
            },
        )

    def _execute_image_generation(
        self,
        *,
        artifact_id: str,
        operation: str,
        stage: str,
        label: str,
        handoff: Path,
        prompt: str,
        output_targets: tuple[Path, ...],
        input_artifacts: tuple[dict[str, Any], ...],
        attempt_id: str,
    ) -> StageResult:
        """Execute one already-planned image envelope through the selected Port."""

        prompt_binding = {
            "role": "runtime_prompt",
            "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        }
        request = ImageGeneratorRequest(
            artifact_id=artifact_id,
            operation=operation,
            execution_request_path=handoff,
            execution_request_sha256=file_sha256(handoff),
            input_artifacts=(*input_artifacts, prompt_binding),
            output_targets=output_targets,
            attempt_id=attempt_id,
        )
        image_port: ImageGeneratorPort = self._modules().image_generator()
        runtime_results: list[StageResult] = []

        def current_codex_image_executor(bound_request: ImageGeneratorRequest) -> ImageGeneratorResult:
            if bound_request != request:
                return ImageGeneratorResult(
                    False,
                    bound_request.operation,
                    (),
                    image_port.capabilities.provider,
                    image_port.capabilities.model_or_tool,
                    "",
                    bound_request.attempt_id,
                    bound_request.execution_request_sha256,
                    image_port.identity.adapter_version,
                    True,
                    failure=ModuleFailure(
                        ModuleFailureCode.INVALID_INPUT,
                        "image executor received a request different from the runtime envelope",
                    ),
                )
            stage_result = self._codex_task(
                stage=stage,
                label=label,
                handoff=handoff,
                prompt=prompt,
            )
            runtime_results.append(stage_result)
            artifacts = tuple(
                {
                    "path": str(target),
                    "sha256": file_sha256(target),
                    "production_eligible": True,
                }
                for target in output_targets
                if target.is_file()
            )
            succeeded = stage_result.status == "done"
            return ImageGeneratorResult(
                succeeded,
                request.operation,
                artifacts,
                image_port.capabilities.provider,
                image_port.capabilities.model_or_tool,
                "",
                request.attempt_id,
                request.execution_request_sha256,
                image_port.identity.adapter_version,
                True,
                failure=None if succeeded else ModuleFailure(
                    ModuleFailureCode.EXECUTION_FAILED,
                    stage_result.message,
                    retryable=stage_result.status in {"blocked", "retrying"},
                    details={"stage_status": stage_result.status},
                ),
            )

        port_result = image_port.execute(request, executor=current_codex_image_executor)
        self._record_provider_receipt(stage, port_result)
        if runtime_results and runtime_results[0].status != "done":
            return runtime_results[0]
        if not port_result.success:
            message = port_result.failure.message if port_result.failure else "image generator failed"
            return StageResult("failed", message, handoff)
        return StageResult("done", f"{label} 已通过 ImageGeneratorPort 执行。", handoff)

    def _execute_music_provider(
        self,
        *,
        artifact_id: str,
        operation: str,
        stage: str,
        label: str,
        handoff: Path,
        prompt: str,
        output_targets: tuple[Path, ...],
        input_artifacts: tuple[dict[str, Any], ...],
        attempt_id: str,
    ) -> StageResult:
        """Execute the existing complete Suno handoff through the selected Port."""

        prompt_binding = {
            "role": "runtime_prompt",
            "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        }
        request = MusicProviderRequest(
            artifact_id=artifact_id,
            operation=operation,
            execution_request_path=handoff,
            execution_request_sha256=file_sha256(handoff),
            input_artifacts=(*input_artifacts, prompt_binding),
            output_targets=output_targets,
            attempt_id=attempt_id,
        )
        music_port: MusicProviderPort = self._modules().music_provider()
        runtime_results: list[StageResult] = []

        def current_suno_codex_executor(bound_request: MusicProviderRequest) -> MusicProviderResult:
            if bound_request != request:
                return MusicProviderResult(
                    False,
                    bound_request.operation,
                    (),
                    music_port.capabilities.provider,
                    music_port.capabilities.model_or_tool,
                    "",
                    bound_request.attempt_id,
                    bound_request.execution_request_sha256,
                    music_port.identity.adapter_version,
                    True,
                    failure=ModuleFailure(
                        ModuleFailureCode.INVALID_INPUT,
                        "music executor received a request different from the runtime envelope",
                    ),
                )
            stage_result = self._codex_task(
                stage=stage,
                label=label,
                handoff=handoff,
                prompt=prompt,
            )
            runtime_results.append(stage_result)
            downloads = self._suno_downloads_dir()
            artifacts = tuple(
                {
                    "path": str(path),
                    "sha256": file_sha256(path),
                    "production_eligible": True,
                }
                for path in sorted(downloads.iterdir())
                if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
            ) if downloads.is_dir() else ()
            succeeded = stage_result.status == "done"
            return MusicProviderResult(
                succeeded,
                request.operation,
                artifacts,
                music_port.capabilities.provider,
                music_port.capabilities.model_or_tool,
                "",
                request.attempt_id,
                request.execution_request_sha256,
                music_port.identity.adapter_version,
                True,
                failure=None if succeeded else ModuleFailure(
                    ModuleFailureCode.EXECUTION_FAILED,
                    stage_result.message,
                    retryable=stage_result.status in {"blocked", "retrying"},
                    details={"stage_status": stage_result.status},
                ),
            )

        port_result = music_port.execute(request, executor=current_suno_codex_executor)
        self._record_provider_receipt(stage, port_result)
        if runtime_results and runtime_results[0].status != "done":
            return runtime_results[0]
        if not port_result.success:
            message = port_result.failure.message if port_result.failure else "music provider failed"
            return StageResult("failed", message, handoff)
        if runtime_results:
            return runtime_results[0]
        return StageResult("done", f"{label} 已通过 MusicProviderPort 执行。", handoff)

    def _codex_task(
        self,
        *,
        stage: str,
        label: str,
        handoff: Path | None,
        prompt: str,
        images: list[Path] | None = None,
    ) -> StageResult:
        prompt_path = self._write_codex_prompt(stage, prompt, handoff)
        if self.context.codex_mode == "handoff":
            return StageResult("blocked", f"需要 {label}；请执行 handoff 后再次运行 Agent。", handoff or prompt_path)
        if not self.context.execute:
            return StageResult("done", f"dry-run: 将调用 Codex CLI 执行 {label}，prompt={prompt_path}")
        result = self._run_codex_exec(stage, prompt_path, images or [])
        if result.status != "done":
            return result
        return StageResult("done", f"{label} 已由 Codex CLI 子任务执行，日志：{result.message}", handoff)

    def _write_codex_prompt(self, stage: str, prompt: str, handoff: Path | None) -> Path:
        prompt_dir = self.context.paths.status / "agent_codex_tasks"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = prompt_dir / f"{stage}_prompt.md"
        prompt_path.write_text(
            "\n".join(
                [
                    f"# Story Agent Codex Task: {stage}",
                    "",
                    f"- 项目目录：`{self.context.project_dir}`",
                    f"- 工程目录：`{ROOT}`",
                    f"- handoff：`{handoff or ''}`",
                    "",
                    "## 执行要求",
                    "",
                    "- 只处理本阶段任务，不要重跑无关阶段。",
                    "- 可以修改当前故事项目目录内的文件。",
                    "- 不要改旧路径 `/Users/baiyanglin/Documents/New project`。",
                    *(
                        [
                            "- 视觉审核只使用当前 GPT 模型通过 --image 附件获得的原生多模态能力。",
                            "- 禁止调用 Claude Vision、claude-vision、vision bridge、GLM/智谱视觉桥接、bigmodel.cn 或任何其他外部视觉服务；也不得把‘尝试但失败’当作正常流程。",
                        ]
                        if stage in NATIVE_VISION_REVIEW_STAGES
                        else []
                    ),
                    "- 完成后用简短中文总结实际写入/修改的文件。",
                    "",
                    "## 任务正文",
                    "",
                    prompt,
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return prompt_path

    def _run_codex_exec(self, stage: str, prompt_path: Path, images: list[Path]) -> StageResult:
        log_dir = self.context.paths.status / "agent_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        output_path = log_dir / f"{int(time.time())}_{stage}_codex_last_message.md"
        log_path = log_dir / f"{int(time.time())}_{stage}_codex_exec.log"
        prompt_text = prompt_path.read_text(encoding="utf-8")
        command = [
            self.context.codex_path,
            "-a",
            self.context.codex_approval,
            "exec",
            "--cd",
            str(ROOT),
            "--sandbox",
            self.context.codex_sandbox,
            "--add-dir",
            str(self.context.project_dir),
            "--skip-git-repo-check",
            "--output-last-message",
            str(output_path),
        ]
        if stage in NATIVE_VISION_REVIEW_STAGES and images:
            # `--ignore-user-config` is an exec subcommand option.  It keeps
            # this isolated to visual review calls while preserving user
            # config for imagegen, Suno, browser and production workers.
            command.insert(4, "--ignore-user-config")
        _role, selected_model, selected_reasoning = self.context.codex_route(stage)
        if selected_model:
            command.extend(["--model", selected_model])
        if selected_reasoning:
            command.extend(["--config", f'model_reasoning_effort="{selected_reasoning}"'])
        # `--image <FILE>...` is variadic in Codex CLI, so the positional prompt
        # must come before it or the prompt is consumed as another image path.
        command.append(prompt_text)
        for image in images:
            if image.exists():
                command.extend(["--image", str(image)])
        display_command = list(command)
        display_command[display_command.index(prompt_text)] = "<prompt>"
        env = normalized_subprocess_environment(overrides=self._module_subprocess_env())
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.time() + max(30, self.context.codex_timeout)
        stdout = ""
        stderr = ""
        while True:
            try:
                stdout, stderr = process.communicate(timeout=min(5, max(0.1, deadline - time.time())))
                break
            except subprocess.TimeoutExpired:
                if self._heartbeat_and_cancelled():
                    process.terminate()
                    try:
                        stdout, stderr = process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        stdout, stderr = process.communicate()
                    log_path.write_text(
                        "COMMAND:\n" + " ".join(display_command) + "\n\n[cancelled]\n" + "\n[stdout]\n" + stdout + "\n[stderr]\n" + stderr,
                        encoding="utf-8",
                    )
                    return StageResult("cancelled", f"Codex CLI 子任务已按取消请求停止：{log_path}", log_path)
                if time.time() >= deadline:
                    process.terminate()
                    try:
                        stdout, stderr = process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        stdout, stderr = process.communicate()
                    log_path.write_text(
                        "COMMAND:\n"
                        + " ".join(display_command)
                        + f"\n\n[timed out after {self.context.codex_timeout}s]\n"
                        + "\n[stdout]\n"
                        + stdout
                        + "\n[stderr]\n"
                        + stderr,
                        encoding="utf-8",
                    )
                    return StageResult("failed", f"Codex CLI 子任务超时：{log_path}", log_path)
        log_path.write_text(
            "COMMAND:\n"
            + " ".join(display_command)
            + "\n\n[stdout]\n"
            + (stdout or "")
            + "\n[stderr]\n"
            + (stderr or ""),
            encoding="utf-8",
        )
        self._record_codex_usage(
            stage=stage,
            model=selected_model,
            reasoning_effort=selected_reasoning,
            stdout=stdout,
            stderr=stderr,
            return_code=int(process.returncode or 0),
        )
        if process.returncode != 0:
            status = classify_command_failure((stdout or "") + "\n" + (stderr or ""))
            return StageResult(status, f"Codex CLI 子任务失败：{log_path}", log_path)
        return StageResult("done", str(log_path), output_path)

    def _record_codex_usage(
        self,
        *,
        stage: str,
        model: str,
        reasoning_effort: str,
        stdout: str,
        stderr: str,
        return_code: int,
    ) -> None:
        combined = (stdout or "") + "\n" + (stderr or "")
        matches = re.findall(r"(?i)tokens?\s+used\s*[:：]?\s*([0-9][0-9,]*)", combined)
        total_tokens = int(matches[-1].replace(",", "")) if matches else None
        append_ndjson(
            self.context.paths.status / "codex_usage.ndjson",
            {
                "kind": "story_codex_usage_v1",
                "timestamp": now(),
                "stage": stage,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "total_tokens": total_tokens,
                "usage_status": "reported" if total_tokens is not None else "not_reported_by_cli",
                "return_code": return_code,
            },
        )

    def _codex_stage_dir(self, stage: str) -> Path:
        # Staging is project-local so a changed Codex worktree can never make
        # completed story assets appear missing or bind a manifest to another
        # checkout. Historical ROOT/output staging remains untouched evidence.
        path = self.context.paths.status / "agent_work" / stage
        if not self.read_only:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def _sync_story_images_from_staging(self, staging: Path, staging_storyboard: Path, staging_images: Path) -> None:
        final_images = self.context.paths.images / "images"
        final_storyboard = self.context.paths.images / f"{self.context.slug}_storyboard_lines.txt"
        if staging_storyboard.exists():
            final_storyboard.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staging_storyboard, final_storyboard)
        if staging_images.exists():
            image_files = sorted(path for path in staging_images.iterdir() if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})
            if image_files:
                final_images.mkdir(parents=True, exist_ok=True)
                for path in image_files:
                    shutil.copy2(path, final_images / path.name)
        for suffix in ("visual_bible.md", "storyboard_plan.json"):
            source = staging / f"{self.context.slug}_{suffix}"
            if source.exists():
                final_storyboard.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, final_storyboard.parent / source.name)
        # The storyboard-image skill writes hand-authored motion prompts and
        # clip naming tables next to the staged stills.  Keep these control
        # artifacts with the final images so prepare never falls back to a
        # generic action prompt after a successful image batch.
        control_patterns = (
            "*_flow_video_prompts.csv",
            "*_flow_video_prompts.md",
            "*_flow_clip_names.csv",
        )
        control_sources = [
            source
            for root in (staging_images, staging)
            if root.exists()
            for pattern in control_patterns
            for source in root.glob(pattern)
            if source.is_file()
        ]
        for source in control_sources:
            final_storyboard.parent.mkdir(parents=True, exist_ok=True)
            target = final_storyboard.parent / source.name
            if source.resolve() != target.resolve():
                shutil.copy2(source, target)

    def _ensure_storyboard_from_lines(self, storyboard: Path, story_lines: list[str]) -> bool:
        expected = "\n".join(story_lines) + "\n"
        current = storyboard.read_text(encoding="utf-8") if storyboard.exists() else ""
        if current == expected:
            return False
        storyboard.parent.mkdir(parents=True, exist_ok=True)
        storyboard.write_text(expected, encoding="utf-8")
        return bool(current)

    def _invalidate_story_image_derivatives(
        self,
        staging_images: Path,
        *,
        additional_staging_images: list[Path] | None = None,
        reason: str = "authoritative_storyboard_changed",
    ) -> Path:
        """Archive stale scene outputs with source/target hashes; never delete them."""

        quarantine_root = self.context.paths.status / "rejected" / "story_images"
        quarantine = quarantine_root / time.strftime("%Y%m%d-%H%M%S")
        if quarantine.exists():
            quarantine = quarantine.with_name(f"{quarantine.name}-{uuid.uuid4().hex[:8]}")
        quarantine.mkdir(parents=True, exist_ok=False)
        final_images = self.context.paths.images / "images"
        candidates: list[Path] = []
        staging_roots = [staging_images]
        for raw in additional_staging_images or []:
            resolved = raw.expanduser().resolve()
            if all(resolved != existing.expanduser().resolve() for existing in staging_roots):
                staging_roots.append(resolved)
        for directory in (*staging_roots, final_images):
            if directory.exists():
                candidates.extend(directory.glob(f"{self.context.slug}_scene_*.png"))
                candidates.extend(directory.glob(f"{self.context.slug}_scene_*.jpg"))
                candidates.extend(directory.glob(f"{self.context.slug}_scene_*.webp"))
        control_roots = (
            *(root for staging_root in staging_roots for root in (staging_root, staging_root.parent)),
            self.context.paths.images,
        )
        candidates.extend(
            path
            for root in control_roots
            if root.exists()
            for pattern in (
                f"{self.context.slug}_flow_video_prompts.csv",
                f"{self.context.slug}_flow_video_prompts.md",
                f"{self.context.slug}_flow_clip_names.csv",
            )
            for path in root.glob(pattern)
        )
        for root in staging_roots:
            candidates.extend(root.parent.glob(f"{self.context.slug}_storyboard.md"))
            candidates.extend(root.parent.glob(f"{self.context.slug}_visual_bible.md"))
            candidates.extend(root.parent.glob(f"{self.context.slug}_storyboard_plan.json"))
        candidates.append(self._story_image_generation_manifest_path())
        seen: set[Path] = set()
        archived: list[dict[str, Any]] = []
        for source in candidates:
            resolved = source.resolve()
            if resolved in seen or not source.exists():
                continue
            seen.add(resolved)
            scope = "final"
            for index, root in enumerate(staging_roots, start=1):
                if root.resolve() in resolved.parents or resolved == root.resolve():
                    scope = "staging" if index == 1 else f"staging_{index:02d}"
                    break
            target = quarantine / f"{scope}_{source.name}"
            counter = 1
            while target.exists():
                target = quarantine / f"{scope}_{counter}_{source.name}"
                counter += 1
            digest = file_sha256(source) if source.is_file() else ""
            size = source.stat().st_size if source.is_file() else 0
            shutil.move(str(source), str(target))
            archived.append(
                {
                    "source": str(source),
                    "target": str(target),
                    "sha256": digest,
                    "bytes": size,
                    "scope": scope,
                }
            )
        save_json(
            quarantine / "archive_manifest.json",
            {
                "kind": "story_image_archive_v1",
                "created_at": now(),
                "project_dir": str(self.context.project_dir.resolve()),
                "slug": self.context.slug,
                "reason": reason,
                "destructive_delete_performed": False,
                "items": archived,
            },
        )
        return quarantine

    def _story_image_filename(self, index: int) -> str:
        return f"{self.context.slug}_scene_{index:02d}.png"

    def _story_images_batch_prompt(
        self,
        *,
        handoff: Path,
        staging_images: Path,
        staging_storyboard: Path,
        story_lines: list[str],
        indices: list[int],
    ) -> str:
        retry_lines: list[str] = []
        review_path = self.context.paths.status / "reviews" / "story_images_review_review.json"
        if review_path.exists():
            try:
                review_payload = json.loads(review_path.read_text(encoding="utf-8"))
                instructions = review_payload.get("retry_instructions", {}) if isinstance(review_payload, dict) else {}
                if isinstance(instructions, dict):
                    retry_lines = [
                        f"- 镜头 {index}：{instructions.get(str(index), instructions.get(index, ''))}"
                        for index in indices
                        if instructions.get(str(index), instructions.get(index, ""))
                    ]
                elif isinstance(instructions, list):
                    for item in instructions:
                        if isinstance(item, dict):
                            scene = item.get("scene")
                            instruction = str(item.get("instruction") or "").strip()
                            if scene in indices and instruction:
                                retry_lines.append(f"- 第{scene}镜：{instruction}")
                            continue
                        if not isinstance(item, str):
                            continue
                        normalized = item.strip()
                        for index in indices:
                            if normalized.startswith((f"第{index}镜", f"镜头{index}", f"镜头 {index}")):
                                retry_lines.append(f"- {normalized}")
                                break
            except (OSError, json.JSONDecodeError):
                retry_lines = []
        lines = [
                "请执行这份由工作台同源模板生成的儿童故事出图任务：",
                f"`{handoff}`",
                "",
                "这是全自动 Agent 模式，不需要向用户确认分镜。状态机已经写好并锁定分镜文本；必须只读使用该文件，绝对不得改写、合并、删减或重排任何一行。",
                "可以创建或更新视觉圣经和图生视频提示词文件，然后连续生成图片；镜头编号必须逐行对应锁定分镜。",
                f"必须先写入机器可读分镜计划：`{staging_images.parent / (self.context.slug + '_storyboard_plan.json')}`。每镜包含 scene、story_text、narrative_function、shot_size、focal_character、visible_characters、excluded_characters、continuity_group、appearance_ids、visual_description、speaker、listener、narrative_focus、emotion、shot_intent、transition_reason、location_state、character_knowledge、required_visible_actions、state_transition_evidence；story_text 必须逐行等于锁定分镜。无说话者/听话者时写 none。",
                "镜头选择必须依据人物关系、说话者/听话者、情绪变化和叙事重点；不机械地逢对白就正反打，也不得让整段对白始终保持同一多人全景。",
                "连续发生且原文没有转场的镜头必须沿用同一个 continuity_group、location_state.location_id 和 time_of_day；若确有地点或时间变化，change_from_previous=true 且 change_cue 必须逐字指出原文中的转场依据，禁止为了画面多样性擅自换到室内、黄昏或另一地点。",
                "character_knowledge 必须逐一记录可见角色的 aware_of、unaware_of 和 gaze_target。角色在发现某事之前不得看向、回应或配合它；偷吃、躲藏、误会、秘密等信息差要同时约束静帧构图和图生视频表演。",
                "required_visible_actions 用对象数组记录 machine_id、action、subject、object、visibility。合同中 must_show_action=true 的迁移，当前镜必须把触发动作明确画在画面里，不能只画动作后的结果，也不能把动作前状态与目的地结果揉成一张图。state_transition_evidence 必须逐状态机记录 from、to、visibility、evidence。",
                "机器可读分镜计划的顶层还必须原样记录合同请求清单中的 contract_schema_version、story_contract_sha256、story_contract_dependency_sha256 和 contract_projection，并把逐镜列表放在 shots 字段；contract_projection 不得删减、改写或用模型推断覆盖。",
                "机器可读分镜计划还必须原样记录当前逐产物语义呈现计划的 artifact_semantic_plan_sha256、artifact_semantic_plan_schema_version、artifact_semantic_plan_dependency_sha256；缺失或旧绑定将被 Runtime 拒绝。",
                "机器可读分镜计划还必须原样记录 visual_sample_schema_version、visual_sample_plan_sha256、visual_sample_review_bundle_sha256、visual_sample_lock_sha256；旧小样或旧审核绑定将被 Runtime 拒绝。",
                "每镜必须记录 scale_basis、current_story_state、visual_state_evidence。scale_basis 必须说明是否适用、引用合同 relationship_id 或说明不适用原因；有状态机时必须逐 machine_id 记录当前 state_id 及可见/不可见证据。",
                "每镜还必须写机器可读视频动作字段 subject_action、environment_motion、camera_motion、entry_state、exit_state、screen_direction、adjacent_handoff、expected_motion。顶层 screen_direction 只能是 left_to_right、right_to_left、toward_camera、away_from_camera、stationary、mixed 之一，不得写自由文本。expected_motion.primary 只能是 subject/environment/camera/quiet，并分别声明 subject/environment/camera 的 none/low/moderate/high 预期和理由；adjacent_handoff 必须显式包含 boolean 类型的 allows_direction_change 与 allows_state_transition，相邻镜头用同一 handoff 标识承接 entry/exit、视线与移动方向。连续性用于稳定身份、状态、尺度和故事逻辑，不能靠完全静止逃避动作。",
                "不得丢弃、缩写或覆盖合同角色、风格、尺度、状态约束；不得擅自新增会成为跨镜头身份锚点的特殊标记、固定配饰、徽记，或违反合同/角色设定的非意图结构。",
                "允许不违背合同的正常人体/动物结构、时代和场景合理普通服饰及非身份性自然细节，但推断细节不得升级为永久身份锚点；合同 required/forbidden 始终优先。",
                "每个唱歌、关键发言、关键动作或明显受挫的角色都要获得焦点镜头；连续场景要安排建立全景、表演者中近景、反应镜头等景别变化，不能所有角色都和主角挤在同一种双人中景。",
                "为反复出现的角色固定 appearance_id；生成后续镜头时必须同时引用风格锚点和该角色最近一张已通过图片，禁止只靠文字重新随机生成角色。",
                "图生视频提示词文件的 CSV 必须包含 `scene,story_text,visual_description,prompt`；`prompt` 要作为后续图生视频 API 和审核页直接使用的最终提示词。图生视频已经有当前图片作为视觉约束，只写具体动作、表情、道具运动、镜头运动和少量禁止项，不要复制文生图视觉圣经、服装细节或画风长描述，也不能用“角色动作自然克制、镜头缓慢推进或轻移”之类通用模板充数。",
                "",
                f"本轮应补齐这些镜头编号：{', '.join(str(i) for i in indices)}。",
                "若目标镜头 PNG 已存在且只是视觉圣经/分镜计划缺失，不要重新生图；读取现有图片补齐控制文件即可。",
                f"图片暂存目录：`{staging_images}`",
                f"分镜文本暂存：`{staging_storyboard}`",
                f"分镜文件当前 SHA-256：`{file_sha256(staging_storyboard)}`；完成返回前必须保持完全不变。",
        ]
        if retry_lines:
            lines.extend(["", "这是独立视觉审核给出的强制重做要求，必须逐条落实：", *retry_lines])
        lines.extend(
            [
                "请使用任务书指定的稳定命名 `{slug}_scene_XX.png`，不要使用旧的 `{slug}_XX.png` 命名。",
                "如果 imagegen 默认保存到 `$CODEX_HOME/generated_images/...`，生成后把每张最终 PNG 复制到任务书指定的暂存目录。",
                "完成后只用简短中文说明生成成功的文件路径，以及任何未能完成的镜头编号。",
            ]
        )
        style_references = self._approved_style_reference_paths()
        if style_references:
            lines.extend(
                [
                    "",
                    "以下是用户确认的整体风格参考。每次 ImageGen 必须将它们中的可爱 3D 动画感与已审核视觉小样一起作为参考，不复制具体角色或构图：",
                    *[f"- `{path}`" for path in style_references],
                ]
            )
        context_path = contract_consumer_path(self.context.project_dir, "storyboard_images")
        if context_path.is_file() and not self._legacy_contract_policy(self._manifest()):
            try:
                projection = json.loads(context_path.read_text(encoding="utf-8"))["contract_projection"]
            except (OSError, KeyError, json.JSONDecodeError):
                projection = None
            if isinstance(projection, dict):
                lines.extend([
                    "", "以下是本轮最终生产指令必须完整遵守的五类合同投影：",
                    "```json", json.dumps(projection, ensure_ascii=False, indent=2, sort_keys=True), "```",
                ])
        if not self._legacy_contract_policy(self._manifest()):
            source = self._artifact_semantic_source(self._manifest())
            if source is not None:
                try:
                    plan_path = self.context.paths.status / "story_contract" / "artifact_semantic_plan.json"
                    plan = load_current_artifact_semantic_plan(self.context.project_dir, source)
                    lines.extend([
                        "", "以下三个逐产物语义计划绑定字段必须原样写入 storyboard plan 顶层：",
                        "```json", json.dumps(artifact_semantic_plan_binding(plan_path, plan), ensure_ascii=False, indent=2, sort_keys=True), "```",
                    ])
                except (OSError, ValueError, KeyError, TypeError):
                    lines.extend(["", "ERROR: 当前逐产物语义计划无效，禁止继续生成分镜计划。"])
            try:
                context = contract_consumer_path(self.context.project_dir, "storyboard_images")
                sample_plan = load_current_visual_sample_plan(self.context.project_dir, context)
                sample_paths = visual_sample_paths(self.context.project_dir)
                lines.extend(
                    [
                        "",
                        "以下是已通过审核的条件式视觉小样计划、资产与强制绑定。必须作为真实出图参考，不得仅抄字段：",
                        "```json",
                        json.dumps(
                            {
                                "binding": visual_sample_binding(self.context.project_dir),
                                "requirements": sample_plan["requirements"],
                                "identity_expansion_policy": sample_plan["identity_expansion_policy"],
                                "lock": str(sample_paths["lock"]),
                            },
                            ensure_ascii=False,
                            indent=2,
                            sort_keys=True,
                        ),
                        "```",
                    ]
                )
            except (OSError, ValueError, KeyError, TypeError):
                lines.extend(["", "ERROR: 当前视觉小样锁无效，禁止生成正式图片。"])
        return "\n".join(lines)

    def _approved_style_reference_paths(self) -> list[Path]:
        """Return user-approved project-local style references in stable order."""

        directory = self.context.paths.status / "style_references"
        if not directory.is_dir():
            return []
        return sorted(
            path.resolve()
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        )

    def _story_image_generation_manifest_path(self) -> Path:
        return self.context.paths.status / "story_images_generation_manifest.json"

    def _story_image_binding_storyboard(self, fallback: Path | None = None) -> Path | None:
        """Return the authoritative storyboard path used by image-generation receipts.

        Production writes the lineage receipt against the immutable staging storyboard,
        then copies that storyboard into the project.  Completion/status checks must use
        the same authoritative path; comparing the receipt to the copied destination path
        makes an otherwise identical batch permanently stale and causes a DAG hot loop.
        """

        staging_storyboard = (
            self._codex_stage_dir("codex_story_images")
            / f"{self.context.slug}_storyboard_lines.txt"
        )
        return staging_storyboard if staging_storyboard.is_file() else fallback

    def _story_image_generation_binding(self, context: Path, storyboard: Path) -> dict[str, Any]:
        sample_lock = visual_sample_paths(self.context.project_dir)["lock"]
        if not context.is_file() or not storyboard.is_file() or not sample_lock.is_file():
            raise OSError("story image generation binding inputs are incomplete")
        return {
            "contract_request_path": str(context),
            "contract_request_sha256": file_sha256(context),
            "visual_sample_lock_path": str(sample_lock),
            "visual_sample_lock_sha256": file_sha256(sample_lock),
            "authoritative_storyboard_path": str(storyboard),
            "authoritative_storyboard_sha256": file_sha256(storyboard),
        }

    def _story_image_generation_context_current(self, context: Path, storyboard: Path) -> bool:
        try:
            payload = json.loads(self._story_image_generation_manifest_path().read_text(encoding="utf-8"))
            expected = self._story_image_generation_binding(context, storyboard)
        except (OSError, json.JSONDecodeError):
            return False
        if payload.get("version") != 1 or any(payload.get(key) != value for key, value in expected.items()):
            return False
        images = payload.get("images")
        if not isinstance(images, dict):
            return False
        final_images = self.context.paths.images / "images"
        staging_images = self._codex_stage_dir("codex_story_images") / "images"
        for name, item in images.items():
            if not isinstance(item, dict) or Path(name).name != name:
                return False
            expected_sha = str(item.get("sha256") or "")
            final_path = final_images / name
            staging_path = staging_images / name
            if (
                len(expected_sha) != 64
                or not final_path.is_file()
                or not staging_path.is_file()
                or file_sha256(final_path) != expected_sha
                or file_sha256(staging_path) != expected_sha
            ):
                return False
        return True

    def _write_story_image_generation_manifest(
        self,
        context: Path,
        storyboard: Path,
        story_lines: list[str],
    ) -> Path:
        payload: dict[str, Any] = {
            "version": 1,
            **self._story_image_generation_binding(context, storyboard),
            "images": {},
        }
        final_images = self.context.paths.images / "images"
        staging_images = self._codex_stage_dir("codex_story_images") / "images"
        for index in range(1, len(story_lines) + 1):
            name = self._story_image_filename(index)
            final_path = final_images / name
            staging_path = staging_images / name
            if not final_path.is_file() or not staging_path.is_file():
                continue
            final_sha = file_sha256(final_path)
            if file_sha256(staging_path) != final_sha:
                continue
            payload["images"][name] = {"sha256": final_sha, "bytes": final_path.stat().st_size}
        target = self._story_image_generation_manifest_path()
        save_json(target, payload)
        return target

    def _story_image_generation_complete(
        self,
        manifest: dict[str, Any],
        context: Path,
        storyboard: Path,
    ) -> bool:
        if not self._story_image_generation_context_current(context, storyboard):
            return False
        try:
            payload = json.loads(self._story_image_generation_manifest_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        expected = self._expected_story_image_count(manifest, storyboard)
        images = payload.get("images", {})
        return expected > 0 and all(
            self._story_image_filename(index) in images for index in range(1, expected + 1)
        )

    def _missing_story_image_indices(self, story_lines: list[str]) -> list[int]:
        final_images = self.context.paths.images / "images"
        missing = [index for index in range(1, len(story_lines) + 1) if not (final_images / self._story_image_filename(index)).exists()]
        if missing:
            return missing
        # Use one lightweight Codex turn to repair missing control artifacts;
        # existing final images are references and must not be regenerated.
        return [1] if not self._has_story_visual_control() else []

    def _has_story_visual_control(self) -> bool:
        staging = self._codex_stage_dir("codex_story_images")
        plan = staging / f"{self.context.slug}_storyboard_plan.json"
        storyboard = staging / f"{self.context.slug}_storyboard_lines.txt"
        return (
            (staging / f"{self.context.slug}_visual_bible.md").exists()
            and self._storyboard_plan_valid(plan, storyboard)
        )

    def _storyboard_plan_valid(self, plan: Path, storyboard: Path) -> bool:
        if not plan.exists() or not storyboard.exists():
            return False
        try:
            payload = json.loads(plan.read_text(encoding="utf-8"))
            rows = payload.get("shots", []) if isinstance(payload, dict) else payload
            story_lines = [line.strip() for line in storyboard.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(rows, list) or len(rows) != len(story_lines):
            return False
        if not self._legacy_contract_policy(self._manifest()):
            try:
                expected = json.loads(
                    contract_consumer_path(self.context.project_dir, "storyboard_images").read_text(encoding="utf-8")
                )
                semantic_source = self._artifact_semantic_source(self._manifest())
                if semantic_source is None:
                    return False
                semantic_plan_path = self.context.paths.status / "story_contract" / "artifact_semantic_plan.json"
                semantic_plan = load_current_artifact_semantic_plan(self.context.project_dir, semantic_source)
                semantic_binding = artifact_semantic_plan_binding(semantic_plan_path, semantic_plan)
                sample_context = contract_consumer_path(self.context.project_dir, "storyboard_images")
                if not visual_sample_lock_is_current(self.context.project_dir, sample_context):
                    return False
                sample_binding = visual_sample_binding(self.context.project_dir)
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                return False
            if not isinstance(payload, dict) or any(
                payload.get(field) != expected.get(field)
                for field in (
                    "contract_schema_version",
                    "story_contract_sha256",
                    "story_contract_dependency_sha256",
                )
            ):
                return False
            expected_projection = expected.get("contract_projection")
            if not isinstance(expected_projection, dict) or payload.get("contract_projection") != expected_projection:
                return False
            if any(payload.get(field) != value for field, value in semantic_binding.items()):
                return False
            if any(payload.get(field) != value for field, value in sample_binding.items()):
                return False
            projection = expected_projection
            scale_rows = projection.get("world_scale", {}).get("relationships", [])
            scale_ids = {
                str(item.get("relationship_id")) for item in scale_rows if isinstance(item, dict)
            }
            state_rows = projection.get("story_state", {}).get("machines", [])
            allowed_states = {
                str(item.get("machine_id")): {
                    str(state.get("state_id"))
                    for state in item.get("states", [])
                    if isinstance(state, dict)
                }
                for item in state_rows
                if isinstance(item, dict)
            }
        required = {
            "scene", "story_text", "narrative_function", "shot_size", "focal_character",
            "visible_characters", "excluded_characters", "continuity_group", "appearance_ids", "visual_description",
            "scale_basis", "current_story_state", "visual_state_evidence",
        }
        director_required = {
            "speaker", "listener", "narrative_focus", "emotion", "shot_intent", "transition_reason",
        }
        motion_required = {
            "subject_action", "environment_motion", "camera_motion", "entry_state", "exit_state",
            "screen_direction", "adjacent_handoff", "expected_motion",
        }
        continuity_required = {
            "location_state", "character_knowledge", "required_visible_actions", "state_transition_evidence",
        }
        for index, (row, text) in enumerate(zip(rows, story_lines), start=1):
            if not isinstance(row, dict) or not required.issubset(row):
                return False
            try:
                scene = int(row["scene"])
            except (TypeError, ValueError):
                return False
            if scene != index or str(row["story_text"]).strip() != text:
                return False
            if not str(row["shot_size"]).strip() or not str(row["focal_character"]).strip():
                return False
            if not self._legacy_contract_policy(self._manifest()):
                if (
                    not motion_required.issubset(row)
                    or not director_required.issubset(row)
                    or not continuity_required.issubset(row)
                ):
                    return False
                if any(not str(row.get(key) or "").strip() for key in director_required):
                    return False
                scale_basis = row.get("scale_basis")
                if not isinstance(scale_basis, dict) or not isinstance(scale_basis.get("applicable"), bool):
                    return False
                relationship_ids = scale_basis.get("relationship_ids", [])
                if not isinstance(relationship_ids, list) or any(str(value) not in scale_ids for value in relationship_ids):
                    return False
                if scale_basis["applicable"] and (not relationship_ids or not str(scale_basis.get("evidence") or "").strip()):
                    return False
                if not scale_basis["applicable"] and not str(scale_basis.get("reason") or "").strip():
                    return False
                current_state = row.get("current_story_state")
                state_evidence = row.get("visual_state_evidence")
                if not isinstance(current_state, dict) or not isinstance(state_evidence, dict):
                    return False
                if set(current_state) != set(allowed_states) or set(state_evidence) != set(allowed_states):
                    return False
                if any(str(current_state[key]) not in values for key, values in allowed_states.items()):
                    return False
                if any(not str(state_evidence[key]).strip() for key in allowed_states):
                    return False
                if any(not str(row.get(key) or "").strip() for key in ("subject_action", "environment_motion", "camera_motion")):
                    return False
                if row.get("screen_direction") not in {
                    "left_to_right", "right_to_left", "toward_camera", "away_from_camera", "stationary", "mixed",
                }:
                    return False
                if any(not isinstance(row.get(key), dict) for key in ("entry_state", "exit_state", "adjacent_handoff", "expected_motion")):
                    return False
                expected_motion = row["expected_motion"]
                if expected_motion.get("primary") not in {"subject", "environment", "camera", "quiet"}:
                    return False
                if any(expected_motion.get(key) not in {"none", "low", "moderate", "high"} for key in ("subject_level", "environment_level", "camera_level")):
                    return False
                if not str(expected_motion.get("rationale") or "").strip():
                    return False
        if not self._legacy_contract_policy(self._manifest()):
            if storyboard_continuity_issues(rows, state_rows):
                return False
        return True

    def _visual_continuity_contract_path(self) -> Path | None:
        candidate = self.context.paths.status / "visual_continuity_contract.json"
        return candidate if candidate.is_file() else None

    def _visual_continuity_storyboard_errors(self, contract_path: Path | None, storyboard_plan: Path) -> list[str]:
        """Validate contract-declared storyboard state fields before review.

        The contract is optional for legacy projects.  When present, a
        ``required_field`` is a machine-readable per-shot enum requirement;
        missing/invalid values are surfaced as critical blockers rather than
        relying on a reviewer prompt alone.
        """

        if contract_path is None:
            return []
        try:
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return [f"合同不可读：{exc}"]
        if not isinstance(contract, dict):
            return ["合同必须是 JSON 对象"]
        requirements = contract.get("storyboard_requirements")
        if not isinstance(requirements, dict):
            return []
        required_field = str(requirements.get("required_field") or "").strip()
        if not required_field:
            return []
        if not storyboard_plan.is_file():
            return [f"storyboard_plan 缺失，无法提供合同要求字段 {required_field}"]
        try:
            payload = json.loads(storyboard_plan.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return [f"storyboard_plan 不可读：{exc}"]
        rows = payload.get("shots", []) if isinstance(payload, dict) else payload
        if not isinstance(rows, list) or not rows:
            return ["storyboard_plan 没有可校验的逐镜列表"]
        allowed = contract.get("allowed_states")
        allowed_states = {str(item).strip() for item in allowed if str(item).strip()} if isinstance(allowed, list) else set()
        errors: list[str] = []
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                errors.append(f"第 {index} 镜不是对象")
                continue
            value = row.get(required_field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"第 {index} 镜缺少 {required_field}")
            elif allowed_states and value.strip() not in allowed_states:
                errors.append(f"第 {index} 镜 {required_field}={value!r} 不属于允许枚举")
        return errors

    def _record_story_image_status(self, index: int, status: str, message: str) -> None:
        progress = self.state.setdefault("codex_story_images", {})
        images = progress.setdefault("images", {})
        images[f"{index:02d}"] = {"status": status, "message": message, "time": now()}
        progress["updated_at"] = now()
        self._save_state()

    def _release_preview_images(self) -> list[Path]:
        preview_dir = self.context.paths.status / "release_preview_frames"
        if not preview_dir.exists():
            return []
        selected: list[Path] = []
        contact = preview_dir / "preview_contact_sheet.png"
        if contact.exists():
            selected.append(contact)
        # Explicitly hand the independent reviewer one representative of each
        # deterministic A/B/C layout plus library and the latest A frame (often
        # the maximum gesture).  Do not depend on historical h84/h90 names.
        for pattern in ("main_*_a*.png", "main_*_b.png", "main_*_c.png", "library_*.png"):
            matches = sorted(preview_dir.glob(pattern))
            if matches:
                selected.append(matches[0])
                if pattern == "main_*_a*.png" and matches[-1] != matches[0]:
                    selected.append(matches[-1])
        return list(dict.fromkeys(selected))[:8]

    def _make_contact_sheet(self, sources: list[Path], target: Path, *, columns: int = 4) -> Path:
        valid: list[tuple[Path, Image.Image]] = []
        for source in sources:
            try:
                image = Image.open(source).convert("RGB")
                image.thumbnail((320, 180))
                valid.append((source, image.copy()))
            except Exception:
                continue
        if not valid:
            raise RuntimeError("没有可用于审核总览的图片。")
        cell_width, cell_height = 340, 215
        rows = (len(valid) + columns - 1) // columns
        sheet = Image.new("RGB", (columns * cell_width, rows * cell_height), "white")
        draw = ImageDraw.Draw(sheet)
        for index, (source, image) in enumerate(valid):
            x = (index % columns) * cell_width + 10
            y = (index // columns) * cell_height + 10
            sheet.paste(image, (x, y))
            label = f"{source.parent.name}/{source.name}" if source.parent.name else source.name
            draw.text((x, y + 184), label[:45], fill="black")
        target.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(target, quality=88)
        return target

    def _probe_duration(self, path: Path) -> float:
        process = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            text=True,
            capture_output=True,
            env=normalized_subprocess_environment(),
        )
        try:
            duration = float(process.stdout.strip())
        except ValueError as exc:
            raise RuntimeError(f"无法读取媒体时长：{path}") from exc
        if process.returncode != 0 or duration <= 0:
            raise RuntimeError(f"媒体时长异常：{path}")
        return duration

    def _structured_review(
        self,
        *,
        stage: str,
        label: str,
        bundle: Path,
        images: list[Path],
        rubric: str,
    ) -> tuple[StageResult, dict[str, Any] | None]:
        review_path = self.context.paths.status / "reviews" / f"{stage}_review.json"
        bundle_sha = file_sha256(bundle)
        prompt = "\n".join(
            [
                "你是独立质量审核员。只审核，不修改生产产物，不读取生产者推理或旧审核结论。",
                f"审核清单及当前产物哈希：`{bundle}`",
                rubric,
                "评分 0–100；低于 85 或存在关键错误时 approved 必须为 false。",
                f"把审核 JSON 写入：`{review_path}`",
                "JSON 必须包含 approved、score、critical_errors、issues、retry_indices、retry_files、retry_instructions、artifact_sha256。",
                "对视觉/连续性审核，JSON 还必须包含 evidence_matrix；每条结论要引用实际文件名或时间戳，禁止无证据自述通过。",
                f"artifact_sha256 必须原样写为：{bundle_sha}",
            ]
        )
        payload: dict[str, Any] | None = None
        try:
            existing = json.loads(review_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and existing.get("artifact_sha256") == bundle_sha:
                reusable = True
                if stage == "story_images_review" and not self._legacy_contract_policy(self._manifest()):
                    reusable = not self._story_image_quality_review_issues(existing)
                if reusable:
                    payload = existing
        except (OSError, json.JSONDecodeError):
            pass
        if payload is None:
            result = self._codex_task(stage=stage, label=label, handoff=bundle, prompt=prompt, images=images)
            if result.status != "done":
                return result, None
            try:
                payload = json.loads(review_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return StageResult("blocked", f"{label}未写入有效 JSON：{review_path}", bundle), None
        if review_passes(payload, artifact=bundle):
            return StageResult("done", f"{label}通过：{payload.get('score')} 分", review_path), payload
        return StageResult("blocked", f"{label}未通过：{payload.get('score', 0)} 分；{review_path}", review_path), payload

    def _review_stage_current(self, stage: str) -> bool:
        review_dir = self.context.paths.status / "reviews"
        bundle = review_dir / f"{stage.removesuffix('_review')}_bundle.json"
        if stage == "story_images_review":
            bundle = review_dir / "story_images_bundle.json"
        elif stage == "video_review":
            bundle = review_dir / "video_bundle.json"
        review = review_dir / f"{stage}_review.json"
        if not bundle.exists() or not review.exists() or not review_bundle_is_current(bundle):
            return False
        try:
            payload = json.loads(review.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return review_passes(payload, artifact=bundle)

    def _review_retry_indices(self, payload: dict[str, Any]) -> list[int]:
        values = payload.get("retry_indices", [])
        if not isinstance(values, list):
            return []
        result: list[int] = []
        for value in values:
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if number > 0 and number not in result:
                result.append(number)
        return result

    def _normalize_video_retry_instructions(self, raw: Any) -> dict[str, dict[str, str]]:
        """Accept legacy mappings and current structured rows without dropping corrections."""

        normalized: dict[str, dict[str, str]] = {}
        values: list[tuple[Any, Any]] = []
        if isinstance(raw, list):
            values = [(item.get("scene", item.get("scene_index")), item) for item in raw if isinstance(item, dict)]
        elif isinstance(raw, dict):
            values = list(raw.items())
        for raw_scene, value in values:
            try:
                scene = str(int(raw_scene))
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                instruction = str(value.get("instruction") or value.get("reason") or "").strip()
                provider_prompt = str(value.get("provider_prompt") or "").strip()
                hard_defect_code = str(value.get("hard_defect_code") or "").strip()
            else:
                instruction = str(value or "").strip()
                provider_prompt = instruction
                hard_defect_code = ""
            if instruction and provider_prompt:
                normalized[scene] = {
                    "instruction": instruction,
                    "provider_prompt": provider_prompt,
                    "hard_defect_code": hard_defect_code,
                }
        return normalized

    def _video_review_retry_counts(self, indices: list[int], jobs: Path) -> dict[int, int]:
        """Return per-scene quality recovery history without imposing an artificial stop."""

        try:
            with jobs.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        except (OSError, csv.Error):
            return {scene: 0 for scene in indices}
        selected = set(indices)
        counts: dict[int, int] = {}
        for row in rows:
            try:
                scene = int(row.get("scene") or 0)
                count = int(row.get("video_review_retry_count") or 0)
            except (TypeError, ValueError):
                continue
            if scene in selected:
                counts[scene] = count
        return {scene: counts.get(scene, 0) for scene in indices}

    def _video_provider_attempt_counts(self, indices: list[int], jobs: Path) -> dict[int, int]:
        """Return paid regeneration counts so all QA paths share one spend cap."""

        try:
            with jobs.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        except (OSError, csv.Error):
            return {scene: 0 for scene in indices}
        selected = set(indices)
        counts: dict[int, int] = {}
        for row in rows:
            try:
                scene = int(row.get("scene") or 0)
                count = int(row.get("provider_attempt") or 0)
            except (TypeError, ValueError):
                continue
            if scene in selected:
                counts[scene] = max(0, count)
        return {scene: counts.get(scene, 0) for scene in indices}

    def _can_retry_stage(
        self,
        stage: str,
        *,
        critical: bool = False,
        attempts_override: int | None = None,
    ) -> bool:
        manifest = self._manifest()
        defaults = load_config().get("agent_defaults", {})
        retry_key = "max_critical_retries" if critical else "max_retries"
        if retry_key in defaults:
            # “重做 N 次”不包含首次生产，因此总质量版本上限为 N + 1。
            limit = int(defaults[retry_key]) + 1
        else:
            # 兼容旧配置：旧字段表示总尝试次数。
            limit = int(defaults.get("max_critical_attempts" if critical else "max_attempts", 3 if critical else 2))
        attempts = (
            int(attempts_override)
            if attempts_override is not None
            else int(manifest.get("agent", {}).get("stages", {}).get(stage, {}).get("attempts", 1))
        )
        return attempts < limit

    @staticmethod
    def _can_retry_contract_review(quality_attempts: int) -> bool:
        defaults = load_config().get("agent_defaults", {})
        max_revisions = max(0, int(defaults.get("contract_review_max_revisions", 1)))
        # quality_attempts includes the initial reviewed contract.  A value of
        # one therefore means the single targeted revision is still available.
        return max(1, int(quality_attempts)) <= max_revisions

    @staticmethod
    def _can_retry_visual_sample_review(request: Mapping[str, Any]) -> bool:
        defaults = load_config().get("agent_defaults", {})
        max_attempts = max(1, int(defaults.get("visual_sample_max_attempts", 3)))
        configured_receipt_limit = max(1, int(request.get("max_total_attempts") or max_attempts))
        failed_attempts = max(1, int(request.get("failed_attempt_count") or 1))
        return failed_attempts < min(max_attempts, configured_receipt_limit)

    def _quarantine_story_images(self, indices: list[int]) -> list[int]:
        image_dir = self._image_dir()
        staging_images = self._codex_stage_dir("codex_story_images") / "images"
        if image_dir is None and not staging_images.exists():
            return []
        quarantine = self.context.paths.status / "rejected" / "story_images" / time.strftime("%Y%m%d-%H%M%S")
        quarantine.mkdir(parents=True, exist_ok=True)
        moved: list[int] = []
        for index in indices:
            moved_index = False
            sources = []
            if image_dir is not None:
                sources.append(("final", image_dir / self._story_image_filename(index)))
            sources.append(("staging", staging_images / self._story_image_filename(index)))
            for scope, source in sources:
                if not source.exists():
                    continue
                target = quarantine / (source.name if scope == "final" else f"staging_{source.name}")
                shutil.move(str(source), str(target))
                moved_index = True
            if moved_index:
                moved.append(index)
        return moved

    def _archive_source_edit_attempt(self, *, preserve_media: bool = False) -> Path:
        manifest = self._manifest()
        inputs = manifest.get("inputs", {})
        outputs = manifest.get("outputs", {})
        archive = self.context.paths.status / "rejected" / "source_edit" / time.strftime("%Y%m%d-%H%M%S")
        archive.mkdir(parents=True, exist_ok=True)
        candidates = [
            first_existing(inputs.get("story_text")),
            first_existing(inputs.get("greenscreen_video")),
            first_existing(outputs.get("consumer_manuscript")),
            first_existing(outputs.get("source_edit_decisions")),
            first_existing(outputs.get("source_subtitles")),
            first_existing(self.context.paths.status / "source_edit" / "source_edit_review.json"),
        ]
        original = first_existing(inputs.get("greenscreen_video_original"))
        clean_video = first_existing(inputs.get("greenscreen_video"))
        for source in candidates:
            if source is None or source == original or (preserve_media and source == clean_video) or not source.exists():
                continue
            target = archive / source.name
            if not target.exists():
                shutil.move(str(source), str(target))
        return archive

    def _quarantine_story_videos(
        self,
        indices: list[int],
        jobs: Path,
        retry_instructions: dict[str, Any] | None = None,
        *,
        count_review_retry: bool = False,
        provider_override: str = "",
        allow_exhausted_provider_for_local_fallback: bool = False,
    ) -> list[int]:
        with jobs.open(encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            rows = list(reader)
            fieldnames = list(reader.fieldnames or [])
        selected = set(indices)
        normalized = self._normalize_video_retry_instructions(retry_instructions or {})
        prompt_changes: dict[str, dict[str, str]] = {}
        selected_rows: dict[int, dict[str, str]] = {}
        for row in rows:
            try:
                scene = int(row.get("scene", "0"))
            except ValueError:
                continue
            if scene in selected:
                selected_rows[scene] = row
        if set(selected_rows) != selected or set(map(int, normalized)) != selected:
            return []
        # Each paid source clip gets at most one automatic provider
        # regeneration. Further recovery must be local/editorial or explicitly
        # user-authorized; it may not silently spend again.
        for row in selected_rows.values():
            try:
                if (
                    int(row.get("provider_attempt") or 0) >= 1
                    and not allow_exhausted_provider_for_local_fallback
                ):
                    return []
            except (TypeError, ValueError):
                return []
        for scene in sorted(selected):
            instruction = normalized[str(scene)]
            provider_prompt = instruction["provider_prompt"].strip()
            prompt_chars = len(provider_prompt.encode("utf-16-le")) // 2
            if not instruction["instruction"].strip() or not provider_prompt or prompt_chars > 120:
                return []
            previous_sha = str(selected_rows[scene].get("provider_prompt_sha256") or "").strip()
            updated_sha = hashlib.sha256(provider_prompt.encode("utf-8")).hexdigest()
            try:
                recovery_cycle = int(selected_rows[scene].get("video_review_retry_count") or 0) + 1
            except ValueError:
                recovery_cycle = 1
            strategies = (
                "动作简化，只保留一个核心行为，避免并发动作。",
                "固定镜头并减慢主体动作，避免快速肢体变化。",
                "主体保持稳定，只做最小自然动作，优先保证结构完整。",
            )
            strategy = ""
            if previous_sha and previous_sha == updated_sha:
                strategy = strategies[min(recovery_cycle - 1, len(strategies) - 1)]
            elif count_review_retry and recovery_cycle >= 2:
                strategy = strategies[min(recovery_cycle - 2, len(strategies) - 1)]
            if strategy:
                candidate = strategy + provider_prompt
                if len(candidate.encode("utf-16-le")) // 2 > 120:
                    candidate = strategy + instruction["instruction"].strip()
                if len(candidate.encode("utf-16-le")) // 2 > 120:
                    candidate = strategy
                provider_prompt = candidate
                normalized[str(scene)]["provider_prompt"] = provider_prompt
                prompt_chars = len(provider_prompt.encode("utf-16-le")) // 2
                updated_sha = hashlib.sha256(provider_prompt.encode("utf-8")).hexdigest()
            prompt_changes[str(scene)] = {
                "previous_provider_prompt_sha256": previous_sha,
                "updated_provider_prompt_sha256": updated_sha,
                "provider_prompt_chars": str(prompt_chars),
                "provider_override": provider_override,
            }

        quarantine = self.context.paths.status / "rejected" / "story_videos" / time.strftime("%Y%m%d-%H%M%S")
        quarantine.mkdir(parents=True, exist_ok=True)
        moved: list[int] = []
        retry_fields = (
            "provider_attempt", "provider_retry_prompt", "previous_provider_prompt_sha256",
            "retry_hard_defect_code", "video_quality_provider_override", "previous_video_model",
        )
        if count_review_retry:
            retry_fields = (*retry_fields, "video_review_retry_count")
        for key in retry_fields:
            if key not in fieldnames:
                fieldnames.append(key)
        for row in rows:
            try:
                scene = int(row.get("scene", "0"))
            except ValueError:
                continue
            if scene not in selected:
                continue
            source = self.context.paths.video_jobs / "videos" / row.get("target_video_filename", "")
            if source.exists():
                shutil.move(str(source), str(quarantine / source.name))
            receipt_path = Path(str(row.get("video_receipt_path") or "")) if row.get("video_receipt_path") else None
            if receipt_path is not None and receipt_path.is_file():
                shutil.copy2(receipt_path, quarantine / receipt_path.name)
            # Preserve the rejected provider identity before clearing it. More
            # importantly, advance provider_attempt so ToAPIs receives a new
            # client_business_id even if an independent prompt review later
            # normalizes the retry prompt back to the previous wording.
            save_json(quarantine / f"scene_{scene:02d}_rejected_job.json", row)
            previous_provider_sha = str(row.get("provider_prompt_sha256") or "").strip()
            previous_video_model = str(row.get("video_model") or "").strip()
            for key in (
                "task_id", "video_url", "error", "api_response", "query_response",
                "video_source_kind", "video_provider", "video_model", "video_execution_mode",
                "production_eligible", "video_receipt_path", "video_receipt_sha256",
                "provider_prompt", "provider_prompt_chars", "provider_prompt_sha256",
                "provider_request_seconds", "client_business_id",
            ):
                if key in row:
                    row[key] = ""
            try:
                provider_attempt = int(row.get("provider_attempt") or "0")
            except ValueError:
                provider_attempt = 0
            row["provider_attempt"] = str(provider_attempt + 1)
            instruction = normalized[str(scene)]
            if instruction["instruction"] not in row.get("prompt", ""):
                row["prompt"] = row.get("prompt", "").rstrip() + " 硬伤定向修复：" + instruction["instruction"]
            row["provider_retry_prompt"] = instruction["provider_prompt"]
            row["previous_provider_prompt_sha256"] = previous_provider_sha
            row["retry_hard_defect_code"] = instruction["hard_defect_code"]
            row["video_quality_provider_override"] = provider_override
            row["previous_video_model"] = previous_video_model
            if count_review_retry:
                try:
                    review_retry_count = int(row.get("video_review_retry_count") or "0")
                except ValueError:
                    review_retry_count = 0
                row["video_review_retry_count"] = str(review_retry_count + 1)
            row["status"] = "todo"
            moved.append(scene)
        with jobs.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        qa_report = self.context.paths.status / "qa_videos_report.md"
        if qa_report.exists():
            shutil.move(str(qa_report), str(quarantine / qa_report.name))
        qa_json = self.context.paths.status / "qa_videos_report.json"
        if qa_json.exists():
            shutil.move(str(qa_json), str(quarantine / qa_json.name))
        save_json(quarantine / "retry_manifest.json", {
            "version": 2,
            "retry_indices": moved,
            "retry_instructions": normalized,
            "prompt_changes": prompt_changes,
            "jobs_csv": str(jobs),
        })
        frames_dir = self.context.paths.status / "video_review_frames"
        if frames_dir.exists():
            shutil.move(str(frames_dir), str(quarantine / frames_dir.name))
        return moved

    def _editorial_fallback_story_videos(
        self,
        indices: list[int],
        jobs: Path,
        retry_instructions: dict[str, Any],
    ) -> list[int]:
        """Replace persistently broken shots with an audited adjacent-shot extension.

        The rejected provider result is still preserved by the normal quarantine
        path.  This is an editorial omission fallback, not a claim that the local
        copy came from a paid provider.
        """

        try:
            with jobs.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        except (OSError, csv.Error):
            return []
        by_scene = {
            int(row["scene"]): row for row in rows
            if str(row.get("scene") or "").isdigit()
        }
        selected = set(indices)
        videos_dir = self.context.paths.video_jobs / "videos"
        source_archive = (
            self.context.paths.status / "editorial_fallback_sources" / time.strftime("%Y%m%d-%H%M%S")
        )
        source_archive.mkdir(parents=True, exist_ok=True)
        sources: dict[int, tuple[int, Path]] = {}
        for scene in indices:
            candidates = sorted(
                (candidate for candidate in by_scene if candidate != scene and candidate not in selected),
                key=lambda candidate: (abs(candidate - scene), candidate),
            )
            if not candidates:
                candidates = sorted(
                    (candidate for candidate in by_scene if candidate != scene),
                    key=lambda candidate: (abs(candidate - scene), candidate),
                )
            for source_scene in candidates:
                source_row = by_scene[source_scene]
                source = videos_dir / str(source_row.get("target_video_filename") or "")
                if not source.is_file():
                    continue
                archived = source_archive / f"scene_{scene:02d}_from_{source_scene:02d}{source.suffix}"
                shutil.copy2(source, archived)
                sources[scene] = (source_scene, archived)
                break
        if set(sources) != selected:
            return []

        moved = self._quarantine_story_videos(
            indices,
            jobs,
            retry_instructions,
            count_review_retry=True,
            allow_exhausted_provider_for_local_fallback=True,
        )
        if set(moved) != selected:
            return []
        with jobs.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            updated_rows = list(reader)
            fieldnames = list(reader.fieldnames or [])
        for field in (
            "editorial_fallback_source_scene", "video_source_kind", "video_provider",
            "video_model", "video_execution_mode", "production_eligible",
            "video_receipt_path", "video_receipt_sha256", "task_id", "client_business_id",
        ):
            if field not in fieldnames:
                fieldnames.append(field)
        completed: list[int] = []
        for row in updated_rows:
            try:
                scene = int(row.get("scene") or 0)
            except ValueError:
                continue
            if scene not in selected:
                continue
            source_scene, archived = sources[scene]
            target = videos_dir / str(row.get("target_video_filename") or "")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(archived, target)
            row["status"] = "downloaded"
            row["editorial_fallback_source_scene"] = str(source_scene)
            row["video_source_kind"] = "editorial_adjacent_extension"
            row["video_provider"] = "local_editorial"
            row["video_model"] = "adjacent-extension-v1"
            row["video_execution_mode"] = "production"
            row["production_eligible"] = "true"
            row["task_id"] = f"editorial-{uuid.uuid4().hex}"
            row["client_business_id"] = f"story-editorial-{uuid.uuid4().hex}"
            receipt, receipt_sha = write_video_receipt(
                jobs,
                row,
                target,
                provider="local_editorial",
                model="adjacent-extension-v1",
                source_kind="editorial_adjacent_extension",
                execution_mode="production",
                production_eligible=True,
            )
            row["video_receipt_path"] = str(receipt)
            row["video_receipt_sha256"] = receipt_sha
            completed.append(scene)
        with jobs.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(updated_rows)
        save_json(source_archive / "editorial_fallback_manifest.json", {
            "version": 1,
            "scenes": completed,
            "sources": {
                str(scene): {"source_scene": sources[scene][0], "archived_source": str(sources[scene][1])}
                for scene in completed
            },
            "jobs_csv": str(jobs),
            "reason": "provider retries exhausted; adjacent visual extended to preserve unattended completion",
        })
        return completed

    def _quarantine_publish_files(self, relative_files: list[str]) -> list[Path]:
        publish = self.context.paths.publish.resolve()
        quarantine = self.context.paths.status / "rejected" / "publish_package" / time.strftime("%Y%m%d-%H%M%S")
        moved: list[Path] = []
        for relative in relative_files:
            candidate = (publish / relative).resolve()
            try:
                candidate.relative_to(publish)
            except ValueError:
                continue
            if not candidate.exists() or not candidate.is_file():
                continue
            target = quarantine / candidate.relative_to(publish)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(candidate), str(target))
            moved.append(candidate)
        return moved

    def _product_preview_images(self) -> list[Path]:
        preview_dir = self.context.paths.status / "product_package_work" / "demo_preview"
        return sorted(path for path in preview_dir.glob("*.png"))[:8] if preview_dir.exists() else []

    def _has_imported_inbox(self, manifest: dict[str, Any]) -> bool:
        return self.context.inbox is None or bool(self.state.get("inbox_imported_at"))

    def _has_source_edit(self, manifest: dict[str, Any]) -> bool:
        contract = manifest.get("agent", {}).get("input_contract", {})
        if contract.get("mode") == "prepared_greenscreen_confirmed_text":
            return not prepared_input_contract_errors(self.context.project_dir, manifest)
        inputs = manifest.get("inputs", {})
        story_text = first_existing(inputs.get("story_text"))
        if story_text is None:
            return False
        decisions = first_existing(manifest.get("outputs", {}).get("source_edit_decisions"))
        original = first_existing(inputs.get("greenscreen_video_original"))
        if original is None:
            return True
        clean_video = first_existing(inputs.get("greenscreen_video"))
        return decisions is not None and clean_video is not None and clean_video != original

    def _has_source_edit_review(self, manifest: dict[str, Any]) -> bool:
        decisions = first_existing(manifest.get("outputs", {}).get("source_edit_decisions"))
        if decisions is None:
            return True
        review_path = self.context.paths.status / "source_edit" / "source_edit_review.json"
        if not review_path.exists():
            return False
        if not self._hashed_qa_report_passes(self.context.paths.status / "qa_source_report.json"):
            return False
        try:
            payload = json.loads(review_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return review_passes(payload, artifact=decisions)

    def _hashed_qa_report_passes(self, report_path: Path) -> bool:
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        artifacts = payload.get("artifacts")
        if not payload.get("passed") or not isinstance(artifacts, dict) or not artifacts:
            return False
        for item in artifacts.values():
            if not isinstance(item, dict):
                return False
            artifact = Path(str(item.get("path", "")))
            if not artifact.is_file():
                return False
            stat = artifact.stat()
            if item.get("bytes") == stat.st_size and item.get("mtime_ns") == stat.st_mtime_ns:
                continue
            if item.get("sha256") != file_sha256(artifact):
                return False
        return True

    def _has_source_text_correction(self, manifest: dict[str, Any]) -> bool:
        decisions = first_existing(manifest.get("outputs", {}).get("source_edit_decisions"))
        if decisions is None:
            return True
        source_dir = self.context.paths.status / "source_edit"
        raw = source_dir / "raw_transcript.json"
        correction = source_dir / "corrected_transcript.json"
        if not raw.exists() or not correction.exists():
            return False
        try:
            payload = json.loads(correction.read_text(encoding="utf-8"))
            raw_payload = json.loads(raw.read_text(encoding="utf-8"))
            decisions_payload = json.loads(decisions.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return (
            payload.get("raw_transcript_sha256") == file_sha256(raw)
            and len(payload.get("segments", [])) == len(raw_payload.get("segments", []))
            and decisions_payload.get("text_overrides_source_sha256") == file_sha256(correction)
        )

    def _has_setup_project(self, manifest: dict[str, Any]) -> bool:
        inputs = manifest.get("inputs", {})
        return self.context.paths.manifest.exists() and bool(inputs.get("story_text")) and bool(inputs.get("narration") or inputs.get("extracted_narration"))

    def _legacy_contract_policy(self, manifest: dict[str, Any]) -> bool:
        return legacy_passthrough_allowed(manifest)

    def _prepare_contract_consumer(self, manifest: dict[str, Any], consumer: str) -> Path | StageResult | None:
        if self._legacy_contract_policy(manifest):
            return None
        try:
            existing = contract_consumer_path(self.context.project_dir, consumer)
            if not existing.exists() or not contract_consumer_context_is_current(self.context.project_dir, consumer, existing):
                self._archive_contract_consumer_outputs(consumer)
            return write_contract_consumer_context(self.context.project_dir, consumer)
        except (OSError, ValueError) as exc:
            return StageResult("blocked", f"{consumer} 无法读取已审核合同锁：{exc}")

    def _complete_contract_consumer(self, manifest: dict[str, Any], consumer: str) -> Path | None:
        if self._legacy_contract_policy(manifest):
            return None
        return mark_contract_consumer_completed(self.context.project_dir, consumer)

    def _consumer_request_current(self, manifest: dict[str, Any], consumer: str) -> bool:
        return self._legacy_contract_policy(manifest) or contract_consumer_context_is_current(self.context.project_dir, consumer)

    def _consumer_output_current(self, manifest: dict[str, Any], consumer: str) -> bool:
        return self._legacy_contract_policy(manifest) or contract_consumer_completion_is_current(self.context.project_dir, consumer)

    def _append_contract_handoff(self, handoff: Path, context: Path, purpose: str) -> None:
        marker = "<!-- STORY_CONTRACT_CONTEXT_V1 -->"
        text = handoff.read_text(encoding="utf-8-sig", errors="strict")
        if marker in text:
            text = text.split(marker, 1)[0].rstrip() + "\n"
        payload = json.loads(context.read_text(encoding="utf-8"))
        block = "\n".join([
            "", marker, "## 已审核 Story Production Contract（强制）", "",
            f"- 使用范围：{purpose}", f"- 请求清单：`{context}`",
            f"- contract_schema_version：`{payload.get('contract_schema_version', '')}`",
            f"- story_contract_sha256：`{payload.get('story_contract_sha256', '')}`",
            f"- story_contract_dependency_sha256：`{payload.get('story_contract_dependency_sha256', '')}`",
            "- 必须读取请求清单的 contract_projection；不得用旧项目、历史样例或自行推断覆盖合同规则。",
            "- 本任务产生的 plan/request manifest 必须原样记录上述三个绑定字段。", "",
        ])
        handoff.write_text(text + block, encoding="utf-8")

    def _archive_contract_consumer_outputs(self, consumer: str) -> Path:
        """Archive only the output family whose dependency projection changed.

        V3.5 currently maps dependencies at output-family granularity.  A
        later Runtime increment may narrow storyboard/video invalidation to
        individual scenes, but unrelated families are deliberately untouched.
        """

        stamp = time.strftime("%Y%m%d-%H%M%S")
        archive = self.context.paths.status / "rejected" / "contract_invalidation" / stamp / consumer
        archive.mkdir(parents=True, exist_ok=True)
        candidates: list[Path] = []
        if consumer == "storyboard_images":
            candidates.extend((self.context.paths.images / "images").glob(f"{self.context.slug}_scene_*.png"))
            candidates.extend(self.context.paths.images.glob(f"{self.context.slug}_visual_bible.md"))
            candidates.extend(self.context.paths.images.glob(f"{self.context.slug}_storyboard_plan.json"))
            staging = self._codex_stage_dir("codex_story_images")
            candidates.extend((staging / "images").glob(f"{self.context.slug}_scene_*.png"))
            candidates.extend(staging.glob(f"{self.context.slug}_visual_bible.md"))
            candidates.extend(staging.glob(f"{self.context.slug}_storyboard_plan.json"))
            candidates.extend((staging / "images").glob(f"{self.context.slug}_flow_*"))
            candidates.append(self._story_image_generation_manifest_path())
        elif consumer == "image_video":
            candidates.extend((self.context.paths.video_jobs / "videos").glob("*.mp4"))
        elif consumer == "music":
            candidates.extend(self._music_dir().glob(f"{self.context.slug}_music_plan.csv"))
            candidates.extend(self._music_dir().glob(f"{self.context.slug}_suno_prompts.md"))
            candidates.extend(self._music_dir().glob(f"{self.context.slug}_background_music.*"))
            candidates.extend(self._suno_downloads_dir().glob("*"))
        elif consumer == "cover":
            candidates.extend(self.context.paths.publish.glob("*/covers/cover_*"))
            candidates.extend(self.context.paths.publish.glob("*/copy.md"))
            candidates.extend(self.context.paths.publish.glob("publish_asset_manifest.json"))
        elif consumer == "release_video":
            candidates.extend((self.context.paths.release / "theme_assets").glob("*.png"))
            candidates.extend(self.context.paths.release.glob("*发布视频.mp4"))
            candidates.extend(self.context.paths.release.glob("release_geometry_manifest*.json"))
            candidates.extend(self.context.paths.release.glob("release_render_manifest*.json"))
        elif consumer == "product_package":
            work = self.context.paths.status / "product_package_work"
            candidates.extend(
                path
                for path in (
                    work / "annotation.json",
                    work / "朗读标注.docx",
                    work / "demo_params.json",
                    work / "第16步资料包_Codex前置审查.md",
                    work / "product_semantic_selection_manifest.json",
                    work / "product_content.compiled.json",
                    work / "story_subtitles_public.srt",
                    work / "story_subtitles_demo.srt",
                )
                if path.exists()
            )
            candidates.extend(path for path in (work / "demo_preview", work / "keying_preview") if path.exists())
            for key in ("product_base", "product_advanced"):
                target = first_existing(self._manifest().get("outputs", {}).get(key))
                if target is not None:
                    candidates.append(target)
        for source in candidates:
            if not source.exists():
                continue
            target = archive / source.name
            counter = 1
            while target.exists():
                target = archive / f"{source.stem}_{counter}{source.suffix}"
                counter += 1
            shutil.move(str(source), str(target))
        return archive

    def _has_story_contract(self, manifest: dict[str, Any]) -> bool:
        if self._legacy_contract_policy(manifest):
            return True
        paths = contract_paths(self.context.project_dir)
        return paths["summary"].is_file() and not contract_runtime_issues(self.context.project_dir)

    def _has_story_contract_review(self, manifest: dict[str, Any]) -> bool:
        if self._legacy_contract_policy(manifest):
            return True
        paths = contract_paths(self.context.project_dir)
        if contract_runtime_issues(self.context.project_dir):
            return False
        if not paths["bundle"].is_file() or not paths["review"].is_file():
            return False
        if not review_bundle_is_current(paths["bundle"]):
            return False
        try:
            payload = json.loads(paths["review"].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return (
            review_passes(payload, artifact=paths["bundle"])
            and not contract_review_payload_issues(payload)
            and contract_lock_is_current(
                self.context.project_dir,
                bundle=paths["bundle"],
                review=paths["review"],
            )
        )

    def _has_story_images(self, manifest: dict[str, Any]) -> bool:
        if not self._consumer_output_current(manifest, "storyboard_images"):
            return False
        if not self._has_story_image_files(manifest):
            return False
        if self._legacy_contract_policy(manifest):
            return True
        context = contract_consumer_path(self.context.project_dir, "storyboard_images")
        storyboard = self._story_image_binding_storyboard(self._storyboard_path(manifest))
        return storyboard is not None and self._story_image_generation_complete(manifest, context, storyboard)

    def _has_story_image_files(self, manifest: dict[str, Any]) -> bool:
        image_dir = self._image_dir()
        storyboard = self._storyboard_path(manifest)
        if image_dir is None or storyboard is None:
            return False
        expected = self._expected_story_image_count(manifest, storyboard)
        return expected > 0 and all((image_dir / self._story_image_filename(index)).exists() for index in range(1, expected + 1))

    def _has_story_images_review(self, manifest: dict[str, Any]) -> bool:
        if not self._review_stage_current("story_images_review"):
            return False
        if self._legacy_contract_policy(manifest):
            return True
        review = self.context.paths.status / "reviews" / "story_images_review_review.json"
        try:
            payload = json.loads(review.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return not self._story_image_quality_review_issues(payload)

    def _has_jobs_csv(self, manifest: dict[str, Any]) -> bool:
        return self._consumer_request_current(manifest, "image_video") and self._jobs_csv(manifest) is not None

    def _has_timing(self, manifest: dict[str, Any]) -> bool:
        timings = first_existing(manifest.get("outputs", {}).get("timings_json"), self.context.paths.assembly / "timings.json")
        if timings is not None:
            try:
                payload = json.loads(timings.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                return False
            # A real alignment JSON is a non-empty list of timing records.  A
            # malformed/empty placeholder must not unlock the next stage.
            if isinstance(payload, list):
                return bool(payload) and all(isinstance(item, dict) for item in payload)
            if isinstance(payload, dict):
                for key in ("timings", "segments", "items"):
                    items = payload.get(key)
                    if isinstance(items, list) and items:
                        return all(isinstance(item, dict) for item in items)
            return False
        jobs = self._jobs_csv(manifest)
        if jobs is None:
            return False
        try:
            with jobs.open(encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
            if not rows:
                return False
            # ``prepare`` intentionally writes duration=10 as a placeholder.
            # Require actual narration alignment plus the generated request
            # duration (or an explicit mode for legacy metadata) before timing
            # is considered complete.
            required = ("target_duration", "narration_start", "narration_end")
            for row in rows:
                if any(not str(row.get(field, "")).strip() for field in required):
                    return False
                if not str(row.get("generation_duration", "")).strip() and not str(row.get("duration_mode", "")).strip():
                    return False
                try:
                    target = float(row["target_duration"])
                    start = float(row["narration_start"])
                    end = float(row["narration_end"])
                except (KeyError, TypeError, ValueError):
                    return False
                if target <= 0 or start < 0 or end <= start:
                    return False
            return True
        except Exception:
            return False

    def _has_generated_videos(self, manifest: dict[str, Any]) -> bool:
        if not self._consumer_output_current(manifest, "image_video"):
            return False
        return self._has_generated_video_files(manifest)

    def _has_generated_video_files(self, manifest: dict[str, Any]) -> bool:
        jobs = self._jobs_csv(manifest)
        videos = self.context.paths.video_jobs / "videos"
        if jobs is None or not videos.exists():
            return False
        try:
            with jobs.open(encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
        except Exception:
            return False
        targets = [(row.get("target_video_filename") or "").strip() for row in rows]
        if not targets or not all(name and (videos / name).is_file() and (videos / name).stat().st_size > 0 for name in targets):
            return False
        if self._legacy_contract_policy(manifest):
            return True
        for row, name in zip(rows, targets):
            if formal_source_issues(row, production_mode=True):
                return False
            if video_receipt_issues(row, videos / name, production_mode=True):
                return False
        return True

    def _has_video_prompt_review(self, manifest: dict[str, Any]) -> bool:
        review_dir = self.context.paths.status / "reviews"
        bundle = review_dir / "video_prompt_bundle.json"
        review = review_dir / "video_prompt_review.json"
        snapshot = review_dir / "video_prompt_inputs.json"
        decisions = self.context.paths.video_jobs / "prompt_review_decisions.csv"
        jobs = self._jobs_csv(manifest)
        if jobs is None or not bundle.exists() or not review.exists() or not snapshot.exists() or not decisions.exists() or not review_bundle_is_current(bundle):
            return False
        if validate_image_video_jobs(jobs):
            return False
        try:
            payload = json.loads(review.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return review_passes(payload, artifact=bundle) and video_prompt_review_matches_current(jobs, snapshot, decisions)

    def _has_video_qa(self, manifest: dict[str, Any]) -> bool:
        report = first_existing(manifest.get("qa", {}).get("videos"), self.context.paths.status / "qa_videos_report.md")
        if report is None:
            return False
        if self._legacy_contract_policy(manifest):
            return True
        evidence = first_existing(manifest.get("qa", {}).get("videos_json"), self.context.paths.status / "qa_videos_report.json")
        if evidence is None:
            return False
        try:
            payload = json.loads(evidence.read_text(encoding="utf-8"))
            jobs = self._jobs_csv(manifest)
            return (
                payload.get("passed") is True
                and jobs is not None
                and payload.get("jobs_sha256") == file_sha256(jobs)
            )
        except (OSError, json.JSONDecodeError):
            return False

    def _has_video_review(self, manifest: dict[str, Any]) -> bool:
        return self._review_stage_current("video_review")

    def _has_applied_review(self, manifest: dict[str, Any]) -> bool:
        return (self.context.paths.assembly / "script_lines.txt").exists() and (self.context.paths.assembly / "clips").exists()

    def _has_music_request(self, manifest: dict[str, Any]) -> bool:
        if self._provided_music(manifest) is not None:
            return True
        return self._consumer_request_current(manifest, "music") and (
            self._music_plan().exists()
            or (self._music_dir() / f"{self.context.slug}_suno_music_request.md").exists()
            or self._background_music().exists()
        )

    def _has_suno_audio(self, manifest: dict[str, Any]) -> bool:
        if self._provided_music(manifest) is not None:
            return True
        downloads = self._suno_downloads_dir()
        if self._legacy_contract_policy(manifest) and not self._music_plan().is_file():
            return downloads.exists() and any(path.suffix.lower() in AUDIO_EXTENSIONS for path in downloads.iterdir())
        targets = self._expected_suno_audio_targets(manifest)
        return bool(targets) and all(path.is_file() and path.stat().st_size > 0 for path in targets)

    def _expected_suno_audio_targets(self, manifest: dict[str, Any]) -> tuple[Path, ...]:
        plan = self._music_plan()
        targets: list[Path] = []
        invalid_plan = False
        if plan.is_file():
            try:
                with plan.open(encoding="utf-8-sig", newline="") as file:
                    rows = list(csv.DictReader(file))
            except (OSError, csv.Error):
                rows = []
                invalid_plan = True
            seen: set[str] = set()
            for row in rows:
                name = str(row.get("target_audio_filename") or "").strip()
                candidate = Path(name)
                if (
                    not name
                    or candidate.name != name
                    or candidate.suffix.lower() not in AUDIO_EXTENSIONS
                    or name in seen
                ):
                    invalid_plan = True
                    continue
                seen.add(name)
                targets.append(self._suno_downloads_dir() / name)
        if targets and not invalid_plan:
            return tuple(targets)
        if invalid_plan or plan.is_file():
            return ()
        if self._legacy_contract_policy(manifest):
            return (self._suno_downloads_dir() / f"01_{self.context.slug}_music.mp3",)
        return ()

    def _has_background_music(self, manifest: dict[str, Any]) -> bool:
        if self._provided_music(manifest) is not None:
            return True
        return self._background_music().exists()

    def _has_music_qa(self, manifest: dict[str, Any]) -> bool:
        return self._music_qa_report_passes(self.context.paths.status / "qa_music_report.json")

    def _music_qa_report_passes(self, report: Path) -> bool:
        return self._json_qa_report_passes(report)

    def _music_plan_contract_bound(self, manifest: dict[str, Any]) -> bool:
        if self._legacy_contract_policy(manifest):
            return True
        plan = self._music_plan()
        try:
            expected = json.loads(contract_consumer_path(self.context.project_dir, "music").read_text(encoding="utf-8"))
            with plan.open(encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
        except (OSError, json.JSONDecodeError, csv.Error):
            return False
        return bool(rows) and all(
            row.get("contract_schema_version") == expected.get("contract_schema_version")
            and row.get("story_contract_sha256") == expected.get("story_contract_sha256")
            and row.get("story_contract_dependency_sha256") == expected.get("story_contract_dependency_sha256")
            for row in rows
        )

    def _json_qa_report_passes(self, report: Path) -> bool:
        try:
            payload = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not payload.get("passed", False):
            return False
        artifacts = payload.get("artifacts")
        if not isinstance(artifacts, dict) or not artifacts:
            return False
        for item in artifacts.values():
            if not isinstance(item, dict):
                return False
            path = Path(str(item.get("path", "")))
            if not path.is_file() or item.get("sha256") != file_sha256(path):
                return False
        return True

    def _has_background_assembly(self, manifest: dict[str, Any]) -> bool:
        if not all((self.context.paths.assembly / name).exists() for name in ("story_no_subs_bgm.mp4", "story_sales_subs_bgm.mp4", "story_demo_voice_bgm.mp4")):
            return False
        if self._legacy_contract_policy(manifest):
            return True
        source = self._artifact_semantic_source(manifest)
        receipt = self.context.paths.assembly / "artifact_semantic_plan_manifest.json"
        if source is None or not receipt.is_file():
            return False
        try:
            plan_path = self.context.paths.status / "story_contract" / "artifact_semantic_plan.json"
            plan = load_current_artifact_semantic_plan(self.context.project_dir, source)
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            return all(payload.get(field) == value for field, value in artifact_semantic_plan_binding(plan_path, plan).items())
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return False

    def _has_release_assets(self, manifest: dict[str, Any]) -> bool:
        from story_project import main_package_receipt_issues

        theme = self.context.paths.release / "theme_assets"
        required = (
            "main_release_plate_top.png", "main_release_plate_bottom.png",
            "library_release_plate_top.png", "library_release_plate_bottom.png",
            "main_package_spec.json", "main_package_generation_receipt.json",
            "main_background_16x9.png", "story_frame_a.png",
        )
        return (
            self._consumer_request_current(manifest, "release_video")
            and all((theme / name).exists() for name in required)
            and not main_package_receipt_issues(self.context.paths)
            and (self.context.paths.release / "keying" / "keying_preset.json").exists()
        )

    def _has_release_preview(self, manifest: dict[str, Any]) -> bool:
        if not self._review_stage_current("release_preview"):
            return False
        if self._legacy_contract_policy(manifest):
            return True
        return not keying_preset_lock_issues(self.context.paths.release / "keying" / "keying_preset.json")

    def _has_release_videos(self, manifest: dict[str, Any]) -> bool:
        outputs = (
            self.context.paths.release / "主账号发布视频.mp4",
            self.context.paths.release / "宝库号发布视频.mp4",
        )
        if not self._consumer_output_current(manifest, "release_video") or not all(path.exists() for path in outputs):
            return False
        if self._legacy_contract_policy(manifest):
            return True
        try:
            from demo_quality import load_preview_demo_geometry_for_release_review
            from release_geometry import canonical_sha256, file_sha256 as geometry_file_sha256, release_render_manifest_issues

            spec_path = self.context.paths.status / "contracts" / "consumers" / "release_video.compiled.json"
            preset_path = self.context.paths.release / "keying" / "keying_preset.json"
            plan_path = semantic_plan_path(self.context.project_dir)
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            preset = json.loads(preset_path.read_text(encoding="utf-8"))
            source = self._artifact_semantic_source(manifest)
            if source is None:
                return False
            plan = load_current_artifact_semantic_plan(self.context.project_dir, source)
            demo_manifest_path = self.context.paths.status / "product_package_work" / "demo_preview_manifest.json"
            _demo_manifest, demo_geometry = load_preview_demo_geometry_for_release_review(
                demo_manifest_path, self.context.project_dir,
            )
            expected = {
                "story_contract_sha256": str(spec["story_contract_sha256"]),
                "contract_schema_version": str(spec["contract_schema_version"]),
                "contract_projection_sha256": str(spec["contract_projection_sha256"]),
                "story_contract_dependency_sha256": str(spec["story_contract_dependency_sha256"]),
                "release_projection_sha256": str(spec["contract_projection_sha256"]),
                "release_dependency_sha256": str(spec["story_contract_dependency_sha256"]),
                "compiled_release_spec_sha256": canonical_sha256(spec),
                "production_keying_filter_fingerprint": self._modules().keyer().fingerprint(preset),
                "keying_preset_sha256": geometry_file_sha256(preset_path),
                "keying_lock_sha256": geometry_file_sha256(preset_path.with_name("keying_preset.lock.json")),
                "demo_render_manifest_sha256": geometry_file_sha256(demo_manifest_path),
                "approved_demo_geometry_sha256": str(demo_geometry["geometry_sha256"]),
                **artifact_semantic_plan_binding(plan_path, plan),
            }
            candidates = sorted(self.context.paths.release.glob("release_render_manifest*.json"))
            if not candidates:
                return False
            for path in candidates:
                candidate_expected = release_render_expected_bindings(path, expected)
                if release_render_manifest_issues(
                    json.loads(path.read_text(encoding="utf-8")),
                    expected_bindings=candidate_expected,
                    verify_outputs=True,
                ):
                    return False
            return True
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return False

    def _has_release_qa(self, manifest: dict[str, Any]) -> bool:
        return self._json_qa_report_passes(self.context.paths.status / "qa_release_report.json")

    def _has_release_video_review(self, manifest: dict[str, Any]) -> bool:
        return self._review_stage_current("release_video_review")

    def _has_publish_package(self, manifest: dict[str, Any]) -> bool:
        if not (self._consumer_output_current(manifest, "cover") and self._has_publish_package_files()):
            return False
        if self._legacy_contract_policy(manifest):
            return True
        try:
            integrated_receipt = self.context.paths.publish / "cover_integrated_generation.json"
            if integrated_receipt.is_file():
                issues, _ = integrated_cover_issues(
                    self.context.paths.publish,
                    receipt_path=integrated_receipt,
                    expected_title=str(manifest.get("story", {}).get("name") or ""),
                )
                return not issues
            compiled = json.loads((self.context.paths.status / "contracts" / "consumers" / "cover.compiled.json").read_text(encoding="utf-8"))
            issues, _ = required_cover_issues(
                self.context.paths.publish,
                render_manifest_path=self.context.paths.publish / "cover_render_manifest.json",
                lineage_path=self.context.paths.publish / "cover_lineage.json",
                compiled_spec=compiled,
                expected_title=str(manifest.get("story", {}).get("name") or ""),
            )
            return not issues
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def _has_publish_package_files(self) -> bool:
        publish = self.context.paths.publish
        required = [
            publish / account / "covers" / f"cover_{ratio}.png"
            for account in ("main", "library")
            for ratio in ("3x4", "4x3", "16x9")
        ]
        required.extend([publish / "main" / "copy.md", publish / "library" / "copy.md"])
        return all(path.exists() for path in required)

    def _has_publish_creative_bases(self) -> bool:
        publish = self.context.paths.publish
        required = [
            publish / account / "covers" / f"creative_base_{ratio}.png"
            for account in ("main", "library") for ratio in ("3x4", "4x3", "16x9")
        ]
        required.extend([publish / "cover_creative_lineage.json", publish / "main" / "copy.md", publish / "library" / "copy.md"])
        return all(path.is_file() for path in required)

    def _has_publish_package_review(self, manifest: dict[str, Any]) -> bool:
        return self._review_stage_current("publish_package_review")

    def _has_product_preflight(self, manifest: dict[str, Any]) -> bool:
        work = self.context.paths.status / "product_package_work"
        current = (
            self._consumer_request_current(manifest, "product_package")
            and (work / "第16步资料包_Codex前置审查.md").exists()
            and self._artifact_semantic_receipt_current(manifest, work / "artifact_semantic_plan_product_manifest.json")
        )
        if not current or self._legacy_contract_policy(manifest):
            return current
        return not demo_render_manifest_issues(work / "demo_preview_manifest.json", self.context.project_dir)

    def _has_product_annotation(self, manifest: dict[str, Any]) -> bool:
        return (
            self._product_annotation() is not None
            and self._artifact_semantic_receipt_current(
                manifest,
                self.context.paths.status / "product_package_work" / "artifact_semantic_plan_product_manifest.json",
            )
        )

    def _has_product_annotation_review(self, manifest: dict[str, Any]) -> bool:
        current = self._review_stage_current("product_annotation_review")
        if not current or self._legacy_contract_policy(manifest):
            return current
        annotation = self._product_annotation()
        content_manifest = self.context.paths.status / "product_package_work" / "product_content_manifest.json"
        review_path = self.context.paths.status / "reviews" / "product_annotation_review_review.json"
        if annotation is None:
            return False
        try:
            payload = json.loads(review_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        from product_quality import annotation_review_payload_issues

        return not annotation_review_payload_issues(
            payload,
            annotation_json=annotation,
            content_manifest=content_manifest,
        )

    def _has_product_package(self, manifest: dict[str, Any]) -> bool:
        outputs = manifest.get("outputs", {})
        current = (
            self._consumer_output_current(manifest, "product_package")
            and first_existing(outputs.get("product_base")) is not None
            and first_existing(outputs.get("product_advanced")) is not None
            and self._artifact_semantic_receipt_current(
                manifest,
                self.context.paths.status / "product_package_work" / "artifact_semantic_plan_product_manifest.json",
            )
        )
        if not current or self._legacy_contract_policy(manifest):
            return current
        work = self.context.paths.status / "product_package_work"
        from product_quality import (
            annotation_receipt_issues,
            manuscript_receipt_issues,
            ppt_render_manifest_issues,
            product_content_manifest_issues,
            product_package_manifest_issues,
        )

        return not any(
            (
                demo_render_manifest_issues(work / "demo_render_manifest.json", self.context.project_dir),
                product_content_manifest_issues(work / "product_content_manifest.json"),
                manuscript_receipt_issues(work / "customer_manuscript_receipt.json"),
                annotation_receipt_issues(work / "reading_annotation_receipt.json"),
                ppt_render_manifest_issues(work / "ppt_with_subtitles_render_manifest.json"),
                ppt_render_manifest_issues(work / "ppt_without_subtitles_render_manifest.json"),
                product_package_manifest_issues(work / "product_package_manifest.json"),
            )
        )

    def _artifact_semantic_receipt_current(self, manifest: dict[str, Any], receipt: Path) -> bool:
        if self._legacy_contract_policy(manifest):
            return True
        source = self._artifact_semantic_source(manifest)
        if source is None or not receipt.is_file():
            return False
        try:
            plan_path = semantic_plan_path(self.context.project_dir)
            plan = load_current_artifact_semantic_plan(self.context.project_dir, source)
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            return all(
                payload.get(field) == value
                for field, value in artifact_semantic_plan_binding(plan_path, plan).items()
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return False

    def _has_product_package_review(self, manifest: dict[str, Any]) -> bool:
        if not self._review_stage_current("product_package_review"):
            return False
        if self._legacy_contract_policy(manifest):
            return True
        if not self._has_product_package(manifest):
            return False
        try:
            from product_quality import product_package_review_payload_issues

            evidence_root = self.context.paths.status / "product_package_work" / "ppt_evidence"
            evidence = sorted(evidence_root.glob("**/*.png"))
            review = json.loads(
                (self.context.paths.status / "reviews" / "product_package_review_review.json").read_text(
                    encoding="utf-8"
                )
            )
            return not product_package_review_payload_issues(
                review,
                evidence_root=evidence_root,
                required_evidence=evidence,
            )
        except (OSError, json.JSONDecodeError):
            return False

    def _has_final_delivery(self, manifest: dict[str, Any]) -> bool:
        return (self.context.project_dir / "总交付清单.md").exists() and bool(manifest.get("completed_at"))

    def _has_doctor_report(self, manifest: dict[str, Any]) -> bool:
        return (self.context.paths.status / "doctor_project_report.md").exists()

    def _write_story_image_handoff(self, manifest: dict[str, Any], *, staging_images: Path, staging_storyboard: Path) -> Path:
        story_text = first_existing(manifest.get("inputs", {}).get("story_text"))
        notes_text = self._story_notes_text()
        final_request_dir = self.context.paths.images
        final_image_dir = final_request_dir / "images"
        final_storyboard = final_request_dir / f"{self.context.slug}_storyboard_lines.txt"
        final_request = final_request_dir / f"{self.context.slug}_agent_story_images.md"
        staging_request = staging_images.parent / f"{self.context.slug}_agent_story_images.md"
        final_request_dir.mkdir(parents=True, exist_ok=True)
        final_image_dir.mkdir(parents=True, exist_ok=True)
        staging_images.mkdir(parents=True, exist_ok=True)
        story_body = story_text.read_text(encoding="utf-8-sig", errors="ignore") if story_text else ""
        story_lines = self._story_lines(manifest)
        story_meta = manifest.get("story", {}) if isinstance(manifest.get("story"), dict) else {}
        requirements = [
            f"权威分镜已由状态机写入 `{staging_storyboard}`；该文件只读，禁止生产者改写、合并、删减、扩写或重排。",
            f"全自动模式下，必须先把视觉圣经保存到：{staging_images.parent / (self.context.slug + '_visual_bible.md')}。",
            f"必须把逐镜叙事覆盖、焦点角色、景别、在场/不在场角色、连续场景组和 appearance_id 保存到：{staging_images.parent / (self.context.slug + '_storyboard_plan.json')}。",
            "V3.5 required_v1 项目必须读取已审核 visual_sample.lock.json 和 visual_sample_plan.json；优先复用其中合同预览/条件小样，不得另造一套风格锚点。",
            "visual_style、characters、world_scale、story_state 必须完整进入最终逐镜计划和真实生图指令，不得删减或用模型偏好覆盖。",
            "多角色同框镜头必须在 scale_basis 逐项引用对应 relationship_id，并在 visual_description 写明大小层级、落点、前后景和透视。单角色特写可改变画面占比，多角色同框不得放大小角色到破坏合同相对尺度。",
            "不得擅自新增合同没有可信来源的身份定义性标记、固定配饰、徽记、非意图结构或永久解剖锚点；允许符合合同和 visual_style 的风格化、拟人化、奇幻结构、夸张比例、普通服饰及非身份自然细节。",
            "每镜必须写 scale_basis、current_story_state、visual_state_evidence，并引用合同中的 relationship_id、machine_id、state_id；不适用也要写明原因。",
            "每镜必须写 subject_action、environment_motion、camera_motion、entry_state、exit_state、screen_direction、adjacent_handoff、expected_motion；adjacent_handoff 必须显式包含 boolean 类型的 allows_direction_change 与 allows_state_transition；动作计划必须明确该动什么、该保持什么，不能用完全静止代替连续性。",
            "不要出现绵羊姐姐形象、主持人形象、羊、小羊、人偶或任何与品牌相关的角色形象。",
            "用户提供的原文已经按镜头分行；原则上每一行就是一个独立镜头。",
            f"如果标题镜头需要文字，只能使用本集标题“{self.context.story_name}”，不得套用任何历史样例标题。",
            "如果结尾需要道理文本，必须从本集清洁文稿提炼，先逐字校对再生成；不得套用历史样例道理。",
            "除明确的本集标题和结尾道理文本外，其他镜头不要生成中文文字、字幕、水印。",
            "原文里的“绵羊姐姐”“小朋友们”“我的故事讲完了”只是旁白口吻，绝对不能变成画面人物或角色。",
        ]
        continuity_contract = self._visual_continuity_contract_path()
        if continuity_contract is not None:
            requirements.extend(
                [
                    f"必须先读取并遵守视觉连续性合同：{continuity_contract}；它是本项目逐镜状态、转折、required/forbidden 约束的唯一机器可读来源。",
                    "storyboard_plan.json 每镜必须写入合同 storyboard_requirements.required_field 指定的字段，并使用合同 allowed_states 中的有效枚举；缺失、越界或没有按 transitions/story_boundaries 拆镜都必须先修正再出图。",
                ]
            )
        skill_path = ROOT / "skills" / "children-storyboard-images" / "SKILL.md"
        final_request.write_text(
            build_children_story_image_request(
                story_title=self.context.story_name,
                story=story_body,
                source_path=story_text or self.context.paths.inputs / f"{self.context.slug}_source.txt",
                storyboard_path=final_storyboard,
                image_dir=final_image_dir,
                slug=self.context.slug,
                short_slug=short_slug(self.context.slug),
                skill_path=skill_path,
                manual_lines=story_lines,
                full_auto=True,
                extra_requirements=requirements,
                notes_text=notes_text,
                story_type=str(story_meta.get("story_type", "")),
                image_style=str(story_meta.get("image_style", "自动")),
            ),
            encoding="utf-8",
        )
        staging_request.write_text(
            build_children_story_image_request(
                story_title=self.context.story_name,
                story=story_body,
                source_path=story_text or self.context.paths.inputs / f"{self.context.slug}_source.txt",
                storyboard_path=staging_storyboard,
                image_dir=staging_images,
                slug=self.context.slug,
                short_slug=short_slug(self.context.slug),
                skill_path=skill_path,
                manual_lines=story_lines,
                full_auto=True,
                extra_requirements=requirements,
                notes_text=notes_text,
                story_type=str(story_meta.get("story_type", "")),
                image_style=str(story_meta.get("image_style", "自动")),
                staging_note=(
                    "当前 Codex CLI 子任务只写工程暂存区，不直接写桌面故事项目目录。"
                    f"父级 Python Agent 会把暂存图片和分镜文本同步到正式目录：`{final_image_dir}`、`{final_storyboard}`。"
                ),
            ),
            encoding="utf-8",
        )
        handoff = staging_images.parent / f"{self.context.slug}_codex_handoff.txt"
        handoff.write_text(build_children_story_handoff(staging_request, full_auto=True) + "\n", encoding="utf-8")
        return staging_request

    def _write_suno_handoff(self) -> Path:
        music_dir = self._music_dir()
        handoff = music_dir / f"{self.context.slug}_suno_browser_handoff.md"
        request = music_dir / f"{self.context.slug}_suno_music_request.md"
        prompts = music_dir / f"{self.context.slug}_suno_prompts.md"
        plan = self._music_plan()
        downloads = self._suno_downloads_dir()
        downloads.mkdir(parents=True, exist_ok=True)
        handoff.write_text(
            "\n".join([
                "# Suno 浏览器自动化任务",
                "",
                "请使用已经登录并验证过的 Suno 页面完成音乐生成。",
                "",
                f"- 配乐任务书：`{request}`",
                f"- Suno 提示词：`{prompts}`",
                f"- 音乐分段 CSV：`{plan}`",
                f"- 下载保存目录：`{downloads}`",
                f"- 外部阻塞报告：`{music_dir / 'suno_cli_blocker.md'}`",
                "",
                "执行口径：如果提示词和分段 CSV 尚未从任务书生成，请先由 Codex 生成它们。然后打开 Suno，逐条粘贴提示词，选择 instrumental，点击 create。CSV 每一行都必须下载一首可用结果，并按该行 target_audio_filename 精确重命名保存；不得只完成第一行。",
            ]) + "\n",
            encoding="utf-8",
        )
        return handoff

    def _storyboard_path(self, manifest: dict[str, Any]) -> Path | None:
        return first_existing(
            manifest.get("outputs", {}).get("storyboard"),
            self.context.paths.images / f"{self.context.slug}_storyboard_lines.txt",
            self.context.paths.video_jobs / f"{self.context.slug}_storyboard_lines.txt",
        )

    def _image_dir(self) -> Path | None:
        for path in (self.context.paths.images / "images", self.context.paths.video_jobs / "images"):
            if path.exists() and any(item.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} for item in path.iterdir()):
                return path
        return None

    def _story_lines(self, manifest: dict[str, Any]) -> list[str]:
        story_text = first_existing(
            manifest.get("inputs", {}).get("storyboard_text"),
            manifest.get("inputs", {}).get("story_text"),
        )
        if story_text is None:
            return []
        try:
            text = story_text.read_text(encoding="utf-8-sig", errors="ignore")
        except Exception:
            return []
        return [line.strip() for line in text.splitlines() if line.strip()]

    def _artifact_semantic_source(self, manifest: dict[str, Any]) -> Path | None:
        """Return the auditable, line-oriented source used by the plan compiler."""

        inputs = manifest.get("inputs", {})
        candidates = (
            inputs.get("confirmed_subtitles"),
            inputs.get("storyboard_text"),
            self.context.paths.inputs / f"{self.context.slug}_storyboard_text.txt",
            inputs.get("story_text"),
            self.context.paths.inputs / "story_source.txt",
        )
        for candidate in candidates:
            path = first_existing(candidate)
            if path is not None and path.suffix.lower() in {".txt", ".md"}:
                return path
        return None

    def _has_artifact_semantic_plan(self, manifest: dict[str, Any]) -> bool:
        if self._legacy_contract_policy(manifest):
            return True
        source = self._artifact_semantic_source(manifest)
        return source is not None and artifact_semantic_plan_is_current(self.context.project_dir, source)

    def _has_visual_samples(self, manifest: dict[str, Any]) -> bool:
        if self._legacy_contract_policy(manifest):
            return True
        context = contract_consumer_path(self.context.project_dir, "storyboard_images")
        if not visual_sample_plan_is_current(self.context.project_dir, context, require_ready=True):
            return False
        try:
            plan = load_current_visual_sample_plan(self.context.project_dir, context)
            qa = json.loads(visual_sample_paths(self.context.project_dir)["machine_qa"].read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        paths = visual_sample_paths(self.context.project_dir)
        return (
            not visual_sample_machine_issues(self.context.project_dir, plan)
            and qa.get("passed") is True
            and qa.get("visual_sample_plan_sha256") == file_sha256(paths["plan"])
        )

    def _has_visual_sample_review(self, manifest: dict[str, Any]) -> bool:
        if self._legacy_contract_policy(manifest):
            return True
        context = contract_consumer_path(self.context.project_dir, "storyboard_images")
        return visual_sample_lock_is_current(self.context.project_dir, context)

    def _expected_story_image_count(self, manifest: dict[str, Any], storyboard: Path | None = None) -> int:
        story_lines = self._story_lines(manifest)
        if story_lines:
            return len(story_lines)
        if storyboard and storyboard.exists():
            return len([line for line in storyboard.read_text(encoding="utf-8-sig", errors="ignore").splitlines() if line.strip()])
        return 0

    def _story_image_count(self, image_dir: Path | None) -> int:
        if image_dir is None or not image_dir.exists():
            return 0
        return len([path for path in image_dir.iterdir() if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}])

    def _expected_named_story_image_count(self, image_dir: Path | None, manifest: dict[str, Any], storyboard: Path | None) -> int:
        if image_dir is None or not image_dir.exists():
            return 0
        expected = self._expected_story_image_count(manifest, storyboard)
        return sum(1 for index in range(1, expected + 1) if (image_dir / self._story_image_filename(index)).exists())

    def _story_image_lineage_status(
        self,
        manifest: dict[str, Any],
        *,
        storyboard: Path | None,
        image_dir: Path | None,
        staging_images: Path,
    ) -> dict[str, Any]:
        """Separate physical files from images bound to current production inputs."""

        expected = self._expected_story_image_count(manifest, storyboard)
        physical = self._expected_named_story_image_count(image_dir, manifest, storyboard)
        physical_staging = self._expected_named_story_image_count(
            staging_images, manifest, storyboard
        )
        generation_manifest = self._story_image_generation_manifest_path()
        context = contract_consumer_path(self.context.project_dir, "storyboard_images")
        generation_context_current = False
        valid_count = 0
        lineage_mode = "sha256_binding"

        if self._legacy_contract_policy(manifest):
            lineage_mode = "eligible_legacy_contract"
            stage_gate_complete = self._has_story_images(manifest)
            if stage_gate_complete:
                valid_count = physical
                generation_context_current = True
        else:
            stage_gate_complete = False
            if storyboard is not None and context.is_file() and storyboard.is_file():
                binding_storyboard = self._story_image_binding_storyboard(storyboard)
                generation_context_current = self._story_image_generation_context_current(
                    context, binding_storyboard or storyboard
                )
            if generation_context_current:
                try:
                    payload = json.loads(generation_manifest.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    payload = {}
                images = payload.get("images", {}) if isinstance(payload, dict) else {}
                if isinstance(images, dict):
                    valid_count = sum(
                        1
                        for index in range(1, expected + 1)
                        if self._story_image_filename(index) in images
                    )
            stage_gate_complete = self._has_story_images(manifest)

        return {
            "lineage_mode": lineage_mode,
            "generation_context_current": generation_context_current,
            "current_lineage_valid_count": valid_count,
            "physical_expected_named_count": physical,
            "physical_staging_expected_named_count": physical_staging,
            "stale_or_unbound_count": max(0, physical - valid_count),
            "stage_gate_complete": stage_gate_complete,
            "generation_manifest": str(generation_manifest),
            "expected": expected,
        }

    def _jobs_csv(self, manifest: dict[str, Any]) -> Path | None:
        return first_existing(manifest.get("outputs", {}).get("jobs_csv"), self.context.paths.video_jobs / f"{self.context.slug}_image_video_jobs.csv")

    def _job_count(self, jobs: Path) -> int:
        try:
            with jobs.open(encoding="utf-8-sig", newline="") as file:
                return len(list(csv.DictReader(file)))
        except Exception:
            return 0

    def _music_dir(self) -> Path:
        path = self.context.paths.video_jobs / "music"
        if not self.read_only:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def _music_plan(self) -> Path:
        return self._music_dir() / f"{self.context.slug}_music_plan.csv"

    def _suno_downloads_dir(self) -> Path:
        return self._music_dir() / "suno_downloads"

    def _background_music(self) -> Path:
        return self._music_dir() / f"{self.context.slug}_background_music.mp3"

    def _provided_music(self, manifest: dict[str, Any]) -> Path | None:
        inputs = manifest.get("inputs", {}) if isinstance(manifest.get("inputs"), dict) else {}
        return first_existing(inputs.get("music"))

    def _music_for_assembly(self, manifest: dict[str, Any]) -> Path:
        return self._provided_music(manifest) or self._background_music()

    def _story_notes_text(self) -> str:
        candidates = list(self.context.project_dir.glob("*备注*.txt")) + list(self.context.paths.inputs.glob("*备注*.txt"))
        texts: list[str] = []
        for path in sorted(candidates):
            try:
                texts.append(path.read_text(encoding="utf-8-sig", errors="ignore").strip())
            except Exception:
                pass
        return "\n\n".join(text for text in texts if text)

    def _product_annotation(self) -> Path | None:
        work = self.context.paths.status / "product_package_work"
        candidates = [work / "annotation.json", work / "朗读标注.json", work / "朗读标注.docx"]
        if work.exists():
            candidates.extend(sorted(work.glob("*标注*.json")))
            candidates.extend(sorted(work.glob("*标注*.docx")))
        for path in candidates:
            if path.exists() and "需精修" not in path.name:
                return path
        return None

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"version": 1, "events": []}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {"version": 1, "events": []}

    def _save_state(self) -> None:
        if self.read_only:
            return
        self.state["updated_at"] = now()
        save_json(self.state_path, self.state)

    def _record_stage(self, stage: str, result: StageResult, *, terminal: bool = True) -> None:
        self.state["last_stage"] = stage
        self.state["last_status"] = result.status
        self._record_event(stage, {"status": result.status, "message": result.message, "handoff": str(result.handoff) if result.handoff else ""})
        if not self.context.execute:
            return
        manifest = self._manifest()
        runtime_status = {"done": "passed", "blocked": "blocked", "failed": "failed"}.get(result.status, result.status)
        artifacts = [result.handoff] if result.handoff is not None else None
        review: dict[str, Any] | None = None
        if result.handoff and result.handoff.suffix.lower() == ".json":
            try:
                candidate_review = json.loads(result.handoff.read_text(encoding="utf-8"))
                review = candidate_review if "approved" in candidate_review else None
            except (OSError, json.JSONDecodeError):
                review = None
        spent = float(manifest.get("agent", {}).get("budget", {}).get("spent", 0.0))
        baseline = self._stage_cost_baselines.pop(stage, spent)
        output_hashes = {"manifest_context": manifest_context_sha256(manifest)}
        if artifacts:
            output_hashes.update(existing_artifact_hashes(artifacts))
        stage_attempt_record = manifest.get("agent", {}).get("stages", {}).get(stage, {})
        if runtime_status == "retrying" and isinstance(stage_attempt_record, dict):
            message_lower = result.message.lower()
            quality_retry = stage.endswith("_review") or any(
                marker in message_lower
                for marker in ("审核", "quality", "score", "门禁", "重做镜头", "review")
            )
            counter = "quality_attempts" if quality_retry else "infrastructure_attempts"
            stage_attempt_record[counter] = int(stage_attempt_record.get(counter) or 0) + 1
        mark_stage(
            manifest,
            stage,
            runtime_status,
            message=result.message,
            artifacts=artifacts,
            output_hashes=output_hashes,
            provider=self._provider_for_stage(stage),
            actual_cost=max(0.0, spent - baseline),
            retry_reason=result.message if runtime_status == "retrying" else "",
            review=review,
        )
        if runtime_status == "passed" and stage == "package_release":
            manifest["agent"]["delivery_state"] = "production_complete"
        elif runtime_status == "passed" and stage == "release_video_review":
            package_record = manifest["agent"].get("stages", {}).get("package_release", {})
            if isinstance(package_record, dict) and package_record.get("status") == "passed":
                manifest["agent"]["delivery_state"] = "internal_qa_passed"
        if terminal and runtime_status in {"blocked", "failed", "cancelled"}:
            freeze_runtime(manifest["agent"])
            manifest["agent"]["status"] = runtime_status
        from story_project import write_manifest

        write_manifest(self.context.paths, manifest)
        try:
            sinks = build_notification_sinks(self.context.notification_sinks)
        except ValueError:
            sinks = []
        job_id = str(manifest.get("agent", {}).get("job_id") or "")
        notification_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "stage": stage,
                    "status": runtime_status,
                    "message": result.message,
                    "output_hashes": output_hashes,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:16]
        if runtime_status == "passed":
            emit_notification(
                self.context.project_dir,
                category="stage_completed",
                severity="info",
                message=f"{stage} 已通过当前产物与哈希门禁。",
                dedupe_key=f"{job_id}:{stage}:passed:{notification_fingerprint}",
                recovery_mode="none",
                stage=stage,
                job_id=job_id,
                sinks=sinks,
            )
        elif runtime_status == "retrying":
            if stage == "story_contract_review":
                recovery_message = f"{stage}：将执行唯一一次定向文本修订；不会在合同阶段生图。{result.message}"
            elif stage == "visual_sample_review":
                recovery_message = f"{stage}：只重做审核点名的小样，并更换失败策略。{result.message}"
            else:
                recovery_message = f"{stage} 将在新 attempt 中自动恢复：{result.message}"
            emit_notification(
                self.context.project_dir,
                category="automatic_recovery",
                severity="warning",
                message=recovery_message,
                dedupe_key=f"{job_id}:{stage}:retrying:{notification_fingerprint}",
                recovery_mode="automatic",
                stage=stage,
                job_id=job_id,
                sinks=sinks,
            )
        elif runtime_status in {"blocked", "failed"}:
            emit_notification(
                self.context.project_dir,
                category="stage_blocked" if runtime_status == "blocked" else "stage_failed",
                severity="warning" if runtime_status == "blocked" else "error",
                message=f"{stage} {runtime_status}：{result.message}",
                dedupe_key=f"{job_id}:{stage}:{runtime_status}:{notification_fingerprint}",
                required_action="系统正在收集证据并判断能否自动恢复；需要授权时会另发通知。",
                recovery_mode="diagnosing",
                stage=stage,
                job_id=job_id,
                sinks=sinks,
            )
        budget = manifest.get("agent", {}).get("budget", {})
        if isinstance(budget, dict):
            spent = float(budget.get("spent") or 0.0)
            soft_limit = float(budget.get("soft_limit") or 0.0)
            hard_limit = float(budget.get("hard_limit") or 0.0)
            if soft_limit > 0 and spent >= soft_limit:
                emit_notification(
                    self.context.project_dir,
                    category="budget_risk",
                    severity="critical" if hard_limit > 0 and spent >= hard_limit * 0.9 else "warning",
                    message=f"成本已达 ¥{spent:.2f}，软上限 ¥{soft_limit:.2f}，硬上限 ¥{hard_limit:.2f}。",
                    dedupe_key=f"{job_id}:budget:soft-limit",
                    required_action="硬上限前不会越权新增付费调用；如需调整预算必须由用户明确确认。",
                    recovery_mode="wait_for_user" if hard_limit > 0 and spent >= hard_limit else "monitoring",
                    job_id=job_id,
                    sinks=sinks,
                )

    def _provider_for_stage(self, stage: str) -> str:
        if stage == "generate_videos":
            try:
                return self._modules().video_generator().identity.adapter_name
            except Exception:
                return "video_api"
        if stage in {"suno_generate", "assemble_music", "music_qa"}:
            return "suno" if stage == "suno_generate" else "local"
        if stage.startswith("codex_") or stage.endswith("_review") or stage in {
            "source_text_correction",
            "release_assets",
            "release_preview",
            "publish_package",
            "product_annotation",
        }:
            return "codex"
        return "local"

    def _record_event(self, event: str, payload: dict[str, Any]) -> None:
        if self.read_only:
            return
        manifest = ensure_manifest_v2(load_manifest(self.context.paths) or {})
        agent = manifest.get("agent", {}) if isinstance(manifest.get("agent"), dict) else {}
        stage = str(payload.get("stage") or (event if event in STORY_STAGE_SEQUENCE else ""))
        stage_record = (
            agent.get("stages", {}).get(stage, {})
            if stage and isinstance(agent.get("stages"), dict)
            else {}
        )
        if not isinstance(stage_record, dict):
            stage_record = {}
        scheduler = agent.get("scheduler", {}) if isinstance(agent.get("scheduler"), dict) else {}
        attempt_id = str(
            payload.get("attempt_id")
            or os.environ.get("STORY_AGENT_ATTEMPT_ID", "")
            or scheduler.get("running", {}).get(stage, {}).get("attempt_id", "")
            if isinstance(scheduler.get("running"), dict)
            else payload.get("attempt_id") or os.environ.get("STORY_AGENT_ATTEMPT_ID", "")
        )
        try:
            append_agent_event(
                self.context.project_dir,
                event_type="stage_result" if event in STORY_STAGE_SEQUENCE else event,
                status=str(payload.get("status") or ""),
                summary=str(payload.get("message") or ""),
                job_id=str(agent.get("job_id") or ""),
                run_id=str(scheduler.get("run_id") or ""),
                stage=stage,
                attempt_id=attempt_id,
                input_evidence_hashes=stage_record.get("input_hashes", {}),
                output_evidence_hashes=stage_record.get("output_hashes", {}),
                blocker_category=str(payload.get("blocker_category") or ""),
                recovery_decision=str(payload.get("recovery_decision") or ""),
                provider=str(stage_record.get("provider") or ""),
                request_id=str(stage_record.get("request_id") or ""),
                cost_cny=float(stage_record.get("actual_cost") or 0.0),
                metadata={
                    key: value
                    for key, value in payload.items()
                    if key not in {"message", "status", "stage", "attempt_id"}
                },
            )
        except (OSError, ValueError) as exc:
            self.state["event_log_error"] = f"{type(exc).__name__}: {exc}"
        events = self.state.setdefault("events", [])
        events.append({"time": now(), "event": event, **payload})
        self.state["events"] = events[-200:]
        self._save_state()


def infer_story_name(inbox: Path | None, explicit: str, project_dir: Path | None = None) -> str:
    if explicit.strip():
        return explicit.strip()
    if inbox is not None:
        name = inbox.expanduser().name.strip()
        if name.startswith("故事剪辑："):
            name = name.split("：", 1)[1].strip()
        return name or "新故事"
    if project_dir is not None:
        name = project_dir.expanduser().name.strip()
        if name.startswith("故事剪辑："):
            name = name.split("：", 1)[1].strip()
        return name or "新故事"
    return "新故事"


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ensure_background_dashboard(project_dir: Path) -> dict[str, Any]:
    """Start one scoped local dashboard and return its visible URL."""

    paths = project_paths(project_dir)
    state_path = paths.status / "story_agent_dashboard.json"
    if state_path.is_file():
        try:
            existing = json.loads(state_path.read_text(encoding="utf-8"))
            if process_is_alive(int(existing.get("pid") or 0)):
                return existing
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    port = 8765
    while port < 8785:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
                break
            except OSError:
                port += 1
    if port >= 8785:
        state = {
            "kind": "story_agent_dashboard_v1",
            "pid": 0,
            "url": "",
            "project_dir": str(project_dir.expanduser().resolve()),
            "started_at": now(),
            "status": "unavailable",
            "reason": "本机端口 8765-8784 均不可用；生产未被看板阻断。",
        }
        save_json(state_path, state)
        return state
    log_path = paths.status / "story_agent_dashboard.log"
    command = [
        resolve_agent_runtime_python(), str(Path(__file__).resolve()), "dashboard",
        "--project-dir", str(project_dir.expanduser().resolve()),
        "--host", "127.0.0.1", "--port", str(port),
    ]
    with log_path.open("a", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            env=normalized_subprocess_environment(),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    state = {
        "kind": "story_agent_dashboard_v1",
        "pid": process.pid,
        "url": f"http://127.0.0.1:{port}",
        "project_dir": str(project_dir.expanduser().resolve()),
        "started_at": now(),
        "log": str(log_path),
    }
    save_json(state_path, state)
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description="Codex 故事生产 Agent：状态机 + 现有脚本 + Codex 原生动作交接")
    subparsers = parser.add_subparsers(dest="command", required=True)

    submit = subparsers.add_parser("submit", help="投喂单绿幕原片，或已还原干净绿幕+人工确认文本")
    submit.add_argument("--video", required=True, type=Path)
    submit.add_argument("--lut", type=Path, help="可选 .cube 输入 LUT；复制进项目并记录 SHA-256")
    submit.add_argument("--input-mode", choices=["single-greenscreen", "prepared"], default="single-greenscreen")
    submit.add_argument("--confirmed-text", type=Path, help="prepared 加速入口必填：人工确认的 UTF-8 .txt/.md 或 .docx")
    submit.add_argument(
        "--confirmed-subtitles",
        type=Path,
        help="prepared 加速入口可选：人工确认的逐行 UTF-8 .txt/.md 字幕；仅登记供后续 timing/示范字幕使用",
    )
    submit.add_argument("--projects-root", default=Path.home() / "Desktop", type=Path)
    submit.add_argument("--story-name", default="")
    submit.add_argument("--slug", default="")
    submit.add_argument("--age-range", required=True, help="用户指定的适合年龄；系统不再根据故事内容猜测")
    submit.add_argument("--registry", type=Path)
    submit.add_argument("--soft-budget", default=50.0, type=float)
    submit.add_argument("--hard-budget", default=100.0, type=float)
    submit.add_argument(
        "--deadline-hours",
        default=10.0,
        type=float,
        help="自主生产目标时限；默认 10 小时，届时冻结最佳有效版本并停止新增审美返工",
    )
    submit.add_argument("--force", action="store_true", help="即使同一原片已投喂也创建新任务")

    refresh_prepared = subparsers.add_parser(
        "refresh-prepared-inputs",
        help="为旧 prepared 项目刷新镜头级 storyboard_text；可选登记人工确认字幕，不复制/重渲染视频",
    )
    refresh_prepared.add_argument("--project-dir", required=True, type=Path)
    refresh_prepared.add_argument(
        "--confirmed-subtitles",
        type=Path,
        help="可选：追加人工确认的 UTF-8 .txt/.md 字幕副本",
    )

    run = subparsers.add_parser("run", help="推进 Agent loop")
    run.add_argument("--job", default="", help="submit 返回的任务 ID")
    run.add_argument("--registry", type=Path)
    run.add_argument("--inbox", type=Path, help="投喂区：放故事文本、音频、绿幕视频")
    run.add_argument("--project-dir", type=Path, help="故事项目目录；默认 ~/Desktop/故事剪辑：故事名")
    run.add_argument("--story-name", default="")
    run.add_argument("--slug", default="")
    run.add_argument("--execute", action="store_true", help="真正执行本地命令；默认只 dry-run")
    run.add_argument("--max-steps", default=999, type=int, help="本轮最多推进阶段数；默认 999，面向一键跑完整链路")
    run.add_argument(
        "--stop-after-stage",
        choices=STORY_STAGE_SEQUENCE,
        default="",
        help="限界短测：指定阶段后置门禁通过后立即暂停，不启动下游生产",
    )
    run.add_argument("--update-latest-episode", action="store_true")
    run.add_argument("--codex-mode", choices=["handoff", "cli"], default="handoff", help="智能节点处理方式：handoff=生成交接文件后暂停；cli=自动调用 codex exec")
    run.add_argument("--codex-model", default="", help="指挥官模型；留空使用 pipeline_config.json 的 commander_model")
    run.add_argument("--codex-worker-model", default="", help="执行工人模型；留空使用 pipeline_config.json 的 worker_model")
    run.add_argument("--codex-reasoning-effort", choices=["low", "medium", "high", "xhigh", "max", "ultra"], default="", help="指挥官推理强度")
    run.add_argument("--codex-worker-reasoning-effort", choices=["low", "medium", "high", "xhigh", "max", "ultra"], default="", help="执行工人推理强度")
    run.add_argument("--codex-sandbox", choices=["read-only", "workspace-write", "danger-full-access"], default="workspace-write")
    run.add_argument("--codex-approval", choices=["untrusted", "on-request", "never"], default="never")
    run.add_argument("--codex-path", default="codex")
    run.add_argument("--codex-timeout", default=3600, type=int, help="单个 codex exec 子任务超时时间，秒")
    run.add_argument("--codex-story-image-batch-size", default=15, type=int, help="codex_story_images 每个 CLI 子任务批量生成的图片数量；默认 15，通常覆盖一个完整故事")
    run.add_argument("--scheduler", choices=["linear", "dag"], default="linear", help="linear 保留旧行为；dag 并行调度独立分支")
    run.add_argument("--max-parallel", default=3, type=int, help="DAG 最多并行 stage worker 数")
    run.add_argument("--module-profile", choices=sorted(ALLOWED_MODULE_PROFILES), default="")
    run.add_argument(
        "--module-execution-mode", choices=sorted(ALLOWED_MODULE_EXECUTION_MODES), default=""
    )
    run.add_argument(
        "--notification-sinks",
        default="project",
        help="逗号分隔：project,codex,desktop,lark；project 始终写 notifications.ndjson",
    )

    start = subparsers.add_parser("start", help="在后台启动无人值守 Agent supervisor")
    start.add_argument("--job", required=True)
    start.add_argument("--registry", type=Path)
    start.add_argument("--codex-model", default="")
    start.add_argument("--codex-worker-model", default="")
    start.add_argument("--codex-reasoning-effort", choices=["low", "medium", "high", "xhigh", "max", "ultra"], default="")
    start.add_argument("--codex-worker-reasoning-effort", choices=["low", "medium", "high", "xhigh", "max", "ultra"], default="")
    start.add_argument("--codex-timeout", default=3600, type=int)
    start.add_argument("--max-steps", default=999, type=int)
    start.add_argument("--scheduler", choices=["linear", "dag"], default="dag")
    start.add_argument("--max-parallel", default=3, type=int)
    start.add_argument("--module-profile", choices=sorted(ALLOWED_MODULE_PROFILES), default="")
    start.add_argument(
        "--module-execution-mode", choices=sorted(ALLOWED_MODULE_EXECUTION_MODES), default=""
    )
    start.add_argument("--heartbeat-interval", default=5.0, type=float)
    start.add_argument("--heartbeat-timeout", default=600, type=int)
    start.add_argument("--initial-backoff", default=10, type=int)
    start.add_argument("--max-backoff", default=600, type=int)
    start.add_argument("--notification-sinks", default="project,codex,desktop")
    start.add_argument("--no-dashboard", action="store_true", help="不自动启动本机只读 Dashboard")
    start.add_argument(
        "--disable-codex-recovery",
        action="store_true",
        help="只使用确定性安全恢复分类，不调用 Codex 恢复判断",
    )

    supervise = subparsers.add_parser("supervise", help=argparse.SUPPRESS)
    supervise.add_argument("--job", required=True)
    supervise.add_argument("--registry", type=Path)
    supervise.add_argument("--codex-model", default="")
    supervise.add_argument("--codex-worker-model", default="")
    supervise.add_argument("--codex-reasoning-effort", choices=["low", "medium", "high", "xhigh", "max", "ultra"], default="")
    supervise.add_argument("--codex-worker-reasoning-effort", choices=["low", "medium", "high", "xhigh", "max", "ultra"], default="")
    supervise.add_argument("--codex-timeout", default=3600, type=int)
    supervise.add_argument("--max-steps", default=999, type=int)
    supervise.add_argument("--scheduler", choices=["linear", "dag"], default="dag")
    supervise.add_argument("--max-parallel", default=3, type=int)
    supervise.add_argument("--module-profile", choices=sorted(ALLOWED_MODULE_PROFILES), default="")
    supervise.add_argument(
        "--module-execution-mode", choices=sorted(ALLOWED_MODULE_EXECUTION_MODES), default=""
    )
    supervise.add_argument("--heartbeat-interval", default=5.0, type=float)
    supervise.add_argument("--heartbeat-timeout", default=600, type=int)
    supervise.add_argument("--initial-backoff", default=10, type=int)
    supervise.add_argument("--max-backoff", default=600, type=int)
    supervise.add_argument("--notification-sinks", default="project,codex,desktop")
    supervise.add_argument("--disable-codex-recovery", action="store_true")
    supervise.add_argument("--max-supervisor-cycles", default=0, type=int, help=argparse.SUPPRESS)

    run_stage = subparsers.add_parser("run-stage", help=argparse.SUPPRESS)
    run_stage.add_argument("--project-dir", required=True, type=Path)
    run_stage.add_argument("--story-name", required=True)
    run_stage.add_argument("--slug", required=True)
    run_stage.add_argument("--stage", required=True, choices=STORY_STAGE_SEQUENCE)
    run_stage.add_argument("--result-file", required=True, type=Path)
    run_stage.add_argument("--run-epoch", required=True, type=int)
    run_stage.add_argument("--attempt-id", required=True)
    run_stage.add_argument("--codex-mode", choices=["handoff", "cli"], default="cli")
    run_stage.add_argument("--codex-model", default="")
    run_stage.add_argument("--codex-worker-model", default="")
    run_stage.add_argument("--codex-reasoning-effort", choices=["low", "medium", "high", "xhigh", "max", "ultra"], default="")
    run_stage.add_argument("--codex-worker-reasoning-effort", choices=["low", "medium", "high", "xhigh", "max", "ultra"], default="")
    run_stage.add_argument("--codex-sandbox", choices=["read-only", "workspace-write", "danger-full-access"], default="workspace-write")
    run_stage.add_argument("--codex-approval", choices=["untrusted", "on-request", "never"], default="never")
    run_stage.add_argument("--codex-path", default="codex")
    run_stage.add_argument("--codex-timeout", default=3600, type=int)
    run_stage.add_argument("--codex-story-image-batch-size", default=15, type=int)
    run_stage.add_argument("--module-profile", choices=sorted(ALLOWED_MODULE_PROFILES), default="")
    run_stage.add_argument(
        "--module-execution-mode", choices=sorted(ALLOWED_MODULE_EXECUTION_MODES), default=""
    )
    run_stage.add_argument("--notification-sinks", default="project")

    status = subparsers.add_parser("status", help="查看 Agent 下一阶段")
    status.add_argument("--job", default="")
    status.add_argument("--registry", type=Path)
    status.add_argument("--project-dir", type=Path)
    status.add_argument("--story-name", default="")
    status.add_argument("--slug", default="")

    dashboard = subparsers.add_parser("dashboard", help="启动只读本地实时 Web 看板")
    dashboard.add_argument("--job", default="")
    dashboard.add_argument("--registry", type=Path)
    dashboard.add_argument("--project-dir", type=Path)
    dashboard.add_argument("--story-name", default="")
    dashboard.add_argument("--slug", default="")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", default=8765, type=int)
    dashboard.add_argument("--poll-seconds", default=2.0, type=float)
    dashboard.add_argument("--snapshot", action="store_true", help="只输出一次看板快照，不启动服务器")

    preflight = subparsers.add_parser("preflight", help="只读执行 supervisor 启动前检查，不调用 provider")
    preflight.add_argument("--job", default="")
    preflight.add_argument("--registry", type=Path)
    preflight.add_argument("--project-dir", type=Path)
    preflight.add_argument("--story-name", default="")
    preflight.add_argument("--slug", default="")
    preflight.add_argument("--module-profile", choices=sorted(ALLOWED_MODULE_PROFILES), default="")
    preflight.add_argument(
        "--module-execution-mode", choices=sorted(ALLOWED_MODULE_EXECUTION_MODES), default=""
    )

    migrate_binding = subparsers.add_parser(
        "migrate-code-binding",
        help="显式把空闲项目从已核对的旧 commit 迁移到当前工作树 commit，并写审计回执",
    )
    migrate_binding.add_argument("--job", default="")
    migrate_binding.add_argument("--registry", type=Path)
    migrate_binding.add_argument("--project-dir", type=Path)
    migrate_binding.add_argument("--expected-old-revision", required=True)
    migrate_binding.add_argument("--migrated-by", default="user")
    migrate_binding.add_argument("--reason", required=True)

    notifications = subparsers.add_parser("notifications", help="查看或确认项目通知")
    notifications.add_argument("--job", default="")
    notifications.add_argument("--registry", type=Path)
    notifications.add_argument("--project-dir", type=Path)
    notifications.add_argument("--ack", default="", help="确认一个 notification_id")
    notifications.add_argument("--acknowledged-by", default="user")

    reconcile = subparsers.add_parser(
        "reconcile",
        help="按当前产物/哈希/依赖整理持久状态；不调用 provider",
    )
    reconcile.add_argument("--job", default="")
    reconcile.add_argument("--registry", type=Path)
    reconcile.add_argument("--project-dir", type=Path)
    reconcile.add_argument("--story-name", default="")
    reconcile.add_argument("--slug", default="")
    reconcile.add_argument(
        "--archive-stale-story-images",
        action="store_true",
        help="把无当前血缘绑定的故事图片移入 99_项目状态/rejected，并保存 SHA-256 清单",
    )
    reconcile.add_argument(
        "--legacy-story-image-staging",
        action="append",
        default=[],
        type=Path,
        help="可重复：旧 worktree 的 codex_story_images/images 目录；仅归档当前故事 slug 的文件",
    )

    for command_name, help_text in (
        ("resume", "清除取消/阻塞标记，允许任务继续"),
        ("cancel", "请求停止任务，不删除任何产物"),
        ("report", "生成早晨交付摘要"),
    ):
        command = subparsers.add_parser(command_name, help=help_text)
        command.add_argument("--job", default="")
        command.add_argument("--registry", type=Path)
        command.add_argument("--project-dir", type=Path)

    accept_current = subparsers.add_parser(
        "accept-current",
        help="接受并冻结当前版本，立即停止排队中的审美返工并生成轻量交付清单",
    )
    accept_current.add_argument("--job", default="")
    accept_current.add_argument("--registry", type=Path)
    accept_current.add_argument("--project-dir", type=Path)
    accept_current.add_argument("--accepted-by", default="user")
    accept_current.add_argument("--notes", default="")
    accept_current.add_argument("--artifact", action="append", default=[], type=Path)

    probe = subparsers.add_parser("probe-codex", help="测试 codex exec 子任务是否可用")
    probe.add_argument("--project-dir", default=Path("/private/tmp/story-agent-codex-probe"), type=Path)
    probe.add_argument("--codex-model", default="")
    probe.add_argument("--codex-sandbox", choices=["read-only", "workspace-write", "danger-full-access"], default="read-only")
    probe.add_argument("--codex-approval", choices=["untrusted", "on-request", "never"], default="never")
    probe.add_argument("--codex-path", default="codex")
    probe.add_argument("--codex-timeout", default=120, type=int)

    probe_image = subparsers.add_parser("probe-imagegen", help="测试非交互 codex exec 是否能调用 imagegen 并保存单张图片")
    probe_image.add_argument("--output-dir", default=ROOT / "output" / "story_agent_cli_probe" / "single_image", type=Path)
    probe_image.add_argument("--codex-model", default="")
    probe_image.add_argument("--codex-sandbox", choices=["read-only", "workspace-write", "danger-full-access"], default="workspace-write")
    probe_image.add_argument("--codex-approval", choices=["untrusted", "on-request", "never"], default="never")
    probe_image.add_argument("--codex-path", default="codex")
    probe_image.add_argument("--codex-timeout", default=240, type=int)

    probe_suno = subparsers.add_parser("probe-suno", help="测试非交互 Codex 是否能使用已登录浏览器访问 Suno 创作页")
    probe_suno.add_argument("--output-dir", default=ROOT / "output" / "story_agent_cli_probe" / "suno", type=Path)
    probe_suno.add_argument("--codex-model", default="")
    probe_suno.add_argument("--codex-sandbox", choices=["read-only", "workspace-write", "danger-full-access"], default="workspace-write")
    probe_suno.add_argument("--codex-approval", choices=["untrusted", "on-request", "never"], default="never")
    probe_suno.add_argument("--codex-path", default="codex")
    probe_suno.add_argument("--codex-timeout", default=300, type=int)

    signoff = subparsers.add_parser("signoff", help="记录用户人工终审，并把结果绑定到当前交付物哈希")
    signoff.add_argument("--job", default="")
    signoff.add_argument("--registry", type=Path)
    signoff.add_argument("--project-dir", type=Path)
    signoff.add_argument("--result", choices=["pass", "fail"], required=True)
    signoff.add_argument("--minutes", required=True, type=float)
    signoff.add_argument("--notes", default="")
    signoff.add_argument("--reviewer", default="用户人工终审")

    qualification = subparsers.add_parser("qualification", help="核验连续三条不同真实故事是否达到默认入口转正门槛")
    qualification.add_argument("--projects-root", default=ROOT / "auto-project" / "runs", type=Path)
    qualification.add_argument("--output", default=ROOT / "FULL_AUTO_PROMOTION_REPORT.md", type=Path)

    args = parser.parse_args()
    if args.command == "run-stage":
        result = StageResult("failed", "worker 未执行")
        try:
            module_registry = build_registry_for_profile(
                args.module_profile,
                load_config(),
                ROOT,
                execution_mode=args.module_execution_mode,
            )
            configured_epoch = int(os.environ.get("STORY_AGENT_RUN_EPOCH", "-1"))
            control = load_control(args.project_dir)
            if configured_epoch != args.run_epoch or int(control.get("run_epoch", -1)) != args.run_epoch:
                result = StageResult("cancelled", "worker run epoch 已过期")
            elif control.get("cancel_requested"):
                result = StageResult("cancelled", "worker 启动前收到取消请求")
            else:
                context = AgentContext(
                    project_dir=args.project_dir.expanduser(),
                    inbox=None,
                    story_name=args.story_name,
                    slug=args.slug,
                    execute=True,
                    update_latest_episode=False,
                    codex_mode=args.codex_mode,
                    codex_model=args.codex_model,
                    codex_sandbox=args.codex_sandbox,
                    codex_approval=args.codex_approval,
                    codex_path=args.codex_path,
                    codex_timeout=max(30, args.codex_timeout),
                    codex_worker_model=args.codex_worker_model,
                    codex_reasoning_effort=args.codex_reasoning_effort,
                    codex_worker_reasoning_effort=args.codex_worker_reasoning_effort,
                    codex_story_image_batch_size=max(1, args.codex_story_image_batch_size),
                    scheduler="linear",
                    max_parallel=1,
                    notification_sinks=args.notification_sinks,
                )
                worker = StoryAgent(context, module_registry=module_registry)
                manifest = worker._manifest()
                assert_runnable(manifest, context.project_dir)
                actions = {name: action for name, _done, action in worker._stage_checks()}
                result = actions[args.stage](manifest)
                if result.status not in {"done", "blocked", "failed", "cancelled", "retrying"}:
                    result = StageResult("failed", f"worker 返回了未知状态：{result.status}", result.handoff)
        except JobCancelled as exc:
            result = StageResult("cancelled", str(exc))
        except Exception as exc:
            result = StageResult("failed", f"{type(exc).__name__}: {exc}")
        save_json(
            args.result_file,
            {
                "version": 1,
                "attempt_id": args.attempt_id,
                "run_epoch": args.run_epoch,
                "stage": args.stage,
                "status": result.status,
                "message": result.message,
                "handoff": str(result.handoff) if result.handoff else "",
                "finished_at": now(),
            },
        )
        print(json.dumps({"stage": args.stage, "status": result.status, "result_file": str(args.result_file)}, ensure_ascii=False))
        return
    if args.command == "submit":
        if args.soft_budget < 0 or args.hard_budget <= 0 or args.soft_budget > args.hard_budget:
            parser.error("预算必须满足 0 <= soft-budget <= hard-budget")
        registry = JobRegistry(args.registry)
        job_id, project_dir, created = submit_video_job(
            args.video,
            lut=args.lut,
            input_mode=args.input_mode,
            confirmed_text=args.confirmed_text,
            confirmed_subtitles=args.confirmed_subtitles,
            projects_root=args.projects_root,
            story_name=args.story_name,
            slug=args.slug,
            age_range=args.age_range,
            registry=registry,
            force=args.force,
            soft_budget_cny=args.soft_budget,
            hard_budget_cny=args.hard_budget,
            deadline_hours=args.deadline_hours,
        )
        print(json.dumps({"job_id": job_id, "project_dir": str(project_dir), "created": created, "input_mode": args.input_mode, "age_range": args.age_range}, ensure_ascii=False, indent=2))
        return
    if args.command == "refresh-prepared-inputs":
        manifest = refresh_prepared_inputs(
            args.project_dir,
            confirmed_subtitles=args.confirmed_subtitles,
        )
        print(
            json.dumps(
                {
                    "project_dir": str(args.project_dir.expanduser().resolve()),
                    "storyboard_text": manifest.get("inputs", {}).get("storyboard_text", ""),
                    "confirmed_subtitles": manifest.get("inputs", {}).get("confirmed_subtitles", ""),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if args.command == "migrate-code-binding":
        project_dir = JobRegistry(args.registry).resolve(args.job) if args.job else args.project_dir
        if project_dir is None:
            parser.error("migrate-code-binding 需要 --job 或 --project-dir")
        manifest, receipt = migrate_code_binding(
            project_dir,
            expected_old_revision=args.expected_old_revision,
            migrated_by=args.migrated_by,
            reason=args.reason,
        )
        print(
            json.dumps(
                {
                    "project_dir": str(project_dir.expanduser().resolve()),
                    "code_identity": manifest.get("agent", {}).get("code_identity", {}),
                    "receipt": str(receipt),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if args.command == "start":
        build_notification_sinks(args.notification_sinks)
        module_registry = build_registry_for_profile(
            args.module_profile,
            load_config(),
            ROOT,
            execution_mode=args.module_execution_mode,
        )
        module_env = module_selection_environment(
            module_registry.selection_profile(), module_registry.selection_execution_mode()
        )
        registry = JobRegistry(args.registry)
        project_dir = registry.resolve(args.job)
        status_dir = project_paths(project_dir).status
        status_dir.mkdir(parents=True, exist_ok=True)
        dashboard_state = {} if args.no_dashboard else ensure_background_dashboard(project_dir)
        supervisor_path = status_dir / "story_agent_supervisor.json"
        with supervisor_start_lock(project_dir):
            if supervisor_path.exists():
                try:
                    existing = json.loads(supervisor_path.read_text(encoding="utf-8"))
                    existing_pid = int(existing.get("pid", 0))
                    if process_is_alive(existing_pid):
                        record_unattended_launch(project_dir, supervisor_record=supervisor_path)
                        print(json.dumps({"job_id": args.job, "project_dir": str(project_dir), "pid": existing_pid, "started": False, "message": "supervisor 已在运行", "dashboard": dashboard_state.get("url", "")}, ensure_ascii=False, indent=2))
                        return
                except (ValueError, json.JSONDecodeError):
                    pass
            log_path = status_dir / "story_agent_supervisor.log"
            command = [
                resolve_agent_runtime_python(),
                str(Path(__file__).resolve()),
                "supervise",
                "--job",
                args.job,
                "--max-steps",
                str(max(1, args.max_steps)),
                "--codex-timeout",
                str(max(30, args.codex_timeout)),
                "--scheduler",
                args.scheduler,
                "--max-parallel",
                str(max(1, args.max_parallel)),
                "--module-profile",
                module_registry.selection_profile(),
                "--module-execution-mode",
                module_registry.selection_execution_mode(),
                "--heartbeat-interval",
                str(max(0.5, args.heartbeat_interval)),
                "--heartbeat-timeout",
                str(max(10, args.heartbeat_timeout)),
                "--initial-backoff",
                str(max(5, args.initial_backoff)),
                "--max-backoff",
                str(max(5, args.max_backoff)),
                "--notification-sinks",
                args.notification_sinks,
            ]
            if args.disable_codex_recovery:
                command.append("--disable-codex-recovery")
            if args.registry:
                command.extend(["--registry", str(args.registry.expanduser())])
            if args.codex_model:
                command.extend(["--codex-model", args.codex_model])
            if args.codex_worker_model:
                command.extend(["--codex-worker-model", args.codex_worker_model])
            if args.codex_reasoning_effort:
                command.extend(["--codex-reasoning-effort", args.codex_reasoning_effort])
            if args.codex_worker_reasoning_effort:
                command.extend(["--codex-worker-reasoning-effort", args.codex_worker_reasoning_effort])
            launched_at = now()
            with log_path.open("a", encoding="utf-8") as log_file:
                process = subprocess.Popen(
                    command,
                    cwd=str(ROOT),
                    env=normalized_subprocess_environment(overrides=module_env),
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
            save_json(
                supervisor_path,
                {
                    "kind": "story_agent_start_v1",
                    "job_id": args.job,
                    "project_dir": str(project_dir.expanduser().resolve()),
                    "pid": process.pid,
                    "started_at": launched_at,
                    "log": str(log_path.expanduser().resolve()),
                    "command": command,
                    "scheduler": args.scheduler,
                    "max_parallel": max(1, args.max_parallel),
                    "supervisor_mode": "persistent_v1",
                    "state": str(supervisor_state_path(project_dir)),
                    "dashboard": dashboard_state.get("url", ""),
                },
            )
        record_unattended_launch(project_dir, supervisor_record=supervisor_path)
        print(json.dumps({"job_id": args.job, "project_dir": str(project_dir), "pid": process.pid, "started": True, "log": str(log_path), "dashboard": dashboard_state.get("url", "")}, ensure_ascii=False, indent=2))
        return
    if args.command == "supervise":
        from story_agent_supervisor import PersistentSupervisor, SupervisorConfig

        module_registry = build_registry_for_profile(
            args.module_profile,
            load_config(),
            ROOT,
            execution_mode=args.module_execution_mode,
        )
        build_notification_sinks(args.notification_sinks)
        registry = JobRegistry(args.registry)
        project_dir = registry.resolve(args.job)
        paths = project_paths(project_dir)
        existing_manifest = ensure_manifest_v2(load_manifest(paths) or {})
        story = existing_manifest.get("story", {}) if isinstance(existing_manifest.get("story"), dict) else {}
        story_name = str(story.get("name") or infer_story_name(None, "", project_dir))
        slug = str(story.get("slug") or slugify(story_name))
        agent_defaults = load_config().get("agent_defaults", {})
        commander_model = args.codex_model or str(agent_defaults.get("commander_model", ""))
        worker_model = args.codex_worker_model or str(agent_defaults.get("worker_model", ""))
        commander_effort = args.codex_reasoning_effort or str(
            agent_defaults.get("commander_reasoning_effort", "")
        )
        worker_effort = args.codex_worker_reasoning_effort or str(
            agent_defaults.get("worker_reasoning_effort", "")
        )
        run_command = [
            resolve_agent_runtime_python(),
            str(Path(__file__).resolve()),
            "run",
            "--job",
            args.job,
            "--execute",
            "--codex-mode",
            "cli",
            "--max-steps",
            str(max(1, args.max_steps)),
            "--codex-timeout",
            str(max(30, args.codex_timeout)),
            "--scheduler",
            args.scheduler,
            "--max-parallel",
            str(max(1, args.max_parallel)),
            "--module-profile",
            module_registry.selection_profile(),
            "--module-execution-mode",
            module_registry.selection_execution_mode(),
            "--notification-sinks",
            args.notification_sinks,
        ]
        if args.registry:
            run_command.extend(["--registry", str(args.registry.expanduser())])
        if commander_model:
            run_command.extend(["--codex-model", commander_model])
        if worker_model:
            run_command.extend(["--codex-worker-model", worker_model])
        if commander_effort:
            run_command.extend(["--codex-reasoning-effort", commander_effort])
        if worker_effort:
            run_command.extend(["--codex-worker-reasoning-effort", worker_effort])
        shared_context = dict(
            project_dir=project_dir,
            inbox=None,
            story_name=story_name,
            slug=slug,
            update_latest_episode=False,
            codex_model=commander_model,
            codex_sandbox="workspace-write",
            codex_approval="never",
            codex_path="codex",
            codex_timeout=max(30, args.codex_timeout),
            codex_worker_model=worker_model,
            codex_reasoning_effort=commander_effort,
            codex_worker_reasoning_effort=worker_effort,
            scheduler=args.scheduler,
            max_parallel=max(1, args.max_parallel),
            notification_sinks=args.notification_sinks,
        )
        snapshot_agent = StoryAgent(
            AgentContext(
                **shared_context,
                execute=False,
                codex_mode="handoff",
            ),
            read_only=True,
        )
        recovery_agent = StoryAgent(
            AgentContext(
                **shared_context,
                execute=True,
                codex_mode="cli",
            ),
            module_registry=module_registry,
        )

        def recovery_runner(prompt_path: Path, evidence_path: Path) -> tuple[bool, str, str]:
            del evidence_path
            result = recovery_agent._run_codex_exec("recovery_controller", prompt_path, [])
            _role, model, _effort = recovery_agent.context.codex_route("recovery_controller")
            if result.status != "done" or result.handoff is None:
                return False, result.message, model
            try:
                output = result.handoff.read_text(encoding="utf-8")
            except OSError as exc:
                return False, f"{type(exc).__name__}: {exc}", model
            return True, output, model

        supervisor = PersistentSupervisor(
            SupervisorConfig(
                project_dir=project_dir,
                job_id=args.job,
                run_command=tuple(run_command),
                log_path=paths.status / "story_agent_supervisor.log",
                heartbeat_interval_seconds=max(0.5, args.heartbeat_interval),
                heartbeat_timeout_seconds=max(10, args.heartbeat_timeout),
                initial_backoff_seconds=max(5, args.initial_backoff),
                max_backoff_seconds=max(
                    max(5, args.initial_backoff),
                    max(5, args.max_backoff),
                ),
                notification_sink_names=args.notification_sinks,
                max_cycles=max(0, args.max_supervisor_cycles),
            ),
            snapshot_provider=snapshot_agent.status_payload,
            recovery_runner=None if args.disable_codex_recovery else recovery_runner,
        )
        raise SystemExit(supervisor.run())
    if args.command == "accept-current":
        project_dir = JobRegistry(args.registry).resolve(args.job) if args.job else args.project_dir
        if project_dir is None:
            parser.error("accept-current 需要 --job 或 --project-dir")
        manifest, target = accept_current_outputs(
            project_dir.expanduser(),
            accepted_by=args.accepted_by,
            notes=args.notes,
            artifacts=args.artifact or None,
        )
        print(json.dumps({
            "project_dir": str(project_dir.expanduser().resolve()),
            "delivery_state": manifest.get("agent", {}).get("delivery_state", ""),
            "acceptance": str(target),
        }, ensure_ascii=False, indent=2))
        return
    if args.command == "signoff":
        project_dir = JobRegistry(args.registry).resolve(args.job) if args.job else args.project_dir
        if project_dir is None:
            parser.error("signoff 需要 --job 或 --project-dir")
        target = record_human_signoff(
            project_dir,
            result=args.result,
            minutes=args.minutes,
            notes=args.notes,
            reviewer=args.reviewer,
        )
        print(json.dumps({"project_dir": str(project_dir), "signoff": str(target)}, ensure_ascii=False, indent=2))
        return
    if args.command == "qualification":
        report = build_promotion_report(args.projects_root)
        output = render_promotion_markdown(report, args.output.expanduser())
        json_output = output.with_suffix(".json")
        save_json(json_output, report)
        print(json.dumps({**report, "markdown": str(output), "json": str(json_output)}, ensure_ascii=False, indent=2))
        return
    if args.command == "run":
        module_registry = build_registry_for_profile(
            args.module_profile,
            load_config(),
            ROOT,
            execution_mode=args.module_execution_mode,
        )
        if args.job:
            args.project_dir = JobRegistry(args.registry).resolve(args.job)
            existing_manifest = load_manifest(project_paths(args.project_dir)) or {}
            existing_story = existing_manifest.get("story", {}) if isinstance(existing_manifest.get("story"), dict) else {}
            args.story_name = args.story_name or str(existing_story.get("name") or "")
            args.slug = args.slug or str(existing_story.get("slug") or "")
        story_name = infer_story_name(args.inbox, args.story_name, args.project_dir)
        slug = args.slug.strip() or slugify(story_name)
        project_dir = args.project_dir.expanduser() if args.project_dir else Path.home() / "Desktop" / f"故事剪辑：{story_name}"
        agent_defaults = load_config().get("agent_defaults", {})
        commander_model = args.codex_model or str(agent_defaults.get("commander_model", ""))
        worker_model = args.codex_worker_model or str(agent_defaults.get("worker_model", ""))
        commander_effort = args.codex_reasoning_effort or str(agent_defaults.get("commander_reasoning_effort", ""))
        worker_effort = args.codex_worker_reasoning_effort or str(agent_defaults.get("worker_reasoning_effort", ""))
        context = AgentContext(
            project_dir=project_dir,
            inbox=args.inbox.expanduser() if args.inbox else None,
            story_name=story_name,
            slug=slug,
            execute=args.execute,
            update_latest_episode=args.update_latest_episode,
            codex_mode=args.codex_mode,
            codex_model=commander_model,
            codex_sandbox=args.codex_sandbox,
            codex_approval=args.codex_approval,
            codex_path=args.codex_path,
            codex_timeout=args.codex_timeout,
            codex_worker_model=worker_model,
            codex_reasoning_effort=commander_effort,
            codex_worker_reasoning_effort=worker_effort,
            codex_story_image_batch_size=max(1, args.codex_story_image_batch_size),
            scheduler=args.scheduler,
            max_parallel=max(1, args.max_parallel),
            notification_sinks=args.notification_sinks,
            stop_after_stage=args.stop_after_stage,
        )
        try:
            exit_code = StoryAgent(context, module_registry=module_registry).run(max(1, args.max_steps))
        except JobCancelled as exc:
            manifest = ensure_manifest_v2(load_manifest(context.paths) or {})
            manifest["agent"]["status"] = "cancelled"
            manifest["agent"]["blocked_reason"] = str(exc)
            from story_project import write_manifest

            write_manifest(context.paths, manifest)
            render_job_report(context.project_dir)
            print(f"CANCELLED: {exc}")
            exit_code = 2
        except AgentRuntimeError as exc:
            manifest = ensure_manifest_v2(load_manifest(context.paths) or {})
            manifest["agent"]["status"] = "blocked"
            manifest["agent"]["blocked_reason"] = str(exc)
            from story_project import write_manifest

            write_manifest(context.paths, manifest)
            render_job_report(context.project_dir)
            print(f"BLOCKED: {exc}")
            exit_code = 2
        raise SystemExit(exit_code)
    if args.command in {"status", "dashboard", "preflight"}:
        if args.job:
            args.project_dir = JobRegistry(args.registry).resolve(args.job)
            existing_manifest = load_manifest(project_paths(args.project_dir)) or {}
            existing_story = existing_manifest.get("story", {}) if isinstance(existing_manifest.get("story"), dict) else {}
            args.story_name = args.story_name or str(existing_story.get("name") or "")
            args.slug = args.slug or str(existing_story.get("slug") or "")
        if args.project_dir is None:
            parser.error(f"{args.command} 需要 --job 或 --project-dir")
        story_name = infer_story_name(None, args.story_name, args.project_dir)
        slug = args.slug.strip() or slugify(story_name)
        agent_defaults = load_config().get("agent_defaults", {})
        context = AgentContext(
            project_dir=args.project_dir.expanduser(),
            inbox=None,
            story_name=story_name,
            slug=slug,
            execute=False,
            update_latest_episode=False,
            codex_mode="handoff",
            codex_model=str(agent_defaults.get("commander_model", "")),
            codex_sandbox="workspace-write",
            codex_approval="never",
            codex_path="codex",
            codex_timeout=900,
            codex_worker_model=str(agent_defaults.get("worker_model", "")),
            codex_reasoning_effort=str(agent_defaults.get("commander_reasoning_effort", "")),
            codex_worker_reasoning_effort=str(agent_defaults.get("worker_reasoning_effort", "")),
        )
        agent = StoryAgent(context, read_only=True)
        if args.command == "status":
            agent.status()
        elif args.command == "preflight":
            from story_agent_preflight import build_start_preflight_report

            selected_modules = build_registry_for_profile(
                args.module_profile,
                load_config(),
                ROOT,
                execution_mode=args.module_execution_mode,
            )
            report = build_start_preflight_report(
                agent.status_payload(),
                code_paths=(
                    Path(__file__).resolve(),
                    ROOT / "story_agent_observability.py",
                    ROOT / "story_agent_dashboard.py",
                    ROOT / "story_agent_preflight.py",
                    ROOT / "story_agent_recovery.py",
                    ROOT / "story_agent_supervisor.py",
                ),
                module_profile=selected_modules.selection_profile(),
                module_execution_mode=selected_modules.selection_execution_mode(),
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            raise SystemExit(0 if report["passed"] else 1)
        elif args.snapshot:
            print(json.dumps(agent.status_payload(), ensure_ascii=False, indent=2))
        else:
            if args.host not in {"127.0.0.1", "localhost", "::1"}:
                parser.error("dashboard 默认只允许本机回环地址")
            from story_agent_dashboard import serve_dashboard

            serve_dashboard(
                agent.status_payload,
                project_dir=context.project_dir,
                host=args.host,
                port=max(0, args.port),
                poll_seconds=max(0.5, args.poll_seconds),
            )
        return
    if args.command == "notifications":
        project_dir = JobRegistry(args.registry).resolve(args.job) if args.job else args.project_dir
        if project_dir is None:
            parser.error("notifications 需要 --job 或 --project-dir")
        project_dir = project_dir.expanduser()
        if args.ack:
            acknowledgement = acknowledge_notification(
                project_dir,
                args.ack,
                acknowledged_by=args.acknowledged_by,
            )
            print(json.dumps(acknowledgement, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(load_notifications(project_dir, limit=200), ensure_ascii=False, indent=2))
        return
    if args.command == "reconcile":
        project_dir = JobRegistry(args.registry).resolve(args.job) if args.job else args.project_dir
        if project_dir is None:
            parser.error("reconcile 需要 --job 或 --project-dir")
        project_dir = project_dir.expanduser()
        existing_manifest = load_manifest(project_paths(project_dir)) or {}
        existing_story = (
            existing_manifest.get("story", {})
            if isinstance(existing_manifest.get("story"), dict)
            else {}
        )
        story_name = infer_story_name(
            None,
            args.story_name or str(existing_story.get("name") or ""),
            project_dir,
        )
        slug = args.slug.strip() or str(existing_story.get("slug") or "") or slugify(story_name)
        context = AgentContext(
            project_dir=project_dir,
            inbox=None,
            story_name=story_name,
            slug=slug,
            execute=True,
            update_latest_episode=False,
            codex_mode="handoff",
            codex_model="",
            codex_sandbox="workspace-write",
            codex_approval="never",
            codex_path="codex",
            codex_timeout=30,
            notification_sinks="project",
        )
        report = StoryAgent(context).reconcile_state(
            archive_stale_story_images=args.archive_stale_story_images,
            legacy_story_image_staging=args.legacy_story_image_staging,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if args.command in {"resume", "cancel", "report"}:
        project_dir = JobRegistry(args.registry).resolve(args.job) if args.job else args.project_dir
        if project_dir is None:
            parser.error(f"{args.command} 需要 --job 或 --project-dir")
        project_dir = project_dir.expanduser()
        if args.command == "resume":
            manifest = resume_job(project_dir)
            print(json.dumps({"job_id": manifest["agent"].get("job_id", ""), "status": manifest["agent"]["status"], "project_dir": str(project_dir)}, ensure_ascii=False, indent=2))
        elif args.command == "cancel":
            manifest = request_cancel(project_dir)
            print(json.dumps({"job_id": manifest["agent"].get("job_id", ""), "status": manifest["agent"]["status"], "project_dir": str(project_dir)}, ensure_ascii=False, indent=2))
        else:
            report_path = render_job_report(project_dir)
            print(json.dumps({"project_dir": str(project_dir), "report": str(report_path)}, ensure_ascii=False, indent=2))
        return
    if args.command == "probe-codex":
        project_dir = args.project_dir.expanduser()
        context = AgentContext(
            project_dir=project_dir,
            inbox=None,
            story_name="Codex CLI 探针",
            slug="codex-cli-probe",
            execute=True,
            update_latest_episode=False,
            codex_mode="cli",
            codex_model=args.codex_model,
            codex_sandbox=args.codex_sandbox,
            codex_approval=args.codex_approval,
            codex_path=args.codex_path,
            codex_timeout=args.codex_timeout,
        )
        agent = StoryAgent(context)
        prompt = context.paths.status / "agent_codex_tasks" / "probe_prompt.md"
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text("请只回复：CLI_OK\n", encoding="utf-8")
        result = agent._run_codex_exec("probe", prompt, [])
        print(json.dumps({"status": result.status, "message": result.message, "handoff": str(result.handoff) if result.handoff else ""}, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result.status == "done" else 1)
    if args.command == "probe-imagegen":
        output_dir = args.output_dir.expanduser()
        image_dir = output_dir / "images"
        image_dir.mkdir(parents=True, exist_ok=True)
        context = AgentContext(
            project_dir=output_dir,
            inbox=None,
            story_name="Codex CLI Imagegen 探针",
            slug="codex-cli-imagegen-probe",
            execute=True,
            update_latest_episode=False,
            codex_mode="cli",
            codex_model=args.codex_model,
            codex_sandbox=args.codex_sandbox,
            codex_approval=args.codex_approval,
            codex_path=args.codex_path,
            codex_timeout=args.codex_timeout,
        )
        agent = StoryAgent(context)
        target = image_dir / "probe_story_01.png"
        prompt = output_dir / "probe_imagegen_prompt.md"
        prompt.write_text(
            "\n".join(
                [
                    "请做一个最小 imagegen 探针：生成 1 张 16:9 儿童历史故事插画。",
                    "画面：古代书房中一个少年夜读竹简，温暖灯光。",
                    "限制：无字幕、无水印、无中文文字、不要出现羊/小羊/绵羊姐姐/主持人/人偶/品牌角色。",
                    f"必须把图片保存为：`{target}`",
                    "如果 imagegen 默认保存到 `$CODEX_HOME/generated_images/...`，请把选中的最终 PNG 复制到上述路径。",
                    f"如果当前非交互 codex exec 无法调用 imagegen，请写入：`{output_dir / 'codex_cli_imagegen_blocker.md'}`，不要假装完成。",
                    "完成后只用中文简短说明是否生成成功和文件路径。",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        result = agent._run_codex_exec("probe_imagegen", prompt, [])
        ok = target.exists()
        print(json.dumps({"status": "done" if ok else result.status, "image": str(target if ok else ""), "message": result.message}, ensure_ascii=False, indent=2))
        raise SystemExit(0 if ok else 1)
    if args.command == "probe-suno":
        output_dir = args.output_dir.expanduser()
        output_dir.mkdir(parents=True, exist_ok=True)
        context = AgentContext(
            project_dir=output_dir,
            inbox=None,
            story_name="Suno 浏览器探针",
            slug="suno-browser-probe",
            execute=True,
            update_latest_episode=False,
            codex_mode="cli",
            codex_model=args.codex_model,
            codex_sandbox=args.codex_sandbox,
            codex_approval=args.codex_approval,
            codex_path=args.codex_path,
            codex_timeout=args.codex_timeout,
        )
        agent = StoryAgent(context)
        target = output_dir / "suno_probe.json"
        prompt = output_dir / "probe_suno_prompt.md"
        prompt.write_text(
            "\n".join(
                [
                    "Use $suno-story-score for a capability probe only.",
                    "Open the authenticated Suno create page using the available browser control tool.",
                    "Do not create music, spend credits, download files, or change account settings.",
                    "Check whether the page is authenticated and whether Custom/Advanced instrumental creation controls are accessible.",
                    "If a CAPTCHA, login, permission, or missing browser tool blocks access, record it without attempting to bypass it.",
                    f"Write JSON to `{target}` with keys: available, authenticated, custom_mode, model_v55_visible, blocked_reason, checked_at.",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        result = agent._run_codex_exec("probe_suno", prompt, [])
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        ok = bool(payload.get("available") and payload.get("authenticated") and payload.get("custom_mode"))
        print(json.dumps({"status": "done" if ok else result.status, "probe": str(target if target.exists() else ""), "result": payload, "message": result.message}, ensure_ascii=False, indent=2))
        raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
