# Story Agent V3.5 — DSH 迁移工作区约束

## 当前任务

- 这是从 Codex 生产仓复制出的独立 DeepSeek Harness（`dsh`）迁移工作区。
- 保留 Python `story_agent.py` 作为持久执行脊柱、38 阶段 DAG、全部 manifest/receipt/QA/review/currentness 语义。
- 用 provider-neutral adapter 接入 DSH；不要把整套生产系统重写为 DSH 插件。
- 当前 Codex 生产仓和桌面 Canary 不在本工作区范围内，不得读写、移动或启动它们。

## 不可破坏规则

- 正常使用只接受一段横屏绿幕口播原片；脚本、旁白和音乐由系统派生或生成。
- 永不覆盖或删除用户原片；自动剪辑必须保留时间区间和 JSON 决策。
- 通用代码不得写死具体故事标题、道理文本、角色、用户主目录或历史项目路径。
- 密钥只来自环境变量或操作系统安全存储；不得进入代码、配置样例、manifest、prompt、日志、测试 fixture 或 Git。
- 文件存在、命令退出码为 0、模型自述完成都不等于 QA 通过。
- 审核必须使用独立会话/上下文，`approved=true`、分数至少 85、关键错误为空，并绑定被审 bundle 的当前 SHA-256。
- 默认软预算 ¥50、硬预算 ¥100；不设固定运行时限。心跳、取消、磁盘和硬预算门禁必须保留，超过硬预算不得发起新付费调用。
- 图生视频仍通过 `video_provider_adapter.py`；不得在 Runtime 中写死供应商、模型或密钥。
- 配乐必须通过带输入哈希的 `qa_music_report.json`。
- 抠像必须保存 `keying_search.json` 和站立/大手势候选证据；独立审核通过前不得渲染全片。
- 视频完成条件逐一匹配 jobs CSV 的 `target_video_filename`，不得用目录 MP4 数量替代。
- 内部成本、QA、异常与恢复报告只能进入 `99_项目状态`，不得泄漏到客户资料包。
- 六张封面先过比例/尺寸/近似同图机器 QA，再做独立视觉审核。
- 保留旧 CLI 兼容；新增中性参数时，旧 `--codex-*` 参数只能作为明确的 deprecated alias，不能静默改变含义。

## 迁移期间的付费与外部调用门禁

- 在用户明确确认“可以做 provider 微型探针”之前：不得调用 DeepSeek API、ToAPIs、Grok、Suno、FMP 或其他付费/外部生成服务。
- 不得启动任何真实 Story Agent supervisor、真实项目 `start/resume` 或 Canary。
- 可以运行离线单元测试、mock provider、临时目录故障注入、静态检查、CLI `--help` 和 DSH 配置 dump。
- 得到确认后也先做逐项有上限的微型探针；每项保存 request/receipt/实际计费状态，禁止直接跑完整故事。

## 实现顺序

1. 完整阅读 `DSH_MIGRATION_HANDOFF_V35_20260821.md`、`AI_CONTEXT.md`、四份架构/合同文档和现有 Port 代码。
2. 先运行离线基线测试；若失败，记录环境差异，不降低断言。
3. 新增 provider-neutral cognitive executor contract 和 DSH adapter；先用 deterministic fake 验证。
4. 保留所有现有 Codex adapter/CLI 作为兼容实现，不在首阶段删除或批量改名。
5. 再实现 DeepSeek role routing、独立视觉审核和 ToAPIs 图片 adapter；生产调用仍由显式授权门禁控制。
6. 每一阶段都运行相关测试；修改 Agent 状态、投喂、审核或恢复逻辑后运行完整：

```bash
python3 -m unittest discover -s tests -v
```

7. 修改 CLI 后同时运行主命令和对应子命令 `--help`；修改项目 Skill 后运行 skill-creator quick validation。

## DSH 特有注意事项

- DeepSeek Harness 仍处于 developer preview；先记录 `dsh --version` 和 effective config，再依赖任何 CLI/SDK 细节。
- DSH 会读取工作区 `AGENTS.md`；所有会话 cwd 必须位于本迁移包，不能指向原 Codex worktree。
- DeepSeek 内置 chat-completions route 可能仍声明为 text-only。视觉实验模型只有在安装版本真实支持且 custom provider 明确声明 `input: [text, image]` 后才能接收图片。
- 视觉能力声明不是能力证明；必须用合成测试图做一次受控识别并核对输入 SHA。
- 生产者与审核者必须使用不同 session id，审核 session 不得继承生产对话历史。
- Headless 模式只用于无需交互审批的已授权步骤；需要用户审批、登录或浏览器状态时必须暂停并写 blocker。

## 证据与交付

- 每个迁移提交说明：改变了哪个 seam、哪些旧行为保持不变、运行了哪些离线测试、是否发生外部调用。
- mock 结果始终 `production_eligible=false`，不得伪造 QA、review、lock、receipt 或 currentness。
- 迁移完成的最低口径是 Codex/DSH 双 backend 离线 parity 和全部不变量通过；真实 provider 质量、真实成本和用户终验是后续独立里程碑。
