# 新 Codex 对话接手提示

请接手这个本地 Story Video Composer 工作台项目，注意使用外置硬盘版本：

```text
/Volumes/语苗计划/外置硬盘/New project
```

桌面故事项目：

```text
/Users/baiyanglin/Desktop/故事剪辑：邯郸学步
```

先阅读这些文件，了解当前流程和第 16 步入口：

```text
/Volumes/语苗计划/外置硬盘/New project/HANDOFF.md
/Volumes/语苗计划/外置硬盘/New project/STORY_PIPELINE.md
/Volumes/语苗计划/外置硬盘/New project/story_workflow.py
/Volumes/语苗计划/外置硬盘/New project/story_project.py
/Volumes/语苗计划/外置硬盘/New project/product_package.py
/Volumes/语苗计划/外置硬盘/New project/workbench_app.py
```

## 当前状态

《邯郸学步》已经完成到第 15 步。第 15 步发布物料先暂时封存，不要继续纠结封面人脸一致性；用户决定当前主账号封面先保留，后续需要时人工后期合成。

第 14 步发布视频已生成：

```text
/Users/baiyanglin/Desktop/故事剪辑：邯郸学步/04_发布视频/主账号发布视频.mp4
/Users/baiyanglin/Desktop/故事剪辑：邯郸学步/04_发布视频/宝库号发布视频.mp4
```

第 15 步发布物料目录已生成：

```text
/Users/baiyanglin/Desktop/故事剪辑：邯郸学步/05_发布物料
```

其中第 15 步的工作台逻辑已经改成“发布物料任务书”模式，类似第 12 步：工作台准备文案、候选帧、提示词和交接文件；真正的封面成图必须回到 Codex 原生生图能力完成。交接文件在：

```text
/Users/baiyanglin/Desktop/故事剪辑：邯郸学步/05_发布物料/publish_package_codex_handoff.md
```

不要再用本地脚本、Pillow、HTML/CSS 或截图拼贴去合成最终封面。

## 现在要做的事：第 16 步资料包

用户要进入工作台第 16 步“生成资料包”。请从资料包生成和 QA 开始，不要回头重做第 12-15 步，除非第 16 步明确缺依赖。

推荐先运行：

```bash
cd "/Volumes/语苗计划/外置硬盘/New project"
/opt/miniconda3/bin/python3 story_workflow.py product-package-project --project-dir "/Users/baiyanglin/Desktop/故事剪辑：邯郸学步" --allow-draft-annotation
```

这一步应该生成：

```text
/Users/baiyanglin/Desktop/故事剪辑：邯郸学步/06_资料包
```

并自动跑资料包 QA，报告通常在：

```text
/Users/baiyanglin/Desktop/故事剪辑：邯郸学步/99_项目状态/qa_product_report.md
```

## 第 16 步需要重点检查

运行前先快速确认必要输入是否存在；如果命令报缺文件，按报错去补，不要猜：

- 故事正文：`00_输入素材/story_source.txt`
- 逐行台词：`03_背景成片/script_lines.txt`
- 旁白或提取旁白音频
- 配乐文件
- 分镜图片目录
- 含字幕背景视频和无字幕背景视频
- 绿幕视频
- 抠像参数：`04_发布视频/keying/keying_preset.json`
- `timings.json`

资料包产物要重点看：

- 是否有基础版和进阶版两个目录。
- 背景视频、PPT、配乐、文稿、朗读标注、示范视频是否都在。
- 示范视频里绿幕人物是否抠像正常，人物大小和位置是否沿用第 12-14 步确认过的参数。
- PPT 翻页、字幕、旁白节奏是否大致同步。
- 如果使用 `--allow-draft-annotation`，要提醒用户：朗读标注是规则草稿，可用于预览；正式售卖前最好再人工或模型精修。

## 已确认的重要参数

当前发布视频最终抠像/布局参数在：

```text
/Users/baiyanglin/Desktop/故事剪辑：邯郸学步/04_发布视频/keying/keying_preset.json
```

关键参数：

```json
{
  "keyer": "colorkey",
  "chroma_color": "0x34944D",
  "chroma_similarity": 0.145,
  "chroma_blend": 0.02,
  "person_crop": [190, 662, 1770, 2600],
  "person_height_ratio": 1.04,
  "person_x": 1085,
  "person_y": 78
}
```

重要经验：不能只看站定帧，必须检查开头或手势动作帧，防止手部裁切。这一点如果资料包示范视频也有抠像预览或 QA，应继续沿用。

## 用户偏好

- 少解释，多检查实际文件和输出。
- 能跑工具就跑工具，不要停在方案。
- 如果第 16 步报错，先读代码和项目 manifest，定位缺哪个输入。
- 如果资料包能生成，直接打开/列出 `06_资料包` 结构和 QA 结论。
- 第 15 步封面问题先不继续 debug，后续用新故事再整体测试。
