# 主链成熟功能覆盖（story-production/v2）

本表是离线验证索引，不代表新故事生产验收。完整日志与短媒体文件在本次独立交付目录的 evidence。历史 v1 测试仍随全量运行，但不把 v1 音乐/PPTX/封面职责重新放回 v2。

|成熟功能|正式入口及职责|验收证据|
|---|---|---|
|ABC|release-windows → release预览/正式 → release-qa；沿用build_abc_scene_windows及字幕边界|test_story_scene_windows、test_release_v2_compatibility、新ABC回归；42秒合成Alpha媒体实际A/B/C解码，实际全A故障检测；最终独立实看|
|抠像与人物几何|media-preview/media-approve/media/release；production_keying、release_geometry；完整RVM Alpha、原画布、大手势、A锚点及C继承Demo|test_keying_quality、test_rvm_keying、test_release_geometry；本轮短媒体使用合成Alpha，不声称神经推理验证|
|字幕|TXT内容/SRT时间独立绑定；透明RGBA一行字幕；主/宝库/Demo全口播，背景仅正文|test_subtitle_layers实际解码等价性；test_customer_media_policy相邻一帧且位置约束；test_sales_subtitle_policy|
|Logo|Demo/主账号既有Logo规则；宝库号两枚反向运动及片尾保留|test_release_geometry中countermoving_watermarks、single-logo合同；test_keying_quality Demo manifest|
|双账号包装|packaging固定参考/模板→release分别消费main/library范围；四张独立面板|test_packaging_defaults、test_visual_scope_contracts、test_release_package_v2；原固定资产随源码封存|
|音轨角色|用户整条成品音乐；最终混音及客户媒体QA；Demo/发布为旁白+音乐，客户背景为music-only|test_customer_media_policy（缺旁白、缺音乐、music-only）；test_release_geometry正式混音；不新增音乐专门QA|
|Word/音乐原字节复制|pack只管理Agent文件；用户新增文件保留、冲突不覆盖、TXT/SRT不进客户包|test_decoupled_production中的word_music_copy、user_edit、interrupted_repackage；不调用编码器|
|PPT素材导出|preflight/materials复用TITLE/shot_id/MORAL图片、原Word映射和权威时长；普通目录|test_decoupled_production compiler_export_and_actual_directory_manifest、material_text_exact_punctuation_and_order；新增集中缺项测试|
|客户背景与Demo|assemble/backgrounds/media分别守住原职责；canonical shot ID严格顺序/区间检查|test_customer_background_render、test_decoupled_production、test_r2v_body_audio_window；614815b等效补丁保留|
|审核/封口|record规范审核ID；current哈希和独立上下文；finalize累计绑定|test_story_run、test_release_package_v2、test_decoupled_production缺审核/自审阻断；本轮独立代码及媒体证据审核|
|恢复和防重复|status/encode-control→受管锁、结束事实回放、当前输入输出/依赖指纹|新增故障注入/短编码测试；首次、恢复、换源码目录、主账号变化的实编码次数前后测量|

不扩展的职责：已外置音乐制作、朗读标注、PPTX、独立封面和发布文案继续外置；Runtime、Supervisor、Dashboard、历史生产入口不恢复。镜头艺术表现不在本轮验收范围。
