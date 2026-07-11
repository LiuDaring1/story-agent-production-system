# 下一轮 Codex 接手提示：故事生产工作台阶段性收口

请先确认当前真实代码项目路径，不要使用旧副本：

`/Volumes/语苗计划/外置硬盘/New project`

旧路径 `/Users/baiyanglin/Documents/New project` 不要再用。之前曾因误用旧副本导致修改落错位置。

## 当前结论

《卧薪尝胆》已经用新的故事完整验证了一遍主流程，可以把当前阶段初步画上句号。

已经完成到：

- 第 1-11 步：故事输入、分镜/图片、图生视频、音乐、背景成片。
- 第 12-15 步：发布素材任务书、发布预览/定参、发布视频、发布物料与封面。
- 第 16-17 步：资料包前置审查、精修朗读标注、正式资料包生成。
- 第 18 步：最终交付清单生成。

当前故事项目路径：

`/Users/baiyanglin/Desktop/故事剪辑：卧薪尝胆`

总交付清单：

`/Users/baiyanglin/Desktop/故事剪辑：卧薪尝胆/总交付清单.md`

## 接手后请先读这些文件

按顺序阅读：

1. `/Volumes/语苗计划/外置硬盘/New project/README.md`
2. `/Volumes/语苗计划/外置硬盘/New project/workbench_app.py`
3. `/Volumes/语苗计划/外置硬盘/New project/story_workflow.py`
4. `/Volumes/语苗计划/外置硬盘/New project/story_project.py`
5. `/Volumes/语苗计划/外置硬盘/New project/product_package.py`
6. `/Volumes/语苗计划/外置硬盘/New project/publish_package.py`
7. `/Volumes/语苗计划/外置硬盘/New project/release_video.py`
8. `/Users/baiyanglin/Desktop/故事剪辑：卧薪尝胆/99_项目状态/project_manifest.json`
9. `/Users/baiyanglin/Desktop/故事剪辑：卧薪尝胆/总交付清单.md`
10. `/Users/baiyanglin/Desktop/故事剪辑：卧薪尝胆/99_项目状态/qa_product_report.md`
11. `/Users/baiyanglin/Desktop/故事剪辑：卧薪尝胆/99_项目状态/qa_release_report.md`
12. `/Users/baiyanglin/Desktop/故事剪辑：卧薪尝胆/99_项目状态/qa_publish_report.md`

如果目标是继续工程整理，还可以读：

- `/Volumes/语苗计划/外置硬盘/New project/NEXT_CODEX_PROMPT_20260511_HANDAN_DONE.md`
- `/Volumes/语苗计划/外置硬盘/New project/ENGINEERING_AUDIT_20260512.md`

## 《卧薪尝胆》关键产物

发布视频：

- `/Users/baiyanglin/Desktop/故事剪辑：卧薪尝胆/04_发布视频/主账号发布视频.mp4`
- `/Users/baiyanglin/Desktop/故事剪辑：卧薪尝胆/04_发布视频/宝库号发布视频.mp4`

发布物料：

- `/Users/baiyanglin/Desktop/故事剪辑：卧薪尝胆/05_发布物料/main/copy.md`
- `/Users/baiyanglin/Desktop/故事剪辑：卧薪尝胆/05_发布物料/library/copy.md`
- `/Users/baiyanglin/Desktop/故事剪辑：卧薪尝胆/05_发布物料/main/covers/`
- `/Users/baiyanglin/Desktop/故事剪辑：卧薪尝胆/05_发布物料/library/covers/`

资料包：

- `/Users/baiyanglin/Desktop/故事剪辑：卧薪尝胆/06_资料包/绵羊故事锦囊：卧薪尝胆（基础版）`
- `/Users/baiyanglin/Desktop/故事剪辑：卧薪尝胆/06_资料包/绵羊故事锦囊：卧薪尝胆（进阶版）`

第 16 步精修朗读标注：

- `/Users/baiyanglin/Desktop/故事剪辑：卧薪尝胆/06_资料包/_work/annotation.json`

第 18 步交付清单：

- `/Users/baiyanglin/Desktop/故事剪辑：卧薪尝胆/总交付清单.md`

## 最近重要代码修改

真实项目里已修改：

- `/Volumes/语苗计划/外置硬盘/New project/workbench_app.py`
- `/Volumes/语苗计划/外置硬盘/New project/story_workflow.py`
- `/Volumes/语苗计划/外置硬盘/New project/story_project.py`

重要改动包括：

- 第 12/13/15 步按钮文案更清楚。
- 第 13 步会生成并复制：
  `/Users/baiyanglin/Desktop/故事剪辑：卧薪尝胆/99_项目状态/release_preview_frames/release_preview_feedback_to_codex.md`
- 第 16 步拆成“前置审查”和“正式打包资料包”两段，正式资料包必须传入精修后的 `--annotation-json` 或 `--annotation-docx`，不要静默使用自动草稿标注。
- 第 18 步完成后会写入 `completed_at`，工作台总耗时会冻结，不再一直按当前时间增长。

## 已验证的关键口径

发布预览最终参数：

`/Users/baiyanglin/Desktop/故事剪辑：卧薪尝胆/04_发布视频/keying/keying_preset.json`

```json
{
  "person_height_ratio": 1.06,
  "person_x": 1065,
  "person_y": 45,
  "chroma_similarity": 0.16,
  "chroma_blend": 0.025
}
```

资料包示范视频参数：

- `demo_person_crop_mode`: `full-width`
- `demo_person_vertical_align`: `bottom`
- `demo_person_crop_bottom_ratio`: `0.055`

第 16 步示范视频预览帧人工审查结论：

- 两只手完整，没有被左右裁掉。
- 人物底部没有露横线，也没有奇怪空隙。
- Logo 在左上安全区，不压人物和字幕。
- 字幕正常，没有挤出画面。

资料包 QA：

`/Users/baiyanglin/Desktop/故事剪辑：卧薪尝胆/99_项目状态/qa_product_report.md`

结论：未发现明显问题。

额外检查：

- 对外售卖的 `docx/pptx` 已扫描，未发现“绵羊姐姐”。
- 示范表演视频为 `1920x1080`，约 `126.4s`，含音频。

## 工作台使用提醒

启动工作台请在真实项目根目录运行：

```bash
cd "/Volumes/语苗计划/外置硬盘/New project"
python3 workbench_app.py
```

如果使用 `/opt/miniconda3/bin/python3` 更稳定，也可以：

```bash
cd "/Volumes/语苗计划/外置硬盘/New project"
/opt/miniconda3/bin/python3 workbench_app.py
```

如果当前已经打开了旧版工作台窗口，代码改动不会热更新；需要关闭后重新打开。

## 给下一轮 Codex 的任务建议

如果用户想继续打磨工程，请优先做这些：

1. 梳理工作台的阶段边界，让第 16/17/18 步状态更明确。
2. 检查所有路径是否仍有硬编码旧路径或特定故事名。
3. 把“前置审查 -> Codex 精修 -> 正式执行”的交接文件格式统一。
4. 做一次轻量工程体检，确认新故事从头跑时不会回退到邯郸学步特例。
5. 如果用户要开新故事，先用工作台创建新故事项目，再按按钮顺序推进；不要拿《卧薪尝胆》的输出目录当新项目。

如果用户只是要新对话接手，请直接把下面这段复制给新对话：

```text
请接手这个故事生产工作台项目。

真实代码项目路径是：
/Volumes/语苗计划/外置硬盘/New project

不要使用旧路径：
/Users/baiyanglin/Documents/New project

请先阅读：
/Volumes/语苗计划/外置硬盘/New project/NEXT_CODEX_PROMPT_20260512_WOXIN_DONE.md

然后按里面列出的顺序阅读 README、workbench_app.py、story_workflow.py、story_project.py、product_package.py，以及《卧薪尝胆》的 project_manifest.json 和总交付清单。

当前《卧薪尝胆》项目已经完整跑到第 18 步，总交付清单已生成：
/Users/baiyanglin/Desktop/故事剪辑：卧薪尝胆/总交付清单.md

你的任务不是重做《卧薪尝胆》，而是先理解当前工程状态、已验证流程、关键修改和剩余工程整理点。除非我明确要求，否则不要回头重跑已完成产物。
```
