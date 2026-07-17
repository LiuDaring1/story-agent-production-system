from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from story_agent_runtime import (
    STORY_STAGE_SEQUENCE,
    ensure_manifest_v2,
    file_sha256,
    review_bundle_is_current,
    review_passes,
    write_review_bundle,
)
from story_project import load_manifest, project_paths, save_json, write_manifest


REQUIRED_REVIEW_FILES = {
    "source_edit_review": ("source_edit/source_edit_review.json", None),
    "story_images_review": ("reviews/story_images_review_review.json", "reviews/story_images_bundle.json"),
    "video_prompt_review": ("reviews/video_prompt_review.json", "reviews/video_prompt_bundle.json"),
    "video_review": ("reviews/video_review_review.json", "reviews/video_bundle.json"),
    "release_preview": ("reviews/release_preview_review.json", "reviews/release_preview_bundle.json"),
    "release_video_review": ("reviews/release_video_review_review.json", "reviews/release_video_bundle.json"),
    "product_annotation_review": ("reviews/product_annotation_review_review.json", "reviews/product_annotation_bundle.json"),
    "product_package_review": ("reviews/product_package_review_review.json", "reviews/product_package_bundle.json"),
    "publish_package_review": ("reviews/publish_package_review_review.json", "reviews/publish_package_bundle.json"),
}

KNOWN_INPUT_FIELDS = {
    "story_text",
    "narration",
    "music",
    "greenscreen_video",
    "greenscreen_video_original",
    "color_lut",
    "extracted_narration",
}


def _same_path(left: Path, right: Path) -> bool:
    return left.expanduser().resolve() == right.expanduser().resolve()


def _artifact_record_error(record: Any, expected_path: Path, *, label: str) -> str:
    if not isinstance(record, dict):
        return f"{label}来源记录缺失"
    recorded_path = Path(str(record.get("path") or ""))
    if not str(recorded_path) or not _same_path(recorded_path, expected_path):
        return f"{label}来源路径不匹配"
    if not expected_path.is_file():
        return f"{label}文件缺失"
    try:
        recorded_bytes = int(record.get("bytes", -1))
    except (TypeError, ValueError):
        return f"{label}大小记录无效"
    if recorded_bytes != expected_path.stat().st_size:
        return f"{label}大小已变化"
    if str(record.get("sha256") or "") != file_sha256(expected_path):
        return f"{label} SHA-256 已变化"
    return ""


def _start_epoch(agent: dict[str, Any]) -> float | None:
    try:
        return time.mktime(time.strptime(str(agent.get("unattended_started_at") or ""), "%Y-%m-%d %H:%M:%S"))
    except (OverflowError, TypeError, ValueError):
        return None


def _predates_start(path: Path, start_epoch: float | None, *, tolerance_seconds: float = 5.0) -> bool:
    return start_epoch is not None and path.is_file() and path.stat().st_mtime < start_epoch - tolerance_seconds


def _bundle_predates_start(bundle: Path, start_epoch: float | None) -> bool:
    payload = _load_json(bundle)
    artifacts = payload.get("artifacts", []) if payload else []
    return any(
        isinstance(item, dict)
        and (path_text := str(item.get("path") or ""))
        and _predates_start(Path(path_text), start_epoch)
        for item in artifacts
    )


def _single_greenscreen_contract_errors(project_dir: Path, manifest: dict[str, Any]) -> list[str]:
    agent = manifest.get("agent", {}) if isinstance(manifest.get("agent"), dict) else {}
    source = agent.get("source", {}) if isinstance(agent.get("source"), dict) else {}
    inputs = manifest.get("inputs", {}) if isinstance(manifest.get("inputs"), dict) else {}
    outputs = manifest.get("outputs", {}) if isinstance(manifest.get("outputs"), dict) else {}
    contract = agent.get("input_contract")
    errors: list[str] = []
    if not isinstance(contract, dict) or contract.get("version") != 1 or contract.get("mode") != "single_greenscreen":
        return ["缺少 submit 创建的单绿幕输入契约"]

    user_inputs = contract.get("user_inputs")
    if not isinstance(user_inputs, list) or len(user_inputs) != 1 or not isinstance(user_inputs[0], dict):
        errors.append("用户内容输入不是唯一一段绿幕视频")
        user_record: dict[str, Any] = {}
    else:
        user_record = user_inputs[0]
        if user_record.get("role") != "greenscreen_video":
            errors.append("唯一用户内容输入角色不是 greenscreen_video")

    source_copy_text = str(source.get("project_copy") or "")
    original_input_text = str(inputs.get("greenscreen_video_original") or "")
    if not source_copy_text or not original_input_text:
        errors.append("原始绿幕项目副本路径缺失")
    else:
        source_copy = Path(source_copy_text)
        original_input = Path(original_input_text)
        if not _same_path(source_copy, original_input):
            errors.append("原始绿幕输入没有指向 submit 项目副本")
        record_error = _artifact_record_error(user_record, source_copy, label="原始绿幕")
        if record_error:
            errors.append(record_error)
        elif str(source.get("sha256") or "") != str(user_record.get("sha256") or ""):
            errors.append("Agent 原片 SHA-256 与单输入契约不一致")

    unknown_inputs = sorted(key for key, value in inputs.items() if key not in KNOWN_INPUT_FIELDS and value)
    if unknown_inputs:
        errors.append("检测到未声明的人工输入字段：" + "、".join(unknown_inputs))
    if inputs.get("music"):
        errors.append("检测到人工提供配乐")

    processing_assets = contract.get("processing_assets")
    if not isinstance(processing_assets, list):
        errors.append("处理配置资产记录无效")
        processing_assets = []
    unexpected_assets = [
        str(item.get("role") or "unknown")
        for item in processing_assets
        if not isinstance(item, dict) or item.get("role") != "color_lut"
    ]
    if unexpected_assets:
        errors.append("检测到未允许的处理配置资产：" + "、".join(unexpected_assets))
    color_lut_text = str(inputs.get("color_lut") or "")
    lut_records = [item for item in processing_assets if isinstance(item, dict) and item.get("role") == "color_lut"]
    if color_lut_text:
        if len(lut_records) != 1:
            errors.append("LUT 输入没有唯一处理配置记录")
        else:
            record_error = _artifact_record_error(lut_records[0], Path(color_lut_text), label="LUT")
            if record_error:
                errors.append(record_error)
    elif lut_records:
        errors.append("输入未使用 LUT，但契约存在 LUT 记录")

    decisions_text = str(outputs.get("source_edit_decisions") or "")
    decisions = Path(decisions_text) if decisions_text else Path()
    decisions_sha256 = file_sha256(decisions) if decisions_text and decisions.is_file() else ""
    if not decisions_sha256:
        errors.append("源剪辑决定缺失，无法证明派生输入")
    derived = contract.get("derived_inputs")
    if not isinstance(derived, dict):
        errors.append("派生输入来源记录无效")
        derived = {}
    source_sha256 = str(source.get("sha256") or "")
    start_epoch = _start_epoch(agent)
    required_derived = {
        "story_text": str(inputs.get("story_text") or ""),
        "clean_greenscreen_video": str(inputs.get("greenscreen_video") or ""),
        "clean_narration": str(inputs.get("extracted_narration") or ""),
    }
    for role, path_text in required_derived.items():
        if not path_text:
            errors.append(f"派生输入缺失：{role}")
            continue
        record = derived.get(role)
        record_error = _artifact_record_error(record, Path(path_text), label=role)
        if record_error:
            errors.append(record_error)
            continue
        if record.get("producer") != "source_video_pipeline":
            errors.append(f"{role} 不是由源视频流水线派生")
        if record.get("source_sha256") != source_sha256:
            errors.append(f"{role} 没有绑定当前原片 SHA-256")
        if record.get("decisions_sha256") != decisions_sha256:
            errors.append(f"{role} 没有绑定当前剪辑决定 SHA-256")
        if _predates_start(Path(path_text), start_epoch):
            errors.append(f"{role} 在无人值守启动前已存在")

    narration_text = str(inputs.get("narration") or "")
    clean_narration_text = required_derived["clean_narration"]
    if narration_text and clean_narration_text and not _same_path(Path(narration_text), Path(clean_narration_text)):
        errors.append("旁白不是源剪辑派生的清洁音频")
    return errors


def _supervisor_record_error(project_dir: Path, manifest: dict[str, Any], record_path: Path) -> str:
    record = _load_json(record_path)
    if record is None:
        return "supervisor 启动记录缺失或损坏"
    agent = manifest.get("agent", {}) if isinstance(manifest.get("agent"), dict) else {}
    expected_job = str(agent.get("job_id") or "")
    if record.get("kind") != "story_agent_start_v1":
        return "supervisor 启动记录类型无效"
    if str(record.get("job_id") or "") != expected_job:
        return "supervisor 启动记录任务号不匹配"
    try:
        recorded_project = Path(str(record.get("project_dir") or ""))
        if not str(recorded_project) or not _same_path(recorded_project, project_dir):
            return "supervisor 启动记录项目路径不匹配"
        pid = int(record.get("pid", 0))
    except (OSError, ValueError, TypeError):
        return "supervisor 启动记录字段无效"
    if pid <= 0:
        return "supervisor 启动 PID 无效"
    started_at = str(record.get("started_at") or "")
    if not started_at:
        return "supervisor 启动时间缺失"
    log_path = Path(str(record.get("log") or ""))
    if not str(log_path) or not log_path.is_file():
        return "supervisor 日志缺失"
    try:
        log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-20000:]
    except OSError:
        return "supervisor 日志不可读"
    if "DONE: 故事生产 Agent 已完成全部阶段。" not in log_tail:
        return "supervisor 日志没有 Agent 完成标记"
    command = record.get("command")
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        return "supervisor 命令记录无效"
    try:
        job_index = command.index("--job")
        mode_index = command.index("--codex-mode")
    except ValueError:
        return "supervisor 命令缺少无人值守参数"
    if (
        "run" not in command
        or "--execute" not in command
        or job_index + 1 >= len(command)
        or command[job_index + 1] != expected_job
        or mode_index + 1 >= len(command)
        or command[mode_index + 1] != "cli"
        or not any(Path(item).name == "story_agent.py" for item in command)
    ):
        return "supervisor 命令不是当前任务的 cli 执行模式"
    if agent.get("unattended_started_at") != started_at:
        return "manifest 与 supervisor 启动时间不一致"
    manifest_log = Path(str(agent.get("unattended_supervisor_log") or ""))
    if not str(manifest_log) or not _same_path(manifest_log, log_path):
        return "manifest 与 supervisor 日志路径不一致"
    manifest_record = Path(str(agent.get("unattended_supervisor_record") or ""))
    if not str(manifest_record) or not _same_path(manifest_record, record_path):
        return "manifest 与 supervisor 启动记录路径不一致"
    record_sha256 = file_sha256(record_path)
    if agent.get("unattended_supervisor_record_sha256") != record_sha256:
        return "supervisor 启动记录 SHA-256 已变化"
    events = agent.get("events", []) if isinstance(agent.get("events"), list) else []
    if not any(
        isinstance(item, dict)
        and item.get("event") == "unattended_supervisor_started"
        and item.get("record_sha256") == record_sha256
        for item in events
    ):
        return "manifest 缺少匹配的 supervisor 启动事件"
    return ""


def record_unattended_launch(project_dir: Path, *, supervisor_record: Path) -> dict[str, Any]:
    """Bind a successfully-created supervisor record to the project manifest."""
    paths = project_paths(project_dir.expanduser())
    manifest = load_manifest(paths)
    if manifest is None:
        raise ValueError("项目 manifest 缺失，不能记录无人值守启动。")
    record = _load_json(supervisor_record)
    if record is None:
        raise ValueError("supervisor 启动记录缺失或损坏，不能记录无人值守启动。")
    manifest = ensure_manifest_v2(manifest)
    expected_job = str(manifest.get("agent", {}).get("job_id") or "")
    if (
        record.get("kind") != "story_agent_start_v1"
        or str(record.get("job_id") or "") != expected_job
        or int(record.get("pid", 0)) <= 0
        or not str(record.get("started_at") or "")
    ):
        raise ValueError("supervisor 启动记录与当前任务不匹配。")
    agent = manifest.setdefault("agent", {})
    agent["unattended_mode"] = True
    agent["unattended_started_at"] = str(record["started_at"])
    agent["unattended_supervisor_log"] = str(Path(str(record["log"])).expanduser().resolve())
    agent["unattended_supervisor_record"] = str(supervisor_record.expanduser().resolve())
    agent["unattended_supervisor_record_sha256"] = file_sha256(supervisor_record)
    launch_event = {
        "time": str(record["started_at"]),
        "event": "unattended_supervisor_started",
        "pid": int(record["pid"]),
        "record_sha256": agent["unattended_supervisor_record_sha256"],
    }
    events = agent.setdefault("events", [])
    if not any(
        isinstance(item, dict)
        and item.get("event") == launch_event["event"]
        and item.get("record_sha256") == launch_event["record_sha256"]
        for item in events
    ):
        agent["events"] = [*events, launch_event][-500:]
    write_manifest(paths, manifest)
    return manifest


def final_delivery_artifacts(project_dir: Path, manifest: dict[str, Any]) -> list[Path]:
    paths = project_paths(project_dir)
    publish = paths.publish
    artifacts = [
        paths.root / "总交付清单.md",
        Path(str(manifest.get("outputs", {}).get("main_release_video", ""))),
        Path(str(manifest.get("outputs", {}).get("library_release_video", ""))),
        Path(str(manifest.get("outputs", {}).get("product_base", ""))),
        Path(str(manifest.get("outputs", {}).get("product_advanced", ""))),
        publish / "main" / "copy.md",
        publish / "library" / "copy.md",
    ]
    artifacts.extend(
        publish / account / "covers" / f"cover_{ratio}.png"
        for account in ("main", "library")
        for ratio in ("3x4", "4x3", "16x9")
    )
    return [path for path in artifacts if str(path) and path.exists()]


def record_human_signoff(
    project_dir: Path,
    *,
    result: str,
    minutes: float,
    notes: str = "",
    reviewer: str = "用户人工终审",
) -> Path:
    project_dir = project_dir.expanduser()
    paths = project_paths(project_dir)
    manifest = load_manifest(paths)
    if manifest is None or not manifest.get("completed_at") or manifest.get("agent", {}).get("status") != "completed":
        raise ValueError("只有已完成全部 Agent 阶段的项目才能记录人工终审。")
    normalized_result = result.strip().lower()
    if normalized_result not in {"pass", "fail"}:
        raise ValueError("人工终审结果只能是 pass 或 fail。")
    if not math.isfinite(float(minutes)) or minutes < 0:
        raise ValueError("人工终审分钟数必须是有限的非负数。")
    artifacts = final_delivery_artifacts(project_dir, manifest)
    if len(artifacts) < 13:
        raise ValueError("最终交付物不完整，不能记录人工终审。")
    bundle = write_review_bundle(paths.status / "reviews" / "human_final_review_bundle.json", artifacts)
    target = paths.status / "human_final_review.json"
    save_json(
        target,
        {
            "version": 1,
            "result": normalized_result,
            "minutes": round(float(minutes), 2),
            "reviewer": reviewer.strip() or "用户人工终审",
            "notes": notes.strip(),
            "reviewed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "artifact_bundle": str(bundle),
            "artifact_sha256": file_sha256(bundle),
        },
    )
    return target


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def evaluate_project_for_promotion(project_dir: Path) -> dict[str, Any]:
    paths = project_paths(project_dir)
    manifest = load_manifest(paths)
    reasons: list[str] = []
    if manifest is None:
        return {"project_dir": str(project_dir), "qualified": False, "reasons": ["manifest 缺失或损坏"]}
    agent = manifest.get("agent", {}) if isinstance(manifest.get("agent"), dict) else {}
    story = manifest.get("story", {}) if isinstance(manifest.get("story"), dict) else {}
    if not manifest.get("completed_at") or agent.get("status") != "completed":
        reasons.append("Agent 未完成全部阶段")
    if not agent.get("unattended_mode") or not agent.get("unattended_started_at"):
        reasons.append("缺少由 start 后台启动的无人值守运行证据")
    else:
        record_path = Path(str(agent.get("unattended_supervisor_record") or ""))
        launch_error = _supervisor_record_error(project_dir, manifest, record_path)
        if launch_error:
            reasons.append(f"无人值守 supervisor 启动证明无效：{launch_error}")
    stages = agent.get("stages", {}) if isinstance(agent.get("stages"), dict) else {}
    incomplete_stages = [name for name in STORY_STAGE_SEQUENCE if stages.get(name, {}).get("status") != "passed"]
    if incomplete_stages:
        reasons.append("Agent 阶段没有全部通过：" + "、".join(incomplete_stages))
    if agent.get("last_checkpoint") != STORY_STAGE_SEQUENCE[-1]:
        reasons.append("最后成功检查点不是 doctor")
    start_epoch = _start_epoch(agent)
    if agent.get("unattended_started_at") and start_epoch is None:
        reasons.append("无人值守启动时间格式无效")
    try:
        spent = float(agent.get("budget", {}).get("spent", 0.0))
    except (TypeError, ValueError):
        spent = math.nan
    try:
        active_hours = float(agent.get("active_elapsed_seconds", 0.0)) / 3600
    except (TypeError, ValueError):
        active_hours = math.nan
    if not math.isfinite(spent) or spent < 0:
        reasons.append("单集成本记录无效")
    elif spent > 50.0:
        reasons.append(f"单集成本 ¥{spent:.2f} 超过 ¥50")
    if not math.isfinite(active_hours) or active_hours < 0:
        reasons.append("有效运行时间记录无效")
    elif active_hours > 10.0:
        reasons.append(f"有效运行 {active_hours:.2f} 小时超过 10 小时")
    source = agent.get("source", {}) if isinstance(agent.get("source"), dict) else {}
    source_sha256 = str(source.get("sha256") or "")
    if not source_sha256:
        reasons.append("缺少原始绿幕视频 SHA-256")
    reasons.extend(f"单绿幕输入证明无效：{item}" for item in _single_greenscreen_contract_errors(project_dir, manifest))

    outputs = manifest.get("outputs", {}) if isinstance(manifest.get("outputs"), dict) else {}
    for key in ("main_release_video", "library_release_video", "publish_package", "product_base", "product_advanced"):
        value = str(outputs.get(key) or "")
        if not value or not Path(value).exists():
            reasons.append(f"缺少交付物：{key}")

    review_status: dict[str, str] = {}
    for name, (review_relative, bundle_relative) in REQUIRED_REVIEW_FILES.items():
        review_path = paths.status / review_relative
        payload = _load_json(review_path)
        if payload is None:
            reasons.append(f"缺少有效独立审核：{name}")
            review_status[name] = "missing"
            continue
        if bundle_relative is None:
            if not review_passes(payload):
                reasons.append(f"独立审核未达标：{name}")
                review_status[name] = "failed"
            elif name == "source_edit_review":
                decisions = Path(str(outputs.get("source_edit_decisions") or ""))
                if not decisions.is_file() or payload.get("artifact_sha256") != file_sha256(decisions):
                    reasons.append("独立审核哈希不是当前版本：source_edit_review")
                    review_status[name] = "stale"
                elif _predates_start(decisions, start_epoch):
                    reasons.append("审核产物早于无人值守启动：source_edit_review")
                    review_status[name] = "predates_start"
                else:
                    review_status[name] = "passed"
            else:
                review_status[name] = "passed"
            continue
        bundle = paths.status / bundle_relative
        if not bundle.is_file() or not review_bundle_is_current(bundle) or not review_passes(payload, artifact=bundle):
            reasons.append(f"独立审核哈希不是当前版本：{name}")
            review_status[name] = "stale"
        elif _bundle_predates_start(bundle, start_epoch):
            reasons.append(f"审核产物早于无人值守启动：{name}")
            review_status[name] = "predates_start"
        else:
            review_status[name] = "passed"

    signoff_path = paths.status / "human_final_review.json"
    signoff = _load_json(signoff_path)
    human_minutes: float | None = None
    if signoff is None:
        reasons.append("缺少用户人工终审记录")
    else:
        try:
            human_minutes = float(signoff.get("minutes", math.nan))
        except (TypeError, ValueError):
            human_minutes = math.nan
        bundle = Path(str(signoff.get("artifact_bundle") or ""))
        if signoff.get("result") != "pass":
            reasons.append("用户人工终审未通过")
        if not math.isfinite(human_minutes) or human_minutes < 0:
            reasons.append("用户人工终审耗时记录无效")
        elif human_minutes > 10.0:
            reasons.append(f"用户人工终审耗时 {human_minutes:.2f} 分钟超过 10 分钟")
        if not bundle.is_file() or signoff.get("artifact_sha256") != file_sha256(bundle) or not review_bundle_is_current(bundle):
            reasons.append("用户人工终审绑定的交付物已变化")
        elif _bundle_predates_start(bundle, start_epoch):
            reasons.append("用户人工终审包含无人值守启动前预置的交付物")

    return {
        "project_dir": str(project_dir),
        "job_id": str(agent.get("job_id") or ""),
        "story_name": str(story.get("name") or ""),
        "source_sha256": source_sha256,
        "completed_at": manifest.get("completed_at", ""),
        "cost_cny": round(spent, 2) if math.isfinite(spent) else None,
        "active_hours": round(active_hours, 3) if math.isfinite(active_hours) else None,
        "human_review_minutes": human_minutes if human_minutes is None or math.isfinite(human_minutes) else None,
        "review_status": review_status,
        "qualified": not reasons,
        "reasons": reasons,
    }


def build_promotion_report(projects_root: Path) -> dict[str, Any]:
    projects_root = projects_root.expanduser()
    manifests = sorted(projects_root.rglob("99_项目状态/project_manifest.json")) if projects_root.exists() else []
    projects = [evaluate_project_for_promotion(path.parent.parent) for path in manifests]
    seen_story_names: set[str] = set()
    seen_sources: set[str] = set()
    qualified: list[dict[str, Any]] = []
    for item in projects:
        if not item.get("qualified"):
            continue
        story_name = str(item.get("story_name") or "")
        source_sha256 = str(item.get("source_sha256") or "")
        if story_name in seen_story_names or source_sha256 in seen_sources:
            item["qualified"] = False
            item.setdefault("reasons", []).append("故事名或原始视频与已计数项目重复")
            continue
        seen_story_names.add(story_name)
        seen_sources.add(source_sha256)
        qualified.append(item)
    return {
        "version": 1,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "projects_root": str(projects_root),
        "target_distinct_stories": 3,
        "qualified_distinct_stories": len(qualified),
        "ready_for_default_entry": len(qualified) >= 3,
        "projects": projects,
    }


def render_promotion_markdown(report: dict[str, Any], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 全自动故事 Agent 转正资格报告",
        "",
        f"- 检查时间：{report.get('checked_at', '')}",
        f"- 合格不同故事：{report.get('qualified_distinct_stories', 0)} / {report.get('target_distinct_stories', 3)}",
        f"- 可设为默认入口：{'是' if report.get('ready_for_default_entry') else '否'}",
        "",
        "| 故事 | 状态 | 成本 | 有效运行 | 人工终审 | 不合格原因 |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for item in report.get("projects", []):
        minutes = item.get("human_review_minutes")
        minute_text = "未记录" if minutes is None or not math.isfinite(float(minutes)) else f"{minutes:.2f} 分钟"
        reasons = "；".join(item.get("reasons", [])) or "无"
        cost = item.get("cost_cny")
        hours = item.get("active_hours")
        cost_text = "无效" if cost is None else f"¥{float(cost):.2f}"
        hours_text = "无效" if hours is None else f"{float(hours):.2f} 小时"
        lines.append(
            f"| {item.get('story_name') or Path(item.get('project_dir', '')).name} | "
            f"{'合格' if item.get('qualified') else '不合格'} | {cost_text} | "
            f"{hours_text} | {minute_text} | {reasons} |"
        )
    if not report.get("projects"):
        lines.append("| 尚无项目 | 不合格 | ¥0.00 | 0.00 小时 | 未记录 | 未发现 manifest |")
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output
