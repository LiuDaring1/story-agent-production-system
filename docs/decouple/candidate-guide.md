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

## 修复候选：正式入口任务身份与取消

v2 每个正式入口都显式传 `--run-file /new/project/99_项目状态/story_run.json`。`story_pipeline.py media-preview/media`、`release_video.py`、`render_customer_backgrounds.py` 和正式 `assemble_r2v_story.py` 从账本验证 run_id 与输出项目归属，再绑定本次进程的编码身份；无需设置 STORY_TASK_ID。v2 缺少显式账本时阻断；旧 v1 调用保留原规则。所有输出/工作目录须位于该账本 project_dir 内，不能把工作目录设为项目外的公共目录。更换输入不会授权接管另一个任务拥有的编码输出。

直接渲染示意（其余参数沿用本指南及各入口 --help）：

```sh
python3 render_customer_backgrounds.py --run-file /new/project/99_项目状态/story_run.json ...
python3 release_video.py --run-file /new/project/99_项目状态/story_run.json ...
python3 assemble_r2v_story.py --run-file /new/project/99_项目状态/story_run.json ...
```

任务内的视频编码和 copy mux 使用同一受管身份。编码回执在 `STORY_ENCODE_STATE_DIR` 指定目录，未指定时为 Python `tempfile.gettempdir()` 下的 `story-encodes-<uid>`。文件名是**实际受管输出绝对路径字符串的 SHA-256**加 `.json`。背景制作可能正在编码 `work-dir/customer_subtitled_silent.mp4`，应控制这条实际运行回执，不能用尚未编码的最终目标代替。以下只读取本机小型编码池，筛选所属账本的运行项：

```sh
python3 - /new/project/99_项目状态/story_run.json <<'PY'
import json, sys
from pathlib import Path
from story_encode import state_directory
from story_run import load_run
run = load_run(Path(sys.argv[1]))
for path in state_directory().glob('*.json'):
    record = json.loads(path.read_text())
    if record.get('task') == run['run_id'] and record.get('status') == 'running':
        print(json.dumps({'output': record['role'], 'action': 'cancel',
                          'expected_fingerprint': record['fingerprint']}, ensure_ascii=False))
PY
```

将所需一条 JSON 保存到项目状态目录的 `cancel.json`，执行：

```sh
python3 story_pipeline.py encode-control --run-file /new/project/99_项目状态/story_run.json --request /new/project/99_项目状态/cancel.json
```

确认该回执进入 cancelled/failed 且锁已释放后，把同一 JSON 的 action 改为 resume，再执行相同控制命令，之后重跑原始渲染命令。若锁仍由活进程持有，resume/重复编码会拒绝。取消会停止本次命令，不会继续后续渲染；解码校验阶段也响应取消，发布有效产物前再次核对。输入/命令指纹或 run_id 不符则拒绝；残片不登记、不复用。回退不会撤销已完成供应商请求。组装器重新进入时可以复用稳定片段，但临时合并步骤可能重做，不承诺从中断字节继续编码。

## 成品音乐与托管更名

media-preview/media 不比较音乐与旁白的时长差来决定准入。音乐没有新增完整性、长度或审美审核；实际解码错误和最终音轨角色错误照常报告，源音乐不替换、不编辑，混音仍使用既有算法。

WAV→MP3 或其他角色目标路径改变时，pack 先把旧路径、角色、哈希和退役位置写进回执。旧文件只有匹配旧回执哈希才移动至 `output_root` 的同级 `.story-managed-retired/<包路径哈希>/<唯一编号>/`，不会留在客户目录。回执保留 retired 历史，中断后重跑同一请求即可继续。用户修改的旧文件、新目标冲突或备份冲突都会明确报错，保留现场；不要删除回执来绕过冲突。重新打包没有编码调用，用户新增文件原位保留。
