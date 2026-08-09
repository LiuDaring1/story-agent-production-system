# Story Agent System V3 候选版

## 版本定位

- 升级前 tracked HEAD：`d1135cc`
- 升级前完整工作区备份：`version_backups/StoryAgentNext_baseline_20260809/`
- 当前默认模型路由：`gpt-5.6-sol` + `xhigh` 作为指挥官；`gpt-5.6-luna` + `max` 作为执行工人。
- 当前候选标签：`system-v3-luna-max-candidate-20260809`
- 升级前代码标签：`system-v3-pre-refactor-head-20260809`

## 候选版范围

- 统一故事文本语义合同与客户产物选择策略。
- DAG 并行执行及两档模型路由。
- 示范视频原生构图、抠像证据、PPT 字幕约束。
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

不传覆盖参数时使用 Luna Max。Sol 指挥官和审核口径在两种运行中保持不变。

## 验证状态

- 自动化测试：133/133 通过。
- Python 编译、JSON 配置、CLI 帮助入口和 `git diff --check` 通过。
- 尚未把下一条真实故事的商业生产效果计入“稳定版”结论；当前应视为候选版。

## 安全回退

- 查看旧代码而不改动当前候选版：从 `system-v3-pre-refactor-head-20260809` 创建新分支。
- 恢复升级前未提交工作区：使用 `version_backups/StoryAgentNext_baseline_20260809/working_tree.patch` 或 `working_tree_files.tar.gz`。
- 不直接对含用户产物的工作区执行破坏性 reset。
