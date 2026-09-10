# 精简与复用

环境资产计划在生成前输出消费者：shot、camera、derive以及明确连续性约束。`single_view_reuse_master` 仅适用于 `spatial_complexity: simple` 且恰好一个机位，母资产的 camera_contract 必须逐项匹配机位 origin、target、background zones、angle、shot size。机位资产仍保留独立语义ID/视线合同，path与SHA指向同一母图，不再生第二张同视角图。反打、多机位、不同裁剪和未知空间保持原路径。

审核请求格式：project_id、items（scope/path/dependencies/parameters）、rules（实际规则文件路径）、可选previous（带各scope独立approval的上次packet）、repairs。repair须含scope、defect_code、requirement_source、requirement_scope、evidence、delivery_impact、retry_strategy、root_cause。准备器在首次投喂前一次检查全部字段、绑定当前产物，按缺陷归组；输出不是通过回执，不替代批量校准或生成后的动作审核。无需按文件数量推断重复审核。

独立审核scope可在packet的item.approval保存review/bundle文件绑定及producer_context。复用需该bundle同时包含当前产物、依赖、规则以及生成的不可变scope_definition（包含参数），且独立审核达85分、无关键错误、hash当前。prepare-review会自动判定review_action；不要把preparation_reused当QA通过。参数变化、新故事、规则变化、依赖变化或显式修复均重新检查。固定主包装重新触发不等于新故事；先核对同项目合格资产再决定生成。

材料目录重跑先验输入、清单与成员哈希，未变不复制。输入改变请输出新版本目录，保留旧审核/恢复证据。可选ZIP可在已完成目录上追加，也可在中断后校验内容恢复。客户后加文件不成为Agent验收依赖。

计量来自现有账本追加事件：operation_id/产物/起止等待/类型/首次或复用或返工/理由和有效性。可得模型、推理、Token与provider请求ID沿用原始事实；未知保持null。本地CLI自动记录，不要求主Agent补长手工日志。`status.work_summary`只统计覆盖到的操作和供应商登记请求，明确不覆盖整个Codex任务；不由缺记录推断零调用。重置窗口、共享账号和实际模型未知时不能推算单故事额度归因。

字幕编码只对严格闭合的数值overlay+between表达式开放依赖快照复用，字幕图内容/顺序/时间参数变更均改变指纹。其他可能读取外部文件的filter图仍不复用，不声称所有复杂媒体编码均可跳过。
