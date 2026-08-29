# Legacy Story Agent v3

这一目录保存已经退役的固定阶段后台系统，作为工程教训、历史项目审计和迁移回归依据。它不是当前生产入口。

归档范围：

- `story_agent.py`：旧命令入口和阶段执行器。
- `story_agent_runtime.py`：38 阶段 DAG、预算、控制和旧 manifest。
- `story_agent_supervisor.py`：后台进程与心跳。
- `story_agent_dashboard.py`：只读工作台。
- `story_agent_observability.py`：旧事件和通知。
- `story_agent_preflight.py`：旧启动检查。
- `story_agent_recovery.py`：旧恢复分类与修复。
- `story_codex_tasks.py`：旧阶段式 Codex 提示构建器。

保留原则：

- 不再接受新产品需求或质量规则。
- 不得由 `story-full-auto` 或 `story_pipeline.py` 调用。
- 根目录兼容模块只为历史导入和回归测试存在。
- 需要复用的通用能力必须先提取到根目录中性模块；新代码禁止新增对本目录的依赖。
- 删除本目录只能在历史项目迁移、兼容导入和审计快照均有替代后另行执行。
