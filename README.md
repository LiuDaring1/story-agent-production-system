# 儿童故事全自动生产 Agent

当前推荐入口是 `story_agent.py`。只投喂一段横版绿幕原片（允许包含重录），Agent 会用本地持久状态机续跑：整理最后一次完整口播、生成故事画面与视频、配乐、合成主账号/宝库号成片、生成两份文案、六张封面，以及基础版/高级版资料包。所有关键阶段都必须经过独立审查，分数不少于 85、无严重问题且审查哈希仍对应当前产物，才会进入下一阶段。

它不会自动上传或发布。默认费用软上限 50 元、硬上限 100 元，运行期限 10 小时；达到硬上限、磁盘不足、登录/CAPTCHA 或无法自动修复的问题时会安全阻塞并保留全部状态。

## 最简使用

```bash
cd "/Volumes/语苗计划/外置硬盘/New project 半自动版"

python3 story_agent.py submit \
  --video "/path/to/横版绿幕原片.mp4" \
  --story-name "故事名"

# submit 会打印 job id；把它填到下面
python3 story_agent.py start --job JOB_ID
python3 story_agent.py status --job JOB_ID
python3 story_agent.py report --job JOB_ID
```

任务可安全取消和续跑：

```bash
python3 story_agent.py cancel --job JOB_ID
python3 story_agent.py resume --job JOB_ID
python3 story_agent.py start --job JOB_ID
```

完整契约见 [`STORY_AGENT_SPEC.md`](STORY_AGENT_SPEC.md)，Codex 项目 Skill 位于 [`skills/story-full-auto/SKILL.md`](skills/story-full-auto/SKILL.md)。下面内容保留为旧合成器和工作台的兼容说明。

## 旧版：儿童故事视频合成器

> 当前项目正在升级为完整的 Story Video Composer。全局流程、桌面项目结构、manifest、自动 QA 和最终交付规范以 [`STORY_PIPELINE.md`](STORY_PIPELINE.md) 为准。

这个工具负责把已经生成好的故事视频片段、逐行台词、完整旁白录音和背景音乐合成为完整故事视频。
现在也包含前置脚本，用来把“文生图结果 / 已排好序的分镜图片 + 分镜文本”整理成图片转视频 API 任务清单、提示词和人工审核页。

它不直接调用图片生成模型；分镜拆分和出图仍然优先由已有的 `children-storyboard-images` 工作流处理。出图完成后，可以用这里的脚本接入图生视频和最终合成。

## 实现方案

输入：

- 按顺序命名的视频片段文件夹，例如 `01_lycs.mp4` 到 `20_lycs.mp4`
- 逐行台词文本，每一行对应一个视频片段
- 完整旁白音频
- 背景音乐
- 输出文件夹

处理流程：

1. 读取逐行台词，按文件名排序读取视频片段。
2. 使用本地 Whisper 识别旁白，并请求词/字块时间戳。
3. 将 Whisper 识别文本与原始逐行台词做文本匹配，把每一行映射到旁白时间轴。
4. 以原始旁白和背景音乐为主时间线，不裁剪、不重拼原始音频。
5. 为每个视频片段生成目标时长：
   - 第一个视频从 0 秒开始。
   - 后续视频从对应台词开始处切换。
   - 句子之间的停顿会保留在前一个视频片段里。
   - 视频比目标时长长：裁剪到目标时长。
   - 视频比目标时长短：放慢视频到目标时长。
6. 统一视频尺寸、帧率和编码后按顺序拼接。
7. 根据原始旁白时间轴生成短字幕，尽量保持单行显示，并清理冗余标点。
8. 混入完整背景音乐和完整旁白，输出多版视频；其中销售版会自动隐藏开头品牌口播字幕和结尾总结口播字幕。

输出：

- `story_no_subs_bgm.mp4`：无字幕 + 背景音乐
- `story_subs_bgm.mp4`：有字幕 + 背景音乐
- `story_sales_subs_bgm.mp4`：销售版有字幕 + 背景音乐；默认跳过开头 2 行台词字幕和结尾 1 行台词字幕
- `story_demo_voice_bgm.mp4`：有字幕 + 旁白人声 + 背景音乐
- `timings.json`：每行台词对应的旁白时间
- `story_subtitles.srt`：字幕文件
- `story_sales_subtitles.srt`：销售版字幕文件

## 运行原生桌面界面

现在推荐优先使用完整工作台。它把“文生图结果整理、图生视频任务、旁白时长、视频生成、审核、最终合成”放到同一个窗口里：

```bash
python3 workbench_app.py
```

如果当前终端不在项目目录，可以直接运行：

```bash
python3 "/Volumes/语苗计划/外置硬盘/New project/workbench_app.py"
```

也可以在 Finder 里双击项目里的 `run_workbench.command`。

## 下次开新项目怎么用

新开一个 Codex 窗口也可以，不需要依赖旧对话上下文。只要告诉 Codex：

```text
请在 /Volumes/语苗计划/外置硬盘/New project 这个项目里，打开儿童故事生产工作台，按 README.md 的流程做新故事。
```

工作台本身可以这样启动：

```bash
cd "/Volumes/语苗计划/外置硬盘/New project"
python3 workbench_app.py
```

也可以在 Finder 里双击项目里的 `run_workbench.command`。

每个新故事只需要换这几类输入：故事名、`Slug`、短名、故事文本、旁白原声、最终图片或 Codex 出图结果。青云聚合 Grok 视频 API、Whisper、本地合成、Suno 半自动配乐、审核页、销售版字幕规则都已经固定在这个项目里。

工作台的基本顺序：

1. 填故事名、`Slug`、短名，选择输出根目录。
2. 在右侧粘贴故事原文，点击“0 生成出图任务”。工作台会生成一份交给 Codex 的 `children-storyboard-images` 任务文件，并指定最终图片目录和分镜文本保存位置。
3. 在 Codex 对话里按这份任务执行分镜确认和出图。出图完成后，如果图片已经在工作台指定的最终图片目录，可以跳过“1 整理/导入图片”；如果图片在别的文件夹，选择“Codex出图目录”，再点“1 整理/导入图片”。
4. 确认最终图片目录和分镜文档后，点击“2 准备任务”。
5. 选择旁白原声，点击“3 写入时长”。
6. 可先点“4 试跑一条”检查青云 API 请求，再点“5 生成视频”。
7. 点“6 打开审核页”，检查视频；坏的标“重做”，不用的标“不使用”，导出审核 CSV。
8. 回到工作台选择审核 CSV，点击“7 重跑审核问题”；重跑后如果满意，再导出/确认审核 CSV。
9. 点击“8 应用审核”，整理最终合成用的 `clips/` 和 `script_lines.txt`。
10. 点击“9 生成配乐任务”。Codex 根据生成的任务书写 `music/*_suno_prompts.md` 和 `music/*_music_plan.csv`；音乐反馈可以直接在 Codex 对话里说。
11. 点“10 打开配乐文本”复制 Suno 提示词；点“11 打开Suno”去网页生成 Instrumental 音乐，下载后放入 `music/suno_downloads/`，并按 CSV 的 `target_audio_filename` 命名。
12. 点击“12 拼接音乐”，工作台会按音乐分段 CSV 裁剪并拼成完整背景音乐。
13. 点击“13 合成成片”，输出普通版、有人声预览版和销售版。

旧版“只做最终合成”的桌面入口仍然保留。它更适合已经有现成视频片段时使用：用系统文件选择器添加视频和音频，台词可以直接编辑，并提供“视频片段 / 台词对应”预览。

```bash
python3 desktop_app.py
```

如果当前终端不在项目目录，可以直接运行：

```bash
python3 "/Volumes/语苗计划/外置硬盘/New project/desktop_app.py"
```

也可以在 Finder 里双击项目里的 `run_desktop_app.command`。

桌面版主要操作：

- 左侧添加视频片段文件夹或单独添加视频，可上移、下移、删除和清空。
- 右侧导入逐行台词，也可以直接在文本框里修改。
- “视频片段 / 台词对应”会显示每个视频对应哪一行台词，数量不一致时会标出缺项。
- 选择旁白录音、背景音乐和输出文件夹后点击“开始合成”。

## 运行网页界面

```bash
python3 app.py
```

这是备用入口。打开终端显示的本地地址，常见形式是：

```text
http://127.0.0.1:某个端口
```

## 命令行运行

```bash
python3 synthesize.py \
  --video-dir /path/to/clips \
  --script /path/to/lines.txt \
  --narration /path/to/narration.wav \
  --music /path/to/music.mp3 \
  --output-dir /path/to/output
```

默认使用 Whisper 的 `base` 模型。可以改模型：

```bash
python3 synthesize.py \
  --video-dir /path/to/clips \
  --script /path/to/lines.txt \
  --narration /path/to/narration.wav \
  --music /path/to/music.mp3 \
  --output-dir /path/to/output \
  --whisper-model small
```

## 统一工作流入口

推荐以后优先用 `story_workflow.py`，不用再记一堆独立脚本。它只是把现有脚本串起来，便于固定流程：

```bash
python3 story_workflow.py prepare \
  --image-dir "/path/to/final/images" \
  --storyboard "/path/to/storyboard.docx" \
  --output-dir "/path/to/story_jobs" \
  --slug story-slug \
  --short-slug ss

python3 story_workflow.py timing \
  --jobs-csv "/path/to/story_jobs/story-slug_image_video_jobs.csv" \
  --narration "/path/to/narration.mp3" \
  --whisper-model-dir "/Volumes/语苗计划/外置硬盘/New project/models/whisper"

python3 story_workflow.py generate \
  --jobs-csv "/path/to/story_jobs/story-slug_image_video_jobs.csv" \
  --images-dir "/path/to/story_jobs/images" \
  --videos-dir "/path/to/story_jobs/videos" \
  --start-scene 1 \
  --end-scene 37
```

如果前一步是文生图工作流，先把最终图片整理成标准命名：

```bash
python3 story_workflow.py normalize-images \
  --source-dir "/path/to/text-to-image/results" \
  --output-dir "/path/to/story_jobs" \
  --slug story-slug \
  --count 37
```

打开 `story-slug_review.html` 审核。已下载的视频会默认标为“通过”，只需要把有问题的镜头改成“重做”，把录音里已经删除、不想进成片的镜头改成“不使用”。点击“导出审核CSV”后，再整理最终合成素材：

```bash
python3 story_workflow.py apply-review \
  --jobs-csv "/path/to/story_jobs/story-slug_image_video_jobs.csv" \
  --videos-dir "/path/to/story_jobs/videos" \
  --output-dir "/path/to/final_assembly" \
  --decisions-csv "/path/to/review_decisions.csv" \
  --short-slug ss
```

最后合成：

```bash
python3 story_workflow.py assemble \
  --video-dir "/path/to/final_assembly/clips" \
  --script "/path/to/final_assembly/script_lines.txt" \
  --narration "/path/to/narration.mp3" \
  --music "/path/to/music.mp3" \
  --output-dir "/path/to/final_assembly" \
  --subtitle-style clean
```

### 小红书发布视频包装

背景故事视频生成后，可以再跑发布包装流程，自动产出主账号版和宝库号版：

```bash
python3 story_workflow.py package-release \
  --story-name "猴子捞月" \
  --duration-text "3分钟" \
  --bg-video "/path/to/story_demo_voice_bgm.mp4" \
  --bg-image "/path/to/story_bg_image.jpg" \
  --person-greenscreen "/path/to/person_greenscreen.mp4" \
  --audio-mix "/path/to/voice_music_mix.m4a" \
  --watermark-logo "/path/to/logo.png" \
  --output-dir "/path/to/release_output"
```

输出：

- `主账号发布视频.mp4`：真人绿幕抠像 + 主题底图 + 框内背景视频 + 信息栏。
- `宝库号发布视频.mp4`：纯背景视频 + 信息栏 + 飘动水印 + 结尾模糊提示。

如果只想先跑宝库号版：

```bash
python3 story_workflow.py package-release \
  --story-name "猴子捞月" \
  --duration-text "3分钟" \
  --bg-video "/path/to/story_demo_voice_bgm.mp4" \
  --output-dir "/path/to/release_output" \
  --variant library
```

使用 AI 生成的完整 3:4 底板时，把底板图传给 `--plate-image`。脚本会把有声字幕成片嵌入中间窗口；水印和尾部模糊也只作用在这个中间视频区域。

底板生图时要把中间 `16:9` 横版区域当成“非创作区”：它最终会被真实视频覆盖，所以底板图只需要生成顶部标题区和底部信息区。中间区域必须是一整条左右打穿的纯色空白条，不要生成故事画面、边框、角标、人物、道具、云纹、卷轴或任何装饰。

先生成一份底板生图提示词：

```bash
python3 story_workflow.py plate-prompt \
  --story-name "滥竽充数" \
  --duration-text "2分29秒" \
  --story-type "成语故事" \
  --theme-elements "中国风、古代宫廷、编钟、竹简、卷轴、课堂表演、儿童友好" \
  --reference-image "/path/to/狐狸分奶酪_参考图.jpeg" \
  --output-dir "/path/to/release_output" \
  --slug "lanyu-chongshu"
```

Codex 根据这份提示词生成底板图后，再包装发布视频：

```bash
python3 story_workflow.py package-release \
  --story-name "猴子捞月" \
  --duration-text "3分钟" \
  --bg-video "/path/to/story_demo_voice_bgm.mp4" \
  --plate-image "/path/to/generated_release_plate.png" \
  --antipiracy-logo "/path/to/anti_piracy_logo.png" \
  --output-dir "/path/to/release_output" \
  --variant library \
  --video-box "0,416,1080,608"
```

`--video-box` 是中间视频窗口位置，格式为 `x,y,w,h`，基于最终 `1080x1440` 画布。默认是 `0,416,1080,608`，也就是中间铺满宽度的 16:9 区域。使用 `--plate-image` 时，脚本会把这块区域在底板前景层里强制抠透明，避免生图模型意外画进去的东西遮挡真实视频。尾部模糊默认按视频时长自动估算：约 2 分钟的视频模糊 20 多秒，约 3 分钟的视频模糊 30 秒；也可以用 `--tail-seconds 30` 手动固定。

主账号绿幕边缘可以用 `--chroma-similarity` 和 `--chroma-blend` 微调；有定制透明框 PNG 时传 `--frame-image`，不传则使用默认框。

### 双账号四平台发布包

主账号和宝库号发布视频都完成后，可以生成发布包。第一次运行会输出四平台文案草稿和候选封面帧：

```bash
python3 story_workflow.py publish-package \
  --story-name "猴子捞月" \
  --episode 12 \
  --duration-text "3分钟" \
  --story-text "/path/to/story.txt" \
  --main-video "/path/to/主账号发布视频.mp4" \
  --library-video "/path/to/宝库号发布视频.mp4" \
  --output-dir "/path/to/publish_package"
```

输出：

- `main/copy.md`：主账号小红书、抖音、视频号、B站、朋友圈文案草稿。
- `library/copy.md`：宝库号小红书、抖音、视频号、B站文案草稿。
- `frame_candidates/main_候选帧索引.jpg`：主账号候选封面帧。
- `frame_candidates/library_候选帧索引.jpg`：宝库号候选封面帧。

确认两个候选帧编号后，再生成参考封面和信息型设计封面提示。主账号建议传入从高清绿幕视频抽出的真人参考帧，人物一致性会比使用发布成片截图更好：

```bash
python3 story_workflow.py publish-package \
  --story-name "猴子捞月" \
  --episode 12 \
  --duration-text "3分钟" \
  --story-text "/path/to/story.txt" \
  --main-video "/path/to/主账号发布视频.mp4" \
  --library-video "/path/to/宝库号发布视频.mp4" \
  --output-dir "/path/to/publish_package" \
  --person-reference "/path/to/greenscreen_person_frame.png" \
  --main-frame 3 \
  --library-frame 5 \
  --generate-covers
```

脚本会生成参考图，并在 `cover_imagegen_prompts.md` 中写好 Codex 原生图像生成提示。默认 `--cover-style designed`：主账号做“标题巨大 + 时长清楚 + 真人一致 + 资料包信息”的设计型封面；宝库号做“资源说明书式”信息型封面。旧的截帧扩展思路仍可用 `--cover-style screenshot` 保留。

### 绵羊故事锦囊资料包

资料包生成会在桌面输出两个文件夹：

- `绵羊故事锦囊：故事名（基础版）`
- `绵羊故事锦囊：故事名（进阶版）`

第一次处理新绿幕素材时，先不传 `--keying-preset-json`，脚本只生成抠像预览和背景候选，不会直接渲染整段示范视频：

```bash
python3 story_workflow.py product-package \
  --story-name "故事名" \
  --slug story-slug \
  --story-text "/path/to/source.txt" \
  --script-lines "/path/to/script_lines.txt" \
  --narration "/path/to/narration.mp3" \
  --music "/path/to/background_music.mp3" \
  --images-dir "/path/to/images" \
  --bg-video-with-sub "/path/to/story_sales_subs_bgm.mp4" \
  --bg-video-no-sub "/path/to/story_no_subs_bgm.mp4" \
  --person-greenscreen "/path/to/调色美颜后的完整绿幕视频.mp4" \
  --work-dir "/path/to/product_work"
```

确认抠像参数和示范背景后再正式生成：

```bash
python3 story_workflow.py product-package \
  --story-name "故事名" \
  --slug story-slug \
  --story-text "/path/to/source.txt" \
  --script-lines "/path/to/script_lines.txt" \
  --narration "/path/to/narration.mp3" \
  --music "/path/to/background_music.mp3" \
  --images-dir "/path/to/images" \
  --bg-video-with-sub "/path/to/story_sales_subs_bgm.mp4" \
  --bg-video-no-sub "/path/to/story_no_subs_bgm.mp4" \
  --person-greenscreen "/path/to/调色美颜后的完整绿幕视频.mp4" \
  --demo-background-image "/path/to/无人物开阔故事环境图.png" \
  --keying-preset-json "/path/to/keying_preset_confirmed.json" \
  --annotation-docx "/path/to/精修朗读标注.docx"
```

注意事项：

- `--bg-video-with-sub` 必须传“只保留故事正文字幕”的背景视频；不要传 `story_subs_bgm.mp4` 这种含开头/结尾字幕的完整字幕版。脚本会默认拦截这类风险文件。
- `--person-greenscreen` 应使用已经调色、美颜、完整的绿幕视频；文件名含 `test`、`测试`、`raw`、`原始` 的素材默认会被拦截。
- 示范视频背景必须显式传入无人物、开阔、符合故事气质的环境图；脚本不会再默认使用镜头 1。
- 文稿会自动把“我是绵羊姐姐”改为“我是____”，并移除其它“绵羊姐姐”字样，保证对外售卖的通用性。
- PPT 不额外生成封面页，第一页就是镜头 1；音乐嵌入在第一页，并按每页台词时长自动切换。

## Suno 配乐流程

Suno 官方网页生成不按稳定 API 处理。当前项目把它设计成“Codex 生成提示词 + Suno 网页生成 + 本地自动拼接”的半自动流程：

```bash
python3 story_workflow.py music-request \
  --story-file "/path/to/story_source.txt" \
  --output-dir "/path/to/story_jobs" \
  --slug story-slug \
  --story-title "故事名" \
  --narration "/path/to/narration.mp3" \
  --jobs-csv "/path/to/story_jobs/story-slug_image_video_jobs.csv"
```

它会生成：

- `music/story-slug_suno_music_request.md`：交给 Codex 的 Suno 配乐任务。
- `music/story-slug_music_plan.csv`：音乐分段表，后续拼接使用。
- `music/suno_downloads/`：Suno 下载音频放这里。
- `music/story-slug_background_music.mp3`：最终拼好的背景音乐。

在 Codex 里按 `story-slug_suno_music_request.md` 执行 Suno 配乐 skill，填好 `music_plan.csv` 和提示词文件；然后去 Suno 网页生成各段纯音乐，下载并改名为 CSV 的 `target_audio_filename`。

下载完成后拼接：

```bash
python3 story_workflow.py assemble-music \
  --plan-csv "/path/to/story_jobs/music/story-slug_music_plan.csv" \
  --clips-dir "/path/to/story_jobs/music/suno_downloads" \
  --output "/path/to/story_jobs/music/story-slug_background_music.mp3"
```

拼好的 `story-slug_background_music.mp3` 可以直接作为最终合成的背景音乐。

## 准备图片转视频任务

当图片已经在网页端生成好，并且文件名按 `01_故事名.png`、`02_故事名.png` 排好序时，可以先生成 API 任务清单和人工检查页：

```bash
python3 prepare_image_video_jobs.py \
  --image-dir "/path/to/final/images" \
  --storyboard "/path/to/storyboard_lines.txt" \
  --output-dir "/path/to/story_image_video_jobs" \
  --slug story-slug \
  --short-slug ss
```

脚本会输出：

- `images/`：按稳定文件名复制的最终图片。
- `videos/`：后续 API 生成的视频片段目标文件夹。
- `story-slug_image_video_jobs.csv`：完整任务清单。
- `story-slug_video_prompts.md` 和 `.csv`：每张图对应的短视频提示词。
- `story-slug_clip_names.csv`：推荐视频片段命名，例如 `01_ss.mp4`。
- `story-slug_review.html`：人工审核页，生成 MP4 后放进 `videos/` 就能逐条检查。已下载视频默认通过，可标记重做/不使用，并导出 `review_decisions.csv`。

如果图片数量和分镜文本数量不同，脚本会继续生成任务，但会提示需要人工核对。接入具体视频生成 API 后，可以让 API 读取这份 CSV，逐行上传图片和提示词，生成到 `videos/` 文件夹。

## 调用青云聚合 Grok 视频生成 API

当前默认模型：

```text
grok-video-3-10s
```

默认接口是 `https://api.qingyuntop.top/v1/video/create`，横屏 `16x9`，每次生成固定 10 秒。如果要让每个视频片段的时长贴合讲故事原声，先把旁白音频对齐到任务 CSV：

```bash
python3 apply_narration_durations.py \
  --jobs-csv "/path/to/story_image_video_jobs/story-slug_image_video_jobs.csv" \
  --narration "/path/to/narration.mp3" \
  --whisper-model base \
  --language zh
```

脚本会把每个镜头的真实旁白秒数写入 `target_duration`，并把 API 生成时长按固定 10 秒写入 `generation_duration` 和 `effective_duration`。低于 10 秒的镜头会标记 `needs_trim=yes`，最终合成器会裁切；超过 10 秒的镜头会标记 `needs_slowdown=yes`，最终合成器会慢放匹配旁白长度。

先把 API Key 放到环境变量，不要写进文件：

```bash
export QINGYUN_API_KEY="你的青云聚合 API Key"
```

建议先只跑 1 条做验证：

```bash
python3 run_image_video_jobs.py \
  --jobs-csv "/path/to/story_image_video_jobs/story-slug_image_video_jobs.csv" \
  --images-dir "/path/to/story_image_video_jobs/images" \
  --videos-dir "/path/to/story_image_video_jobs/videos" \
  --limit 1 \
  --dry-run
```

确认请求体没问题后，再真实提交 1 条：

```bash
python3 run_image_video_jobs.py \
  --jobs-csv "/path/to/story_image_video_jobs/story-slug_image_video_jobs.csv" \
  --images-dir "/path/to/story_image_video_jobs/images" \
  --videos-dir "/path/to/story_image_video_jobs/videos" \
  --limit 1
```

批量生成时建议一次性先提交全部任务，让青云侧并发排队/生成。半自动工作台的“5 生成视频”默认已经会这样做；如果手动终端跑全量，记得加 `--submit-all-first`：

```bash
python3 run_image_video_jobs.py \
  --jobs-csv "/path/to/story_image_video_jobs/story-slug_image_video_jobs.csv" \
  --images-dir "/path/to/story_image_video_jobs/images" \
  --videos-dir "/path/to/story_image_video_jobs/videos" \
  --submit-all-first
```

脚本会按青云聚合的 `video/create` 格式提交请求：

```text
model=grok-video-3-10s
seconds=10
size=720P
aspect_ratio=16:9
images=[data:image/png;base64,...]
```

确认单条成功后，再去掉 `--limit 1` 批量跑。每完成一个任务，CSV 会写入 `task_id`、`video_url`、`status`，视频保存到 `videos/`，预览页会自动读取对应文件名。

## 文生图结果接入

如果图片来自文本生图片工作流，先把最终选中的图片整理成统一命名：

```bash
python3 normalize_story_images.py \
  --source-dir "~/.codex/generated_images/story-slug" \
  --output-dir "/path/to/story_jobs" \
  --slug story-slug \
  --count 37
```

它会生成：

```text
/path/to/story_jobs/images/story-slug_scene_01.png
/path/to/story_jobs/images/story-slug_scene_02.png
...
```

之后这一路径就可以直接作为 `story_workflow.py prepare --image-dir` 的输入。

## 审核后整理合成素材

审核页导出的 CSV 可以用来生成最终合成目录：

```bash
python3 apply_review_decisions.py \
  --jobs-csv "/path/to/story_jobs/story-slug_image_video_jobs.csv" \
  --videos-dir "/path/to/story_jobs/videos" \
  --output-dir "/path/to/final_assembly" \
  --decisions-csv "/path/to/review_decisions.csv" \
  --short-slug ss
```

如果不传 `--decisions-csv`，脚本会默认把所有已经下载的片段视为通过，用于快速测试。

## 注意事项

- 本机需要能运行 `ffmpeg` 和 `ffprobe`。
- 首次使用某个 Whisper 模型时，Whisper 可能需要下载模型文件。
- 如果项目里存在 `models/whisper`，桌面版会优先把它当作 Whisper 模型目录，方便后续打包成 `.app`。
- 视频片段数量必须和台词行数一致。
- 第一版默认输出为 1920x1080、30fps，适合 16:9 儿童故事视频。
- 默认字幕样式是 `clean`：白字、细黑描边、轻阴影、位置更靠下；旧版黑底字幕可用 `--subtitle-style box`。

## 设置说明

- Whisper 模型越大，对齐通常越稳，但越慢、越吃内存。`tiny` 最快但误差大，`base` 是当前推荐默认，`small` 更稳但更慢，`medium/large` 适合对齐要求高且愿意等待的任务。
- 音量是倍率：`1.0` 表示原始音量，`0.22` 表示原音量的 22%，`2.0` 表示放大到 200%。
- 当前合成会显示制作耗时；耗时主要来自 Whisper 对齐、视频片段变速和字幕视频轨编码。

## 打包成 macOS 应用

推荐先用本机应用外壳：

```bash
bash build_local_app_bundle.sh
```

生成位置：

```text
dist/儿童故事视频合成器.app
```

这个版本可以双击启动，并会携带 `models/whisper` 里的模型文件；它仍然使用本机 `/opt/miniconda3/bin/python3` 环境。

本机已经有 PyInstaller 时，可以运行：

```bash
bash build_macos_app.sh
```

PyInstaller 版本更接近完全独立应用，但 macOS 可能要求先同意 Xcode license。打包结果会出现在：

```text
dist_pyinstaller_full/儿童故事视频合成器.app
```

如果需要把模型一起放进应用，先把 Whisper 模型文件放到：

```text
models/whisper
```
