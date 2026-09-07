# 独立历史源码归档

旧 Agent、固定阶段 Runtime、Supervisor、Dashboard、Recovery、工作台、动态 PPT 实验、根目录别名和专用测试已从日常源码树移出。归档来自清理时的工作区，包括未提交版本；不依赖 HEAD 快照。入库前已将个人绝对路径替换为通用占位符，原始字节副本保留在仓库外交接目录的 private-evidence 中。`privacy-redactions.json` 记录变更成员的原始与脱敏 SHA-256；源码清单校验的是当前脱敏副本，不宣称与原文逐字相同。

运行 `python3 historical_archive/verify.py` 只检查归档成员及 SHA-256，不解压、不执行旧代码，也不读取或改写故事项目。`legacy-source-manifest.json` 与 `extra-source-manifest.json` 是逐文件清单；`retired-files.json` 列出主要移出文件；`split-test-inventory.json` 列出混合测试中移入历史的旧 Agent 检查。混合测试的完整原版本也在主归档中。

如需历史取证，在仓库外的临时目录读取归档。历史源码不接入当前生产，不自动迁移项目，不运行归档中的付费生成或恢复命令。现行主题资产的 `story-theme-assets-lightweight/v3` 等合同不受此次清理影响。

`story_style.py` 与 `story_text.py` 承接现行消费者所需的纯样式解析、只读文本/DOCX 解码。仍有真实消费者的合同、语义计划、模块端口和确定性媒体工具保留在当前树；不因命名中含有旧版本号而删除。合同兼容能力不再导入旧 Agent，当前 runner 必须有原生账本及适用要求，不能以缺字段为由回落旧生产。
