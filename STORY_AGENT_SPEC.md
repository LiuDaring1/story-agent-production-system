# Story Production Agent 规格书

## 目标

在保留现有工作台和 `story_workflow.py` 的前提下，新增一条 Codex 原生总控链路：用户把故事文本、旁白/绿幕原片等素材放入投喂区后，Agent 自己循环推进项目，直到生成发布视频、发布物料、资料包、QA 报告和 `总交付清单.md`。

这条链路不替代工作台。工作台继续作为人工驾驶舱；Agent 是无人值守路线。

## 总体结构

Agent 的职责分三层：

- 执行层：调用现有 Python 脚本和 `story_workflow.py`，不重写已跑通的图生视频、合成、发布、资料包逻辑。
- 判断层：读取 manifest、QA 报告、抽帧、预览图和交接文件，决定继续、重跑、调参或进入 Codex 原生动作。
- 原生动作层：由 Codex 自身完成脚本无法直接完成的任务，包括 imagegen 生图、视觉审查、朗读标注精修、Suno 浏览器自动化。

## Loop

每次循环执行：

1. 读取 `99_项目状态/project_manifest.json` 和 `story_agent_state.json`。
2. 刷新项目输出。
3. 找到第一个未完成阶段。
4. 如果阶段是本地自动阶段，执行对应命令并写入日志。
5. 如果阶段需要 Codex 原生能力，生成明确任务文件并暂停/交还给 Codex 当前对话执行。
6. 执行后重新检查输出是否达标。
7. 重复，直到 `总交付清单.md` 存在并且 manifest 写入 `completed_at`。

## 阶段

| 阶段 | 类型 | 完成条件 |
| --- | --- | --- |
| import_inbox | local | 投喂区素材复制到 `00_输入素材` |
| source_edit / source_text_correction / source_edit_review | local/codex_native | 原片、清洁视频/音频、消费者文稿、字幕和可追溯剪辑决定通过独立审核 |
| setup_project | local | manifest 存在，故事正文/旁白/绿幕被识别 |
| codex_story_images | codex_native | `01_分镜与图片/images` 或 `02_图生视频/images` 有图片，且分镜文本存在 |
| story_images_review | codex_native | 分镜图片独立视觉审核通过，哈希为当前版本 |
| prepare_jobs | local | `*_image_video_jobs.csv` 存在 |
| timing | local | `timings.json` 或 jobs CSV 写入时长信息 |
| video_prompt_review | codex_native | 动作提示逐镜头确认，审核快照与当前 jobs 语义一致 |
| generate_videos | provider_adapter | 由配置的 API/本地适配器生成全部目标视频片段 |
| video_qa / video_review | local/codex_native | 五点抽帧机器 QA 与独立视觉审核均通过 |
| apply_review | local | `03_背景成片/clips` 和 `script_lines.txt` 存在 |
| music_request | local | Suno 任务书或音乐分段 CSV 存在 |
| suno_generate | browser | `music/suno_downloads` 存在所需音频 |
| assemble_music | local | `*_background_music.mp3` 存在 |
| music_qa | local | 时长覆盖、响度、削波、长静音、分段结构通过，输入哈希仍有效 |
| assemble_final | local | 背景成片三件套存在 |
| release_assets | codex_native | 发布底板、背景图、故事框、keying 参数存在 |
| release_preview | local/codex_native | 站立/大手势双帧的 3×3 抠像候选搜索完成，发布预览与最终参数通过独立审核 |
| package_release / release_video_review | local/codex_native | 主账号/宝库号成片存在并通过五点抽帧独立审核 |
| publish_package / publish_package_review | local/codex_native | 两份文案、六张封面存在并通过独立版式审核 |
| product_preflight | local | 第 16 步前置审查文件和示范预览帧存在 |
| product_annotation / product_annotation_review | codex_native | 朗读标注与示范参数存在并通过独立审核 |
| product_package / product_package_review | local/codex_native | 基础版/进阶版资料包存在并通过独立审核 |
| final_delivery | local | `总交付清单.md` 存在 |
| doctor | local | 工程体检报告存在 |

## Codex 原生动作

这些动作不应该塞进普通 Python 脚本伪装完成，而是由 Codex 当前运行环境执行：

- `codex_story_images`：读取 Agent 生成的出图任务，完成分镜、imagegen、生图文件保存。
- `release_assets`：读取第 12 步任务书，使用 Codex 原生 imagegen 生成发布视觉素材，查看预览并写回 `keying_preset.json`。
- `release_preview`：查看 `preview_contact_sheet.png` 和单帧，必要时改 `keying_preset.json` 后重跑预览。
- `suno_generate`：通过 Chrome/Suno 登录态粘贴提示词、生成、下载、重命名音乐。
- `product_annotation`：按朗读标注 skill 生成精修 `annotation.json`。
- `publish_package`：根据任务书生成最终平台文案和封面衍生图。

## 失败恢复

Agent 必须以“可续跑”为默认：

- 每一步只检查产物，不依赖上一次内存状态。
- 命令失败写入 `story_agent_state.json`，下次从失败阶段继续。
- 本地命令不删除用户素材。
- 对生成文件的覆盖必须局限在当前故事项目目录。
- 遇到登录、验证码、付费弹窗、API 鉴权失败时暂停，写清楚需要用户处理的外部状态。
- 当前已验证后台 `codex exec` 不暴露 Browser 工具；Suno 节点遇到此情况写 `suno_cli_blocker.md`，由 Codex 主任务在已登录浏览器中执行 handoff 后再 `resume/start`，不得在后台盲目重试。

## 当前 v2 边界

`story_agent.py` 已采用“Codex 入口 + 本地持久状态机”结构。工作台不删除，但正常运行不再依赖逐步点击。唯一必需输入是一段横屏绿幕口播原片；系统派生音频、转写、清洁文稿、分镜和字幕。

- 默认软预算 ¥50、硬预算 ¥100、运行时限 10 小时。
- 图生视频供应商由 `video_api.adapters` 选择；`qingyun_api` 是当前生产适配器，`mock_local` 用于零费用端到端与故障注入，模型不写死在状态机中。
- manifest 每阶段记录输入/输出上下文指纹、供应商、实际阶段成本、尝试次数和重试原因。
- `status` 返回剩余阶段、三档经验 ETA、运行/剩余时限、心跳、预算预留、重试/失败明细和具体恢复动作；ETA 不把外部排队或登录等待伪装成确定承诺。
- 智能/视觉阶段必须有独立审核 JSON：至少 85 分、无关键错误、产物哈希一致。
- 图片和视频审核失败时保留失败版本并按镜头重排队；发布预览、终片、朗读标注和资料包同样支持限次回退。
- 发布物料固定为两份账号文案和六张封面（两账号各 3:4、4:3、16:9）。
- 首版仍不自动点击平台发布，也不自动上传网盘。

## Codex CLI 子任务

Agent 支持两种智能节点模式：

```bash
python3 story_agent.py run ... --codex-mode handoff
python3 story_agent.py run ... --codex-mode cli --execute
```

- `handoff`：生成任务文件后暂停，适合人工观察和调试。
- `cli`：由 Python 调度器调用 `codex exec`，把当前智能节点交给 Codex CLI 子进程执行。

CLI 子任务默认使用：

```bash
codex -a never exec \
  --cd "/Volumes/语苗计划/外置硬盘/New project 半自动版" \
  --sandbox workspace-write \
  --add-dir "<故事项目目录>" \
  --skip-git-repo-check
```

注意：

- 在普通终端里运行 Agent 时，Codex CLI 可以直接使用已登录的 ChatGPT 账号。
- 在 Codex 桌面端沙盒里测试 `codex exec`，可能因为 CLI 需要写入 `~/.codex/state_5.sqlite` 而失败；这是桌面沙盒限制，不代表普通终端不可用。
- 可以用下面命令测试 CLI 子任务：

```bash
python3 story_agent.py probe-codex --project-dir /private/tmp/story-agent-codex-probe
```

当前已验证：本机 `codex login status` 显示 `Logged in using ChatGPT`，沙盒外 `probe-codex` 可正常返回。

## v2 日常 CLI

```bash
python3 story_agent.py submit --video "/path/to/greenscreen.mp4"
python3 story_agent.py start --job "<job_id>"
python3 story_agent.py status --job "<job_id>"
python3 story_agent.py cancel --job "<job_id>"
python3 story_agent.py resume --job "<job_id>"
python3 story_agent.py report --job "<job_id>"
```

`submit` 对原片做 SHA-256 去重并创建 manifest v2；`start` 在后台运行 supervisor；`cancel` 不删除任何素材；`report` 生成早晨交付摘要。项目级入口 Skill 位于 `skills/story-full-auto/`。

故障注入回归覆盖死进程锁回收、活锁保护、API 无进展、目标镜头缺失、Codex 子任务失败、Suno 浏览器/登录失效、磁盘不足和取消/续跑。额外 MP4 不得掩盖 jobs CSV 中命名目标的缺失。
