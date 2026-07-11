from __future__ import annotations

import os
import queue
import hashlib
import csv
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from tkinter import (
    BOTH,
    BooleanVar,
    DISABLED,
    END,
    LEFT,
    NORMAL,
    RIGHT,
    W,
    X,
    Button,
    Checkbutton,
    Entry,
    Frame,
    Label,
    LabelFrame,
    StringVar,
    Text,
    Toplevel,
    Tk,
    filedialog,
    messagebox,
    ttk,
)

from analyze_storyboard_pacing import analyze_storyboard_pacing as run_storyboard_pacing_analysis
from story_codex_tasks import build_children_story_handoff, build_children_story_image_request, image_style_options
from story_project import create_theme_asset_request, ensure_project_dirs, load_config, project_paths


class StoryWorkbenchApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("儿童故事生产工作台")
        self.root.geometry("1380x880")
        self.root.minsize(1180, 760)

        self.message_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.is_running = False
        self.last_auto_slug = ""
        self.open_after_done: Path | None = None
        self.copy_after_done: Path | None = None
        self.current_task_args: list[str] = []
        self.current_task_started_at: float | None = None
        self.progress_total = 0
        self.progress_label = ""
        self.alarm_window = None
        self.alarm_active = False
        self._syncing_ui = False

        self.story_title = StringVar(value="新故事")
        self.slug = StringVar(value="story")
        self.short_slug = StringVar(value="story")
        self.output_root = StringVar(value=str(self._app_dir() / "output"))
        self.desktop_project_dir = StringVar(value=str(Path.home() / "Desktop" / "故事剪辑：新故事"))
        self.project_dir = StringVar()
        config = load_config()
        self.episode = StringVar(value=str(int(config.get("latest_episode", 92)) + 1))
        self.story_type = StringVar(value=str(config.get("default_story_type", "童话故事")))
        self.image_style = StringVar(value=str(config.get("default_image_style", "自动")))
        self.age_range = StringVar(value=str(config.get("default_age_range", "6-8岁")))
        self.duration_text = StringVar()
        self.duration_minutes = StringVar()
        self.duration_seconds = StringVar()
        self.update_latest_episode = BooleanVar(value=True)
        self.does_not_count_episode = BooleanVar(value=False)
        self.show_advanced = BooleanVar(value=False)
        self.status_text = StringVar(value="空闲")
        self.project_timer_text = StringVar(value="本故事总耗时：暂无记录｜步骤累计：暂无记录")

        self.text_image_dir = StringVar()
        self.image_dir = StringVar()
        self.storyboard_path = StringVar()
        self.narration_path = StringVar()
        self.music_path = StringVar()
        self.suno_audio_dir = StringVar()
        self.music_plan_path = StringVar()
        self.review_csv_path = StringVar()
        self.final_output_dir = StringVar()

        self.start_scene = StringVar(value="1")
        self.end_scene = StringVar(value="9999")
        self.limit = StringVar(value="0")
        self.whisper_model = StringVar(value="base")
        self.language = StringVar(value="zh")
        self.subtitle_style = StringVar(value="clean")
        video_api = config.get("video_api", {}) if isinstance(config.get("video_api"), dict) else {}
        self.api_key = StringVar(value=os.getenv("QINGYUN_API_KEY", ""))
        self.video_base_url = str(video_api.get("base_url") or "").strip()

        self._build_ui()
        self.story_title.trace_add("write", lambda *_: self._sync_generated_project_fields())
        self._refresh_paths()
        self._poll_messages()
        self._progress_tick()

    def _build_ui(self) -> None:
        main = Frame(self.root)
        main.pack(fill=BOTH, expand=True, padx=12, pady=10)

        top = LabelFrame(main, text="项目")
        top.pack(fill=X)
        self._entry_row(top, "故事名", self.story_title, width=28).pack(side=LEFT, fill=X, expand=True, padx=8, pady=8)
        Button(top, text="创建/更新项目文件夹", command=self.use_story_name_as_project).pack(side=RIGHT, padx=8, pady=8)

        story_info = LabelFrame(main, text="故事信息")
        story_info.pack(fill=X, pady=(10, 0))
        self._path_row(story_info, "桌面项目", self.desktop_project_dir, self.choose_desktop_project_dir).pack(fill=X, padx=8, pady=(8, 3))
        info_row = Frame(story_info)
        info_row.pack(fill=X, padx=8, pady=6)
        Label(info_row, text="集数").pack(side=LEFT)
        Entry(info_row, textvariable=self.episode, width=8).pack(side=LEFT, padx=(4, 12))
        Label(info_row, text="故事类型").pack(side=LEFT)
        ttk.Combobox(info_row, textvariable=self.story_type, values=tuple(load_config().get("story_type_options", [])), width=14).pack(side=LEFT, padx=(4, 12))
        Label(info_row, text="画面风格").pack(side=LEFT)
        ttk.Combobox(info_row, textvariable=self.image_style, values=tuple(image_style_options()), width=14, state="readonly").pack(side=LEFT, padx=(4, 12))
        Label(info_row, text="适龄段").pack(side=LEFT)
        ttk.Combobox(info_row, textvariable=self.age_range, values=tuple(load_config().get("age_range_options", [])), width=10).pack(side=LEFT, padx=(4, 12))
        Label(info_row, text="时长").pack(side=LEFT)
        Entry(info_row, textvariable=self.duration_minutes, width=4).pack(side=LEFT, padx=(4, 2))
        Label(info_row, text="分").pack(side=LEFT)
        Entry(info_row, textvariable=self.duration_seconds, width=4).pack(side=LEFT, padx=(4, 2))
        Label(info_row, text="秒").pack(side=LEFT, padx=(0, 12))
        Checkbutton(info_row, text="交付后更新集数", variable=self.update_latest_episode, command=self.on_update_latest_changed).pack(side=LEFT, padx=(0, 10))
        Checkbutton(info_row, text="本期不占集数", variable=self.does_not_count_episode, command=self.on_does_not_count_changed).pack(side=LEFT)
        Button(info_row, text="保存并识别", command=lambda: self._guard(self.setup_project)).pack(side=RIGHT, padx=(8, 0))

        body = Frame(main)
        body.pack(fill=BOTH, expand=True, pady=(10, 0))

        left = Frame(body)
        left.pack(side=LEFT, fill=BOTH, expand=True)

        quick = LabelFrame(left, text="推荐操作")
        quick.pack(fill=X)
        quick_grid = Frame(quick)
        quick_grid.pack(fill=X, padx=8, pady=8)
        quick_buttons = [
            ("① 保存并识别", self.setup_project),
            ("② 生成给Codex的出图任务", self.create_codex_image_request),
            ("③ 准备图生视频", self.prepare_jobs),
            ("④ 写入旁白时长", self.apply_timing),
            ("⑤ 生成视频片段", self.generate_videos),
            ("⑥ 打开审核页", self.open_review),
            ("⑦ 重跑审核问题", self.rerun_review_redos),
            ("⑧ 应用审核", self.apply_review),
            ("⑨ 生成配乐任务", self.create_music_request),
            ("⑩ 拼接音乐", self.assemble_music),
            ("⑪ 合成背景成片", self.assemble_final),
            ("⑫ 生成/打开发布素材任务书", self.prepare_release_assets),
            ("⑬ 接收并体检发布素材", self.preview_release_project),
            ("⑭ 生成发布视频", self.package_release_project),
            ("⑮ 生成/打开发布物料任务书", self.publish_package_project),
            ("⑯ 资料包前置审查", self.product_package_project),
            ("⑰ 正式打包资料包", self.product_package_final),
            ("⑱ 最终交付清单", self.final_delivery),
            ("⑲ 工程体检", self.doctor_project),
        ]
        for index, (text, command) in enumerate(quick_buttons):
            button = Button(quick_grid, text=text, command=lambda action=command: self._guard(action), height=2)
            button.grid(row=index // 5, column=index % 5, sticky="ew", padx=4, pady=4)
            quick_grid.columnconfigure(index % 5, weight=1)

        status = LabelFrame(left, text="运行状态")
        status.pack(fill=X, pady=(10, 0))
        self.progress_bar = ttk.Progressbar(status, mode="indeterminate")
        self.progress_bar.pack(fill=X, padx=8, pady=(8, 3))
        Label(status, textvariable=self.status_text, anchor=W).pack(fill=X, padx=8, pady=(0, 2))
        Label(status, textvariable=self.project_timer_text, anchor=W).pack(fill=X, padx=8, pady=(0, 8))

        self.advanced_container = Frame(left)
        Button(left, text="高级设置", command=self.toggle_advanced).pack(fill=X, pady=(10, 0))

        files = LabelFrame(self.advanced_container, text="高级路径（通常不用管）")
        files.pack(fill=X)
        self._path_row(files, "输出根目录", self.output_root, self.choose_output_root).pack(fill=X, padx=8, pady=(8, 3))
        self._path_row(files, "Codex出图目录", self.text_image_dir, self.choose_text_image_dir).pack(fill=X, padx=8, pady=3)
        self._path_row(files, "最终图片目录", self.image_dir, self.choose_image_dir).pack(fill=X, padx=8, pady=3)
        self._path_row(files, "分镜文档", self.storyboard_path, self.choose_storyboard).pack(fill=X, padx=8, pady=3)
        self._path_row(files, "旁白原声", self.narration_path, self.choose_narration).pack(fill=X, padx=8, pady=3)
        self._path_row(files, "背景音乐", self.music_path, self.choose_music).pack(fill=X, padx=8, pady=3)
        self._path_row(files, "Suno音频目录", self.suno_audio_dir, self.choose_suno_audio_dir).pack(fill=X, padx=8, pady=3)
        self._path_row(files, "音乐分段CSV", self.music_plan_path, self.choose_music_plan).pack(fill=X, padx=8, pady=3)
        self._path_row(files, "审核CSV", self.review_csv_path, self.choose_review_csv).pack(fill=X, padx=8, pady=(3, 8))

        settings = LabelFrame(self.advanced_container, text="高级设置")
        settings.pack(fill=X, pady=(10, 0))
        slug_row = Frame(settings)
        slug_row.pack(fill=X, padx=8, pady=(8, 0))
        Label(slug_row, text="系统短码", width=9, anchor=W).pack(side=LEFT)
        Entry(slug_row, textvariable=self.slug, width=24).pack(side=LEFT, padx=(0, 8))
        Label(slug_row, text="短名").pack(side=LEFT, padx=(6, 4))
        Entry(slug_row, textvariable=self.short_slug, width=10).pack(side=LEFT)
        Label(slug_row, text="自动生成；只有续接旧项目或特殊命名时才需要改").pack(side=LEFT, padx=(8, 0))
        row = Frame(settings)
        row.pack(fill=X, padx=8, pady=8)
        Label(row, text="Whisper", width=9, anchor=W).pack(side=LEFT)
        ttk.Combobox(row, textvariable=self.whisper_model, values=("tiny", "base", "small", "medium", "large"), width=9, state="readonly").pack(side=LEFT)
        Label(row, text="语言").pack(side=LEFT, padx=(14, 4))
        Entry(row, textvariable=self.language, width=8).pack(side=LEFT)
        Label(row, text="字幕").pack(side=LEFT, padx=(14, 4))
        ttk.Combobox(row, textvariable=self.subtitle_style, values=("clean", "box"), width=8, state="readonly").pack(side=LEFT)
        Label(row, text="场景").pack(side=LEFT, padx=(14, 4))
        Entry(row, textvariable=self.start_scene, width=5).pack(side=LEFT)
        Label(row, text="-").pack(side=LEFT, padx=3)
        Entry(row, textvariable=self.end_scene, width=5).pack(side=LEFT)
        Label(row, text="Limit").pack(side=LEFT, padx=(14, 4))
        Entry(row, textvariable=self.limit, width=5).pack(side=LEFT)

        key_row = Frame(settings)
        key_row.pack(fill=X, padx=8, pady=(0, 8))
        Label(key_row, text="青云 Key", width=9, anchor=W).pack(side=LEFT)
        Entry(key_row, textvariable=self.api_key, show="*", width=44).pack(side=LEFT, fill=X, expand=True)
        Label(key_row, text="可选，不保存；留空则使用环境变量").pack(side=LEFT, padx=(8, 0))

        steps = LabelFrame(self.advanced_container, text="旧流程细分按钮")
        steps.pack(fill=X, pady=(10, 0))
        step_grid = Frame(steps)
        step_grid.pack(fill=X, padx=8, pady=8)
        buttons = [
            ("0 生成给Codex的出图任务", self.create_codex_image_request),
            ("1 整理/导入图片", self.normalize_images),
            ("2 准备任务", self.prepare_jobs),
            ("3 写入时长", self.apply_timing),
            ("4 试跑一条", self.dry_run_one),
            ("5 生成视频", self.generate_videos),
            ("6 打开审核页", self.open_review),
            ("7 重跑审核问题", self.rerun_review_redos),
            ("8 应用审核", self.apply_review),
            ("9 生成配乐任务", self.create_music_request),
            ("10 打开配乐文本", self.open_music_prompts),
            ("11 打开Suno", self.open_suno),
            ("12 拼接音乐", self.assemble_music),
            ("13 合成成片", self.assemble_final),
            ("14 图片QA", self.qa_images),
            ("15 视频QA", self.qa_videos),
            ("16 生成/打开发布素材任务书", self.theme_assets),
            ("17 自动抠像", self.auto_keying),
            ("18 发布视频QA", self.qa_release),
            ("19 发布物料QA", self.qa_publish),
            ("20 资料包QA", self.qa_product),
            ("21 生成发布视频", self.package_release_project),
            ("22 生成/打开发布物料任务书", self.publish_package_project),
            ("23 资料包前置审查", self.product_package_project),
            ("24 正式打包资料包", self.product_package_final),
            ("25 最终交付清单", self.final_delivery),
            ("26 工程体检", self.doctor_project),
            ("打开项目目录", self.open_project_dir),
        ]
        for index, (text, command) in enumerate(buttons):
            button = Button(step_grid, text=text, command=lambda action=command: self._guard(action), height=2)
            button.grid(row=index // 5, column=index % 5, sticky="ew", padx=4, pady=4)
            step_grid.columnconfigure(index % 5, weight=1)

        derived = LabelFrame(left, text="当前项目路径")
        derived.pack(fill=X, pady=(10, 0))
        self.path_text = Text(derived, height=8, wrap="none", state=DISABLED)
        self.path_text.pack(fill=X, padx=8, pady=8)

        right = Frame(body)
        right.pack(side=RIGHT, fill=BOTH, expand=True, padx=(12, 0))

        script_box = LabelFrame(right, text="故事原文 / 备注（换行=候选分镜）")
        script_box.pack(fill=BOTH, expand=True)
        script_tools = Frame(script_box)
        script_tools.pack(fill=X, padx=8, pady=(8, 3))
        Button(script_tools, text="导入文本", command=self.import_story_text).pack(side=LEFT)
        Button(script_tools, text="保存到项目", command=self.save_story_text).pack(side=LEFT, padx=(6, 0))
        Button(script_tools, text="分析分镜节奏", command=lambda: self._guard(self.analyze_storyboard_pacing)).pack(side=LEFT, padx=(6, 0))
        self.story_text = Text(script_box, height=14, wrap="word", undo=True)
        self.story_text.pack(fill=BOTH, expand=True, padx=8, pady=(0, 8))

        log_frame = LabelFrame(right, text="日志")
        log_frame.pack(fill=BOTH, expand=True, pady=(10, 0))
        self.log_text = Text(log_frame, height=16, wrap="word", state=DISABLED)
        self.log_text.pack(fill=BOTH, expand=True, padx=8, pady=8)
        self.toggle_advanced(force=False)

    def _entry_row(self, parent: Frame, label: str, variable: StringVar, width: int = 24) -> Frame:
        row = Frame(parent)
        Label(row, text=label, anchor=W).pack(side=LEFT)
        Entry(row, textvariable=variable, width=width).pack(side=LEFT, fill=X, expand=True, padx=(6, 0))
        return row

    def _path_row(self, parent: Frame, label: str, variable: StringVar, command) -> Frame:
        row = Frame(parent)
        Label(row, text=label, width=12, anchor=W).pack(side=LEFT)
        Entry(row, textvariable=variable).pack(side=LEFT, fill=X, expand=True)
        Button(row, text="选择", command=command).pack(side=RIGHT, padx=(6, 0))
        return row

    def choose_output_root(self) -> None:
        self._choose_dir(self.output_root, "选择输出根目录")

    def choose_desktop_project_dir(self) -> None:
        folder = filedialog.askdirectory(title="选择桌面故事项目文件夹")
        if folder:
            self.desktop_project_dir.set(folder)
            self._refresh_paths()
            self._load_manifest_to_ui()

    def use_story_name_as_project(self) -> None:
        title = self.story_title.get().strip() or "新故事"
        self.desktop_project_dir.set(str(Path.home() / "Desktop" / f"故事剪辑：{title}"))
        self._sync_generated_project_fields(force=True)
        self._refresh_paths()
        paths = project_paths(Path(self.desktop_project_dir.get()).expanduser())
        ensure_project_dirs(paths)
        self.log(f"已创建/更新桌面项目文件夹：{paths.root}")

    def _sync_generated_project_fields(self, *, force: bool = False) -> None:
        if self._syncing_ui:
            return
        title = self.story_title.get().strip()
        if not title:
            return
        slug = self._safe_slug(title)
        try:
            self._syncing_ui = True
            if force or self.slug.get().strip() in {"", "story", self.last_auto_slug}:
                self.slug.set(slug)
                self.short_slug.set(self._short_slug(slug))
            current = Path(self.desktop_project_dir.get()).expanduser()
            default_old = Path.home() / "Desktop" / "故事剪辑：新故事"
            if force or not self.desktop_project_dir.get().strip() or current == default_old:
                self.desktop_project_dir.set(str(Path.home() / "Desktop" / f"故事剪辑：{title}"))
        finally:
            self._syncing_ui = False

    def on_does_not_count_changed(self) -> None:
        if self.does_not_count_episode.get():
            self.update_latest_episode.set(False)

    def on_update_latest_changed(self) -> None:
        if self.update_latest_episode.get():
            self.does_not_count_episode.set(False)

    def toggle_advanced(self, force: bool | None = None) -> None:
        show = (not self.show_advanced.get()) if force is None else force
        self.show_advanced.set(show)
        if show:
            self.advanced_container.pack(fill=X, pady=(10, 0))
        else:
            self.advanced_container.pack_forget()

    def choose_text_image_dir(self) -> None:
        self._choose_dir(self.text_image_dir, "选择 Codex 出图结果文件夹")

    def choose_image_dir(self) -> None:
        self._choose_dir(self.image_dir, "选择最终图片文件夹")

    def choose_storyboard(self) -> None:
        self._choose_file(self.storyboard_path, "选择分镜文档", [("文档", "*.docx *.txt *.md"), ("所有文件", "*.*")])

    def choose_narration(self) -> None:
        self._choose_file(self.narration_path, "选择旁白原声", [("音频", "*.mp3 *.wav *.m4a *.aac *.flac"), ("所有文件", "*.*")])

    def choose_music(self) -> None:
        self._choose_file(self.music_path, "选择背景音乐", [("音频", "*.mp3 *.wav *.m4a *.aac *.flac"), ("所有文件", "*.*")])

    def choose_suno_audio_dir(self) -> None:
        self._choose_dir(self.suno_audio_dir, "选择 Suno 下载音频文件夹")

    def choose_music_plan(self) -> None:
        self._choose_file(self.music_plan_path, "选择音乐分段 CSV", [("CSV", "*.csv"), ("所有文件", "*.*")])

    def choose_review_csv(self) -> None:
        self._choose_file(self.review_csv_path, "选择审核页导出的 review_decisions.csv", [("CSV", "*.csv"), ("所有文件", "*.*")])

    def _choose_dir(self, variable: StringVar, title: str) -> None:
        folder = filedialog.askdirectory(title=title)
        if folder:
            variable.set(folder)
            self._refresh_paths()

    def _choose_file(self, variable: StringVar, title: str, filetypes) -> None:
        path = filedialog.askopenfilename(title=title, filetypes=filetypes)
        if path:
            variable.set(path)
            self._refresh_paths()

    def import_story_text(self) -> None:
        path = filedialog.askopenfilename(title="导入故事文本", filetypes=[("文本", "*.txt *.md"), ("所有文件", "*.*")])
        if not path:
            return
        self.story_text.delete("1.0", END)
        self.story_text.insert("1.0", Path(path).read_text(encoding="utf-8-sig"))

    def save_story_text(self) -> None:
        self._refresh_paths()
        paths = project_paths(Path(self.desktop_project_dir.get()).expanduser())
        paths.inputs.mkdir(parents=True, exist_ok=True)
        path = paths.inputs / f"{self.slug.get().strip() or 'story'}_source.txt"
        story = self._clean_story_input(self.story_text.get("1.0", END).strip())
        path.write_text(story + "\n", encoding="utf-8")
        self.log(f"已保存故事文本：{path}")

    def create_codex_image_request(self) -> None:
        self._refresh_paths()
        story = self._clean_story_input(self.story_text.get("1.0", END).strip())
        self._require(story, "请先在右侧粘贴故事原文，或导入故事文本。")
        paths = project_paths(Path(self.desktop_project_dir.get()).expanduser())
        project_dir = paths.video_jobs
        image_dir = paths.images / "images"
        paths.inputs.mkdir(parents=True, exist_ok=True)
        paths.images.mkdir(parents=True, exist_ok=True)
        project_dir.mkdir(parents=True, exist_ok=True)
        image_dir.mkdir(parents=True, exist_ok=True)

        slug = self.slug.get().strip()
        source_path = paths.inputs / f"{slug}_source.txt"
        storyboard_path = paths.images / f"{slug}_storyboard_lines.txt"
        request_path = paths.images / f"{slug}_codex_image_request.md"
        handoff_path = paths.images / f"{slug}_codex_handoff.txt"
        source_path.write_text(story + "\n", encoding="utf-8")
        manual_lines = [line.strip() for line in story.splitlines() if line.strip()]
        if len(manual_lines) > 1:
            storyboard_path.write_text("\n".join(manual_lines) + "\n", encoding="utf-8")
        pacing = self._run_storyboard_pacing_analysis(story, paths.images, slug, open_report=False)
        request_path.write_text(
            self._codex_image_request_text(
                story=story,
                source_path=source_path,
                storyboard_path=storyboard_path,
                image_dir=image_dir,
                manual_lines=manual_lines,
                pacing_report_path=pacing.report_path if pacing else None,
                pacing_draft_path=pacing.draft_path if pacing else None,
                pacing_summary=pacing.summary if pacing else "",
            ),
            encoding="utf-8",
        )
        self.storyboard_path.set(str(storyboard_path))
        self.text_image_dir.set(str(image_dir))
        self.image_dir.set(str(image_dir))
        self.log(f"已生成 Codex 出图任务：{request_path}")
        self.log(f"分镜文本目标：{storyboard_path}")
        self.log(f"最终图片目标：{image_dir}")
        handoff = self._codex_handoff_text(request_path)
        handoff_path.write_text(handoff + "\n", encoding="utf-8")
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(handoff)
        except Exception:
            pass
        self.log(f"Codex 交接指令：{handoff_path}")
        self.log("下一步：这段给 Codex 的指令已复制到剪贴板，并已打开交接指令文件。请回到当前 Codex 对话窗口粘贴并发送。")
        self.log(handoff)
        self._open_document(handoff_path)
        self.start_alarm(
            "出图任务已经生成，需要交给 Codex。\n\n"
            f"交接指令已复制并打开：\n{handoff_path}\n\n"
            "请回到当前 Codex 对话窗口粘贴并发送。"
        )

    def analyze_storyboard_pacing(self) -> None:
        self._refresh_paths()
        story = self._clean_story_input(self.story_text.get("1.0", END).strip())
        self._require(story, "请先在右侧粘贴故事原文，或导入故事文本。")
        paths = project_paths(Path(self.desktop_project_dir.get()).expanduser())
        paths.images.mkdir(parents=True, exist_ok=True)
        slug = self.slug.get().strip() or "story"
        analysis = self._run_storyboard_pacing_analysis(story, paths.images, slug, open_report=True)
        if analysis:
            self.log(f"分镜节奏分析：{analysis.report_path}")
            self.log(f"建议换行草稿：{analysis.draft_path}")
            self.log(analysis.summary)

    def _run_storyboard_pacing_analysis(self, story: str, output_dir: Path, slug: str, *, open_report: bool):
        model_dir = self._app_dir() / "models" / "whisper"
        analysis = run_storyboard_pacing_analysis(
            story_text=story,
            output_dir=output_dir,
            slug=slug,
            narration_path=Path(self.narration_path.get()).expanduser() if self.narration_path.get().strip() else None,
            whisper_model=self.whisper_model.get(),
            language=self.language.get(),
            whisper_model_dir=model_dir if model_dir.exists() else None,
        )
        if open_report:
            self._open_document(analysis.report_path)
        return analysis

    def _codex_handoff_text(self, request_path: Path) -> str:
        return build_children_story_handoff(request_path, full_auto=False)

    def _clean_story_input(self, text: str) -> str:
        marker = "## 故事原文"
        marker_at = text.rfind(marker)
        if marker_at >= 0:
            tail = text[marker_at + len(marker) :]
            blocks = re.findall(r"```(?:text)?\s*(.*?)```", tail, flags=re.S)
            if blocks:
                return blocks[0].strip()
        return text.strip()

    def normalize_images(self) -> None:
        self._refresh_paths()
        self._require(self.text_image_dir.get(), "请选择 Codex 出图结果文件夹。")
        source_dir = Path(self.text_image_dir.get()).expanduser()
        target_dir = project_paths(Path(self.desktop_project_dir.get()).expanduser()).images / "images"
        if source_dir.resolve() == target_dir.resolve():
            self.image_dir.set(str(target_dir))
            self.log("Codex 出图目录已经是最终图片目录，无需再整理。可以直接点“2 准备任务”。")
            return
        count = self._guess_expected_count()
        command = [
            "normalize-images",
            "--source-dir",
            str(source_dir),
            "--output-dir",
            str(project_paths(Path(self.desktop_project_dir.get()).expanduser()).video_jobs),
            "--slug",
            self.slug.get(),
        ]
        if count:
            command.extend(["--count", str(count)])
        self.image_dir.set(str(project_paths(Path(self.desktop_project_dir.get()).expanduser()).video_jobs / "images"))
        self._run_workflow(command)

    def _guard(self, action) -> None:
        try:
            action()
        except Exception as exc:
            messagebox.showerror("无法执行", str(exc))

    def prepare_jobs(self) -> None:
        self._refresh_paths()
        self._require(self.image_dir.get(), "请选择最终图片目录。")
        self._require(self.storyboard_path.get(), "请选择分镜文档。")
        if self.jobs_csv().exists():
            self._prompt_review_decisions_csv()
        self.open_after_done = self.review_html()
        self._run_workflow(
            [
                "prepare",
                "--image-dir",
                self.image_dir.get(),
                "--storyboard",
                self.storyboard_path.get(),
                "--output-dir",
                self.project_dir.get(),
                "--slug",
                self.slug.get(),
                "--short-slug",
                self.short_slug.get(),
            ]
        )

    def apply_timing(self) -> None:
        self._refresh_paths()
        self._require(self.jobs_csv(), "请先准备任务。")
        self._require(self.narration_path.get(), "请选择旁白原声。")
        command = [
            "timing",
            "--jobs-csv",
            str(self.jobs_csv()),
            "--narration",
            self.narration_path.get(),
            "--whisper-model",
            self.whisper_model.get(),
            "--language",
            self.language.get(),
        ]
        model_dir = self._app_dir() / "models" / "whisper"
        if model_dir.exists():
            command.extend(["--whisper-model-dir", str(model_dir)])
        self._run_workflow(command)

    def dry_run_one(self) -> None:
        self._refresh_paths()
        self._require(self.jobs_csv(), "请先准备任务。")
        self._run_workflow(self._generate_command(dry_run=True, limit_override="1"))

    def generate_videos(self) -> None:
        self._refresh_paths()
        self._require(self.jobs_csv(), "请先准备任务。")
        prompt_review_csv = self._prompt_review_decisions_csv()
        if not prompt_review_csv.exists():
            messagebox.showerror(
                "请先确认图生视频提示词",
                "生成视频片段前需要先打开审核页，检查/修改图生视频提示词，"
                "并点击“导出提示词确认CSV”。\n\n"
                f"期望文件：\n{prompt_review_csv}",
            )
            if self.review_html().exists():
                self._open_document(self.review_html())
            return
        command = self._generate_command(dry_run=False)
        command.extend(["--prompt-review-csv", str(prompt_review_csv)])
        self._run_workflow(command)

    def open_review(self) -> None:
        self._refresh_paths()
        path = self.review_html()
        if not path.exists():
            messagebox.showerror("找不到审核页", f"请先准备任务：\n{path}")
            return
        self.review_csv_path.set(str(self.current_review_csv_path()))
        self._open_path(path)

    def open_project_dir(self) -> None:
        self._refresh_paths()
        project_dir = Path(self.desktop_project_dir.get()).expanduser()
        project_dir.mkdir(parents=True, exist_ok=True)
        self._open_path(project_dir)

    def save_story_info(self) -> None:
        self._refresh_paths()
        project_dir = Path(self.desktop_project_dir.get()).expanduser()
        command = [
            "set-story-info",
            "--project-dir",
            str(project_dir),
            "--story-name",
            self.story_title.get(),
            "--slug",
            self.slug.get(),
            "--story-type",
            self.story_type.get(),
            "--image-style",
            self.image_style.get(),
            "--age-range",
            self.age_range.get(),
            "--update-latest-episode-on-delivery",
            "yes" if self.update_latest_episode.get() else "no",
            "--does-not-count-episode",
            "yes" if self.does_not_count_episode.get() else "no",
        ]
        if self.episode.get().strip():
            command.extend(["--episode", self.episode.get().strip()])
        duration_text = self._duration_text_value()
        if duration_text:
            command.extend(["--duration-text", duration_text])
        self._run_workflow(command)

    def setup_project(self) -> None:
        self._refresh_paths()
        story = self._clean_story_input(self.story_text.get("1.0", END).strip())
        if story:
            self.save_story_text()
        command = [
            "setup-project",
            "--project-dir",
            self.desktop_project_dir.get(),
            "--story-name",
            self.story_title.get(),
            "--slug",
            self.slug.get(),
            "--story-type",
            self.story_type.get(),
            "--image-style",
            self.image_style.get(),
            "--age-range",
            self.age_range.get(),
            "--update-latest-episode-on-delivery",
            "yes" if self.update_latest_episode.get() else "no",
            "--does-not-count-episode",
            "yes" if self.does_not_count_episode.get() else "no",
            "--extract-audio",
        ]
        if self.episode.get().strip():
            command.extend(["--episode", self.episode.get().strip()])
        duration_text = self._duration_text_value()
        if duration_text:
            command.extend(["--duration-text", duration_text])
        self._run_workflow(command)

    def detect_assets(self) -> None:
        self._refresh_paths()
        self._run_workflow(["detect-assets", "--project-dir", self.desktop_project_dir.get(), "--extract-audio"])

    def qa_images(self) -> None:
        self._refresh_paths()
        self._run_workflow(["qa-images", "--project-dir", self.desktop_project_dir.get(), "--image-dir", self.image_dir.get()])

    def qa_videos(self) -> None:
        self._refresh_paths()
        self._run_workflow(["qa-videos", "--project-dir", self.desktop_project_dir.get(), "--videos-dir", str(self.videos_dir())])

    def theme_assets(self) -> None:
        self._refresh_paths()
        self.prepare_release_assets()

    def auto_keying(self) -> None:
        self._refresh_paths()
        self._run_workflow(["auto-keying", "--project-dir", self.desktop_project_dir.get()])

    def prepare_release_assets(self) -> None:
        self._refresh_paths()
        outputs = create_theme_asset_request(Path(self.desktop_project_dir.get()).expanduser())
        request_path = outputs["request"]
        handoff_path = outputs["handoff"]
        handoff = handoff_path.read_text(encoding="utf-8")
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(handoff)
        except Exception:
            pass
        self.log(f"已生成发布视觉定版 Codex 任务：{request_path}")
        self.log("说明：打开的 md 是自动填入项目信息的规格书；剪贴板里的文字才是交给 Codex 执行素材生成、抠像融合和预览定参的指令。")
        self.log("下一步：请回到当前 Codex 对话窗口，直接粘贴剪贴板内容并发送。")
        self.log(handoff)
        self._open_document(request_path)
        messagebox.showinfo(
            "下一步：交给 Codex 做发布视觉定版",
            "规格书已经生成并打开；它是自动填入项目信息和路径的规则文件，不是最终智能提示词。\n\n"
            "交给 Codex 执行发布素材生成、绿幕抠像、融合预览和布局定参的指令已复制到剪贴板。请回到当前 Codex 对话窗口，直接粘贴并发送。\n\n"
            "Codex 会生成素材、预览图并写回 keying_preset.json。完成后回工作台点击 ⑬ 接收并体检发布素材；如果体检通过，会打开预览图。你看着满意后再点 ⑭ 生成发布视频。",
        )

    def qa_release(self) -> None:
        self._refresh_paths()
        self._run_workflow(["qa-release", "--project-dir", self.desktop_project_dir.get()])

    def qa_product(self) -> None:
        self._refresh_paths()
        self._run_workflow(["qa-product", "--project-dir", self.desktop_project_dir.get()])

    def qa_publish(self) -> None:
        self._refresh_paths()
        self._run_workflow(["qa-publish", "--project-dir", self.desktop_project_dir.get()])

    def package_release_project(self) -> None:
        self._refresh_paths()
        self._run_workflow(["package-release-project", "--project-dir", self.desktop_project_dir.get()])

    def preview_release_project(self) -> None:
        self._refresh_paths()
        project_dir = Path(self.desktop_project_dir.get()).expanduser()
        preview_dir = project_dir / "99_项目状态" / "release_preview_frames"
        self.open_after_done = preview_dir
        self.copy_after_done = preview_dir / "release_preview_feedback_to_codex.md"
        self.log("第 13 步会先检查发布素材和故事框，再生成/刷新预览图。素材不合格会在这里拦住，不代表你点错了。")
        self._run_workflow(["preview-release-project", "--project-dir", self.desktop_project_dir.get()])

    def publish_package_project(self) -> None:
        self._refresh_paths()
        project_dir = Path(self.desktop_project_dir.get()).expanduser()
        handoff = project_dir / "05_发布物料" / "publish_package_codex_handoff.md"
        self.open_after_done = handoff
        self.copy_after_done = handoff
        try:
            self.root.clipboard_clear()
        except Exception:
            pass
        self.log("第 15 步开始生成发布物料任务书；完成后会自动打开任务书，并复制新版 Codex 交接指令。")
        self._run_workflow(["publish-package-project", "--project-dir", self.desktop_project_dir.get()])

    def product_package_project(self) -> None:
        self._refresh_paths()
        project_dir = Path(self.desktop_project_dir.get()).expanduser()
        handoff = project_dir / "06_资料包" / "_work" / "第16步资料包_Codex前置审查.md"
        self.open_after_done = handoff
        self.copy_after_done = handoff
        try:
            self.root.clipboard_clear()
        except Exception:
            pass
        self.log("第 16 步先生成 Codex 前置审查材料：示范视频预览帧、朗读标注请求和正式生成说明。")
        self._run_workflow(["product-package-preflight-project", "--project-dir", self.desktop_project_dir.get()])

    def product_package_final(self) -> None:
        self._refresh_paths()
        project_dir = Path(self.desktop_project_dir.get()).expanduser()
        work_dir = project_dir / "06_资料包" / "_work"
        annotation = self._find_product_annotation(work_dir)
        if annotation is None:
            selected = filedialog.askopenfilename(
                title="选择精修后的朗读标注 annotation.json 或 docx",
                initialdir=str(work_dir if work_dir.exists() else project_dir),
                filetypes=[("朗读标注", "*.json *.docx"), ("所有文件", "*.*")],
            )
            if not selected:
                self.log("已取消正式打包：没有选择精修朗读标注。")
                return
            annotation = Path(selected)
        command = ["product-package-project", "--project-dir", self.desktop_project_dir.get()]
        if annotation.suffix.lower() == ".json":
            command.extend(["--annotation-json", str(annotation)])
        elif annotation.suffix.lower() == ".docx":
            command.extend(["--annotation-docx", str(annotation)])
        else:
            messagebox.showerror("朗读标注格式不支持", "请选择 .json 或 .docx 格式的精修朗读标注。")
            return
        demo_params = self._product_demo_params(work_dir)
        for key, option in (
            ("demo_person_crop_mode", "--demo-person-crop-mode"),
            ("demo_person_vertical_align", "--demo-person-vertical-align"),
            ("demo_person_crop_bottom_ratio", "--demo-person-crop-bottom-ratio"),
        ):
            if key in demo_params:
                command.extend([option, str(demo_params[key])])
        self.open_after_done = project_dir / "06_资料包"
        self.log(f"第 17 步开始正式打包资料包，使用朗读标注：{annotation}")
        self._run_workflow(command)

    def _find_product_annotation(self, work_dir: Path) -> Path | None:
        candidates = [
            work_dir / "annotation.json",
            work_dir / "朗读标注.json",
            work_dir / "朗读标注.docx",
        ]
        candidates.extend(sorted(work_dir.glob("*标注*.json")) if work_dir.exists() else [])
        candidates.extend(sorted(work_dir.glob("*标注*.docx")) if work_dir.exists() else [])
        for path in candidates:
            if path.exists() and "需精修" not in path.name:
                return path
        return None

    def _product_demo_params(self, work_dir: Path) -> dict:
        params_path = work_dir / "demo_params.json"
        if not params_path.exists():
            return {}
        try:
            data = json.loads(params_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.log(f"读取 demo_params.json 失败，将使用默认示范视频参数：{exc}")
            return {}
        if not isinstance(data, dict):
            self.log("demo_params.json 不是对象，将使用默认示范视频参数。")
            return {}
        return data

    def final_delivery(self) -> None:
        self._refresh_paths()
        command = ["final-delivery", "--project-dir", self.desktop_project_dir.get()]
        if self.update_latest_episode.get() and not self.does_not_count_episode.get():
            command.append("--update-latest-episode")
        self._run_workflow(command)

    def doctor_project(self) -> None:
        self._refresh_paths()
        project_dir = Path(self.desktop_project_dir.get()).expanduser()
        report = project_dir / "99_项目状态" / "doctor_project_report.md"
        self.open_after_done = report
        self._run_workflow(["doctor-project", "--project-dir", self.desktop_project_dir.get()])

    def apply_review(self) -> None:
        self._refresh_paths()
        self._require(self.jobs_csv(), "请先准备任务。")
        command = [
            "apply-review",
            "--jobs-csv",
            str(self.jobs_csv()),
            "--videos-dir",
            str(self.videos_dir()),
            "--output-dir",
            self.final_output_dir.get(),
            "--short-slug",
            self.short_slug.get(),
        ]
        decisions_csv = self._review_decisions_csv()
        if decisions_csv.exists():
            command.extend(["--decisions-csv", str(decisions_csv)])
        self._run_workflow(command)

    def rerun_review_redos(self) -> None:
        self._refresh_paths()
        self._require(self.jobs_csv(), "请先准备任务。")
        decisions_csv = self._review_decisions_csv()
        self._require(decisions_csv, "请先在审核页导出审核 CSV，或选择审核 CSV。")
        self.review_csv_path.set(str(decisions_csv))
        command = [
            "rerun-review",
            "--jobs-csv",
            str(self.jobs_csv()),
            "--images-dir",
            str(self.images_dir()),
            "--videos-dir",
            str(self.videos_dir()),
            "--decisions-csv",
            str(decisions_csv),
        ]
        self._run_workflow(command)

    def create_music_request(self) -> None:
        self._refresh_paths()
        story_path = self._ensure_story_file()
        command = [
            "music-request",
            "--story-file",
            str(story_path),
            "--output-dir",
            self.project_dir.get(),
            "--slug",
            self.slug.get(),
            "--story-title",
            self.story_title.get(),
        ]
        if self.narration_path.get().strip():
            command.extend(["--narration", self.narration_path.get()])
        if self.jobs_csv().exists():
            command.extend(["--jobs-csv", str(self.jobs_csv())])
        self.music_plan_path.set(str(self.music_plan_csv()))
        self.suno_audio_dir.set(str(self.suno_downloads_dir()))
        self.open_after_done = self.music_dir() / f"{self.slug.get()}_suno_music_request.md"
        self._run_workflow(command)

    def open_suno(self) -> None:
        self._open_path("https://suno.com/create")

    def open_music_prompts(self) -> None:
        self._refresh_paths()
        prompts_path = self.suno_prompts_path()
        request_path = self.music_dir() / f"{self.slug.get()}_suno_music_request.md"
        if request_path.exists() and (
            not prompts_path.exists() or request_path.stat().st_mtime > prompts_path.stat().st_mtime
        ):
            self._open_document(request_path)
            messagebox.showinfo(
                "需要 Codex 生成",
                "目前是给 Codex 的智能配乐任务书。请把它发给 Codex，让 Codex 根据故事、旁白和时间轴生成 Suno 提示词与音乐分段 CSV。",
            )
            return
        if prompts_path.exists():
            self._open_document(prompts_path)
            return
        if request_path.exists():
            self._open_document(request_path)
            messagebox.showinfo(
                "需要 Codex 生成",
                "目前是给 Codex 的智能配乐任务书。请把它发给 Codex，让 Codex 根据故事、旁白和时间轴生成 Suno 提示词与音乐分段 CSV。",
            )
            return
        raise FileNotFoundError("请先点击 9 生成配乐任务。")

    def assemble_music(self) -> None:
        self._refresh_paths()
        plan_csv = Path(self.music_plan_path.get()).expanduser() if self.music_plan_path.get().strip() else self.music_plan_csv()
        clips_dir = Path(self.suno_audio_dir.get()).expanduser() if self.suno_audio_dir.get().strip() else self.suno_downloads_dir()
        output = self.background_music_path()
        self._require(plan_csv, "请先生成或选择音乐分段 CSV。")
        self._require(clips_dir, "请选择 Suno 下载音频目录。")
        self.music_path.set(str(output))
        self._run_workflow(
            [
                "assemble-music",
                "--plan-csv",
                str(plan_csv),
                "--clips-dir",
                str(clips_dir),
                "--output",
                str(output),
            ]
        )

    def assemble_final(self) -> None:
        self._refresh_paths()
        self._require(self.narration_path.get(), "请选择旁白原声。")
        paths = project_paths(Path(self.desktop_project_dir.get()).expanduser())
        if not self.music_path.get().strip():
            generated_music = self.background_music_path()
            if generated_music.exists():
                self.music_path.set(str(generated_music))
            else:
                raise ValueError("请选择背景音乐，或先点击“10 拼接音乐”。")
        final_dir = Path(self.final_output_dir.get()).expanduser()
        command = [
            "assemble",
            "--video-dir",
            str(final_dir / "clips"),
            "--script",
            str(final_dir / "script_lines.txt"),
            "--narration",
            self.narration_path.get(),
            "--music",
            self.music_path.get(),
            "--output-dir",
            str(final_dir),
            "--whisper-model",
            self.whisper_model.get(),
            "--language",
            self.language.get(),
            "--subtitle-style",
            self.subtitle_style.get(),
        ]
        story_source = paths.inputs / "story_source.txt"
        if story_source.exists():
            command.extend(["--subtitle-script", str(story_source)])
        model_dir = self._app_dir() / "models" / "whisper"
        if model_dir.exists():
            command.extend(["--whisper-model-dir", str(model_dir)])
        self._run_workflow(command)

    def _generate_command(self, *, dry_run: bool, limit_override: str | None = None) -> list[str]:
        self._refresh_paths()
        command = [
            "generate",
            "--jobs-csv",
            str(self.jobs_csv()),
            "--images-dir",
            str(self.images_dir()),
            "--videos-dir",
            str(self.videos_dir()),
            "--start-scene",
            self.start_scene.get().strip() or "1",
            "--end-scene",
            self.end_scene.get().strip() or "9999",
        ]
        limit = limit_override if limit_override is not None else self.limit.get().strip()
        if limit and limit != "0":
            command.extend(["--limit", limit])
        if dry_run:
            command.append("--dry-run")
        return command

    def _run_workflow(self, args: list[str]) -> None:
        if self.is_running:
            messagebox.showinfo("正在运行", "当前任务还没有结束。")
            return
        self._refresh_paths()
        self.is_running = True
        self.current_task_args = list(args)
        self.current_task_started_at = time.time()
        self.progress_total = self._estimate_progress_total(args)
        self.progress_label = self._task_label(args)
        self.status_text.set(f"正在运行：{self.progress_label}")
        self.progress_bar.start(12)
        self.clear_log()
        self.log("运行：python3 story_workflow.py " + " ".join(args))
        thread = threading.Thread(target=self._worker, args=(args,), daemon=True)
        thread.start()

    def _worker(self, args: list[str]) -> None:
        try:
            command = [sys.executable, str(self._app_dir() / "story_workflow.py")] + args
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
            if self.api_key.get().strip():
                env["QINGYUN_API_KEY"] = self.api_key.get().strip()
            if self.video_base_url:
                env["QINGYUN_BASE_URL"] = self.video_base_url
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
                line = line.rstrip()
                if line:
                    self.message_queue.put(("log", line))
            return_code = process.wait()
            if return_code != 0:
                raise RuntimeError(f"流程退出，错误码：{return_code}")
            self.message_queue.put(("done", "完成。"))
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
                    self.progress_bar.stop()
                    self._record_task_timing("done")
                    self.log(message)
                    self.status_text.set(f"已完成：{self.progress_label}，用时 {self._elapsed_text()}")
                    self._refresh_paths()
                    self._load_manifest_to_ui()
                    open_after_done = self.open_after_done
                    self.open_after_done = None
                    copy_after_done = self.copy_after_done
                    self.copy_after_done = None
                    if copy_after_done and copy_after_done.exists():
                        try:
                            handoff = copy_after_done.read_text(encoding="utf-8")
                            self.root.clipboard_clear()
                            self.root.clipboard_append(handoff)
                            self.log("已复制 Codex 交接任务到剪贴板。请回到当前 Codex 对话窗口粘贴并发送。")
                            message = f"{message}\n\n已复制 Codex 交接任务：{copy_after_done}"
                        except Exception as exc:
                            self.log(f"复制 Codex 交接任务失败：{exc}")
                    if open_after_done and open_after_done.exists():
                        if open_after_done.is_dir():
                            self._open_path(open_after_done)
                        else:
                            self._open_document(open_after_done)
                        message = f"{message}\n\n已打开：{open_after_done}"
                    self.start_alarm(f"{self.progress_label} 已完成，需要你接着处理。\n\n{message}")
                elif kind == "error":
                    self.is_running = False
                    self.progress_bar.stop()
                    self._record_task_timing("error")
                    self.log(f"失败：{message}")
                    self.status_text.set(f"失败：{self.progress_label}，用时 {self._elapsed_text()}")
                    self.start_alarm(f"{self.progress_label} 失败，需要查看日志。\n\n{message}")
        except queue.Empty:
            pass
        self.root.after(150, self._poll_messages)

    def _progress_tick(self) -> None:
        paths = project_paths(Path(self.desktop_project_dir.get()).expanduser())
        if self.is_running and self.current_task_started_at is not None:
            progress = self._progress_text()
            self.status_text.set(f"正在运行：{self.progress_label}｜已用时 {self._elapsed_text()}{progress}")
        self._update_project_timer(paths)
        self.root.after(1000, self._progress_tick)

    def _task_label(self, args: list[str]) -> str:
        labels = {
            "setup-project": "保存并识别",
            "prepare": "准备图生视频",
            "timing": "写入旁白时长",
            "generate": "生成视频片段",
            "rerun-review": "重跑审核问题",
            "apply-review": "应用审核",
            "music-request": "生成配乐任务",
            "assemble-music": "拼接音乐",
            "assemble": "合成背景成片",
            "prepare-release-assets-project": "发布视觉定版任务",
            "preview-release-project": "刷新发布预览",
            "package-release-project": "生成发布视频",
            "publish-package-project": "生成发布物料任务书",
            "product-package-preflight-project": "资料包前置审查",
            "product-package-project": "正式打包资料包",
            "final-delivery": "最终交付清单",
            "doctor-project": "工程体检",
        }
        return labels.get(args[0] if args else "", args[0] if args else "任务")

    def _estimate_progress_total(self, args: list[str]) -> int:
        if not args or args[0] != "generate":
            return 0
        jobs = self.jobs_csv()
        if not jobs.exists():
            return 0
        try:
            start = int(self.start_scene.get().strip() or "1")
            end = int(self.end_scene.get().strip() or "9999")
            limit = int(self.limit.get().strip() or "0")
        except ValueError:
            start, end, limit = 1, 9999, 0
        try:
            with jobs.open(encoding="utf-8-sig", newline="") as file:
                rows = [
                    row for row in csv.DictReader(file)
                    if start <= int(row.get("scene", "0") or 0) <= end
                ]
        except Exception:
            return 0
        total = len(rows)
        if limit:
            total = min(total, limit)
        return total

    def _progress_text(self) -> str:
        if self.current_task_args and self.current_task_args[0] == "generate" and self.progress_total:
            done = len([path for path in self.videos_dir().glob("*.mp4") if path.is_file()])
            done = min(done, self.progress_total)
            return f"｜视频片段 {done}/{self.progress_total}"
        return ""

    def _elapsed_text(self) -> str:
        if self.current_task_started_at is None:
            return "0秒"
        seconds = int(time.time() - self.current_task_started_at)
        minutes, sec = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}小时{minutes:02d}分{sec:02d}秒"
        if minutes:
            return f"{minutes}分{sec:02d}秒"
        return f"{sec}秒"

    def _record_task_timing(self, status: str) -> None:
        if self.current_task_started_at is None:
            return
        paths = project_paths(Path(self.desktop_project_dir.get()).expanduser())
        history_path = paths.status / "workbench_run_history.json"
        paths.status.mkdir(parents=True, exist_ok=True)
        try:
            history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else []
        except Exception:
            history = []
        record = {
            "project_root": str(paths.root),
            "story_name": self.story_title.get().strip(),
            "episode": self.episode.get().strip(),
            "task": self.current_task_args[0] if self.current_task_args else "",
            "label": self.progress_label,
            "status": status,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.current_task_started_at)),
            "ended_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_seconds": round(time.time() - self.current_task_started_at, 2),
            "command_args": self.current_task_args,
        }
        project_started_at = self._project_started_at(paths)
        if project_started_at is not None:
            record["project_started_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(project_started_at))
            record["project_wall_elapsed_seconds"] = round(max(0.0, time.time() - project_started_at), 2)
            step_total, step_count = self._project_elapsed_seconds(paths)
            record["project_step_elapsed_seconds_before_this"] = round(step_total, 2)
            record["project_done_step_count_before_this"] = step_count
        history.append(record)
        history_path.write_text(json.dumps(history[-300:], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self._append_global_timing_record(record)

    def _append_global_timing_record(self, record: dict) -> None:
        metrics_dir = self._app_dir() / "output" / "workbench_metrics"
        metrics_dir.mkdir(parents=True, exist_ok=True)
        jsonl = metrics_dir / "workbench_run_history.jsonl"
        with jsonl.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _project_timing_summary(self, paths, *, include_current: bool = False) -> str:
        total, count = self._project_elapsed_seconds(paths)
        if include_current and self.is_running and self.current_task_started_at is not None:
            total += time.time() - self.current_task_started_at
        history_path = paths.status / "workbench_run_history.json"
        if not history_path.exists() and total <= 0:
            return "暂无记录"
        if count == 0 and total > 0:
            return self._format_seconds(total)
        if count == 0:
            return "暂无完成步骤"
        return f"{self._format_seconds(total)}（{count} 个完成步骤）"

    def _update_project_timer(self, paths) -> None:
        self.project_timer_text.set(
            f"本故事总耗时：{self._project_wall_clock_summary(paths)}｜"
            f"步骤累计：{self._project_timing_summary(paths, include_current=True)}"
        )

    def _project_wall_clock_summary(self, paths) -> str:
        started_at = self._project_started_at(paths)
        if started_at is None:
            return "暂无记录"
        finished_at = self._project_finished_at(paths)
        end_at = finished_at if finished_at is not None else time.time()
        return self._format_seconds(max(0.0, end_at - started_at))

    def _project_finished_at(self, paths) -> float | None:
        if not paths.manifest.exists():
            return None
        try:
            data = json.loads(paths.manifest.read_text(encoding="utf-8"))
        except Exception:
            return None
        value = data.get("completed_at")
        if not value:
            return None
        try:
            return time.mktime(time.strptime(str(value), "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            return None

    def _project_started_at(self, paths) -> float | None:
        if paths.manifest.exists():
            try:
                data = json.loads(paths.manifest.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            for key in ("created_at", "updated_at"):
                value = data.get(key)
                if not value:
                    continue
                try:
                    return time.mktime(time.strptime(str(value), "%Y-%m-%d %H:%M:%S"))
                except ValueError:
                    continue
        if paths.root.exists():
            stat = paths.root.stat()
            return float(getattr(stat, "st_birthtime", stat.st_ctime))
        return None

    def _project_elapsed_seconds(self, paths) -> tuple[float, int]:
        history_path = paths.status / "workbench_run_history.json"
        if not history_path.exists():
            return 0.0, 0
        try:
            history = json.loads(history_path.read_text(encoding="utf-8"))
        except Exception:
            return 0.0, 0
        total = sum(float(item.get("elapsed_seconds", 0) or 0) for item in history if item.get("status") == "done")
        count = len([item for item in history if item.get("status") == "done"])
        return total, count

    def _format_seconds(self, seconds: float) -> str:
        seconds = int(round(seconds))
        minutes, sec = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}小时{minutes:02d}分{sec:02d}秒"
        if minutes:
            return f"{minutes}分{sec:02d}秒"
        return f"{sec}秒"

    def start_alarm(self, message: str) -> None:
        self.alarm_active = True
        if self.alarm_window is not None and self.alarm_window.winfo_exists():
            try:
                self.alarm_window.destroy()
            except Exception:
                pass
        self.alarm_window = Toplevel(self.root)
        self.alarm_window.title("需要处理")
        self.alarm_window.geometry("460x180")
        Label(self.alarm_window, text=message, wraplength=400, justify=LEFT).pack(fill=X, padx=20, pady=(22, 12))
        Button(self.alarm_window, text="我知道了，停止提醒", command=self.stop_alarm).pack(pady=(0, 16))
        self.alarm_window.protocol("WM_DELETE_WINDOW", self.stop_alarm)
        try:
            self.alarm_window.lift()
            self.alarm_window.attributes("-topmost", True)
            self.alarm_window.after(1200, lambda: self.alarm_window.attributes("-topmost", False))
        except Exception:
            pass
        self._ring_alarm()

    def _ring_alarm(self) -> None:
        if not self.alarm_active:
            return
        try:
            self.root.bell()
        except Exception:
            pass
        self._play_alert_sound()
        self.root.after(2500, self._ring_alarm)

    def _play_alert_sound(self) -> None:
        if sys.platform != "darwin":
            return
        sound = Path("/System/Library/Sounds/Ping.aiff")
        if not sound.exists():
            sound = Path("/System/Library/Sounds/Glass.aiff")
        if not sound.exists():
            return
        try:
            subprocess.Popen(["afplay", str(sound)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def stop_alarm(self) -> None:
        self.alarm_active = False
        if self.alarm_window is not None:
            try:
                self.alarm_window.destroy()
            except Exception:
                pass
        self.alarm_window = None

    def _refresh_paths(self) -> None:
        slug = self._safe_slug(self.slug.get() or self.story_title.get())
        if slug != self.slug.get():
            self.slug.set(slug)
        if not self.desktop_project_dir.get().strip():
            self.desktop_project_dir.set(str(Path.home() / "Desktop" / f"故事剪辑：{self.story_title.get().strip() or slug}"))
        paths = project_paths(Path(self.desktop_project_dir.get()).expanduser())
        project_dir = paths.video_jobs
        final_dir = paths.assembly
        music_dir = project_dir / "music"
        self._update_project_timer(paths)
        old_project_dir = None
        self.project_dir.set(str(project_dir))
        current_job_images = project_dir / "images"
        current_story_images = paths.images / "images"
        if self._should_refresh_auto_path(self.image_dir.get(), old_project_dir, "images") or not self._is_under_project(self.image_dir.get(), paths.root):
            self.image_dir.set(str(current_job_images if current_job_images.exists() else current_story_images))
        if self._should_refresh_auto_path(self.text_image_dir.get(), old_project_dir, "images") or not self._is_under_project(self.text_image_dir.get(), paths.root):
            self.text_image_dir.set("")
        if self._should_refresh_auto_path(self.storyboard_path.get(), old_project_dir, f"{self.last_auto_slug}_storyboard_lines.txt") or not self._is_under_project(self.storyboard_path.get(), paths.root):
            self.storyboard_path.set("")
        if self._should_refresh_auto_path(self.suno_audio_dir.get(), old_project_dir, "music/suno_downloads") or not self._is_under_project(self.suno_audio_dir.get(), paths.root):
            self.suno_audio_dir.set(str(music_dir / "suno_downloads"))
        if self._should_refresh_auto_path(self.music_plan_path.get(), old_project_dir, f"music/{self.last_auto_slug}_music_plan.csv") or not self._is_under_project(self.music_plan_path.get(), paths.root):
            self.music_plan_path.set(str(music_dir / f"{slug}_music_plan.csv"))
        if self._should_refresh_auto_path(self.music_path.get(), old_project_dir, f"music/{self.last_auto_slug}_background_music.mp3") or not self._is_under_project(self.music_path.get(), paths.root):
            self.music_path.set("")
        self.review_csv_path.set(str(project_dir / "review_decisions.csv"))
        self.final_output_dir.set(str(final_dir))
        self.last_auto_slug = slug
        lines = [
            f"桌面故事文件夹：{paths.root}",
            f"项目状态：{paths.manifest}",
            f"总耗时：{self._project_wall_clock_summary(paths)}",
            f"步骤累计：{self._project_timing_summary(paths)}",
            f"图生视频目录：{project_dir}",
            f"图片目录：{self.image_dir.get()}",
            f"视频目录：{project_dir / 'videos'}",
            f"音乐目录：{music_dir}",
            f"音乐分段：{music_dir / (slug + '_music_plan.csv')}",
            f"Suno下载：{music_dir / 'suno_downloads'}",
            f"背景音乐：{music_dir / (slug + '_background_music.mp3')}",
            f"任务CSV：{project_dir / (slug + '_image_video_jobs.csv')}",
            f"审核页：{project_dir / (slug + '_review.html')}",
            f"审核CSV：{project_dir / 'review_decisions.csv'}",
            f"最终合成：{final_dir}",
            f"最终片段：{final_dir / 'clips'}",
            f"逐行文本：{final_dir / 'script_lines.txt'}",
        ]
        self.path_text.configure(state=NORMAL)
        self.path_text.delete("1.0", END)
        self.path_text.insert("1.0", "\n".join(lines))
        self.path_text.configure(state=DISABLED)

    def _load_manifest_to_ui(self) -> None:
        paths = project_paths(Path(self.desktop_project_dir.get()).expanduser())
        if not paths.manifest.exists():
            return
        try:
            manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
        except Exception:
            return
        story = manifest.get("story", {})
        inputs = manifest.get("inputs", {})
        if story.get("name"):
            self.story_title.set(str(story["name"]))
        if story.get("slug"):
            self.slug.set(str(story["slug"]))
        if story.get("short_slug"):
            self.short_slug.set(str(story["short_slug"]))
        if story.get("episode"):
            self.episode.set(str(story["episode"]))
        if story.get("story_type"):
            self.story_type.set(str(story["story_type"]))
        if story.get("image_style"):
            self.image_style.set(str(story["image_style"]))
        if story.get("age_range"):
            self.age_range.set(str(story["age_range"]))
        if story.get("duration_text"):
            self.duration_text.set(str(story["duration_text"]))
            self._set_duration_fields(str(story["duration_text"]))
        self.update_latest_episode.set(bool(story.get("update_latest_episode_on_delivery", True)))
        self.does_not_count_episode.set(bool(story.get("does_not_count_episode", False)))
        if self.update_latest_episode.get() and self.does_not_count_episode.get():
            self.update_latest_episode.set(False)
        if inputs.get("narration"):
            self.narration_path.set(str(inputs["narration"]))
        story_text = inputs.get("story_text")
        if story_text and Path(story_text).exists() and not self.story_text.get("1.0", END).strip():
            try:
                self.story_text.insert("1.0", Path(story_text).read_text(encoding="utf-8-sig"))
            except Exception:
                pass

    def _should_refresh_auto_path(self, value: str, old_project_dir: Path | None, relative: str) -> bool:
        if not value.strip():
            return True
        if old_project_dir is None:
            return False
        try:
            return Path(value).expanduser().resolve() == (old_project_dir / relative).resolve()
        except Exception:
            return False

    def _is_under_project(self, value: str, project_root: Path) -> bool:
        if not value.strip():
            return True
        try:
            Path(value).expanduser().resolve().relative_to(project_root.resolve())
            return True
        except Exception:
            return False

    def jobs_csv(self) -> Path:
        return Path(self.project_dir.get()).expanduser() / f"{self.slug.get()}_image_video_jobs.csv"

    def review_html(self) -> Path:
        return Path(self.project_dir.get()).expanduser() / f"{self.slug.get()}_review.html"

    def _review_decisions_csv(self) -> Path:
        default_path = self.current_review_csv_path()
        selected = Path(self.review_csv_path.get()).expanduser() if self.review_csv_path.get().strip() else None
        if selected and selected.exists() and selected.resolve() != default_path.resolve():
            default_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(selected, default_path)
            return default_path
        downloaded = self._latest_downloaded_review_csv()
        prompt_review = self._latest_downloaded_prompt_review_csv()
        if self._csv_has_video_review_marks(prompt_review) and (
            downloaded is None or prompt_review.stat().st_mtime >= downloaded.stat().st_mtime
        ):
            downloaded = prompt_review
        if downloaded is not None and (not default_path.exists() or downloaded.stat().st_mtime >= default_path.stat().st_mtime):
            default_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(downloaded, default_path)
            self.log(f"已从下载目录导入审核CSV：{downloaded}")
            return default_path
        if default_path.exists():
            return default_path
        if downloaded is not None:
            default_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(downloaded, default_path)
            self.log(f"已从下载目录导入审核CSV：{downloaded}")
            return default_path
        if selected and selected.exists():
            return selected
        return default_path

    def current_review_csv_path(self) -> Path:
        return Path(self.project_dir.get()).expanduser() / "review_decisions.csv"

    def current_prompt_review_csv_path(self) -> Path:
        return Path(self.project_dir.get()).expanduser() / "prompt_review_decisions.csv"

    def _prompt_review_decisions_csv(self) -> Path:
        default_path = self.current_prompt_review_csv_path()
        downloaded = self._latest_downloaded_prompt_review_csv()
        if downloaded is not None and (not default_path.exists() or downloaded.stat().st_mtime >= default_path.stat().st_mtime):
            default_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(downloaded, default_path)
            self.log(f"已从下载目录导入提示词确认CSV：{downloaded}")
            return default_path
        return default_path

    def _latest_downloaded_review_csv(self) -> Path | None:
        download_dirs = [
            Path.home() / "Downloads",
            Path.home() / "Desktop" / "谷歌浏览器下载",
            Path.home() / "Desktop" / "浏览器下载",
        ]
        candidates = [
            path
            for folder in download_dirs
            if folder.exists()
            for path in folder.glob("review_decisions*.csv")
            if path.is_file()
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def _csv_has_video_review_marks(self, path: Path | None) -> bool:
        if path is None or not path.exists():
            return False
        try:
            with path.open(encoding="utf-8-sig", newline="") as file:
                rows = csv.DictReader(file)
                return any((row.get("review_status") or "").strip() in {"redo", "unused"} for row in rows)
        except Exception:
            return False

    def _latest_downloaded_prompt_review_csv(self) -> Path | None:
        download_dirs = [
            Path.home() / "Downloads",
            Path.home() / "Desktop" / "谷歌浏览器下载",
            Path.home() / "Desktop" / "浏览器下载",
        ]
        candidates = [
            path
            for folder in download_dirs
            if folder.exists()
            for path in folder.glob("prompt_review_decisions*.csv")
            if path.is_file()
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def images_dir(self) -> Path:
        return Path(self.image_dir.get()).expanduser()

    def videos_dir(self) -> Path:
        return Path(self.project_dir.get()).expanduser() / "videos"

    def music_dir(self) -> Path:
        return Path(self.project_dir.get()).expanduser() / "music"

    def music_plan_csv(self) -> Path:
        return self.music_dir() / f"{self.slug.get()}_music_plan.csv"

    def suno_prompts_path(self) -> Path:
        return self.music_dir() / f"{self.slug.get()}_suno_prompts.md"

    def suno_downloads_dir(self) -> Path:
        return self.music_dir() / "suno_downloads"

    def background_music_path(self) -> Path:
        return self.music_dir() / f"{self.slug.get()}_background_music.mp3"

    def _ensure_story_file(self) -> Path:
        self._refresh_paths()
        paths = project_paths(Path(self.desktop_project_dir.get()).expanduser())
        paths.inputs.mkdir(parents=True, exist_ok=True)
        story_path = paths.inputs / f"{self.slug.get().strip() or 'story'}_source.txt"
        story = self._clean_story_input(self.story_text.get("1.0", END).strip())
        if story:
            story_path.write_text(story + "\n", encoding="utf-8")
        elif not story_path.exists() or story_path.stat().st_size == 0:
            raise ValueError("请先在右侧粘贴故事原文，或导入故事文本。")
        return story_path

    def _guess_expected_count(self) -> int:
        text = self.story_text.get("1.0", END).strip()
        lines = [line for line in text.splitlines() if line.strip()]
        return len(lines) if lines else 0

    def _codex_image_request_text(
        self,
        *,
        story: str,
        source_path: Path,
        storyboard_path: Path,
        image_dir: Path,
        manual_lines: list[str],
        pacing_report_path: Path | None = None,
        pacing_draft_path: Path | None = None,
        pacing_summary: str = "",
    ) -> str:
        slug = self.slug.get().strip()
        return build_children_story_image_request(
            story_title=self.story_title.get().strip() or slug,
            story=story,
            source_path=source_path,
            storyboard_path=storyboard_path,
            image_dir=image_dir,
            slug=slug,
            short_slug=self.short_slug.get().strip(),
            skill_path=self._app_dir() / "skills" / "children-storyboard-images" / "SKILL.md",
            manual_lines=manual_lines,
            full_auto=False,
            story_type=self.story_type.get().strip(),
            image_style=self.image_style.get().strip(),
            pacing_report_path=pacing_report_path,
            pacing_draft_path=pacing_draft_path,
            pacing_summary=pacing_summary,
        )

    def _open_path(self, path: Path | str) -> None:
        target = str(path)
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", target])
            elif os.name == "nt":
                os.startfile(target)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", target])
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))

    def _open_document(self, path: Path) -> None:
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-a", "TextEdit", str(path)])
            else:
                self._open_path(path)
        except Exception:
            self._open_path(path)

    def _duration_text_value(self) -> str:
        minutes = self.duration_minutes.get().strip()
        seconds = self.duration_seconds.get().strip()
        if minutes or seconds:
            try:
                minute_value = max(0, int(minutes or "0"))
                second_value = max(0, int(seconds or "0"))
            except ValueError as exc:
                raise ValueError("时长请填写数字分钟和数字秒数。") from exc
            minute_value += second_value // 60
            second_value = second_value % 60
            if minute_value and second_value:
                return f"{minute_value}分{second_value:02d}秒"
            if minute_value:
                return f"{minute_value}分钟"
            if second_value:
                return f"{second_value}秒"
        return self.duration_text.get().strip()

    def _set_duration_fields(self, value: str) -> None:
        text = value.strip()
        if not text:
            self.duration_minutes.set("")
            self.duration_seconds.set("")
            return
        minute = 0
        second = 0
        match = re.search(r"(\d+)\s*分", text)
        if match:
            minute = int(match.group(1))
        match = re.search(r"(\d+)\s*秒", text)
        if match:
            second = int(match.group(1))
        if not minute and not second:
            colon = re.match(r"^(\d+):(\d{1,2})$", text)
            if colon:
                minute = int(colon.group(1))
                second = int(colon.group(2))
        self.duration_minutes.set(str(minute) if minute else "")
        self.duration_seconds.set(str(second) if second else "")

    def _require(self, value, message: str) -> None:
        if isinstance(value, Path):
            if not value.exists():
                raise FileNotFoundError(message)
            return
        if not str(value).strip():
            raise ValueError(message)

    def log(self, message: str) -> None:
        self.log_text.configure(state=NORMAL)
        self.log_text.insert(END, message + "\n")
        self.log_text.see(END)
        self.log_text.configure(state=DISABLED)

    def clear_log(self) -> None:
        self.log_text.configure(state=NORMAL)
        self.log_text.delete("1.0", END)
        self.log_text.configure(state=DISABLED)

    def _safe_slug(self, value: str) -> str:
        original = value.strip()
        known = {
            "自相矛盾": "zixiangmaodun",
            "胡萝卜妖怪": "huluobo-yaoguai",
        }
        if original in known:
            return known[original]
        value = original.lower().replace(" ", "-")
        value = re.sub(r"[^a-z0-9._-]+", "-", value)
        value = re.sub(r"-+", "-", value).strip("-")
        if value and (re.search(r"[a-z]", value) or len(value) >= 3):
            return value
        if original:
            digest = hashlib.sha1(original.encode("utf-8")).hexdigest()[:8]
            return f"story-{digest}"
        return "story"

    def _short_slug(self, slug: str) -> str:
        parts = [part for part in re.split(r"[-_.]+", slug) if part]
        if len(parts) > 1:
            return "".join(part[0] for part in parts)[:8] or "st"
        return slug[:6] or "story"

    def _app_dir(self) -> Path:
        if getattr(sys, "frozen", False):
            return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return Path(__file__).resolve().parent


def main() -> None:
    root = Tk()
    StoryWorkbenchApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
