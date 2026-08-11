from __future__ import annotations

import argparse
from pathlib import Path

from story_video_synthesizer.image_video import (
    VisualContinuityContractError,
    build_jobs,
    discover_visual_continuity_paths,
    write_job_outputs,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="准备图片转视频 API 任务清单和预览页")
    parser.add_argument("--image-dir", required=True, type=Path, help="按序号命名的分镜图片文件夹")
    parser.add_argument("--storyboard", required=True, type=Path, help="分镜脚本，支持 .docx/.txt/.md")
    parser.add_argument("--output-dir", required=True, type=Path, help="输出任务文件夹")
    parser.add_argument("--slug", default="story", help="故事 slug，用于文件命名")
    parser.add_argument("--short-slug", default="hlbyg", help="视频片段短名，例如 01_hlbyg.mp4")
    parser.add_argument(
        "--continuity-contract",
        type=Path,
        default=None,
        help="可选视觉连续性合同 JSON；存在时逐镜状态硬约束会注入最终提示词",
    )
    parser.add_argument(
        "--storyboard-plan",
        type=Path,
        default=None,
        help="可选机器可读 storyboard_plan.json；合同存在时必填",
    )
    args = parser.parse_args()

    discovered_contract, discovered_plan = discover_visual_continuity_paths(
        args.image_dir.expanduser(),
        args.storyboard.expanduser(),
        args.output_dir.expanduser(),
        args.slug,
    )
    continuity_contract = args.continuity_contract.expanduser() if args.continuity_contract else discovered_contract
    storyboard_plan = args.storyboard_plan.expanduser() if args.storyboard_plan else discovered_plan

    try:
        jobs, warnings = build_jobs(
            image_dir=args.image_dir.expanduser(),
            storyboard_path=args.storyboard.expanduser(),
            output_dir=args.output_dir.expanduser(),
            slug=args.slug,
            short_slug=args.short_slug,
            continuity_contract_path=continuity_contract,
            storyboard_plan_path=storyboard_plan,
        )
    except VisualContinuityContractError as exc:
        raise SystemExit(f"视觉连续性合同校验失败，未生成/付费：{exc}") from exc
    outputs = write_job_outputs(jobs, args.output_dir.expanduser(), args.slug)

    print(f"已生成 {len(jobs)} 个图片转视频任务。")
    for warning in warnings:
        print(f"提醒：{warning}")
    print(f"图片副本：{outputs['images_dir']}")
    print(f"视频目标文件夹：{outputs['videos_dir']}")
    print(f"任务清单：{outputs['manifest_csv']}")
    print(f"提示词 Markdown：{outputs['prompts_md']}")
    print(f"提示词 CSV：{outputs['prompts_csv']}")
    print(f"片段命名表：{outputs['clip_names_csv']}")
    print(f"预览页：{outputs['review_html']}")
    print("下一步：请打开预览页检查/修改每条图生视频提示词，全部确认后点击“导出提示词确认CSV”。未确认前不会调用图生视频 API。")


if __name__ == "__main__":
    main()
