# Module Ports（M3-3A）

本页记录 Story Agent 的最小模块接口模式。核心原则是“装插座，不换电器”：Runtime 通过 Port 调用当前实现，但不改变 38-stage DAG、供应商、滤镜、质量门禁、重试、预算或 currentness 语义。

## 四层边界

```text
Orchestrator / Runtime
        ↓
Versioned Port contract
        ↓
Concrete adapter
        ↓
Current provider / local tool

Quality Policy（独立审核结果，不属于 Port 执行失败）
```

- **Runtime** 决定阶段、门禁、blocked/failed/retry 和持久化事实。
- **Port** 只描述能力、输入、执行结果、模块失败和最小 usage event。
- **Adapter** 委托现有 provider/tool，不复制业务事实源。
- **Quality Policy** 判断结果是否够好。视频运动仍由 `video_motion.py` 和独立审核负责；发丝、spill、halo、P0 仍由 `keying_quality.py` 和独立审核负责。

Port Result 是一次调用的结构化返回，不取代 M2 已有 receipt、manifest、review bundle 或 SHA-256 currentness。`ModuleUsageEvent` 只预留调用级事件结构；M3-3A 没有建立请求账本、Token 账本、币种结算或 Agent tree。

## Kernel 与 Registry

- `story_module_ports.py`：冻结 dataclass、`typing.Protocol`、统一 failure vocabulary、usage event、Python validator。
- `schemas/module_ports/v1/module_ports.schema.json`：identity、capabilities、request、result、failure、usage event 的机器合同。
- `story_module_adapters.py`：现有视频 provider 和生产 FFmpeg keyer 的薄适配器，以及完全离线 deterministic mock。
- `story_module_registry.py`：显式注册/选择 adapter；不按价格或质量智能路由，也不自动 fallback。

只读诊断：

```bash
python3 story_module_registry.py list
python3 story_module_registry.py describe video_generator
python3 story_module_registry.py describe keyer
```

诊断只输出版本和能力，不输出 API key、secret 或浏览器凭据。

## VideoGeneratorPort v1

版本：`story-video-generator-port/v1`。

能力声明包含当前 provider/model/runner、单图首帧、时长范围、默认分辨率和比例、external/paid 属性。参考视频、参考音频和多图 conditioning 明确为不支持，不做能力臆测。

Request 绑定 scene/artifact、源图路径与 SHA、prompt 与 SHA、时长/比例/分辨率、输出目标、attempt、Story Contract dependency 和 motion-plan SHA。Port 不重新解释动作计划。

Result 表达 success/failure、输出、provider/model/request id、请求/产出时长、attempt、输入绑定、adapter version 和 usage events。现有正式视频 receipt 仍是持久事实源。

`ExistingVideoGeneratorAdapter` 持有原 `VideoProviderAdapter`，继续使用原 runner、CLI 参数、环境变量名、逐行秒数与成本估算。`story_agent.py` 与 `story_workflow.py` 的正式 provider resolution / invocation seam 已经过 Registry；实际提交、轮询、下载、付费门禁和 receipt 继续走原 `run_image_video_jobs.py`。

## KeyerPort v1

版本：`story-keyer-port/v1`。

Request 绑定源文件/SHA、现有 preset settings、crop 所在 settings、输出目标、attempt、preset/lock SHA。Result 记录输出/SHA、keyer、filter version/fingerprint、输入和 adapter 绑定。

`ProductionKeyerAdapter` 只委托 `production_keying.py` 的 contract、filter chain、filter parts、fingerprint 和正式 foreground renderer，不复制 FFmpeg filter string。Demo、Release 与 keying evidence 的调用 seam 都经过 KeyerPort，但最终仍落到同一生产 renderer，因此 M2-2C1.1 的同链证据不变。

本地 FFmpeg usage 标记为 `not_applicable`，不伪造外部费用或完整账单。

## 注入 Mock

`StoryAgent(..., module_registry=custom_registry)` 可注入测试 Registry；独立 consumer 也只依赖 Registry/Port。`MockVideoGeneratorAdapter` 支持确定性成功、unsupported、execution failure 和 invalid output；`MockKeyerAdapter` 支持确定性复制、invalid input 和 execution failure。它们不访问网络，也不代表真实视觉质量。

## 兼容与后续

- 旧 `resolve_video_provider()`、CLI、workbench 和 legacy passthrough 保留。
- required_v1 的 Story Contract、审核哈希、付费门禁和 M2 质量政策没有降低。
- M3-3A 只完成 VideoGeneratorPort 与 KeyerPort。StorySemantics、VisualDesign、ImageGenerator、Music、Compositor、Release、Publish 和 Product Package 等 Port 属于后续 M3，尚未实现。
- 自动多供应商路由、请求级账本、context pack、Agent tree、真实成本结算和逐镜依赖图不属于本阶段。
