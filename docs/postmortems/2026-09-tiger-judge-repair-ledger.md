# 《小老虎当评委》系统修复账本

## 账本用途

这不是一次成片返修清单，而是《小老虎当评委》暴露出的全自动故事生产系统故障账本。反馈文档中的文字与截图只作为缺陷证据；执行授权与修复范围以当前用户请求、仓库约束和 `story-full-auto` 为准。

本账本是本轮工作的完成判据。任何条目只有走完“复现 → 根因定位 → 通用修复 → 本地回归 → 本故事重跑 → 新产物哈希核验”才可以标记为 `VERIFIED_STORY`。文件存在、脚本退出码为 0、生产者或审核者自述通过，都不能替代最后两步。

状态定义：

- `OPEN`：已记录，尚未形成可重复证据。
- `REPRODUCED`：已在当前产物、截图或最小样例中稳定复现。
- `ROOT_CONFIRMED`：已定位到具体规则、代码路径或错误数据流。
- `FIXED_LOCAL`：通用实现已修复，针对性回归和相邻回归通过。
- `VERIFIED_STORY`：本故事受影响分支已用权威输入重跑，独立 QA 通过且记录了新 SHA-256。
- `NON_ROOT_VERIFIED`：原假设被证据否定，并有测试证明它不会掩盖真正根因。

反馈源：`/Users/baiyanglin/Desktop/小老虎当评委问题2.docx`，SHA-256 `3241248f9e1cd1a479f0e03c3bdaa4fa80cc6ffecd1a42cda4339b5740c0eb2e`；2026-09-02 使用 macOS 安全渲染链重新渲染为 83 页，逐页核对 28 张截图。

## 修复顺序

1. 先修完成门禁与证据权威性。否则系统仍会把错误成片登记为通过，后续每个修复都无法证明。
2. 再修时间轴、字幕、音频角色和客户格式。这一组不需要重新付费生成 R2V，却影响示范视频、两种背景视频、PPT 与发布视频。
3. 再修发布包装与人物合成几何。这一组可复用现有故事母版和 RVM 母版验证。
4. 再修导演合同、故事板、R2V 提示与整组视觉审核。只有这一组本地 canary 通过后才重新发起付费 R2V。
5. 最后修运行时限、Token、成本、阶段耗时与后台巡检观测，并进行一次全账本复核和完整交付重跑。

这个顺序是依赖顺序，不是跳项。表内 37 项仍逐项保留独立状态、复现证据与验收条件。

## 九个系统性根因

| 根因 | 系统层定位 | 传播到的条目 | 当前证据 |
| --- | --- | --- | --- |
| SR-01 导演合同只校验结构 | `story-r2v-plan` validator 接受 16 镜相同机位、相同轴线与相同运动，`shot_reverse_shot` 只是标签 | DIR-01–DIR-06, R2V-09, R2V-10 | 当前计划 16/16 的 `camera_plan.movement` 完全相同，validator 仍为 0 error/0 advisory |
| SR-02 参考资产默认把在场角色都塞进画面 | 镜头没有明确 `on_screen/off_screen`、舞台/观众席区域与持久位置，提示和参考图诱导强制同框 | DIR-06, R2V-01–R2V-06 | 青蛙表演段出现舞台侧老虎、桌边青蛙、双老虎与漂移桌子 |
| SR-03 R2V 审核缺少整组语义证据 | 机器 QA 只看文件/时长/编码；视觉审核能在克隆、穿模、漂移存在时自述“没有” | R2V-01–R2V-10 | `r2v_group_visual_review.json` SHA `ee3517...` 仍批准当前错误组 |
| SR-04 时间轴权威源被降级产物覆盖 | Whisper 对齐先成功，后生成的均分 fallback 被项目脚本重新读取并冒充确认时间轴 | R2V-07, R2V-08, SUB-01, SUB-02, SUB-04 | 权威对齐与 fallback 最大起点漂移 14.067 秒；修复后 `authoritative_timeline_receipt.json` SHA `c3bb73f0...8ad3f` 拒绝无回执 fallback |
| SR-05 字幕合成没有画布绑定回执 | 1280×720 字幕层被放进 1920×1080 画布左上角，最终没有保留可验证 render receipt | SUB-03 | 旧版差分中心约 x=641/y=659；v2 五点中心 x=958–960/y=998，输出 SHA `829aae7c...10f1`；独立审核待回传 |
| SR-06 交付合同只有字幕角色、没有音频角色 | 三条客户复用视频继承了含旁白总混音；打包器只复制/转码上游音轨；客户配乐格式未固定为 MP3 | AUDIO-01, AUDIO-02 | 旧三条残差约 0.996；v2 三条配乐相关系数 0.999903–0.999937、残差 0.000127–0.000194；AUDIO-01 新正式包固定交付 MP3，独立审核 100/无关键错误 |
| SR-07 正式发布美术合同存在旁路 | 强合同要求四张 ImageGen 面板和回执，但无 geometry 时可走 lightweight fallback；最终整张封面被当 plate | REL-01–REL-07 | 两账号 receipt 均为 `plate_package_render`，且 plate SHA 等于 `cover_3x4.png` |
| SR-08 前景母版质量与显示尺度被混为一谈 | “原生 4K Alpha 不缩放”错误延伸为合成时也不得做一次性等比适配 | REL-08, REL-09 | 1080 高画布中 `person_height=2160`、x=-218、y=0 |
| SR-09 最终账本只认文件和哈希，不认业务语义 | `story_run.finalize` 未解析审核内容；测试还允许多数 JSON 为占位对象；Token/成本/阶段耗时可为 null | RUN-01–RUN-04 及全部假通过 | 当前 `story_run.json` SHA `8b31ea...` 已完成但包含错误交付和空观测字段 |

## 37 项完整问题登记

| 顺序 | ID | 优先级 | 用户可见问题 | 已复现事实 / 根因位置 | 完成判据 | 状态 |
| ---: | --- | --- | --- | --- | --- | --- |
| 01 | RUN-01 | P1 | 10 小时冻结策略再次出现，且正常项目常常超过 10 小时 | `AGENTS.md`、`story_project.py` 与测试把 10 小时写成默认运行时限；不是本项目临时参数 | 将硬停止改成显式可配置的“付费审美返工截止”，默认不因墙钟 10 小时阻断确定性收尾；预算、取消、磁盘门禁仍有效 | `VERIFIED_STORY` |
| 02 | RUN-02 | P1 | 实际约 11–12 小时，但系统没有可信的阶段/等待/重试耗时账 | 当前 run 可见约 11.24 小时，只存粗粒度时间，不能区分运行、排队、登录、巡检和返工 | 每个工作包、供应商请求、审核和人工阻塞都有 start/end/wait/retry；汇总与事件可重算一致 | `VERIFIED_STORY` |
| 03 | RUN-03 | P1 | 用户只能从额度百分比估算消耗，Token 与实付成本没有落账 | 当前 `story_run.json` 的 `token_usage`、paid cost 等字段为空或不可结算 | 每次模型/付费请求记录 provider、request_id、模型、Token/时长、币种、估算与实扣状态；未知不得写 0 或 settled | `VERIFIED_STORY` |
| 04 | RUN-04 | P1 | 后台 15 分钟巡检是否导致灾难无法判断 | 哈希稳定且错误可重复；没有发现后台并发写坏文件。真正问题是语义门禁弱，后台模式只让前台大脑没机会临时纠错 | 用同一 fixture 证明前台/后台结果一致，且新门禁在两种模式都阻断同类错误；记录为“非主根因”而非继续归咎调度方式 | `NON_ROOT_VERIFIED` |
| 05 | DIR-01 | P0 | 小老虎发言、选手表演、虎妈妈说话都缺少应有近景 | 导演计划未把说话者、表演动作和反应镜头拆成可拍摄 beat；16 镜景别变化不足 | 对话/表演 fixture 必须产出说话者近景、反应镜头和必要全景，且由语义审核而非数量凑数判定 | `VERIFIED_STORY` |
| 06 | DIR-02 | P0 | 写了“正反打”却没有真实正反打，机位和轴线仍相同 | `shot_reverse_shot` 仅是字符串；validator 不校验 A/B 机位、视线、屏幕方向和反向镜头主体 | 正反打必须绑定互补 camera setup、主体、视线和 180° 轴；同机位冒充时验证失败 | `VERIFIED_STORY` |
| 07 | DIR-03 | P1 | 运镜反复“推进、拉远、推进、拉远” | 当前 16/16 movement 完全相同：“平稳推拉或轻横移，仅一个连续摄影机动作” | 导演计划按叙事意图选择静机、摇、移、推、拉或切镜；重复模板触发审核提醒并要求重审 | `VERIFIED_STORY` |
| 08 | DIR-04 | P0 | 相邻镜头同场景、同角度、同景别，形成明显跳剪 | 导演只规划单镜内容，没有相邻镜头的入/出构图、角度差、动作承接和剪辑理由 | 连续预览审核必须检查相邻构图、角度与动作承接；无合理变化的相似切换不得通过 | `VERIFIED_STORY` |
| 09 | DIR-05 | P1 | 一个长镜头承担上台、唱歌、嘲笑、生气、退场等多个节拍 | 以文本区间而非可视动作 beat 切镜；单镜内部顺势变形替代真正剪辑 | 每镜只有一个主叙事动作和可验证起止状态；多 beat 台词需拆镜或明确受控镜内调度 | `VERIFIED_STORY` |
| 10 | DIR-06 | P0 | 系统似乎强迫评委和表演者一直同框 | 计划缺少舞台/台下的区域拓扑与 `off_screen` 角色；参考图把双方都作为必须保留元素 | 每镜明确可见角色、不可见角色和区域；表演者近景不得凭空带入台下评委，评委反应镜头另拍 | `VERIFIED_STORY` |
| 11 | PPT-01 | P0 | PPT 第四张图中角色半身嵌进桌子 | 对应封存故事板本身已含碰撞，故事板视觉审核未拒绝，后续 R2V 继承 | 故事板逐镜做角色/桌面/舞台遮挡与接触关系检查；该 fixture 必须被退回，PPT 不得打包 | `VERIFIED_STORY` |
| 12 | R2V-01 | P0 | 老虎多次嵌进桌子或舞台，只剩半身 | 输入构图已碰撞，提示又要求同框；现有整组审核未识别 | 首/中/末帧和运动段均无人体/角色与桌、台穿插；视觉审核带证据帧和被审哈希 | `VERIFIED_STORY` |
| 13 | R2V-02 | P0 | 台下评委瞬移到舞台侧面 | 场景没有持久位置/区域约束；单镜参考优先于全局空间关系 | 角色区域状态跨镜封存；无明确走位镜头不得从观众席跳到舞台 | `VERIFIED_STORY` |
| 14 | R2V-03 | P0 | 同一画面出现两只小老虎 | 多参考/同框约束和生成漂移产生身份复制，QA 自述“无克隆” | 多时点检测角色实例数；同一身份超出计划数量即关键错误并阻断 | `VERIFIED_STORY` |
| 15 | R2V-04 | P0 | 青蛙从舞台猛跳到评委桌前，违背正在表演的内容 | 多 beat 长镜头和错误目标区域共同触发无因果位移 | 动作目标区域、起止位置和台词 beat 绑定；未计划的跨区动作即退件 | `VERIFIED_STORY` |
| 16 | R2V-05 | P0 | 桌子距离、位置和朝向不断漂移，像自己长腿移动 | 桌子未作为持久场景锚点；为了强制同框由模型重排场景 | 场景锚点 manifest 固定桌/舞台的区域、朝向与比例；跨镜漂移超阈值需视觉复核 | `VERIFIED_STORY` |
| 17 | R2V-06 | P1 | 麦克风、胡萝卜、法槌等道具突然出现或消失 | 道具由台词联想临时生成，没有逐镜持有者与连续性状态 | 道具必须来自计划清单，记录 owner/presence/entry/exit；未授权道具或状态跳变失败 | `VERIFIED_STORY` |
| 18 | R2V-07 | P0 | 画面比真实旁白提前约 7–14 秒，唱歌未完就进入嘲笑 | 成功 Whisper 对齐被后生成的均分 fallback 覆盖；S03 早约 7 秒，最大行起点漂移 14.067 秒 | 所有镜头、字幕、PPT 只绑定同一权威时间轴 receipt 和 SHA；存在成功对齐时 fallback 不得被选中 | `VERIFIED_STORY` |
| 19 | R2V-08 | P0 | 青蛙在旁白仍描述嘲笑时已经生气离场 | 同一时间轴优先级错误，加上单镜多 beat | 用权威音频对齐重组后，退场画面只能覆盖退场台词区间；边界误差进入可量化 QA | `VERIFIED_STORY` |
| 20 | R2V-09 | P1 | 虎妈妈和小老虎莫名旋转、方位掉头；该给虎妈妈近景时仍无近景 | 摄像机/角色朝向没有 180° 轴和屏幕方向合同，模型以旋转制造变化 | 母子对话 fixture 的屏幕方向稳定，换轴有建立镜头；说话者近景存在且无旋转代替切镜 | `VERIFIED_STORY` |
| 21 | R2V-10 | P0 | 单镜偶尔可看，但整段连续播放衔接很差 | 审核按单文件/抽帧判断，缺少整组低成本连续预览和相邻镜头证据 | 独立审核必须观看哈希绑定连续预览，并逐一记录相邻切点；关键连续性错误为空 | `VERIFIED_STORY` |
| 22 | DEMO-01 | P1 | 示范视频右上角官方 Logo 缺失 | 配置默认 `include_demo_logo=true` 且 Logo 文件存在，但项目 precheck 写“未传入”，非合同路径又把 Logo 当可选 | 当配置要求 Logo 时，缺输入或最终帧未检测到 Logo 都必须失败；首中末帧验证安全区 | `VERIFIED_STORY` |
| 23 | SUB-01 | P0 | 用户观察到“漏了一句话” | SRT 文本实际完整 62 行；视觉上的漏句是错误时间轴让一句在错误时刻显示，属于错位而非文本丢失 | 精确文本行数/顺序/规范化内容全等，同时每行时间覆盖权威对齐；两者分别报告 | `VERIFIED_STORY` |
| 24 | SUB-02 | P0 | 示范视频从某句开始字幕一直对不上 | 项目临时 `build_confirmed_srts.py` 读取均分 fallback 并标成 confirmed | 示范 SRT 构建只能消费权威 receipt；首段、错位起点和末段均做音频抽检/边界统计 | `VERIFIED_STORY` |
| 25 | SUB-03 | P0 | 资料包含字幕背景视频的字幕跑到画面偏左上位置 | 1280×720 overlay 未按 1920×1080 输出画布生成/缩放，且无 render receipt | overlay receipt 绑定目标宽高、字体、safe area、合成命令与输出哈希；字幕中心/底边落入目标安全区 | `VERIFIED_STORY` |
| 26 | SUB-04 | P0 | 资料包背景视频字幕同样完全不同步 | 与 SUB-02 共用错误 fallback 时间轴 | 客户含字幕背景视频与示范视频共享同一权威正文时间轴哈希；随机和边界采样均通过 | `VERIFIED_STORY` |
| 27 | AUDIO-01 | P1 | 客户收到的故事配乐是 M4A，不够通用 | 音乐 QA 把 M4A 标为 authoritative，打包器保留输入后缀；虽有 MP3 convenience 文件却未选择 | 客户包固定交付 MP3；内部可保留无损/高效母版，receipt 同时记录源母版与客户 MP3 哈希 | `VERIFIED_STORY` |
| 28 | AUDIO-02 | P0 | 三条供客户复用的视频都带主持人人声，无法二次使用 | 三条视频解码 PCM 与 `audio_mix.m4a` 完全一致；交付政策未定义音频角色，打包器盲目复制上游音轨 | 含字幕、无字幕、A 镜无人物三条客户视频均为“配乐有、旁白无”，不是静音；音频角色 QA 以语音/音乐 stem 与哈希证明 | `VERIFIED_STORY` |
| 29 | REL-01 | P0 | 正式成片没有真正调用 ImageGen 生成发布包装 | 正式路径在缺 geometry 时允许 lightweight 分支，不要求 ImageGen 请求/任务回执 | 正式发布必须有当前故事、当前几何、当前提示的 provider receipt；缺失即阻断 | `VERIFIED_STORY` |
| 30 | REL-02 | P0 | 主/宝库号需要的四张独立包装面板根本没有生成 | 强合同虽定义四面板，但不是 `finalize` 的必要 artifact，theme manifest v3 只登记背景和故事框 | 新增发布包装 manifest/receipt，完整绑定主/宝库 × 上/下四面板、源图、几何与 SHA | `VERIFIED_STORY` |
| 31 | REL-03 | P0 | 程序化占位包装被当作正式可交付素材 | `release_video.render_static_assets` 的无 geometry 分支会生成 fallback panels，机器 QA 未禁止 formal fallback | canary/debug 可使用占位，formal/customer 模式看到 fallback 标记必须失败 | `VERIFIED_STORY` |
| 32 | REL-04 | P0 | 最终把 3:4 封面当整张底板，中间挖洞塞视频 | 两份 release receipt 的 plate SHA 都等于 `cover_3x4.png` | 包装输入只接受声明角色为 panel/plate 的资产；cover 角色资产不得进入 release plate；角色与尺寸双校验 | `VERIFIED_STORY` |
| 33 | REL-05 | P0 | 主账号和宝库号沿用同一套包装 | 两账号 receipt 的 `plate_image` SHA 完全相同 | 两账号各自绑定独立四面板集合和不同视觉合同；除明确共享装饰外，成套哈希不得相同 | `VERIFIED_STORY` |
| 34 | REL-06 | P0 | 包装没有遵循上下两条独立资产和参考几何 | 参考几何只在强分支有效，fallback/whole-plate 路径可绕过 | 上/下条的尺寸、字段、安全区、开口和目标账号写入强制 geometry receipt；合成只能按锚点放置 | `VERIFIED_STORY` |
| 35 | REL-07 | P1 | “3 分钟、4–8 岁”等文字乱放、未居中、无层级 | QA 只验证文字存在/不越界，没有语义分组、中心偏移和版式重心 | 机器检查字段锚点、中心偏移、边距、层级；独立视觉审核比较目标参考与当前哈希 | `VERIFIED_STORY` |
| 36 | REL-08 | P0 | 主账号中间故事框/故事画面异常巨大 | 错误整板和框几何进入 plate path，未验证 story viewport 相对画布比例 | viewport 必须来自正式 geometry，比例/位置/safe area QA 通过；不得从封面透明挖洞推导 | `VERIFIED_STORY` |
| 37 | REL-09 | P0 | 真人巨大到只剩头部，整体像局部放大 | 1080 画布直接放入 2160 高 Alpha 人物；“母版不缩放”错误约束了显示合成 | 保留原生 Alpha 母版，合成时基于人物有效 bbox 做一次固定等比缩放与定位；首中末帧完整、安全、无动态缩放 | `VERIFIED_STORY` |

## 已确认应保留的正确行为

- PPT 的大体镜头划分可以作为重新导演时的语义参考，但不能沿用错误构图。
- 真人抠像边缘与画面本身本轮没有被用户判为问题；修复 REL-09 时不得破坏 Alpha 母版质量。
- 片头视觉设计及其结束时间与主持人口播一致，应保持同一权威时间轴。
- 小兔子段有一次“越过小老虎、只呈现舞台”的运镜相对成功，可作为 off-screen/过肩转单人镜头的正样例。
- 寓意卡动画、宝库号水印和约 30 秒后的模糊保护有效，包装重做时必须回归。
- 宝库号两枚官方防盗版 Logo 按既定全画面轨迹反向移动；用户未要求其避开角色或字幕，审核不得将该审美偏好升级为硬错。
- 字幕 TXT 的 62 行正文内容没有丢失；修复重点是权威时间轴和画布位置，不能擅自改文案。
- 所有客户可复用视频都应保留配乐并去除旁白，绝不是默认静音。

## 批次验收记录

| 批次 | 范围 | 开始时状态 | 本地测试 | 本故事重跑与新哈希 | 结论 |
| --- | --- | --- | --- | --- | --- |
| B0 | 账本、失败式完成门禁、证据 schema | 37 项已登记；25 项已定位根因，12 项已稳定复现 | 完整测试集 765/765 通过，另 1 项按环境跳过；控制入口与 Skill 校验通过 | 全量重跑回执、客户媒体、双版 PPT、资料包、双账号成片与独立审核均已登记；`story_run.json` 已 finalize | `VERIFIED_STORY` |
| B1 | SR-09 根因门禁、时间轴、字幕、音频、Logo | fallback 覆盖、字幕画布、客户旁白混入、Logo 缺失均已复现 | 时间轴、音频角色、MP3、Demo Logo、客户字幕几何与强制独立证据的 fail-before/pass-after 测试通过；邻接回归 106/106 | v2 四媒体机器 QA 通过；独立复核 92/100、关键错误为空；客户 MP3 已由 AUDIO-01 闭环，R2V 视觉边界也已由 B3 的 19 镜连续预览独立复核闭环 | `VERIFIED_STORY` |
| B2 | 四面板包装、账号差异、viewport、人物布局 | 整板/fallback 绕过、四面板缺失、账号复用、viewport 与 4K 人物显示比例问题均已复现 | 发行定向 61/61；用户范围澄清后水印正向回归 3/3；`story_run` 唯一红灯已修复并通过 | 四张 2304×888 ImageGen panel 、preview R5 审核 98/无关键错误；主片 `dfc999…` / 宝库 `d1c775…` / receipt `d4e274…` / 机器 QA `fb55e9…`；正式独立审核 98/无关键错误，SHA `7b01cd…` | `VERIFIED_STORY` |
| B3 | 导演合同、故事板、R2V 提示与整组视觉 QA | 旧 16 镜同机位/同运镜、伪正反打、强制同框、空间漂移、观众增殖与画外追镜已复现 | 导演计划当前 schema 0 错误/0 advisory；故事板、资产、provider 参考投喂、画外区域与固定人群门禁均通过；全量回归 759/759（另 1 项环境跳过） | 19 个新 R2V 任务全部哈希绑定；机器 QA 19/19；154.792 秒连续预览与切点证据经独立视觉复核 98/100、关键错误为空 | `VERIFIED_STORY` |
| B4 | 运行时限、Token/成本/阶段耗时、后台一致性 | 新项目与 pipeline config 默认 10h/36000，legacy loader 重写并可自动进入 best/accepted_with_exceptions；未知成本/Token 被展示为 0；原生账本缺请求级并发锁 | 默认墙钟截止退役；请求级时间/等待/重试/Token/估算/实扣状态和未知 `null` 门禁通过；线程及两独立进程并发回归通过；全量回归 764/764（另 1 项环境跳过） | 当前 19 个 R2V request_id 全部与 provider receipt 匹配；供应商未报实扣故总成本/剩余预算为 `null` 且新付费被阻断；B4 独立复核 100/100、关键错误为空 | `VERIFIED_STORY` |

### B1 调试与重跑证据（2026-09-02）

- 失败复现：`99_项目状态/product_assets/customer_media_receipt_pre_fix.json` 明确拒绝三条客户视频的 `audio_not_music_only`，旧字幕五个差分样本中心约为 x=640、y=659。
- 时间轴修复：新增 `story-authoritative-timeline/v1`，只接受已确认 Whisper 行级时间或原生合成回执；无回执均分 fallback 的测试先失败、修复后被拒绝。当前回执 SHA `c3bb73f09f03fa168b1c7d9c1d24047943e1cb02bac4c3280364102eae88ad3f`。
- 视觉重定时：`故事视觉母版_权威时间轴_v2.mp4` SHA `7698c8226f6dbede86f98a03bf5536d0a3464280db7dd3272211ffa38050131b`；旧 S03 起点 29.058 秒，新起点 36.420 秒，S04 从 40.474 秒改为 48.180 秒。
- 客户背景：含字幕 v2 SHA `829aae7cafa0520ea2818af82dfeb715dfdb6d9c1c5f775020999f40b6f010f1`；无字幕 v2 SHA `917deb92f08727f005ca7b05513679754290a4c53ee31c93c2ce886469e6403d`；两者配乐相关系数 0.999937、残差 0.000127。
- 字幕几何：5/5 样本通过，中心 x=958–960、y=998，底边 y=1019；编码差异导致的稀疏假阳性先由失败夹具复现，再改为选择最强连续文字带，旧中部字幕测试仍保持失败。
- A-only v2 SHA `42c666f858e89a8bea53f7b51956643b369b2c330435edc0e136e4870b0193b2`；配乐相关系数 0.999903、残差 0.000194，非静音、无旁白。
- Demo v2 SHA `eb857eaf4236adde142f9864bcefa9bee0bcd995c61793dcb0f3259159d4c709`；正式 0.5/45/90/130/179.5 秒帧重新抽取，官方 Logo 可见，前景从 3840×2160 固定缩放至 1920×1080（scale 0.5），字幕与权威 SRT 对应。音轨 RMS 0.0590，纯配乐拟合失败，符合“旁白 + 配乐”角色。
- 综合机器回执：`99_项目状态/product_assets/customer_media_receipt_v2.json`，`passed=true`、`critical_errors=[]`，所有输入输出均绑定当前 SHA-256。
- 独立复核：`99_项目状态/product_assets/customer_media_independent_review_v2.json`，SHA `416de6028ee254262503f6cafa8d709fc5607acc69300dadd2767fbca9d37b5d`，92/100、关键错误为空；复核者重新解码音频、重算哈希，并检查五张正式帧、官方 Logo、完整 62 行 SRT 与 0.5 倍人物显示几何。
- 尚未关闭：R2V 视觉内容与旁白的镜头边界尚未由新审核逐段观看；导演/R2V 重做及全交付仍在后续批次。客户 MP3 已在 AUDIO-01 独立闭环。

### B2 调试与重跑证据（2026-09-02）

- 唯一红灯：`tests.test_story_run.StoryRunLedgerTests.test_new_storyboard_pipeline_requires_bound_static_ppt_delivery_receipt` 补全新增发行回执门禁的测试 fixture，`tests.test_story_run` 及发行定向回归通过。
- 四面板：主上 `e9a0455f…`、主下 `5447ce63…`、宝库上 `8b1ce092…`、宝库下 `a88d41e9…`；均为 2304×888 独立 ImageGen 栅格资产，面板独立审核 97/无关键错误。
- 强合同与几何：`release_video.compiled.json` 、`main_package_spec.json` 、`main_package_generation_receipt.json` 、Demo render manifest 与 preview/formal geometry 均当前有效；正式 geometry 文件 SHA `aa50941ddb261b8c2258fb89ad8526aa0374b0ce9e2c0dbeea24b6bfca50c4df`。
- 预览审核：R5 证据 SHA `9e8f38fc4a1f1c77a04e5fa6749413cf40554e574eac9d554298f40b6619d291`，独立审核 SHA `9f187331313c3207f5358540ad68ddc8b4b7d4da199a389819cd11310f832fa0`，98/100、`critical_errors=[]`。round1/2/3 对宝库号双 Logo、片尾联系提示及“必须避开角色/字幕”的拦截超出用户要求，保留为被驳回的审核证据，不驱动产品修改。
- 正式成片：主账号 SHA `dfc999b1bffab28acb817b5535554a648c5eede4fbd8cdca4ec428d7a36bbd54`，宝库号 SHA `d1c7759f3b38288618e3f851a859b9d326e9745bfcf2cd4736a6ca8d31ec298d`；两者均为 1080×1440 / 25fps / H.264 + AAC，视频 179.880 秒、音频 179.861 秒、起点为 0，完整解码通过。
- 正式回执与审核：`release_render_manifest_both.json` 文件 SHA `d4e274a3f97e4b22c8760f209542051ad4a99a6f9764b837836dff9d549520c8`，`release_package_receipt_issues=[]`；机器 QA SHA `fb55e963345d77d83a0ccc98956f5bf53e72e75734ff4a7a9fc697cbe7c1dc5d`；正式独立审核 SHA `7b01cddd32f6a8c0bbf0720b242bcb498dc634086fcb41c0be379b3f485fca38`，98/100、`critical_errors=[]`。
- `story_run.json` 已登记当前 `release_package_receipt`、`main_release_video`、`library_release_video` 与 `qa_release_report`；旧成片未覆盖，`finalized_at` 保持为空，等待 AUDIO-01 → B3 → B4 → 全量重跑后再 finalize。

### AUDIO-01 调试与重跑证据（2026-09-02）

- 通用修复：客户资料包策略固定为 `.mp3`，非 MP3 母版必须先转码，`create_package_dirs` 对 M4A 等非 MP3 客户输入 fail-closed；定向回归 11/11，全量回归 726/726（另 1 项按环境跳过）。
- 版本化正式包：`05_产品素材准备/客户资料包_AUDIO01_v2/`；基础版精确 5 项、进阶版精确 10 项，两包均只有一份故事配乐 MP3，且没有 M4A；旧正式包 15/15 文件哈希保持不变。
- 音频绑定：内部 M4A 母版 SHA `dc00dcc162e6f15da6a6de526019edbdad99f6920e91e4f3033075605b8f79b5` 未改变；客户 MP3 SHA `1068d27a2e2e9987ef71a0ddd42b4889153ab03af4ae766092c735821e96d648`，两包一致；解码相关性 0.999993、残差 0.000013。
- 正式回执：`99_项目状态/product_assets/audio01_v2/product_package_receipt.json` SHA `2811f659aeca52e91ef82d85d76bf6bf047f6fc0e3e66f3a86e868213b3a95a1`，同时绑定内部母版、客户 MP3、15 个客户文件、上游客户媒体回执和新静态 PPT 交付回执。
- 机器 QA：`99_项目状态/qa_product_report.json` SHA `e4ee6626d724a28dab1feb3ced485decd7589e4575bf8bf90321175d3fa2756d`，`passed=true`、`critical_errors=[]`。
- 独立复核：`99_项目状态/product_assets/audio01_v2/product_package_independent_review.json` SHA `4f02db3479183df6768003b1bbc14fdce84c3650093cf26f0fa698cac27b0acc`，100/100、`critical_errors=[]`；复核者重算 30 个路径/哈希绑定、验证 6 个 Office ZIP、两份 18 页 PPT、三份 MP3 流及旧包未覆盖。
- `story_run.json` 已登记新的 `static_ppt_delivery_receipt`、`product_package_receipt`、`qa_product_report` 与 `product_package_independent_review`；`finalized_at` 继续保持为空，下一步严格进入 B3。

### B3 调试与重跑证据（2026-09-03）

- 导演合同：`master_director_plan_v4_r7.json` SHA `07dd074b137cabb252616f0c6ca78de220300faa2566e62f84602755e0a213b4`；当前 schema 严格验证 0 error/0 advisory，独立计划审核 98/100、关键错误为空，审核 SHA `0c8bf57fcbc97d30e8d8f589712690a2d14ac8245a713512648d476ce0c71f63`。
- 通用空间门禁：将场景拓扑、持久锚点、摄像机原点/目标/背景、角色在场/画外、固定人群数量和画外追镜防护写入 `story-r2v-director` schema/validator 与投喂编译器。A/B/C 仍只表示发行版式，不被误当为镜头类型；Logo/水印不参与空间审核。
- 用户反例进入不可污染的否定样例：舞台→评委桌/小老虎→观众席的朝向和前后关系、舞台方向看见观众时不得跳过前方评委席、旁边劝导镜头不得泄漏赛场家具、虎妈妈上衣完全扣合、禁止黄/琼脂色洗画面。
- 故事板：封存 manifest 文件 SHA `73f52df136fae35cb3ca39cd62636189aad6d9e188abd9d3e2849b9d0f79e428`，语义 bundle SHA `e12228ab2a0c2416ee637712a1bf5459a3b2d9e61d0335e25f0ae649837f6aad`；独立故事板审核 98/100、关键错误为空，SHA `d81b99e815689562abb5aef8a61742b277e3491f0107d06988b15b863892506e`。
- R2V 真实重跑：19 个不重复 provider task 全部下载且逐镜绑定，组回执 SHA `018abf7e5ed51f9b3265c2d9cd034dd196a1035a4fb11fe62e7a26a26ef9df9c`；供应商未暴露实扣成本，回执如实记为 `actual_cost_cny=null` / `provider_not_exposed_do_not_invent`。
- 视觉失败与返工未覆盖：保留 S01 观众多出小松鼠、S13 人群增殖、S18 画外追镜泄漏场景等 rejected 哈希证据；最终 S01/S13/S18 分别更换为新 provider task，而非修饰审核文本。
- 整组 QA：机器 QA 生产器缺失显式 `critical_errors=[]` 时被 `story_run` 拒绝；通用生产器修复后重生成，19/19 通过，SHA `b6652ed1c57fe74a457e11b8e45b1f550cdbd53be4bc5293004bb2c12e22a57c`。154.792 秒连续预览 SHA `e7d450c380feadbfd5c09e2e4c1314152327c10d0b65103415f090ea07276e40`；切点联系表 SHA `e0729adc16d8cff06dcf991f95740c2a7b7675f507ae7bf04112121d0fdc9013`。
- 独立 R2V 复核：重算当前 machine QA、组回执、19 个视频、连续预览和切点证据后为 98/100、`critical_errors=[]`，最终审核 SHA `8b01d438855dc11609f32e37f203d4694d3a414d089c4c95c379f30d81490ac9`。
- 回归：定向 R2V/投喂/story_run 测试通过；完整测试集 759/759 通过，另 1 项按环境跳过；Skill `quick_validate` 通过。`story_run.json` 已把三个当前 R2V 哈希登记为唯一有效版并将 `r2v_visuals=done`。R2V-07/08 随后在下述全量故事重跑中完成权威时间轴重组并升级为 `VERIFIED_STORY`。

### B4 调试与并发证据（2026-09-03）

- 运行时限：`story_project.DEFAULT_CONFIG`、`pipeline_config.json`、`AGENTS.md`、`story-full-auto` 与审核合同均改为“无默认项目墙钟完成时限”。legacy `ensure_manifest_v2` 不再把无来源的旧 manifest 强写为 10h/36000，也不因兼容加载注入 `accepted_with_exceptions`；显式传入的项目截止仍可选保留。
- 原生可观测账本：`story_run.py observe-request` 记录 provider、request_id、model、status、start/end/duration/wait、retry、Token status、币种和 estimated/actual cost status；工作包汇总可重算。未报告 Token/时长/成本必须保持 `null`，不再伪装成 0。
- 预算门禁：当前小老虎项目 19 个 R2V 请求的供应商实扣均未暴露，因此 `paid_total=null`、`remaining_hard_budget=null`、`can_start_paid_work=false`；已结算小计 0 不再冒充总成本。这不阻断使用已有资产做确定性收尾。
- 文件锁：对 `story_run.json` 的 init/record/request/finalize 读—改—写交易同时使用进程内 `RLock` 和跨进程 `flock`，再由同目录原子替换落盘。
- 15 分钟后台 fixture：线程夹具以及两个独立 Python 进程的前台 20 次 + 模拟 900 秒后台 20 次同账本写入均通过，最终 requests/events=40/40，无丢失、无损坏。该证据说明后台巡检本身不是小老虎视觉灾难的主根因；主根因是 B1/B3 已修复的语义与空间门禁。
- 定向测试覆盖未知值、实扣/Token 聚合、硬预算、旧 10h 迁移、显式 deadline 保留、线程和跨进程锁；完整测试集 764/764 通过，另 1 项环境跳过。控制入口 `story_pipeline.py --help/describe`、`story_run.py --help`、legacy 兼容入口 `story_agent.py --help` 均通过；`story-full-auto` quick validate 通过。
- 本故事 B4 证据 SHA `12dc5063f0fe3ad338bdec4af40d2e093bdd5a16303633dbe630f153b5d1e494`；独立复核重算 story run、provider receipt、5 个源文件、预览/切点和并发夹具后为 100/100、`critical_errors=[]`，审核 SHA `bc28fd36267cd482c22fa612a1631e65033881eddf3f0069afee2ce4b48ecef1`。

### 全量故事重跑与最终收口证据（2026-09-03）

- 严格顺序：唯一红灯复测通过后依次完成 B2 → AUDIO-01 → B3 → B4，最后才执行全量故事重跑；没有用后续产物倒填前置批次。
- 权威时间轴与视觉重组：全量 assembly plan SHA `0ad1b36cfd4de53113ce98ac0ebe923355b3912006530c0b4dca18e3283b50d3`；含正文字幕背景视频 SHA `15d3858f44bf3b8fc4af4182af4c5414cac512477f620ab6d1bc40ebafde69c9`；无字幕背景视频 SHA `d3c0fb695b17d95e3dcdd92e3564a986e35c6ae5ca37522a7d8fe4f2f0a638bf`。R2V-07/08 的唱歌、嘲笑、生气与退场边界均只消费同一权威时间轴。
- 静态 PPT：当前 plan SHA `acb0131f9d479c2e2ce9e9a38e21313e8f572122145647b80be14e23386fd63f`；含字幕版 SHA `77824016818946771b2f23d0187d599f12c0dfb1b8c58351da8a3c9638e141bb`；无字幕版 SHA `1dd134e671ee4060af61f92442022f716d990bc3b8e4d47b4d067c5cf340754c`；双版均为 21 页，交付回执 SHA `c6b7a5fae2f3a9e715c2062a3a48e03b8cc4cc490f6c48486c1cdcb60690178b`。用户指出的多行字幕旧版 SHA `d05eb3a3adbddb242569b47b9b8969c2ee0e46511cde9998045f7a95a39aa0e0` 已移入 `99_项目状态/rejected_evidence`；当前编译器强制去除 CR/LF，计划与 OOXML 双门禁要求每页恰好一个字幕对象、一个 `a:p`、零个 `a:br` 且文本全等，旧版在第 2 页被明确拒绝。
- 客户媒体与资料包：客户媒体回执 SHA `60787ea2bf8f59feb37ad52c6a2c8896d81292e661c5b613b875fc5b3ac7a124`；独立复核 96/100、`critical_errors=[]`，SHA `59358d353fa4a987ceda0188250ff28a54cd5e84553b5c60f676f25f46d67c51`。基础版精确 5 项、进阶版精确 10 项，客户音乐均为同一 MP3；资料包回执 SHA `35674b0ef59290edea4aa9a57f13f0148a2c4849f7a166e5e96b2112ce2373e0`，产品机器 QA SHA `df1c16939b7ae921daf4ddcd8db6bf9161a5f652742b69fa05398a3a18a03838`，`passed=true`、`critical_errors=[]`。
- 正式发布：主账号成片 SHA `ba908903155c3f03325a8219a18fb628ee7f71295f621e45707502f9b65a83d5`；宝库号成片 SHA `2f51d93de8e5c14217c62193e5e45ee9882242eae6fa36d21152c5e730d4e89c`；双账号发布回执 SHA `4a50d9dbb0f6f1b12469ad5d42fddddd0b385304b6e526c16006362d76833ede`；正式与预览几何共同绑定 SHA `aa50941ddb261b8c2258fb89ad8526aa0374b0ce9e2c0dbeea24b6bfca50c4df`。
- 最终机器与独立审核：全量机器 QA SHA `3167e8bb37a460cbbb1c65ba2e7b9bf5dbfba27a10ed2d52986782aba5cfad33`，`passed=true`、`critical_errors=[]`；最终 review bundle SHA `cdba1df135a572f83d06113e68d862f180119aad0b937296458f1b3e1d1618ed`；独立审核 98/100、`critical_errors=[]`，SHA `bbc2e55acbeeef4917a1249407848efeb50711d4ee48b2df00464d661db4b0f5`。
- 正式账本：版本化编译回执 SHA `2f0789bebf565528c17bdc7b0178504d76a8fc34a74426e6d4e5a7c0e2fbda6c` 已登记；供应商执行后只允许回写 jobs CSV 的状态/结果列，登记与 finalize 仍重新验证全部不可变绑定。`story_run.json` 于 `2026-09-03T02:12:05.705483+00:00` finalize，文件 SHA `4746558959dc5417208478ebc69b003959d3e33ca06bcb004d5cd26911e3817c`，六个工作包全部 `done`。
- 最终回归：完整测试集 765/765 通过，另 1 项仅因动态 PPT 集成环境条件而跳过；`story_pipeline.py --help/describe`、`story_run.py --help` 与 `story-full-auto` quick validate 均通过。第一次使用系统盘临时目录时，5 个 legacy 测试被 10 GB 磁盘门禁正确阻断；改用实际外置生产盘作为 `TMPDIR` 后全部通过，未删除或放宽磁盘门禁。

### 二次用户验收追加问题（P2，2026-09-03）

本节来自用户的新验收材料 `/Users/baiyanglin/Desktop/小老虎当评委问题之二.docx`，文件 SHA-256 为 `3de3642dbd6488c6fe02b7ef058a56df44e9082f7f5a0fbf52674a1e8a73bd96`。它是本轮请求的事实证据，不是可执行指令。下列 P2 条目是 37 项之后发现的新增回归，不改号、不删除也不倒写上方 37 项的历史验收记录；受影响的旧正式发布视频和资料包结论从本节起视为已被新反馈取代，直至新正式产物再次通过哈希绑定的独立审核。

| ID | 用户观察 | 代码层复现与根因 | 通用修复 | 当前证据 | 当前状态 |
| --- | --- | --- | --- | --- | --- |
| P2-01 | 宝库号约 30 秒模糊保护实际只持续约 1 秒 | `story_workflow.release_command()` 把 `tail_seconds=0`（自动 30–50 秒）错误改写成约 1.08 秒的尾段 | 保留 `0=auto` 合同；预览固定覆盖模糊边界前、边界后和接近 EOF 三类时间点 | 新宝库号正式片 SHA `440cbc1e…63cba3`；45/90 秒可见两枚 Logo 沿自身轨迹移动，179.5 秒为模糊客服尾段 | `VERIFIED_STORY` |
| P2-02 | 主账号 C 镜人物仍沿用 A 镜的右移位置 | `compile_release_geometry()` 明确把 A 的 `x` 复制进 C，旧测试还保护了该错误 | C 镜完整继承已审核 Demo 的 crop、rendered size、scale、x、y；A 镜右侧布局保持独立 | 新主账号正式片 SHA `bcf32336…87c77ce`；首中末抽帧显示 C 镜人物居中、A 镜保持右侧 | `VERIFIED_STORY` |
| P2-03 | 主账号 B 镜故事画面过大 | B 的默认框在多处重复写成 `[150,88,1620,911]`，偏离当前项目已确认几何 | 唯一常量固定为 `[356,180,1209,680]`，编译、CLI、工作流和 manifest QA 同时约束 | 当前双账号正式回执 SHA `e8130aa5…4d20f2f`，`release_package_receipt_issues=[]` | `VERIFIED_STORY` |
| P2-04 | 主账号末尾人物消失，故事框仍在 | RVM WebM 只有 `format.duration`、没有 `stream.duration`；探测器返回未知后没有人物尾帧安全延长 | 仅视频流文件可回退到 format duration；未知时补一帧；接近 EOF 的预览窗口向前平移而不虚构节目时长 | 主账号正式片 179.5 秒抽帧人物仍在；发布 QA SHA `347bdb37…40ffd3`、`critical_errors=[]` | `VERIFIED_STORY` |
| P2-05 | 客户故事文稿被做成 62 行无标点字幕稿 | 打包器直接把语义行列表逐行写成 Word 段落，回执 QA 又要求“一字幕行一段”；正式入口还优先取 manifest 的字幕 TXT | 资料包优先使用哈希绑定的确认 DOCX；按自然段和标点生成文稿，同时以规范化语义等价校验不漏文案 | `06_资料包` 基础版/进阶版文稿 SHA 均为 `bf4ab663…4476a9`；10 自然段，Word 安全渲染通过 | `VERIFIED_STORY` |
| P2-06 | 短暂手掌/前臂出框是否应判错 | 旧解释容易把任何肢体出框都升级为人物构图错误 | 保留既定合同：仅短暂手掌/前臂越界、躯干未裁切时不是根因；持续躯干裁切才触发 B/C 构图失败 | 正向行为回归测试保留；未添加“Logo 必须避让角色”伪规则 | `NON_ROOT_VERIFIED` |

本轮复现还暴露并修复了九个支撑性缺陷：DOCX 被当 UTF-8 文本读取；资产扫描擅自覆盖已锁定 `story_text`；主题 QA 在校验前改写已收据化图片并自造哈希漂移；Demo 预览路径写死到失效历史目录；发布时错误选用 155.32 秒正文音频而非 179.883 秒全节目音频；接近 EOF 仍强取 0.4 秒导致抽帧失败；宝库号误要求 `story_logo` 且执行不适用的 C 镜人物检查；新 `06_资料包` 已生成但 QA 仍绑定旧 `05_产品素材准备` 目录；发布 QA 缺失 `schema_version` 无法进入严格账本。对应修复均进入通用代码和回归测试，没有写死故事标题或项目路径。

当前预览审核包 SHA-256 为 `976ef1c20ffb03d67bb0d4a96d01401d0703ab63b0f393e76c53c35fe77b51e7`，包内所有文件当前性校验通过。机器证据为 `/Users/baiyanglin/Desktop/故事剪辑：小老虎当评委/99_项目状态/reviews/problem2_repair_preview_machine_qa.json`，SHA-256 `b144f73bf215b26383f116b99633f763f0dfaaefae5c1d2bf306dee229280e15`，`machine_checks_passed=true`、`critical_errors=[]`，并证明正式渲染门禁因缺少当前独立预览审核而保持关闭；报告明确写有 `formal_delivery_passed=false`。代码回归为 779/779 通过，另 1 项环境型动态 PPT 集成跳过；五个控制入口及 `story-full-auto` Skill 校验通过。

二次验收也已严格按上述顺序收口，未跳项。当前正式主账号/宝库号成片 SHA 分别为 `bcf32336a549ebd8a7c4fd480f60ac3d7f08ae3eebfea88d3ec03106687c77ce` / `440cbc1e8fcf8422e99977c08afe9bf624d21291df526f960df317c21163cba3`；基础版 5 项、进阶版 10 项均位于 `06_资料包`。客户媒体独立审核文件 SHA `dddf8d266908a9362b0f5416ef3509f94c3e6e2a09c443fe07677f180ef5afc4`，97/100、`critical_errors=[]`；最终交付独立审核文件 SHA `019833a1ec15fbcba767ef9caa9c27437995f4a660b0b68872d9c669a6862560`，97/100、`critical_errors=[]`。审核独立重算 review bundle 112/112 与 checklist 111/111 项，零漂移。`story_run.json` 于 `2026-09-04T01:42:05.024025+00:00` 再次 finalize，SHA `0e558cd9f25c7eb756a6185dfcfdd42b235759ee8b51971fc82e64ca997e9acc`。最终全量回归 786/786 通过，另 1 项环境条件型动态 PPT 集成跳过；控制入口、legacy 兼容入口和 `story-full-auto` quick validate 全部通过。

### 发布音轨追加问题（P3，2026-09-04）

| ID | 用户观察 | 代码层复现与根因 | 通用修复 | 当前证据 | 当前状态 |
| --- | --- | --- | --- | --- | --- |
| P3-01 | 主账号和宝库号发布视频都有口播，但都没有背景配乐 | 正式发布入口把完整口播直接当作最终音轨且未传入混合开关；宝库号渲染器即使收到开关也只映射口播；旧 QA 只确认“存在音轨”，没有确认音轨角色 | 正式发布统一要求口播与已审核配乐混合；宝库号执行同一合同；发布 QA 必须证明两种成分同时存在 | 修复回执 `99_项目状态/release_audio_fix_20260904/release_audio_fix_receipt.json`；当前主账号 SHA `a201dd4f…0165c`、宝库号 SHA `cfbb6a67…7a69b3`；发布 QA v3 通过；独立音轨审核 98/100、`critical_errors=[]`，SHA `e28b0d82…082d1` | `VERIFIED_STORY` |

本项修复只重建两条发布片的音轨，画面视频流沿用已验收版本；没有重新生成故事画面，也没有改变 Logo 轨迹、版式或前述空间规则。旧 P2 正式视频哈希作为历史证据保留，但从本节起不再代表当前交付文件。

最终交付独立审核已重新绑定当前 bundle/checklist，97/100、`critical_errors=[]`，SHA `e68f94a17c434c3572eb0df828aab5d023653b73988328070a25625631b84da6`。完整回归 790/790 通过，另 1 项环境条件型集成测试跳过；`story_run.json` 于 `2026-09-04T04:09:16.314027+00:00` 再次 finalize，SHA `356ca060d42c9255d04c6289f6ec7cf683940a355eddb7fa6ed8b7efb559fd92`。

## 最终收口检查

每次准备宣称“完成”前必须重新执行以下检查：

1. 本表恰好保留 37 个问题 ID，无删除、合并后消失或以“同类问题”代替。
2. 所有条目均为 `VERIFIED_STORY` 或有证据与回归支撑的 `NON_ROOT_VERIFIED`。
3. 每项都能指向复现证据、通用修复文件、针对性测试、本故事新产物及其 SHA-256。
4. 九个系统根因全部有 fail-before/pass-after 证据，不能只修项目目录里的临时脚本。
5. 正向行为回归全部通过，尤其是片头时长、寓意卡、宝库号水印/模糊、RVM 质量和 62 行字幕文本。
6. 最终独立审核在新上下文中完成，分数至少 85、关键错误为空，并校验被审产物当前 SHA-256。
7. 成本、Token、实际耗时、重试与尚未结算金额只写入 `99_项目状态`，不混入客户资料包。
