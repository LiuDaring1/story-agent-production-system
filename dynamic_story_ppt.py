#!/usr/bin/env python3
"""Build full-motion, WPS-oriented story PPT variants from finished clips."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterable

from lxml import etree


SCHEMA_VERSION = "story-ppt-plan-v1"
PPT_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
PPT_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PPT_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PPT_P14_NS = "http://schemas.microsoft.com/office/powerpoint/2010/main"
PPT_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
PPT_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
PPT_VIDEO_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/video"
PPT_AUDIO_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/audio"
PPT_MEDIA_REL = "http://schemas.microsoft.com/office/2007/relationships/media"
PPT_P14_EXT_URI = "{DAA4B4D4-6D71-4841-9C94-3DE7FCFB9230}"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_command(command: list[str]) -> None:
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(
            f"命令失败（{completed.returncode}）：{' '.join(command)}\n{completed.stderr[-4000:]}"
        )


def probe_duration(path: Path, ffprobe: str = "ffprobe") -> float:
    completed = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"无法读取视频时长：{path}\n{completed.stderr[-2000:]}")
    duration = float(completed.stdout.strip())
    if duration <= 0:
        raise ValueError(f"视频时长无效：{path}")
    return duration


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "-", value.strip()).strip("-.")
    return cleaned or "shot"


def load_source_plan(path: Path) -> dict[str, Any]:
    payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"动态 PPT 计划版本必须是 {SCHEMA_VERSION}")
    slides = payload.get("slides")
    if not isinstance(slides, list) or not slides:
        raise ValueError("动态 PPT 计划必须包含非空 slides")
    seen: set[str] = set()
    for index, row in enumerate(slides, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"slides[{index}] 必须是对象")
        shot_id = str(row.get("shot_id") or "").strip()
        if not shot_id or shot_id in seen:
            raise ValueError(f"slides[{index}].shot_id 缺失或重复")
        seen.add(shot_id)
        source = Path(str(row.get("video_path") or "")).expanduser()
        if not source.is_file() or source.stat().st_size <= 0:
            raise FileNotFoundError(f"slides[{index}] 视频不存在或为空：{source}")
        if not isinstance(row.get("subtitle", ""), str):
            raise ValueError(f"slides[{index}].subtitle 必须是字符串")
    return payload


def create_proxy_and_poster(
    *,
    source: Path,
    proxy: Path,
    poster: Path,
    ffmpeg: str,
    ffprobe: str,
) -> float:
    proxy.parent.mkdir(parents=True, exist_ok=True)
    poster.parent.mkdir(parents=True, exist_ok=True)
    run_command([
        ffmpeg, "-y", "-i", str(source),
        "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:black,setsar=1",
        "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "28",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(proxy),
    ])
    duration = probe_duration(proxy, ffprobe)
    run_command([
        ffmpeg, "-y", "-ss", f"{min(0.12, duration / 4):.3f}", "-i", str(proxy),
        "-frames:v", "1", "-q:v", "2", str(poster),
    ])
    return duration


def _next_relationship_id(root: etree._Element) -> str:
    used = {element.get("Id") for element in root}
    index = 1
    while f"rId{index}" in used:
        index += 1
    return f"rId{index}"


def _add_relationship(
    root: etree._Element,
    rel_type: str,
    target: str,
    *,
    external: bool = False,
) -> str:
    rel_id = _next_relationship_id(root)
    element = etree.SubElement(root, f"{{{PPT_RELS_NS}}}Relationship")
    element.set("Id", rel_id)
    element.set("Type", rel_type)
    element.set("Target", target)
    if external:
        element.set("TargetMode", "External")
    return rel_id


def _ensure_content_type(files: dict[str, bytes], extension: str, content_type: str) -> None:
    root = etree.fromstring(files["[Content_Types].xml"])
    defaults = root.findall(f"{{{PPT_CT_NS}}}Default")
    if not any((element.get("Extension") or "").lower() == extension.lower() for element in defaults):
        element = etree.SubElement(root, f"{{{PPT_CT_NS}}}Default")
        element.set("Extension", extension)
        element.set("ContentType", content_type)
        files["[Content_Types].xml"] = etree.tostring(
            root, xml_declaration=True, encoding="UTF-8", standalone=True
        )


def _media_timing_xml(video_shape_id: int, audio_shape_id: int | None, audio_loop: bool) -> etree._Element:
    audio_xml = ""
    build_audio = ""
    if audio_shape_id is not None:
        repeat = ' repeatCount="indefinite"' if audio_loop else ""
        audio_xml = (
            f'<p:audio><p:cMediaNode numSld="999" showWhenStopped="0">'
            f'<p:cTn id="30"{repeat} fill="hold" display="1"><p:stCondLst><p:cond delay="0"/>'
            f'</p:stCondLst></p:cTn><p:tgtEl><p:spTgt spid="{audio_shape_id}"/>'
            f'</p:tgtEl></p:cMediaNode></p:audio>'
        )
        build_audio = f'<p:bldP spid="{audio_shape_id}" grpId="0"/>'
    xml = (
        f'<p:timing xmlns:p="{PPT_P_NS}"><p:tnLst><p:par>'
        f'<p:cTn id="1" dur="indefinite" restart="never" nodeType="tmRoot"><p:childTnLst>'
        f'<p:seq concurrent="1" nextAc="seek"><p:cTn id="2" dur="indefinite" nodeType="mainSeq">'
        f'<p:childTnLst><p:par><p:cTn id="3" fill="hold"><p:stCondLst>'
        f'<p:cond delay="indefinite"/><p:cond evt="onBegin" delay="0"><p:tn val="2"/></p:cond>'
        f'</p:stCondLst><p:childTnLst><p:par><p:cTn id="4" fill="hold"><p:stCondLst>'
        f'<p:cond delay="0"/></p:stCondLst><p:childTnLst><p:par>'
        f'<p:cTn id="5" presetID="1" presetClass="mediacall" presetSubtype="0" fill="hold" nodeType="afterEffect">'
        f'<p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>'
        f'<p:cmd type="call" cmd="playFrom(0.0)"><p:cBhvr additive="base">'
        f'<p:cTn id="6" dur="1" fill="hold"/><p:tgtEl><p:spTgt spid="{video_shape_id}"/>'
        f'</p:tgtEl></p:cBhvr></p:cmd></p:childTnLst></p:cTn></p:par>'
        f'</p:childTnLst></p:cTn></p:par></p:childTnLst></p:cTn></p:par>'
        f'</p:childTnLst></p:cTn><p:prevCondLst><p:cond evt="onPrev" delay="0">'
        f'<p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:prevCondLst><p:nextCondLst>'
        f'<p:cond evt="onNext" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond>'
        f'</p:nextCondLst></p:seq><p:video><p:cMediaNode showWhenStopped="1">'
        f'<p:cTn id="20" fill="hold" display="1"><p:stCondLst><p:cond delay="0"/>'
        f'</p:stCondLst></p:cTn><p:tgtEl><p:spTgt spid="{video_shape_id}"/>'
        f'</p:tgtEl></p:cMediaNode></p:video>{audio_xml}</p:childTnLst></p:cTn>'
        f'</p:par></p:tnLst><p:bldLst><p:bldP spid="{video_shape_id}" grpId="0"/>'
        f'{build_audio}</p:bldLst></p:timing>'
    )
    return etree.fromstring(xml.encode("utf-8"))


def _add_video_metadata(
    picture: etree._Element,
    video_rel: str,
    media_rel: str,
    *,
    embedded: bool,
) -> int:
    namespace = {"p": PPT_P_NS, "a": PPT_A_NS}
    non_visual = picture.find("p:nvPicPr/p:nvPr", namespaces=namespace)
    properties = picture.find("p:nvPicPr/p:cNvPr", namespaces=namespace)
    if non_visual is None or properties is None:
        raise ValueError("PPTX poster 结构异常，无法附加视频")
    shape_id = int(properties.get("id", "0"))
    video_file = etree.SubElement(non_visual, f"{{{PPT_A_NS}}}videoFile")
    video_file.set(f"{{{PPT_R_NS}}}link", video_rel)
    ext_list = etree.SubElement(non_visual, f"{{{PPT_P_NS}}}extLst")
    ext = etree.SubElement(ext_list, f"{{{PPT_P_NS}}}ext")
    ext.set("uri", PPT_P14_EXT_URI)
    media = etree.SubElement(ext, f"{{{PPT_P14_NS}}}media", nsmap={"p14": PPT_P14_NS})
    media.set(f"{{{PPT_R_NS}}}{'embed' if embedded else 'link'}", media_rel)
    return shape_id


def _add_audio_picture(
    slide: etree._Element,
    audio_rel: str,
    media_rel: str,
    *,
    embedded: bool,
) -> int:
    namespace = {"p": PPT_P_NS}
    shape_tree = slide.find("p:cSld/p:spTree", namespaces=namespace)
    if shape_tree is None:
        raise ValueError("slide XML 缺少 spTree")
    ids = [int(value) for value in slide.xpath("//@id") if str(value).isdigit()]
    shape_id = max(ids or [1]) + 1
    xml = (
        f'<p:pic xmlns:p="{PPT_P_NS}" xmlns:a="{PPT_A_NS}" xmlns:r="{PPT_R_NS}" xmlns:p14="{PPT_P14_NS}">'
        f'<p:nvPicPr><p:cNvPr id="{shape_id}" name="background-music">'
        f'<a:hlinkClick r:id="" action="ppaction://media"/></p:cNvPr><p:cNvPicPr/>'
        f'<p:nvPr><a:audioFile r:link="{audio_rel}"/><p:extLst><p:ext uri="{PPT_P14_EXT_URI}">'
        f'<p14:media r:{"embed" if embedded else "link"}="{media_rel}"/></p:ext></p:extLst></p:nvPr></p:nvPicPr>'
        f'<p:blipFill><a:blip/><a:stretch><a:fillRect/></a:stretch></p:blipFill>'
        f'<p:spPr><a:xfrm><a:off x="-500" y="-500"/><a:ext cx="500" cy="500"/></a:xfrm>'
        f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>'
    )
    shape_tree.append(etree.fromstring(xml.encode("utf-8")))
    return shape_id


def patch_ppt_media(
    *,
    pptx_path: Path,
    videos: list[Path],
    durations: list[float],
    music_path: Path,
    automatic: bool,
    embedded: bool,
) -> None:
    with zipfile.ZipFile(pptx_path, "r") as archive:
        files = {item.filename: archive.read(item.filename) for item in archive.infolist()}
    audio_ext = music_path.suffix.lower().lstrip(".") or "mp3"
    if embedded:
        _ensure_content_type(files, "mp4", "video/mp4")
        audio_mime = {"mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4"}.get(audio_ext, "audio/mpeg")
        _ensure_content_type(files, audio_ext, audio_mime)
        files[f"ppt/media/background-music.{audio_ext}"] = music_path.read_bytes()

    for index, (video, duration) in enumerate(zip(videos, durations), start=1):
        slide_name = f"ppt/slides/slide{index}.xml"
        rels_name = f"ppt/slides/_rels/slide{index}.xml.rels"
        if slide_name not in files:
            raise ValueError(f"PPTX 缺少 {slide_name}")
        if embedded:
            video_target = f"../media/shot-{index:03d}.mp4"
            files[f"ppt/media/shot-{index:03d}.mp4"] = video.read_bytes()
        else:
            video_target = os.path.relpath(video, pptx_path.parent).replace(os.sep, "/")

        rels = etree.fromstring(files.get(
            rels_name,
            f'<Relationships xmlns="{PPT_RELS_NS}"/>'.encode("utf-8"),
        ))
        video_rel = _add_relationship(rels, PPT_VIDEO_REL, video_target, external=not embedded)
        media_rel = _add_relationship(rels, PPT_MEDIA_REL, video_target, external=not embedded)
        slide = etree.fromstring(files[slide_name])
        pictures = slide.findall(f".//{{{PPT_P_NS}}}pic")
        if not pictures:
            raise ValueError(f"{slide_name} 缺少 poster 图片")
        video_shape_id = _add_video_metadata(
            pictures[0], video_rel, media_rel, embedded=embedded
        )
        audio_shape_id: int | None = None
        if index == 1:
            music_target = (
                f"../media/background-music.{audio_ext}"
                if embedded
                else os.path.relpath(music_path, pptx_path.parent).replace(os.sep, "/")
            )
            audio_rel = _add_relationship(
                rels, PPT_AUDIO_REL, music_target, external=not embedded
            )
            audio_media_rel = _add_relationship(
                rels, PPT_MEDIA_REL, music_target, external=not embedded
            )
            audio_shape_id = _add_audio_picture(
                slide, audio_rel, audio_media_rel, embedded=embedded
            )

        for timing in slide.findall(f"{{{PPT_P_NS}}}timing"):
            slide.remove(timing)
        for transition in slide.findall(f"{{{PPT_P_NS}}}transition"):
            slide.remove(transition)
        transition = etree.Element(f"{{{PPT_P_NS}}}transition")
        transition.set("advClick", "1")
        if automatic:
            transition.set("advTm", str(max(1, round(duration * 1000))))
        etree.SubElement(transition, f"{{{PPT_P_NS}}}fade")
        child_names = [etree.QName(child).localname for child in slide]
        insert_at = child_names.index("cSld") + 1 if "cSld" in child_names else len(slide)
        slide.insert(insert_at, transition)
        slide.append(_media_timing_xml(video_shape_id, audio_shape_id, audio_loop=not automatic))
        files[slide_name] = etree.tostring(slide, xml_declaration=True, encoding="UTF-8", standalone=True)
        files[rels_name] = etree.tostring(rels, xml_declaration=True, encoding="UTF-8", standalone=True)

    temporary = pptx_path.with_name(f".{pptx_path.name}.tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    os.replace(temporary, pptx_path)


def dynamic_ppt_plan_issues(plan_path: Path) -> list[str]:
    """Machine-check lineage, media, timing modes, and subtitle variants."""

    issues: list[str] = []
    try:
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["dynamic_ppt_plan_invalid"]
    if payload.get("schema_version") != SCHEMA_VERSION:
        issues.append("dynamic_ppt_plan_version_mismatch")
    music = Path(str(payload.get("music_path") or ""))
    if not music.is_file() or payload.get("music_sha256") != file_sha256(music):
        issues.append("dynamic_ppt_music_stale")
    slides = payload.get("slides") if isinstance(payload.get("slides"), list) else []
    if not slides:
        issues.append("dynamic_ppt_slides_missing")
    for row in slides:
        index = row.get("slide_index", "?") if isinstance(row, dict) else "?"
        if not isinstance(row, dict):
            issues.append(f"dynamic_ppt_slide_invalid:{index}")
            continue
        for key, hash_key in (
            ("source_video_path", "source_video_sha256"),
            ("proxy_video_path", "proxy_video_sha256"),
            ("poster_path", "poster_sha256"),
        ):
            path = Path(str(row.get(key) or ""))
            if not path.is_file() or row.get(hash_key) != file_sha256(path):
                issues.append(f"dynamic_ppt_slide_stale:{index}:{key}")
        if float(row.get("duration_seconds") or 0) <= 0:
            issues.append(f"dynamic_ppt_duration_invalid:{index}")

    expected_keys = {
        "with_subtitles_auto", "without_subtitles_auto",
        "with_subtitles_control", "without_subtitles_control",
    }
    outputs = payload.get("outputs") if isinstance(payload.get("outputs"), dict) else {}
    for key in sorted(expected_keys):
        record = outputs.get(key) if isinstance(outputs.get(key), dict) else {}
        path = Path(str(record.get("path") or ""))
        if not path.is_file() or record.get("sha256") != file_sha256(path):
            issues.append(f"dynamic_ppt_output_stale:{key}")
            continue
        try:
            with zipfile.ZipFile(path) as archive:
                slide_names = [
                    name for name in archive.namelist()
                    if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
                ]
                if len(slide_names) != len(slides):
                    issues.append(f"dynamic_ppt_slide_count_mismatch:{key}")
                embedded = payload.get("media_mode") == "embedded_fallback"
                media = [name for name in archive.namelist() if name.startswith("ppt/media/shot-")]
                if embedded and len(media) != len(slides):
                    issues.append(f"dynamic_ppt_media_count_mismatch:{key}")
                if embedded and not any(name.startswith("ppt/media/background-music.") for name in archive.namelist()):
                    issues.append(f"dynamic_ppt_music_missing:{key}")
                for index in range(1, len(slides) + 1):
                    slide_name = f"ppt/slides/slide{index}.xml"
                    root = etree.fromstring(archive.read(slide_name))
                    if not root.xpath("//*[local-name()='videoFile']"):
                        issues.append(f"dynamic_ppt_video_object_missing:{key}:{index}")
                    rels_name = f"ppt/slides/_rels/slide{index}.xml.rels"
                    rels = etree.fromstring(archive.read(rels_name))
                    video_relationships = rels.xpath("//*[contains(@Type, '/video')]")
                    if not video_relationships:
                        issues.append(f"dynamic_ppt_video_relationship_missing:{key}:{index}")
                    elif not embedded and video_relationships[0].get("TargetMode") != "External":
                        issues.append(f"dynamic_ppt_video_relationship_not_external:{key}:{index}")
                    transitions = root.xpath("//*[local-name()='transition']")
                    advance = transitions[0].get("advTm") if transitions else None
                    if key.endswith("_auto") and advance is None:
                        issues.append(f"dynamic_ppt_auto_timing_missing:{key}:{index}")
                    if key.endswith("_control") and advance is not None:
                        issues.append(f"dynamic_ppt_control_timing_present:{key}:{index}")
                    if index == 1:
                        loops = root.xpath("//*[local-name()='audio']//*[local-name()='cTn']/@repeatCount")
                        if key.endswith("_control") and "indefinite" not in loops:
                            issues.append(f"dynamic_ppt_control_music_not_looping:{key}")
                        if key.endswith("_auto") and loops:
                            issues.append(f"dynamic_ppt_auto_music_looping:{key}")
        except (OSError, zipfile.BadZipFile, KeyError, etree.XMLSyntaxError):
            issues.append(f"dynamic_ppt_output_invalid:{key}")
    return issues


def ensure_node_builder(
    *,
    work_dir: Path,
    node_modules: Path,
    builder_source: Path,
) -> Path:
    build_dir = work_dir / "artifact_tool_builder"
    build_dir.mkdir(parents=True, exist_ok=True)
    builder = build_dir / "build_dynamic_story_ppt.mjs"
    shutil.copy2(builder_source, builder)
    link = build_dir / "node_modules"
    if link.is_symlink() and link.resolve() != node_modules.resolve():
        link.unlink()
    if not link.exists():
        link.symlink_to(node_modules, target_is_directory=True)
    return builder


def build_dynamic_story_ppts(
    *,
    source_plan_path: Path,
    music_path: Path,
    output_dir: Path,
    work_dir: Path,
    node: Path,
    node_modules: Path,
    builder_source: Path,
    subtitle_layout: Callable[[str], tuple[str, int, list[float]]] | None = None,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    embed_media: bool = False,
) -> dict[str, Path]:
    source_plan = load_source_plan(source_plan_path)
    music = music_path.expanduser().resolve()
    if not music.is_file() or music.stat().st_size <= 0:
        raise FileNotFoundError(f"PPT 背景音乐不存在或为空：{music}")
    output_dir.mkdir(parents=True, exist_ok=True)
    proxy_dir = output_dir / "PPT动态素材"
    proxy_dir.mkdir(parents=True, exist_ok=True)
    shared_music = proxy_dir / f"故事背景音乐{music.suffix.lower()}"
    shutil.copy2(music, shared_music)
    poster_dir = work_dir / "dynamic_ppt_posters"
    slides: list[dict[str, Any]] = []
    videos: list[Path] = []
    durations: list[float] = []
    for index, source_row in enumerate(source_plan["slides"], start=1):
        shot_id = str(source_row["shot_id"])
        source = Path(str(source_row["video_path"])).expanduser().resolve()
        stem = f"{index:03d}-{safe_name(shot_id)}"
        proxy = proxy_dir / f"{stem}.mp4"
        poster = poster_dir / f"{stem}.jpg"
        duration = create_proxy_and_poster(
            source=source, proxy=proxy, poster=poster, ffmpeg=ffmpeg, ffprobe=ffprobe
        )
        subtitle = str(source_row.get("subtitle") or "")
        clean, font_size, bbox = (
            subtitle_layout(subtitle) if subtitle_layout else (subtitle.strip(), 20, [0.0, 0.92, 1.0, 0.08])
        )
        slides.append({
            "slide_index": index,
            "shot_id": shot_id,
            "source_video_path": str(source),
            "source_video_sha256": file_sha256(source),
            "proxy_video_path": str(proxy.resolve()),
            "proxy_video_sha256": file_sha256(proxy),
            "poster_path": str(poster.resolve()),
            "poster_sha256": file_sha256(poster),
            "duration_seconds": duration,
            "subtitle": subtitle,
            "subtitle_layout": {"clean_text": clean, "font_size": font_size, "bbox": bbox},
            "semantic_card": bool(source_row.get("semantic_card", False)),
        })
        videos.append(proxy)
        durations.append(duration)

    resolved_plan_path = work_dir / "story_ppt_plan.json"
    resolved_plan = {
        "schema_version": SCHEMA_VERSION,
        "story_name": str(source_plan.get("story_name") or "").strip(),
        "media_mode": "embedded_fallback" if embed_media else "linked_shared_directory",
        "music_path": str(shared_music.resolve()),
        "music_sha256": file_sha256(shared_music),
        "slides": slides,
        "outputs": {},
    }
    resolved_plan_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_plan_path.write_text(json.dumps(resolved_plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    builder = ensure_node_builder(work_dir=work_dir, node_modules=node_modules, builder_source=builder_source)
    story_name = re.sub(r'[\\/:*?"<>|]+', "_", str(source_plan.get("story_name") or "story")).strip() or "story"
    variants = {
        "with_subtitles_auto": (True, True, f"故事PPT：{story_name}（含字幕·自动播放）.pptx"),
        "without_subtitles_auto": (False, True, f"故事PPT：{story_name}（无字幕·自动播放）.pptx"),
        "with_subtitles_control": (True, False, f"故事PPT：{story_name}（含字幕·人工控场）.pptx"),
        "without_subtitles_control": (False, False, f"故事PPT：{story_name}（无字幕·人工控场）.pptx"),
    }
    outputs: dict[str, Path] = {}
    for key, (with_subtitles, automatic, filename) in variants.items():
        output = output_dir / filename
        run_command([
            str(node), str(builder), "--plan", str(resolved_plan_path), "--output", str(output),
            "--subtitles", "yes" if with_subtitles else "no",
        ])
        patch_ppt_media(
            pptx_path=output,
            videos=videos,
            durations=durations,
            music_path=shared_music,
            automatic=automatic,
            embedded=embed_media,
        )
        outputs[key] = output

    resolved_plan["outputs"] = {
        key: {"path": str(path), "sha256": file_sha256(path)} for key, path in outputs.items()
    }
    resolved_plan_path.write_text(json.dumps(resolved_plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    qa_issues = dynamic_ppt_plan_issues(resolved_plan_path)
    qa_report = work_dir / "qa_dynamic_ppt_report.json"
    qa_report.write_text(json.dumps({
        "schema_version": "qa-dynamic-ppt-v1",
        "passed": not qa_issues,
        "issues": qa_issues,
        "story_ppt_plan_path": str(resolved_plan_path),
        "story_ppt_plan_sha256": file_sha256(resolved_plan_path),
        "output_sha256s": {
            key: file_sha256(path) for key, path in outputs.items()
        },
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if qa_issues:
        raise RuntimeError("动态 PPT 机器 QA 未通过：" + "；".join(qa_issues))
    outputs["plan"] = resolved_plan_path
    outputs["qa_report"] = qa_report
    outputs["media_dir"] = proxy_dir
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(description="从完整故事镜头生成四种 WPS 动态 PPT")
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--music", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--node", required=True, type=Path)
    parser.add_argument("--node-modules", required=True, type=Path)
    parser.add_argument(
        "--embed-media",
        action="store_true",
        help="WPS 相对媒体路径不稳定时使用：把视频和音乐内嵌进每个 PPTX",
    )
    parser.add_argument(
        "--builder",
        type=Path,
        default=Path(__file__).resolve().parent / "skills" / "story-full-auto" / "scripts" / "build_dynamic_story_ppt.mjs",
    )
    args = parser.parse_args()
    outputs = build_dynamic_story_ppts(
        source_plan_path=args.plan,
        music_path=args.music,
        output_dir=args.output_dir,
        work_dir=args.work_dir,
        node=args.node,
        node_modules=args.node_modules,
        builder_source=args.builder,
        embed_media=args.embed_media,
    )
    print(json.dumps({key: str(value) for key, value in outputs.items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
