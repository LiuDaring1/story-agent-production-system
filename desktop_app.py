from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMBA_NUM_THREADS", "1")
os.environ.setdefault("NUMBA_THREADING_LAYER", "workqueue")
if getattr(sys, "frozen", False):
    resource_dir = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    os.environ["PATH"] = f"{resource_dir}{os.pathsep}{os.environ.get('PATH', '')}"

import queue
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from tkinter import (
    BOTH,
    DISABLED,
    END,
    LEFT,
    NORMAL,
    RIGHT,
    W,
    X,
    Button,
    Entry,
    Frame,
    Label,
    LabelFrame,
    StringVar,
    Tk,
    Text,
    filedialog,
    messagebox,
    ttk,
)

from PIL import Image, ImageTk

from story_video_synthesizer.media import VIDEO_EXTENSIONS, probe_duration, sorted_video_files


@dataclass
class VideoItem:
    path: Path
    duration: float | None = None
    thumbnail_path: Path | None = None


class StoryVideoApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("儿童故事视频合成器")
        self.root.geometry("1360x860")
        self.root.minsize(1120, 720)

        self.video_items: list[VideoItem] = []
        self.thumbnail_refs: list[ImageTk.PhotoImage] = []
        self.temp_dir = Path(tempfile.mkdtemp(prefix="story_video_app_"))
        self.bundled_model_dir = self._app_dir() / "models" / "whisper"
        self.message_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.is_running = False

        self.narration_path = StringVar()
        self.music_path = StringVar()
        self.output_dir = StringVar(value=str(Path.home() / "Desktop"))
        self.story_name = StringVar(value="完整故事")
        self.whisper_model = StringVar(value="base")
        self.language = StringVar(value="zh")
        self.alignment_mode = StringVar(value="Whisper 语音识别对齐")
        self.music_volume = StringVar(value="0.22")
        self.narration_volume = StringVar(value="1.0")

        self._build_ui()
        self._poll_messages()

    def _build_ui(self) -> None:
        main = Frame(self.root)
        main.pack(fill=BOTH, expand=True, padx=12, pady=10)

        left = LabelFrame(main, text="视频片段")
        left.pack(side=LEFT, fill=BOTH, expand=False, ipadx=4, ipady=4)
        left.configure(width=430)
        left.pack_propagate(False)

        toolbar = Frame(left)
        toolbar.pack(fill=X, padx=8, pady=(6, 4))
        Button(toolbar, text="+ 添加文件夹", command=self.add_folder).pack(side=LEFT)
        Button(toolbar, text="+ 添加视频", command=self.add_videos).pack(side=LEFT, padx=(6, 0))
        self.video_count_label = Label(toolbar, text="0 个视频")
        self.video_count_label.pack(side=RIGHT)

        self.video_canvas = ttk.Treeview(left, columns=("index", "file", "duration"), show="headings", height=18)
        self.video_canvas.heading("index", text="序号")
        self.video_canvas.heading("file", text="文件名")
        self.video_canvas.heading("duration", text="时长")
        self.video_canvas.column("index", width=54, anchor="center")
        self.video_canvas.column("file", width=250)
        self.video_canvas.column("duration", width=72, anchor="center")
        self.video_canvas.pack(fill=BOTH, expand=True, padx=8, pady=4)
        self.video_canvas.bind("<<TreeviewSelect>>", self.on_video_select)

        move_bar = Frame(left)
        move_bar.pack(fill=X, padx=8, pady=(4, 8))
        Button(move_bar, text="上移", command=lambda: self.move_selected(-1)).pack(side=LEFT, fill=X, expand=True)
        Button(move_bar, text="下移", command=lambda: self.move_selected(1)).pack(side=LEFT, fill=X, expand=True, padx=6)
        Button(move_bar, text="删除", command=self.delete_selected).pack(side=LEFT, fill=X, expand=True)
        Button(move_bar, text="清空", command=self.clear_videos).pack(side=LEFT, fill=X, expand=True, padx=(6, 0))

        preview_frame = LabelFrame(left, text="当前片段预览")
        preview_frame.pack(fill=X, padx=8, pady=(0, 8))
        self.preview_label = Label(preview_frame, text="选择一个视频查看缩略图", width=36, height=8)
        self.preview_label.pack(padx=8, pady=8)

        right = Frame(main)
        right.pack(side=RIGHT, fill=BOTH, expand=True, padx=(12, 0))

        text_frame = LabelFrame(right, text="台词")
        text_frame.pack(fill=BOTH, expand=True)
        text_top = Frame(text_frame)
        text_top.pack(fill=X, padx=8, pady=(6, 2))
        Button(text_top, text="导入文本", command=self.import_script).pack(side=LEFT)
        Button(text_top, text="保存当前文本", command=self.save_script_as).pack(side=LEFT, padx=(6, 0))
        self.script_count_label = Label(text_top, text="0 行台词")
        self.script_count_label.pack(side=RIGHT)
        self.script_text = Text(text_frame, height=13, wrap="word", undo=True)
        self.script_text.pack(fill=BOTH, expand=True, padx=8, pady=(2, 8))
        self.script_text.bind("<KeyRelease>", lambda _event: self.refresh_mapping())

        mapping_frame = LabelFrame(right, text="视频片段 / 台词对应")
        mapping_frame.pack(fill=BOTH, expand=True, pady=(10, 0))
        self.mapping = ttk.Treeview(mapping_frame, columns=("index", "video", "duration", "line"), show="headings", height=8)
        self.mapping.heading("index", text="序号")
        self.mapping.heading("video", text="视频片段")
        self.mapping.heading("duration", text="视频时长")
        self.mapping.heading("line", text="对应台词")
        self.mapping.column("index", width=54, anchor="center")
        self.mapping.column("video", width=230)
        self.mapping.column("duration", width=76, anchor="center")
        self.mapping.column("line", width=520)
        self.mapping.pack(fill=BOTH, expand=True, padx=8, pady=8)
        self.mapping.bind("<<TreeviewSelect>>", self.on_mapping_select)

        file_frame = LabelFrame(right, text="文件")
        file_frame.pack(fill=X, pady=(10, 0))
        self._path_row(file_frame, "旁白录音", self.narration_path, self.choose_narration).pack(fill=X, padx=8, pady=(8, 3))
        self._path_row(file_frame, "背景音乐", self.music_path, self.choose_music).pack(fill=X, padx=8, pady=3)

        output_frame = LabelFrame(right, text="输出")
        output_frame.pack(fill=X, pady=(10, 0))
        row1 = Frame(output_frame)
        row1.pack(fill=X, padx=8, pady=(8, 3))
        Label(row1, text="故事名", width=10, anchor=W).pack(side=LEFT)
        Entry(row1, textvariable=self.story_name).pack(side=LEFT, fill=X, expand=True)
        row2 = Frame(output_frame)
        row2.pack(fill=X, padx=8, pady=3)
        Label(row2, text="保存到", width=10, anchor=W).pack(side=LEFT)
        Entry(row2, textvariable=self.output_dir).pack(side=LEFT, fill=X, expand=True)
        Button(row2, text="选择", command=self.choose_output_dir).pack(side=RIGHT, padx=(6, 0))

        settings = LabelFrame(right, text="设置")
        settings.pack(fill=X, pady=(10, 0))
        settings_row = Frame(settings)
        settings_row.pack(fill=X, padx=8, pady=8)
        Label(settings_row, text="Whisper").pack(side=LEFT)
        ttk.Combobox(
            settings_row,
            textvariable=self.whisper_model,
            values=("tiny", "base", "small", "medium", "large"),
            width=10,
            state="readonly",
        ).pack(side=LEFT, padx=(6, 14))
        Label(settings_row, text="语言").pack(side=LEFT)
        Entry(settings_row, textvariable=self.language, width=8).pack(side=LEFT, padx=(6, 14))
        ttk.Combobox(
            settings_row,
            textvariable=self.alignment_mode,
            values=("Whisper 语音识别对齐", "按台词长度平均分配"),
            width=20,
            state="readonly",
        ).pack(side=LEFT)
        Label(settings_row, text="音乐音量").pack(side=LEFT, padx=(14, 4))
        Entry(settings_row, textvariable=self.music_volume, width=6).pack(side=LEFT)
        Label(settings_row, text="旁白音量").pack(side=LEFT, padx=(14, 4))
        Entry(settings_row, textvariable=self.narration_volume, width=6).pack(side=LEFT)

        self.run_button = Button(right, text="开始合成", command=self.start_synthesis, height=2)
        self.run_button.pack(fill=X, pady=(12, 0))

        log_frame = LabelFrame(right, text="日志")
        log_frame.pack(fill=BOTH, expand=False, pady=(10, 0))
        self.log_text = Text(log_frame, height=8, wrap="word", state=DISABLED)
        self.log_text.pack(fill=BOTH, expand=True, padx=8, pady=8)

    def _path_row(self, parent: Frame, label: str, variable: StringVar, command) -> Frame:
        row = Frame(parent)
        Label(row, text=label, width=10, anchor=W).pack(side=LEFT)
        Entry(row, textvariable=variable).pack(side=LEFT, fill=X, expand=True)
        Button(row, text="选择", command=command).pack(side=RIGHT, padx=(6, 0))
        return row

    def add_folder(self) -> None:
        folder = filedialog.askdirectory(title="选择视频片段文件夹")
        if not folder:
            return
        paths = sorted_video_files(Path(folder))
        self._add_video_paths(paths)

    def add_videos(self) -> None:
        filetypes = [("视频文件", "*.mp4 *.mov *.m4v *.avi *.mkv *.webm"), ("所有文件", "*.*")]
        paths = filedialog.askopenfilenames(title="选择视频片段", filetypes=filetypes)
        self._add_video_paths([Path(path) for path in paths])

    def import_script(self) -> None:
        path = filedialog.askopenfilename(title="选择逐行台词文本", filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
        if not path:
            return
        text = Path(path).read_text(encoding="utf-8-sig")
        self.script_text.delete("1.0", END)
        self.script_text.insert("1.0", text)
        self.refresh_mapping()

    def save_script_as(self) -> None:
        path = filedialog.asksaveasfilename(title="保存台词文本", defaultextension=".txt", filetypes=[("文本文件", "*.txt")])
        if not path:
            return
        Path(path).write_text(self.script_text.get("1.0", END).strip() + "\n", encoding="utf-8")
        self.log(f"已保存台词：{path}")

    def choose_narration(self) -> None:
        path = filedialog.askopenfilename(title="选择旁白录音", filetypes=[("音频文件", "*.wav *.mp3 *.m4a *.aac *.flac"), ("所有文件", "*.*")])
        if path:
            self.narration_path.set(path)

    def choose_music(self) -> None:
        path = filedialog.askopenfilename(title="选择背景音乐", filetypes=[("音频文件", "*.wav *.mp3 *.m4a *.aac *.flac"), ("所有文件", "*.*")])
        if path:
            self.music_path.set(path)

    def choose_output_dir(self) -> None:
        folder = filedialog.askdirectory(title="选择输出文件夹")
        if folder:
            self.output_dir.set(folder)

    def _add_video_paths(self, paths: list[Path]) -> None:
        existing = {item.path.resolve() for item in self.video_items}
        new_items = []
        for path in paths:
            if path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            resolved = path.resolve()
            if resolved not in existing:
                new_items.append(VideoItem(path=resolved))
                existing.add(resolved)
        self.video_items.extend(new_items)
        self.refresh_video_list(load_metadata=True)

    def refresh_video_list(self, load_metadata: bool = False) -> None:
        for row in self.video_canvas.get_children():
            self.video_canvas.delete(row)
        for index, item in enumerate(self.video_items, start=1):
            if load_metadata and item.duration is None:
                item.duration = self._safe_duration(item.path)
            duration = f"{item.duration:.1f}s" if item.duration else "-"
            self.video_canvas.insert("", END, iid=str(index - 1), values=(index, item.path.name, duration))
        self.video_count_label.configure(text=f"{len(self.video_items)} 个视频")
        self.refresh_mapping()

    def refresh_mapping(self) -> None:
        lines = self.script_lines()
        self.script_count_label.configure(text=f"{len(lines)} 行台词")
        for row in self.mapping.get_children():
            self.mapping.delete(row)
        total = max(len(self.video_items), len(lines))
        for index in range(total):
            video = self.video_items[index] if index < len(self.video_items) else None
            line = lines[index] if index < len(lines) else ""
            duration = f"{video.duration:.1f}s" if video and video.duration else ""
            video_name = video.path.name if video else "缺少视频"
            line_text = line if line else "缺少台词"
            self.mapping.insert("", END, iid=str(index), values=(index + 1, video_name, duration, line_text))

    def script_lines(self) -> list[str]:
        return [line.strip() for line in self.script_text.get("1.0", END).splitlines() if line.strip()]

    def move_selected(self, direction: int) -> None:
        selected = self.video_canvas.selection()
        if not selected:
            return
        index = int(selected[0])
        target = index + direction
        if target < 0 or target >= len(self.video_items):
            return
        self.video_items[index], self.video_items[target] = self.video_items[target], self.video_items[index]
        self.refresh_video_list()
        self.video_canvas.selection_set(str(target))
        self.video_canvas.focus(str(target))

    def delete_selected(self) -> None:
        selected = self.video_canvas.selection()
        if not selected:
            return
        index = int(selected[0])
        del self.video_items[index]
        self.refresh_video_list()

    def clear_videos(self) -> None:
        self.video_items.clear()
        self.refresh_video_list()
        self.preview_label.configure(image="", text="选择一个视频查看缩略图")

    def on_video_select(self, _event=None) -> None:
        selected = self.video_canvas.selection()
        if not selected:
            return
        self.show_thumbnail(int(selected[0]))

    def on_mapping_select(self, _event=None) -> None:
        selected = self.mapping.selection()
        if not selected:
            return
        index = int(selected[0])
        if index < len(self.video_items):
            self.video_canvas.selection_set(str(index))
            self.video_canvas.focus(str(index))
            self.show_thumbnail(index)
        self.highlight_script_line(index + 1)

    def highlight_script_line(self, line_number: int) -> None:
        self.script_text.tag_remove("current_line", "1.0", END)
        start = f"{line_number}.0"
        end = f"{line_number}.end"
        self.script_text.tag_add("current_line", start, end)
        self.script_text.tag_configure("current_line", background="#fff3bf")
        self.script_text.see(start)

    def show_thumbnail(self, index: int) -> None:
        if index >= len(self.video_items):
            return
        item = self.video_items[index]
        if item.thumbnail_path is None:
            item.thumbnail_path = self._make_thumbnail(item.path, index)
        if item.thumbnail_path and item.thumbnail_path.exists():
            image = Image.open(item.thumbnail_path)
            image.thumbnail((360, 200))
            photo = ImageTk.PhotoImage(image)
            self.thumbnail_refs = [photo]
            self.preview_label.configure(image=photo, text="")
        else:
            self.preview_label.configure(image="", text=item.path.name)

    def start_synthesis(self) -> None:
        if self.is_running:
            return
        try:
            self._validate_before_run()
        except Exception as exc:
            messagebox.showerror("无法开始合成", str(exc))
            return

        self.is_running = True
        self.run_button.configure(text="合成中...", state=DISABLED)
        self.clear_log()
        thread = threading.Thread(target=self._run_synthesis_worker, daemon=True)
        thread.start()

    def _validate_before_run(self) -> None:
        if not self.video_items:
            raise ValueError("请先添加视频片段。")
        if not self.script_lines():
            raise ValueError("请先导入或填写逐行台词。")
        if len(self.video_items) != len(self.script_lines()):
            raise ValueError(f"视频有 {len(self.video_items)} 个，台词有 {len(self.script_lines())} 行，需要一一对应。")
        if not self.narration_path.get().strip():
            raise ValueError("请选择旁白录音。")
        if not self.music_path.get().strip():
            raise ValueError("请选择背景音乐。")
        if not self.output_dir.get().strip():
            raise ValueError("请选择输出文件夹。")

    def _run_synthesis_worker(self) -> None:
        try:
            output_base = Path(self.output_dir.get()).expanduser()
            story_dir = output_base / self._safe_folder_name(self.story_name.get())
            story_dir.mkdir(parents=True, exist_ok=True)
            video_dir = story_dir / "_selected_clips"
            video_dir.mkdir(parents=True, exist_ok=True)

            for index, item in enumerate(self.video_items, start=1):
                target = video_dir / f"{index:02d}_{item.path.name}"
                if not target.exists() or target.stat().st_size != item.path.stat().st_size:
                    shutil.copy2(item.path, target)

            script_path = story_dir / "_edited_script.txt"
            script_path.write_text("\n".join(self.script_lines()) + "\n", encoding="utf-8")
            alignment_mode = "even" if "平均" in self.alignment_mode.get() else "whisper"
            self.log_threadsafe(f"输出文件夹：{story_dir}")

            command = self._worker_command_prefix() + [
                "--video-dir",
                str(video_dir),
                "--script",
                str(script_path),
                "--narration",
                str(Path(self.narration_path.get()).expanduser()),
                "--music",
                str(Path(self.music_path.get()).expanduser()),
                "--output-dir",
                str(story_dir),
                "--whisper-model",
                self.whisper_model.get(),
                "--language",
                self.language.get().strip(),
                "--alignment-mode",
                alignment_mode,
                "--music-volume",
                self.music_volume.get(),
                "--narration-volume",
                self.narration_volume.get(),
            ]
            if self.bundled_model_dir.exists():
                command.extend(["--whisper-model-dir", str(self.bundled_model_dir)])

            env = os.environ.copy()
            env.update(
                {
                    "KMP_DUPLICATE_LIB_OK": "TRUE",
                    "OMP_NUM_THREADS": "1",
                    "MKL_NUM_THREADS": "1",
                    "NUMBA_NUM_THREADS": "1",
                    "NUMBA_THREADING_LAYER": "workqueue",
                }
            )
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                cwd=str(self._app_dir()),
            )
            assert process.stdout is not None
            for line in process.stdout:
                line = line.strip()
                if line:
                    self.log_threadsafe(line)
            return_code = process.wait()
            if return_code != 0:
                raise RuntimeError(f"合成进程退出，错误码：{return_code}")
            self.message_queue.put(("done", "合成完成。"))
        except Exception as exc:
            self.message_queue.put(("error", str(exc)))

    def _poll_messages(self) -> None:
        try:
            while True:
                kind, message = self.message_queue.get_nowait()
                if kind == "log":
                    self.log(message)
                elif kind == "done":
                    self.is_running = False
                    self.run_button.configure(text="开始合成", state=NORMAL)
                    self.log(message)
                    messagebox.showinfo("完成", message)
                elif kind == "error":
                    self.is_running = False
                    self.run_button.configure(text="开始合成", state=NORMAL)
                    self.log(f"失败：{message}")
                    messagebox.showerror("合成失败", message)
        except queue.Empty:
            pass
        self.root.after(150, self._poll_messages)

    def log_threadsafe(self, message: str) -> None:
        self.message_queue.put(("log", message))

    def log(self, message: str) -> None:
        self.log_text.configure(state=NORMAL)
        self.log_text.insert(END, message + "\n")
        self.log_text.see(END)
        self.log_text.configure(state=DISABLED)

    def clear_log(self) -> None:
        self.log_text.configure(state=NORMAL)
        self.log_text.delete("1.0", END)
        self.log_text.configure(state=DISABLED)

    def _safe_duration(self, path: Path) -> float | None:
        try:
            return probe_duration(path)
        except Exception:
            return None

    def _make_thumbnail(self, path: Path, index: int) -> Path | None:
        output = self.temp_dir / f"thumb_{index:03}.jpg"
        process = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                "0.3",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-q:v",
                "3",
                str(output),
            ],
            text=True,
            capture_output=True,
        )
        return output if process.returncode == 0 and output.exists() else None

    def _safe_folder_name(self, name: str) -> str:
        cleaned = "".join(char for char in name.strip() if char not in '/\\:*?"<>|')
        return cleaned or "完整故事"

    def _app_dir(self) -> Path:
        if getattr(sys, "frozen", False):
            return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return Path(__file__).resolve().parent

    def _worker_command_prefix(self) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, "--worker-synthesize"]
        return [sys.executable, str(self._app_dir() / "synthesize.py")]


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--worker-synthesize":
        from synthesize import main as synthesize_main

        sys.argv = [sys.argv[0]] + sys.argv[2:]
        synthesize_main()
        return

    root = Tk()
    StoryVideoApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
