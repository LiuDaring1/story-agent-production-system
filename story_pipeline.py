#!/usr/bin/env python3
"""Single supported control entry for Codex-native story production.

The human-facing entry remains a Codex task using ``story-full-auto``.  Codex
uses this small command only for durable state, receipts, and finalization; it
does not recreate the retired fixed-stage Story Agent scheduler.
"""

from __future__ import annotations

import json
import sys

import story_run


ARCHITECTURE = {
    "schema_version": "codex-native-story-pipeline/v2",
    "candidate_contract": "story-production/v2",
    "deterministic_entrypoints": {"asset-plan": "story_asset_efficiency.py", "prepare-review": "story_review_preparation.py", "assemble": "assemble_r2v_story.py", "backgrounds": "render_customer_backgrounds.py", "release": "release_video.py", "release-windows": "story_scene_windows.py"},
    "candidate_operations": ["preflight", "review-create", "timeline", "release-qa", "media", "media-preview", "media-approve", "pack", "materials", "packaging", "checklist", "encode-control"],
    "promotion": "candidate until a real new story passes QA and user acceptance",
    "human_entry": "Codex task + skills/story-full-auto",
    "supported_control_entry": "story_pipeline.py",
    "orchestrator": "foreground Codex task",
    "policy": ["AGENTS.md", "skills/story-full-auto", "skills/story-r2v-director"],
    "state": "99_项目状态/story_run.json",
    "execution": "small deterministic Python/JS modules and provider adapters",
    "evidence": "SHA-256-bound manifests, independent reviews, QA, and delivery receipts",
    "legacy": "historical_archive/legacy-source-20260907.tar.gz",
    "legacy_is_production_entry": False,
}


def print_help() -> None:
    print(
        "Codex 原生故事生产控制入口\n\n"
        "用户入口：在 Codex 中调用 story-full-auto。\n"
        "内部命令：\n"
        "  story_pipeline.py describe\n"
        "  story_pipeline.py init ...\n"
        "  story_pipeline.py record ...\n"
        "  story_pipeline.py observe-request ...\n"
        "  story_pipeline.py observe-performance ...\n"
        "  story_pipeline.py status ...\n"
        "  story_pipeline.py finalize ...\n\n"
        "  正式执行：assemble / backgrounds / release-windows / release（沿用对应模块参数）\n"
        "  候选操作：timeline / release-qa / media / pack / materials / packaging / checklist / encode-control\n"
        "账本命令参数与 story_run.py 相同；候选操作使用 --run-file 和 --request。"
    )


def main() -> None:
    if len(sys.argv) == 1 or sys.argv[1] in {"-h", "--help"}:
        print_help()
        return
    if sys.argv[1] == "describe":
        if len(sys.argv) != 2:
            raise SystemExit("describe 不接受其他参数")
        print(json.dumps(ARCHITECTURE, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if sys.argv[1] in ARCHITECTURE["deterministic_entrypoints"]:
        import runpy
        from pathlib import Path
        script = Path(__file__).resolve().parent / ARCHITECTURE["deterministic_entrypoints"][sys.argv[1]]
        sys.argv = [str(script), *sys.argv[2:]]
        runpy.run_path(str(script), run_name="__main__")
        return
    if sys.argv[1] in {"preflight", "review-create", "timeline", "release-qa", "media", "media-preview", "media-approve", "pack", "materials", "packaging", "checklist", "encode-control"}:
        from story_candidate_cli import main as candidate_main
        candidate_main(sys.argv[1:])
        return
    story_run.main()


if __name__ == "__main__":
    main()
