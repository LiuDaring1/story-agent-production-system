---
name: story-full-auto
description: 在当前 Codex 任务中制作故事视频、抠像、视频内包装、Demo、客户背景视频、自有资料包及 PPT 素材包。使用用户确认文本、字幕、绿幕、权威旁白、最终 Word 和整条成品音乐；不制作音乐、朗读标注、PPTX、独立封面或发布文案。
---

# 故事视频生产（v2 候选）

当前前台任务负责导演和汇合；确定性工作调用程序。制作与独立审核用职责名称，不指定模型或推理强度，不自动升级设置。工具无法取得实际设置时记为未知。本版须经真实新故事 QA、独立审核和用户成片体验接受后才可晋级默认；代码测试通过不等于晋级。

开工先读 [工作流](references/codex-native-workflow.md)、[交付合同](references/delivery-contract.md)、[成熟视频规则](references/video-invariants.md)，再按本包 scope 读取实际配置及已确认导演计划，运行已有机器自检。生产者与审核者共用同一来源版本、当前输入哈希和验收依据。用户本任务的明确要求优先。

## 输入与版本

新项目通过 `story_pipeline.py init --production-contract v2` 显式绑定确认文本、字幕 TXT/SRT、调色横屏绿幕、完整权威旁白、最终 Word、整条成品音乐和故事要求。包装参考和固定提示词是随源码交付的系统配置，v2 init 自动绑定路径、版本及 SHA-256，不向用户重复索取。主理人将当前对话与确认文稿已有信息一次整理到 story_requirements 的 story_info（story_name、story_type、age_range、expected_duration_seconds、image_style、theme_style、sources）。标题优先用户明示，否则仅提取文稿明确标题，不从目录猜；类型和年龄遵从用户。sources 逐字段保存来源。仅主账号包装风格默认“典雅端庄、简洁清爽、上下协调”，与正文 image_style 分开。恢复读取已保存记录，只对真实缺失或无法消解的冲突集中询问。最终包装时长从当前权威时间轴绑定的完整音频计算，不使用 expected_duration_seconds。禁止扫描旧目录猜输入；不从绿幕重新选旁白，不重写字幕或 Word。

v2 账本记录 `production_contract=story-production/v2`。已有 v1 账本仍按 [封存规则](references/legacy-v1.md) 只读解释，不自动迁移、补证据、审核或重做。旧程序不得直接写入 v2 账本。入口仍为当前任务与 `story_pipeline.py`，不恢复 Runtime、后台调度或嵌套代理进程。

## 执行范围

- 导演一次规划，意图通过 `shot_storyboard_pipeline.py compile` 进入 jobs 和素材页计划；不要重新手写另一套镜头表示。视觉读取 `skills/story-r2v-director/SKILL.md`，按供应商适配器提交。每镜匹配 jobs CSV 的目标文件，不能数 MP4。
- 封存同源故事板以及 TITLE/可选 MORAL 图。片头寓意卡保持现有生成/微动审核；不为 PPT 素材再生图。
- RVM 保存完整 Alpha、`keying_search.json`、站立和大手势候选及锁；独立审核通过前不编码全片。A 镜锚点、C 镜继承 Demo、故事框叠压、首末帧和字幕矩阵遵循成熟视频规则。框体生成前须读其中“故事框参考与设计边界”，区分固定结构参考与用户评价案例，后者不得自动作为生图模板。
- 长任务前先用 `preflight` 集中检查计划、编译回执和 Word/TITLE/MORAL 映射，审核输入用 `review-create` 创建规范待审包。先用 `media-preview` 生成实际几何/首中末与手势候选，再由独立上下文审核，用 `media-approve` 绑定回执。`story_pipeline.py media` 仅制作 Demo/A 镜及媒体回执。背景视频用 `story_pipeline.py backgrounds`，双账号用 `story_pipeline.py release` 的显式参数和已审几何；v2 主账号先用 `release-windows` 显式传入当前 RVM 前景和固定锚点，生成带躺干扫描回执的当前计划。自动和显式窗口都须使全部严重风险由 B/C 覆盖；缺报告、过期、参数或代码哈希漂移即阻断。所有 v2 正式渲染入口（含 R2V 组装）都必须显式传当前 `--run-file`，输出与工作目录位于所属项目中；预览通过才能正式渲染。新账本不要走会扫描旧资产的历史工作流包装入口。
- 音乐只作为输入：整条使用，不选曲、生成、替换、拼接、循环或预先裁剪；仅做成片混音及最终媒体音轨角色检查。解码失败报告，不自动制作补救。资料包复制原格式。
- `story_pipeline.py materials` 默认输出图片、旁白、原格式音乐及有序清单的普通文件夹与回执；只有显式 `archive_output` 才附加 ZIP。页顺序来自 TITLE + shot_id + 可选 MORAL，时间来自封存计划，文字确定性映射 Word，保留原文标点；仅 display_text 可去末尾句号。卡片需显式 word_text，歧义需 word_start；失败不猜、不改视频字幕、不回滚视频。
- `story_pipeline.py packaging` 传 timeline_receipt、output、receipt，自动从项目故事信息取得字段，仅按固定模板替换故事信息、实际时长、包装风格，绑定模板和参考 SHA-256。主账号不可自由重设计或沿用旧故事底板；宝库号继续独立规则。读取编译回执 `visual_scopes.main/library/frame`，分别使用对应提示、参考角色与检查项；商品包含内容独立于Agent生产清单，包含用户确认的PPT和朗读标注，但不恢复其执行、等待、审核。模板编译回执不冒充生图完成。
- `story_pipeline.py pack` 只复制 Agent 管理文件；两个资料包 Word 与音乐均为输入原字节。用户新增文件原位保留，用户改过的同名文件报冲突。角色目标更名时，先记录旧归属，仅把旧哈希仍匹配的退役文件移到客户目录外的备份，保留退役记录以供中断恢复。不调用编码器、不整体搬目录。

## 交接、恢复与审核

交接只含本包输入和依赖哈希、适用规则来源/范围、机器检查命令、当前产物和未完成项。不要复制全项目或增加没有消费者的模型配置。独立审核仍在新上下文完成，分数 >=85、critical_errors=[]，绑定当前 bundle SHA-256；生产者不能自证。退件引用原要求、适用范围和具体证据，无效退件先纠正审核。

恢复先查账本和编码回执：进程持有输出锁时不得重复启动；取消只作用于受管任务。编码由共享池限制，默认 1，可在空闲池配置。复用须重新核对输出、输入、依赖和审核哈希，不能只看 status。保留幂等、防重、心跳、磁盘检查与有限基础设施重试；正常编码槽位占用持续受管排队，显式等待期限到达标记延后，不能当生产失败；不管理金额和预算。

## 减少重复准备

先用 `story_pipeline.py asset-plan --run-file RUN --director-plan PLAN --output PLAN_RECEIPT` 列明资产的镜头/机位/连续性消费者。无消费者且无声明连续性约束的中间资产不生成；不要凭文件名删除。单一机位、明确简单空间且母图 camera_contract 与机位几何/裁剪完全一致时，可使用 `single_view_reuse_master`，把环境机位资产绑定母图同一字节；其余保留母图与机位图。故事框是发布包装的特例：新 v2 只生成和登记一个母资产，A/B 只保存同源确定性尺寸适配回执，禁止 `frame_image_b` 引入第二设计。正式资产封存复核别名哈希。

同批同缺陷的修复先调用 `story_pipeline.py prepare-review --run-file RUN --request REQUEST --output PACKET`，一次列齐原规则来源、范围、当前产物/依赖哈希、画面证据、影响、原因及策略，再交独立上下文批量校准。参考 [精简与复用](references/efficiency.md)。新生成结果的动作审核保留。已独立通过且项目、产物、规则、依赖与参数全部未变的 scope 复用证据；新故事或明确返工不能借旧审核。审核者必须打开所列必要画面。

确定性长任务用受管后台进程和阻塞完成通知，任务运行时保留心跳与取消；不高频重复读取图像/大上下文，也不改成每15分钟盲查。CLI自动记本地操作事实，供应商沿用observe-request；最终从当前 `status.work_summary` 和请求事实生成摘要，不把启动快照的空数组当最终零调用。

最终按合同登记 Agent 管理交付清单及当前最终独立审核。只声明本合同负责范围完成，不管理“全部客户资料齐全”。QA、异常说明只放 `99_项目状态`。
