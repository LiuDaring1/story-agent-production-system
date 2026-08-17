# Module Ports（M3-3A + M3-3B-1）

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
- **Product Policy / Quality Policy** 判断结果如何用于产品、以及结果是否够好。逐产物 include/exclude、视觉替代和互斥仍在 `artifact_semantic_plan.py`；视觉小样要求、身份规则、构图/色彩/光照、解剖一致性、机器 QA、独立审核和 P0 仍在 `visual_sample_gate.py`。视频运动仍由 `video_motion.py` 和独立审核负责；发丝、spill、halo、P0 仍由 `keying_quality.py` 和独立审核负责。

Port Result 是一次调用的结构化返回，不取代 M2 已有 receipt、manifest、review bundle 或 SHA-256 currentness。`ModuleUsageEvent` 只预留调用级事件结构；M3-3A/3B-1 没有建立请求账本、Token 账本、币种结算或 Agent tree。

## Kernel 与 Registry

- `story_module_ports.py`：冻结 dataclass、`typing.Protocol`、统一 failure vocabulary、usage event、Python validator。
- `schemas/module_ports/v1/module_ports.schema.json`：Video/Keyer identity、capabilities、request、result、failure、usage event 的机器合同。
- `schemas/module_ports/v1/story_semantics_port.schema.json` 与 `visual_design_port.schema.json`：前半链两个 Port 的独立 v1 schema；不改变旧 M3-3A payload。
- `story_module_adapters.py`：现有 Story Semantics、已审核 Story Contract projection、视频 provider 和生产 FFmpeg keyer 的薄适配器，以及 deterministic mock。
- `story_module_registry.py`：显式注册/选择 adapter；不按价格或质量智能路由，也不自动 fallback。

只读诊断：

```bash
python3 story_module_registry.py list
python3 story_module_registry.py describe story_semantics
python3 story_module_registry.py describe visual_design
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

## StorySemanticsPort v1

版本：`story-semantics-port/v1`。

`ExistingStorySemanticsAdapter` 逐次委托现有 `story_semantics.py`，返回与源文件 SHA-256、逐行编号/原文、语义 kind、segment boundary 和 compiler version 绑定的结果。它不复制 classifier，也不决定逐产物 include/exclude、`visual_substitute`、互斥、卡片或客户产品规则。

正式 consumer seam 位于 `artifact_semantic_plan.compile_artifact_semantic_plan()`。consumer 只依赖 `StorySemanticsPort` Protocol，并在 Product Policy 消费前验证源 SHA、行身份、原文、kind 和 segment boundary。`artifact_semantic_plan.json` 的 schema、字段和 currentness 仍由原 compiler 负责，没有新增第二 semantic manifest。

`MockStorySemanticsAdapter` 是确定性已知 fixture；`mock-semantics` profile 必须显式选择。父进程要求该 profile 而子进程丢失选择时会 fail closed，不回落 production。

## VisualDesignPort v1

版本：`story-visual-design-port/v1`。

`ApprovedStoryContractVisualDesignAdapter` 只通过现有 `locked_contract_binding("storyboard_images")` 读取当前已审核、已锁定的 projection，并从同一 Story Production Contract 返回 preview references。每次调用都重新验证 contract、review、lock 和 currentness；Story Production Contract 始终是唯一视觉设计事实源，不创建 `visual_design_contract.json`、第二 visual bible 或其他权威副本。

正式 consumer seam 位于 `visual_sample_gate.compile_visual_sample_plan()`。consumer 只依赖 `VisualDesignPort` Protocol，并再次核对 Story Contract SHA、projection SHA、projection 内容和调用 binding。Port 只回答“当前允许消费哪个已批准 projection”，不判断“设计好不好”。视觉小样的需求、identity policy、quality profile、机器 QA、独立多模态审核、P0 和 review/lock 全部保留在原 Quality Policy。

`MockVisualDesignAdapter` 使用同一已锁事实源做确定性替换，不生成第二合同；`mock-visual-design` profile 丢失时同样 fail closed。

## 注入 Mock

`StoryAgent(..., module_registry=custom_registry)` 可注入测试 Registry；独立 consumer 也只依赖 Registry/Port。`MockVideoGeneratorAdapter` 支持确定性成功、unsupported、execution failure 和 invalid output；`MockKeyerAdapter` 支持确定性复制、invalid input 和 execution failure；Semantics/VisualDesign mock 如上所述。Registry profile 是固定 allowlist，禁止任意 import、Python class 或 shell command。mock 不访问网络，也不代表真实产品或视觉质量。

## 兼容与后续

- 旧 `resolve_video_provider()`、CLI、workbench 和 legacy passthrough 保留。
- required_v1 的 Story Contract、审核哈希、付费门禁和 M2 质量政策没有降低。
- M3-3A 已完成 VideoGeneratorPort 与 KeyerPort；M3-3B-1 已完成 StorySemanticsPort 与 VisualDesignPort 的 contract、adapter、Registry 和 production seam。
- ImageGeneratorPort 与 MusicProviderPort 尚未进入 M3-3B-2；Compositor、Release、Publish 和 Product Package 等 Port 也尚未实现。M3-3B-1 完成不代表 M3-3B 或 Milestone 3 完成。
- 自动多供应商路由、请求级账本、context pack、Agent tree、真实成本结算和逐镜依赖图不属于本阶段。
