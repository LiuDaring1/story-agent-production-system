# 半自动高效版流程规范

## 隔离原则

半自动版是老工作台的独立复制版，只修改本目录内代码。老版不引用本版，本版也不回写老版工作台代码。

## 状态中心

每个故事项目的状态仍由：

```text
99_项目状态/project_manifest.json
```

维护。半自动版新增：

```json
{
  "manual_outputs": {
    "background_video_no_sub": "",
    "background_video_with_sub": "",
    "demo_voice_bgm": "",
    "main_release_video": "",
    "library_release_video": "",
    "a_scene_no_person_video": "",
    "annotation_file": "",
    "ppt_file": ""
  },
  "semi_auto": {
    "mode": "manual_first",
    "current_stage": "",
    "parallel_tasks": {}
  }
}
```

登记手工产物时，会同步写入兼容旧脚本的 `outputs` 字段。

## CLI

新增命令：

```bash
python3 story_workflow.py semi-auto-status --project-dir "/path/to/故事剪辑：故事名"
python3 story_workflow.py register-output --project-dir "/path/to/故事剪辑：故事名" --kind main_release_video --source "/path/to/video.mp4"
python3 story_workflow.py qa-manual-outputs --project-dir "/path/to/故事剪辑：故事名"
python3 story_workflow.py publish-package-draft-project --project-dir "/path/to/故事剪辑：故事名"
```

`register-output --kind` 支持：

- `background_video_no_sub`
- `background_video_with_sub`
- `demo_voice_bgm`
- `main_release_video`
- `library_release_video`
- `a_scene_no_person_video`
- `annotation_file`
- `ppt_file`

## 人工优先边界

- 背景成片默认人工优先，机器合成保留为备用。
- 主账号发布视频默认人工优先。
- 宝库号发布视频可人工，也可机器生成。
- 发布物料草稿不依赖发布视频完成；最终 QA 再检查完整性。
- 外部朗读标注由用户在外部 AI/工具中完成，工作台只登记和打包。

