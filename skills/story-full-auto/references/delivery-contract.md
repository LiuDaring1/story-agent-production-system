# story-production/v2 候选交付合同

程序权威定义：`story_production_v2.py`；入口、QA、清单和封口消费同一版本。未带该版本的历史账本保留旧校验，不自动升级。

故事输入及内部绑定：confirmed_text、subtitle_txt、subtitle_srt、greenscreen_video、audio、final_word、finished_music、story_requirements、packaging_reference、packaging_prompt。包装参考和提示词默认来自源码内固定配置，无需用户逐故事提供；内部每项路径和 SHA-256 必须当前有效。故事信息与来源保存在 story_requirements.story_info，不新增合同。

Agent 交付：主账号和宝库号视频；基础版含原 Word、原音乐、Demo、背景原图；进阶版含基础版四项、含/无字幕背景视频、A 镜背景视频、PPT 素材 ZIP。ZIP 含 TITLE/shot_id/可选 MORAL 同源图、原旁白、原音乐、顺序/文字/权威时间/哈希清单。用户后来加入的文件不属于完成门禁，不要求目录总数等于清单数。

退出：音乐制作及音乐专门 QA、朗读标注、PPTX 制作/接收、独立封面、发布文案。无需这些执行、等待、审核或回执，不写伪造通过。最终媒体仍检查旁白+音乐或仅音乐角色、字幕、Logo、几何、完整时长及可解码性。

内置必需证据由 v1 基础集合去除 qa_music_report、static_ppt_delivery_receipt、qa_publish_report，并增加 managed_package_receipt、ppt_materials_receipt、packaging_prompt_receipt。保留导演、故事板、编译、语义卡、R2V、RVM、主题资产、客户媒体、发布包装、发布 QA、最终清单与独立审核。qa_product_report 使用管理文件复制 QA，不要求标注或 PPTX。

最终审核 bundle 覆盖当前清单、全部成员、发布 QA；分数 >=85、critical_errors=[]，并保留合并审核的音频、字幕、Logo、人物几何证据。文件存在/状态完成不能替代 QA。自有资料包 Word/音乐须与输入 SHA 完全一致，材料 ZIP 内容逐项校验。
