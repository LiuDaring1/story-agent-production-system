# v2 候选版操作与回退

本版是候选，未作为默认生产版本。真实新故事通过现有 QA、独立审核，并由用户结合成片体验接受后才能晋级；旧基线继续保留。本次不调用任何付费生成服务。

## 新旧接口

| 工作 | 旧链 | v2 候选 |
|---|---|---|
| 输入 | 部分目录发现/派生 | 十个角色显式路径与哈希 |
| 音乐 | 生成与音乐专门 QA | 用户整条成品；最终视频音轨角色 QA |
| 文稿 | 生成/重写 | 最终 Word 原字节复制 |
| 资料包 | 媒体、标注、PPT 与复制混合 | media 制作，pack 仅复制 |
| PPT | 两份 PPTX | 同源图片、旁白、原音乐和清单 ZIP |
| 包装 | 固定默认参考 | 确认参考与提示词哈希；仅替换批准字段 |
| 封面/文案/标注 | 执行与完成门禁 | 退出 Agent 主链 |
| 恢复 | 状态及产物记录 | 当前哈希＋输出锁＋子进程锁继承＋任务/请求指纹 |

六包仅是事实分组，不是固定阶段：director_plan、r2v_visuals、presenter_keying、media_render、product_assets、delivery。历史 `story-run-v1` 没有 `production_contract` 时继续旧读取校验；没有自动迁移或旧项目补证据操作。

## 启动新项目

在候选独立工作目录，用新项目目录执行。建议使用已安装的 Codex bundled Python，避免系统 Python 缺少 numpy 等依赖。

```sh
python3 story_pipeline.py init --production-contract v2 \
  --run-file /new/project/99_项目状态/story_run.json \
  --project-dir /new/project \
  --text /inputs/confirmed.txt --subtitle-txt /inputs/subtitles.txt \
  --subtitle-srt /inputs/subtitles.srt --video /inputs/greenscreen.mp4 \
  --audio /inputs/authority.wav --final-word /inputs/final.docx \
  --finished-music /inputs/finished.wav \
  --story-requirements /inputs/requirements.json \
  --packaging-reference /inputs/reference.png --packaging-prompt /inputs/confirmed-prompt.txt
```

`--production-contract` 保持 v1 默认，仅显式 v2 进入候选。不得将缺失输入用历史目录推测补齐。

确定性操作均使用 `story_pipeline.py OPERATION --run-file RUN --request REQUEST.json`。请求参数按以下函数签名填写，路径显式给出，输入/产物描述均含 path、sha256、bytes：

- `media-preview`：`story_candidate_cli.render_media` 的 inputs（preset、background_image、story_frame、logo、两种背景视频、body_srt、timeline_receipt）、requirements_projection、producer_context；输出五帧及当前几何预览清单。
- `media-approve`：preview、review、output。review 明确 independent_context=true、reviewer_context（与制作上下文不同）、approved=true、score>=85、critical_errors=[]、artifact_sha256。
- `media`：与预览相同的输入，再给 approved_demo 绑定。输入/几何变化需重新预览审核；无 PPT 参数仍执行时间轴、字幕、Logo、RVM、几何及客户媒体 QA。
- `materials`：director、plan、compile_receipt、output（ZIP）、receipt。复用 `shot_storyboard_pipeline.compile_consumers` 的同源页计划；TITLE/MORAL 提供 word_text，重复句提供 word_start。编译器保留映射字段。输出失败不会修改视频。
- `packaging`：fields、output、receipt。fields 固定 story_name、story_type、age_range、duration_text、theme_style；模板其余文字保持原样。该回执证明提示词编译，不能冒充生图完成。后续真正生图需 `story-confirmed-panels/v2` 回执及严格独立审核，所有实际使用的账号面板均绑定审核。宝库号继续独立包装与动态 Logo/片尾规则。
- `pack`：output_root、receipt、sources、story_name。sources 固定为 `ADVANCED_ROLES` 八项；两个包共享原 Word/音乐，PPT 素材仅进阶版。更新不移动目录、不编码；用户修改同名托管文件时报冲突。
- `checklist`：managed_package_receipt、main_release_video、library_release_video、output。只列 Agent 管理角色，不扫描用户新增文件。

背景视频仍用 `render_customer_backgrounds.py`；双账号仍用 `release_video.py` 的显式参数、当前编译配置和已审 Demo 几何。不要用历史 `story_workflow` 的自动发现包装路径来操作 v2 项目。`--main-package-spec` 可接新提示词回执，`--main-package-receipt` 接实际面板生成审核回执。`--demo-render-manifest` 接 media-approve 的输出。

最终按 `story_production_v2` 要求登记证据。qa_product_report 可登记同一 managed_package_receipt；这是原字节/成员校验，媒体质量仍由 customer_media_receipt、qa_release_report、发布几何及最终独立审核承担。清单和全部成员、发布 QA 被最终审核 bundle 覆盖后才能 finalize。

## 编码与恢复

通过共同 media.run_command 执行的正式 FFmpeg 视频重编码共享本机文件锁池，初始容量 1。`pipeline_config.json.resource_limits.formal_encode_concurrency` 或 STORY_ENCODE_CONCURRENCY 可设置容量；变更须等池空闲，否则有界等待后报告。短测试可以隔离 STORY_ENCODE_STATE_DIR，实际生产使用同一个池。该机制不调度供应商或导演任务。

每个输出的独立锁、输入/命令指纹、当前输出哈希、进程 ID、任务 ID、心跳写入本机临时状态目录。子 FFmpeg 继承锁，父任务中断后，子进程仍运行时不可重入。失败或中断输出保留为临时残片，不登记有效产物。完成后按当前输入、命令及输出哈希复用。

取消使用 encode-control，请求包含 output、action=cancel、expected_fingerprint；任务必须匹配当前账本。它写受管取消标记，不按进程名或外部 PID 杀进程。确认所属编码停止后以 action=resume 清除对应取消标记，再重跑原请求。恢复不会撤销供应商已完成请求，也不会恢复模型内部上下文。

## 两种回退

**回退新故事环境：**使用 `codex/pre-decouple-*` 所映射的独立恢复目录及工程快照；先核对工程清单和入口只读检查，再以另一个新项目目录启动。不要 reset 当前目录或把旧代码写入新账本。

**修复新版项目：**先保留账本、输入/当前有效产物哈希、请求/编码现场和审核证据；定位受影响消费者，仅修这一部分，重新校验变化及实际下游。不能直接用旧程序操作新版账本。

人工恢复交接至少保留：版本/合同、输入路径与哈希、当前有效产物及证据、在途请求身份、未完成项、已知问题、下一条明确命令。密钥只记环境变量名或安全存储引用。
