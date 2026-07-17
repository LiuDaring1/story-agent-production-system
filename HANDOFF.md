# Story Video Composer 工作台交接说明

> 2026-07-13 说明：本文件保留的是旧工作台历史交接，工作台现在仅用于故障诊断，不再是正常生产入口。全自动 Agent 的当前准确信息请优先阅读 `README.md`、`STORY_AGENT_SPEC.md`、`FULL_AUTO_READINESS_AUDIT.md`、`SHADOW_PRODUCTION_REPORT.md` 和 `FULL_AUTO_PROMOTION_REPORT.md`；正常入口是 Codex + `story_agent.py`。

更新时间：2026-05-08

## 交接对象

本文件交接的是 **Story Video Composer 工作台本身**，不是某一个具体故事项目。

本轮曾用一个外部委托样例验证过背景成片链路。该样例只需要做到背景视频阶段，后续不需要继续做发布视频、发布物料或资料包。交接时不需要围绕这个样例展开。

## 本地位置

- 当前工作台代码项目：`/Volumes/语苗计划/外置硬盘/New project`
- 工作台入口脚本：`/Volumes/语苗计划/外置硬盘/New project/workbench_app.py`
- 统一 CLI 入口：`/Volumes/语苗计划/外置硬盘/New project/story_workflow.py`
- 全局规范：`/Volumes/语苗计划/外置硬盘/New project/STORY_PIPELINE.md`
- 全局配置：`/Volumes/语苗计划/外置硬盘/New project/pipeline_config.json`
- 轻量源码备份：`/Volumes/语苗计划/外置硬盘/New project/backups/story_video_composer_workbench_source_lite_20260508_封版.tar.gz`

## 工作台定位

最终目标是让用户只围绕桌面故事文件夹工作：

```text
~/Desktop/故事剪辑：故事名
```

用户提供尽量少的输入：

- 故事正文
- 旁白原声，或可提取音频的视频
- 如果要做发布视频和资料包示范视频，再提供已美颜调色后的绿幕视频

工作台负责把素材识别、分镜、图片、图生视频、音乐、背景成片、发布视频、发布物料、资料包和最终交付清单串成一套流程。

## 当前工作台能力

已经具备并部分验证：

- 创建/选择桌面故事文件夹。
- 自动建立统一目录结构：
  - `00_输入素材`
  - `01_分镜与图片`
  - `02_图生视频`
  - `03_背景成片`
  - `04_发布视频`
  - `05_发布物料`
  - `06_资料包`
  - `99_项目状态`
- 保存并读取 `project_manifest.json`。
- 在工作台设置故事名、集数、故事类型、适龄段、时长文案。
- 根据 `latest_episode` 生成默认集数。
- 将“出图任务”复制给 Codex，并指向正确的桌面项目目录。
- 准备图生视频任务、写入旁白时长、批量生成视频片段。
- 打开审核页，导入/应用审核 CSV，重跑审核问题。
- 生成可直接复制到 Suno 的配乐提示词和音乐分段表。
- 拼接 Suno 音乐。
- 合成背景成片多版本。
- 生成主账号/宝库号发布视频并回收 QA。
- 生成发布物料并回收 QA。
- 生成机器 QA 报告和总交付清单。

保留但未完整端到端验证：

- 主题底板/故事框生成。
- 自动绿幕抠像。
- 基础版/进阶版资料包。

## 主要代码文件

- `workbench_app.py`：Tkinter 工作台 UI，推荐操作按钮、高级路径折叠、manifest 回填、路径刷新。
- `story_workflow.py`：统一命令入口，负责串联底层脚本和项目级子命令。
- `story_project.py`：项目目录、配置、manifest、素材识别、QA、主题素材、最终交付。
- `prepare_image_video_jobs.py`：图片和分镜文本转图生视频任务。
- `run_image_video_jobs.py`：调用视频 API 生成片段。
- `apply_review_decisions.py`：按审核结果整理 clips / 重跑。
- `prepare_suno_music_request.py`：生成 Suno 可复制提示词和音乐分段表。
- `assemble_suno_music.py`：拼接 Suno 下载音频。
- `synthesize.py`：背景成片合成。
- `release_video.py`：发布视频底层合成。
- `publish_package.py`：发布物料生成。
- `product_package.py`：资料包生成。

## 本轮已修复的工作台问题

- 简化 UI：推荐操作按钮成为主入口，高级路径与旧按钮默认隐藏。
- 修复审核应用/重跑时串到旧故事项目路径的问题。
- 修复图生视频准备/生成时图片目录串到旧项目的问题。
- 修改生成规则：视频生成不再固定 6 张一批，默认能做多少做多少，中断后再继续。
- 修复第 9 步配乐任务：现在直接生成 `story_suno_prompts.md`，不再只生成“任务书”。
- 修复故事正文污染：如果文本框里是 Codex 任务书，保存和配乐会提取其中的纯故事原文。
- 修复素材识别：不再把 `01_分镜与图片`、`02_图生视频`、`03_背景成片` 等生成目录里的文件误识别为用户输入。
- 修复空发布物料目录被误判为完成的问题。
- 修复没有绿幕输入时保留误抽人物参考帧的问题。
- 修复发布视频链路未直接读取 `keying_preset.json` 的问题。
- 重新生成轻量源码备份，包含最新工作台代码和本交接文档。

## 账号与接力

工作台是本地代码，不绑定当前 ChatGPT/Codex 账号。换账号后，只要仍在这台电脑、同一个 macOS 用户、路径不变，新账号下的 Codex 可以继续读取和修改。

但这些内容会受账号/环境影响：

- Codex 对话上下文不会迁移，新账号必须先读本交接文档和 `STORY_PIPELINE.md`。
- 图片生成能力和 Codex 智能调用消耗当前登录账号额度。
- 图生视频默认依赖本地 `TOAPIS_API_KEY` 或 macOS 钥匙串 `story-agent.TOAPIS_API_KEY`；旧青云 Key 不再作为默认生产凭据。
- Suno 仍是人工网页生成和下载。
- macOS 文件访问审批可能需要新会话重新允许。

推荐新账号接手提示：

```text
请接手这个本地 Story Video Composer 工作台项目：
/Volumes/语苗计划/外置硬盘/New project

请先阅读 HANDOFF.md、STORY_PIPELINE.md、pipeline_config.json。
注意：交接对象是工作台本身，不是某一个故事项目。已有故事项目只作为历史验证记录，不作为下一阶段工作主体。
请先检查工作台代码与现有流程，再用一个我们自己要发布的新故事，从桌面故事文件夹开始继续端到端测试。
```

## 启动方式

命令行启动：

```bash
cd "/Volumes/语苗计划/外置硬盘/New project"
python3 workbench_app.py
```

或双击：

```text
/Volumes/语苗计划/外置硬盘/New project/run_workbench.command
```

## 已验证

封版前执行过：

```bash
python3 -m compileall -q app.py apply_narration_durations.py apply_review_decisions.py assemble_suno_music.py desktop_app.py normalize_story_images.py prepare_image_video_jobs.py prepare_suno_music_request.py product_package.py publish_package.py release_plate_prompt.py release_video.py run_image_video_jobs.py story_project.py story_workflow.py synthesize.py workbench_app.py story_video_synthesizer
python3 story_workflow.py --help
python3 story_workflow.py music-request --help
python3 story_workflow.py final-delivery --help
```

用一个历史样例验证过：

- 29 张分镜图已进入标准目录。
- 29 个图生视频片段已生成。
- 视频审核、重跑、应用审核链路已跑过并修过路径问题。
- Suno 提示词生成、音乐拼接、背景成片已跑通。
- 图片 QA、视频 QA 生成报告，未发现明显问题。
- 总交付清单可生成，能正确显示本期只完成背景视频。

## 尚未完整验证

下一轮应使用“我们自己要发布的故事”验证，不要继续消耗在历史样例上。

重点验证：

- 从一个全新的桌面故事文件夹开始，跑完整工作台一条龙。
- 新账号下运行工作台是否能读取本地 skill、配置和桌面目录。
- 绿幕视频自动识别、音频提取、人物参考帧抽取。
- 自动抠像参数质量。
- 主账号/宝库号发布视频的视觉 QA。
- 双账号四平台发布物料是否完整回收到 `05_发布物料`。
- 基础版/进阶版资料包是否完整回收到 `06_资料包`。
- macOS `.app` 打包版本是否同步最新源码。
- 尚未建立系统化单元测试，目前主要依赖编译检查和真实流程测试。

## 下一步建议

下一轮不要先重构，先用新故事跑实战测试：

1. 在桌面创建 `故事剪辑：新故事名`。
2. 放入故事正文、旁白原声、绿幕视频。
3. 打开工作台，选择这个故事文件夹。
4. 从 `① 保存并识别` 开始逐步跑。
5. 发现问题时优先修路径、manifest、按钮交互和状态提示。
6. 背景成片稳定后，再测试发布视频、发布物料和资料包。

## 2026-05-10 第 12 步交接更新

当前仍在处理桌面项目：

```text
/Users/baiyanglin/Desktop/故事剪辑：邯郸学步
```

重点卡点是第 12 步主题素材：

```text
/Users/baiyanglin/Desktop/故事剪辑：邯郸学步/04_发布视频/theme_assets
```

最新规则已经同步到代码和任务书：

- 第 12 步只做两张 3:4 发布底板、一个 16:9 无框主账号背景、一个统一故事框源图。
- `story_frame_a.png` 和 `story_frame_b.png` 应由同一个 `story_frame_source.png` 自动导出，不要生成两套不同故事框。
- 底板资料文案只表达 6 项：背景视频、PPT、配乐、文稿、标注、示范视频。不要写“发布物料”，也不要把“联系私信客服”写到底板。
- 背景图不能自带故事框，故事框是后期透明 PNG 叠加。
- Pillow 只允许做后处理和 QA：裁切、清理视频安全区、中文修正、透明通道、A/B 导出。不要用本地代码从零画占位图。

已做的代码修复：

- `story_project.py`：第 12 步任务书改为单一 `story_frame_source.png`，交接指令要求使用当前 Codex 原生图像生成能力。
- `story_workflow.py`：发布视频前会触发主题素材 QA，并可从统一故事框源图导出 A/B。
- `workbench_app.py`：第 12 步按钮生成规格书和交接指令，提醒用户回 Codex 执行。
- `postprocess_handan_imagegen_assets.py`：加入源图新鲜度防呆；如果 source 图不是本轮新生成，会拒绝覆盖 `theme_assets`。

重要配置状态：

- `~/.codex/config.toml` 当前为 `model_provider = "my_codex2"`、`model = "gpt-5.5"`、`wire_api = "responses"`。
- 已从 `~/.zshrc` 和 `~/.zprofile` 移除旧 `XAI_API_KEY`，备份为：
  - `/Users/baiyanglin/.zshrc.bak-20260510093348`
  - `/Users/baiyanglin/.zprofile.bak-20260510093348`
- 不要再调用旧的 `scripts/image_gen.py` / `OPENAI_API_KEY` / `XAI_API_KEY` 图像生成回退链路。

下一步在新对话中应做：

1. 先确认当前 Codex 对话是否能调用原生图像生成能力。
2. 如果能，读取：
   `/Users/baiyanglin/Desktop/故事剪辑：邯郸学步/04_发布视频/theme_assets/theme_assets_codex_handoff.txt`
3. 用当前 Codex 原生图像生成能力生成新的 source 图并保存到：
   - `/Users/baiyanglin/Documents/New project/output/imagegen/handan_real/main_release_plate_source.png`
   - `/Users/baiyanglin/Documents/New project/output/imagegen/handan_real/library_release_plate_source.png`
   - `/Users/baiyanglin/Documents/New project/output/imagegen/handan_real/main_background_16x9_source.png`
   - `/Users/baiyanglin/Documents/New project/output/imagegen/handan_real/story_frame_source.png`
4. 然后运行：

```bash
cd "/Users/baiyanglin/Documents/New project"
python3 postprocess_handan_imagegen_assets.py
python3 -c "from pathlib import Path; from story_project import project_paths, qa_theme_assets; qa_theme_assets(project_paths(Path('/Users/baiyanglin/Desktop/故事剪辑：邯郸学步')), strict=True)"
```

5. QA 通过后再继续第 13 步生成发布视频。
