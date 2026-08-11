# 运行与恢复手册

## 环境

推荐 Python 环境必须能导入 `python-docx`、Pillow 和项目依赖。真实生产前确认 FFmpeg、ffprobe、LibreOffice、Codex CLI、Ego Browser 和密钥来源可用。

本机绝对素材路径属于部署配置，不是通用架构。其他机器应使用 `pipeline_config.local.json` 或环境配置覆盖，禁止将新机器路径写死进通用代码。

## 提交与启动

```bash
python3 story_agent.py submit \
  --video "/path/to/source.mp4" \
  --story-name "故事名"

python3 story_agent.py start --job JOB_ID
python3 story_agent.py status --job JOB_ID
python3 story_agent.py report --job JOB_ID
```

默认 `start` 使用 DAG 和最多三个并行 stage worker。不要重复启动同一 job 的多个 supervisor。

## 取消与恢复

```bash
python3 story_agent.py cancel --job JOB_ID
python3 story_agent.py resume --job JOB_ID
python3 story_agent.py start --job JOB_ID
```

恢复前检查：

1. 旧 supervisor 是否仍存活；
2. manifest 的 blocked reason；
3. 当前 bundle 和 review 是否哈希匹配；
4. jobs 中目标视频是否逐一存在；
5. 是否会因旧 review 失效触发新的付费重做；
6. 成本账本是否覆盖既有调用。

不要通过手改 `passed`、替换旧 `artifact_sha256` 或删除失败记录来恢复。

## 常见阻断

### CAPTCHA / 登录

保留任务状态，请用户完成人机验证后恢复。不要尝试绕过验证码。

### Codex 周额度或网络 502

记录等待原因和最早恢复时间。不要不断重启相同大任务；优先保存阶段上下文和已完成镜头。

### 视频审核失败

隔离失败视频，保留旧 task_id 和备份。只重做失败镜头，并在提示词变化后重新做对应增量审核。

### 审核 bundle 不 current

用标准 bundle 生成函数重建，不要只改 review JSON 哈希。重建后必须重新独立审核。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 story_agent.py --help
python3 story_agent.py submit --help
python3 story_agent.py start --help
```

正式交付还应运行项目 Doctor、机器 QA、标准独立审核和人工终审。

## 运行账本的未来要求

每个执行实例都应记录：stage、attempt、parent、模型、档位、开始/结束、等待、Token、工具调用、外部费用、产物哈希和失效原因。没有这些字段，不应声称已精确计算效率。
