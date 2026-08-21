# 给 DeepSeek Harness 的首次任务

请先完整读取根目录 `AGENTS.md`、`docs/migration/DSH_MIGRATION_HANDOFF_V35_20260821.md`、`AI_CONTEXT.md`、`docs/architecture/overall-architecture.md`、`docs/architecture/stage-dag.md`、`docs/architecture/module-ports.md`、`docs/contracts/review-and-safety.md` 和 `docs/operations/runbook.md`。

这是 Story Agent V3.5 从 Codex 分出的独立迁移副本。目标不是重写整个 Python 项目，而是保留 `story_agent.py` 持久状态机、38 阶段 DAG、10 个 Port、Story Production Contract、SHA-256 独立审核、预算/心跳/取消/恢复和全部 QA 语义，在边界处完成 DSH 适配：

1. 先只读核验 package manifest、源码版本、目录隔离和离线测试基线。
2. 盘点 handoff 所列 Codex 耦合点，给出“实际代码位置 → 中性 contract → DSH adapter”的逐项映射；发现 handoff 与代码不一致时，以代码和测试为准并记录差异。
3. 在不删除旧 CLI、不启动真实 supervisor、不调用任何 provider 的前提下，完成迁移 Phase 1：provider-neutral cognitive task executor contract、deterministic fake、DSH adapter shell/SDK boundary 和离线测试。DSH adapter 在无授权/无配置时必须 fail closed，不能回落到真实调用。
4. 为后续 Phase 2 准备但不要执行：`deepseek-v4-flash` 工人、`deepseek-v4-pro` 指挥官、`deepseek-v4-flash-vision-exp` 独立视觉审核，以及 ToAPIs `gpt-image-2` 图片 adapter。
5. 运行相关测试和完整 `python3 -m unittest discover -s tests -v`；修改 CLI 时运行对应 `--help`。
6. 最终报告应明确：修改文件、保持的不变量、测试结果、剩余阻断、下一次最小 provider 探针计划，并明确写出 `provider_calls_made=false`、`canary_started=false`。

禁止事项：不要访问原 Codex worktree或桌面真实故事项目；不要读取/索要/回显 API key；不要登录、付费、调用 DeepSeek/ToAPIs/Grok/Suno/FMP；不要把文件存在或模型自述当成 QA；不要用 manifest 手改绕过审核。
