# 桌面稳定版本

发布标签：story-agent-v2-stable-20260916。逻辑基线：8fdeb3eccf72897ba81cbc5964f4179ca9050e67。

当前用户的桌面目录为 StoryAgent，源码位于其中的“源码”，本机运行库和 RVM 官方锁定模型位于“本机运行环境”。本机 pipeline_config.local.json 使用本机绝对路径，已被 Git 忽略。需要移动整个目录时更新这两个本机配置路径；不编辑共享 pipeline_config.json。

## 新故事入口

在 Codex 中以本目录为工作区创建任务，读取 AGENTS.md 与 skills/story-full-auto/SKILL.md。新故事输出放在桌面的独立项目目录，显式采用 v2 合同。输入为确认文本、TXT/SRT、调色绿幕、权威旁白、最终 Word、整条成品音乐及故事要求。固定品牌参考和包装提示词随源码交付，不从旧故事或外置盘猜取。

稳定标签是用户明确选择的生产版本，不免除每个新故事的预览、QA 与独立审核。保留模型继承与现有功能边界。旧 v1/v2/v3 项目不自动重跑。

## 本机运行环境

本次部署使用本机 Codex bundled Python，FFmpeg/FFprobe 使用本机 Homebrew 安装；RVM 运行库和模型单独保存在桌面。桌面根目录的 python.command 为这些已核对路径提供命令入口，例如：

```sh
./python.command story_pipeline.py describe
```

该脚本只运行确定性命令，不是另一套生产调度器。无需连接外置硬盘；在线生成仍需要网络、Codex 工具和供应商凭据。凭据只通过系统安全存储/环境变量读取，不在 Git 或恢复包里。

GitHub 提供源码、规则、固定参考和测试，不包含本机运行库、模型、密钥和真实故事大媒体。换一台机器应安装 requirements.txt、FFmpeg，配置 RVM 运行库并获取校验过的官方模型；不能把 GitHub 源码误认为所有环境均已安装。

## 保留与恢复

桌面“版本与恢复”保存完整 Git bundle、清单、部署验证及旧候选交付。GitHub 的 main 指向这条当前主链，更新前 main 保留为 story-agent-before-desktop-stable-20260916；已有标签不删除。

需要回退时，在新的独立目录检出旧标签/提交，使用新项目或明确的兼容读取流程；不要用 git reset --hard 覆盖正在运行或含未提交修改的工作区。
