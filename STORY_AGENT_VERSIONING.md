# Story Agent 版本与回退

## 已保留的稳定版

- 稳定提交：`b43705b` (`snapshot: preserve review2 stable story agent`)
- 稳定分支：`codex/system-v2-review2-stable`
- 稳定标签：`system-v2-review2-stable-20260717`

这个版本保留了“大象和蚂蚁”与“小老虎当评委”两轮人工审核后的线性 Agent。不要改写或移动该标签。

## 当前开发版

- 分支：`codex/system-v3-parallel-orchestration`
- 新能力：prepared 加速入口、DAG 并行调度、单写协调器、worker shadow manifest、run epoch 取消隔离、分支级阻塞。
- 兼容性：`run` 默认仍为 `linear`；`start` 默认为 `dag`；旧 CLI 与旧 manifest 可继续读取。

## 安全回退

先保留当前任务产物和 manifest，再基于稳定标签创建新的回退分支：

```bash
git switch -c codex/rollback-system-v2 system-v2-review2-stable-20260717
```

不对已有项目目录执行删除或覆盖；新旧运行时的切换应通过 Git 分支完成。
