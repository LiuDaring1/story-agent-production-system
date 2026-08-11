# AI 阅读入口

本文是任何新 AI、维护者或审查者理解本工程的最短路径。先读本文，再按任务需要读取链接文档，不要无目的遍历整个仓库、历史运行目录或媒体文件。

## 一句话定义

这是一个面向儿童故事视频生产的持久化 Agent 系统。正常入口只接受一段横屏绿幕口播原片；系统派生文本、故事画面、图生视频、配乐、横屏成片、竖屏发布视频、封面文案、PPT、朗读标注和客户资料包。

Codex 是用户入口，`story_agent.py` 是持久执行脊柱，`story_agent_runtime.py` 定义阶段图、状态、预算、并行资源和审核合同。工作台仅作诊断与旧流程兼容。

## 必读顺序

1. [`AGENTS.md`](AGENTS.md)：不可破坏规则。
2. [`docs/architecture/overall-architecture.md`](docs/architecture/overall-architecture.md)：五层架构和数据流。
3. [`docs/architecture/stage-dag.md`](docs/architecture/stage-dag.md)：33 个阶段、依赖和并行边界。
4. [`docs/architecture/model-routing.md`](docs/architecture/model-routing.md)：Sol/Luna 两角色路由。
5. [`docs/contracts/review-and-safety.md`](docs/contracts/review-and-safety.md)：审核、哈希、预算和密钥合同。
6. [`docs/operations/runbook.md`](docs/operations/runbook.md)：提交、启动、恢复、诊断和验证。
7. [`docs/postmortems/2026-08-lizard-tail.md`](docs/postmortems/2026-08-lizard-tail.md)：《小壁虎借尾巴》真实生产复盘。

## 核心代码地图

| 文件 | 责任 |
| --- | --- |
| `story_agent.py` | 用户任务、阶段执行、DAG worker、模型路由、恢复与 supervisor |
| `story_agent_runtime.py` | manifest v2、33 阶段 DAG、资源锁、预算、审核与哈希合同 |
| `story_workflow.py` | 把阶段翻译为本地脚本、FFmpeg 和产品制作命令 |
| `story_project.py` | 项目目录、产物检测、机器 QA、交付和内部报告 |
| `story_semantics.py` | 标题、主持人口播、正文、道理和结尾的语义合同 |
| `video_provider_adapter.py` | 视频供应商、模型能力、时长与成本接口 |
| `run_image_video_jobs.py` | 视频任务提交、轮询、下载和逐镜时长 |
| `release_video.py` | 竖屏布局、抠像、画框完整性、尾帧和发布渲染 |
| `product_package.py` | 故事锦囊、PPT、示范视频、朗读标注和客户资料包 |
| `publish_package.py` | 六张封面、文案和发布物料 |

## 外部能力

- 图片：Codex ImageGen。
- 视频：通过 `video_provider_adapter.py`；当前首选 ToAPIs Grok Video 1.5。
- 音乐：Ego Browser/Suno，必须经过带输入哈希的音乐 QA。
- 媒体：FFmpeg/ffprobe。
- 文档：LibreOffice、python-docx、PPTX 工具链。

## 两角色模型合同

- 指挥官：`gpt-5.6-sol`，负责关键判断和独立审核。
- 工人：`gpt-5.6-luna`，负责生产、工具执行和确定性工作。
- 当前配置使用 Sol X-high 与 Luna Max；这是基线，不代表所有任务永久必须使用最高档。
- 独立审核必须与生产上下文隔离，不能让生产者自证完成。

## 不要做

- 不要把真实密钥写入代码、Markdown、日志、manifest 或命令参数。
- 不要提交 `auto-project/`、媒体、客户资料、浏览器会话或原始对话导出。
- 不要把文件存在或命令成功当成 QA 通过。
- 不要绕过 SHA-256 绑定审核或直接手改阶段为 passed。
- 不要在通用代码中写死具体故事、角色、道理或历史项目路径。
- 不要删除、覆盖用户原片。

## 修改后的最低验证

```bash
python3 -m unittest discover -s tests -v
python3 story_agent.py --help
```

修改子命令时同时运行对应 `--help`；修改 `skills/story-full-auto` 时运行 skill-creator 的 `quick_validate.py`。

## 当前已知重点

- 《小壁虎借尾巴》基线已交付，但暴露出返工粒度过大、上下文重复、运行账本不足和成本币种错误等问题。
- 当前成本字段名以 CNY 表示，但 ToAPIs 模型页按美元报价；在修正前不能把本地估算当作人民币实扣。
- 优化应先解决可观察性、增量返工、上下文包和真实账单，再比较模型档位。
