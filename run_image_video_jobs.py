from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

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
from story_video_synthesizer.image_video import write_review_page_from_rows


RETRYABLE_STATUSES = {"failed", "error", "cancelled", "canceled", "expired"}


def reset_retryable_failed_row(row: dict[str, str], video_path: Path) -> bool:
    if video_path.exists() and video_path.stat().st_size > 0:
        return False
    if row.get("status", "").strip() not in RETRYABLE_STATUSES:
        return False
    if not row.get("task_id", "").strip():
        return False

    scene = row.get("scene", "")
    print(f"重置失败任务 {scene}：清除旧 task_id 后重新提交。", flush=True)
    for key in ["task_id", "video_url", "error", "api_response", "query_response"]:
        if key in row:
            row[key] = ""
    row["status"] = "todo"
    return True


def saved_video_base_url() -> str:
    config_path = Path(__file__).resolve().parent / "pipeline_config.json"
    if not config_path.exists():
        return DEFAULT_BASE_URL
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return DEFAULT_BASE_URL
    video_api = config.get("video_api")
    if not isinstance(video_api, dict):
        return DEFAULT_BASE_URL
    return str(video_api.get("base_url") or DEFAULT_BASE_URL).strip()


def saved_video_model() -> str:
    config_path = Path(__file__).resolve().parent / "pipeline_config.json"
    if not config_path.exists():
        return DEFAULT_MODEL
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return DEFAULT_MODEL
    video_api = config.get("video_api")
    if not isinstance(video_api, dict):
        return DEFAULT_MODEL
    return str(video_api.get("model") or DEFAULT_MODEL).strip()


def parse_scene_filter(value: str) -> set[int]:
    scenes: set[int] = set()
    for raw_part in value.split(","):
        part = raw_part.strip()
        if not part:
            continue
        scenes.add(int(part))
    return scenes


def main() -> None:
    parser = argparse.ArgumentParser(description="用青云聚合 Grok 视频生成 API 批量生成图片转视频片段")
    parser.add_argument("--jobs-csv", required=True, type=Path, help="prepare_image_video_jobs.py 生成的任务 CSV")
    parser.add_argument("--images-dir", required=True, type=Path, help="稳定命名图片文件夹")
    parser.add_argument("--videos-dir", required=True, type=Path, help="生成视频保存文件夹")
    parser.add_argument("--api-key", default=os.getenv("QINGYUN_API_KEY", ""), help="青云聚合 API Key；只从 QINGYUN_API_KEY 环境变量或本参数读取")
    parser.add_argument("--base-url", default=os.getenv("QINGYUN_BASE_URL") or saved_video_base_url(), help="API Base URL")
    parser.add_argument("--model", default=saved_video_model() or os.getenv("QINGYUN_VIDEO_MODEL", DEFAULT_MODEL), help="视频生成模型")
    parser.add_argument("--ratio", default="16:9")
    parser.add_argument("--duration", default=10.0, type=float)
    parser.add_argument("--resolution", default="720p")
    parser.add_argument("--seconds", default=os.getenv("QINGYUN_VIDEO_SECONDS", DEFAULT_SECONDS), help="青云接口固定生成秒数，Grok video 3 当前按次生成 10 秒")
    parser.add_argument("--size", default=os.getenv("QINGYUN_VIDEO_SIZE", DEFAULT_SIZE), help="视频清晰度，例如 720P 或 1080P")
    parser.add_argument("--timing-mode", choices=["frames", "duration"], default="frames", help="默认用 frames 支持小数秒")
    parser.add_argument("--parameter-style", choices=["prompt", "body"], default="prompt", help="兼容旧参数；青云接口会忽略旧的 prompt/body 参数风格")
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
        default=int(os.getenv("QINGYUN_MAX_SUBMIT_FIRST", "20")),
        type=int,
        help="批量提交模式下最多保留多少个已提交未完成任务；0 表示不限制",
    )
    parser.add_argument("--submit-delay", default=0.2, type=float, help="批量提交时每次创建任务后的短暂停顿，避免请求过快")
    parser.add_argument("--poll-existing", action="store_true", help="只轮询 CSV 中已有 task_id 的任务")
    parser.add_argument("--dry-run", action="store_true", help="只打印请求体，不调用 API")
    parser.add_argument("--extra-body-json", default="", help="额外请求体 JSON，例如 '{\"watermark\": false}'")
    args = parser.parse_args()

    rows = read_jobs_csv(args.jobs_csv.expanduser())
    args.videos_dir.expanduser().mkdir(parents=True, exist_ok=True)
    client = None if args.dry_run else QingyunVideoClient(api_key=args.api_key or "", base_url=args.base_url)
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
            duration = float(row.get("duration") or args.duration)
            frames = int(row["frames"]) if args.timing_mode == "frames" and row.get("frames", "").strip() else None
            print(f"批量创建任务 {row['scene']}：{row['image_filename']}", flush=True)
            try:
                created = client.create_task(
                    model=args.model,
                    prompt=row["prompt"],
                    image_path=image_path,
                    ratio=args.ratio,
                    duration=duration,
                    resolution=args.resolution.strip() or None,
                    frames=frames,
                    seconds=args.seconds,
                    size=args.size,
                    parameter_style=args.parameter_style,
                    camera_fixed=args.camerafixed,
                    watermark=args.watermark,
                    extra_body=extra_body,
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
            row["status"] = "downloaded"
            continue
        if status in {"downloaded", "approved"}:
            continue

        try:
            duration = float(row.get("duration") or args.duration)
            frames = int(row["frames"]) if args.timing_mode == "frames" and row.get("frames", "").strip() else None
            if args.dry_run:
                image_path = args.images_dir.expanduser() / row["image_filename"]
                body = build_create_task_body(
                    model=args.model,
                    prompt=row["prompt"],
                    image_path=image_path,
                    ratio=args.ratio,
                    duration=duration,
                    resolution=args.resolution.strip() or None,
                    frames=frames,
                    seconds=args.seconds,
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
                    created = client.create_task(
                        model=args.model,
                        prompt=row["prompt"],
                        image_path=image_path,
                        ratio=args.ratio,
                        duration=duration,
                        resolution=args.resolution.strip() or None,
                        frames=frames,
                        seconds=args.seconds,
                        size=args.size,
                        parameter_style=args.parameter_style,
                        camera_fixed=args.camerafixed,
                        watermark=args.watermark,
                        extra_body=extra_body,
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
            elif result.status in SUCCESS_STATUSES:
                print(f"下载视频 {row['scene']}：{video_path.name}", flush=True)
                client.download_task_video(task_id, video_path)
                row["status"] = "downloaded"

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
