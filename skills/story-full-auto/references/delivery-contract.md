# 交付合同

## 用户可见产物

- `主账号发布视频.mp4`、`宝库号发布视频.mp4`。
- 两个账号各自的标题、正文和话题建议。
- 两个账号各 3:4、4:3、16:9 三张审核通过的封面，共六张。
- 基础版：客户故事文稿、朗读标注、配乐、示范视频，以及清晰、无预模糊、无暗角、无角标/Logo/文字的 16:9 背景原图。
- 进阶版：基础版全部内容、含/无字幕背景视频，以及两份静态图片故事 PPT：含字幕版、无字幕版。两份均内嵌配乐并按权威音频时间轴自动翻页。
- 字幕产物矩阵：主账号、宝库号与示范视频都显示片头+正文+寓意全字幕；资料包含字幕背景视频只显示正文；含字幕 PPT 只在正文页叠字幕，但片头和寓意卡必须保留完整口播时长。
- 包装时长禁止“约”，按实际时长输出“3分钟”、“2分59秒”或“3分10秒”。

## 必要内部证据

- 确认文本、用户已换好行的字幕 TXT、调色绿幕视频和提取音频的路径及 SHA-256。TXT 只作制作输入，不进入客户资料包。
- `story_r2v_plan.json`、供应商任务回执和逐镜目标文件对应关系。
- `qa_music_report.json` 与当前输入哈希。
- `keying_search.json`、候选帧、锁定 RVM 预设及独立审核。
- `theme_assets_manifest.json`（`story-theme-assets-lightweight/v3`），绑定当前故事的 ImageGen 栅格背景、正式故事框几何参考 SHA-256、纯洋红故事框源图和由其色键派生的透明 PNG；不得包含 SVG/矢量派生、棋盘格源图、白底源图或直接透明生图。
- 两份 PPT 的逐页 `TITLE + 导演 shot_id + 可选 MORAL` 图片/字幕/时长清单、导演计划 SHA-256、内嵌配乐与自动翻页 QA；客户 PPT 不含逐句灌页、视频、GIF 或外链媒体目录。
- `shot_storyboard_compile_receipt.json` 与 `static_ppt_delivery_receipt.json`。前者证明 R2V/PPT 计划来自同一封存故事板，后者进一步绑定客户进阶版目录中含/无字幕两份最终 PPTX 的当前 SHA-256；两者必须互相绑定。
- `01_分镜与图片/semantic_cards/semantic_card_generation_receipt.json` 与同目录的 `semantic_card_motion_receipt.json`：前者证明片头/寓意文字与画面由 ImageGen 一体生成，后者绑定正式 provider/API 请求、任务 ID、视频哈希和首中末帧文字稳定性。
- 当前有效的独立审核文件、总成本和 `story_run.json`。
- 成本报告、QA 汇总与异常说明只允许出现在 `99_项目状态`，不得混入客户资料包。

## 完成门禁

`story_run.py` 内置 `CODEX_NATIVE_REQUIRED_ARTIFACTS` 作为新架构最低封口清单；命令行 `--require` 只能追加项目特有产物，不能删减内置清单。清单覆盖导演计划/审核、封存故事板/审核/编译回执、R2V 供应商与整组 QA/审核、音乐 QA、抠像锁定/审核、主题美术、PPT 交付、产品/封面/发布 QA 与审核、双账号视频和最终交付清单。

生产过程中按下列固定 artifact ID 登记，不自行改名：

```text
master_director_plan
director_plan_review
storyboard_manifest_sealed
storyboard_review
shot_storyboard_compile_receipt
semantic_card_generation_receipt
semantic_card_motion_receipt
r2v_provider_group_receipt
r2v_group_machine_qa
r2v_group_visual_review
qa_music_report
keying_preset_lock
keying_visual_review
theme_assets_manifest
static_ppt_delivery_receipt
qa_product_report
qa_publish_report
main_release_video
library_release_video
qa_release_report
final_delivery_review
final_delivery_checklist
```

- 文件存在不能替代 QA；独立审核必须 `approved: true`、分数至少 85、关键错误为空且 SHA-256 当前有效。
- 每个视频任务必须逐一匹配计划中的目标文件，不能用 MP4 数量代替。
- 任何输入或依赖哈希变化都会使对应下游产物失效，但不得使无关分支或上游自动重跑。
- 外部登录、CAPTCHA、支付、磁盘或密钥问题应记录为分支阻塞，禁止伪造成功。
- 使用逐镜故事板主链时，缺少 v3 发布美术清单、故事板编译回执或静态 PPT 交付回执中的任一项，`story_pipeline.py finalize` 必须失败。
