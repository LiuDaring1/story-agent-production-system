from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from secret_store import read_secret
from story_video_synthesizer.toapis_video import ToAPIsVideoClient
from story_video_synthesizer.volcengine_video import SUCCESS_STATUSES, TERMINAL_STATUSES
from video_provider_adapter import resolve_video_provider


ROOT = Path(__file__).resolve().parent


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层必须是对象：{path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="一次提交、无审美重试的 R2V 小样执行器")
    parser.add_argument("--spec", required=True, type=Path, help="R2V 小样 JSON")
    parser.add_argument("--config", type=Path, default=ROOT / "pipeline_config.json")
    parser.add_argument("--provider", default="", help="可选 provider 覆盖")
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--max-wait-seconds", type=float, default=1800.0)
    args = parser.parse_args()

    spec = load_json(args.spec.expanduser())
    config = load_json(args.config.expanduser())
    adapter = resolve_video_provider(config, ROOT, args.provider or str(spec.get("provider") or ""))
    api_key = read_secret(adapter.api_key_env)
    if not api_key:
        raise RuntimeError(f"缺少 {adapter.api_key_env}，请配置环境变量或 macOS Keychain。")
    client = ToAPIsVideoClient(api_key, base_url=adapter.base_url)

    output_dir_value = str(spec.get("output_dir") or "").strip()
    if not output_dir_value:
        raise ValueError("spec 缺少 output_dir")
    output_dir = Path(output_dir_value).expanduser()
    receipt_path = output_dir / "r2v_canary_receipt.json"
    receipt = load_json(receipt_path) if receipt_path.is_file() else {
        "schema_version": "story-r2v-canary-receipt-v1",
        "provider": adapter.name,
        "model": adapter.model,
        "shots": [],
    }
    saved = {str(item.get("id")): item for item in receipt.get("shots", []) if isinstance(item, dict)}

    shots = spec.get("shots")
    if not isinstance(shots, list) or not shots:
        raise ValueError("spec.shots 必须是非空数组")

    for shot in shots:
        if not isinstance(shot, dict):
            raise ValueError("spec.shots 每项必须是对象")
        shot_id = str(shot.get("id") or "").strip()
        if not shot_id:
            raise ValueError("每个 R2V 镜头都必须有稳定 id")
        item = saved.setdefault(shot_id, {"id": shot_id})
        references = [Path(str(value)).expanduser() for value in shot.get("references", [])]
        if not references or any(not path.is_file() for path in references):
            raise FileNotFoundError(f"镜头 {shot_id} 存在缺失参考图：{references}")
        item.update({
            "filename": str(shot.get("filename") or f"{shot_id}.mp4"),
            "source_start": float(shot.get("source_start", 0.0)),
            "source_end": float(shot.get("source_end", 0.0)),
            "text": str(shot.get("text") or ""),
            "prompt": str(shot.get("prompt") or ""),
            "references": [str(path) for path in references],
            "reference_sha256": [file_sha256(path) for path in references],
            "generation_seconds": int(shot.get("seconds") or adapter.default_seconds or 6),
            "attempt": 1,
        })
        if not item.get("task_id"):
            print(f"提交 {shot_id}（唯一一次生成）", flush=True)
            created = client.create_reference_task(
                model=adapter.model,
                prompt=item["prompt"],
                reference_paths=references,
                ratio=str(spec.get("ratio") or adapter.default_ratio or "16:9"),
                seconds=item["generation_seconds"],
                resolution=str(spec.get("resolution") or adapter.default_resolution or "720p"),
                extra_body={"client_business_id": shot_id},
            )
            item["task_id"] = created.task_id
            item["status"] = str(created.raw.get("status") or "submitted")
            item["create_response"] = created.raw
            receipt["shots"] = list(saved.values())
            write_json(receipt_path, receipt)

    deadline = time.monotonic() + args.max_wait_seconds
    pending = {shot_id for shot_id, item in saved.items() if str(item.get("status", "")).lower() not in TERMINAL_STATUSES and str(item.get("status", "")).lower() != "downloaded"}
    while pending and time.monotonic() < deadline:
        for shot_id in list(pending):
            item = saved[shot_id]
            result = client.get_task(str(item["task_id"]))
            status = result.status.strip().lower()
            item["status"] = status
            item["query_response"] = result.raw
            item["video_url"] = result.video_url or ""
            item["error"] = result.error or ""
            print(f"{shot_id}: {status}", flush=True)
            if status in TERMINAL_STATUSES:
                pending.remove(shot_id)
                if status in SUCCESS_STATUSES:
                    output_path = output_dir / str(item["filename"])
                    if not output_path.is_file():
                        if result.video_url:
                            client.download(result.video_url, output_path)
                        else:
                            client.download_task_video(str(item["task_id"]), output_path)
                    item["status"] = "downloaded"
                    item["output_path"] = str(output_path)
                    item["output_sha256"] = file_sha256(output_path)
            receipt["shots"] = list(saved.values())
            write_json(receipt_path, receipt)
        if pending:
            time.sleep(max(1.0, args.poll_interval))

    if pending:
        receipt["timed_out_shots"] = sorted(pending)
        write_json(receipt_path, receipt)
        raise TimeoutError(f"等待 R2V 任务超时：{sorted(pending)}")
    failed = [shot_id for shot_id, item in saved.items() if item.get("status") != "downloaded"]
    if failed:
        raise RuntimeError(f"R2V 小样存在失败镜头，不自动重试：{failed}")
    print(f"R2V 小样已下载：{output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
