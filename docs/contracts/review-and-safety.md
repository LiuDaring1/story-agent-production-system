# 审核、安全、运行事实与隐私合同

## 审核合同

关键审核必须同时满足：

- 独立上下文，生产者不能自证；
- `approved=true`；
- `score >= 85`；
- `critical_errors=[]`；
- `artifact_sha256` 对应当前标准 bundle；
- bundle 中所有 artifact 仍存在且哈希不变；
- 需要逐镜或逐页证据时提供 `evidence_matrix`。

文件存在、命令返回 0、模型声称完成均不是通过证据。

## 当前生产的规则前置与退件依据

当前入口是 Codex 前台任务与 `skills/story-full-auto`。开工前按该 Skill 的「工作交接与审核依据」读取本工作包的适用规则；生产和审核使用同一份可定位的要求及来源，生产者先运行已有机器自检。退件须绑定现有要求和具体产物证据，模型新增审美偏好不得变成硬门禁。无效退件需纠正审核后按原规则复核，不能直接返工或直接判作品通过。

## 历史 V3.5 合同门禁（只供旧项目取证）

旧 V3.5 Runtime 曾先执行 `story_contract` 与 `story_contract_review`；下面描述只解释历史回执，不能作为启动或恢复旧生产入口的指令。合同包含 `semantic_artifacts / visual_style / characters / world_scale / story_state / brand / release_layout` 七节；Luna 只生成草案，Sol 独立审核，Runtime 在重新验证全部绑定后写 crash-safe 锁。规则来源固定为 `task_input > project_config > brand_or_global_default > agent_inference`，模型推断不得伪装为前三类。

锁不是一个状态字段，而是合同文件、canonical contract、可信来源链、审核 bundle、审核 JSON 的完整 SHA-256 等式。半写、损坏、缺字段或任一哈希变化都阻断生产。六类消费者使用最小 section projection 和 completed receipt；当前实现模块族级增量失效，不是逐镜头依赖图。详见 [`story-production-contract.md`](story-production-contract.md)。

## 运行事实合同

生产链不再管理预算、币种换算、入账、成本汇总或金额门禁。保留 provider/model、request/task ID、请求哈希、起止时间、耗时、等待、重试、成功/失败、错误类型和可获得的 Token。历史金额字段只读保留，既不删除也不参与 status、record、finalize 或供应商准入。

`story_run.py observe-performance` 只登记有证据的计划/机器检查耗时、独立审核轮次、无效退件数和重复编码数；重复 provider 请求由相同 provider + request SHA 的不同 request ID 计算，最长依赖链由当前产物依赖计算。未知值保持 `null`，不得用模拟耗时或金额替代。

## 密钥合同

- 只从环境变量或 macOS Keychain 读取。
- 不能写入 Git、Markdown、任务提示、CSV、manifest、URL 或日志。
- 不在聊天中转述真实密钥。
- 聊天中曾经出现过的密钥应轮换。
- `.env*`、`secrets.*`、`pipeline_config.local.json` 始终忽略。

## GitHub 隐私边界

允许进入私有代码仓库：

- 通用代码、测试、Schema、脱敏配置示例；
- 架构、决策、运行手册和脱敏复盘；
- 不含真实人物和客户数据的小型假样例。

禁止进入普通 Git 历史：

- `auto-project/` 全量运行目录；
- 原始绿幕、MP4、音频、图片、DOCX、PDF 客户包；
- 浏览器 Cookie、验证码和登录会话；
- 原始对话导出；
- API Key、账户余额截图、私人文件；
- 未脱敏绝对路径和第三方个人资料。

大型基准产物如需远程保存，应使用独立私有对象存储、加密云盘或受控 Git LFS，不与源码仓库混放。
