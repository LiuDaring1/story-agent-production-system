# DSH 迁移资料入口

本目录只负责把 Story Agent V3.5 安全交给 DeepSeek Harness（`dsh`）继续适配，不改变当前 Codex 生产链，也不包含任何真实故事媒体、项目运行目录、密钥或浏览器会话。

建议阅读顺序：

1. `DSH_MIGRATION_HANDOFF_V35_20260821.md`：完整架构、迁移边界、阶段计划与验收标准。
2. `DSH_FIRST_PROMPT.md`：首次在 DSH 工作区中提交的任务提示。
3. `DSH_AGENTS.md`：迁移包构建时覆盖到包根目录的 DSH 专用工程约束。
4. `dsh_settings.example.yaml`：模型路由示意，必须按实际安装版本核验后才能使用。
5. `pipeline_config.dsh-template.json`：迁移包中的脱敏、可移植基线配置。

构建迁移包：

```bash
python3 scripts/build_dsh_migration_package.py \
  --destination "/absolute/path/Story-Agent-V3.5-DSH-Migration-20260821"
```

校验已有迁移包：

```bash
python3 scripts/build_dsh_migration_package.py \
  --verify "/absolute/path/Story-Agent-V3.5-DSH-Migration-20260821"
```

构建器只读取已提交的 `HEAD`，默认拒绝 dirty source、拒绝覆盖已有目标目录，并在最终目录写入逐文件 SHA-256 manifest。它不会启动 Story Agent、Canary、DSH 或任何 provider。
