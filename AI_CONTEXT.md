# AI 阅读入口

本文是新 Codex 任务、维护者和审查者理解工程的最短路径。当前生产系统与旧 Story Agent 已分家；不要从旧 DAG、工作台或历史运行目录开始理解工程。

## 一句话定义

这是一个由 Codex 前台主导的儿童故事 Agentic Workflow。用户提供确认文本和已调色横屏绿幕视频；Codex 根据真实产物动态规划并调用确定性模块，使用轻量账本、独立审核、QA 和 SHA-256 回执完成视频、发布物料、封面、PPT 与资料包。

## 唯一入口

- 用户入口：当前 Codex 任务。
- 生产策略：`skills/story-full-auto/SKILL.md`。
- 内部控制入口：`story_pipeline.py`。
- 状态事实源：项目 `99_项目状态/story_run.json`。
- 旧 `story_agent.py` 不是生产入口；其实现位于 `legacy/story_agent_v3/`。

## 必读顺序

1. [`AGENTS.md`](AGENTS.md)：不可破坏规则和新旧边界。
2. [`docs/architecture/execution-chain.md`](docs/architecture/execution-chain.md)：逐步说明谁决策、谁执行、Schema 做什么、结果与记录去哪里。
3. [`docs/architecture/codex-native-story-pipeline.md`](docs/architecture/codex-native-story-pipeline.md)：当前架构、数据流和观察方法。
4. [`skills/story-full-auto/SKILL.md`](skills/story-full-auto/SKILL.md)：总生产策略。
5. [`skills/story-full-auto/references/codex-native-workflow.md`](skills/story-full-auto/references/codex-native-workflow.md)：并行工作包与汇合顺序。
6. [`skills/story-r2v-director/SKILL.md`](skills/story-r2v-director/SKILL.md)：R2V 导演、资产与连续性规范。
7. [`skills/story-full-auto/references/delivery-contract.md`](skills/story-full-auto/references/delivery-contract.md)：最终交付合同。
8. [`legacy/story_agent_v3/LEGACY.md`](legacy/story_agent_v3/LEGACY.md)：仅在审计旧项目或修改兼容层时读取。

## 当前代码地图

| 文件/目录 | 责任 |
|---|---|
| `story_pipeline.py` | 唯一受支持的内部状态控制入口；转交轻量账本命令并描述架构 |
| `story_run.py` | 六个工作包、成本、当前产物、哈希、恢复与最终封口 |
| `story_evidence.py` | 与旧 Runtime 解耦的通用审核 bundle/currentness 能力 |
| `shot_storyboard_pipeline.py` | 资产审核后逐镜生成/封存故事板，并同源编译 R2V 与 PPT 消费者 |
| `static_ppt_contract.py` | 动态导演镜头页数、双版静态 PPT 和客户目录交付回执 |
| `story_workflow.py` | 确定性工程命令的兼容汇入口，不负责总调度 |
| `story_project.py` | 项目路径、素材发现、部分 QA 和交付工具 |
| `video_provider_adapter.py` / `run_image_video_jobs.py` | 视频供应商能力解析、付费前门禁、提交/轮询/下载 |
| `assemble_r2v_story.py` / `r2v_group_qa.py` / `r2v_local_postfix.py` | R2V 组装、安全后处理和整组 QA |
| `production_keying.py` / `keying_quality.py` / `rvm_keying.py` | 抠像、候选证据与锁定 |
| `release_geometry.py` / `release_video.py` | A/B/C 发布布局、故事框遮挡与正式渲染 |
| `product_package.py` / `product_quality.py` | 示范视频、文稿、朗读标注、PPT 与双版客户资料包 |
| `publish_package.py` / `cover_quality.py` | 双账号文案与六张封面 |
| `legacy/story_agent_v3/` | 已退役固定 38 阶段后台系统，只读兼容与审计 |

## 架构边界

- Skill 是策略，不是进程、调度器或完成证据。
- Codex 前台是唯一指挥官；不得启动嵌套 `codex exec` 或旧 supervisor。
- `story_pipeline.py` 只保存事实，不替 Codex 作创意判断，也不恢复旧 stage。
- 小型模块可以复用；任何通用能力应位于根目录中性模块，不得从 Legacy 反向导入。
- 供应商只能通过 adapter 进入；密钥只能来自环境或安全存储。
- 文件存在和命令成功不等于 QA 通过。

## 外部能力

- 图片：Codex ImageGen。
- 视频：`video_provider_adapter.py` 解析的供应商，当前主线为 ToAPIs Grok R2V。
- 音乐：Suno 工作流及带输入哈希的音乐 QA。
- 媒体：FFmpeg/ffprobe。
- 文档和 PPT：python-docx、PPTX/Artifact 工具和 LibreOffice 渲染验证。

## 修改后的最低验证

```bash
python3 -m unittest discover -s tests -v
python3 story_pipeline.py --help
python3 story_pipeline.py describe
python3 story_pipeline.py status --help
```

修改 `skills/story-full-auto` 后运行 skill-creator 的 `quick_validate.py`。只有修改 Legacy 兼容层时才额外运行 `python3 story_agent.py --help`。

## Legacy 原则

旧 V3 的固定阶段、后台恢复和 Dashboard 是历史教训，不继续演进。根目录兼容模块暂时保留，以便旧项目、测试和审计仍能导入；新故事、现行 Skill、`story_pipeline.py` 和新模块不得调用它。未来删除 Legacy 必须另有迁移测试证明历史数据和审计证据已有替代。
