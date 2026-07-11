# Codex 交接：邯郸学步已跑通，下一步做代码工程体检和小功能

当前时间：2026-05-11 晚  
代码工程目录：`/Volumes/语苗计划/外置硬盘/New project`  
已跑通故事项目：`/Users/baiyanglin/Desktop/故事剪辑：邯郸学步`

## 当前目标

我们已经用 `邯郸学步` 跑通了一次完整生产流程。下一步不是继续纠结某一个步骤，而是：

1. 检视整个代码工程，看它是否已经可以稳定复用到下一个故事。
2. 梳理当前工作台、脚本、目录、配置之间的关系。
3. 找出容易在换故事时出错的硬编码、路径残留、状态残留、流程断点。
4. 未来会加几个小功能
5. 等代码工程整理好后，再用一个新故事做端到端验证。

## 已完成的一次端到端样例

样例故事：`邯郸学步`

项目目录：

`/Users/baiyanglin/Desktop/故事剪辑：邯郸学步`

最终交付清单：

`/Users/baiyanglin/Desktop/故事剪辑：邯郸学步/总交付清单.md`

QA 文件：

- `/Users/baiyanglin/Desktop/故事剪辑：邯郸学步/99_项目状态/qa_product_report.md`
- `/Users/baiyanglin/Desktop/故事剪辑：邯郸学步/99_项目状态/qa_release_report.md`

最后状态：两个 QA 都是“未发现明显问题”。

这个故事可以作为后续工程检视的回归样例。

## 需要先看的工程文件

建议新窗口 Codex 先从这些文件读起：

- `/Volumes/语苗计划/外置硬盘/New project/workbench_app.py`
- `/Volumes/语苗计划/外置硬盘/New project/story_workflow.py`
- `/Volumes/语苗计划/外置硬盘/New project/story_project.py`
- `/Volumes/语苗计划/外置硬盘/New project/release_video.py`
- `/Volumes/语苗计划/外置硬盘/New project/product_package.py`
- `/Volumes/语苗计划/外置硬盘/New project/publish_package.py`
- `/Volumes/语苗计划/外置硬盘/New project/pipeline_config.json`
- `/Volumes/语苗计划/外置硬盘/New project/STORY_PIPELINE.md`
- `/Volumes/语苗计划/外置硬盘/New project/HANDOFF.md`

重点不是只看某一步，而是看整个工程的架构、状态流、入口命令、目录约定和复用稳定性。

## 本次已知重要改动

最近一次为了跑通 `邯郸学步`，主要动了：

- `product_package.py`
- `story_workflow.py`
- `workbench_app.py`

这些改动让资料包流程从“一键直接打包”变成“先生成需要智能审查的材料，再正式打包”。这只是整个工程的一部分，不应该成为下一步唯一重点。

另外修过：

- 示范视频 Logo、人物居中、预览帧、参数读取。
- 示范视频字幕保留“我是绵羊姐姐”。
- 售卖资料/PPT/文稿/朗读标注保持脱敏。
- PPT 长字幕自动换行/缩小。
- 正式资料包不再静默使用自动草稿标注。

## 工程体检重点

请围绕“能不能换一个故事稳定复用”来检查：

1. 工作台按钮是否只是封装命令，命令本身是否都可独立运行。
2. 每一步输入输出是否有清晰目录约定。
3. 是否有 `邯郸学步`、`胡萝卜`、`handan` 等硬编码残留。
4. 是否有路径依赖用户桌面、外置硬盘、Downloads 中某个文件，且缺失时没有提示。
5. `project_manifest.json` 的状态流是否足够完整。
6. `pipeline_config.json` 中哪些是全局配置，哪些应该是项目配置。
7. 工作台日志是否足够告诉用户“下一步该做什么”。
8. 如果中途失败，是否能从当前步骤继续，而不是重头跑。
9. Codex 参与的步骤和本地纯自动步骤是否边界清楚。
10. 最终交付目录、QA、清单是否都能从 manifest 回溯。


正确任务是：

- 先读工程。
- 再做工程体检。
- 再挑几个小功能实现。
- 最后再用新故事验证复用性。

## 给新窗口 Codex 的第一句话

可以直接这样发：

“请先阅读 `/Volumes/语苗计划/外置硬盘/New project/NEXT_CODEX_PROMPT_20260511_HANDAN_DONE.md`。我们已经用 `邯郸学步` 跑通了一次完整流程。现在不要只盯着资料包步骤，请先检视整个代码工程，重点判断它能不能稳定复用到下一个故事；根据用户需求增加一些额外功能，完成工程整理与升级后，再准备跑一个新故事做验证。”

