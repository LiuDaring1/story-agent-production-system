# 版本基线

本目录记录已经真实跑过、可以复核和回退的系统版本。基线不是“完美版本”，而是对某一时点的代码、生产样本、审核结果、成本和已知缺陷作不可含糊的说明。

## 当前基线

- [`V3_BASELINE_2026-08-12.md`](V3_BASELINE_2026-08-12.md)：Story Agent V3 正式基线。
- [`V3_FEEDBACK_REGISTER_XIAOBIHU.md`](V3_FEEDBACK_REGISTER_XIAOBIHU.md)：《小壁虎借尾巴》用户终验问题台账。
- [`V3_FREEZE_MANIFEST.json`](V3_FREEZE_MANIFEST.json)：代码与本地生产证据的哈希清单。
- [`../postmortems/2026-08-lizard-tail.md`](../postmortems/2026-08-lizard-tail.md)：本轮运行时间、Token、调用和故障复盘。
- [`../roadmaps/V3.5_QUALITY_AND_MODULARIZATION.md`](../roadmaps/V3.5_QUALITY_AND_MODULARIZATION.md)：下一版实施范围。

生产媒体、用户原片、Feedback 原文和截图不提交 Git。基线通过文件名、字节数和 SHA-256 绑定这些本地证据；任何人可以在获准访问项目目录后复核，但不能从仓库反推出私有素材。
