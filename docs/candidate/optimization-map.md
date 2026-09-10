# 候选优化改动与回归地图

本候选沿既有 v2 主线继续，不恢复音乐制作、朗读标注、PPTX、独立封面或发布文案生产。当前任务安排创意判断，模块执行，候选不自动晋级。

| 改动位置 | 实际调用 | 验证 |
|---|---|---|
| visual_scopes / story_visual_contracts | packaging → 三份提示及来源回执 → 面板审核；theme writer/handoff → 框体提示 | scope隔离、商品六项与外置职责、原生Alpha像素/合成、完整寓意正式拼装 |
| story_pipeline / story_final_media_qa | timeline → assemble → backgrounds → media → release → release-qa | 隔离短媒体及独立上下文审核；目录已执行；发布音频QA、最终独立审核及封口恢复尚未完成，见随附运行证据 |
| story_materials / production_v2 / managed_package / artifact_validation | 有序目录 → 成员哈希 → 拷贝QA → 最终清单/独立审核展开成员 | 未变复用、输入漂移拒绝、可选ZIP中断恢复、旧ZIP只读、Word/音乐字节不变 |
| asset_efficiency / storyboard_pipeline | 生成前消费者计划 → 正式资产封存 | 单视角母图同字节别名、复杂几何回退、未消费资产不阻塞封存 |
| review_preparation | 一份修复清单 → 按缺陷分组 → 独立scope证据复用 | 缺字段投喂前拒绝，项目/参数/规则/依赖/产物变更失效，保留生成后动作审核 |
| subtitle_layers / synthesizer / encode | 透明空白裁剪 → 同位置同时间叠加 → 可取消共享池 | 等设置逐帧比较，严格闭合字幕图过滤表达式复用，未知图仍保守，资源延后不记失败 |
| work_observation / run | 自动追加operation事实与原有provider请求 → status摘要 | 未知模型/推理/Token保持null，复用/返工/等待分开，不以空启动记录推断0请求 |

回归保护：主账号结构、ABC真人几何、RVM/Logo/字幕/音画、宝库中间横版及双Logo片尾模糊、Demo和含/无字幕背景、原Word和音乐复制。真实旧故事仅取证，所有实验输出在隔离目录。
