# 前台工作流

六个粗粒度包：director_plan、r2v_visuals、presenter_keying、media_render、product_assets、delivery；只登记事实和依赖，不是固定阶段调度器。

## 工作交接与审核依据

每包先读本次实际规则和机器自检，生成 `story-applicable-requirements/v1` 小投影：当前输入哈希、规则源版本、账号/镜头/产物范围、原要求与来源、执行参数、验收证据。生产和独立审核共用此文件。用户要求覆盖默认时保留来源与范围，不能再写回默认。

导演计划独立审核一次；资产和故事板整组分别审核；RVM 候选锁定独立审核；R2V 下载做逐镜机器检查，连续预览与问题镜头整组审核；最后合并交付审核。只对真实变化及显式依赖扩大检查，不把审美偏好当硬门禁，不默认 Max。

确定性素材导出、复制、哈希和 QA 直接调用程序，不为形式分工增加模型请求。导演的既有编译产物同时服务 R2V 和素材导出，TITLE/MORAL 使用封存图。音乐为成品输入，不建立音乐工作包。

## 恢复

使用 `story_pipeline.py status` 检查当前输入及产物绑定。正在运行的编码持有输出锁和共享槽，不能因旧账本 status 或 PID 看起来异常就重启。取消采用对应任务指纹的取消请求，不按进程名批量杀进程。完成后仅当命令/输入/输出 SHA 相同才复用；残片不能交付。

每次外部请求仍记录 provider/request_id/model、请求哈希、起止/等待/重试/Token/结果/错误类型。未知 Token 为 null；无法取得的实际模型和推理设置标为未知。保留心跳、磁盘检查、幂等及可取消的资源排队。并发 1/2 短夹具仅证明本地行为，不证明全故事效率。

模型对照和真实故事试用须独立记录输入哈希、模型实际设置（未知可空）、质量结果和实际耗时；不推断额度收益。本轮不做付费实验。

正式确定性入口：`timeline` 绑定确认SRT；`assemble` 消费正式区间；`backgrounds` 导出客户背景；`release` 双账号发布；`release-qa` 通用媒体机器检查。均由 story_pipeline.py 转发现有模块，不另建调度系统。目录素材与受管打包仍用 materials/pack/checklist；最终 finalize 继续核验完整独立审核。

## 主账号 ABC 正式准备

先运行 `story_pipeline.py release-windows --run-file RUN --output PROJECT/99_项目状态/release_windows_vN.json`。它从当前权威时长和字幕边界生成成熟自动窗口及抽样；输入和规则内容未变可复用，变化时用新版本计划。某些边界时长无法同时满足成熟自动规则和 A/B/C 覆盖时显式阻断，由当前任务提供合法且经过合并审核的计划，不静默退化全 A。

`release` 默认 `--b-windows auto --c-windows auto`，显式传 `--release-windows-plan PLAN`。预览根据实际计划抽取每段中点及切换两侧，并运行真实时间窗滤镜。计划SHA并入已有几何独立审核，预览不必另开窗口审核；正式使用当前 `--approved-preview-geometry` 和 `--approved-preview-review`。兼容已有独立窗口回执，不要求每个窗口单独审核。

正式主账号生成 `main_abc_coverage.json`；`release-qa`核验当前计划、视频和模板哈希并重新解码。expected_mode来自计划；observed_mode来自A/B框体实际几何和C镜当前人物源/已审Demo几何像素，空白不当成C。机器检查仍不替代合并独立视觉审核：审核者打开生成的ABC及边界帧，判断人物/构图、字幕、Logo及首尾。宝库号不套主账号切镜规则。
