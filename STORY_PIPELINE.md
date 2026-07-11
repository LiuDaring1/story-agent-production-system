# Story Video Composer 全局流程规范

## 目标

Story Video Composer 的最终形态是“极简输入、工作台一条龙、桌面故事文件夹统一交付”。

每期故事优先使用一个桌面项目文件夹：

```text
~/Desktop/故事剪辑：故事名
```

用户只需要提供故事正文和声音来源；如果要生成主账号发布视频和资料包示范视频，再提供已美颜调色后的绿幕视频。集数、故事类型、适龄段、时长文案、人物参考帧、底板图、故事框和 QA 报告都由系统自动生成或在工作台中覆盖。

## 桌面项目结构

系统自动维护这些目录：

```text
00_输入素材
01_分镜与图片
02_图生视频
03_背景成片
04_发布视频
05_发布物料
06_资料包
99_项目状态
```

`99_项目状态/project_manifest.json` 是单期故事的状态中心。工作台和 CLI 都优先读取它，避免反复手动选择路径。

## 全局配置

`pipeline_config.json` 是项目级配置，包含：

- `latest_episode`：当前最新正式集数，默认 92。
- 默认故事类型：`儿童童话故事`。
- 常用故事类型和适龄段选项。
- 品牌 LOGO、水印、防盗标识路径。
- 发布视频和资料包默认参数。

工作台中的用户手动设置优先于配置和模型建议。

## 一条龙流程

1. 选择或创建桌面故事文件夹。
2. 工作台识别故事正文、旁白音频、绿幕视频；没有独立原声时可从绿幕视频提取。
3. 设置或确认故事信息：故事名、集数、故事类型、适龄段、时长文案。
4. 生成 Codex 出图任务，默认保留分镜确认。
5. 图片生成后机器审查数量、比例、命名、文字水印和明显异常。
6. 准备图生视频任务，写入旁白时长，生成视频片段。
7. 机器抽帧审查视频片段，输出重跑建议。
8. 生成 Suno 配乐任务和分段表，人工听选并下载音乐，系统拼接。
9. 合成 16:9 背景成片多版本。
10. 按故事主题生成发布底板图、16:9 主账号背景图和一个统一透明故事框。
11. 在 Codex 中完成发布视觉定版：抽绿幕采样、自动抠像、融合主题素材生成预览、看图调参并写回 `keying_preset.json`。
12. 生成主账号发布视频和宝库号发布视频，并做机器 QA。
13. 生成双账号四平台发布物料，并做机器 QA。
14. 生成基础版和进阶版资料包，并做机器 QA。
15. 生成桌面项目根目录下的 `总交付清单.md`。

## 发布封面生成原则

- 宝库号封面：可以用选中的发布视频截屏生成 3:4 母版，再回到 Codex 对话中使用原生图像生成能力衍生 4:3 和 16:9。
- 主账号封面：禁止直接使用发布视频截屏做最终封面。必须从绿幕原片抽取真人动作参考、从背景故事视频抽取故事画面参考，再回到 Codex 对话中使用原生图像生成能力生成新的 3:4 设计封面，最后基于该母版衍生 4:3 和 16:9。
- 最终封面禁止用 Pillow、HTML/CSS、截图拼接、模板叠字或其他本地脚本合成；本地代码只允许准备参考图、候选帧、提示词、尺寸标准化和文件复制。
- 主账号真人一致性优先级高于风格统一：必须尽量保持主持人参考图中的脸型、五官比例、眼睛、鼻子、嘴型、发际线、发型、肤色、服装和胸前麦克风；如果明显换脸，应重新生成。

## 自动化边界

- 分镜默认人工确认，可选择全自动。
- 图片和视频片段默认机器先审，只把异常列出。
- 绿幕抠像和发布布局默认由 Codex 看融合预览后自动定参，用户只反馈视觉方向，不需要猜参数；定参必须包含开头或手势动作帧，防止只看站定帧时漏掉手部/手臂裁切问题。
- 底板图和故事框每期按故事主题生成，不作为用户输入。
- 音乐听感仍保留人工选择，系统负责提示词、命名、时长覆盖和拼接检查。
- 最终发布前建议人工查看主账号视频、宝库号视频、封面和资料包目录。

## CLI 入口

统一入口仍是：

```bash
python3 story_workflow.py <command>
```

项目层命令：

```bash
python3 story_workflow.py init-project --project-dir "~/Desktop/故事剪辑：故事名"
python3 story_workflow.py detect-assets --project-dir "~/Desktop/故事剪辑：故事名" --extract-audio
python3 story_workflow.py set-story-info --project-dir "~/Desktop/故事剪辑：故事名" --episode 93 --story-type "成语故事"
python3 story_workflow.py qa-images --project-dir "~/Desktop/故事剪辑：故事名"
python3 story_workflow.py qa-videos --project-dir "~/Desktop/故事剪辑：故事名"
python3 story_workflow.py theme-assets --project-dir "~/Desktop/故事剪辑：故事名"
python3 story_workflow.py auto-keying --project-dir "~/Desktop/故事剪辑：故事名"
python3 story_workflow.py package-release-project --project-dir "~/Desktop/故事剪辑：故事名"
python3 story_workflow.py publish-package-project --project-dir "~/Desktop/故事剪辑：故事名"
python3 story_workflow.py product-package-project --project-dir "~/Desktop/故事剪辑：故事名" --allow-draft-annotation
python3 story_workflow.py qa-release --project-dir "~/Desktop/故事剪辑：故事名"
python3 story_workflow.py qa-publish --project-dir "~/Desktop/故事剪辑：故事名"
python3 story_workflow.py qa-product --project-dir "~/Desktop/故事剪辑：故事名"
python3 story_workflow.py final-delivery --project-dir "~/Desktop/故事剪辑：故事名" --update-latest-episode
```

原有 `prepare`、`timing`、`generate`、`apply-review`、`music-request`、`assemble-music`、`assemble`、`package-release`、`publish-package`、`product-package` 保留，作为底层执行单元。
