# 静态故事 PPT

正式客户 PPT 只使用静态图片，交付含字幕版和无字幕版两份。两份必须使用相同图片顺序、相同页面时长和同一配乐，仅字幕层不同。

页面清单的唯一权威是已审核导演计划里的有序 `shot_id`。绝不能用图片目录文件数、逐句台词数、SRT 字幕条数、抽帧数量或视频帧数推断 PPT 页数。即使台词行数或中间图片数远多于导演镜头数，正文页数也必须严格等于导演镜头数。

## 页面顺序

1. 第一页只放已经审核的故事标题图。主持人自我介绍和“今天讲……”等报幕期间不显示字幕。
2. 从故事正文第一句开始，按权威时间轴依次放置每个分镜对应的静态图片；含字幕版从这一页才开始显示字幕。
3. 故事存在寓意卡时，把已经审核的寓意图放在最后一页。寓意文字已在图片中时不要再叠重复字幕。

TITLE 的时长从完整音频 0 秒开始，覆盖“大家好，我是……”到报完故事名的全部口播；MORAL 的时长从“小朋友们，这个故事告诉我们……”开始，一直到完整音频结束。正文 SRT 只能决定正文页，不得把 TITLE 改成 0 秒或吞掉 MORAL。

先写出静态 PPT 计划，页 ID 必须严格等于：

```text
TITLE + 导演计划中按顺序排列的全部 shot_id + 可选 MORAL
```

不得缺页、重复、改序或额外插入逐句页。没有独立寓意卡时不得为了凑版式增加 `MORAL`。计划必须绑定导演计划 SHA-256，并为每页记录静态图片路径/哈希、页面时长和字幕文本。

每个导演镜头页的主图必须直接消费 `story-shot-storyboards/v1` 中同 `shot_id` 的封存图片。这批图片已经基于完整 `story_text`、导演设计和已审核角色/道具/环境资产逐镜生成，同时也是对应 R2V 镜头的最后一张语义参考。PPT 阶段禁止再次调用 ImageGen 另造第二套图；也禁止从 AutoViz、R2V、I2V 视频、旧逐句图片目录或任意抽帧目录挑图。

计划必须记录 `poster_origin=imagegen_shot_illustration`、故事板 bundle SHA-256、完整台词哈希、导演镜头哈希、参考资产哈希和 ImageGen 提示词哈希；缺一项即失败。单张插图应以一个连贯、可读的决定性画面表达整句话的因果关系，不做多格漫画或拼贴。

## 播放

- 每页铺满一张 16:9 图片，不添加模板边框、角标或多余装饰。
- 配乐必须内嵌在 PPTX，从第一页开始跨页播放，不依赖客户电脑上的外部路径。
- 每页按旁白权威时间轴自动翻页，同时保留人工提前翻页能力。
- 含字幕版使用既有单行底部字幕规范；无字幕版不得保留隐藏字幕对象。

## QA

- 逐页核对图片顺序、字幕起点、字幕内容、标题/寓意卡语义和页面时长。
- 比较导演计划和 PPT 页 ID：必须逐项严格相等。机器 QA 必须明确报告 `director_shot_count`、`story_slide_count` 和 `total_slide_count`，不能只报告 PPTX 可打开。
- 渲染检查每一页，确认没有拉伸、黑边、裁掉主体、重复标题或重复寓意文字。
- 解包 PPTX 检查所有页面图片、配乐和自动翻页关系均已内嵌；不得出现视频、GIF、外链关系或 `PPT动态素材` 依赖。
- 在 WPS 或 PowerPoint 做一次从头自动播放抽查，确认音乐连续、页面按时翻页且返回页面不会出现视频播放壳。

## 正式构建与打包顺序

1. 由 `shot_storyboard_pipeline.py compile` 生成同源 `static_ppt_plan.json` 与 `shot_storyboard_compile_receipt.json`；不要从目录重新拼页。
2. 用 `build_static_story_ppt.mjs` 按同一计划各构建一次含字幕版和无字幕版。
3. 对两份 PPTX 分别运行 `finalize_static_ppt_runtime.py`，写入逐页自动翻页和同一份内嵌配乐。
4. 用 `validate_static_ppt_plan.py --director-plan ... --ppt-plan ... --pptx 含字幕.pptx --pptx 无字幕.pptx` 一次校验完整双版。
5. 调用 `product_package.py` 或 `story_workflow.py product-package` 时，同时传：

   ```text
   --director-plan
   --shot-storyboard-compile-receipt
   --static-ppt-plan
   --static-ppt-with-subtitles
   --static-ppt-without-subtitles
   ```

   五项只允许全给或全不提供。正式 Codex 原生生产必须全给；打包器只复制并复核封存双版，不能调用旧的逐句/图片目录 PPT 生成分支。
6. 将打包器输出的 `static_ppt_delivery_receipt.json` 以 artifact ID `static_ppt_delivery_receipt` 登记到 `story_run.json`。最终 `finalize` 会重新校验客户目录里的两份 PPTX、编译回执、导演计划、页面清单、配乐和自动翻页哈希。
