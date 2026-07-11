# Story Video Composer 工程二层体检

检查时间：2026-05-12

## 结论

当前主流程已经可以按“桌面故事项目 + manifest + 统一 CLI/工作台”复用到新故事。二层体检主要收口了三类复用风险：

1. 配置边界：把本机 skill 路径从代码散落项收进 `pipeline_config.json` 的 `external_tools`。
2. 状态回溯：补齐 `storyboard`、`jobs_csv`、`review_html` 的自动发现，工程体检和交付清单更容易从 manifest 回溯。
3. 样例残留：保留邯郸/胡萝卜作为历史样例或特例模板，但把会误触的新故事默认值改成中性值，并给邯郸一次性脚本加 legacy 标记。

## 已处理

- 新增/保留 `doctor-project`，并把报告接入工作台 `⑲ 工程体检`。
- `pipeline_config.json` 新增：
  - `external_tools.suno_story_score_skill`
  - `external_tools.story_performance_script_skill`
- `story_workflow.py music-request` 默认从 `pipeline_config.json` 读取 Suno skill。
- `product_package.py` 新增 `--annotation-skill-path`，第 16 步前置审查和朗读标注请求不再写死 `/Users/.../Downloads`。
- `prepare_suno_music_request.py` 的兜底默认改为 `Path.home() / "Downloads"`。
- `prepare_image_video_jobs.py` 的底层默认 slug 从历史 `huluobo-yaoguai` 改为 `story`。
- `generate_handan_release_assets.py` 和 `postprocess_handan_imagegen_assets.py` 标为邯郸回归样例专用 legacy helper。

## 保留项说明

- `publish_package.py` 中的 `邯郸学步`、`自相矛盾` 等是文案特例词库。未知故事会走通用逻辑，不会强制套用邯郸。
- `tmp/imagegen/handan_*`、`NEXT_CODEX_PROMPT*` 是历史交接/生成记录，不参与工作台按钮和统一 CLI 主链路。
- `README.md` 仍保留部分早期底层命令示例；正式新故事流程以 `STORY_PIPELINE.md`、工作台推荐按钮和 `story_workflow.py` 项目级命令为准。

## 下一步验证建议

用一个全新桌面故事项目验证：

```bash
python3 story_workflow.py init-project --project-dir "~/Desktop/故事剪辑：新故事名"
python3 story_workflow.py doctor-project --project-dir "~/Desktop/故事剪辑：新故事名"
```

期望：新项目生成稳定 slug，体检能提示缺少输入素材，但不出现旧故事路径、旧样例文件或误判的 handan/huluobo 依赖。
