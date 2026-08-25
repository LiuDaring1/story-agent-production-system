# Story Agent v3.6.2-canary.8（2026-08-25）

## 目标

把已通过合同和视觉小样后的正式故事出图改成轻量快速路径，不改原文，不重跑上游，不启动视频、音乐或包装分支。

## 改动

- 新增 `frontend_handoff` / `codex_cli` 共用的精简导演 brief/result 协议。
- 模型只提交影响画面的镜头字段；Runtime 继承未变化状态并编译原有完整 storyboard plan，旧下游格式保持不变。
- 前台结果和图片分别通过受控 ingest 接收，校验 brief/决策/图片尺寸/SHA-256，且不会人工伪造 stage passed。
- 每次导演批次限制为 3–5 镜；控制提示低于 10 KB。
- 嵌套 CLI 遇到额度、账号或写入权限阻塞时立即进入 `handoff_required`，不自动重复消耗。
- 控制计划 CLI 从故事项目目录启动，工程目录仅作为附加只读/可访问根，避免向项目写结果时被 workspace-write 拒绝。

## 验证

- `python3 -m unittest discover -s tests -v`：568/568 通过。
- `story_agent.py --help`、`run --help`、两个 ingest 子命令 `--help` 通过。
- 前台模式测试证明不会启动嵌套 Codex；图片 ingest 回执明确记录 `stage_marked_passed=false` 与 `downstream_started=false`。
