from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from story_project import init_project, load_manifest, project_paths, save_json, slugify, write_internal_agent_reports, write_manifest


MANIFEST_VERSION = 2
DEFAULT_SOFT_BUDGET_CNY = 50.0
DEFAULT_HARD_BUDGET_CNY = 100.0
DEFAULT_DEADLINE_HOURS = 10.0
DEFAULT_MIN_FREE_DISK_GB = 10.0
PASS_SCORE = 85
STAGE_STATUSES = {"pending", "running", "reviewing", "retrying", "passed", "blocked", "failed", "cancelled"}
STORY_STAGE_SEQUENCE = (
    "import_inbox",
    "source_edit",
    "source_text_correction",
    "source_edit_review",
    "setup_project",
    "codex_story_images",
    "story_images_review",
    "prepare_jobs",
    "timing",
    "video_prompt_review",
    "generate_videos",
    "video_qa",
    "video_review",
    "apply_review",
    "music_request",
    "suno_generate",
    "assemble_music",
    "music_qa",
    "assemble_final",
    "release_assets",
    "release_preview",
    "package_release",
    "release_qa",
    "release_video_review",
    "publish_package",
    "publish_package_review",
    "product_preflight",
    "product_annotation",
    "product_annotation_review",
    "product_package",
    "product_package_review",
    "final_delivery",
    "doctor",
)
STAGE_ESTIMATES_MINUTES = {
    "import_inbox": 1,
    "source_edit": 25,
    "source_text_correction": 8,
    "source_edit_review": 8,
    "setup_project": 2,
    "codex_story_images": 45,
    "story_images_review": 12,
    "prepare_jobs": 2,
    "timing": 8,
    "video_prompt_review": 10,
    "generate_videos": 120,
    "video_qa": 8,
    "video_review": 12,
    "apply_review": 3,
    "music_request": 8,
    "suno_generate": 30,
    "assemble_music": 5,
    "music_qa": 3,
    "assemble_final": 25,
    "release_assets": 25,
    "release_preview": 12,
    "package_release": 30,
    "release_qa": 5,
    "release_video_review": 12,
    "publish_package": 35,
    "publish_package_review": 12,
    "product_preflight": 5,
    "product_annotation": 25,
    "product_annotation_review": 10,
    "product_package": 20,
    "product_package_review": 10,
    "final_delivery": 5,
    "doctor": 3,
}


class AgentRuntimeError(RuntimeError):
    pass


class BudgetExceeded(AgentRuntimeError):
    pass


class JobCancelled(AgentRuntimeError):
    pass


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def media_fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"sha256:{file_sha256(path)}:bytes:{stat.st_size}"


def probe_source_video(path: Path) -> dict[str, Any]:
    process = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,format_name,bit_rate",
            "-show_entries",
            "stream=index,codec_type,codec_name,width,height,pix_fmt,r_frame_rate,color_space,color_transfer,color_primaries,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        text=True,
        capture_output=True,
    )
    if process.returncode != 0:
        raise ValueError(f"无法读取绿幕视频编码信息：{process.stderr.strip() or path}")
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ffprobe 返回无效 JSON：{path}") from exc
    streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
    video_stream = next((item for item in streams if item.get("codec_type") == "video"), None)
    if not isinstance(video_stream, dict):
        raise ValueError(f"输入文件没有视频轨：{path}")
    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ValueError(f"无法读取视频尺寸：{path}")
    if width <= height:
        raise ValueError(f"首版只接受横屏绿幕原片，当前尺寸为 {width}x{height}")
    format_info = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    try:
        duration = float(format_info.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise ValueError(f"视频时长无效：{path}")
    audio_stream = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if not isinstance(audio_stream, dict):
        raise ValueError(f"绿幕原片没有旁白音轨，无法作为唯一输入：{path}")
    return {
        "duration_sec": round(duration, 3),
        "format_name": str(format_info.get("format_name", "")),
        "bit_rate": int(format_info.get("bit_rate") or 0),
        "video": {
            key: video_stream.get(key)
            for key in ("codec_name", "width", "height", "pix_fmt", "r_frame_rate", "color_space", "color_transfer", "color_primaries")
        },
        "audio": {key: audio_stream.get(key) for key in ("codec_name", "sample_rate", "channels")},
    }


def ensure_manifest_v2(
    manifest: dict[str, Any],
    *,
    job_id: str = "",
    soft_budget_cny: float = DEFAULT_SOFT_BUDGET_CNY,
    hard_budget_cny: float = DEFAULT_HARD_BUDGET_CNY,
    deadline_hours: float = DEFAULT_DEADLINE_HOURS,
) -> dict[str, Any]:
    manifest["version"] = MANIFEST_VERSION
    agent = manifest.setdefault("agent", {})
    if job_id:
        agent["job_id"] = job_id
    agent.setdefault("job_id", "")
    agent.setdefault("status", "pending")
    agent.setdefault("cancel_requested", False)
    agent.setdefault("heartbeat_at", "")
    agent.setdefault("last_checkpoint", "")
    agent.setdefault("blocked_reason", "")
    agent.setdefault("deadline_hours", float(deadline_hours))
    agent.setdefault("min_free_disk_gb", DEFAULT_MIN_FREE_DISK_GB)
    agent.setdefault("started_at", "")
    agent.setdefault("finished_at", "")
    budget = agent.setdefault("budget", {})
    budget.setdefault("currency", "CNY")
    budget.setdefault("soft_limit", float(soft_budget_cny))
    budget.setdefault("hard_limit", float(hard_budget_cny))
    budget.setdefault("spent", 0.0)
    budget.setdefault("reserved", 0.0)
    budget.setdefault("entries", [])
    agent.setdefault("source", {})
    stages = agent.setdefault("stages", {})
    for stage in STORY_STAGE_SEQUENCE:
        record = stages.setdefault(stage, {})
        for key, default in (
            ("status", "pending"),
            ("attempts", 0),
            ("started_at", ""),
            ("finished_at", ""),
            ("message", ""),
            ("artifacts", []),
            ("input_hashes", {}),
            ("output_hashes", {}),
            ("provider", ""),
            ("request_id", ""),
            ("actual_cost", 0.0),
            ("retry_reason", ""),
            ("review", {}),
        ):
            record.setdefault(key, default)
    agent.setdefault("events", [])
    return manifest


def stage_record(manifest: dict[str, Any], stage: str) -> dict[str, Any]:
    ensure_manifest_v2(manifest)
    record = manifest["agent"]["stages"].setdefault(
        stage,
        {
            "status": "pending",
            "attempts": 0,
            "started_at": "",
            "finished_at": "",
            "message": "",
            "artifacts": [],
            "input_hashes": {},
            "output_hashes": {},
            "provider": "",
            "request_id": "",
            "actual_cost": 0.0,
            "retry_reason": "",
            "review": {},
        },
    )
    for key, default in (
        ("input_hashes", {}),
        ("output_hashes", {}),
        ("provider", ""),
        ("request_id", ""),
        ("actual_cost", 0.0),
        ("retry_reason", ""),
    ):
        record.setdefault(key, default)
    return record


def mark_stage(
    manifest: dict[str, Any],
    stage: str,
    status: str,
    *,
    message: str = "",
    artifacts: list[Path | str] | None = None,
    input_hashes: dict[str, str] | None = None,
    output_hashes: dict[str, str] | None = None,
    provider: str | None = None,
    request_id: str | None = None,
    actual_cost: float | None = None,
    retry_reason: str | None = None,
    review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in STAGE_STATUSES:
        raise ValueError(f"未知阶段状态：{status}")
    record = stage_record(manifest, stage)
    previous = record.get("status")
    if status == "running" and previous != "running":
        record["attempts"] = int(record.get("attempts", 0)) + 1
    if status == "running" and not record.get("started_at"):
        record["started_at"] = now()
    if status in {"passed", "blocked", "failed", "cancelled"}:
        record["finished_at"] = now()
    record["status"] = status
    record["message"] = message
    if artifacts is not None:
        record["artifacts"] = [str(Path(item)) for item in artifacts]
    if input_hashes is not None:
        record["input_hashes"] = dict(input_hashes)
    if output_hashes is not None:
        record["output_hashes"] = dict(output_hashes)
    if provider is not None:
        record["provider"] = provider
    if request_id is not None:
        record["request_id"] = request_id
    if actual_cost is not None:
        record["actual_cost"] = round(max(0.0, float(actual_cost)), 4)
    if retry_reason is not None:
        record["retry_reason"] = retry_reason
    if review is not None:
        record["review"] = review
    agent = manifest["agent"]
    agent["heartbeat_at"] = now()
    if status == "passed":
        agent["last_checkpoint"] = stage
    if status == "blocked":
        agent["blocked_reason"] = message
    agent["events"] = [
        *agent.get("events", []),
        {"time": now(), "event": "stage", "stage": stage, "status": status, "message": message},
    ][-500:]
    return record


def manifest_context_sha256(manifest: dict[str, Any]) -> str:
    """Hash stable project context while excluding mutable runtime bookkeeping."""
    payload = {
        "version": manifest.get("version"),
        "story": manifest.get("story", {}),
        "inputs": manifest.get("inputs", {}),
        "outputs": manifest.get("outputs", {}),
        "qa": manifest.get("qa", {}),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def existing_artifact_hashes(artifacts: list[Path | str]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for raw in artifacts:
        path = Path(raw)
        if path.is_file():
            hashes[str(path)] = file_sha256(path)
    return hashes


def review_passes(payload: dict[str, Any], *, artifact: Path | None = None, threshold: int = PASS_SCORE) -> bool:
    if not payload.get("approved", False):
        return False
    if float(payload.get("score", 0)) < threshold:
        return False
    if payload.get("critical_errors"):
        return False
    if artifact is not None:
        expected = str(payload.get("artifact_sha256") or "")
        if not expected or expected != file_sha256(artifact):
            return False
    return True


def write_review_bundle(path: Path, artifacts: list[Path]) -> Path:
    files: list[Path] = []
    for artifact in artifacts:
        if artifact.is_dir():
            files.extend(sorted(item for item in artifact.rglob("*") if item.is_file()))
        elif artifact.is_file():
            files.append(artifact)
    payload = {
        "version": 1,
        "artifacts": [
            {"path": str(item), "sha256": file_sha256(item), "bytes": item.stat().st_size}
            for item in files
        ],
    }
    save_json(path, payload)
    return path


def review_bundle_is_current(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return False
    for item in artifacts:
        target = Path(str(item.get("path", "")))
        if not target.exists() or not target.is_file():
            return False
        if str(item.get("sha256", "")) != file_sha256(target):
            return False
    return True


@dataclass
class BudgetLedger:
    manifest: dict[str, Any]

    @property
    def data(self) -> dict[str, Any]:
        ensure_manifest_v2(self.manifest)
        return self.manifest["agent"]["budget"]

    def authorize(self, amount: float, *, label: str, critical: bool = False) -> str:
        amount = max(0.0, float(amount))
        projected = float(self.data["spent"]) + float(self.data["reserved"]) + amount
        hard = float(self.data["hard_limit"])
        soft = float(self.data["soft_limit"])
        if projected > hard:
            raise BudgetExceeded(f"{label} 预计使成本达到 ¥{projected:.2f}，超过硬上限 ¥{hard:.2f}")
        if projected > soft and not critical:
            raise BudgetExceeded(f"{label} 预计使成本达到 ¥{projected:.2f}，超过软上限 ¥{soft:.2f}；非关键重试已停止")
        reservation_id = uuid.uuid4().hex
        self.data["reserved"] = round(float(self.data["reserved"]) + amount, 4)
        self.data["entries"].append(
            {"id": reservation_id, "time": now(), "label": label, "kind": "reservation", "amount": amount, "status": "open"}
        )
        return reservation_id

    def settle(self, reservation_id: str, actual_amount: float, *, provider: str = "", request_id: str = "") -> None:
        entry = next((item for item in self.data["entries"] if item.get("id") == reservation_id), None)
        if entry is None or entry.get("status") != "open":
            raise AgentRuntimeError(f"找不到有效预算预留：{reservation_id}")
        reserved = float(entry["amount"])
        actual = max(0.0, float(actual_amount))
        self.data["reserved"] = round(max(0.0, float(self.data["reserved"]) - reserved), 4)
        self.data["spent"] = round(float(self.data["spent"]) + actual, 4)
        entry.update({"status": "settled", "actual_amount": actual, "provider": provider, "request_id": request_id})

    def release(self, reservation_id: str, *, reason: str = "") -> None:
        entry = next((item for item in self.data["entries"] if item.get("id") == reservation_id), None)
        if entry is None or entry.get("status") != "open":
            return
        self.data["reserved"] = round(max(0.0, float(self.data["reserved"]) - float(entry["amount"])), 4)
        entry.update({"status": "released", "reason": reason})


class JobRegistry:
    def __init__(self, path: Path | None = None) -> None:
        default = Path(os.getenv("STORY_AGENT_REGISTRY", str(Path.cwd() / "output" / "story_agent_jobs.json")))
        self.path = (path or default).expanduser()

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "jobs": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentRuntimeError(f"任务索引损坏：{self.path}: {exc}") from exc
        payload.setdefault("version", 1)
        payload.setdefault("jobs", {})
        return payload

    def save(self, payload: dict[str, Any]) -> None:
        save_json(self.path, payload)

    def register(self, job_id: str, project_dir: Path, source_fingerprint: str) -> None:
        payload = self.load()
        payload["jobs"][job_id] = {
            "project_dir": str(project_dir),
            "source_fingerprint": source_fingerprint,
            "created_at": now(),
        }
        self.save(payload)

    def resolve(self, job_id: str) -> Path:
        record = self.load()["jobs"].get(job_id)
        if record is None:
            raise AgentRuntimeError(f"未知任务 ID：{job_id}")
        return Path(record["project_dir"]).expanduser()

    def find_by_source(self, source_fingerprint: str) -> tuple[str, Path] | None:
        for job_id, record in self.load()["jobs"].items():
            if record.get("source_fingerprint") == source_fingerprint:
                return job_id, Path(record["project_dir"]).expanduser()
        return None


def submit_video_job(
    video: Path,
    *,
    projects_root: Path,
    story_name: str = "",
    slug: str = "",
    registry: JobRegistry | None = None,
    force: bool = False,
    soft_budget_cny: float = DEFAULT_SOFT_BUDGET_CNY,
    hard_budget_cny: float = DEFAULT_HARD_BUDGET_CNY,
    deadline_hours: float = DEFAULT_DEADLINE_HOURS,
) -> tuple[str, Path, bool]:
    source = video.expanduser().resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"绿幕视频不存在：{source}")
    if source.suffix.lower() not in {".mp4", ".mov", ".mkv", ".m4v"}:
        raise ValueError(f"不支持的视频格式：{source.suffix}")
    fingerprint = media_fingerprint(source)
    media_info = probe_source_video(source)
    registry = registry or JobRegistry()
    existing = registry.find_by_source(fingerprint)
    if existing is not None and not force:
        return existing[0], existing[1], False

    inferred_name = story_name.strip() or source.stem.replace("绿幕", "").replace("greenscreen", "").strip(" _-") or "新故事"
    safe_slug = slug.strip() or slugify(inferred_name)
    job_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{safe_slug}-{file_sha256(source)[:8]}"
    project_dir = projects_root.expanduser() / f"故事剪辑：{inferred_name}"
    if force and project_dir.exists():
        project_dir = projects_root.expanduser() / f"故事剪辑：{inferred_name}-{job_id[-8:]}"
    manifest = init_project(project_dir, story_name=inferred_name, slug=safe_slug)
    paths = project_paths(project_dir)
    target = paths.inputs / f"{safe_slug}_greenscreen_source{source.suffix.lower()}"
    if not target.exists():
        shutil.copy2(source, target)
    ensure_manifest_v2(
        manifest,
        job_id=job_id,
        soft_budget_cny=soft_budget_cny,
        hard_budget_cny=hard_budget_cny,
        deadline_hours=deadline_hours,
    )
    manifest["inputs"]["greenscreen_video"] = str(target)
    manifest["agent"]["source"] = {
        "original_path": str(source),
        "project_copy": str(target),
        "fingerprint": fingerprint,
        "sha256": file_sha256(source),
        "bytes": source.stat().st_size,
        "media": media_info,
        "submitted_at": now(),
    }
    manifest["agent"]["status"] = "pending"
    write_manifest(paths, manifest)
    registry.register(job_id, project_dir, fingerprint)
    return job_id, project_dir, True


@contextmanager
def job_lock(project_dir: Path, *, stale_seconds: int = 12 * 3600) -> Iterator[Path]:
    lock = project_paths(project_dir).status / "story_agent.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        stale = time.time() - lock.stat().st_mtime > stale_seconds
        alive = False
        try:
            payload = json.loads(lock.read_text(encoding="utf-8"))
            pid = int(payload.get("pid", 0))
            if pid > 0:
                os.kill(pid, 0)
                alive = True
        except (OSError, ValueError, json.JSONDecodeError):
            alive = False
        if stale or not alive:
            lock.unlink(missing_ok=True)
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise AgentRuntimeError(f"任务已经在运行：{lock}") from exc
    try:
        os.write(descriptor, json.dumps({"pid": os.getpid(), "started_at": now()}).encode("utf-8"))
        os.close(descriptor)
        yield lock
    finally:
        lock.unlink(missing_ok=True)


def request_cancel(project_dir: Path) -> dict[str, Any]:
    paths = project_paths(project_dir)
    manifest = ensure_manifest_v2(load_manifest(paths) or init_project(project_dir))
    manifest["agent"]["cancel_requested"] = True
    manifest["agent"]["status"] = "cancelled"
    manifest["agent"]["events"].append({"time": now(), "event": "cancel_requested"})
    write_manifest(paths, manifest)
    return manifest


def resume_job(project_dir: Path) -> dict[str, Any]:
    paths = project_paths(project_dir)
    manifest = ensure_manifest_v2(load_manifest(paths) or init_project(project_dir))
    manifest["agent"]["cancel_requested"] = False
    manifest["agent"]["status"] = "pending"
    manifest["agent"]["blocked_reason"] = ""
    manifest["agent"]["started_at"] = ""
    manifest["agent"]["events"].append({"time": now(), "event": "resume_requested"})
    write_manifest(paths, manifest)
    return manifest


def assert_runnable(manifest: dict[str, Any], project_dir: Path | None = None) -> None:
    ensure_manifest_v2(manifest)
    if manifest["agent"].get("cancel_requested"):
        raise JobCancelled("任务已取消；使用 resume 后才能继续。")
    started_at = manifest["agent"].get("started_at")
    if started_at:
        try:
            started = time.mktime(time.strptime(started_at, "%Y-%m-%d %H:%M:%S"))
            elapsed_hours = (time.time() - started) / 3600
            if elapsed_hours > float(manifest["agent"].get("deadline_hours", DEFAULT_DEADLINE_HOURS)):
                raise AgentRuntimeError(f"任务已超过运行时限：{elapsed_hours:.1f} 小时")
        except ValueError:
            pass
    if project_dir is not None:
        usage = shutil.disk_usage(project_dir)
        minimum = float(manifest["agent"].get("min_free_disk_gb", DEFAULT_MIN_FREE_DISK_GB)) * 1024**3
        if usage.free < minimum:
            raise AgentRuntimeError(
                f"磁盘可用空间不足：{usage.free / 1024**3:.1f} GB，最低要求 {minimum / 1024**3:.1f} GB"
            )


def recovery_guidance(manifest: dict[str, Any]) -> str:
    ensure_manifest_v2(manifest)
    agent = manifest["agent"]
    reason = str(agent.get("blocked_reason", ""))
    combined = reason.lower()
    job_id = str(agent.get("job_id") or "<job_id>")
    if agent.get("cancel_requested") or agent.get("status") == "cancelled":
        return f"依次运行 resume 和 start 恢复任务：job={job_id}。"
    if "suno" in combined or "browser" in combined or "登录" in reason or "captcha" in combined:
        return "在 Codex 主任务恢复 Suno 登录/浏览器能力，完成 handoff 下载后再 resume/start。"
    if "磁盘" in reason:
        return "释放磁盘空间至最低阈值以上，再 resume/start。"
    if "api key" in combined or "credential" in combined or "鉴权" in reason:
        return "在环境变量中配置供应商密钥，确认不写入 manifest 后再 resume/start。"
    if "预算" in reason or "上限" in reason:
        return "查看成本报告；只有用户明确调整预算或切换零费用供应商后才能恢复。"
    if agent.get("status") in {"blocked", "failed"}:
        return "查看最新阶段日志，处理记录的原因后运行 resume/start。"
    return "无需人工恢复；后台 supervisor 可继续推进。"


def render_job_report(project_dir: Path) -> Path:
    paths = project_paths(project_dir)
    write_internal_agent_reports(project_dir)
    manifest = ensure_manifest_v2(load_manifest(paths) or {})
    agent = manifest["agent"]
    budget = agent["budget"]
    started_text = str(agent.get("started_at", ""))
    elapsed_hours = 0.0
    if started_text:
        try:
            elapsed_hours = max(0.0, (time.time() - time.mktime(time.strptime(started_text, "%Y-%m-%d %H:%M:%S"))) / 3600)
        except ValueError:
            pass
    deadline_hours = float(agent.get("deadline_hours", DEFAULT_DEADLINE_HOURS))
    stages = agent.get("stages", {}) if isinstance(agent.get("stages"), dict) else {}
    remaining = [name for name in STORY_STAGE_SEQUENCE if stages.get(name, {}).get("status") != "passed"]
    nominal_minutes = sum(STAGE_ESTIMATES_MINUTES.get(name, 10) for name in remaining)
    open_reservations = [entry for entry in budget.get("entries", []) if entry.get("status") == "open"]
    lines = [
        "# 故事生产 Agent 交付摘要",
        "",
        f"- 任务 ID：{agent.get('job_id') or '未登记'}",
        f"- 项目：{project_dir}",
        f"- 状态：{agent.get('status', 'pending')}",
        f"- 最后检查点：{agent.get('last_checkpoint') or '无'}",
        f"- 心跳：{agent.get('heartbeat_at') or '无'}",
        f"- 已运行/剩余时限：{elapsed_hours:.2f} / {max(0.0, deadline_hours - elapsed_hours):.2f} 小时",
        f"- 成本：¥{float(budget.get('spent', 0)):.2f} / 软上限 ¥{float(budget.get('soft_limit', 0)):.2f} / 硬上限 ¥{float(budget.get('hard_limit', 0)):.2f}",
        f"- 预算预留：¥{float(budget.get('reserved', 0)):.2f}（开放 {len(open_reservations)} 笔）",
        f"- 阻塞原因：{agent.get('blocked_reason') or '无'}",
        f"- 恢复动作：{recovery_guidance(manifest)}",
        f"- 剩余阶段：{len(remaining)}；经验估算约 {nominal_minutes} 分钟（不含外部排队/登录等待）",
        "",
        "## 阶段",
        "",
        "| 阶段 | 状态 | 尝试 | 供应商 | 阶段成本 | 分数 | 重试/说明 |",
        "| --- | --- | ---: | --- | ---: | ---: | --- |",
    ]
    for name in STORY_STAGE_SEQUENCE:
        record = stages.get(name, {})
        score = record.get("review", {}).get("score", "")
        detail = record.get("retry_reason") or record.get("message", "")
        lines.append(
            f"| {name} | {record.get('status', 'pending')} | {record.get('attempts', 0)} | {record.get('provider', '')} | "
            f"¥{float(record.get('actual_cost', 0.0)):.2f} | {score} | {str(detail).replace('|', '/')} |"
        )
    lines.extend(["", "## 剩余工作", ""])
    if remaining:
        for name in remaining:
            lines.append(f"- `{name}`：经验值约 {STAGE_ESTIMATES_MINUTES.get(name, 10)} 分钟")
    else:
        lines.append("- 无")
    lines.extend(["", "## 已登记输出", ""])
    for key, value in manifest.get("outputs", {}).items():
        if value:
            path = Path(value)
            lines.append(f"- {key}：{'✅' if path.exists() else '⚠️'} `{path}`")
    report = paths.status / "agent_morning_report.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
