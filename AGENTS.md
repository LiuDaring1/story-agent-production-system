# 全自动故事生产工程约束

## 目标入口

- 唯一生产入口是当前 Codex 前台任务与 `skills/story-full-auto`；正常输入是用户确认的故事文本和已调色横屏绿幕视频。
- `story_pipeline.py` / `story_run.py` 只维护六个工作包、运行事实、产物、依赖和哈希，不调度固定阶段；创意判断与汇合由当前 Codex 任务负责。
- 旧 `story_agent.py`、38 阶段 Runtime、Supervisor、Dashboard、Recovery、工作台及根目录别名已移入 `historical_archive/` 的可校验独立源码归档，不再留生产导入入口。
- 工作台和历史 CLI 只作故障取证与旧项目审计；新代码禁止新增对 `legacy.story_agent_v3` 的依赖。

## 不可破坏规则

- 永不覆盖或删除用户原片；所有自动剪辑必须保留时间区间和 JSON 决策。
- 通用代码不得写死任何具体故事标题、道理文本、角色或历史项目路径。
- 密钥只能来自环境变量或系统安全存储，不得写入仓库、manifest、任务提示或日志。
- 文件存在、命令退出码为 0、生产者自述完成均不等于 QA 通过。
- 审核必须是独立上下文，分数至少 85、关键错误为空，并校验被审产物 SHA-256。
- 生产者开工前读取本工作包适用规则并先做已有机器自检；审核者使用同一份规则来源。退件必须引用既有要求、适用范围和具体产物证据；不得将个人审美偏好或临时发明的规则升级成硬门禁。无效退件先纠正审核，再按原规则复核。
- 保留心跳、取消、磁盘空间、请求幂等、防重提交和有限基础设施重试；金额、币种、预算、入账不是生产状态或准入条件。
- 每次外部或模型请求记录 provider、request_id、model、请求哈希、开始/结束/等待、重试、Token 和成功/失败及错误类型；未报告 Token 保持 `null`，不得写 0。供应商原始响应中的金额可原样归档，不汇总、换算或作门禁。
- 图生视频必须通过 `video_provider_adapter.py` 解析供应商；不要在 `story_pipeline.py`、`story_run.py` 或 `story_workflow.py` 写死供应商、模型或密钥。
- 配乐必须通过带输入哈希的 `qa_music_report.json`，不能仅凭音频文件存在进入最终合成。
- 抠像必须保存 `keying_search.json` 和站立/大手势候选图；独立审核通过前不得渲染全片。
- 视频片段完成条件必须逐一匹配 jobs CSV 的 `target_video_filename`，不能用目录 MP4 数量代替。
- QA 汇总和异常说明只能写入 `99_项目状态`，不得混入基础版/进阶版客户资料包。
- 六张封面必须通过比例/尺寸/近似同图机器 QA，再由独立视觉审核判断版式和真人一致性。

## 修改与验证

- 修改原生流水线状态、投喂或审核逻辑后运行：`python3 -m unittest discover -s tests -v`。
- 修改控制入口后同时运行：`python3 story_pipeline.py --help`、`python3 story_pipeline.py describe` 和对应内部命令 `--help`。
- 修改历史归档时运行 `python3 historical_archive/verify.py`；不为验证归档而恢复或执行旧生产入口。
- 修改项目 Skill 后运行 skill-creator 的 `quick_validate.py skills/story-full-auto`。
- 不使用工作台按钮作为自动化测试证据；优先用临时目录、模拟供应商和故障注入。

## v2 候选合同适用范围

新项目显式使用 `story-production/v2`，以 `story_production_v2.py` 和生产 Skill 的 v2 合同为准。音乐为用户成品，不要求音乐 QA；独立封面、文案、朗读标注、PPTX 退出主链。上文对应旧交付要求仅用于历史账本读取。保留全部视频、抠像、Logo、字幕和独立审核保护，不改全局模型配置。候选版须经真实新故事 QA 与用户体验接受方可晋级默认。
