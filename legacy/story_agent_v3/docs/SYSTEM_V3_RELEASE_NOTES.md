# 已归档：Story Agent V3.6.1 Canary

> 本文属于已退役固定阶段 Story Agent，不是现行生产版本说明。

## 版本定位

- 当前产品版本：`3.6.1-canary`。
- 本轮修复分支：`story-agent-v36-qisehua-feedback-20260825`；起点：`d584f37`。
- 当前默认模型路由：`gpt-5.6-sol` + `medium` 作为指挥官；`gpt-5.6-luna` + `high` 作为执行工人。
- 默认运行时限：10 小时；到时冻结最佳哈希有效版本，不再新增无边界审美返工。
- 历史标签 `system-v3-luna-max-candidate-20260809` 与 `system-v3-pre-refactor-head-20260809` 只用于回看旧基线，不代表当前生产配置。

## 候选版范围

- 统一故事文本语义合同、连续场景、角色知情状态、关键动作和可消耗道具状态机。
- DAG 并行执行及两档模型路由。
- RVM 完整透明画布、抠像证据、人物初始锚点和 PPT/WPS 单行描边字幕。
- 发布底板、A 框、人物尾帧和最后两秒完整性检查。
- 六张封面固定 Logo 的确定性后处理与哈希凭据。
- 视觉审核强制逐文件、逐时间点证据矩阵。

## 模型对照实验

保持代码不变，仅覆盖执行模型：

```bash
python3 story_agent.py start \
  --job <JOB_ID> \
  --codex-worker-model gpt-5.6-terra \
  --codex-worker-reasoning-effort medium
```

不传覆盖参数时使用 Luna High；Sol Medium 指挥官和独立审核口径保持不变。

## 验证状态

- 验证数字以当前提交的完整测试结果为准，不再沿用旧版 133/133 口径。
- Python 编译、JSON 配置、CLI 帮助、Skill 校验和 `git diff --check` 都是提交门禁。
- 尚未把下一条真实故事的商业生产效果计入“稳定版”结论；当前应视为候选版。

## 安全回退

- 查看旧代码而不改动当前候选版：从 `system-v3-pre-refactor-head-20260809` 创建新分支。
- 恢复升级前未提交工作区：使用 `version_backups/StoryAgentNext_baseline_20260809/working_tree.patch` 或 `working_tree_files.tar.gz`。
- 不直接对含用户产物的工作区执行破坏性 reset。
