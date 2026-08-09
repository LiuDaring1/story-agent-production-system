from __future__ import annotations

from pathlib import Path


STYLE_PRESETS: dict[str, dict[str, str]] = {
    "3d_cartoon": {
        "label": "3D卡通",
        "goal": "16:9、明亮温暖、儿童友好的 3D 卡通分镜图片",
        "prompt": (
            "高质量 3D 卡通儿童故事视觉，圆润可爱的角色比例，柔和体积光，"
            "干净明亮的色彩，材质细腻但不过度真实，适合儿童观看。"
        ),
    },
    "chinese_2d_storybook": {
        "label": "中国风2D绘本",
        "goal": "16:9、明亮温暖、儿童友好的中国风 2D 绘本分镜图片",
        "prompt": (
            "中国风 2D 绘本卡通，带一点国画水彩和工笔设色的感觉；线条温柔清晰，"
            "人物表情可爱克制，色彩以温暖米白、青绿、朱红、黛蓝和木色点缀；"
            "乡村屋舍、灶台、田埂、竹篱、水缸、卷轴等元素要有中国民间故事气质。"
            "避免厚重写实、阴暗古装剧质感、过度 3D 塑料感和网游仙侠风。"
        ),
    },
    "ink_wash_picturebook": {
        "label": "水墨绘本",
        "goal": "16:9、明亮温暖、儿童友好的水墨绘本分镜图片",
        "prompt": (
            "轻水墨儿童绘本风，宣纸肌理、淡墨轮廓、浅彩点染，留白清爽，"
            "角色仍然要亲切可爱、动作读得清楚；适合神话、民间故事、古代寓言。"
            "避免灰暗、抽象、成人艺术海报感和难以看清表情的过度写意。"
        ),
    },
}

DEFAULT_STYLE_KEY = "3d_cartoon"
CHINESE_STYLE_STORY_TYPES = {"民间故事", "神话故事", "成语故事", "历史故事"}
CHINESE_STYLE_KEY = "chinese_2d_storybook"


def image_style_options() -> list[str]:
    return ["自动", *[preset["label"] for preset in STYLE_PRESETS.values()]]


def resolve_image_style(style: str | None = None, *, story_type: str = "", story_title: str = "", story: str = "") -> dict[str, str]:
    requested = (style or "").strip()
    if not requested or requested == "自动":
        key = infer_image_style_key(story_type=story_type, story_title=story_title, story=story)
    else:
        key = _style_key_from_label_or_key(requested)
    preset = STYLE_PRESETS.get(key, STYLE_PRESETS[DEFAULT_STYLE_KEY]).copy()
    preset["key"] = key
    return preset


def infer_image_style_key(*, story_type: str = "", story_title: str = "", story: str = "") -> str:
    if story_type.strip() in CHINESE_STYLE_STORY_TYPES:
        return CHINESE_STYLE_KEY
    source = f"{story_title}\n{story}"
    chinese_markers = (
        "民间故事",
        "神话",
        "天帝",
        "姑娘",
        "田螺",
        "灶",
        "水缸",
        "村",
        "从前",
        "古时候",
        "天上",
        "仙",
        "书生",
        "农夫",
        "皇帝",
    )
    return CHINESE_STYLE_KEY if any(marker in source for marker in chinese_markers) else DEFAULT_STYLE_KEY


def _style_key_from_label_or_key(value: str) -> str:
    if value in STYLE_PRESETS:
        return value
    for key, preset in STYLE_PRESETS.items():
        if value == preset["label"]:
            return key
    return DEFAULT_STYLE_KEY


def story_lines_from_text(story: str) -> list[str]:
    return [line.strip() for line in story.splitlines() if line.strip()]


def build_children_story_image_request(
    *,
    story_title: str,
    story: str,
    source_path: Path,
    storyboard_path: Path,
    image_dir: Path,
    slug: str,
    short_slug: str,
    skill_path: Path,
    manual_lines: list[str] | None = None,
    full_auto: bool = False,
    extra_requirements: list[str] | None = None,
    notes_text: str = "",
    staging_note: str = "",
    story_type: str = "",
    image_style: str | None = None,
    pacing_report_path: Path | None = None,
    pacing_draft_path: Path | None = None,
    pacing_summary: str = "",
) -> str:
    lines = manual_lines if manual_lines is not None else story_lines_from_text(story)
    style = resolve_image_style(image_style, story_type=story_type, story_title=story_title, story=story)
    manual_note = ""
    if len(lines) > 1:
        source_label = "工作台/Agent" if full_auto else "工作台"
        manual_note = (
            "\n## 用户预切分意图\n\n"
            "用户在故事原文中保留了换行；请优先把每个非空行当作一个候选镜头/分镜边界。"
            "如果某一行过长或过短，可以在给出分镜表时说明并微调，但不要忽略这个预切分意图。\n"
            f"{source_label}已先把这些非空行写入：{storyboard_path}\n"
        )
    confirmation_rule = (
        "2. 当前是全自动 Agent 模式：先生成分镜表和视觉圣经并保存，不要停下来等待用户确认；"
        "如果发现任务规则冲突，写 blocker 文件说明，不要假装完成。"
        if full_auto
        else "2. 先给出分镜表，让我确认；如果我明确说全自动，才跳过确认。"
    )
    extra_block = ""
    if extra_requirements:
        extra_block = "\n## 本期硬性画面要求\n\n" + "\n".join(f"- {item}" for item in extra_requirements) + "\n"
    notes_block = ""
    if notes_text.strip():
        notes_block = "\n## 用户备注\n\n```text\n" + notes_text.strip() + "\n```\n"
    staging_block = ""
    if staging_note.strip():
        staging_block = "\n## Agent 暂存说明\n\n" + staging_note.strip() + "\n"
    pacing_block = ""
    if pacing_report_path or pacing_draft_path or pacing_summary.strip():
        pacing_lines = [
            "\n## Grok Video 1.5 分镜节奏建议",
            "",
            "本期按 Grok Video 1.5 图生视频能力设计分镜：单镜头支持 1-15 秒，默认 8 秒，画面为 720p、16:9；不要超过 15 秒。",
            "用户的手动换行是候选镜头边界，不是必须逐行出图的硬规则；请结合节奏分析和叙事连贯性给出分镜表。",
            "",
            "- 推荐单镜头时长：约 6-12 秒；内容需要时可落在 1-15 秒范围内，但不能超过 15 秒。",
            "- 连续同场景、同动作、同对话目的的短句优先合并。",
            "- 短句如果跨场景、跨角色目标、情绪转折明显，则保留独立镜头。",
            "- 12-15 秒的镜头可接受，但图生视频提示词必须写清镜头内节奏；超过 15 秒的段落必须拆分。",
            "- 图生视频提示词允许写 15 秒内的镜头调度，例如“先中景建立关系，再轻推近到表情特写”，不要为了一个句子内的景别变化拆成多张图。",
        ]
        if pacing_summary.strip():
            pacing_lines.extend(["", f"节奏分析摘要：{pacing_summary.strip()}"])
        if pacing_report_path:
            pacing_lines.append(f"节奏分析报告：`{pacing_report_path}`")
        if pacing_draft_path:
            pacing_lines.append(f"建议换行草稿：`{pacing_draft_path}`")
        pacing_lines.append("")
        pacing_block = "\n".join(pacing_lines)
    return f"""# Codex 出图任务：{story_title.strip() or slug}

请使用这个本地流程说明：
[$children-storyboard-images]({skill_path})

## 工作目标

把下面的儿童故事文本做成 {style["goal"]}。

## 画面风格

- 本次选用：{style["label"]}
- 风格说明：{style["prompt"]}
- 视觉圣经必须沿用这个风格写角色、服装、道具、场景、光线和色彩；如果故事有中国民间/神话/历史元素，优先保留中国绘本气质，不要自动退回通用 3D 卡通。

请按 skill 的规则执行：

1. 先阅读故事，拆成适合视频节奏的镜头。
{confirmation_rule}
3. 确认后先建立视觉圣经：固定主角长相、服装、道具、场景、光线和整体风格。
   - 视觉圣经必须记录每个角色的物种、颜色、关键外形特征与正常解剖约束（四足动物四条腿，嘴必须位于头部而非鼻子/象鼻等部位）。
   - 分镜表每一镜增加“在场角色 / 明确不在场角色 / 本镜头已知信息”三项。对白里提到某角色，不等于该角色物理在场；角色尚未听到的信息不得提前表演。
   - 为每个反复出现的角色建立稳定 `appearance_id`，固定脸部特征、花纹、服装主色/款式和饰品；后续镜头必须复用同一 id 和最近一张已通过参考图，不得重新随机捏角色。
   - 把机器可读分镜计划写入 `{image_dir.parent / (slug + '_storyboard_plan.json')}`。每镜必须包含 scene、story_text、narrative_function、shot_size、focal_character、visible_characters、excluded_characters、continuity_group、appearance_ids、visual_description；story_text 与锁定分镜逐行一致。
   - 先检查叙事覆盖：每个唱歌/发言/关键动作/受挫反应角色都要有焦点镜头；同一场比赛要有全景建立、表演者中近景、评委反应、受挫者反应等合理景别变化，不能所有角色都和主角挤在同一种双人中景。
4. 出图不要人为限制总张数，也不要把“每批 8 张以内”当作业务规则。确认后应按完整分镜连续生成到本故事全部镜头完成；如果工具、网络或工程中断，再根据目标目录里已经存在的 `{slug}_scene_XX.png` 从断点继续。
5. 不要一次性生成多宫格/联系表作为最终图片。如果模型先生成了联系表，只能把它当作风格母版或审查参考，最终仍然必须整理出一张张独立的 16:9 图片。
6. 不要生成无关文字、水印、字幕、UI 或二维码。但如果分镜表明确要求画面中出现中文标题、匾额、书页、卷轴文字或结尾文字，必须把这些中文作为画面内容由图像模型直接生成出来。禁止用本地脚本、Pillow、HTML/SVG、截图、局部贴片、后期覆盖文字或任何非生图方式补字；那不算合格成片。生成后要核对指定文字是否自然融入书页/卷轴/牌匾等画面材质、是否出现、是否位置正确；如果缺字、错字、乱码、留白或像后期贴片，丢弃该图，用 Codex 图像生成重新生成该镜头。
   - 如果故事里有《故事名》这样的标题镜头，分镜表必须明确要求画面内直接生成中文标题，不能后期加字。
   - 如果结尾有“告诉我们”“道理是”等总结句，分镜表必须明确要求画面内直接生成核心道理中文文本，不能后期加字。
   - 如果故事开头只是“我是某某姐姐/哥哥/老师”这类旁白自称，默认不要把讲述者画进分镜，除非用户或任务书明确要求出现。
7. 每张最终图片都必须保持同一个主角形象、同一套道具样式、同一个故事世界。发现角色或道具漂移时，先停下来修正提示词，不要继续往下生成。
   后续镜头必须把风格锚点和同一角色最近一张已通过图片作为参考图生成；禁止只复用文字 prompt 后独立随机生成。
   每张图还要逐张自检：角色数量、腿/手数量、五官与嘴的位置、物种和颜色、该镜头应出现/不应出现的角色；任何一项异常都必须重生，不能交给视频阶段碰运气。
8. 确认后再批量生成图片。
9. 出图后把最终选中的图片整理到下面这个文件夹，使用稳定文件名：

```text
{image_dir}
{slug}_scene_01.png
{slug}_scene_02.png
...
```

禁止把 `generated_xxx`、`generated_{slug}` 或任何自建临时目录当作最终交付目录；这类目录只能作为临时暂存。即使图像工具先把图片保存到临时目录，也必须在任务完成前把最终选中的图片复制/重命名到上面的 `images` 目录，并严格使用 `{slug}_scene_XX.png` 命名。没有完成这一步，就不算完成出图任务。

10. 同时把最终确认的一行一镜头文本保存到：

```text
{storyboard_path}
```

这个文件要求每个非空行就是一个镜头，顺序必须和图片一致。后续工作台/Agent 会用它生成图生视频任务。
{manual_note}

11. 继续按 skill 保存图生视频提示词文件：

```text
{image_dir / (slug + "_flow_video_prompts.md")}
{image_dir / (slug + "_flow_video_prompts.csv")}
{image_dir / (slug + "_flow_clip_names.csv")}
```
这些图生视频提示词必须是一镜一条、按本镜头故事情节手写，不能批量套用同一段模板。图生视频已经有当前图片作为视觉约束，`prompt` 只写动作表演、表情变化、关键道具运动、镜头运动和少量禁止项；不要复制文生图视觉圣经、服装细节或画风长描述。后续工作台会优先读取这些 `{slug}_flow_video_prompts.*` 文件，缺失时才退回自动推断。
CSV 至少包含 `scene,story_text,visual_description,prompt` 四列，其中 `prompt` 就是后续图生视频 API 和审核页直接使用的最终提示词。提示词建议以“参考当前图片”开头，然后写本镜头的具体动作，例如“参考当前图片，年轻人弯腰拾起田螺，水面泛起涟漪，镜头轻微推近。保持角色和场景不变，不新增文字或水印。”不要以“画面保持某风格、角色动作自然克制、镜头缓慢推进或轻移”这类通用模板开头；角色/服装/场景一致只需一句短约束。保存前请自检相邻镜头的 `prompt`，如果多条只有故事文本/画面描述不同而动作提示相同，必须重写。
{pacing_block}{extra_block}{notes_block}{staging_block}
## 输出目录

```text
故事原文：{source_path}
分镜文本：{storyboard_path}
最终图片：{image_dir}
故事 slug：{slug}
短名：{short_slug}
```

## 故事原文

```text
{story}
```
"""


def build_children_story_handoff(request_path: Path, *, full_auto: bool = False) -> str:
    if not full_auto:
        return (
            "请读取并执行这份儿童故事出图任务：\n"
            f"{request_path}\n\n"
            "请使用 children-storyboard-images 流程。先给我分镜表确认；"
            "我确认后再生成图片，并把最终图片、分镜文本和图生视频提示词保存到任务里指定的目录。"
        )
    mode = (
        "当前是全自动 Agent 验收模式，请跳过人工确认，先写分镜表和视觉圣经，再连续生成完整图片。"
    )
    return (
        "请读取并执行这份儿童故事出图任务：\n"
        f"{request_path}\n\n"
        "请使用 children-storyboard-images 流程。"
        f"{mode}"
        "并把最终图片、分镜文本和图生视频提示词保存到任务里指定的目录。"
    )


def build_release_assets_agent_prompt(handoff: Path) -> str:
    return f"""请完整执行第 12 步发布视觉定版任务，不要只总结任务书：
`{handoff}`

执行原则：
- 以任务书里的规则、尺寸、路径、文字要求为准；这是已在工作台验证过的发布视觉流程。
- 使用 Codex 原生 imagegen 生成发布底板、主账号 16:9 背景、统一故事框等位图素材。
- 禁止用 Pillow/HTML/CSS/脚本从零画最终图或后期补底板文字；本地代码只允许裁切、拼接、尺寸整理、透明通道和 QA。
- 生成后必须把文件保存到任务书指定的绝对路径，文件名完全一致。
- 生成素材后运行或触发旧流程里的 QA/预览定参步骤，查看预览图，判断人物大小、位置、抠像边缘、故事框、字幕、Logo、背景虚化和手部裁切。
- 必须把最终布局和抠像参数写回 `04_发布视频/keying/keying_preset.json`。
- 如果当前环境缺少 imagegen、图像查看或运行命令能力，请在任务书同目录写 blocker 文件说明原因，不要假装完成。

完成后用简短中文列出：实际生成/修改的文件、是否写回 keying_preset、是否还需要重跑预览。
"""


def build_release_preview_agent_prompt(handoff: Path, approval_path: Path) -> str:
    return f"""请完整执行第 13 步发布预览审查，不要只总结图片：
`{handoff}`

执行原则：
- 先阅读反馈文件和预览索引，再查看总览拼图与关键单帧。
- 使用 Codex 图像理解能力判断主账号和宝库号是否适合进入正式生成。
- 重点检查：人物大小和位置、手部/头发/裙摆抠像边缘、故事框遮挡、字幕、Logo、水印、宝库号安全区、背景虚化和画面留白。
- 如果需要调整，直接修改 `04_发布视频/keying/keying_preset.json`，并在回复中说明 Agent 需要重跑本阶段。
- 如果已经合格，写入 `{approval_path}`，JSON 内容至少包含 `approved: true`、`reason` 和 `checked_files`。
- 如果无法看图或无法判断，请写 blocker 文件，不要假装通过。

完成后用简短中文说明：通过/需重跑、修改了哪些参数、approval/blocker 文件路径。
"""


def build_publish_package_agent_prompt(handoff: Path, project_dir: Path) -> str:
    return f"""请使用第 15 步交接中的候选帧和参考素材，生成完整发布物料：
`{handoff}`

本任务采用全自动 Agent 新交付规范；如果旧 handoff 仍写着“只做 4:3/不做文案”，以本任务为准。

执行原则：
- 逐项读取候选帧索引、真人参考、故事参考和旧 4:3 设计任务。
- 生成发布文案：`main/copy.md`、`library/copy.md`。每份包含标题、正文、话题建议；两个账号定位不同，不能机械复制。
- 封面必须按“母版 → 参考图编辑衍生”顺序完成，禁止六次互不相干的随机生成：
  1. 用本期真人帧、本期故事代表帧、历史已确认封面样例三张参考图生成主账号 `cover_4x3.png` 母版；样例只约束版式层级，不能带入旧故事人物或标题。
  2. 把主账号 4:3 母版作为必选参考图，通过图像编辑/扩图分别衍生主账号 `cover_3x4.png`、`cover_16x9.png`；重新排版安全区，但保持同一真人、同一故事角色、同一字体体系、同一色彩和装饰语言。
  3. 以主账号 4:3 母版为参考图编辑，移除真人并重排空位，得到宝库号 `cover_4x3.png`；不得从文字重新生成另一个版本。
  4. 再以宝库号 4:3 母版为必选参考图编辑衍生宝库号 3:4、16:9。
- 两个账号最终仍分别交付 `cover_3x4.png`、`cover_4x3.png`、`cover_16x9.png`，共六张。衍生是重新组织版式，不是机械裁切、拉伸或补边。
- 把生成血缘写入两个账号 covers 目录共同上级的 `cover_lineage.json`：记录每张图的 parent、parent_sha256、generation_mode（master/edit-derived）、reference_files 和当前 sha256；所有 edit-derived 的 parent 哈希必须与实际文件一致。
- 六张封面必须使用 Codex 原生生图能力生成或衍生；禁止用 Pillow/HTML/CSS/截图拼接/模板叠字作为最终封面。
- 品牌 Logo 不得由生图模型临摹或改造。六张图的上方中间预留干净安全区，不要自行生成花朵/图标/品牌字样；生产任务返回后主 Agent 会把配置中的原始 Logo PNG 原样叠加并记录哈希。这是唯一允许的确定性后期。
- 如果还没有参考帧，主账号优先使用第 13 步确认的发布预览帧；宝库号查看候选帧索引并选择故事动作/冲突帧，然后运行：
  `python3 story_workflow.py publish-package-project --project-dir "{project_dir}" --generate-covers --library-frame <编号>`
  之后继续执行新生成的 handoff 和封面任务。
- 主账号真人一致性优先：三种比例都保持参考帧的脸型、五官比例、发型、服装和姿态，不得换脸或卡通化真人。
- 版式沿用用户历史样例的平面信息层级：故事类型/标题为上半部主信息；时长、年龄、适用场景集中在同一简洁信息区；不要重新发明复杂木框、嵌套视频框或多层装饰风格。
- 生成后逐项确认两份文案和六张封面都落在目标路径。
- 如果当前环境缺少 imagegen 或无法查看候选图，请写 blocker 文件说明封面阻塞，不要假装完成。

完成后用简短中文列出：两份文案、六张封面、是否使用选帧重跑命令、任何需要人工后期的风险。
"""


def build_product_annotation_agent_prompt(handoff: Path, annotation_json: Path, demo_params: Path) -> str:
    return f"""请完整执行第 16 步资料包 Codex 前置审查：
`{handoff}`

执行原则：
- 先查看 handoff 中列出的示范视频预览帧，判断人物手部、底部边缘、字幕、左上角 Logo 是否协调。
- 如果示范视频参数需要调整，请写入 `{demo_params}`，字段使用 handoff 示例中的 `demo_person_crop_mode`、`demo_person_vertical_align`、`demo_person_crop_bottom_ratio`。
- 朗读标注必须按 handoff 引用的 `story-performance-script.skill` 和朗读标注请求从头精修。
- 优先输出 `{annotation_json}`，必须是 `product_package.py` 可读取的 JSON：数组或包含 `blocks` 的对象；不要使用自动草稿冒充精修。
- 标注内容要服务儿童朗读/表演：分段、语气、停顿、重音、动作提示清楚；对外售卖资料不得出现“绵羊姐姐”。
- 每段重音不能只标人物名词；要覆盖动作推进、情绪、反差、判断和转折。长段落应有足够重音组，避免只有一两个红字的机械标注。
- `marked_text` 必须逐字忠实于 handoff 中经过审核的客户正文，除主持人身份脱敏外不得润色、增删或替换代词。
- `notes` 要像有经验的幼儿园故事老师当面提醒朗读者：温和、自然、短句，先给角色当下的心情或画面，再给声音/目光/上半身动作。禁止“内容重点、节奏落点、情绪层次”等分析术语和干硬教案腔。
- 如果无法读取 skill 或无法完成精修，请写 blocker 文件说明，不要假装完成。

完成后用简短中文列出：annotation.json、demo_params.json 是否写入、需要正式打包时采用的参数。
"""
