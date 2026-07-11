---
name: mianyang-publish-package
description: 为绵羊姐姐主账号和绵羊姐姐语言节目宝库生成精简发布物料。用户说“发布包”“发布物料”“从视频截封面”“生成封面候选帧”“做发布封面”时使用。流程不生成发布文案，只准备候选帧和 Codex 原生生图任务，最终交付主账号 4:3 封面、宝库号 4:3 封面各一张。
---

# 绵羊姐姐精简发布物料

## 适用场景

当一期故事已经完成主账号发布视频和宝库号发布视频后，用本 Skill 生成精简发布物料：

- 不生成发布文案；发布文案不在第 15 步处理。
- 不生成 3:4 发布封面；上传发布站后直接从视频中选帧。
- 不生成 16:9 发布封面。
- 只生成两张最终封面：主账号 `cover_4x3.png`、宝库号 `cover_4x3.png`。
- 候选帧仍然需要抽取，用于选择能代表故事情节和角色动作的生图参考。

## 输入

必须收集或从工作区定位：

- 故事名称
- 第几集
- 故事时长
- 完整故事正文 txt/md
- 主账号发布视频
- 宝库号发布视频
- 输出目录

可选输入：

- 故事类型
- 适合年龄
- 主账号高清绿幕人物参考帧，优先从原始绿幕视频抽帧
- 背景故事成片，用于抽主账号故事画面参考

## 标准流程

1. 运行工作台脚本，生成候选帧、参考图和生图任务书：

```bash
python3 story_workflow.py publish-package-project \
  --project-dir "/path/to/project" \
  --generate-covers \
  --library-frame <编号> \
  --main-person-frame <编号> \
  --main-story-frame <编号>
```

如果还没有确认编号，先运行不带编号的版本，查看：

- `frame_candidates/main_候选帧索引.jpg`
- `frame_candidates/library_候选帧索引.jpg`
- `main/covers/person候选帧索引.jpg`
- `main/covers/story候选帧索引.jpg`

2. 宝库号选帧规则：

- 选择能体现故事关键情节或角色动作的帧。
- 不要选标题页、空景、普通书卷景或纯资料展示帧。
- 选好后用 `--library-frame <编号>` 重跑。

3. 主账号选帧规则：

- 主账号 4:3 封面优先保持真人一致性。
- 人物参考选表情自然、正面清楚、服装和麦克风完整的绿幕帧。
- 故事画面参考选最能代表冲突、动作或情绪变化的帧。

4. 使用 `publish_package_codex_handoff.md` 和对应 `cover_derivative_prompts.md`，通过 Codex 原生图像生成能力生成：

- `main/covers/cover_4x3.png`
- `library/covers/cover_4x3.png`

生成后只允许做尺寸标准化、文件复制和轻微压缩。禁止用 Pillow、HTML/CSS、截图拼接、模板叠字或其他本地脚本合成最终封面。

## 生成原则

主账号：

- 必须是统一场景型 4:3 封面，不是普通视频截图，也不是模板拼贴。
- 真人一致性优先，尽量保持参考帧的脸型、五官比例、发型、服装、麦克风和姿态。
- 标题、故事时长、年龄和资料包信息要清楚。
- 禁止木质大相框、内嵌小画面、白色信息卡、分栏排版和截图边框。

宝库号：

- 统一场景型 4:3 封面，资源信息用同一条底部信息带或自然场景标识表达。
- 无真人，突出标题、故事类型、时长、适合年龄和资料内容。
- 清楚表达买了能得到什么：背景视频、PPT、配乐、文稿、朗读标注、示范视频。
- 禁止六宫格、资源卡片矩阵、白色卡片、播放器框、PPT框、木质大相框和模板拼贴感。

## 交付检查

发布物料至少包含：

- `publish_package_codex_handoff.md`
- `frame_candidates/main_候选帧索引.jpg`
- `frame_candidates/library_候选帧索引.jpg`
- `main/covers/reference_3x4.png`
- `main/covers/cover_4x3.png`
- `library/covers/reference_3x4.png`
- `library/covers/reference_choice.md`
- `library/covers/cover_4x3.png`

不要求也不生成：

- `main/copy.md`
- `library/copy.md`
- `cover_3x4.png`
- `cover_16x9.png`
