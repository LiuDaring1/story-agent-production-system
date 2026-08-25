# Story Agent v3.6.2-canary.9（2026-08-25）

## 修复

- `story_agent.py run --project-dir ...` 现在与 `--job` 一样，优先复用项目 manifest 中已有的故事名和 slug。
- 防止续跑时把历史短 slug 重新推导为标题 slug，进而造成可信来源链漂移、已审核合同被误归档并回到上游。
- DAG worker 显式继承精简图片导演执行器，避免前台快速模式在子进程中退回嵌套 CLI。

## 验证

- 新增 project-dir 保留 manifest story context 的回归测试。
- 完整测试与 CLI 帮助验证通过后才允许迁移真实短测。
