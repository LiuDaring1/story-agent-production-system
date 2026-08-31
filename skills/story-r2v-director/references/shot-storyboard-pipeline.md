# 逐镜故事板同源流水线

这条流水线只接受已经完成镜头划分、资产声明和连续性设计的基础 `story_r2v_plan.json`。导演计划中的有序 `shot_id` 是唯一镜头索引；资产目录文件数、字幕行数、抽帧数和旧 PPT 页数都不能产生新镜头。

## 产物关系

```text
基础导演计划
  → 资产清单 + 独立审核
  → 逐 shot_id 的 ImageGen 请求
  → 封存故事板清单 + 独立审核
  → 同时编译：R2V 计划 / Grok 任务 CSV / 静态 PPT 计划
```

PPT 主图与 R2V 任务记录的语义故事板必须指向同一图片路径和 SHA-256。逐镜 `storyboard_reference_mode=runtime` 时它同时是 R2V 最后一张参考；`director_only` 时它只供导演和 PPT 使用，不上传给视频模型。任何一张图片、资产或导演镜头变化，封存清单和两个消费者同时失效。

## 1. 封装并审核正式资产

```bash
python3 shot_storyboard_pipeline.py assets \
  --director-plan /absolute/path/story_r2v_plan.json \
  --output /absolute/path/runtime_asset_bundle.json
```

新的独立上下文审核角色、状态特定角色、空环境及其连续场景组、道具状态家族、尺度和物理合同。资产清单保存完整资产合同和 `continuity_groups`，因此同一地点的昼夜、天气或损坏/修复变化也必须在这里先审核。审核 JSON 必须满足：

- `approved: true`
- `score >= 85`
- `critical_errors: []`
- `artifact_sha256` 等于资产清单中的 `asset_bundle_sha256`

## 2. 生成逐镜故事板请求

```bash
python3 shot_storyboard_pipeline.py plan \
  --director-plan /absolute/path/story_r2v_plan.json \
  --asset-bundle /absolute/path/runtime_asset_bundle.json \
  --asset-review /absolute/path/runtime_asset_review.json \
  --output-dir /absolute/path/shot_storyboards \
  --output-manifest /absolute/path/shot_storyboards_planned.json
```

使用清单中的 `imagegen_requests` 逐镜调用 ImageGen。每个请求只生成一张 16:9 完整句意图；必须使用该镜头已审核资产，不能从视频、旧项目、逐句图库或相邻镜头选图。

人物、道具和环境资产总数超过 ImageGen 引用上限时，只能移除可选风格图；身份关键人物、入口道具或环境仍超限时回到导演计划拆镜，不能静默丢弃正式资产。

## 3. 机器预检、封存和整组审核

```bash
python3 shot_storyboard_pipeline.py seal \
  --input-manifest /absolute/path/shot_storyboards_planned.json \
  --output-manifest /absolute/path/shot_storyboards_sealed.json
```

封存阶段要求每个 `shot_id` 的图片存在、至少 1280×700、比例为 16:9，并记录当前 SHA-256。新的独立上下文随后整组审核人物身份、道具状态、场景、人物数量、空间关系和整句可读性；同时必须核对当前事实与角色设想的时态、头/嘴/躯干/四肢的身体连接关系，并对内容插入镜执行去字幕可读性检查。任一设想被误画为当前事实、肢体来源无法追溯或插入镜无法对应具体台词职责，都是关键错误。审核 JSON 的 `artifact_sha256` 必须等于 `storyboard_bundle_sha256`。

## 4. 一次编译两个消费者

```bash
python3 shot_storyboard_pipeline.py compile \
  --sealed-manifest /absolute/path/shot_storyboards_sealed.json \
  --storyboard-review /absolute/path/shot_storyboards_review.json \
  --output-r2v-plan /absolute/path/story_r2v_plan_with_storyboards.json \
  --output-jobs-csv /absolute/path/r2v_jobs.csv \
  --previous-ppt-plan /absolute/path/static_ppt_timing_plan.json \
  --output-ppt-plan /absolute/path/static_ppt_plan.json \
  --output-receipt /absolute/path/shot_storyboard_compile_receipt.json
```

编译器执行以下机器门禁：

- 导演镜头、故事板条目、PPT 正文页和 R2V 任务严格同序同数；
- 为每镜登记一个 `kind: storyboard` 资产；`runtime` 时追加到 `reference_asset_ids` 最后，`director_only` 时只登记证据路径；
- 实际上传的正式资产加 runtime 故事板不超过供应商 7 图上限；超限时报错，不自动丢资产；
- 只有 runtime 故事板的 R2V 提示自动加入“不是首帧、不锁构图”的短声明；
- 任务 CSV 记录全部参考图的有序路径和哈希、故事板清单哈希及目标 `shot_id.mp4`；
- PPT 正文图片直接消费封存故事板，不再次调用 ImageGen。

ToAPIs 批处理读取 `reference_image_paths_json` 时调用真正的 R2V `reference_images` 接口；缺图、改图、runtime 故事板不在最后、director-only 故事板误入上传列表或清单哈希失效都会在付费前阻断。

旧的 I2V/逐句图片 CSV 保持兼容。只有带 `generation_mode=reference_to_video` 和 `reference_image_paths_json` 的任务进入多参考 R2V 分支。

## 5. 交付侧封口

`shot_storyboard_compile_receipt.json` 只证明两份消费者计划同源，还不能证明客户目录中的 PPTX 没被后续替换。静态 PPT 构建、内嵌配乐和自动翻页 QA 完成后，必须把导演计划、该编译回执、静态 PPT 计划和双版 PPTX 一起交给 `product_package.py` 的封存静态 PPT 模式。打包器复制后生成 `static_ppt_delivery_receipt.json`；最终账本同时登记两个回执，并验证后者绑定前者的当前 SHA-256。
