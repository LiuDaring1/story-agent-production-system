# 模型路由与 Agent 层级

## 两角色模型

当前正式设计只有两个认知角色，不是 Sol、Terra、Luna、DeepSeek 四层常驻体系。

| 角色 | 默认模型 | 当前推理档位 | 责任 |
| --- | --- | --- | --- |
| 指挥官 | `gpt-5.6-sol` | X-high | 关键判断、视觉审核、发布审核、产品审核 |
| 工人 | `gpt-5.6-luna` | Max | 生产、工具执行、图片、发布资产和一般阶段 |

指挥官阶段由 `AgentContext.codex_route()` 明确定义：

- `source_edit_review`
- `story_images_review`
- `video_prompt_review`
- `video_review`
- `release_preview`
- `release_video_review`
- `product_annotation_review`
- `product_package_review`
- `publish_package_review`

其他需要 Codex 的阶段在配置了 worker 时走 Luna。

## “很多 Agent”从哪里来

用户界面中看到的任务数量可能同时包含：

1. V3.5 当前 35 个逻辑阶段（V3 基线为 33，合同前置新增 2）；它们不是 35 个同时运行的模型。
2. DAG stage worker；最多三个并行进程。
3. 阶段内部的独立 `codex exec`；返工或重新审核会创建新任务。
4. 主任务在故障恢复、系统开发或独立复核时临时创建的协作 Agent。

所以任务数会累计，但它们通常不是同时活跃。未来运行账本必须把每个执行实例标记为“项目 / 阶段 / 尝试 / 生产或审核 / 当前或失效”。

## 原生视觉

GPT-5.6 系列具备原生多模态能力。关键视觉审核不应再通过为旧文本模型准备的外部视觉桥接层；当前视觉审核阶段使用原生附件上下文，以避免额外网络依赖和延迟。

## 模型优化方向

当前档位是保守质量基线，不是永久结论。未来应通过相同代表镜头进行受控实验，比较：

- Luna X-high 与 Luna Max；
- Terra Medium；
- 必要时的 Sol High/X-high。

指标必须是“合格产物的总成本”：首轮通过率、返工次数、墙钟时间、Token 和人工干预，而不是只比较单次响应速度。

建议升级梯度：确定性脚本不用模型；普通生产使用经济档；复杂连续性使用 Luna Max；连续失败才升级 Sol；最终关键审核保留 Sol。
