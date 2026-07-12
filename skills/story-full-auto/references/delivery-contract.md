# Delivery contract

## Required user-facing outputs

- `主账号发布视频.mp4`
- `宝库号发布视频.mp4`
- Main-account and library-account title, body, and topic suggestions
- Six approved covers: 3:4, 4:3, and 16:9 for each account
- Basic package: consumer manuscript, reading annotation, music, demonstration video, titled background image
- Advanced package: consumer manuscript, reading annotation, music, demonstration video, PPT with/without subtitles, background video with/without subtitles

## Required internal evidence

- Original source fingerprint and preserved project copy
- Raw transcript, edit decisions, kept intervals, clean script, and subtitles
- Stage attempts, timestamps, artifact paths, reviewer score, and critical-error list
- Cost ledger with soft limit ¥50 and hard limit ¥100
- `99_项目状态/成本报告.md`, `QA汇总.md`, and `异常说明.md`; none may appear inside either customer package
- Final QA summary, exception report, and total delivery checklist

## Completion gate

Claim completion only when all required outputs exist and every intelligent/visual stage has a current passing review. A review is current only when its `artifact_sha256` matches the artifact on disk. External login, CAPTCHA, payment, platform-risk, disk-space, and API-auth failures are blockers, not permission to fabricate success.
