# Story Production Contract

## 目的与边界

V3.5 Milestone 1 在高成本批量生产前建立每个故事自己的生产规范。通用代码不写死故事、人物、画风或绝对路径；预览资产按故事需要条件生成，无角色不造角色卡，无可靠尺度依据不造精确比例。

当前仍使用 Codex + GPT：Luna Max 生成草案，Sol 独立审核；图片、视频、音乐和媒体栈仍是 ImageGen、Grok、Suno 与 FFmpeg。合同前置不等于去 Codex，也不等于完整模块化。

## 七个 section

| section | 责任 |
| --- | --- |
| `semantic_artifacts` | 标题、开场、正文、道理、结尾在各产物中的确定性选择 |
| `visual_style` | 画风、必要特征、禁止特征与条件式风格锚点 |
| `characters` | 角色身份、角色卡和一致性；允许 `mode=none` |
| `world_scale` | 定性大小/环境参照；只有可信依据或 QA 需要时才使用宽容数值范围 |
| `story_state` | 角色/物体状态机、允许转移和故事边界 |
| `brand` | 官方品牌资产、数量、用途和禁止生成 Logo |
| `release_layout` | 比例、标题/字幕/真人/故事画面安全区和版式约束 |

JSON Schema 位于 `schemas/story_contract/v1/`，Python 验证器位于 `story_contracts.py`。parity 测试保证二者接受/拒绝同一组关键样本，生产运行不新增 jsonschema 依赖。

## 可信来源与冲突优先级

Runtime 先写 `story_contract_trusted_inputs.json`，优先级固定为：

1. `task_input`；
2. `project_config`；
3. `brand_or_global_default`；
4. `agent_inference`。

前三类只能引用 Runtime 已登记的 source/ref/SHA，并提供可复核 quote 或 JSON pointer。模型自己的分析只能写 `agent_inference`，不能自行抬高 provenance。普通冲突按优先级自动解决；只有无法安全解决的真实冲突才阻断。

## 生成、审核与锁

1. `story_contract`：Luna 读取可信来源链和 Schema，写合同 JSON、人读摘要及条件式预览。
2. `story_contract_review`：Sol 在独立上下文审七个 section，标准 review bundle 绑定全部证据 SHA-256；必须 `approved=true`、`score>=85`、`critical_errors=[]`，且 `evidence_matrix` 覆盖七节。
3. Runtime 重新验证合同、来源、预览、bundle 和 review，随后同目录临时文件 + fsync + atomic replace 写 `story_contract.lock.json`。

恢复时不会信任 manifest 中的 `status=locked`。半写、JSON 损坏、字段缺失、合同/来源/bundle/review 任一 SHA 不匹配，都使锁无效并阻断消费者。

## 六类消费者

消费者只拿最小 projection，不解析整份合同：

| 消费者 | section | 消费方式 |
| --- | --- | --- |
| 分镜/生图 `storyboard_images` | semantic/style/characters/scale/state | 结构化 Agent handoff；计划必须原样绑定 projection |
| 图生视频 `image_video` | characters/state | 直接确定性 jobs/prompt + 付费前双重锁门禁 |
| 音乐 `music` | semantic/state | 结构化 Suno request/handoff；计划绑定当前合同 |
| 封面 `cover` | brand/characters/layout | 编译后的确定性 cover spec + 创意 Agent |
| 发布视频 `release_video` | brand/layout | 编译后的确定性 render spec 进入最终 FFmpeg 参数 |
| PPT/资料包 `product_package` | semantic | 确定性内容选择器；Agent 只负责语言/表演理解 |

`<consumer>.json` 是生产请求清单（consumer manifest），记录 schema、完整合同审计 SHA、projection dependency SHA 和 projection。真正完成后才写 `<consumer>.completed.json` receipt，它绑定 request manifest 文件 SHA。封面 asset manifest、发布视频 render manifest、产品 preflight/annotation/demo manifest 还会记录同三项合同字段；它们不能替代 completed receipt。

## 增量失效与诊断

dependency SHA 只覆盖消费者声明的 section。品牌/布局变化不重做故事图片；视觉风格变化不重做音乐；语义变化会失效分镜、音乐和资料包，但不失效封面品牌布局。当前粒度是模块族，不宣称是逐镜头 dependency graph。

`story_agent.py status` 的 `story_contract` 对象和 `story_agent.py report` 的合同表复用生产验证器，只读显示：policy、合同/schema/SHA、审核 current、锁有效性、六类 consumer manifest/receipt 状态，以及造成 stale 的 section。诊断不会修文件、生成 legacy receipt 或触发付费调用。

## V3 legacy

V3 冻结项目不补造合同、不改交付物，也不伪造新审核。`legacy_passthrough` 只在 manifest 创建时间早于冻结点、存在已通过的历史阶段，并且 Runtime 生成的 eligibility receipt 与这些事实完全匹配时成立。V3.5 新任务手改 policy 或复制 receipt 不能绕过门禁。
