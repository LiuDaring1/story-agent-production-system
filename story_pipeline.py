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
    "schema_version": "codex-native-story-pipeline/v1",
    "human_entry": "Codex task + skills/story-full-auto",
    "supported_control_entry": "story_pipeline.py",
    "orchestrator": "foreground Codex task",
    "policy": ["AGENTS.md", "skills/story-full-auto", "skills/story-r2v-director"],
    "state": "99_项目状态/story_run.json",
    "execution": "small deterministic Python/JS modules and provider adapters",
    "evidence": "SHA-256-bound manifests, independent reviews, QA, and delivery receipts",
    "legacy": "legacy/story_agent_v3",
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
        "  story_pipeline.py status ...\n"
        "  story_pipeline.py finalize ...\n\n"
        "init/record/status/finalize 参数与 story_run.py 相同。"
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
    story_run.main()


if __name__ == "__main__":
    main()
