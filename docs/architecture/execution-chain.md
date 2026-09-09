# v2 候选接口说明

新生产合同和职责见 [候选操作手册](../decouple/candidate-guide.md) 与 `story_production_v2.py`。本轮不晋级默认。以下为原执行链/合同的保留说明，仅供历史兼容与模块原理参考；音乐生成、标注、PPTX、封面和文案不属于 v2 主链。

# 当前故事系统：实际执行链

这份文档回答五个具体问题：谁作决定、谁真正执行、Schema 做什么、结果返回给谁、证据记录在哪里。

## 先把角色说清楚

| 角色 | 实际责任 | 不是什么 |
|---|---|---|
| 用户 | 提供确认文本、绿幕原片和本故事要求；对需要人类判断的成片问题反馈 | 不需要操作旧工作台或阶段按钮 |
| 当前 Codex 前台任务 | 唯一总导演和编排者：读输入、写导演方案、调用工具、观看结果、决定通过/返工、安排汇合 | 不是常驻后台进程，也不是固定 38 阶段 Runtime |
| Skill | 给 Codex 提供现行生产规则、质量边界、工具路由和停止条件 | 不执行 Python、不提交供应商、不保存项目状态 |
| JSON Schema / validator | 检查计划或回执的字段、类型、ID、时窗、引用和哈希关系 | 不会生成创意、不调度任务，也不会把“Schema 执行结果”自动交给下一阶段 |
| Python/Node/FFmpeg 模块 | 真正执行确定性工作：时间轴、编译 jobs、提交/下载、媒体处理、PPT、包装、机器 QA | 不得擅自改变导演意图 |
| ImageGen / Grok / Suno | 生成图片、视频、配乐 | 它们的“已完成”自述不等于系统审核通过 |
| 独立审核上下文 | 在不继承生产者判断的上下文里，按当前文件哈希给出分数、关键错误和通过结论 | 不负责继续生产或替 Codex选创意方向 |
| `story_pipeline.py` / `story_run.json` | 记录六个粗粒度工作包、当前有效产物、依赖、SHA-256 与请求恢复事实，并在最后封口 | 不导演、不调度、不管理金额、不自动恢复某个阶段 |

## 一条完整的生产链

每一行都写明“输入 → 谁执行 → 返回什么 → 谁判断 → 记录在哪里”。

| 顺序 | 输入 | 谁真正执行 | 返回给 Codex 的结果 | 谁判断是否继续 | 主要记录 |
|---:|---|---|---|---|---|
| 1 | 确认文本、DOCX、已调色绿幕视频、本故事要求 | Codex + 只读探测命令 | 输入一致性、视频/音频参数、运行环境与模型可用性 | Codex；阻断项未解决不启动生产 | `99_项目状态/preflight/` 及任务中的检查结论 |
| 2 | 原视频 | FFmpeg、Whisper/对齐模块 | 派生干净音频、确认文本的权威时间窗；不改用户文字 | Codex核对覆盖范围和单调性 | 派生音频、时间轴 JSON、输入 SHA |
| 3 | 已确认输入路径和哈希 | `story_pipeline.py init` | 六个工作包的轻量账本 | 命令只校验事实；Codex继续编排 | `99_项目状态/story_run.json` |
| 3A | 当前输入、本包规则源和用户覆盖 | Codex + `story_requirements.py` | 带范围、版本、参数检查和验收证据的小型要求投影 | 生产者与审核者共用；真实工具入口在外部请求前复核 | `requirements_projection.json` + 账本 artifact |
| 4 | 文本、时间轴、年龄/风格/片头等 brief | Codex | 导演计划：`shot_id`、语义时窗、画面因果、状态、景别/构图、资产需求和提示词 | 独立审核上下文；失败则 Codex修订 | 导演计划 JSON、review JSON、review bundle |
| 5 | 通过审核的导演计划 | Codex 调用 ImageGen | 人物、状态、道具、环境等候选资产 | Codex先看；独立资产审核再放行 | 资产 manifest、候选图、资产 review/bundle |
| 6 | 已审计划和资产 | Codex 调用 ImageGen；`shot_storyboard_pipeline.py` 封存 | 每个 `shot_id` 一张故事板、故事板 manifest | Codex整组看片；独立故事板审核 | 故事板图片、sealed manifest、review/bundle |
| 7 | 封存故事板、导演提示词、资产引用 | `shot_storyboard_pipeline.py compile` | R2V jobs CSV、PPT 页面计划、编译回执；所有条目用同一个 `shot_id` | validator 检查结构/哈希；Codex检查语义 | jobs CSV、PPT plan、compile receipt |
| 8A | R2V jobs | provider adapter + ToAPIs Grok 1.0 runner | 逐镜任务号、实际提示词、下载 MP4 和请求状态事实 | Codex：完整性检查→连续预览→有证据的返工；随后独立整组审核 | jobs CSV、provider receipts、QA、review/bundle |
| 8B | 故事文本与情绪段 | Suno 工作流 + 本地音频模块 | 候选 MP3、段落计划、最终混音 | Codex选曲；音乐 QA 按输入哈希放行 | 音乐计划、源 MP3、`qa_music_report.json` |
| 8C | 绿幕视频 | RVM/抠像模块 | Alpha 视频、搜索结果、站立与大手势候选 | Codex检查；独立抠像审核锁定预设 | `keying_search.json`、样张、preset/review |
| 8D | 同一故事板清单和产品输入 | PPT、文稿、标注、封面、发布美术模块 | 双版 PPT、资料包、封面、文案、主题资产 | 各自机器 QA + Codex视觉检查 + 必要的独立审核 | manifests、QA、delivery receipts |
| 9 | 已审核 R2V、音乐、RVM、包装资产 | FFmpeg/发布模块 | 背景视频、主账号/宝库号预览 | Codex观看真实预览；通过后才正式编码 | preview review、布局 JSON、抽样证据 |
| 10 | 已通过预览的固定输入 | 正式渲染与打包模块 | 双账号视频、基础版/进阶版资料包、六张封面 | 最终独立审核核对当前 SHA-256 | 发布视频、产品包、最终 QA/review |
| 11 | 六包均完成、当前产物和审核 | `story_pipeline.py finalize` | `finalized=true` 或明确阻断原因 | 这是封口校验，不会再生成任何东西 | `story_run.json` |

各工作包有实测证据时，通过 `observe-performance` 记录计划与机器检查耗时、独立审核轮次、无效退件和重复编码；状态摘要同时从请求指纹计算重复 provider 请求，并从显式 `input_sha256s` 计算最长依赖链。未知项保留为 `null`，离线结构基准不能冒充真实供应商提速。

8A–8D 是可以并行的工作包；它们不是一套必须逐格执行的固定阶段。Codex根据依赖关系决定何时启动和汇合。

## Schema 到底处在什么位置

Schema 是“格式和引用关系的尺子”，不是“生产流水线”。例如：

```text
Codex 写导演计划
  → validator 用 Schema 检查 shot_id、时窗、字段和引用
    → 不合格：把错误信息返回 Codex，由 Codex修改
    → 合格：Codex才调用资产/故事板/视频模块
```

所以不存在“执行 Schema 后，Schema 自动把内容交给下一个 Agent”。真正的交接动作始终由当前 Codex 任务发起；Schema 只允许或拒绝这次交接。

## 审核如何回到生产链

独立审核收到的是明确文件清单和 SHA-256，而不是生产者一句“做好了”。它返回 review JSON：

- `approved`；
- `score`；
- `critical_errors`；
- 被审产物的 SHA-256；
- 必要时的具体退件依据。

Codex读取 review：通过就把该哈希版本登记为当前有效产物；不通过就分析原因、修改上游或执行受控返工。产物后来变更哈希，旧审核自动失效。

## 记录是否完整

有记录，但分成三层，不能只看一个文件：

1. `story_run.json`：项目级摘要——六包状态、当前有效产物、依赖哈希、任务号、时间/重试/Token 事实、是否封版。
2. 各模块 manifest/jobs/QA/review：执行细节——每镜提示词、参考图、供应商回执、退件、哈希绑定和机器指标。
3. 当前 Codex 任务：创意推理和临场判断——为什么选择某个构图、为什么接受或拒绝某个镜头。

当前系统没有把全部创意推理复制进一份巨型阶段日志；这是有意避免恢复旧 38 阶段 Runtime。交付事实必须落盘，导演判断保留在任务和导演文件中。

## 两个容易混淆的词

### `runtime` 与 `director_only`

这不是两套故事板。每个镜头仍只有一张同源故事板，PPT 都使用它。

- `runtime`：默认模式；这张故事板也作为 Grok 的最后一张语义参考图上传。
- `director_only`：导演计划必须填写 `storyboard_reference_reason`，说明出口结果与本镜入口过程的具体冲突；导演和 PPT 继续使用该图，Grok 使用独立资产与表演节拍。

因此执行规则是“默认上传，记录到具体冲突才例外”。

### “不机械划分动作所有权”

相邻镜头连播后，以四项结果确定动作边界：动作是否重复、状态是否连续、因果是否清楚、节奏是否顺畅。自然表演延伸到下一句时，下一镜可以顺势聚焦后续触发或结果。

## `finalize --require` 是什么

`story_run.py` 已内置 Codex 原生生产的最低 artifact 清单。`--require artifact_id` 只用于追加某个项目特有的产物，例如额外回执；它不能删减内置清单，也不是让用户填写的故事参数。
