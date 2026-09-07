"""Small on-disk delivery fixtures; media probes are mocked by ledger tests."""

import hashlib
from pathlib import Path


def binding(path: Path) -> dict:
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def make_delivery(project: Path) -> dict:
    release = project / "04_发布视频"
    files = [release / f"{name}发布视频.mp4" for name in ("主账号", "宝库号")]
    for account in ("main", "library"):
        publish = release / "publish_package" / account
        files.append(publish / "copy.md")
        files.extend(publish / "covers" / f"cover_{ratio}.png" for ratio in ("3x4", "4x3", "16x9"))
    base = ["故事文稿：测试.docx", "故事配乐：测试.mp3", "朗读标注：测试.docx", "示范表演：测试.mp4", "背景图片：测试.png"]
    advanced = base + [
        "背景视频：测试（含字幕）.mp4", "背景视频：测试（无字幕）.mp4",
        "故事PPT：测试（含字幕）.pptx", "故事PPT：测试（无字幕）.pptx", "A镜无人物背景视频：测试.mp4",
    ]
    for variant, names in (("基础版", base), ("进阶版", advanced)):
        files.extend(project / "06_资料包" / f"故事锦囊（{variant}）" / name for name in names)
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(path.name.encode())
    return {
        "schema_version": "story-final-delivery-checklist/v1",
        "status": "complete_pending_independent_final_review", "missing": [],
        "matrix": {"release_videos": 2, "account_copy_files": 2, "covers": 6,
                   "basic_customer_files": 5, "advanced_customer_files": 10, "static_ppt_variants": 2},
        "artifacts": [binding(path) for path in files],
    }


def release_qa(videos: dict, narration: Path, music: Path, duration: float = 2) -> dict:
    return {
        "schema_version": "story-release-machine-qa/v3", "passed": True,
        "critical_errors": [], "issues": [], "artifacts": videos,
        "audio_contract": {"required_audio_role": "narration_plus_music",
                           "narration": binding(narration), "music_bed": binding(music)},
        "results": [{
            "path": item["path"], "duration_sec": duration, "issues": [],
            "audio_role": "narration_plus_music", "audio_role_fit": {
                "passed": True, "rms": 0.1, "voice_gain": 1, "music_gain": 0.2,
                "voice_component_rms_ratio": 0.9, "music_component_rms_ratio": 0.1,
                "residual_energy_ratio": 0.001,
            },
        } for item in videos.values()],
    }
