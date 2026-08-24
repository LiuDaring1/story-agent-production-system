from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from secret_store import read_secret
from video_provider_adapter import resolve_row_generation_seconds
from video_motion import video_receipt_issues, write_video_receipt

from story_video_synthesizer.volcengine_video import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_SECONDS,
    DEFAULT_SIZE,
    SUCCESS_STATUSES,
    QingyunVideoClient,
    build_create_task_body,
    poll_until_done,
    read_jobs_csv,
    write_jobs_csv,
)
from story_video_synthesizer.image_video import validate_image_video_jobs, write_review_page_from_rows
from story_contract_runtime import assert_request_contract_binding
from story_video_synthesizer.toapis_video import (
    DEFAULT_MODEL as TOAPIS_DEFAULT_MODEL,
    MAX_SECONDS as TOAPIS_MAX_SECONDS,
    MAX_PROMPT_CHARS as TOAPIS_MAX_PROMPT_CHARS,
    DEFAULT_RATIO as TOAPIS_DEFAULT_RATIO,
    DEFAULT_SECONDS as TOAPIS_DEFAULT_SECONDS,
    DEFAULT_RESOLUTION as TOAPIS_DEFAULT_RESOLUTION,
    LEGACY_DEFAULT_SECONDS as TOAPIS_LEGACY_DEFAULT_SECONDS,
    ToAPIsVideoClient,
    build_toapis_task_body,
)


RETRYABLE_STATUSES = {"failed", "error", "cancelled", "canceled", "expired"}
INTERNAL_PROMPT_MARKERS = ("[STORY_CONTRACT_V1]", "[VIDEO_MOTION_PLAN_V1]")
# The public model page exposes a 1200-character browser limit, but production
# task processing has rejected much shorter Chinese prompts.  Keep the provider
# payload deliberately below the observed failure range while retaining the
# full reviewed prompt in the jobs CSV.
TOAPIS_SAFE_PROVIDER_PROMPT_CHARS = 120
# ToAPIs advertises 1–15 seconds, but the active upstream channels rejected
# every 2-second Grok Video 1.5 task with "Value must be within the specified
# range".  Four seconds is the user-authorized canary floor; the assembly
# pipeline still probes and trims the downloaded source to the narration span.
TOAPIS_OPERATIONAL_MIN_SECONDS = 4
TOAPIS_COMPACT_PROMPT_MODELS = {"grok-video-1.0", TOAPIS_DEFAULT_MODEL}


def _with_terminal_punctuation(value: str) -> str:
    text = str(value or "").strip()
    if text and not text.endswith(("。", "！", "？", ".", "!", "?")):
        text += "。"
    return text


def compact_provider_prompt(row: dict[str, str], *, max_chars: int) -> str:
    """Compile reviewed machine fields into short provider prose without truncation."""

    prefix = "以首图为准。"
    preservation = "保持首图角色、物体数量、外观和画风，不新增文字、水印或畸形肢体。"
    subject = _with_terminal_punctuation(row.get("subject_action", ""))
    if not subject:
        return ""
    parts = [prefix, subject]
    prompt = "".join(parts) + preservation
    if len(prompt.encode("utf-16-le")) // 2 > max_chars:
        return ""
    for key in ("camera_motion", "environment_motion"):
        clause = _with_terminal_punctuation(row.get(key, ""))
        candidate = "".join(parts) + clause + preservation
        if clause and len(candidate.encode("utf-16-le")) // 2 <= max_chars:
            parts.append(clause)
            prompt = candidate
    return prompt


def provider_prompt_for_row(row: dict[str, str], *, model: str, is_toapis: bool) -> str:
    """Return only provider-facing prose while preserving full audit context in ``prompt``.

    Story contracts and motion plans are intentionally embedded in the jobs CSV so
    deterministic validation and independent review can bind to them.  They are not
    model instructions and must not cross a provider prompt-length boundary.
    """

    retry_prompt = str(row.get("provider_retry_prompt") or "").strip()
    prompt = retry_prompt or str(row.get("prompt") or "").strip()
    if retry_prompt:
        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        previous_sha = str(row.get("previous_provider_prompt_sha256") or "").strip()
        if previous_sha and prompt_sha == previous_sha:
            raise ValueError(
                f"镜头 {row.get('scene', '')} 的硬伤重试 provider prompt 与上次完全相同，禁止重复付费提交"
            )
    if is_toapis and model.strip().lower() in TOAPIS_COMPACT_PROMPT_MODELS:
        if not retry_prompt:
            marker_offsets = [prompt.find(marker) for marker in INTERNAL_PROMPT_MARKERS]
            marker_offsets = [offset for offset in marker_offsets if offset >= 0]
            if marker_offsets:
                prompt = prompt[: min(marker_offsets)].rstrip()
        length = len(prompt.encode("utf-16-le")) // 2
        if not prompt:
            raise ValueError(f"镜头 {row.get('scene', '')} 的 ToAPIs provider prompt 为空")
        if length > TOAPIS_SAFE_PROVIDER_PROMPT_CHARS and not retry_prompt:
            compact = compact_provider_prompt(row, max_chars=TOAPIS_SAFE_PROVIDER_PROMPT_CHARS)
            if not compact:
                raise ValueError(
                    f"镜头 {row.get('scene', '')} 的 ToAPIs provider prompt 为 {length} 字符，"
                    f"且缺少可安全压缩到 {TOAPIS_SAFE_PROVIDER_PROMPT_CHARS} 字符的结构化动作字段"
                )
            prompt = compact
            length = len(prompt.encode("utf-16-le")) // 2
        if retry_prompt and length > TOAPIS_SAFE_PROVIDER_PROMPT_CHARS:
            raise ValueError(
                f"镜头 {row.get('scene', '')} 的硬伤重试 provider prompt 为 {length} 字符，"
                f"超过安全上限 {TOAPIS_SAFE_PROVIDER_PROMPT_CHARS}，禁止静默截断"
            )
        if length > TOAPIS_MAX_PROMPT_CHARS:
            raise ValueError(
                f"镜头 {row.get('scene', '')} 的 ToAPIs provider prompt 超过 "
                f"{TOAPIS_MAX_PROMPT_CHARS} 字符限制：{length}"
            )
    return prompt


def bind_provider_request(row: dict[str, str], prompt: str, seconds: str) -> None:
    row["provider_prompt"] = prompt
    row["provider_prompt_chars"] = str(len(prompt.encode("utf-16-le")) // 2)
    row["provider_prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    row["provider_request_seconds"] = str(seconds)


def reset_retryable_failed_row(row: dict[str, str], video_path: Path) -> bool:
    if video_path.exists() and video_path.stat().st_size > 0:
        return False
    if row.get("status", "").strip() not in RETRYABLE_STATUSES:
        return False
    scene = row.get("scene", "")
    if row.get("task_id", "").strip():
        print(f"重置失败任务 {scene}：清除旧 task_id 后重新提交。", flush=True)
    else:
        print(f"重置提交前失败任务 {scene}：清除错误后重新提交。", flush=True)
    for key in ["task_id", "video_url", "error", "api_response", "query_response"]:
        if key in row:
            row[key] = ""
    row["status"] = "todo"
    row["provider_attempt"] = str(int(row.get("provider_attempt") or "0") + 1)
    return True


def toapis_extra_body(jobs_csv: Path, row: dict[str, str], extra_body: dict[str, object] | None) -> dict[str, object]:
    payload = dict(extra_body or {})
    if "client_business_id" not in payload:
        identity = "|".join(
            [
                str(jobs_csv.expanduser().resolve()),
                row.get("scene", ""),
                row.get("image_filename", ""),
                row.get("provider_prompt", "") or row.get("prompt", ""),
                row.get("provider_request_seconds", "") or row.get("generation_duration", "") or row.get("duration", ""),
                row.get("provider_attempt", "0"),
            ]
        )
        payload["client_business_id"] = "story_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return payload


def saved_video_adapter_value(key: str, fallback: str) -> str:
    config_path = Path(__file__).resolve().parent / "pipeline_config.json"
    if not config_path.exists():
        return fallback
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return fallback
    video_api = config.get("video_api")
    if not isinstance(video_api, dict):
        return fallback
    provider = str(video_api.get("provider") or "").strip()
    adapters = video_api.get("adapters")
    if isinstance(adapters, dict) and isinstance(adapters.get(provider), dict):
        return str(adapters[provider].get(key) or fallback).strip()
    return str(video_api.get(key) or fallback).strip()


def saved_video_base_url() -> str:
    return saved_video_adapter_value("base_url", DEFAULT_BASE_URL)


def saved_video_model() -> str:
    return saved_video_adapter_value("model", DEFAULT_MODEL)


def parse_scene_filter(value: str) -> set[int]:
    scenes: set[int] = set()
    for raw_part in value.split(","):
        part = raw_part.strip()
        if not part:
            continue
        scenes.add(int(part))
    return scenes


def row_duration_value(row: dict[str, str], fallback: float) -> float:
    """Read the actual generation duration before falling back to CLI defaults."""

    raw = row.get("generation_duration") or row.get("duration")
    if raw is None or str(raw).strip() == "":
        return float(fallback)
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"镜头 {row.get('scene', '')} 的 generation_duration/duration 不是数字：{raw!r}") from exc


def row_request_seconds(row: dict[str, str], *, model: str, is_toapis: bool, fallback_seconds: str) -> str:
    """Resolve seconds for one task while preserving legacy provider behavior."""

    if not is_toapis:
        return str(fallback_seconds)
    operational_minimum = (
        TOAPIS_OPERATIONAL_MIN_SECONDS
        if model.strip().lower() == TOAPIS_DEFAULT_MODEL
        else None
    )
    return resolve_row_generation_seconds(
        row,
        model=model,
        fallback_seconds=fallback_seconds,
        min_seconds=operational_minimum,
        max_seconds=TOAPIS_MAX_SECONDS if operational_minimum is not None else None,
    )


def row_extra_body(jobs_csv: Path, row: dict[str, str], extra_body: dict[str, object] | None, *, model: str) -> dict[str, object]:
    """Build idempotency metadata without letting extra_body override row seconds."""

    payload = toapis_extra_body(jobs_csv, row, extra_body)
    if str(model).strip().lower() == TOAPIS_DEFAULT_MODEL:
        payload.pop("seconds", None)
    return payload


def _discover_project_root(jobs_csv: Path) -> Path | None:
    """Find the owning project without accepting an arbitrary manifest path."""

    current = jobs_csv.expanduser().resolve().parent
    for candidate in (current, *current.parents):
        if (candidate / "99_项目状态" / "project_manifest.json").is_file():
            return candidate
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="通过已配置的视频供应商批量生成图片转视频片段")
    parser.add_argument("--jobs-csv", required=True, type=Path, help="prepare_image_video_jobs.py 生成的任务 CSV")
    parser.add_argument("--project-dir", type=Path, help="V3.5 合同锁项目根目录；合同绑定 jobs 必填（可自动发现）")
    parser.add_argument("--images-dir", required=True, type=Path, help="稳定命名图片文件夹")
    parser.add_argument("--videos-dir", required=True, type=Path, help="生成视频保存文件夹")
    parser.add_argument("--api-key-env", default=saved_video_adapter_value("api_key_env", "QINGYUN_API_KEY"), help="只读取这个环境变量或同名 macOS Keychain 服务中的 API Key；不会写入 CSV 或日志")
    parser.add_argument("--api-key", default="", help="兼容旧 CLI；优先使用 --api-key-env 指定的环境变量")
    parser.add_argument("--base-url", default=os.getenv("VIDEO_API_BASE_URL") or saved_video_base_url(), help="API Base URL")
    parser.add_argument("--model", default=os.getenv("VIDEO_MODEL") or saved_video_model(), help="视频生成模型")
    parser.add_argument("--ratio", default="")
    parser.add_argument("--duration", default=10.0, type=float)
    parser.add_argument("--resolution", default="")
    parser.add_argument(
        "--seconds",
        default=os.getenv("VIDEO_SECONDS", ""),
        help="供应商接口生成秒数；ToAPIs grok-video-1.5 支持 1–15 秒，默认 8 秒",
    )
    parser.add_argument("--size", default=os.getenv("VIDEO_SIZE", ""), help="视频清晰度，例如 720P 或 1080P")
    parser.add_argument("--timing-mode", choices=["frames", "duration"], default="frames", help="默认用 frames 支持小数秒")
    parser.add_argument("--parameter-style", choices=["prompt", "body"], default="prompt", help="旧供应商兼容参数；ToAPIs 路径忽略此项")
    parser.add_argument("--camerafixed", action="store_true", help="固定镜头；默认 false")
    parser.add_argument("--watermark", action="store_true", help="添加水印；默认 false")
    parser.add_argument("--start-scene", default=1, type=int)
    parser.add_argument("--end-scene", default=9999, type=int)
    parser.add_argument("--scenes", default="", help="只处理指定镜头，逗号分隔，例如 4,13,18；为空时使用 start/end 范围")
    parser.add_argument("--limit", default=0, type=int, help="最多处理多少条；0 表示不限制")
    parser.add_argument("--poll-interval", default=10, type=int)
    parser.add_argument("--max-wait-seconds", default=1800, type=int)
    parser.add_argument("--max-poll-errors", default=12, type=int, help="轮询任务状态时允许的连续临时网络错误次数")
    parser.add_argument("--submit-only", action="store_true", help="只创建任务，不轮询下载")
    parser.add_argument("--submit-all-first", action="store_true", help="先批量提交待生成任务，再逐个轮询下载；可减少服务端排队导致的串行等待")
    parser.add_argument(
        "--max-submit-first",
        default=int(os.getenv("VIDEO_MAX_SUBMIT_FIRST", "20")),
        type=int,
        help="批量提交模式下最多保留多少个已提交未完成任务；0 表示不限制",
    )
    parser.add_argument("--submit-delay", default=0.2, type=float, help="批量提交时每次创建任务后的短暂停顿，避免请求过快")
    parser.add_argument("--poll-existing", action="store_true", help="只轮询 CSV 中已有 task_id 的任务")
    parser.add_argument("--dry-run", action="store_true", help="只打印请求体，不调用 API")
    parser.add_argument("--execution-mode", choices=["production", "test"], default="production", help="正式模式只接受真实 provider 输出；test 模式允许离线 fixture")
    parser.add_argument("--extra-body-json", default="", help="额外请求体 JSON，例如 '{\"watermark\": false}'")
    args = parser.parse_args()

    continuity_errors = validate_image_video_jobs(args.jobs_csv)
    if continuity_errors:
        raise ValueError(
            "视觉连续性合同/任务校验失败，已在付费调用前阻断：" + "；".join(continuity_errors)
        )

    is_toapis = urlparse(args.base_url).netloc.lower() in {"toapis.com", "www.toapis.com"}
    configured_model = saved_video_model()
    same_configured_toapis_model = is_toapis and args.model.strip() == configured_model
    if not args.seconds.strip():
        configured_seconds = saved_video_adapter_value("default_seconds", "") if same_configured_toapis_model else ""
        if is_toapis:
            fallback_seconds = (
                TOAPIS_DEFAULT_SECONDS
                if args.model.strip().lower() == TOAPIS_DEFAULT_MODEL
                else TOAPIS_LEGACY_DEFAULT_SECONDS
            )
        else:
            fallback_seconds = DEFAULT_SECONDS
        args.seconds = configured_seconds or fallback_seconds
    if not args.ratio.strip():
        configured_ratio = saved_video_adapter_value("default_ratio", "") if same_configured_toapis_model else ""
        args.ratio = configured_ratio or (TOAPIS_DEFAULT_RATIO if is_toapis else "16:9")
    if not args.resolution.strip():
        configured_resolution = saved_video_adapter_value("default_resolution", "") if same_configured_toapis_model else ""
        args.resolution = configured_resolution or (TOAPIS_DEFAULT_RESOLUTION if is_toapis else "720p")
    if not args.size.strip():
        args.size = os.getenv("VIDEO_SIZE") or DEFAULT_SIZE

    rows = read_jobs_csv(args.jobs_csv.expanduser())
    contract_bound = any(str(row.get("story_contract_dependency_sha256") or "").strip() for row in rows)
    project_dir = args.project_dir.expanduser() if args.project_dir else _discover_project_root(args.jobs_csv)
    if contract_bound and project_dir is None:
        raise ValueError("合同绑定 jobs 无法定位 project_dir，已在付费调用前阻断")
    provider_label = "toapis" if is_toapis else "qingyun"

    def bind_download(row: dict[str, str], video_path: Path) -> None:
        receipt, receipt_sha = write_video_receipt(
            args.jobs_csv, row, video_path, provider=provider_label, model=args.model,
            source_kind="provider_generated", execution_mode=args.execution_mode,
            production_eligible=args.execution_mode == "production",
        )
        row.update({
            "video_source_kind": "provider_generated",
            "video_provider": provider_label,
            "video_model": args.model,
            "video_execution_mode": args.execution_mode,
            "production_eligible": "true" if args.execution_mode == "production" else "false",
            "video_receipt_path": str(receipt.resolve()),
            "video_receipt_sha256": receipt_sha,
        })
    args.videos_dir.expanduser().mkdir(parents=True, exist_ok=True)
    api_key = read_secret(args.api_key_env) or args.api_key.strip()
    client_type = ToAPIsVideoClient if is_toapis else QingyunVideoClient
    client = None if args.dry_run else client_type(api_key=api_key, base_url=args.base_url)
    extra_body = json.loads(args.extra_body_json) if args.extra_body_json.strip() else None
    selected_scene_numbers = parse_scene_filter(args.scenes)
    selected_rows = [
        row for row in rows
        if (
            int(row["scene"]) in selected_scene_numbers
            if selected_scene_numbers
            else args.start_scene <= int(row["scene"]) <= args.end_scene
        )
    ]
    if args.limit:
        selected_rows = selected_rows[: args.limit]

    # Resolve every selected request before resetting failed rows or issuing a
    # paid call.  A provider-limit violation therefore fails closed without
    # discarding the previous task/error evidence.
    provider_prompts = {
        row.get("scene", ""): provider_prompt_for_row(row, model=args.model, is_toapis=is_toapis)
        for row in selected_rows
    }

    reset_any = False
    for row in selected_rows:
        video_path = args.videos_dir.expanduser() / row["target_video_filename"]
        reset_any = reset_retryable_failed_row(row, video_path) or reset_any
    if reset_any:
        write_jobs_csv(args.jobs_csv.expanduser(), rows)

    batch_submit_limited = False
    if args.submit_all_first and not args.dry_run and not args.poll_existing:
        assert client is not None
        submitted = 0
        in_flight = sum(
            1
            for row in selected_rows
            if row.get("task_id", "").strip()
            and row.get("status", "") not in {"downloaded", "approved"}
            and not ((args.videos_dir.expanduser() / row["target_video_filename"]).exists())
        )
        for row in selected_rows:
            video_path = args.videos_dir.expanduser() / row["target_video_filename"]
            if video_path.exists() and video_path.stat().st_size > 0:
                if contract_bound and video_receipt_issues(row, video_path, production_mode=args.execution_mode == "production"):
                    raise ValueError(f"镜头 {row['scene']} 已有视频缺少当前正式来源 receipt，禁止按文件存在跳过")
                row["status"] = "downloaded"
                continue
            if row.get("status", "") in {"downloaded", "approved"}:
                continue
            if row.get("task_id", "").strip():
                continue
            if args.max_submit_first and in_flight >= args.max_submit_first:
                batch_submit_limited = True
                print(
                    f"本轮批量提交已达到上限 {args.max_submit_first} 条，先轮询下载已提交任务；剩余镜头下次继续。",
                    flush=True,
                )
                break
            image_path = args.images_dir.expanduser() / row["image_filename"]
            duration = row_duration_value(row, args.duration)
            frames = int(row["frames"]) if args.timing_mode == "frames" and row.get("frames", "").strip() else None
            request_seconds = row_request_seconds(
                row,
                model=args.model,
                is_toapis=is_toapis,
                fallback_seconds=args.seconds,
            )
            print(f"批量创建任务 {row['scene']}：{row['image_filename']}", flush=True)
            try:
                request_prompt = provider_prompts[row.get("scene", "")]
                bind_provider_request(row, request_prompt, request_seconds)
                request_extra = row_extra_body(args.jobs_csv, row, extra_body, model=args.model) if is_toapis else extra_body
                if is_toapis and isinstance(request_extra, dict):
                    row["client_business_id"] = str(request_extra.get("client_business_id") or "")
                if contract_bound:
                    assert_request_contract_binding(project_dir, "image_video", row)
                created = client.create_task(
                    model=args.model,
                    prompt=request_prompt,
                    image_path=image_path,
                    ratio=args.ratio,
                    duration=duration,
                    resolution=args.resolution.strip() or None,
                    frames=frames,
                    seconds=request_seconds,
                    size=args.size,
                    parameter_style=args.parameter_style,
                    camera_fixed=args.camerafixed,
                    watermark=args.watermark,
                    extra_body=request_extra,
                )
            except Exception as exc:
                if "local_quota_not_enough" in str(exc) or "上游负载已饱和" in str(exc):
                    batch_submit_limited = True
                    print(f"批量提交暂停：{exc}", flush=True)
                    print("先轮询下载已经提交成功的任务；剩余镜头下次继续。", flush=True)
                    break
                raise
            row["task_id"] = created.task_id
            row["status"] = "submitted"
            row["api_response"] = json.dumps(created.raw, ensure_ascii=False)
            write_jobs_csv(args.jobs_csv.expanduser(), rows)
            submitted += 1
            in_flight += 1
            if args.submit_delay > 0:
                import time
                time.sleep(args.submit_delay)
        print(f"批量提交完成：新增 {submitted} 条任务。", flush=True)
        if args.submit_only:
            output_dir = args.jobs_csv.expanduser().parent
            slug = args.jobs_csv.expanduser().stem.removesuffix("_image_video_jobs")
            review_html = write_review_page_from_rows(rows, output_dir, slug)
            print(f"处理完成：{submitted} 条。")
            print(f"已刷新审核页：{review_html}")
            return

    if args.submit_all_first and not args.dry_run and (args.max_submit_first or batch_submit_limited):
        selected_rows = [
            row
            for row in selected_rows
            if row.get("task_id", "").strip()
            and row.get("status", "") not in {"downloaded", "approved"}
        ]

    processed = 0
    for row in selected_rows:
        scene = int(row["scene"])
        status = row.get("status", "")
        video_path = args.videos_dir.expanduser() / row["target_video_filename"]
        if video_path.exists() and video_path.stat().st_size > 0:
            if contract_bound and video_receipt_issues(row, video_path, production_mode=args.execution_mode == "production"):
                raise ValueError(f"镜头 {row['scene']} 已有视频缺少当前正式来源 receipt，禁止按文件存在跳过")
            row["status"] = "downloaded"
            continue
        if status in {"downloaded", "approved"}:
            continue

        try:
            duration = row_duration_value(row, args.duration)
            frames = int(row["frames"]) if args.timing_mode == "frames" and row.get("frames", "").strip() else None
            request_seconds = row_request_seconds(
                row,
                model=args.model,
                is_toapis=is_toapis,
                fallback_seconds=args.seconds,
            )
            if args.dry_run:
                image_path = args.images_dir.expanduser() / row["image_filename"]
                request_prompt = provider_prompts[row.get("scene", "")]
                if is_toapis:
                    request_row = dict(row)
                    bind_provider_request(request_row, request_prompt, request_seconds)
                    request_extra = row_extra_body(args.jobs_csv, request_row, extra_body, model=args.model)
                    body = build_toapis_task_body(
                        model=args.model,
                        prompt=request_prompt,
                        image_url=f"UPLOAD_REQUIRED:{image_path.name}",
                        ratio=args.ratio,
                        seconds=request_seconds,
                        resolution=args.resolution.strip() or args.size,
                        extra_body=request_extra,
                    )
                else:
                    body = build_create_task_body(
                        model=args.model,
                        prompt=request_prompt,
                        image_path=image_path,
                        ratio=args.ratio,
                        duration=duration,
                        resolution=args.resolution.strip() or None,
                        frames=frames,
                        seconds=request_seconds,
                        size=args.size,
                        parameter_style=args.parameter_style,
                        camera_fixed=args.camerafixed,
                        watermark=args.watermark,
                        extra_body=extra_body,
                    )
                redacted = json.loads(json.dumps(body, ensure_ascii=False))
                if "input_reference" in redacted:
                    redacted["input_reference"] = str(image_path)
                print(json.dumps(redacted, ensure_ascii=False, indent=2), flush=True)
                processed += 1
                continue

            if not args.poll_existing:
                task_id = row.get("task_id", "").strip()
                if not task_id:
                    image_path = args.images_dir.expanduser() / row["image_filename"]
                    print(f"创建任务 {row['scene']}：{row['image_filename']}", flush=True)
                    assert client is not None
                    request_prompt = provider_prompts[row.get("scene", "")]
                    bind_provider_request(row, request_prompt, request_seconds)
                    request_extra = row_extra_body(args.jobs_csv, row, extra_body, model=args.model) if is_toapis else extra_body
                    if is_toapis and isinstance(request_extra, dict):
                        row["client_business_id"] = str(request_extra.get("client_business_id") or "")
                    if contract_bound:
                        assert_request_contract_binding(project_dir, "image_video", row)
                    created = client.create_task(
                        model=args.model,
                        prompt=request_prompt,
                        image_path=image_path,
                        ratio=args.ratio,
                        duration=duration,
                        resolution=args.resolution.strip() or None,
                        frames=frames,
                        seconds=request_seconds,
                        size=args.size,
                        parameter_style=args.parameter_style,
                        camera_fixed=args.camerafixed,
                        watermark=args.watermark,
                        extra_body=request_extra,
                    )
                    row["task_id"] = created.task_id
                    row["status"] = "submitted"
                    row["api_response"] = json.dumps(created.raw, ensure_ascii=False)
                    write_jobs_csv(args.jobs_csv.expanduser(), rows)

            if args.submit_only:
                processed += 1
                continue

            task_id = row.get("task_id", "").strip()
            if not task_id:
                continue

            print(f"等待任务 {row['scene']}：{task_id}", flush=True)
            assert client is not None
            result = poll_until_done(
                client,
                task_id,
                interval=args.poll_interval,
                max_wait_seconds=args.max_wait_seconds,
                max_poll_errors=args.max_poll_errors,
            )
            row["status"] = result.status
            row["video_url"] = result.video_url or ""
            row["error"] = result.error or ""
            row["query_response"] = json.dumps(result.raw, ensure_ascii=False)

            if result.status in SUCCESS_STATUSES and result.video_url:
                print(f"下载视频 {row['scene']}：{video_path.name}", flush=True)
                client.download(result.video_url, video_path)
                row["status"] = "downloaded"
                bind_download(row, video_path)
            elif result.status in SUCCESS_STATUSES:
                print(f"下载视频 {row['scene']}：{video_path.name}", flush=True)
                client.download_task_video(task_id, video_path)
                row["status"] = "downloaded"
                bind_download(row, video_path)

            write_jobs_csv(args.jobs_csv.expanduser(), rows)
            processed += 1
        except Exception as exc:
            row["status"] = "error"
            row["error"] = str(exc)
            write_jobs_csv(args.jobs_csv.expanduser(), rows)
            print(f"任务 {row['scene']} 失败：{exc}", flush=True)
            raise

    output_dir = args.jobs_csv.expanduser().parent
    slug = args.jobs_csv.expanduser().stem.removesuffix("_image_video_jobs")
    if args.dry_run:
        print(f"Dry run 完成：{processed} 条。")
        return
    write_jobs_csv(args.jobs_csv.expanduser(), rows)
    review_html = write_review_page_from_rows(rows, output_dir, slug)
    print(f"处理完成：{processed} 条。")
    print(f"已刷新审核页：{review_html}")


if __name__ == "__main__":
    main()
