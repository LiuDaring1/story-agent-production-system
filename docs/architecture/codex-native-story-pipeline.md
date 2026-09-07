# Codex 原生故事生产架构

## 唯一生产入口

正常生产只从一个当前 Codex 任务进入，并由 `skills/story-full-auto/SKILL.md` 提供生产策略。`story_pipeline.py` 是 Codex 使用的轻量状态控制命令，不是另一个后台 Agent，也不包含固定阶段调度器。

```text
用户
  → Codex 前台任务（唯一指挥官）
    → story-full-auto / 专业 Skill（策略与路由）
      → story_pipeline.py / story_run.json（状态与当前有效产物）
        → 确定性模块与供应商适配器（执行）
          → 独立审核、QA、manifest、SHA-256 回执（事实）
            → 最终交付
```

## 各层职责

需要逐步查看“输入交给谁、模块返回什么、审核如何回流、证据落在哪里”时，读取 [`execution-chain.md`](execution-chain.md)。Schema 只做被动校验，不是调度器或执行者。

| 层 | 负责 | 不负责 |
|---|---|---|
| Codex 前台 | 导演判断、任务分解、结果观察、异常处置、汇合 | 把模型自述当作完成证据 |
| Skill | 生产策略、质量边界、模块路由、停止条件 | 持久进程、供应商调用、文件状态 |
| `story_pipeline.py` / `story_run.py` | 六个工作包、当前产物与依赖哈希、请求恢复事实和最终封口 | 创意判断、固定 DAG、自动重试、金额管理 |
| 确定性模块 | 媒体处理、计划编译、API 适配、PPT、包装、机器 QA | 自行改变导演意图或绕过审核 |
| 回执与审核 | 证明输入、产物、审核和交付仍是同一版本 | 因文件存在而推定成功 |

## 权威数据流

```text
确认文本 + 已调色绿幕视频
  → 权威音频时间轴 + 导演镜头
  → 已审核人物/状态道具/场景资产
  → 每个 shot_id 一张封存故事板
      ├─→ R2V 最后一张语义参考（不是首帧）
      └─→ 静态 PPT 正文主图
  → 正文视频 / 音乐 / RVM / 发布美术 / 产品资料并行汇合
  → 双账号视频 + 双版资料包 + 六张封面
  → 最终独立审核与 story_pipeline.py finalize
```

导演 `shot_id` 是镜头、故事板、R2V 任务和 PPT 正文页的唯一索引。图片目录数量、字幕行数、抽帧数和旧项目文件都不能产生新镜头。

## 观察与优化

- 看进度：`python3 story_pipeline.py status --run-file .../story_run.json`。
- 看架构：`python3 story_pipeline.py describe`。
- 看为什么这样做：当前 Codex 任务、导演计划和 Skill。
- 看产物是否可信：manifest、独立审核、QA 和交付回执。
- 修改导演/策略判断：更新 Skill、合同和代表性 Canary。
- 修改确定性行为：更新对应模块并增加回归测试。
- 修改供应商：只更新 provider adapter，不把模型或密钥写进编排层。

## Legacy 边界

旧 Agent、固定阶段 Runtime、工作台、恢复工具和根目录别名已移入 `historical_archive/` 的可校验独立源码包，包含清理时的未提交版本。当前样式与只读文本能力位于 `story_style.py` / `story_text.py`；生产导入不再到达旧 Agent。
