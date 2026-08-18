# Module Ports（V3.5 Milestone 3 engineering implementation complete）

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

Port Result 是一次调用的结构化返回，不取代 M2 已有 receipt、manifest、review bundle 或 SHA-256 currentness。`ModuleUsageEvent` 只预留调用级事件结构；M3-3A/3B/3C 没有建立请求账本、Token 账本、币种结算或 Agent tree。

## Kernel 与 Registry

- `story_module_ports.py`：冻结 dataclass、`typing.Protocol`、统一 failure vocabulary、usage event、Python validator。
- `schemas/module_ports/v1/module_ports.schema.json`：Video/Keyer identity、capabilities、request、result、failure、usage event 的机器合同。
- `schemas/module_ports/v1/`：十个 Port 的版本化 schema；3B/3C 独立 schema 不改变旧 M3-3A payload。
- `story_module_adapters.py`：十个现有模块执行边界的薄适配器，以及相应 deterministic mock。
- `story_module_registry.py`：显式注册/选择 adapter；不按价格或质量智能路由，也不自动 fallback。

只读诊断：

```bash
python3 story_module_registry.py list
python3 story_module_registry.py describe story_semantics
python3 story_module_registry.py describe visual_design
python3 story_module_registry.py describe image_generator
python3 story_module_registry.py describe video_generator
python3 story_module_registry.py describe music_provider
python3 story_module_registry.py describe keyer
python3 story_module_registry.py describe compositor
python3 story_module_registry.py describe release_layout
python3 story_module_registry.py describe publish_asset
python3 story_module_registry.py describe product_package
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

## ImageGeneratorPort v1

版本：`story-image-generator-port/v1`。

Request 只绑定 Runtime 已经确定的一次图片执行 envelope：artifact/operation、当前 handoff 路径与 SHA、输入 artifact bindings、输出目标和 attempt。它不规划 prompt、batch、文件名、路径、staging/sync、paid gate 或 currentness。

`CodexImageGeneratorAdapter` 只委托 Runtime 传入的原 `_codex_task` executor。正式 consumer seam 位于 `StoryAgent._stage_visual_samples()` 的 supplemental sample 调用和 `StoryAgent._stage_codex_story_images()` 的逐 batch 调用；原 handoff、prompt、batch size、`{slug}_scene_XX.png` 命名、staging 到 final sync、authoritative storyboard SHA 保护、visual sample QA/review/lock 与 story image review 均留在 Runtime 和原 Quality Policy。

`MockImageGeneratorAdapter` 完全离线并生成确定性、可解码的测试 PNG；其 Result 和 artifact 均明确 `production_eligible=false`。`mock-image` 只有在 selected profile、required profile、selected execution mode 和 required execution mode 四项同时锁定为 `mock-image`/`test` 时才能执行，任一丢失或错配都 fail closed，不回落 Codex/ImageGen。

## MusicProviderPort v1

版本：`story-music-provider-port/v1`。

Request 只绑定 Runtime 已经确定的一次音乐 provider execution envelope：当前 Suno handoff/request 路径与 SHA、输入 artifact bindings、既有输出目标/位置语义和 attempt。故事情绪、分段、prompt/request、provider 和 target filename policy 仍由原 music planning 决定。

`SunoMusicProviderAdapter` 只委托 Runtime 传入的原 `_codex_task("suno_generate")` executor。正式 consumer seam 仅位于 `StoryAgent._stage_suno_generate()`；原 stage/label/prompt、Ego Browser/Suno 流程、下载目录、`target_audio_filename`、`_has_suno_audio()` 完成判断、`assemble_suno_music.py` 的 exact-name/segment-prefix fallback 和 music QA/currentness 均保持独立。

`MockMusicProviderAdapter` 完全离线，Result 和 artifact 均为 `production_eligible=false`。`mock-music` 使用与 `mock-image` 相同的 profile + execution-mode 双锁，任一传播丢失或错配都在启动 Codex/browser/Suno 前 fail closed。

## ProductPackagePort v1

版本：`story-product-package-port/v1`。

Request 只绑定 `create_package_dirs()` 的一次已规划 filesystem execution：artifact/operation、base+advanced invocation、output root、caller 已确定的 source SHA-256 → destination mapping、output targets 和 attempt。基础版/进阶版包含内容、客户目录名、客户文件名和 canonical naming 仍由 `product_package.py` 的原 Product Policy 决定。

`LocalProductPackageAdapter` 在任何目录变更前验证全部 source 文件和 SHA-256，再原样委托 caller 提供的现有 backup/mkdir/`shutil.copy2` executor。Result 只记录本次复制观察到的路径与 SHA-256；它不声明 package approved、QA passed、current、complete 或 delivery ready，也不替代 `product_package_manifest.json` 及其依赖 receipts。

正式 consumer seam 仅位于 `product_package.create_package_dirs()`。DOCX/PPT/Demo 生成、semantic selection、product content manifest、package manifest、机器 QA、独立审核与 Runtime completion 均保持原调用链。

`MockProductPackageAdapter` 只把确定性 fixture 写入请求 output root 下的 `_mock_product_package` 测试隔离目录，不调用 production executor、不写请求中的正式客户 targets，并始终返回 `production_eligible=false`。C1 不新增 CLI mock profile；测试通过 Protocol 注入 mock/fake，因此没有新增第二套 profile 安全机制。

## CompositorPort v1

版本：`story-compositor-port/v1`。

Request 只绑定 caller 已决定的一次本地合成 invocation：`background_story`、`presenter_demo` 或 `a_only_background`，输入路径/SHA-256、既定输出目标、已经解析的 execution/config binding 和 attempt。时间线、视频顺序、semantic card、完整/sales/Demo 字幕选择、旁白/音乐 mix、音量、Demo geometry、A 镜 layout 与 canonical filename 均留在原 caller/core。

`LocalCompositorAdapter` 先验证全部输入 SHA，再精确委托 caller 提供的原 Python/FFmpeg executor；不重写 filter graph、编码参数或失败策略。三个正式 seam 分别位于 `story_video_synthesizer.pipeline.synthesize_story()`、`product_package.render_demo_video()` 和 `product_package.render_a_only_background_video()`，原函数体作为 executor 保留。

Result 只记录一次执行观察到的输出路径/SHA-256、adapter、usage/failure 和 production eligibility，不声明 QA、review、currentness、stage complete、product complete 或 release ready。`project_manifest.json`、artifact semantic plan/assembly manifest、music QA、Demo render manifest、machine QA 和 hash-bound review 仍是原权威事实源。

`MockCompositorAdapter` 完全离线，只在系统临时目录的 `_mock_compositor` 隔离目录写 deterministic marker，不调用 production executor，也不写正式 targets，并始终返回 `production_eligible=false`。C2 不新增 runtime selectable mock profile。

## ReleaseLayoutPort v1

版本：`story-release-layout-port/v1`。

Request 只绑定 caller 已完成选择的一次 release leaf render：输入路径/SHA-256、已解析 layout/geometry binding、单一输出目标和 attempt。主账号/宝库号定义、A/B/C 时间窗、final Demo geometry inheritance、presenter safe zone、Logo/故事框/水印、字幕选择、尾段文案、canonical filename 和 preview-before-full-render 均留在原 Product/Quality Policy。

正式 seam 位于 `render_release_previews()` 和 `package_release_videos()` 内部已解析完成的 preview main/library、main wide/vertical、library window/vertical 与 legacy plate render call。`LocalReleaseLayoutAdapter` 在执行前验证输入 SHA，再精确委托原 PIL/FFmpeg renderer；原 geometry compiler、render functions、编码参数和 output naming 未改写。

Release geometry manifest 与 release render manifest 仍由 `release_video.py` 在 Port 外的原位置写入并回读验证。Port Result 只报告一次 render 的 output/SHA、adapter、usage/failure 和 production eligibility，不声明 geometry/keying/QA/review/currentness/release ready，也不替代 preview lock、keying lock、`qa_release_report.json` 或 hash-bound review。

`MockReleaseLayoutAdapter` 只在系统临时目录 `_mock_release_layout` 写 deterministic marker，不调用 production renderer、不写 canonical targets，也不生成 geometry/render manifest、QA、review 或 lock，并始终返回 `production_eligible=false`。C3 不新增 runtime selectable mock profile。

## PublishAssetPort v1

版本：`story-publish-asset-port/v1`。

Request 只绑定 caller 已决定的一次本地 publish execution：输入路径/SHA-256、已解析 reference/cover binding、输出目标和 attempt。正式 seam 位于 `publish_package.py` 的候选帧抽取/contact sheet leaf，以及 `cover_quality.render_required_covers()` 已解析 creative base、比例 variant、标题、官方 Logo 和安全区后的逐封面 deterministic render。Creative base 的真实生成仍只经过现有 `ImageGeneratorPort`，PublishAssetPort 不持有 Codex/ImageGen provider、model、prompt 或 fallback。

`LocalPublishAssetAdapter` 在 FFmpeg/Pillow executor 前验证全部输入 SHA，再精确委托原本地实现。六比例、文案、account positioning、creative/final lineage、official Logo policy、canonical filenames、ratio/size/near-duplicate QA、独立审核和 currentness 均留在 caller 与原 Product/Quality Policy。

`cover_creative_lineage.json`、`cover_lineage.json`、`cover_render_manifest.json`、`publish_asset_manifest.json`、`publish_cover_branding.json`、`qa_publish_report.json`、publish bundle/review、Story Contract receipt 与 `project_manifest.json` 继续由原逻辑写入和验证。Port Result 只报告本次输出路径/SHA、adapter、usage/failure 和 production eligibility，不替代这些事实源。

`MockPublishAssetAdapter` 只在系统临时目录 `_mock_publish_asset` 写 deterministic marker，不调用 production executor、Codex 或 ImageGen，不写正式封面或伪造 lineage/manifest/QA/review/receipt，并始终返回 `production_eligible=false`。C4 不新增 runtime selectable mock profile。

## 注入 Mock

`StoryAgent(..., module_registry=custom_registry)` 可注入测试 Registry；独立 consumer 也只依赖 Registry/Port。`MockVideoGeneratorAdapter` 支持确定性成功、unsupported、execution failure 和 invalid output；`MockKeyerAdapter` 支持确定性复制、invalid input 和 execution failure；Semantics/VisualDesign/Image/Music mock 如上所述。Registry profile 是固定 allowlist，禁止任意 import、Python class 或 shell command。mock 不访问网络，也不代表真实产品或视觉质量。

profile selection 通过显式 CLI 参数和四个非秘密环境锁在 parent、supervisor、run、run-stage/DAG worker、`story_workflow.py` 与 Codex subprocess boundary 传播：`STORY_MODULE_PROFILE`、`STORY_MODULE_PROFILE_REQUIRED`、`STORY_MODULE_EXECUTION_MODE`、`STORY_MODULE_EXECUTION_MODE_REQUIRED`。required/selected 任一缺失或错配均拒绝执行；不序列化 arbitrary Python object，也不在 profile/env 中携带 provider secret。当前单 profile 一次只替换一个模块，不做组合路由。

## M3-Z 总体离线验证

`tests/test_m3_cross_port_smoke.py` 在同一个 `ModuleRegistry` 中注册十个 deterministic mock，并以哈希绑定的 Request/Result 串起 Semantics → VisualDesign → Image → Video → Music → Keyer → Compositor → Release → Publish → Product。测试为外部 executor 和 subprocess 设置 zero-call sentinel，并确认：

- mock 不调用 ImageGen、Grok、Suno、browser 或其他外部执行；
- 下游 Request 消费上游产物路径与 SHA-256；
- 四个本地 Compositor/Release/Publish/Product mock 只写系统临时目录下的隔离 fixture，不写 canonical target；
- 所有可用于产品判定的 mock Result 都保持 `production_eligible=false`；
- QA/currentness sentinel 不被 Port 修改，Port Result 不会伪造 review、lock、receipt 或 manifest；
- production-default 仍精确解析当前十个正式 adapter，profile/execution-mode 四锁跨 subprocess 传播，丢锁则 fail closed。

## 兼容与后续

- 旧 `resolve_video_provider()`、CLI、workbench 和 legacy passthrough 保留。
- required_v1 的 Story Contract、审核哈希、付费门禁和 M2 质量政策没有降低。
- M3-3A 已完成 VideoGeneratorPort 与 KeyerPort；M3-3B 已完成 StorySemanticsPort、VisualDesignPort、ImageGeneratorPort 与 MusicProviderPort 的 contract、adapter、Registry、mock 和 production seam。
- M3-3C 完成 C1 ProductPackage filesystem seam、C2 Compositor 三个本地 leaf execution seam、C3 ReleaseLayout preview/full leaf render seam、C4 PublishAsset reference/final-cover leaf execution seam。
- M3-Z 已完成十 Port 总审计、跨 Port 离线 smoke、production-default parity、Schema/Protocol 总回归和文档收口。详细证据见 `docs/roadmaps/V3.5_M3_CLOSEOUT_2026-08-19.md`。
- “Milestone 3 engineering implementation complete” 只表示十个模块插座的工程边界和离线兼容验证已完成；真实新故事 Canary、真实供应商产品质量和用户验收均未进行，M4 未开始。
- 自动多供应商路由、请求级账本、context pack、Agent tree、真实成本结算和逐镜依赖图不属于本阶段。
