#!/usr/bin/env python3
"""Build a human-readable review page for preserved R2V rejections."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote


ATTEMPT_RE = re.compile(r"(?:scene_\d+_|[A-Za-z]\d+_)?attempt_(\d+)$", re.IGNORECASE)


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_reasons(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("reasons JSON 顶层必须是对象")
    if isinstance(payload.get("attempts"), list):
        payload = {
            f"{item.get('shot_id')}:{int(item.get('attempt') or 0)}": item
            for item in payload["attempts"]
            if isinstance(item, dict) and item.get("shot_id")
        }
    result: dict[str, dict[str, str]] = {}
    for key, value in payload.items():
        if isinstance(value, str):
            result[str(key)] = {"reason": value, "evidence_source": "人工记录"}
        elif isinstance(value, dict):
            result[str(key)] = {
                "reason": str(value.get("reason") or "").strip(),
                "evidence_source": str(value.get("evidence_source") or "人工记录").strip(),
                "user_feedback": str(value.get("user_feedback") or "").strip(),
                "review_outcome": str(value.get("review_outcome") or "").strip(),
            }
    return result


def relative_url(path: Path, output_dir: Path) -> str:
    return quote(Path(os.path.relpath(path, output_dir)).as_posix())


def read_jobs(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def collect_attempts(
    *,
    project_dir: Path,
    jobs_csv: Path,
    rejected_dir: Path,
    reasons: dict[str, dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = read_jobs(jobs_csv)
    jobs_by_shot = {str(row.get("shot_id") or f"S{int(row['scene']):02d}"): row for row in rows}
    attempts: list[dict[str, Any]] = []
    preserved_by_shot: dict[str, set[int]] = {}
    for video in sorted(rejected_dir.rglob("*.mp4")):
        shot_id = video.parent.name
        match = ATTEMPT_RE.search(video.stem)
        if match is None:
            raise ValueError(f"无法从文件名解析 attempt：{video}")
        attempt = int(match.group(1))
        key = f"{shot_id}:{attempt}"
        reason = reasons.get(key, {})
        receipt_candidates = [
            video.with_suffix(".json"),
            video.parent / f"scene_{int(shot_id[1:]):02d}_attempt_{attempt:02d}.json",
        ]
        receipt = next((candidate for candidate in receipt_candidates if candidate.is_file()), None)
        receipt_payload: dict[str, Any] = {}
        if receipt is not None:
            try:
                loaded = json.loads(receipt.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    receipt_payload = loaded
            except (OSError, UnicodeError, json.JSONDecodeError):
                receipt_payload = {}
        job = jobs_by_shot.get(shot_id, {})
        attempts.append({
            "shot_id": shot_id,
            "attempt": attempt,
            "video_path": str(video),
            "video_sha256": sha256_path(video),
            "receipt_path": str(receipt) if receipt else None,
            "reason": reason.get("reason") or "没有找到可核验的逐版退件理由；请人工复核后补录。",
            "evidence_source": reason.get("evidence_source") or "缺失",
            "user_feedback": reason.get("user_feedback") or "",
            "review_outcome": reason.get("review_outcome") or "",
            "story_text": str(job.get("story_text") or "").strip(),
            "provider_prompt_chars": receipt_payload.get("provider_prompt_chars"),
            "accepted_path": str(project_dir / "02_背景动画" / "正文R2V" / f"{shot_id}.mp4"),
        })
        preserved_by_shot.setdefault(shot_id, set()).add(attempt)
    missing: list[dict[str, Any]] = []
    for shot_id, row in sorted(jobs_by_shot.items()):
        final_attempt = int(row.get("provider_attempt") or 0)
        preserved = preserved_by_shot.get(shot_id, set())
        for attempt in range(final_attempt):
            if attempt not in preserved:
                missing.append({
                    "shot_id": shot_id,
                    "attempt": attempt,
                    "status": "attempt counter exists, but no rejected video was preserved",
                })
    return attempts, missing


def write_report(
    *,
    project_dir: Path,
    output_dir: Path,
    attempts: list[dict[str, Any]],
    missing: list[dict[str, Any]],
) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "rejection_review.json"
    md_path = output_dir / "退件镜头审片清单.md"
    html_path = output_dir / "退件镜头审片页.html"
    shots = sorted({item["shot_id"] for item in attempts})
    payload = {
        "schema_version": "story-r2v-rejection-review/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_dir": str(project_dir),
        "playable_rejected_video_count": len(attempts),
        "affected_shot_count": len(shots),
        "missing_attempt_evidence_count": len(missing),
        "attempts": attempts,
        "missing_attempt_evidence": missing,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    md_lines = [
        "# R2V 退件镜头审片审计导出",
        "",
        f"- 可播放退件：{len(attempts)} 个",
        f"- 涉及镜头：{len(shots)} 个",
        f"- 有计数但缺少可播放退件证据：{len(missing)} 个",
        "- 说明：这是网页审片页的纯文字审计导出，不是第二套审片入口；视频请在 HTML 网页中播放。",
        "",
    ]
    for item in attempts:
        video = Path(item["video_path"])
        accepted = Path(item["accepted_path"])
        md_lines.extend([
            f"## {item['shot_id']} / attempt {item['attempt']:02d}",
            "",
            f"- [播放退件视频]({video})",
            f"- [播放最终采用版]({accepted})",
            f"- 对应原文：{item['story_text'] or '缺失'}",
            f"- 实际投喂长度：{item['provider_prompt_chars'] if item['provider_prompt_chars'] is not None else '缺失'} 字符",
            f"- 退件理由：{item['reason']}",
            f"- 理由证据：{item['evidence_source']}",
            *([f"- 用户复核：{item['user_feedback']}"] if item.get("user_feedback") else []),
            *([f"- 复核结论：{item['review_outcome']}"] if item.get("review_outcome") else []),
            f"- 退件 SHA-256：`{item['video_sha256']}`",
            "",
        ])
    if missing:
        md_lines.extend(["## 缺失证据", ""])
        for item in missing:
            md_lines.append(f"- {item['shot_id']} / attempt {item['attempt']:02d}：{item['status']}")
        md_lines.append("")
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    cards = []
    for item in attempts:
        rejected_url = relative_url(Path(item["video_path"]), output_dir)
        accepted_url = relative_url(Path(item["accepted_path"]), output_dir)
        feedback_html = (
            f"<p class=\"feedback\"><strong>用户复核：</strong>{html.escape(item['user_feedback'])}</p>"
            if item.get("user_feedback") else ""
        )
        outcome_html = (
            f"<p class=\"outcome\"><strong>复核结论：</strong>{html.escape(item['review_outcome'])}</p>"
            if item.get("review_outcome") else ""
        )
        cards.append(f"""
<section class="card">
  <h2>{html.escape(item['shot_id'])} / attempt {item['attempt']:02d}</h2>
  <p><strong>对应原文：</strong>{html.escape(item['story_text'] or '缺失')}</p>
  <p><strong>实际投喂长度：</strong>{html.escape(str(item['provider_prompt_chars'])) if item['provider_prompt_chars'] is not None else '缺失'} 字符</p>
  <p><strong>退件理由：</strong>{html.escape(item['reason'])}</p>
  <p><strong>理由证据：</strong>{html.escape(item['evidence_source'])}</p>
  {feedback_html}
  {outcome_html}
  <div class="videos">
    <figure><figcaption>退件</figcaption><video controls preload="metadata" src="{rejected_url}"></video></figure>
    <figure><figcaption>最终采用版</figcaption><video controls preload="metadata" src="{accepted_url}"></video></figure>
  </div>
</section>""")
    missing_html = "".join(
        f"<li>{html.escape(item['shot_id'])} / attempt {item['attempt']:02d}：{html.escape(item['status'])}</li>"
        for item in missing
    )
    html_path.write_text(f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>R2V 退件镜头审片页</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;margin:24px;background:#f5f3ee;color:#222}}
.summary,.card{{max-width:1400px;margin:0 auto 24px;background:white;border-radius:14px;padding:20px;box-shadow:0 2px 12px #0001}}
.feedback{{background:#fff5d6;border-left:4px solid #d89a00;padding:10px 12px}} .outcome{{background:#eaf6ed;border-left:4px solid #2f8a48;padding:10px 12px}}
.videos{{display:grid;grid-template-columns:1fr 1fr;gap:18px}} video{{width:100%;background:#111}} figcaption{{font-weight:700;margin-bottom:8px}}
@media(max-width:900px){{.videos{{grid-template-columns:1fr}}}}
</style></head><body>
<section class="summary"><h1>R2V 退件镜头审片页</h1>
<p>可播放退件 {len(attempts)} 个，涉及 {len(shots)} 个镜头；缺少可播放证据 {len(missing)} 个。</p>
<p>左右并排比较退件与最终采用版。理由区分原始记录和事后视觉复核，不补造缺失证据。</p>
<p>每个镜头同时显示对应故事原文与实际 provider 提示词长度；用户复核会单独标注，不再把原审核意见冒充最终结论。</p>
<h2>缺失证据</h2><ul>{missing_html or '<li>无</li>'}</ul></section>
{''.join(cards)}
</body></html>""", encoding="utf-8")
    return json_path, md_path, html_path


def main() -> int:
    parser = argparse.ArgumentParser(description="生成可播放的 R2V 退件审片清单")
    parser.add_argument("--project-dir", required=True, type=Path)
    parser.add_argument("--jobs-csv", type=Path)
    parser.add_argument("--rejected-dir", type=Path)
    parser.add_argument("--reasons-json", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    project_dir = args.project_dir.expanduser().resolve()
    status_dir = project_dir / "99_项目状态"
    jobs_csv = (args.jobs_csv or status_dir / "r2v" / "r2v_jobs.csv").expanduser().resolve()
    rejected_dir = (args.rejected_dir or status_dir / "rejected_r2v").expanduser().resolve()
    output_dir = (args.output_dir or status_dir / "r2v_rejection_review").expanduser().resolve()
    reasons = load_reasons(args.reasons_json.expanduser().resolve() if args.reasons_json else None)
    attempts, missing = collect_attempts(
        project_dir=project_dir,
        jobs_csv=jobs_csv,
        rejected_dir=rejected_dir,
        reasons=reasons,
    )
    outputs = write_report(
        project_dir=project_dir,
        output_dir=output_dir,
        attempts=attempts,
        missing=missing,
    )
    print(json.dumps({"json": str(outputs[0]), "markdown": str(outputs[1]), "html": str(outputs[2])}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
