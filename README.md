# Codex 原生儿童故事生产系统

## 唯一生产入口

在当前 Codex 前台任务中提供用户确认的故事文本和已调色横屏绿幕视频，并使用 [`story-full-auto`](skills/story-full-auto/SKILL.md)。

- 人类入口：当前 Codex 任务。
- 生产策略：`skills/story-full-auto/SKILL.md`。
- R2V 导演策略：`skills/story-r2v-director/SKILL.md`。
- 内部状态入口：`story_pipeline.py`。
- 项目事实源：`99_项目状态/story_run.json`。
- 当前架构：[`docs/architecture/codex-native-story-pipeline.md`](docs/architecture/codex-native-story-pipeline.md)。
- 逐步执行链：[`docs/architecture/execution-chain.md`](docs/architecture/execution-chain.md)。
- AI 阅读入口：[`AI_CONTEXT.md`](AI_CONTEXT.md)。

`story_pipeline.py` / `story_run.py` 只保存六个工作包、成本、当前产物和 SHA-256，不调度固定阶段。创意判断、结果观察、异常处置和工作包汇合由当前 Codex 任务负责。

## 快速观察

```bash
python3 story_pipeline.py describe
python3 story_pipeline.py status --run-file /path/to/project/99_项目状态/story_run.json
```

## 退役系统

旧 `story_agent.py`、38 阶段 Runtime、Supervisor、Dashboard、Recovery、工作台操作手册和动态 PPT 实验已归档到 [`legacy/story_agent_v3/`](legacy/story_agent_v3/LEGACY.md)。根目录兼容文件只用于旧项目审计和回归测试，不得用于新故事。

## 修改验证

```bash
python3 -m unittest discover -s tests -v
python3 story_pipeline.py --help
python3 story_pipeline.py describe
```

修改 `story-full-auto` 后还必须运行 Skill 校验。详细工程约束见 [`AGENTS.md`](AGENTS.md)。
