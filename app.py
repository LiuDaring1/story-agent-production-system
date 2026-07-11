from __future__ import annotations

import socket
from pathlib import Path

import gradio as gr

from story_video_synthesizer import SynthesisConfig, synthesize_story


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run_synthesis(
    video_dir: str,
    script_file: str,
    narration_file: str,
    music_file: str,
    output_dir: str,
    whisper_model: str,
    language: str,
    alignment_mode_label: str,
    width: int,
    height: int,
    fps: int,
    music_volume: float,
    narration_volume: float,
    keep_workdir: bool,
) -> tuple[str, str | None, str | None, str | None]:
    try:
        alignment_mode = "even" if "平均" in alignment_mode_label else "whisper"
        result = synthesize_story(
            SynthesisConfig(
                video_dir=Path(video_dir).expanduser(),
                script_path=Path(script_file).expanduser(),
                narration_path=Path(narration_file).expanduser(),
                music_path=Path(music_file).expanduser(),
                output_dir=Path(output_dir).expanduser(),
                whisper_model=whisper_model,
                language=language.strip() or None,
                alignment_mode=alignment_mode,
                width=int(width),
                height=int(height),
                fps=int(fps),
                music_volume=float(music_volume),
                narration_volume=float(narration_volume),
                keep_workdir=keep_workdir,
            )
        )
    except Exception as exc:
        return f"合成失败：{exc}", None, None, None

    message = "\n".join(
        [
            "合成完成。",
            f"总时长：{result.total_duration:.2f} 秒",
            f"时间轴：{result.timings_json}",
            f"字幕：{result.subtitles_srt}",
        ]
    )
    return (
        message,
        str(result.no_subs_bgm),
        str(result.subs_bgm),
        str(result.demo_voice_bgm),
    )


with gr.Blocks(title="儿童故事视频合成器") as demo:
    gr.Markdown("# 儿童故事视频合成器")
    gr.Markdown("把现成视频片段、逐行台词、旁白录音和背景音乐合成为完整故事视频。")

    with gr.Row():
        with gr.Column(scale=1):
            video_dir = gr.Textbox(label="视频片段文件夹", placeholder="/path/to/01_lycs.mp4 所在文件夹")
            script_file = gr.File(label="逐行台词文本", file_types=[".txt"], type="filepath")
            narration_file = gr.File(label="旁白音频", file_types=["audio"], type="filepath")
            music_file = gr.File(label="背景音乐", file_types=["audio"], type="filepath")
            output_dir = gr.Textbox(label="输出文件夹", placeholder="/path/to/output")

            with gr.Accordion("高级设置", open=False):
                whisper_model = gr.Dropdown(
                    label="Whisper 模型",
                    choices=["tiny", "base", "small", "medium", "large"],
                    value="base",
                )
                language = gr.Textbox(label="旁白语言", value="zh")
                alignment_mode = gr.Radio(
                    label="对齐模式",
                    choices=["Whisper 语音识别对齐", "按台词长度平均分配"],
                    value="Whisper 语音识别对齐",
                )
                with gr.Row():
                    width = gr.Number(label="宽度", value=1920, precision=0)
                    height = gr.Number(label="高度", value=1080, precision=0)
                    fps = gr.Number(label="帧率", value=30, precision=0)
                music_volume = gr.Slider(label="背景音乐音量", minimum=0, maximum=1, value=0.22, step=0.01)
                narration_volume = gr.Slider(label="旁白音量", minimum=0, maximum=2, value=1.0, step=0.05)
                keep_workdir = gr.Checkbox(label="保留中间文件", value=False)

            submit = gr.Button("开始合成", variant="primary")

        with gr.Column(scale=1):
            status = gr.Textbox(label="状态", lines=6)
            no_subs_video = gr.Video(label="无字幕 + 背景音乐")
            subs_video = gr.Video(label="有字幕 + 背景音乐")
            demo_video = gr.Video(label="有字幕 + 旁白 + 背景音乐")

    submit.click(
        fn=run_synthesis,
        inputs=[
            video_dir,
            script_file,
            narration_file,
            music_file,
            output_dir,
            whisper_model,
            language,
            alignment_mode,
            width,
            height,
            fps,
            music_volume,
            narration_volume,
            keep_workdir,
        ],
        outputs=[status, no_subs_video, subs_video, demo_video],
    )


if __name__ == "__main__":
    port = find_free_port()
    print(f"本地界面：http://127.0.0.1:{port}", flush=True)
    demo.launch(server_name="127.0.0.1", server_port=port)
