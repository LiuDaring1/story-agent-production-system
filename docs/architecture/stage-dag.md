# 33 阶段 DAG

权威定义位于 `story_agent_runtime.py` 的 `STORY_STAGE_SEQUENCE`、`STORY_STAGE_DEPENDENCIES`、`STAGE_WRITE_SETS` 和 `STAGE_RESOURCES`。

## 阶段表

| 分支 | 顺序 | 阶段 |
| --- | --- | --- |
| 素材 | 1—5 | `import_inbox` → `source_edit` → `source_text_correction` → `source_edit_review` → `setup_project` |
| 视觉 | 6—14 | `codex_story_images` → `story_images_review` → `prepare_jobs` → `timing` → `video_prompt_review` → `generate_videos` → `video_qa` → `video_review` → `apply_review` |
| 配乐 | 15—18 | `music_request` → `suno_generate` → `assemble_music` → `music_qa` |
| 汇合 | 19 | `assemble_final` 同时等待 `apply_review` 与 `music_qa` |
| 发布资产 | 20 | `release_assets` 在 `setup_project` 后即可并行 |
| 发布视频 | 21—24 | `release_preview` → `package_release` → `release_qa` → `release_video_review` |
| 发布物料 | 25、31 | `publish_package` → `publish_package_review` |
| 产品包 | 26—30 | `product_preflight` → `product_annotation` → `product_annotation_review` → `product_package` → `product_package_review` |
| 交付 | 32—33 | `final_delivery` → `doctor` |

```mermaid
flowchart LR
    I["输入"] --> SE["素材处理 5 阶段"]
    SE --> VI["视觉 9 阶段"]
    SE --> MU["配乐 4 阶段"]
    SE --> RA["发布资产"]
    VI --> A["横屏合成"]
    MU --> A
    A --> RP["发布视频 4 阶段"]
    RA --> RP
    RP --> PUB["发布物料 2 阶段"]
    RP --> PROD["产品包 5 阶段"]
    PUB --> FD["最终交付"]
    PROD --> FD
    FD --> DR["Doctor"]
```

## 并行资源上限

默认 DAG `max_parallel=3`，但资源容量进一步约束：

| 资源 | 容量 | 典型使用者 |
| --- | ---: | --- |
| `codex_exec` | 2 | 模型生产与独立审核 |
| `imagegen` | 1 | 故事图片、主题资产、封面 |
| `browser_suno` | 1 | Suno 浏览器会话 |
| `video_api` | 1 | 图生视频提交 |
| `paid_work` | 1 | 付费任务预算门禁 |
| `ffmpeg_heavy` | 1 | 横屏、竖屏和产品视频渲染 |

因此“最多三个 worker”不等于三个 ImageGen 或三个 FFmpeg 同时运行。

## 完成判定

阶段完成不是“文件存在”，而是对应 predicate 全部满足。关键产物还必须：

1. 通过机器 QA；
2. 生成标准审核 bundle；
3. bundle 中每个文件哈希仍为当前版本；
4. 独立审核分数至少 85；
5. `critical_errors` 为空；
6. 决策文件与当前 jobs 精确匹配。

## 重试原则

- 只隔离失败镜头，不删除旧版本。
- jobs 必须以 `target_video_filename` 逐一核对。
- 提示词、图片或连续性状态变化后，旧审核失效。
- 不允许仅修改 manifest 状态来绕过审核。
- 长期目标是变更镜头增量审核；当前部分阶段仍存在大范围重读与返工放大。
