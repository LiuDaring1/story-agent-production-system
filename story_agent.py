from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw
from video_provider_adapter import resolve_video_provider

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
    STORY_STAGE_SEQUENCE,
    STAGE_ESTIMATES_MINUTES,
    assert_runnable,
    ensure_manifest_v2,
    existing_artifact_hashes,
    file_sha256,
    job_lock,
    mark_stage,
    manifest_context_sha256,
    render_job_report,
    request_cancel,
    resume_job,
    review_bundle_is_current,
    review_passes,
    submit_video_job,
    write_review_bundle,
)
from story_project import (
    AUDIO_EXTENSIONS,
    TEXT_EXTENSIONS,
    VIDEO_EXTENSIONS,
    ensure_project_dirs,
    first_existing,
    init_project,
    load_config,
    load_manifest,
    project_paths,
    refresh_project_outputs,
    save_json,
    short_slug,
    slugify,
)


ROOT = Path(__file__).resolve().parent
AGENT_STATE_NAME = "story_agent_state.json"


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
    codex_story_image_batch_size: int = 15

    @property
    def paths(self):
        return project_paths(self.project_dir)


def canonical_video_prompt_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    result: list[dict[str, str]] = []
    for row in rows:
        raw_scene = (row.get("scene") or "").strip()
        try:
            scene = str(int(raw_scene)).zfill(2)
        except ValueError:
            scene = raw_scene
        result.append(
            {
                "scene": scene,
                "image_filename": (row.get("image_filename") or "").strip(),
                "story_text": (row.get("story_text") or "").strip(),
                "prompt": (row.get("prompt") or "").strip(),
            }
        )
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
        approved.append(
            {
                "scene": scene,
                "image_filename": (row.get("image_filename") or source.get("image_filename") or "").strip(),
                "story_text": (row.get("story_text") or source.get("story_text") or "").strip(),
                "prompt": (row.get("prompt") or source.get("prompt") or "").strip(),
            }
        )
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
    def __init__(self, context: AgentContext, *, read_only: bool = False) -> None:
        self.context = context
        self.read_only = read_only
        if not read_only:
            ensure_project_dirs(context.paths)
        self.state_path = context.paths.status / AGENT_STATE_NAME
        self.state: dict[str, Any] = self._load_state()
        self._stage_cost_baselines: dict[str, float] = {}

    def run(self, max_steps: int) -> int:
        with job_lock(self.context.project_dir):
            manifest = self._manifest()
            ensure_manifest_v2(manifest)
            assert_runnable(manifest, self.context.project_dir)
            from story_project import write_manifest

            if self.context.execute:
                manifest["agent"]["status"] = "running"
                manifest["agent"]["started_at"] = manifest["agent"].get("started_at") or now()
                write_manifest(self.context.paths, manifest)
            self._record_event("agent_start", {"execute": self.context.execute, "codex_mode": self.context.codex_mode})
            for _ in range(max_steps):
                manifest = self._manifest()
                assert_runnable(manifest, self.context.project_dir)
                stage_name, action = self._next_stage(manifest)
                if self.context.execute:
                    self._reconcile_completed_stage_records(manifest, stage_name)
                if stage_name == "done":
                    if self.context.execute:
                        manifest["agent"]["status"] = "completed"
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
                result = action(manifest)
                if result.status == "failed":
                    critical_stage = stage_name in {
                        "source_edit",
                        "codex_story_images",
                        "generate_videos",
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
        print(f"PAUSED: 已达到本轮最大步数 {max_steps}，可再次运行继续。")
        render_job_report(self.context.project_dir)
        return 0

    def status(self) -> None:
        manifest = self._manifest()
        stage_name, _ = self._next_stage(manifest)
        remaining_work: list[dict[str, Any]] = []
        if stage_name != "done":
            start_index = STORY_STAGE_SEQUENCE.index(stage_name)
            for name in STORY_STAGE_SEQUENCE[start_index:]:
                remaining_work.append({"stage": name, "estimated_minutes": STAGE_ESTIMATES_MINUTES.get(name, 10)})
        estimate_total = sum(int(item["estimated_minutes"]) for item in remaining_work)
        agent_data = manifest.get("agent", {})
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
        safe_project = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in self.context.slug) or "story"
        staging_images = ROOT / "output" / "story_agent_cli" / safe_project / "codex_story_images" / "images"
        supervisor: dict[str, Any] = {"running": False}
        supervisor_path = self.context.paths.status / "story_agent_supervisor.json"
        if supervisor_path.exists():
            try:
                supervisor = json.loads(supervisor_path.read_text(encoding="utf-8"))
                pid = int(supervisor.get("pid", 0))
                os.kill(pid, 0)
                supervisor["running"] = True
            except (OSError, ValueError, json.JSONDecodeError):
                supervisor["running"] = False
        print(json.dumps({
            "job_id": manifest.get("agent", {}).get("job_id", ""),
            "project_dir": str(self.context.project_dir),
            "next_stage": stage_name,
            "agent_status": agent_data.get("status", "pending"),
            "last_checkpoint": agent_data.get("last_checkpoint", ""),
            "heartbeat_at": agent_data.get("heartbeat_at", ""),
            "blocked_reason": agent_data.get("blocked_reason", ""),
            "recovery_action": self._recovery_action(manifest, stage_name),
            "budget": agent_data.get("budget", {}),
            "timing": timing,
            "retries_and_failures": retries,
            "remaining_work": remaining_work,
            "estimated_remaining_minutes": {
                "optimistic": round(estimate_total * 0.65),
                "nominal": estimate_total,
                "conservative": round(estimate_total * 1.8),
                "note": "按阶段经验值估算；外部生成队列、登录阻塞和重试不包含在确定性承诺内。",
            },
            "supervisor": supervisor,
            "state_file": str(self.state_path),
            "codex_mode": self.context.codex_mode,
            "story_images": {
                "actual": self._story_image_count(image_dir),
                "expected_named_actual": self._expected_named_story_image_count(image_dir, manifest, storyboard),
                "expected": self._expected_story_image_count(manifest, storyboard),
                "staging_actual": self._story_image_count(staging_images),
                "staging_expected_named_actual": self._expected_named_story_image_count(staging_images, manifest, storyboard),
                "staging_dir": str(staging_images),
                "image_dir": str(image_dir or ""),
                "storyboard": str(storyboard or ""),
            },
            "final_delivery": manifest.get("outputs", {}).get("final_delivery_checklist", ""),
            "completed_at": manifest.get("completed_at", ""),
            "completion_valid": stage_name == "done" and bool(manifest.get("completed_at")),
        }, ensure_ascii=False, indent=2))

    def _timing_status(self, agent_data: dict[str, Any]) -> dict[str, Any]:
        deadline_hours = float(agent_data.get("deadline_hours", 10.0))
        started_text = str(agent_data.get("started_at", ""))
        elapsed_hours = 0.0
        if started_text:
            try:
                elapsed_hours = max(0.0, (time.time() - time.mktime(time.strptime(started_text, "%Y-%m-%d %H:%M:%S"))) / 3600)
            except ValueError:
                pass
        return {
            "started_at": started_text,
            "elapsed_hours": round(elapsed_hours, 3),
            "deadline_hours": deadline_hours,
            "remaining_deadline_hours": round(max(0.0, deadline_hours - elapsed_hours), 3),
        }

    def _recovery_action(self, manifest: dict[str, Any], next_stage: str) -> str:
        agent_data = manifest.get("agent", {})
        status = str(agent_data.get("status", ""))
        reason = str(agent_data.get("blocked_reason", ""))
        combined = f"{next_stage} {reason}".lower()
        job_id = str(agent_data.get("job_id", "<job_id>"))
        if status == "cancelled" or agent_data.get("cancel_requested"):
            return f"依次运行 `python3 story_agent.py resume --job {job_id}` 和 `python3 story_agent.py start --job {job_id}`。"
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
            ("release_preview", self._has_release_preview, self._stage_release_preview),
            ("package_release", self._has_release_videos, self._stage_package_release),
            ("release_qa", self._has_release_qa, self._stage_release_qa),
            ("release_video_review", self._has_release_video_review, self._stage_release_video_review),
            ("publish_package", self._has_publish_package, self._stage_publish_package),
            ("publish_package_review", self._has_publish_package_review, self._stage_publish_package_review),
            ("product_preflight", self._has_product_preflight, self._stage_product_preflight),
            ("product_annotation", self._has_product_annotation, self._stage_product_annotation),
            ("product_annotation_review", self._has_product_annotation_review, self._stage_product_annotation_review),
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
            if record.get("status") == "pending" and complete:
                mark_stage(manifest, name, "passed", message="由当前输入/已验证产物满足，或按配置安全跳过。")
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
        return self._workflow(command, "保存故事信息并识别素材")

    def _stage_source_edit(self, manifest: dict[str, Any]) -> StageResult:
        inputs = manifest.get("inputs", {})
        video = first_existing(inputs.get("greenscreen_video_original"), inputs.get("greenscreen_video"))
        if video is None:
            return StageResult("blocked", "缺少绿幕原片，无法生成故事文本和清洁视频。")
        agent_defaults = load_config().get("agent_defaults", {})
        whisper_model = str(agent_defaults.get("whisper_model", "small"))
        command = [
            sys.executable,
            str(ROOT / "source_video_pipeline.py"),
            "--project-dir",
            str(self.context.project_dir),
            "--video",
            str(video),
            "--whisper-model",
            whisper_model,
            "--language",
            "zh",
        ]
        model_dir = ROOT / "models" / "whisper"
        if model_dir.exists():
            command.extend(["--whisper-model-dir", str(model_dir)])
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
        prompt = "\n".join(
            [
                "你是中文故事听辨校对生产者。只修正 ASR 文本，不改变 segment 编号、时间范围或保留/删除决定。",
                f"原始 Whisper 转写：`{raw_transcript}`",
                f"当前剪辑决定和上下文：`{decisions}`",
                "结合完整上下文修正同音字、繁简体、他/她/它、的/地/得、专有名词和标点。不要润色、浓缩或改写原意。",
                f"把 JSON 写入：`{correction}`",
                "格式必须为 {\"raw_transcript_sha256\":\"...\",\"segments\":[{\"segment\":1,\"corrected_text\":\"...\",\"confidence\":0.0,\"notes\":\"...\"}]}。",
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
        if payload.get("raw_transcript_sha256") != raw_sha or not isinstance(corrected_segments, list) or len(corrected_segments) != expected_count:
            return StageResult("blocked", f"上下文校对未覆盖全部 segment 或源哈希不匹配：{correction}", correction)
        original = first_existing(manifest.get("inputs", {}).get("greenscreen_video_original"))
        if original is None:
            return StageResult("blocked", "缺少绿幕原片，无法应用听辨校对。", correction)
        self._archive_source_edit_attempt()
        rerun = self._run_command(
            [
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
            ],
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
        review_path = self.context.paths.status / "source_edit" / "source_edit_review.json"
        artifact_sha = file_sha256(decisions)
        prompt = "\n".join(
            [
                "你是独立文本剪辑审核员。不要延续生产者的判断，也不要修改任何生产文件。",
                f"原始转写：`{self.context.paths.status / 'source_edit' / 'raw_transcript.json'}`",
                f"自动剪辑决定：`{decisions}`",
                f"清洁分镜文本：`{story_text}`",
                "检查是否误删语义、是否仍保留口误/重复、是否默认保留最后一次完整表达、分镜是否适合约 8–12 秒。",
                "评分必须为 0–100；总分低于 85 或存在语义误删、故事缺段等关键错误时 approved 必须为 false。",
                f"把 JSON 写入：`{review_path}`",
                "JSON 必须包含 approved、score、critical_errors、issues、retry_instructions、artifact_sha256。",
                f"artifact_sha256 必须原样写为：{artifact_sha}",
                "只做审核，不要读取任何生产者推理或旧审核结论。",
            ]
        )
        result = self._codex_task(stage="source_edit_review", label="独立文本剪辑审核", handoff=decisions, prompt=prompt)
        if result.status != "done":
            return result
        try:
            payload = json.loads(review_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return StageResult("blocked", f"独立审核未写入有效 JSON：{review_path}", decisions)
        if not review_passes(payload, artifact=decisions):
            if self._can_retry_stage("source_edit_review", critical=True):
                revision_path = self.context.paths.status / "source_edit" / "source_edit_keep_overrides.json"
                revision_prompt = "\n".join(
                    [
                        "你是文本剪辑修订生产者。根据独立审核意见提出最小 segment 保留/删除修正，不要自行改写故事。",
                        f"原始转写：`{self.context.paths.status / 'source_edit' / 'raw_transcript.json'}`",
                        f"当前剪辑决定：`{decisions}`",
                        f"独立审核：`{review_path}`",
                        f"把 JSON 写入：`{revision_path}`",
                        "格式：{\"overrides\":[{\"segment\":1,\"keep\":true,\"reason\":\"...\"}]}。",
                        "只列出必须改变的 segment；不得用改写文本掩盖误删或口误。",
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
                    revision_payload = json.loads(revision_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    return StageResult("blocked", f"文本修订未写入有效覆盖 JSON：{revision_path}", review_path)
                if not isinstance(revision_payload.get("overrides"), list) or not revision_payload["overrides"]:
                    return StageResult("blocked", f"审核未通过且修订器没有给出可执行 segment 覆盖：{revision_path}", review_path)
                original = first_existing(manifest.get("inputs", {}).get("greenscreen_video_original"))
                raw_transcript = self.context.paths.status / "source_edit" / "raw_transcript.json"
                if original is None or not raw_transcript.exists():
                    return StageResult("blocked", "缺少原片或原始转写，无法执行文本剪辑自动修订。", review_path)
                self._archive_source_edit_attempt()
                rerun = self._run_command(
                    [
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
                    ]
                    + (["--text-overrides-json", str(self.context.paths.status / "source_edit" / "corrected_transcript.json")] if (self.context.paths.status / "source_edit" / "corrected_transcript.json").exists() else []),
                    "按独立审核意见重新剪辑绿幕原片",
                    log_name="source_edit_retry",
                )
                if rerun.status != "done":
                    return rerun
                return StageResult("retrying", "已按独立审核意见重新剪辑，将使用新上下文再次审核。", revision_path)
            return StageResult("blocked", f"文本剪辑审核未通过（{payload.get('score', 0)} 分）：{review_path}", review_path)
        return StageResult("done", f"文本剪辑独立审核通过：{payload.get('score')} 分", review_path)

    def _stage_codex_story_images(self, manifest: dict[str, Any]) -> StageResult:
        staging = self._codex_stage_dir("codex_story_images")
        staging_images = staging / "images"
        staging_storyboard = staging / f"{self.context.slug}_storyboard_lines.txt"
        staging_images.mkdir(parents=True, exist_ok=True)
        handoff = self._write_story_image_handoff(manifest, staging_images=staging_images, staging_storyboard=staging_storyboard)
        story_lines = self._story_lines(manifest)
        if not story_lines:
            return StageResult("blocked", "缺少可用故事镜头行，无法生成故事图片。", handoff)
        self._ensure_storyboard_from_lines(staging_storyboard, story_lines)
        self._sync_story_images_from_staging(staging, staging_storyboard, staging_images)
        missing = self._missing_story_image_indices(story_lines)
        if not missing:
            return StageResult("done", f"故事图片已完整：{len(story_lines)}/{len(story_lines)} 张。", handoff)

        batch_size = max(1, self.context.codex_story_image_batch_size)
        batch = missing[:batch_size]
        result = self._codex_task(
            stage=f"codex_story_images_{batch[0]:02d}_{batch[-1]:02d}",
            label=f"Codex 原生故事批量出图 {len(batch)} 张",
            handoff=handoff,
            prompt=self._story_images_batch_prompt(
                handoff=handoff,
                staging_images=staging_images,
                staging_storyboard=staging_storyboard,
                story_lines=story_lines,
                indices=batch,
            ),
        )
        if result.status != "done":
            for index in batch:
                self._record_story_image_status(index, "failed", result.message)
            return result
        self._sync_story_images_from_staging(staging, staging_storyboard, staging_images)
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

        manifest = self._manifest()
        if self._has_story_images(manifest):
            return StageResult("done", f"故事图片已完整生成：{len(story_lines)}/{len(story_lines)} 张。", handoff)
        current = self._story_image_count(self._image_dir())
        return StageResult("done", f"已逐张生成 {len(completed)} 张，本阶段进度：{current}/{len(story_lines)}。再次运行 Agent 将继续。", handoff)

    def _stage_prepare_jobs(self, manifest: dict[str, Any]) -> StageResult:
        storyboard = self._storyboard_path(manifest)
        image_dir = self._image_dir()
        if storyboard is None or image_dir is None:
            return StageResult("blocked", "缺少分镜文本或图片目录，无法准备图生视频任务。")
        return self._workflow([
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
        ], "准备图生视频任务")

    def _stage_story_images_review(self, manifest: dict[str, Any]) -> StageResult:
        image_dir = self._image_dir()
        storyboard = self._storyboard_path(manifest)
        if image_dir is None or storyboard is None:
            return StageResult("blocked", "缺少故事图片或分镜文本，无法独立审核。")
        contact_sheet = self._make_contact_sheet(
            sorted(path for path in image_dir.iterdir() if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}),
            self.context.paths.status / "reviews" / "story_images_contact_sheet.jpg",
        )
        bundle = write_review_bundle(
            self.context.paths.status / "reviews" / "story_images_bundle.json",
            [storyboard, image_dir],
        )
        result, payload = self._structured_review(
            stage="story_images_review",
            label="故事图片独立审核",
            bundle=bundle,
            images=[contact_sheet],
            rubric=(
                "逐镜头检查角色和服装一致性、故事语义、物理错误、文字错误、构图和相邻连续性。"
                "输出 retry_indices（需要重做的镜头编号整数数组）。角色身份错、肢体崩坏、错误文字、漏镜头属于关键错误。"
            ),
        )
        if result.status == "done":
            return result
        if payload and self._can_retry_stage("story_images_review", critical=True):
            indices = self._review_retry_indices(payload)
            if indices:
                moved = self._quarantine_story_images(indices)
                if moved:
                    return StageResult("retrying", f"图片审核未通过，已保留失败版本并排队重做镜头：{', '.join(map(str, moved))}", result.handoff)
        return result

    def _stage_timing(self, manifest: dict[str, Any]) -> StageResult:
        narration = first_existing(manifest.get("inputs", {}).get("narration"), manifest.get("inputs", {}).get("extracted_narration"))
        jobs = self._jobs_csv(manifest)
        if narration is None or jobs is None:
            return StageResult("blocked", "缺少旁白或 jobs CSV，无法写入时长。")
        command = ["timing", "--jobs-csv", str(jobs), "--narration", str(narration), "--whisper-model", "base", "--language", "zh"]
        model_dir = ROOT / "models" / "whisper"
        if model_dir.exists():
            command.extend(["--whisper-model-dir", str(model_dir)])
        return self._workflow(command, "写入旁白时长")

    def _stage_generate_videos(self, manifest: dict[str, Any]) -> StageResult:
        jobs = self._jobs_csv(manifest)
        image_dir = self._image_dir()
        if jobs is None or image_dir is None:
            return StageResult("blocked", "缺少 jobs CSV 或图片目录，无法生成视频片段。")
        videos_dir = self.context.paths.video_jobs / "videos"
        actual = len(list(videos_dir.glob("*.mp4"))) if videos_dir.exists() else 0
        pending = max(0, self._job_count(jobs) - actual)
        config = load_config()
        video_api = config.get("video_api", {}) if isinstance(config.get("video_api"), dict) else {}
        provider = resolve_video_provider(config, ROOT)
        estimated_per_clip = provider.estimated_cost_cny_per_clip
        estimate = round(pending * estimated_per_clip, 2)
        ledger = BudgetLedger(manifest)
        try:
            reservation = ledger.authorize(estimate, label=f"图生视频 {pending} 个镜头", critical=True)
        except BudgetExceeded as exc:
            return StageResult("blocked", str(exc))
        from story_project import write_manifest

        write_manifest(self.context.paths, manifest)
        result = self._workflow([
            "generate",
            "--jobs-csv",
            str(jobs),
            "--images-dir",
            str(image_dir),
            "--videos-dir",
            str(self.context.paths.video_jobs / "videos"),
        ], "调用图生视频 API 生成片段")
        manifest = self._manifest()
        ledger = BudgetLedger(manifest)
        after = len(list(videos_dir.glob("*.mp4"))) if videos_dir.exists() else 0
        generated_by_api = max(0, after - actual)
        if result.status == "done" or generated_by_api > 0:
            ledger.settle(
                reservation,
                round(generated_by_api * estimated_per_clip, 2),
                provider=provider.name,
            )
        else:
            ledger.release(reservation, reason=result.message)
        write_manifest(self.context.paths, manifest)
        if result.status != "done" and str(video_api.get("fallback_provider", "")).lower() == "browser":
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
        bundle = write_review_bundle(review_dir / "video_prompt_bundle.json", [snapshot, image_dir])
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
                "逐镜头检查动作是否具体、是否符合当前图片和故事、是否过度文学化、是否可能触发眼睛发光/肢体畸变/新增角色。",
                f"把最终确认 CSV 写入：`{decisions_csv}`",
                "CSV 必须包含 scene,image_filename,story_text,review_status,prompt,notes；每个镜头一行，review_status 必须是 approved，必要时直接在 prompt 列给出修正后的最终动作提示。",
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
        return self._workflow(["qa-videos", "--project-dir", str(self.context.project_dir), "--videos-dir", str(self.context.paths.video_jobs / "videos")], "视频片段 QA")

    def _stage_video_review(self, manifest: dict[str, Any]) -> StageResult:
        frames_dir = self.context.paths.status / "video_review_frames"
        frame_paths = sorted(path for path in frames_dir.rglob("*.jpg")) + sorted(path for path in frames_dir.rglob("*.png"))
        jobs = self._jobs_csv(manifest)
        qa_report = first_existing(manifest.get("qa", {}).get("videos"), self.context.paths.status / "qa_videos_report.md")
        videos_dir = self.context.paths.video_jobs / "videos"
        if jobs is None or qa_report is None or not frame_paths:
            return StageResult("blocked", "缺少视频任务、QA 报告或多帧抽样，无法独立审核。")
        contact_sheet = self._make_contact_sheet(frame_paths, self.context.paths.status / "reviews" / "video_contact_sheet.jpg")
        bundle = write_review_bundle(
            self.context.paths.status / "reviews" / "video_bundle.json",
            [jobs, qa_report, videos_dir, frames_dir],
        )
        result, payload = self._structured_review(
            stage="video_review",
            label="图生视频独立审核",
            bundle=bundle,
            images=[contact_sheet],
            rubric=(
                "结合分镜任务和首尾/25%/50%/75%抽帧检查动作崩坏、反物理现象、角色漂移、黑帧、文字水印和镜头连续性。"
                "输出 retry_indices（需要重新调用视频生成的镜头编号整数数组）。人物肢体崩坏、主体变形、关键动作错误属于关键错误。"
            ),
        )
        if result.status == "done":
            return result
        if payload and self._can_retry_stage("video_review", critical=True):
            indices = self._review_retry_indices(payload)
            if indices:
                moved = self._quarantine_story_videos(indices, jobs)
                if moved:
                    return StageResult("retrying", f"视频审核未通过，已保留失败版本并排队重做镜头：{', '.join(map(str, moved))}", result.handoff)
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
        return self._workflow(command, "生成配乐任务")

    def _stage_suno_generate(self, manifest: dict[str, Any]) -> StageResult:
        provided_music = self._provided_music(manifest)
        if provided_music is not None:
            return StageResult("done", f"已检测到用户提供的背景音乐，跳过 Suno 生成：{provided_music}")
        handoff = self._write_suno_handoff()
        result = self._codex_task(
            stage="suno_generate",
            label="Codex/Suno 浏览器音乐生成",
            handoff=handoff,
            prompt=(
                f"请读取并执行这份 Suno 浏览器自动化任务：\n{handoff}\n\n"
                "目标是生成并下载第一首可用音乐，按音乐分段 CSV 的 target_audio_filename 重命名，"
                "保存到指定 suno_downloads 目录。若当前 CLI 无浏览器控制能力、Suno 未登录、遇到验证码或付费弹窗，"
                f"请写入 `{self._music_dir() / 'suno_cli_blocker.md'}` 说明原因，不要假装完成。"
            ),
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
        if self._has_suno_audio(self._manifest()):
            return result
        blocker = self._music_dir() / "suno_cli_blocker.md"
        if blocker.exists():
            return StageResult("blocked", f"Suno 需要外部恢复动作：{blocker}", blocker)
        return StageResult("failed", "Suno 子任务返回但没有下载任何目标音频。", handoff)

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
        model_dir = ROOT / "models" / "whisper"
        if model_dir.exists():
            command.extend(["--whisper-model-dir", str(model_dir)])
        return self._workflow(command, "合成背景成片")

    def _stage_release_assets(self, manifest: dict[str, Any]) -> StageResult:
        result = self._workflow(["prepare-release-assets-project", "--project-dir", str(self.context.project_dir)], "生成发布视觉任务书")
        if result.status != "done":
            return result
        handoff = self.context.paths.release / "theme_assets" / "theme_assets_codex_handoff.txt"
        result = self._codex_task(
            stage="release_assets",
            label="Codex 原生发布视觉素材生成",
            handoff=handoff if handoff.exists() else None,
            prompt=build_release_assets_agent_prompt(handoff),
        )
        if result.status == "done" and not self._has_release_assets(self._manifest()):
            return StageResult("blocked", "Codex CLI 子任务已返回，但发布视觉素材/keying 参数没有完整落盘。", handoff if handoff.exists() else None)
        return result

    def _stage_release_preview(self, manifest: dict[str, Any]) -> StageResult:
        result = self._workflow(["preview-release-project", "--project-dir", str(self.context.project_dir)], "生成发布预览")
        if result.status != "done":
            return result
        preview_dir = self.context.paths.status / "release_preview_frames"
        handoff = preview_dir / "release_preview_feedback_to_codex.md"
        images = self._release_preview_images()
        preset = self.context.paths.release / "keying" / "keying_preset.json"
        keying_search = self.context.paths.release / "keying" / "keying_search.json"
        keying_candidates = self.context.paths.release / "keying" / "keying_candidates.jpg"
        if keying_candidates.exists():
            images = [keying_candidates, *images]
        bundle = write_review_bundle(
            self.context.paths.status / "reviews" / "release_preview_bundle.json",
            [preset, keying_search, keying_candidates, preview_dir],
        )
        result, payload = self._structured_review(
            stage="release_preview",
            label="发布预览独立视觉审核",
            bundle=bundle,
            images=images,
            rubric=(
                "先比较 keying_candidates.jpg 中站立帧与大手势帧的 3×3 参数候选，再检查主账号和宝库号预览中的"
                "抠像边缘、头发和手部、绿色溢出、透明孔洞、人物比例与位置、故事框覆盖、"
                "字幕安全区以及 A（人物+故事框）、B（故事框）、C（人物+主题背景）三种构图。抠像截断、人物被框遮挡、框体露缝属于关键错误。"
                "失败时在 retry_instructions 中明确给出候选 id 或可执行的 keying_preset 参数修订建议。"
            ),
        )
        if result.status == "done":
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
                    "优先从候选中选择最能兼顾头发、手部和大手势的 similarity/blend，并同步写入 keying_candidate；"
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
        return self._workflow(["package-release-project", "--project-dir", str(self.context.project_dir)], "生成主账号/宝库号发布视频")

    def _stage_release_qa(self, manifest: dict[str, Any]) -> StageResult:
        result = self._workflow(["qa-release", "--project-dir", str(self.context.project_dir)], "发布终片编码与音画同步 QA")
        report = self.context.paths.status / "qa_release_report.json"
        if result.status != "done":
            return result
        if not self._json_qa_report_passes(report):
            return StageResult("failed", f"发布终片编码或音画同步 QA 未通过：{report}", report)
        return StageResult("done", "发布终片编码与音画同步 QA 通过。", report)

    def _stage_release_video_review(self, manifest: dict[str, Any]) -> StageResult:
        videos = [self.context.paths.release / "主账号发布视频.mp4", self.context.paths.release / "宝库号发布视频.mp4"]
        if not all(path.exists() for path in videos):
            return StageResult("blocked", "主账号或宝库号发布视频缺失，无法终片独立审核。")
        frame_dir = self.context.paths.status / "release_video_review_frames"
        frames: list[Path] = []
        for video in videos:
            duration = self._probe_duration(video)
            for index, timestamp in enumerate((0.5, duration * 0.25, duration * 0.5, duration * 0.75, max(0.0, duration - 0.5)), start=1):
                target = frame_dir / video.stem / f"frame_{index:02d}_{timestamp:.1f}s.jpg"
                target.parent.mkdir(parents=True, exist_ok=True)
                command = ["ffmpeg", "-y", "-v", "error", "-ss", f"{timestamp:.3f}", "-i", str(video), "-frames:v", "1", "-q:v", "2", str(target)]
                process = subprocess.run(command, text=True, capture_output=True)
                if process.returncode == 0 and target.exists():
                    frames.append(target)
        if not frames:
            return StageResult("blocked", "发布终片无法抽帧，拒绝仅凭文件存在放行。")
        contact_sheet = self._make_contact_sheet(frames, self.context.paths.status / "reviews" / "release_videos_contact_sheet.jpg", columns=5)
        bundle = write_review_bundle(self.context.paths.status / "reviews" / "release_video_bundle.json", [*videos, frame_dir])
        result, payload = self._structured_review(
            stage="release_video_review",
            label="发布终片独立审核",
            bundle=bundle,
            images=[contact_sheet],
            rubric=(
                "检查主账号和宝库号最终竖版视频抽帧：A/B/C 切换、人物抠像、字幕、标题信息、故事框、Logo、水印、尾部提示、"
                "黑帧和安全区。任何人物截断、错误标题、画面露缝、黑帧或缺少账号版本都是关键错误。"
            ),
        )
        if result.status == "done":
            return result
        preset = self.context.paths.release / "keying" / "keying_preset.json"
        if payload and preset.exists() and self._can_retry_stage("release_video_review", critical=True):
            previous_sha = file_sha256(preset)
            review_path = self.context.paths.status / "reviews" / "release_video_review.json"
            revision = self._codex_task(
                stage=f"release_video_revision_{int(self._manifest().get('agent', {}).get('stages', {}).get('release_video_review', {}).get('attempts', 1))}",
                label="发布终片参数自动修订",
                handoff=review_path,
                prompt=(
                    f"根据独立审核 `{review_path}` 和终片抽帧修改 `{preset}` 中最小必要的抠像/布局参数。"
                    "不要伪造批准；修改后系统会重新走预览审核和终片审核。"
                ),
                images=[contact_sheet],
            )
            if revision.status != "done":
                return revision
            if file_sha256(preset) == previous_sha:
                return StageResult("blocked", "终片审核未通过，但参数修订器没有产生可验证修改。", review_path)
            archive = self.context.paths.status / "rejected" / "release_videos" / time.strftime("%Y%m%d-%H%M%S")
            archive.mkdir(parents=True, exist_ok=True)
            for video in videos:
                shutil.move(str(video), str(archive / video.name))
            return StageResult("retrying", "终片审核未通过，已保留失败成片并回到预览/渲染链重做。", review_path)
        return result

    def _stage_publish_package(self, manifest: dict[str, Any]) -> StageResult:
        result = self._workflow(["publish-package-project", "--project-dir", str(self.context.project_dir)], "生成发布物料任务包")
        if result.status != "done":
            return result
        handoff = self.context.paths.publish / "publish_package_codex_handoff.md"
        if handoff.exists():
            result = self._codex_task(
                stage="publish_package",
                label="Codex 4:3 封面衍生",
                handoff=handoff,
                prompt=build_publish_package_agent_prompt(handoff, self.context.project_dir),
            )
            if result.status == "done" and not self._has_publish_package(self._manifest()):
                return StageResult("blocked", "Codex CLI 子任务已返回，但主账号/宝库号 4:3 封面没有完整落盘。", handoff)
            return result
        return result

    def _stage_publish_package_review(self, manifest: dict[str, Any]) -> StageResult:
        publish = self.context.paths.publish
        covers = [
            publish / account / "covers" / f"cover_{ratio}.png"
            for account in ("main", "library")
            for ratio in ("3x4", "4x3", "16x9")
        ]
        copy_files = [publish / "main" / "copy.md", publish / "library" / "copy.md"]
        if not all(path.exists() for path in [*covers, *copy_files]):
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
                moved = self._quarantine_publish_files([str(item) for item in retry_files])
                if moved:
                    return StageResult("retrying", f"发布物料机器 QA 未通过，已保留并排队重做 {len(moved)} 个文件。", qa_json)
            return StageResult("blocked", f"发布物料机器 QA 未通过或已达到重做上限：{qa_json}", qa_json)
        contact_sheet = self._make_contact_sheet(covers, self.context.paths.status / "reviews" / "publish_covers_contact_sheet.jpg", columns=3)
        bundle = write_review_bundle(
            self.context.paths.status / "reviews" / "publish_package_bundle.json",
            [*covers, *copy_files, qa_report, qa_json],
        )
        result, payload = self._structured_review(
            stage="publish_package_review",
            label="发布物料独立审核",
            bundle=bundle,
            images=[contact_sheet],
            rubric=(
                "检查两个账号文案定位、标题准确性、敏感承诺和话题相关性；检查六张封面标题文字、真人一致性、故事角色、"
                "比例构图和安全区。每个比例必须是独立合理版式，不能只是机械裁切。失败时输出 retry_files，使用相对发布物料目录的路径。"
            ),
        )
        if result.status == "done":
            return result
        if payload and self._can_retry_stage("publish_package_review", critical=True):
            retry_files = payload.get("retry_files", [])
            if isinstance(retry_files, list):
                moved = self._quarantine_publish_files([str(item) for item in retry_files])
                if moved:
                    return StageResult("retrying", f"发布物料审核未通过，已保留失败版本并排队重做 {len(moved)} 个文件。", result.handoff)
        return result

    def _stage_product_preflight(self, manifest: dict[str, Any]) -> StageResult:
        return self._workflow(["product-package-preflight-project", "--project-dir", str(self.context.project_dir)], "资料包前置审查")

    def _stage_product_annotation(self, manifest: dict[str, Any]) -> StageResult:
        handoff = self.context.paths.product / "_work" / "第16步资料包_Codex前置审查.md"
        images = self._product_preview_images()
        result = self._codex_task(
            stage="product_annotation",
            label="Codex 资料包示范预览审查与朗读标注精修",
            handoff=handoff if handoff.exists() else None,
            prompt=build_product_annotation_agent_prompt(
                handoff,
                self.context.paths.product / "_work" / "annotation.json",
                self.context.paths.product / "_work" / "demo_params.json",
            ),
            images=images,
        )
        if result.status == "done" and not self._has_product_annotation(self._manifest()):
            return StageResult("blocked", "Codex CLI 子任务已返回，但没有写入精修 annotation.json/docx。", handoff if handoff.exists() else None)
        return result

    def _stage_product_package(self, manifest: dict[str, Any]) -> StageResult:
        annotation = self._product_annotation()
        if annotation is None:
            return StageResult("blocked", "缺少精修朗读标注。")
        command = ["product-package-project", "--project-dir", str(self.context.project_dir)]
        if annotation.suffix.lower() == ".json":
            command.extend(["--annotation-json", str(annotation)])
        else:
            command.extend(["--annotation-docx", str(annotation)])
        demo_params = self.context.paths.product / "_work" / "demo_params.json"
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
        return self._workflow(command, "正式打包资料包")

    def _stage_product_annotation_review(self, manifest: dict[str, Any]) -> StageResult:
        annotation = self._product_annotation()
        if annotation is None:
            return StageResult("blocked", "缺少朗读标注，无法独立审核。")
        work = self.context.paths.product / "_work"
        demo_params = work / "demo_params.json"
        handoff = work / "第16步资料包_Codex前置审查.md"
        artifacts = [annotation]
        if demo_params.exists():
            artifacts.append(demo_params)
        if handoff.exists():
            artifacts.append(handoff)
        bundle = write_review_bundle(self.context.paths.status / "reviews" / "product_annotation_bundle.json", artifacts)
        images = self._product_preview_images()
        result, payload = self._structured_review(
            stage="product_annotation_review",
            label="朗读标注与示范参数独立审核",
            bundle=bundle,
            images=images,
            rubric=(
                "检查朗读标注是否逐段覆盖清洁文稿，重音、停连、语气和动作提示是否适合儿童表演，是否混入绵羊姐姐等对外禁用口吻；"
                "结合预览检查示范视频裁切参数是否会截断手部或身体。漏段、错重音导致语义改变属于关键错误。"
            ),
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
        bundle = write_review_bundle(self.context.paths.status / "reviews" / "product_package_bundle.json", [base, advanced, report, report_json])
        result, payload = self._structured_review(
            stage="product_package_review",
            label="资料包独立审核",
            bundle=bundle,
            images=[],
            rubric=(
                "核对基础版必须包含故事文稿、朗读标注、音乐、示范视频、背景图片；进阶版必须包含故事文稿、朗读标注、音乐、"
                "示范视频、含/无字幕 PPT、含/无字幕背景视频。检查文件名、重复/缺失、对外禁用口吻和 QA 报告。缺少任一必备文件属于关键错误。"
            ),
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
        command = [sys.executable, str(ROOT / "story_workflow.py"), *args]
        return self._run_command(command, label, log_name=args[0])

    def _run_command(self, command: list[str], label: str, *, log_name: str) -> StageResult:
        if not self.context.execute:
            return StageResult("done", "dry-run: " + " ".join(str(part) for part in command))
        started = time.time()
        log_dir = self.context.paths.status / "agent_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{int(started)}_{log_name}.log"
        process = subprocess.Popen(command, cwd=str(ROOT), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
                    log_path.write_text((stdout or "") + ("\n[stderr]\n" + stderr if stderr else "") + "\n[cancelled]\n", encoding="utf-8")
                    return StageResult("cancelled", f"{label}已按取消请求停止，日志：{log_path}", log_path)
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
        if self.context.codex_model:
            command.extend(["--model", self.context.codex_model])
        for image in images:
            if image.exists():
                command.extend(["--image", str(image)])
        command.append(prompt_text)
        display_command = [*command[:-1], "<prompt>"]
        process = subprocess.Popen(command, cwd=str(ROOT), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
        if process.returncode != 0:
            status = classify_command_failure((stdout or "") + "\n" + (stderr or ""))
            return StageResult(status, f"Codex CLI 子任务失败：{log_path}", log_path)
        return StageResult("done", str(log_path), output_path)

    def _codex_stage_dir(self, stage: str) -> Path:
        safe_project = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in self.context.slug) or "story"
        path = ROOT / "output" / "story_agent_cli" / safe_project / stage
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

    def _ensure_storyboard_from_lines(self, storyboard: Path, story_lines: list[str]) -> None:
        if storyboard.exists():
            return
        storyboard.parent.mkdir(parents=True, exist_ok=True)
        storyboard.write_text("\n".join(story_lines) + "\n", encoding="utf-8")

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
        return "\n".join(
            [
                "请执行这份由工作台同源模板生成的儿童故事出图任务：",
                f"`{handoff}`",
                "",
                "这是全自动 Agent 模式，不需要向用户确认分镜；但必须先在任务指定位置写入分镜表、视觉圣经和图生视频提示词文件，再连续生成图片。",
                "图生视频提示词文件的 CSV 必须包含 `scene,story_text,visual_description,prompt`；`prompt` 要作为后续图生视频 API 和审核页直接使用的最终提示词。图生视频已经有当前图片作为视觉约束，只写具体动作、表情、道具运动、镜头运动和少量禁止项，不要复制文生图视觉圣经、服装细节或画风长描述，也不能用“角色动作自然克制、镜头缓慢推进或轻移”之类通用模板充数。",
                "",
                f"本轮应补齐这些镜头编号：{', '.join(str(i) for i in indices)}。",
                f"图片暂存目录：`{staging_images}`",
                f"分镜文本暂存：`{staging_storyboard}`",
                "请使用任务书指定的稳定命名 `{slug}_scene_XX.png`，不要使用旧的 `{slug}_XX.png` 命名。",
                "如果 imagegen 默认保存到 `$CODEX_HOME/generated_images/...`，生成后把每张最终 PNG 复制到任务书指定的暂存目录。",
                "完成后只用简短中文说明生成成功的文件路径，以及任何未能完成的镜头编号。",
            ]
        )

    def _missing_story_image_indices(self, story_lines: list[str]) -> list[int]:
        final_images = self.context.paths.images / "images"
        if not self._has_story_visual_control():
            return list(range(1, len(story_lines) + 1))
        return [index for index in range(1, len(story_lines) + 1) if not (final_images / self._story_image_filename(index)).exists()]

    def _has_story_visual_control(self) -> bool:
        safe_project = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in self.context.slug) or "story"
        staging = ROOT / "output" / "story_agent_cli" / safe_project / "codex_story_images"
        return (staging / f"{self.context.slug}_visual_bible.md").exists() and (staging / "images" / f"{self.context.slug}_style_anchor.png").exists()

    def _record_story_image_status(self, index: int, status: str, message: str) -> None:
        progress = self.state.setdefault("codex_story_images", {})
        images = progress.setdefault("images", {})
        images[f"{index:02d}"] = {"status": status, "message": message, "time": now()}
        progress["updated_at"] = now()
        self._save_state()

    def _release_preview_images(self) -> list[Path]:
        preview_dir = self.context.paths.status / "release_preview_frames"
        preferred = [
            preview_dir / "preview_contact_sheet.png",
            preview_dir / "main_002s_a_h84.png",
            preview_dir / "main_002s_a_h90.png",
            preview_dir / "main_002s_c.png",
            preview_dir / "main_037s_b.png",
            preview_dir / "library_002s.png",
            preview_dir / "library_037s.png",
        ]
        existing = [path for path in preferred if path.exists()]
        if existing:
            return existing[:8]
        return sorted(path for path in preview_dir.glob("*.png"))[:8] if preview_dir.exists() else []

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
            draw.text((x, y + 184), source.name[:45], fill="black")
        target.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(target, quality=88)
        return target

    def _probe_duration(self, path: Path) -> float:
        process = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            text=True,
            capture_output=True,
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
                f"artifact_sha256 必须原样写为：{bundle_sha}",
            ]
        )
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

    def _can_retry_stage(self, stage: str, *, critical: bool = False) -> bool:
        manifest = self._manifest()
        defaults = load_config().get("agent_defaults", {})
        limit = int(defaults.get("max_critical_attempts" if critical else "max_attempts", 3 if critical else 2))
        attempts = int(manifest.get("agent", {}).get("stages", {}).get(stage, {}).get("attempts", 1))
        return attempts < limit

    def _quarantine_story_images(self, indices: list[int]) -> list[int]:
        image_dir = self._image_dir()
        if image_dir is None:
            return []
        quarantine = self.context.paths.status / "rejected" / "story_images" / time.strftime("%Y%m%d-%H%M%S")
        quarantine.mkdir(parents=True, exist_ok=True)
        moved: list[int] = []
        for index in indices:
            source = image_dir / self._story_image_filename(index)
            if source.exists():
                shutil.move(str(source), str(quarantine / source.name))
                moved.append(index)
        return moved

    def _archive_source_edit_attempt(self) -> Path:
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
        for source in candidates:
            if source is None or source == original or not source.exists():
                continue
            target = archive / source.name
            if not target.exists():
                shutil.move(str(source), str(target))
        return archive

    def _quarantine_story_videos(self, indices: list[int], jobs: Path) -> list[int]:
        with jobs.open(encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            rows = list(reader)
            fieldnames = list(reader.fieldnames or [])
        quarantine = self.context.paths.status / "rejected" / "story_videos" / time.strftime("%Y%m%d-%H%M%S")
        quarantine.mkdir(parents=True, exist_ok=True)
        moved: list[int] = []
        selected = set(indices)
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
            for key in ("task_id", "video_url", "error", "api_response", "query_response"):
                if key in row:
                    row[key] = ""
            row["status"] = "todo"
            moved.append(scene)
        with jobs.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        qa_report = self.context.paths.status / "qa_videos_report.md"
        if qa_report.exists():
            shutil.move(str(qa_report), str(quarantine / qa_report.name))
        frames_dir = self.context.paths.status / "video_review_frames"
        if frames_dir.exists():
            shutil.move(str(frames_dir), str(quarantine / frames_dir.name))
        return moved

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
        preview_dir = self.context.paths.product / "_work" / "demo_preview"
        return sorted(path for path in preview_dir.glob("*.png"))[:8] if preview_dir.exists() else []

    def _has_imported_inbox(self, manifest: dict[str, Any]) -> bool:
        return self.context.inbox is None or bool(self.state.get("inbox_imported_at"))

    def _has_source_edit(self, manifest: dict[str, Any]) -> bool:
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
        try:
            payload = json.loads(review_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return review_passes(payload, artifact=decisions)

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

    def _has_story_images(self, manifest: dict[str, Any]) -> bool:
        image_dir = self._image_dir()
        storyboard = self._storyboard_path(manifest)
        if image_dir is None or storyboard is None:
            return False
        expected = self._expected_story_image_count(manifest, storyboard)
        return expected > 0 and all((image_dir / self._story_image_filename(index)).exists() for index in range(1, expected + 1))

    def _has_story_images_review(self, manifest: dict[str, Any]) -> bool:
        return self._review_stage_current("story_images_review")

    def _has_jobs_csv(self, manifest: dict[str, Any]) -> bool:
        return self._jobs_csv(manifest) is not None

    def _has_timing(self, manifest: dict[str, Any]) -> bool:
        timings = first_existing(manifest.get("outputs", {}).get("timings_json"), self.context.paths.assembly / "timings.json")
        if timings is not None:
            return True
        jobs = self._jobs_csv(manifest)
        if jobs is None:
            return False
        try:
            with jobs.open(encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
            return bool(rows) and all(row.get("duration") or row.get("frames") for row in rows)
        except Exception:
            return False

    def _has_generated_videos(self, manifest: dict[str, Any]) -> bool:
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
        return bool(targets) and all(name and (videos / name).is_file() and (videos / name).stat().st_size > 0 for name in targets)

    def _has_video_prompt_review(self, manifest: dict[str, Any]) -> bool:
        review_dir = self.context.paths.status / "reviews"
        bundle = review_dir / "video_prompt_bundle.json"
        review = review_dir / "video_prompt_review.json"
        snapshot = review_dir / "video_prompt_inputs.json"
        decisions = self.context.paths.video_jobs / "prompt_review_decisions.csv"
        jobs = self._jobs_csv(manifest)
        if jobs is None or not bundle.exists() or not review.exists() or not snapshot.exists() or not decisions.exists() or not review_bundle_is_current(bundle):
            return False
        try:
            payload = json.loads(review.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return review_passes(payload, artifact=bundle) and video_prompt_review_matches_current(jobs, snapshot, decisions)

    def _has_video_qa(self, manifest: dict[str, Any]) -> bool:
        return first_existing(manifest.get("qa", {}).get("videos"), self.context.paths.status / "qa_videos_report.md") is not None

    def _has_video_review(self, manifest: dict[str, Any]) -> bool:
        return self._review_stage_current("video_review")

    def _has_applied_review(self, manifest: dict[str, Any]) -> bool:
        return (self.context.paths.assembly / "script_lines.txt").exists() and (self.context.paths.assembly / "clips").exists()

    def _has_music_request(self, manifest: dict[str, Any]) -> bool:
        if self._provided_music(manifest) is not None:
            return True
        return self._music_plan().exists() or (self._music_dir() / f"{self.context.slug}_suno_music_request.md").exists() or self._background_music().exists()

    def _has_suno_audio(self, manifest: dict[str, Any]) -> bool:
        if self._provided_music(manifest) is not None:
            return True
        downloads = self._suno_downloads_dir()
        return downloads.exists() and any(path.suffix.lower() in AUDIO_EXTENSIONS for path in downloads.iterdir())

    def _has_background_music(self, manifest: dict[str, Any]) -> bool:
        if self._provided_music(manifest) is not None:
            return True
        return self._background_music().exists()

    def _has_music_qa(self, manifest: dict[str, Any]) -> bool:
        return self._music_qa_report_passes(self.context.paths.status / "qa_music_report.json")

    def _music_qa_report_passes(self, report: Path) -> bool:
        return self._json_qa_report_passes(report)

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
        return all((self.context.paths.assembly / name).exists() for name in ("story_no_subs_bgm.mp4", "story_sales_subs_bgm.mp4", "story_demo_voice_bgm.mp4"))

    def _has_release_assets(self, manifest: dict[str, Any]) -> bool:
        theme = self.context.paths.release / "theme_assets"
        return all((theme / name).exists() for name in ("main_release_plate.png", "library_release_plate.png", "main_background_16x9.png", "story_frame_a.png")) and (self.context.paths.release / "keying" / "keying_preset.json").exists()

    def _has_release_preview(self, manifest: dict[str, Any]) -> bool:
        return self._review_stage_current("release_preview")

    def _has_release_videos(self, manifest: dict[str, Any]) -> bool:
        return (self.context.paths.release / "主账号发布视频.mp4").exists() and (self.context.paths.release / "宝库号发布视频.mp4").exists()

    def _has_release_qa(self, manifest: dict[str, Any]) -> bool:
        return self._json_qa_report_passes(self.context.paths.status / "qa_release_report.json")

    def _has_release_video_review(self, manifest: dict[str, Any]) -> bool:
        return self._review_stage_current("release_video_review")

    def _has_publish_package(self, manifest: dict[str, Any]) -> bool:
        publish = self.context.paths.publish
        required = [
            publish / account / "covers" / f"cover_{ratio}.png"
            for account in ("main", "library")
            for ratio in ("3x4", "4x3", "16x9")
        ]
        required.extend([publish / "main" / "copy.md", publish / "library" / "copy.md"])
        return all(path.exists() for path in required)

    def _has_publish_package_review(self, manifest: dict[str, Any]) -> bool:
        return self._review_stage_current("publish_package_review")

    def _has_product_preflight(self, manifest: dict[str, Any]) -> bool:
        return (self.context.paths.product / "_work" / "第16步资料包_Codex前置审查.md").exists()

    def _has_product_annotation(self, manifest: dict[str, Any]) -> bool:
        return self._product_annotation() is not None

    def _has_product_annotation_review(self, manifest: dict[str, Any]) -> bool:
        return self._review_stage_current("product_annotation_review")

    def _has_product_package(self, manifest: dict[str, Any]) -> bool:
        outputs = manifest.get("outputs", {})
        return first_existing(outputs.get("product_base")) is not None and first_existing(outputs.get("product_advanced")) is not None

    def _has_product_package_review(self, manifest: dict[str, Any]) -> bool:
        return self._review_stage_current("product_package_review")

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
            f"全自动模式下，必须先把视觉圣经保存到：{staging_images.parent / (self.context.slug + '_visual_bible.md')}。",
            f"必须先生成一张非最终交付的风格/角色锚点图，保存到：{staging_images / (self.context.slug + '_style_anchor.png')}。",
            "不要出现绵羊姐姐形象、主持人形象、羊、小羊、人偶或任何与品牌相关的角色形象。",
            "用户提供的原文已经按镜头分行；原则上每一行就是一个独立镜头。",
            f"如果标题镜头需要文字，只能使用本集标题“{self.context.story_name}”，不得套用任何历史样例标题。",
            "如果结尾需要道理文本，必须从本集清洁文稿提炼，先逐字校对再生成；不得套用历史样例道理。",
            "除明确的本集标题和结尾道理文本外，其他镜头不要生成中文文字、字幕、水印。",
            "原文里的“绵羊姐姐”“小朋友们”“我的故事讲完了”只是旁白口吻，绝对不能变成画面人物或角色。",
        ]
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
                "执行口径：如果提示词和分段 CSV 尚未从任务书生成，请先由 Codex 生成它们。然后打开 Suno，逐条粘贴提示词，选择 instrumental，点击 create，下载第一首可用结果，并按 CSV 的 target_audio_filename 重命名保存。",
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
        story_text = first_existing(manifest.get("inputs", {}).get("story_text"))
        if story_text is None:
            return []
        try:
            text = story_text.read_text(encoding="utf-8-sig", errors="ignore")
        except Exception:
            return []
        return [line.strip() for line in text.splitlines() if line.strip()]

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
        work = self.context.paths.product / "_work"
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
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state["updated_at"] = now()
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _record_stage(self, stage: str, result: StageResult) -> None:
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
        if runtime_status in {"blocked", "failed", "cancelled"}:
            manifest["agent"]["status"] = runtime_status
        from story_project import write_manifest

        write_manifest(self.context.paths, manifest)

    def _provider_for_stage(self, stage: str) -> str:
        if stage == "generate_videos":
            try:
                return resolve_video_provider(load_config(), ROOT).name
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Codex 故事生产 Agent：状态机 + 现有脚本 + Codex 原生动作交接")
    subparsers = parser.add_subparsers(dest="command", required=True)

    submit = subparsers.add_parser("submit", help="仅投喂一段绿幕视频并创建可续跑任务")
    submit.add_argument("--video", required=True, type=Path)
    submit.add_argument("--projects-root", default=Path.home() / "Desktop", type=Path)
    submit.add_argument("--story-name", default="")
    submit.add_argument("--slug", default="")
    submit.add_argument("--registry", type=Path)
    submit.add_argument("--soft-budget", default=50.0, type=float)
    submit.add_argument("--hard-budget", default=100.0, type=float)
    submit.add_argument("--deadline-hours", default=10.0, type=float)
    submit.add_argument("--force", action="store_true", help="即使同一原片已投喂也创建新任务")

    run = subparsers.add_parser("run", help="推进 Agent loop")
    run.add_argument("--job", default="", help="submit 返回的任务 ID")
    run.add_argument("--registry", type=Path)
    run.add_argument("--inbox", type=Path, help="投喂区：放故事文本、音频、绿幕视频")
    run.add_argument("--project-dir", type=Path, help="故事项目目录；默认 ~/Desktop/故事剪辑：故事名")
    run.add_argument("--story-name", default="")
    run.add_argument("--slug", default="")
    run.add_argument("--execute", action="store_true", help="真正执行本地命令；默认只 dry-run")
    run.add_argument("--max-steps", default=999, type=int, help="本轮最多推进阶段数；默认 999，面向一键跑完整链路")
    run.add_argument("--update-latest-episode", action="store_true")
    run.add_argument("--codex-mode", choices=["handoff", "cli"], default="handoff", help="智能节点处理方式：handoff=生成交接文件后暂停；cli=自动调用 codex exec")
    run.add_argument("--codex-model", default="", help="传给 codex exec 的模型；留空使用 CLI 默认模型")
    run.add_argument("--codex-sandbox", choices=["read-only", "workspace-write", "danger-full-access"], default="workspace-write")
    run.add_argument("--codex-approval", choices=["untrusted", "on-request", "never"], default="never")
    run.add_argument("--codex-path", default="codex")
    run.add_argument("--codex-timeout", default=3600, type=int, help="单个 codex exec 子任务超时时间，秒")
    run.add_argument("--codex-story-image-batch-size", default=15, type=int, help="codex_story_images 每个 CLI 子任务批量生成的图片数量；默认 15，通常覆盖一个完整故事")

    start = subparsers.add_parser("start", help="在后台启动无人值守 Agent supervisor")
    start.add_argument("--job", required=True)
    start.add_argument("--registry", type=Path)
    start.add_argument("--codex-model", default="")
    start.add_argument("--codex-timeout", default=3600, type=int)
    start.add_argument("--max-steps", default=999, type=int)

    status = subparsers.add_parser("status", help="查看 Agent 下一阶段")
    status.add_argument("--job", default="")
    status.add_argument("--registry", type=Path)
    status.add_argument("--project-dir", type=Path)
    status.add_argument("--story-name", default="")
    status.add_argument("--slug", default="")

    for command_name, help_text in (
        ("resume", "清除取消/阻塞标记，允许任务继续"),
        ("cancel", "请求停止任务，不删除任何产物"),
        ("report", "生成早晨交付摘要"),
    ):
        command = subparsers.add_parser(command_name, help=help_text)
        command.add_argument("--job", default="")
        command.add_argument("--registry", type=Path)
        command.add_argument("--project-dir", type=Path)

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

    args = parser.parse_args()
    if args.command == "submit":
        if args.soft_budget < 0 or args.hard_budget <= 0 or args.soft_budget > args.hard_budget:
            parser.error("预算必须满足 0 <= soft-budget <= hard-budget")
        registry = JobRegistry(args.registry)
        job_id, project_dir, created = submit_video_job(
            args.video,
            projects_root=args.projects_root,
            story_name=args.story_name,
            slug=args.slug,
            registry=registry,
            force=args.force,
            soft_budget_cny=args.soft_budget,
            hard_budget_cny=args.hard_budget,
            deadline_hours=args.deadline_hours,
        )
        print(json.dumps({"job_id": job_id, "project_dir": str(project_dir), "created": created}, ensure_ascii=False, indent=2))
        return
    if args.command == "start":
        registry = JobRegistry(args.registry)
        project_dir = registry.resolve(args.job)
        status_dir = project_paths(project_dir).status
        status_dir.mkdir(parents=True, exist_ok=True)
        supervisor_path = status_dir / "story_agent_supervisor.json"
        if supervisor_path.exists():
            try:
                existing = json.loads(supervisor_path.read_text(encoding="utf-8"))
                existing_pid = int(existing.get("pid", 0))
                if existing_pid > 0:
                    os.kill(existing_pid, 0)
                    print(json.dumps({"job_id": args.job, "project_dir": str(project_dir), "pid": existing_pid, "started": False, "message": "supervisor 已在运行"}, ensure_ascii=False, indent=2))
                    return
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        log_path = status_dir / "story_agent_supervisor.log"
        command = [
            sys.executable,
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
        ]
        if args.registry:
            command.extend(["--registry", str(args.registry.expanduser())])
        if args.codex_model:
            command.extend(["--codex-model", args.codex_model])
        with log_path.open("a", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                command,
                cwd=str(ROOT),
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        from story_project import save_json

        save_json(
            supervisor_path,
            {"job_id": args.job, "project_dir": str(project_dir), "pid": process.pid, "started_at": now(), "log": str(log_path), "command": command},
        )
        print(json.dumps({"job_id": args.job, "project_dir": str(project_dir), "pid": process.pid, "started": True, "log": str(log_path)}, ensure_ascii=False, indent=2))
        return
    if args.command == "run":
        if args.job:
            args.project_dir = JobRegistry(args.registry).resolve(args.job)
            existing_manifest = load_manifest(project_paths(args.project_dir)) or {}
            existing_story = existing_manifest.get("story", {}) if isinstance(existing_manifest.get("story"), dict) else {}
            args.story_name = args.story_name or str(existing_story.get("name") or "")
            args.slug = args.slug or str(existing_story.get("slug") or "")
        story_name = infer_story_name(args.inbox, args.story_name, args.project_dir)
        slug = args.slug.strip() or slugify(story_name)
        project_dir = args.project_dir.expanduser() if args.project_dir else Path.home() / "Desktop" / f"故事剪辑：{story_name}"
        context = AgentContext(
            project_dir=project_dir,
            inbox=args.inbox.expanduser() if args.inbox else None,
            story_name=story_name,
            slug=slug,
            execute=args.execute,
            update_latest_episode=args.update_latest_episode,
            codex_mode=args.codex_mode,
            codex_model=args.codex_model,
            codex_sandbox=args.codex_sandbox,
            codex_approval=args.codex_approval,
            codex_path=args.codex_path,
            codex_timeout=args.codex_timeout,
            codex_story_image_batch_size=max(1, args.codex_story_image_batch_size),
        )
        try:
            exit_code = StoryAgent(context).run(max(1, args.max_steps))
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
    if args.command == "status":
        if args.job:
            args.project_dir = JobRegistry(args.registry).resolve(args.job)
            existing_manifest = load_manifest(project_paths(args.project_dir)) or {}
            existing_story = existing_manifest.get("story", {}) if isinstance(existing_manifest.get("story"), dict) else {}
            args.story_name = args.story_name or str(existing_story.get("name") or "")
            args.slug = args.slug or str(existing_story.get("slug") or "")
        if args.project_dir is None:
            parser.error("status 需要 --job 或 --project-dir")
        story_name = infer_story_name(None, args.story_name, args.project_dir)
        slug = args.slug.strip() or slugify(story_name)
        context = AgentContext(
            project_dir=args.project_dir.expanduser(),
            inbox=None,
            story_name=story_name,
            slug=slug,
            execute=False,
            update_latest_episode=False,
            codex_mode="handoff",
            codex_model="",
            codex_sandbox="workspace-write",
            codex_approval="never",
            codex_path="codex",
            codex_timeout=900,
        )
        StoryAgent(context, read_only=True).status()
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
