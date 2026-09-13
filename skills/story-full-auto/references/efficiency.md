# 精简与复用

环境资产计划在生成前输出消费者：shot、camera、derive以及明确连续性约束。`single_view_reuse_master` 仅适用于 `spatial_complexity: simple` 且恰好一个机位，母资产的 camera_contract 必须逐项匹配机位 origin、target、background zones、angle、shot size。机位资产仍保留独立语义ID/视线合同，path与SHA指向同一母图，不再生第二张同视角图。反打、多机位、不同裁剪和未知空间保持原路径。

审核请求格式：project_id、items（scope/path/dependencies/parameters）、rules（实际规则文件路径）、可选previous（带各scope独立approval的上次packet）、repairs。repair须含scope、defect_code、requirement_source、requirement_scope、evidence、delivery_impact、retry_strategy、root_cause。准备器在首次投喂前一次检查全部字段、绑定当前产物，按缺陷归组；输出不是通过回执，不替代批量校准或生成后的动作审核。无需按文件数量推断重复审核。

独立审核scope可在packet的item.approval保存review/bundle文件绑定及producer_context。复用需该bundle同时包含当前产物、依赖、规则以及生成的不可变scope_definition（包含参数），且独立审核达85分、无关键错误、hash当前。prepare-review会自动判定review_action；不要把preparation_reused当QA通过。参数变化、新故事、规则变化、依赖变化或显式修复均重新检查。固定主包装重新触发不等于新故事；先核对同项目合格资产再决定生成。

材料目录重跑先验输入、清单与成员哈希，未变不复制。输入改变请输出新版本目录，保留旧审核/恢复证据。可选ZIP可在已完成目录上追加，也可在中断后校验内容恢复。客户后加文件不成为Agent验收依赖。

计量来自现有账本追加事件：operation_id/产物/起止等待/类型/首次或复用或返工/理由和有效性。可得模型、推理、Token与provider请求ID沿用原始事实；未知保持null。本地CLI自动记录，不要求主Agent补长手工日志。`status.work_summary`只统计覆盖到的操作和供应商登记请求，明确不覆盖整个Codex任务；不由缺记录推断零调用。重置窗口、共享账号和实际模型未知时不能推算单故事额度归因。

字幕图内容/顺序/时间参数变更均改变指纹。编码仅对已枚举且不读外部文件的内联滤镜开放依赖快照复用；可能读取外部文件、随机/时钟表达式或未知滤镜仍不复用，不声称所有复杂媒体编码均可跳过。实际FFmpeg可执行文件SHA也参与指纹；动态库和硬件驱动不是本轮逐文件追踪范围。

## 稳定候选的准备与恢复

长任务前运行 `story_pipeline.py preflight --run-file RUN --request REQUEST`，请求提供 director、plan、compile_receipt。一次处理 Word/TITLE/MORAL 映射、计划和编译回执问题。`review-create` 使用 artifact_id、artifact、producer_context、requirements、evidence 创建规范待审包；字段为空的待审包不是审核通过，独立上下文填结论并绑定当前被审 SHA。

CLI 默认输出摘要，完整账本读取 `--run-file` 指向的文件或显式 `--full-json`。候选操作的摘要包含 `full_result.path`；目录成员较大时通过 `members_manifest` 指向状态目录内内容寻址文件，读取时校验 SHA，历史内联 members 仍可读。保留这些文件一起恢复，不把目录清单放进被绑定的客户目录。

一次封口内首次哈希完整读盘，仅在 dev/ino/size/mtime_ns/ctime_ns 未变时复用；封口前再次核对文件身份。替换文件、并发写入或目录成员变化使验证失败，不能把这次缓存跨运行保存。无锁的文件系统不能提供整个目录的原子快照；生产期间不要外部编辑正在审核的输入和产物。

恢复先运行 status 核对受管回执；仍持锁的进程继续受管，显式等待期限只表示延后。停止/掉盘后的闭合依赖输出锁释放、受管回执和当前输出哈希；失败临时文件不登记成品。编码复用以当前内容、参数、实际代码依赖及输出 SHA 为依据。只改变源码目录位置不失效；主账号改动不自动使未变化宝库号失效；未能枚举的外部滤镜依赖继续保守重跑。

计时分别看父操作耗时、叶子编码耗时和墙钟跨度，三者不能相加。完整请求指纹与提示词哈希分开；完整指纹包含实际参考输入、模型和参数。首次、有效返工、失败重试、必要并行和明确误重复分别登记，不从相同提示词推断误重复。请求、模型、Token 只报告已覆盖范围，未知仍为 null。
