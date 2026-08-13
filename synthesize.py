from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMBA_NUM_THREADS", "1")
os.environ.setdefault("NUMBA_THREADING_LAYER", "workqueue")

from story_video_synthesizer import SynthesisConfig, synthesize_story


def main() -> None:
    parser = argparse.ArgumentParser(description="儿童故事视频合成器")
    parser.add_argument("--video-dir", required=True, type=Path, help="按顺序命名的视频片段文件夹")
    parser.add_argument("--script", required=True, type=Path, help="逐行台词文本")
    parser.add_argument("--subtitle-script", default=None, type=Path, help="字幕专用原文脚本；不传则沿用 --script")
    parser.add_argument("--narration", required=True, type=Path, help="完整旁白录音")
    parser.add_argument("--music", required=True, type=Path, help="背景音乐")
    parser.add_argument("--output-dir", required=True, type=Path, help="输出文件夹")
    parser.add_argument("--project-dir", type=Path, help="required_v1 项目根目录，用于重新验证合同锁")
    parser.add_argument("--artifact-semantic-plan", type=Path, help="已锁定合同确定性编译的逐产物语义呈现计划")
    parser.add_argument("--whisper-model", default="base", help="Whisper 模型名，例如 tiny/base/small/medium")
    parser.add_argument("--language", default="zh", help="旁白语言，中文用 zh；自动识别可留空字符串")
    parser.add_argument("--alignment-mode", choices=["whisper", "even"], default="whisper", help="对齐模式")
    parser.add_argument("--width", default=1920, type=int, help="输出宽度")
    parser.add_argument("--height", default=1080, type=int, help="输出高度")
    parser.add_argument("--fps", default=30, type=int, help="输出帧率")
    parser.add_argument("--music-volume", default=0.22, type=float, help="背景音乐音量")
    parser.add_argument("--narration-volume", default=1.0, type=float, help="旁白音量")
    parser.add_argument("--x264-preset", default="veryfast", help="视频编码速度，例如 ultrafast/veryfast/fast/medium")
    parser.add_argument("--x264-crf", default=20, type=int, help="视频质量，数值越低质量越高、文件越大")
    parser.add_argument("--subtitle-style", choices=["clean", "box"], default="clean", help="字幕样式：clean 为白字描边阴影，box 为旧版黑底")
    parser.add_argument("--sales-skip-head-lines", default=-1, type=int, help="销售版字幕跳过开头台词行数；-1 按主持开场语义自动识别")
    parser.add_argument("--sales-skip-tail-lines", default=-1, type=int, help="销售版字幕跳过结尾台词行数；-1 在寓意/主持收尾前自动停止")
    parser.add_argument("--keep-workdir", action="store_true", help="保留中间文件")
    parser.add_argument("--whisper-model-dir", default=None, type=Path, help="Whisper 模型目录")
    args = parser.parse_args()

    language = args.language.strip() or None
    start_time = time.perf_counter()
    result = synthesize_story(
        SynthesisConfig(
            video_dir=args.video_dir,
            script_path=args.script,
            subtitle_script_path=args.subtitle_script,
            narration_path=args.narration,
            music_path=args.music,
            output_dir=args.output_dir,
            whisper_model=args.whisper_model,
            language=language,
            alignment_mode=args.alignment_mode,
            width=args.width,
            height=args.height,
            fps=args.fps,
            music_volume=args.music_volume,
            narration_volume=args.narration_volume,
            x264_preset=args.x264_preset,
            x264_crf=args.x264_crf,
            subtitle_style=args.subtitle_style,
            sales_skip_head_lines=args.sales_skip_head_lines,
            sales_skip_tail_lines=args.sales_skip_tail_lines,
            keep_workdir=args.keep_workdir,
            whisper_model_dir=args.whisper_model_dir,
            progress_callback=lambda message: print(message, flush=True),
            project_dir=args.project_dir,
            artifact_semantic_plan_path=args.artifact_semantic_plan,
        )
    )

    print("合成完成：")
    print(f"- 无字幕 + 背景音乐：{result.no_subs_bgm}")
    print(f"- 有字幕 + 背景音乐：{result.subs_bgm}")
    print(f"- 销售版有字幕 + 背景音乐：{result.sales_subs_bgm}")
    print(f"- 有字幕 + 旁白 + 背景音乐：{result.demo_voice_bgm}")
    print(f"- 时间轴：{result.timings_json}")
    print(f"- 字幕：{result.subtitles_srt}")
    print(f"- 销售版字幕：{result.sales_subtitles_srt}")
    print(f"- 总时长：{result.total_duration:.2f} 秒")
    print(f"- 制作耗时：{time.perf_counter() - start_time:.1f} 秒")


if __name__ == "__main__":
    main()
