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

`story_pipeline.py` / `story_run.py` 只保存六个工作包、轻量请求事实、当前产物和 SHA-256，不调度固定阶段，也不管理预算、换算、入账或成本门禁。创意判断、结果观察、异常处置和工作包汇合由当前 Codex 任务负责。

## 快速观察

```bash
python3 story_pipeline.py describe
python3 story_pipeline.py status --run-file /path/to/project/99_项目状态/story_run.json
```

## 退役系统

旧 Agent、固定阶段 Runtime、工作台、恢复工具、动态 PPT 实验和根目录别名已移入[可校验独立源码归档](historical_archive/README.md)，包含清理时的未提交版本。当前生产不再导入旧系统。

## 修改验证

```bash
python3 -m unittest discover -s tests -v
python3 story_pipeline.py --help
python3 story_pipeline.py describe
```

修改 `story-full-auto` 后还必须运行 Skill 校验。详细工程约束见 [`AGENTS.md`](AGENTS.md)。
# 本机配置

`pipeline_config.json` 保存通用配置。可在同目录的 `pipeline_config.local.json` 覆盖本机品牌素材和外部工具路径；此文件已被 Git 忽略，读取配置时按字段合并，不应提交。
