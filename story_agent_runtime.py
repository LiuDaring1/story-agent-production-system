from __future__ import annotations

import hashlib
import json
import math
import os
import re
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
    "product_preflight",
    "product_annotation",
    "product_annotation_review",
    "product_package",
    "product_package_review",
    "publish_package_review",
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

# Keep STORY_STAGE_SEQUENCE as the stable topological/reporting order for old
# manifests and CLI consumers.  The DAG scheduler uses these explicit edges.
STORY_STAGE_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "import_inbox": (),
    "source_edit": ("import_inbox",),
    "source_text_correction": ("source_edit",),
    "source_edit_review": ("source_text_correction",),
    "setup_project": ("source_edit_review",),
    "codex_story_images": ("setup_project",),
    "story_images_review": ("codex_story_images",),
    "prepare_jobs": ("story_images_review",),
    "timing": ("prepare_jobs",),
    "video_prompt_review": ("timing",),
    "generate_videos": ("video_prompt_review",),
    "video_qa": ("generate_videos",),
    "video_review": ("video_qa",),
    "apply_review": ("video_review",),
    "music_request": ("setup_project",),
    "suno_generate": ("music_request",),
    "assemble_music": ("suno_generate",),
    "music_qa": ("assemble_music",),
    "assemble_final": ("apply_review", "music_qa"),
    "release_assets": ("setup_project",),
    "release_preview": ("assemble_final", "release_assets"),
    "package_release": ("release_preview",),
    "release_qa": ("package_release",),
    "release_video_review": ("release_qa",),
    "publish_package": ("release_video_review",),
    "product_preflight": ("release_video_review",),
    "product_annotation": ("product_preflight",),
    "product_annotation_review": ("product_annotation",),
    "product_package": ("product_annotation_review",),
    "product_package_review": ("product_package",),
    "publish_package_review": ("publish_package",),
    "final_delivery": ("product_package_review", "publish_package_review"),
    "doctor": ("final_delivery",),
}

STAGE_BRANCHES = {
    **{name: "source" for name in ("import_inbox", "source_edit", "source_text_correction", "source_edit_review", "setup_project")},
    **{name: "visual" for name in ("codex_story_images", "story_images_review", "prepare_jobs", "timing", "video_prompt_review", "generate_videos", "video_qa", "video_review", "apply_review")},
    **{name: "music" for name in ("music_request", "suno_generate", "assemble_music", "music_qa")},
    "assemble_final": "assembly",
    "release_assets": "release_assets",
    **{name: "release" for name in ("release_preview", "package_release", "release_qa", "release_video_review")},
    **{name: "publish" for name in ("publish_package", "publish_package_review")},
    **{name: "product" for name in ("product_preflight", "product_annotation", "product_annotation_review", "product_package", "product_package_review")},
    "final_delivery": "final",
    "doctor": "final",
}

# Abstract write sets are checked before launching a parallel batch.  A stage
# worker writes a shadow manifest, while artifacts remain in these disjoint
# canonical areas.  Descendant/identical write sets never run together.
STAGE_WRITE_SETS = {
    "import_inbox": ("inputs",),
    "source_edit": ("inputs", "status/source_edit"),
    "source_text_correction": ("inputs", "status/source_edit"),
    "source_edit_review": ("status/source_edit",),
    "setup_project": ("inputs", "release/person_reference"),
    "codex_story_images": ("images", "status/story_image_progress"),
    "story_images_review": ("status/reviews/story_images",),
    "prepare_jobs": ("video_jobs/jobs",),
    "timing": ("video_jobs/jobs", "assembly/timings"),
    "video_prompt_review": ("video_jobs/prompt_review", "status/reviews/video_prompt"),
    "generate_videos": ("video_jobs/videos",),
    "video_qa": ("status/video_qa",),
    "video_review": ("status/reviews/video",),
    "apply_review": ("assembly/clips",),
    "music_request": ("video_jobs/music",),
    "suno_generate": ("video_jobs/music",),
    "assemble_music": ("video_jobs/music",),
    "music_qa": ("status/music_qa",),
    "assemble_final": ("assembly/final",),
    "release_assets": ("release/theme_assets", "release/keying"),
    "release_preview": ("status/release_preview", "status/reviews/release_preview"),
    "package_release": ("release/renders",),
    "release_qa": ("status/release_qa",),
    "release_video_review": ("status/reviews/release_video",),
    "publish_package": ("publish",),
    "publish_package_review": ("status/reviews/publish",),
    "product_preflight": ("status/product_work",),
    "product_annotation": ("status/product_work",),
    "product_annotation_review": ("status/reviews/product_annotation",),
    "product_package": ("product",),
    "product_package_review": ("status/reviews/product",),
    "final_delivery": ("status/final_delivery",),
    "doctor": ("status/doctor",),
}

STAGE_RESOURCES = {
    "codex_story_images": ("codex_exec", "imagegen"),
    "story_images_review": ("codex_exec",),
    "video_prompt_review": ("codex_exec",),
    "generate_videos": ("video_api", "paid_work"),
    "video_review": ("codex_exec",),
    "suno_generate": ("codex_exec", "browser_suno"),
    "assemble_final": ("ffmpeg_heavy",),
    "release_assets": ("codex_exec", "imagegen"),
    "release_preview": ("codex_exec",),
    "package_release": ("ffmpeg_heavy",),
    "release_video_review": ("codex_exec",),
    "publish_package": ("codex_exec", "imagegen"),
    "publish_package_review": ("codex_exec",),
    "product_annotation": ("codex_exec",),
    "product_annotation_review": ("codex_exec",),
    "product_package": ("ffmpeg_heavy",),
    "product_package_review": ("codex_exec",),
}

DEFAULT_RESOURCE_CAPACITIES = {
    "codex_exec": 2,
    "imagegen": 1,
    "browser_suno": 1,
    "video_api": 1,
    "paid_work": 1,
    "ffmpeg_heavy": 1,
}


class AgentRuntimeError(RuntimeError):
    pass


class BudgetExceeded(AgentRuntimeError):
    pass


class JobCancelled(AgentRuntimeError):
    pass


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def runtime_elapsed_seconds(agent: dict[str, Any], *, include_current: bool = True) -> float:
    elapsed = max(0.0, float(agent.get("active_elapsed_seconds", 0.0) or 0.0))
    started_at = str(agent.get("started_at", ""))
    if include_current and started_at:
        try:
            elapsed += max(0.0, time.time() - time.mktime(time.strptime(started_at, "%Y-%m-%d %H:%M:%S")))
        except ValueError:
            pass
    return elapsed


def start_runtime(agent: dict[str, Any]) -> None:
    agent.setdefault("active_elapsed_seconds", 0.0)
    if not agent.get("started_at"):
        agent["started_at"] = now()


def freeze_runtime(agent: dict[str, Any]) -> None:
    agent["active_elapsed_seconds"] = round(runtime_elapsed_seconds(agent), 3)
    agent["started_at"] = ""


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
    agent.setdefault("active_elapsed_seconds", 0.0)
    agent.setdefault("finished_at", "")
    budget = agent.setdefault("budget", {})
    budget.setdefault("currency", "CNY")
    budget.setdefault("soft_limit", float(soft_budget_cny))
    budget.setdefault("hard_limit", float(hard_budget_cny))
    budget.setdefault("spent", 0.0)
    budget.setdefault("reserved", 0.0)
    budget.setdefault("entries", [])
    agent.setdefault("source", {})
    agent.setdefault("input_contract", {})
    agent.setdefault("external_blockers", [])
    agent.setdefault("branch_blockers", {})
    scheduler = agent.setdefault("scheduler", {})
    scheduler.setdefault("schema_version", 1)
    scheduler.setdefault("mode", "linear")
    scheduler.setdefault("max_parallel", 1)
    scheduler.setdefault("run_epoch", 0)
    scheduler.setdefault("run_id", "")
    scheduler.setdefault("running", {})
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
    branch = STAGE_BRANCHES.get(stage, "other")
    branch_blockers = agent.setdefault("branch_blockers", {})
    if status == "passed":
        branch_blockers.pop(stage, None)
        current_checkpoint = str(agent.get("last_checkpoint") or "")
        current_index = STORY_STAGE_SEQUENCE.index(current_checkpoint) if current_checkpoint in STORY_STAGE_SEQUENCE else -1
        if STORY_STAGE_SEQUENCE.index(stage) > current_index:
            agent["last_checkpoint"] = stage
        unresolved = any(
            item.get("status") in {"blocked", "failed", "cancelled"}
            for item in agent.get("stages", {}).values()
            if isinstance(item, dict)
        )
        if not unresolved:
            agent["blocked_reason"] = ""
    if status == "blocked":
        agent["blocked_reason"] = message
        branch_blockers[stage] = {"branch": branch, "message": message, "updated_at": now()}
    elif status in {"failed", "cancelled"}:
        branch_blockers[stage] = {"branch": branch, "message": message, "updated_at": now()}
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
    try:
        score = float(payload.get("score", 0))
    except (TypeError, ValueError):
        return False
    if not math.isfinite(score) or score < threshold:
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


def segment_confirmed_story_text(text: str, *, target_chars: int = 32) -> list[str]:
    """Change line breaks only; never rewrite a user-confirmed manuscript."""
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    paragraphs = [line.strip() for line in normalized.splitlines() if line.strip()]
    if len(paragraphs) >= 3 and max((len(line) for line in paragraphs), default=0) <= target_chars * 2:
        return paragraphs
    units: list[str] = []
    for paragraph in paragraphs or [normalized]:
        matches = re.findall(r".*?[。！？!?](?:[”’\"』】])?|.+$", paragraph)
        units.extend(item.strip() for item in matches if item.strip())
    lines: list[str] = []
    current = ""
    for unit in units:
        if current and len(current) + len(unit) > target_chars:
            lines.append(current)
            current = unit
        else:
            current += unit
    if current:
        lines.append(current)
    if not lines:
        raise ValueError("确认文本无法形成故事分镜行")
    source_compact = re.sub(r"\s+", "", normalized)
    output_compact = re.sub(r"\s+", "", "".join(lines))
    if source_compact != output_compact:
        raise AgentRuntimeError("确认文本分行改变了正文内容，拒绝创建加速任务")
    return lines


def prepared_input_contract_errors(project_dir: Path, manifest: dict[str, Any]) -> list[str]:
    agent = manifest.get("agent", {}) if isinstance(manifest.get("agent"), dict) else {}
    contract = agent.get("input_contract") if isinstance(agent.get("input_contract"), dict) else {}
    if contract.get("mode") != "prepared_greenscreen_confirmed_text":
        return ["不是 prepared 加速入口契约"]
    records = contract.get("user_inputs") if isinstance(contract.get("user_inputs"), list) else []
    by_role = {
        str(item.get("role")): item
        for item in records
        if isinstance(item, dict) and item.get("role")
    }
    errors: list[str] = []
    expected_roles = {"prepared_greenscreen_video", "confirmed_story_text"}
    if set(by_role) != expected_roles:
        errors.append("用户输入必须恰好包含干净绿幕视频和人工确认文本")
        return errors
    root = project_dir.expanduser().resolve()
    inputs_dir = project_paths(root).inputs.resolve()
    for role in sorted(expected_roles):
        item = by_role[role]
        path = Path(str(item.get("path") or "")).expanduser()
        try:
            resolved = path.resolve()
            resolved.relative_to(inputs_dir)
        except (OSError, ValueError):
            errors.append(f"{role} 没有指向项目输入目录内的不可变副本")
            continue
        if not resolved.is_file():
            errors.append(f"{role} 项目副本缺失")
            continue
        try:
            recorded_bytes = int(item.get("bytes", -1))
        except (TypeError, ValueError):
            recorded_bytes = -1
        if recorded_bytes != resolved.stat().st_size:
            errors.append(f"{role} 文件大小已变化")
        if str(item.get("sha256") or "") != file_sha256(resolved):
            errors.append(f"{role} SHA-256 已变化")
    inputs = manifest.get("inputs", {}) if isinstance(manifest.get("inputs"), dict) else {}
    video_record = by_role["prepared_greenscreen_video"]
    text_record = by_role["confirmed_story_text"]
    video = Path(str(video_record.get("path") or ""))
    original = Path(str(inputs.get("greenscreen_video_original") or ""))
    clean = Path(str(inputs.get("greenscreen_video") or ""))
    if not str(original) or not str(clean) or original.expanduser().resolve() != video.expanduser().resolve() or clean.expanduser().resolve() != video.expanduser().resolve():
        errors.append("manifest 的原始/干净绿幕路径没有绑定 prepared 视频副本")
    if video.is_file():
        try:
            probe_source_video(video)
        except (OSError, ValueError) as exc:
            errors.append(f"prepared 视频验证失败：{exc}")
    confirmed = Path(str(text_record.get("path") or ""))
    if confirmed.is_file():
        try:
            confirmed_text = confirmed.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            errors.append("人工确认文本不再是 UTF-8")
            confirmed_text = ""
        if not confirmed_text.strip():
            errors.append("人工确认文本为空")
    story_text = Path(str(inputs.get("story_text") or ""))
    derived = contract.get("derived_inputs") if isinstance(contract.get("derived_inputs"), dict) else {}
    derived_story = derived.get("story_text") if isinstance(derived.get("story_text"), dict) else {}
    if not story_text.is_file():
        errors.append("prepared 分行故事文本缺失")
    else:
        try:
            recorded_story_bytes = int(derived_story.get("bytes", -1))
        except (TypeError, ValueError):
            recorded_story_bytes = -1
        if (
            Path(str(derived_story.get("path") or "")).expanduser().resolve() != story_text.expanduser().resolve()
            or derived_story.get("sha256") != file_sha256(story_text)
            or recorded_story_bytes != story_text.stat().st_size
        ):
            errors.append("prepared 分行故事文本派生哈希失效")
    return errors


def record_contract_derivative(
    manifest: dict[str, Any],
    *,
    role: str,
    path: Path,
    source_sha256: str,
    producer: str,
) -> None:
    contract = manifest.get("agent", {}).get("input_contract")
    if not isinstance(contract, dict) or not path.is_file():
        return
    derived = contract.setdefault("derived_inputs", {})
    derived[role] = {
        "path": str(path),
        "sha256": file_sha256(path),
        "bytes": path.stat().st_size,
        "producer": producer,
        "source_sha256": source_sha256,
    }


def submit_video_job(
    video: Path,
    *,
    lut: Path | None = None,
    input_mode: str = "single-greenscreen",
    confirmed_text: Path | None = None,
    projects_root: Path,
    story_name: str = "",
    slug: str = "",
    registry: JobRegistry | None = None,
    force: bool = False,
    soft_budget_cny: float = DEFAULT_SOFT_BUDGET_CNY,
    hard_budget_cny: float = DEFAULT_HARD_BUDGET_CNY,
    deadline_hours: float = DEFAULT_DEADLINE_HOURS,
) -> tuple[str, Path, bool]:
    normalized_mode = input_mode.strip().lower().replace("_", "-")
    if normalized_mode not in {"single-greenscreen", "prepared"}:
        raise ValueError(f"未知输入模式：{input_mode}")
    if normalized_mode == "prepared" and confirmed_text is None:
        raise ValueError("prepared 加速入口必须提供 --confirmed-text")
    if normalized_mode == "single-greenscreen" and confirmed_text is not None:
        raise ValueError("single-greenscreen 模式不能提供 --confirmed-text；请显式选择 --input-mode prepared")
    if normalized_mode == "prepared" and lut is not None:
        raise ValueError("prepared 视频应已完成颜色还原，禁止再次传入 LUT 以免重复套色")
    source = video.expanduser().resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"绿幕视频不存在：{source}")
    if source.suffix.lower() not in {".mp4", ".mov", ".mkv", ".m4v"}:
        raise ValueError(f"不支持的视频格式：{source.suffix}")
    fingerprint = media_fingerprint(source)
    confirmed_source: Path | None = None
    confirmed_body = ""
    if confirmed_text is not None:
        confirmed_source = confirmed_text.expanduser().resolve()
        if not confirmed_source.is_file() or confirmed_source.suffix.lower() not in {".txt", ".md"}:
            raise ValueError("prepared 确认文本首版只接受 UTF-8 .txt/.md 文件")
        try:
            confirmed_body = confirmed_source.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("prepared 确认文本必须是 UTF-8 编码") from exc
        if not confirmed_body.strip():
            raise ValueError("prepared 确认文本不能为空")
        fingerprint = hashlib.sha256(
            f"prepared:{fingerprint}:{file_sha256(confirmed_source)}".encode("utf-8")
        ).hexdigest()
    color_lut: Path | None = None
    lut_sha256 = ""
    if lut is not None:
        color_lut = lut.expanduser().resolve()
        if not color_lut.is_file():
            raise FileNotFoundError(f"LUT 不存在：{color_lut}")
        if color_lut.suffix.lower() != ".cube":
            raise ValueError(f"当前只支持 .cube LUT：{color_lut}")
        lut_sha256 = file_sha256(color_lut)
        fingerprint = hashlib.sha256(f"{fingerprint}:{lut_sha256}".encode("utf-8")).hexdigest()
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
    video_stem = "prepared_greenscreen_source" if normalized_mode == "prepared" else "greenscreen_source"
    target = paths.inputs / f"{safe_slug}_{video_stem}{source.suffix.lower()}"
    copy_verified_input(source, target)
    confirmed_target: Path | None = None
    story_text_target: Path | None = None
    consumer_target: Path | None = None
    if confirmed_source is not None:
        confirmed_target = paths.inputs / f"{safe_slug}_confirmed_story{confirmed_source.suffix.lower()}"
        copy_verified_input(confirmed_source, confirmed_target)
        story_text_target = paths.inputs / f"{safe_slug}_prepared_story_source.txt"
        story_text_target.write_text("\n".join(segment_confirmed_story_text(confirmed_body)) + "\n", encoding="utf-8")
        consumer_target = paths.inputs / f"{safe_slug}_consumer_manuscript.txt"
        consumer_target.write_text(confirmed_body.lstrip("\ufeff").replace("\r\n", "\n").strip() + "\n", encoding="utf-8")
    lut_target: Path | None = None
    if color_lut is not None:
        lut_target = paths.inputs / f"{safe_slug}_input_lut.cube"
        copy_verified_input(color_lut, lut_target)
    ensure_manifest_v2(
        manifest,
        job_id=job_id,
        soft_budget_cny=soft_budget_cny,
        hard_budget_cny=hard_budget_cny,
        deadline_hours=deadline_hours,
    )
    manifest["inputs"]["greenscreen_video"] = str(target)
    if normalized_mode == "prepared":
        assert confirmed_target is not None and story_text_target is not None and consumer_target is not None
        manifest["inputs"]["greenscreen_video_original"] = str(target)
        manifest["inputs"]["story_text"] = str(story_text_target)
        manifest["outputs"]["consumer_manuscript"] = str(consumer_target)
    if lut_target is not None:
        manifest["inputs"]["color_lut"] = str(lut_target)
    manifest["agent"]["source"] = {
        "original_path": str(source),
        "project_copy": str(target),
        "fingerprint": fingerprint,
        "sha256": file_sha256(source),
        "bytes": source.stat().st_size,
        "media": media_info,
        "submitted_at": now(),
    }
    if normalized_mode == "single-greenscreen":
        manifest["agent"]["input_contract"] = {
            "version": 1,
            "mode": "single_greenscreen",
            "created_at": now(),
            "user_inputs": [
                {
                    "role": "greenscreen_video",
                    "path": str(target),
                    "sha256": file_sha256(target),
                    "bytes": target.stat().st_size,
                }
            ],
            "processing_assets": [],
            "derived_inputs": {},
        }
    else:
        assert confirmed_target is not None and story_text_target is not None
        manifest["agent"]["source"]["confirmed_text"] = {
            "original_path": str(confirmed_source),
            "project_copy": str(confirmed_target),
            "sha256": file_sha256(confirmed_target),
            "bytes": confirmed_target.stat().st_size,
        }
        manifest["agent"]["input_contract"] = {
            "version": 1,
            "mode": "prepared_greenscreen_confirmed_text",
            "track": "assisted_accelerated",
            "counts_toward_default_entry": False,
            "created_at": now(),
            "user_declarations": ["already_color_restored", "complete_clean_take", "text_manually_confirmed"],
            "user_inputs": [
                {
                    "role": "prepared_greenscreen_video",
                    "path": str(target),
                    "sha256": file_sha256(target),
                    "bytes": target.stat().st_size,
                },
                {
                    "role": "confirmed_story_text",
                    "path": str(confirmed_target),
                    "sha256": file_sha256(confirmed_target),
                    "bytes": confirmed_target.stat().st_size,
                },
            ],
            "processing_assets": [],
            "derived_inputs": {
                "story_text": {
                    "path": str(story_text_target),
                    "sha256": file_sha256(story_text_target),
                    "bytes": story_text_target.stat().st_size,
                    "producer": "prepared_input_segmentation",
                    "source_sha256": file_sha256(confirmed_target),
                },
                "consumer_manuscript": {
                    "path": str(consumer_target),
                    "sha256": file_sha256(consumer_target),
                    "bytes": consumer_target.stat().st_size,
                    "producer": "prepared_input_copy",
                    "source_sha256": file_sha256(confirmed_target),
                },
            },
        }
    if color_lut is not None and lut_target is not None:
        manifest["agent"]["source"]["color_lut"] = {
            "original_path": str(color_lut),
            "project_copy": str(lut_target),
            "sha256": lut_sha256,
            "bytes": color_lut.stat().st_size,
        }
        manifest["agent"]["input_contract"]["processing_assets"].append(
            {
                "role": "color_lut",
                "path": str(lut_target),
                "sha256": lut_sha256,
                "bytes": lut_target.stat().st_size,
            }
        )
    manifest["agent"]["status"] = "pending"
    write_manifest(paths, manifest)
    registry.register(job_id, project_dir, fingerprint)
    return job_id, project_dir, True


def record_derived_input(
    manifest: dict[str, Any],
    *,
    role: str,
    path: Path,
    source_sha256: str,
    decisions_sha256: str,
    producer: str = "source_video_pipeline",
) -> None:
    """Record a source-edit derivative without creating a submit contract retroactively."""
    contract = manifest.get("agent", {}).get("input_contract")
    if not isinstance(contract, dict) or contract.get("mode") != "single_greenscreen" or not path.is_file():
        return
    derived = contract.setdefault("derived_inputs", {})
    derived[role] = {
        "path": str(path),
        "sha256": file_sha256(path),
        "bytes": path.stat().st_size,
        "producer": producer,
        "source_sha256": source_sha256,
        "decisions_sha256": decisions_sha256,
    }


def copy_verified_input(source: Path, target: Path) -> None:
    """Copy an immutable task input without accepting an interrupted partial file."""
    source_size = source.stat().st_size
    source_sha = file_sha256(source)
    if target.is_file() and target.stat().st_size == source_size and file_sha256(target) == source_sha:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.partial-{uuid.uuid4().hex}")
    try:
        shutil.copy2(source, temporary)
        if temporary.stat().st_size != source_size or file_sha256(temporary) != source_sha:
            raise AgentRuntimeError(f"输入素材复制校验失败：{source} -> {target}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def process_is_alive(pid: int) -> bool:
    """Treat EPERM as alive: sandboxed status readers may not signal the supervisor."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def control_path(project_dir: Path) -> Path:
    return project_paths(project_dir).status / "story_agent_control.json"


@contextmanager
def control_update_lock(project_dir: Path, *, timeout_seconds: float = 5.0) -> Iterator[Path]:
    lock = project_paths(project_dir).status / "story_agent_control.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    while True:
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError as exc:
            try:
                payload = json.loads(lock.read_text(encoding="utf-8"))
                owner_alive = process_is_alive(int(payload.get("pid", 0)))
            except (OSError, ValueError, json.JSONDecodeError):
                try:
                    owner_alive = time.time() - lock.stat().st_mtime < max(10.0, timeout_seconds)
                except OSError:
                    owner_alive = False
            if not owner_alive:
                lock.unlink(missing_ok=True)
                continue
            if time.monotonic() >= deadline:
                raise AgentRuntimeError("控制面正在更新，未能在 5 秒内取得锁") from exc
            time.sleep(0.02)
    try:
        os.write(descriptor, json.dumps({"pid": os.getpid(), "token": token, "created_at": now()}).encode("utf-8"))
        os.close(descriptor)
        yield lock
    finally:
        try:
            payload = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if payload.get("token") == token:
            lock.unlink(missing_ok=True)


def load_control(project_dir: Path) -> dict[str, Any]:
    path = control_path(project_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    try:
        run_epoch = max(0, int(payload.get("run_epoch", 0) or 0))
    except (TypeError, ValueError):
        run_epoch = 0
    return {
        "version": 1,
        "run_epoch": run_epoch,
        "cancel_requested": bool(payload.get("cancel_requested", False)),
        "updated_at": str(payload.get("updated_at") or ""),
    }


def update_control(project_dir: Path, *, cancel_requested: bool, increment_epoch: bool = True) -> dict[str, Any]:
    with control_update_lock(project_dir):
        payload = load_control(project_dir)
        if increment_epoch:
            payload["run_epoch"] = int(payload.get("run_epoch", 0)) + 1
        payload["cancel_requested"] = bool(cancel_requested)
        payload["updated_at"] = now()
        save_json(control_path(project_dir), payload)
    return payload


@contextmanager
def job_lock(project_dir: Path, *, stale_seconds: int = 12 * 3600) -> Iterator[Path]:
    lock = project_paths(project_dir).status / "story_agent.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        alive = False
        try:
            payload = json.loads(lock.read_text(encoding="utf-8"))
            pid = int(payload.get("pid", 0))
            alive = process_is_alive(pid)
        except (OSError, ValueError, json.JSONDecodeError):
            try:
                alive = time.time() - lock.stat().st_mtime <= stale_seconds
            except OSError:
                alive = False
        if not alive:
            lock.unlink(missing_ok=True)
    token = uuid.uuid4().hex
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise AgentRuntimeError(f"任务已经在运行：{lock}") from exc
    try:
        os.write(descriptor, json.dumps({"pid": os.getpid(), "token": token, "started_at": now()}).encode("utf-8"))
        os.close(descriptor)
        yield lock
    finally:
        try:
            current = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}
        if current.get("token") == token:
            lock.unlink(missing_ok=True)


@contextmanager
def supervisor_start_lock(project_dir: Path) -> Iterator[Path]:
    lock = project_paths(project_dir).status / "story_agent_start.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    if lock.exists():
        try:
            payload = json.loads(lock.read_text(encoding="utf-8"))
            owner_alive = process_is_alive(int(payload.get("pid", 0)))
        except (OSError, ValueError, json.JSONDecodeError):
            try:
                owner_alive = time.time() - lock.stat().st_mtime < 10.0
            except OSError:
                owner_alive = False
        if not owner_alive:
            lock.unlink(missing_ok=True)
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise AgentRuntimeError("另一个 start 正在创建 supervisor，请稍后读取 status") from exc
    try:
        os.write(descriptor, json.dumps({"pid": os.getpid(), "token": token, "created_at": now()}).encode("utf-8"))
        os.close(descriptor)
        yield lock
    finally:
        try:
            payload = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if payload.get("token") == token:
            lock.unlink(missing_ok=True)


def request_cancel(project_dir: Path) -> dict[str, Any]:
    paths = project_paths(project_dir)
    manifest = ensure_manifest_v2(load_manifest(paths) or init_project(project_dir))
    control = update_control(project_dir, cancel_requested=True, increment_epoch=True)
    manifest["agent"]["cancel_requested"] = True
    manifest["agent"]["control_epoch"] = control["run_epoch"]
    if not (paths.status / "story_agent.lock").exists():
        freeze_runtime(manifest["agent"])
        manifest["agent"]["status"] = "cancelled"
        manifest["agent"]["events"].append({"time": now(), "event": "cancel_requested"})
        write_manifest(paths, manifest)
    return manifest


def resume_job(project_dir: Path) -> dict[str, Any]:
    paths = project_paths(project_dir)
    manifest = ensure_manifest_v2(load_manifest(paths) or init_project(project_dir))
    control = update_control(project_dir, cancel_requested=False, increment_epoch=True)
    freeze_runtime(manifest["agent"])
    manifest["agent"]["cancel_requested"] = False
    manifest["agent"]["control_epoch"] = control["run_epoch"]
    manifest["agent"]["status"] = "pending"
    manifest["agent"]["blocked_reason"] = ""
    manifest["agent"]["branch_blockers"] = {}
    for record in manifest["agent"].get("stages", {}).values():
        if isinstance(record, dict) and record.get("status") in {"blocked", "failed", "cancelled", "running"}:
            record["status"] = "pending"
    manifest["agent"]["events"].append({"time": now(), "event": "resume_requested"})
    write_manifest(paths, manifest)
    return manifest


def assert_runnable(manifest: dict[str, Any], project_dir: Path | None = None) -> None:
    ensure_manifest_v2(manifest)
    control_cancelled = bool(load_control(project_dir).get("cancel_requested")) if project_dir is not None else False
    if manifest["agent"].get("cancel_requested") or control_cancelled:
        raise JobCancelled("任务已取消；使用 resume 后才能继续。")
    elapsed_hours = runtime_elapsed_seconds(manifest["agent"]) / 3600
    if elapsed_hours > float(manifest["agent"].get("deadline_hours", DEFAULT_DEADLINE_HOURS)):
        raise AgentRuntimeError(f"任务已超过累计运行时限：{elapsed_hours:.1f} 小时")
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
    elapsed_hours = runtime_elapsed_seconds(agent) / 3600
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
