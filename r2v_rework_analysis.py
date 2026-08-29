#!/usr/bin/env python3
"""Create a reproducible root-cause analysis for one targeted R2V repair round."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON 顶层必须是对象：{path}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 R2V 定点返工根因分析")
    parser.add_argument("--initial-review", required=True, type=Path)
    parser.add_argument("--repair-manifest", required=True, type=Path)
    parser.add_argument("--selection", required=True, type=Path)
    parser.add_argument("--postfix-decisions", type=Path)
    parser.add_argument("--final-review", required=True, type=Path)
    parser.add_argument("--output-json", required=True, type=Path)
    parser.add_argument("--output-md", required=True, type=Path)
    args = parser.parse_args()

    initial_path = args.initial_review.expanduser()
    repair_path = args.repair_manifest.expanduser()
    selection_path = args.selection.expanduser()
    final_path = args.final_review.expanduser()
    initial = load_json(initial_path)
    repair = load_json(repair_path)
    selection = load_json(selection_path)
    postfix_path = args.postfix_decisions.expanduser() if args.postfix_decisions else None
    postfix = load_json(postfix_path) if postfix_path else None
    final = load_json(final_path)
    failed = [str(item.get("shot_id") or "") for item in initial.get("critical_errors", [])]
    expected = ["S02", "S06", "S11", "S16", "S18", "S23", "S24", "S25", "S26"]
    if failed != expected:
        raise ValueError(f"首轮失败镜头不符合已冻结证据：{failed}")
    if sorted(selection.get("repair_shots", [])) != sorted(expected):
        raise ValueError("有效镜头选择与首轮严重失败清单不一致")

    categories = [
        {
            "category": "asset_semantic_ambiguity",
            "shots": ["S23", "S24", "S25", "S26"],
            "evidence": "正面对称单瓣被镜像复制；中性站姿男孩诱发许愿前站起。",
            "prevention": "单剩部件使用非对称三分之四视角；禁止状态变化的角色使用同身份状态特定姿态资产。",
        },
        {
            "category": "action_capacity_and_exit_state",
            "shots": ["S02", "S11", "S16"],
            "evidence": "短时长内连续消耗/恢复/分离动作过多，模型没有完成严格数量出口或同时脱落多个部件。",
            "prevention": "6秒内四次以上可数变化改用10秒并吸收同语义停顿；只上传入口状态；为严格出口预留最后1.5–2秒稳定展示。",
        },
        {
            "category": "reuse_continuity_blind_spot",
            "shots": ["S06"],
            "evidence": "旧单镜审核只证明该镜头自身可用，没有发现它在当前S05/S07粉裙镜头之间形成T恤长裤跳装。",
            "prevention": "任何历史已通过复用镜头必须重新进入当前完整镜头组，审核身份、服装、姿态、道具状态和相邻剪辑。",
        },
        {
            "category": "severity_misclassification_and_overfix",
            "shots": ["S18"],
            "evidence": "把旁白中的七只熊误当作逐帧必须精确计数的剧情硬事实，导致一次不必要的付费返修；用户最终确认追逐与恐惧的动态表演比精确数量重要。",
            "prevention": "付费前先区分硬事实与软事实。只有数量改变剧情因果、教学答案或后续状态时才锁精确数量；背景群体允许近似和遮挡，绝不为软数量把动态视频替换成静态补片。",
        },
    ]
    initial_score = int(initial.get("score") or 0)
    final_score = int(final.get("score") or 0)
    final_critical = final.get("critical_errors") if isinstance(final.get("critical_errors"), list) else []
    total_shots = len(selection.get("selected", []))
    repair_count = len(expected)
    payload = {
        "schema_version": "story-r2v-rework-analysis-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "evidence": {
            "initial_review": {"path": str(initial_path), "sha256": sha256_path(initial_path)},
            "repair_manifest": {"path": str(repair_path), "sha256": sha256_path(repair_path)},
            "effective_selection": {"path": str(selection_path), "sha256": sha256_path(selection_path)},
            "final_review": {"path": str(final_path), "sha256": sha256_path(final_path)},
        },
        "metrics": {
            "total_story_shots": total_shots,
            "initial_new_paid_tasks": 27,
            "initial_cost_cny": 1.62,
            "initial_review_score": initial_score,
            "initial_critical_shots": repair_count,
            "targeted_repair_tasks": repair_count,
            "targeted_repair_cost_cny": 0.54,
            "total_r2v_cost_cny": 2.16,
            "repair_share_of_story_shots": round(repair_count / total_shots, 4) if total_shots else None,
            "final_review_score": final_score,
            "final_approved": bool(final.get("approved")),
            "final_critical_error_count": len(final_critical),
            "third_paid_round_used": False,
            "local_video_only_postfix_count": len((postfix or {}).get("decisions", [])),
            "soft_fact_reclassified_shots": ["S18"],
        },
        "root_causes": categories,
        "repair_policy": {
            "unchanged_shots_reused": total_shots - repair_count,
            "failed_shots_repaired_once": repair_count,
            "original_versions_preserved": True,
            "provider_receipts_preserved": True,
            "input_and_output_sha256_preserved": True,
            "further_paid_redraws": "forbidden_without_new_user_authorization",
            "remaining_minor_issues": "prefer best existing take, editorial avoidance, retiming, crop, or deterministic local post-production",
            "motion_media_priority": "do not replace acceptable moving footage with a static image merely to fix a soft count fact",
        },
        "rules_written_back": [
            "skills/story-r2v-director/references/asset-contract.md",
            "skills/story-r2v-director/references/stateful-props.md",
            "skills/story-r2v-director/references/prompt-review.md",
            "skills/story-r2v-director/references/shot-continuity.md",
        ],
    }
    if postfix_path and postfix is not None:
        payload["evidence"]["local_postfix_decisions"] = {
            "path": str(postfix_path),
            "sha256": sha256_path(postfix_path),
        }
    output_json = args.output_json.expanduser()
    output_md = args.output_md.expanduser()
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# R2V 定点返工根因分析",
        "",
        f"- 首轮整组审核：{initial_score} 分，{repair_count} 个严重镜头。",
        f"- 定点修复：{repair_count} 个镜头，各一次；其余 {total_shots - repair_count} 个镜头直接复用。",
        f"- R2V 成本：首轮 ¥1.62，定点修复 ¥0.54，合计 ¥2.16。",
        f"- 最终整组审核：{final_score} 分，approved={str(bool(final.get('approved'))).lower()}，关键错误 {len(final_critical)} 个。",
        "- 未启动第三轮付费抽卡。",
        f"- 本地后期：{len((postfix or {}).get('decisions', []))} 个镜头，全部只用已有动态视频剪接或裁切。",
        "- S18 复盘结论：熊群精确数量属于软事实，保留动态原视频，不插静态补片。",
        "",
        "## 根因与预防",
        "",
    ]
    for item in categories:
        lines.extend([
            f"### {item['category']}",
            "",
            f"- 镜头：{', '.join(item['shots'])}",
            f"- 证据：{item['evidence']}",
            f"- 预防：{item['prevention']}",
            "",
        ])
    lines.extend([
        "## 后续成本纪律",
        "",
        "每个严重失败镜头最多一次定点重做。达到上限后不再抽卡；普通审美瑕疵和可规避问题使用最佳已有版本、剪辑、变速、裁切或本地确定性后期解决。所有失败版本、回执、提示词修改和 SHA-256 继续保留。",
        "",
    ])
    output_md.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({
        "json": str(output_json),
        "json_sha256": sha256_path(output_json),
        "markdown": str(output_md),
        "markdown_sha256": sha256_path(output_md),
        "final_approved": payload["metrics"]["final_approved"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
