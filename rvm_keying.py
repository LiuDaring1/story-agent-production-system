from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any


RVM_BACKEND_VERSION = "story-rvm-onnx-keying/v1"
RVM_RECEIPT_SCHEMA_VERSION = "story-rvm-keying-receipt/v1"
OFFICIAL_RVM_MOBILENETV3_FP32_SHA256 = (
    "88d4531297118f595bf2fd60f6f566aec2e559393802d1f436c380f0cbbd2828"
)
OFFICIAL_RVM_MOBILENETV3_FP32_URL = (
    "https://github.com/PeterL1n/RobustVideoMatting/releases/download/v1.0.0/"
    "rvm_mobilenetv3_fp32.onnx"
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_runtime(runtime_path: Path | None):
    if runtime_path is not None:
        resolved = str(runtime_path.expanduser().resolve())
        if resolved not in sys.path:
            sys.path.insert(0, resolved)
    try:
        import numpy as np
        import onnxruntime as ort
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "RVM 后端缺少 numpy/onnxruntime/Pillow；请先安装并通过 rvm_runtime_path 指向运行目录"
        ) from exc
    return np, ort, Image


def validate_rvm_model(model_path: Path) -> str:
    model = model_path.expanduser()
    if not model.is_file():
        raise FileNotFoundError(f"RVM 模型不存在：{model}")
    digest = file_sha256(model)
    if digest != OFFICIAL_RVM_MOBILENETV3_FP32_SHA256:
        raise ValueError(
            "RVM 模型哈希不是已锁定的官方 MobileNetV3 FP32 权重："
            f"expected={OFFICIAL_RVM_MOBILENETV3_FP32_SHA256} actual={digest}"
        )
    return digest


def ensure_official_rvm_model(model_path: Path) -> Path:
    """Download the pinned official model once and verify it before use."""

    target = model_path.expanduser()
    if target.is_file():
        validate_rvm_model(target)
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="rvm-model-",
        suffix=".download",
        dir=target.parent,
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        urllib.request.urlretrieve(OFFICIAL_RVM_MOBILENETV3_FP32_URL, temporary_path)
        validate_rvm_model(temporary_path)
        temporary_path.replace(target)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return target


def _session(model_path: Path, runtime_path: Path | None):
    np, ort, Image = _load_runtime(runtime_path)
    validate_rvm_model(model_path)
    session = ort.InferenceSession(
        str(model_path.expanduser()),
        providers=["CPUExecutionProvider"],
    )
    return np, Image, session


def _infer_rgba(session: Any, np: Any, rgb: Any, recurrent: list[Any], downsample_ratio: float):
    source = rgb.astype(np.float32).transpose(2, 0, 1)[None] / 255.0
    fgr, pha, *next_recurrent = session.run(
        [],
        {
            "src": source,
            "r1i": recurrent[0],
            "r2i": recurrent[1],
            "r3i": recurrent[2],
            "r4i": recurrent[3],
            "downsample_ratio": np.asarray([downsample_ratio], dtype=np.float32),
        },
    )
    foreground = np.clip(fgr[0].transpose(1, 2, 0), 0.0, 1.0)
    alpha = np.clip(pha[0, 0], 0.0, 1.0)
    rgba = np.concatenate([foreground, alpha[..., None]], axis=2)
    return (rgba * 255.0 + 0.5).astype(np.uint8), list(next_recurrent)


def render_rvm_foreground_frame(
    source_frame: Path,
    output_path: Path,
    *,
    model_path: Path,
    runtime_path: Path | None = None,
    width: int | None = None,
    height: int | None = None,
    downsample_ratio: float = 0.4,
) -> Path:
    if not 0 < downsample_ratio <= 1:
        raise ValueError("RVM downsample_ratio 必须在 (0, 1] 范围内")
    np, Image, session = _session(model_path, runtime_path)
    with Image.open(source_frame).convert("RGB") as loaded:
        if width is not None or height is not None:
            if width is None or height is None or min(width, height) <= 0:
                raise ValueError("RVM 单帧缩放必须同时提供正数 width/height")
            loaded = loaded.resize((width, height), Image.Resampling.LANCZOS)
        rgb = np.asarray(loaded)
    recurrent = [np.zeros((1, 1, 1, 1), dtype=np.float32) for _ in range(4)]
    rgba, _ = _infer_rgba(session, np, rgb, recurrent, downsample_ratio)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba, "RGBA").save(output_path)
    return output_path


def _read_exact(stream: Any, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        block = stream.read(remaining)
        if not block:
            break
        chunks.append(block)
        remaining -= len(block)
    return b"".join(chunks)


def _probe_output(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,pix_fmt,r_frame_rate,nb_frames:stream_tags=alpha_mode:format=duration",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def render_rvm_foreground_video(
    source_video: Path,
    output_path: Path,
    receipt_path: Path,
    *,
    model_path: Path,
    runtime_path: Path | None = None,
    width: int = 1920,
    height: int = 1080,
    fps: int = 25,
    downsample_ratio: float = 0.4,
    start_seconds: float = 0.0,
    duration_seconds: float | None = None,
) -> Path:
    """Render one reusable temporal matte as VP9 WebM with alpha.

    Demo and Release consume this same hash-bound intermediate.  RVM therefore
    runs once per presenter source instead of once per downstream encoding.
    """

    source = source_video.expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"RVM 输入视频不存在：{source}")
    if min(width, height, fps) <= 0:
        raise ValueError("RVM width/height/fps 必须为正数")
    if not 0 < downsample_ratio <= 1:
        raise ValueError("RVM downsample_ratio 必须在 (0, 1] 范围内")
    if start_seconds < 0 or (duration_seconds is not None and duration_seconds <= 0):
        raise ValueError("RVM 时间范围无效")
    if output_path.suffix.lower() != ".webm":
        raise ValueError("RVM 透明缓存必须使用 .webm（VP9 Alpha）")
    np, _Image, session = _session(model_path, runtime_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    decoder_command = ["ffmpeg", "-v", "error"]
    if start_seconds:
        decoder_command.extend(["-ss", f"{start_seconds:.6f}"])
    decoder_command.extend(["-i", str(source)])
    if duration_seconds is not None:
        decoder_command.extend(["-t", f"{duration_seconds:.6f}"])
    decoder_command.extend(
        [
            "-an",
            "-vf",
            f"scale={width}:{height}:flags=lanczos,fps={fps}",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ]
    )
    encoder_command = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgba",
        "-s",
        f"{width}x{height}",
        "-r",
        str(fps),
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "libvpx-vp9",
        "-pix_fmt",
        "yuva420p",
        "-crf",
        "18",
        "-b:v",
        "0",
        "-deadline",
        "good",
        "-cpu-used",
        "3",
        "-row-mt",
        "1",
        str(output_path),
    ]
    decoder = subprocess.Popen(decoder_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    encoder = subprocess.Popen(encoder_command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    if decoder.stdout is None or encoder.stdin is None:
        raise RuntimeError("RVM 无法建立 FFmpeg 视频管道")
    frame_size = width * height * 3
    recurrent = [np.zeros((1, 1, 1, 1), dtype=np.float32) for _ in range(4)]
    frame_count = 0
    started = time.monotonic()
    try:
        while True:
            raw = _read_exact(decoder.stdout, frame_size)
            if not raw:
                break
            if len(raw) != frame_size:
                raise RuntimeError("RVM 解码器返回不完整视频帧")
            rgb = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
            rgba, recurrent = _infer_rgba(session, np, rgb, recurrent, downsample_ratio)
            encoder.stdin.write(rgba.tobytes())
            frame_count += 1
    except Exception:
        decoder.kill()
        encoder.kill()
        raise
    finally:
        decoder.stdout.close()
        encoder.stdin.close()
    decoder_stderr = decoder.stderr.read().decode("utf-8", errors="replace") if decoder.stderr else ""
    encoder_stderr = encoder.stderr.read().decode("utf-8", errors="replace") if encoder.stderr else ""
    decoder_code = decoder.wait()
    encoder_code = encoder.wait()
    if decoder_code != 0 or encoder_code != 0 or frame_count == 0:
        raise RuntimeError(
            "RVM 视频生成失败："
            f"decoder={decoder_code} encoder={encoder_code} frames={frame_count} "
            f"decoder_stderr={decoder_stderr[-500:]} encoder_stderr={encoder_stderr[-500:]}"
        )
    elapsed = time.monotonic() - started
    probe = _probe_output(output_path)
    streams = probe.get("streams") if isinstance(probe, dict) else None
    first_stream = streams[0] if isinstance(streams, list) and streams else {}
    tags = first_stream.get("tags") if isinstance(first_stream, dict) else {}
    alpha_verified = isinstance(tags, dict) and str(tags.get("alpha_mode")) == "1"
    if not alpha_verified:
        raise RuntimeError("RVM VP9 输出没有 alpha_mode=1，拒绝写入完成回执")
    receipt = {
        "schema_version": RVM_RECEIPT_SCHEMA_VERSION,
        "backend_version": RVM_BACKEND_VERSION,
        "status": "complete",
        "source_video": str(source),
        "source_sha256": file_sha256(source),
        "model_path": str(model_path.expanduser()),
        "model_sha256": validate_rvm_model(model_path),
        "official_model_url": OFFICIAL_RVM_MOBILENETV3_FP32_URL,
        "runtime_path": str(runtime_path.expanduser()) if runtime_path is not None else "environment",
        "provider": "CPUExecutionProvider",
        "width": width,
        "height": height,
        "fps": fps,
        "downsample_ratio": downsample_ratio,
        "start_seconds": start_seconds,
        "requested_duration_seconds": duration_seconds,
        "frame_count": frame_count,
        "elapsed_seconds": round(elapsed, 3),
        "output_video": str(output_path),
        "output_sha256": file_sha256(output_path),
        "output_probe": probe,
        "alpha_channel_verified": True,
        "temporal_recurrence_used": True,
        "downstream_reuse_required": True,
    }
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path


def rvm_receipt_issues(
    receipt_path: Path,
    *,
    expected_source: Path | None = None,
    expected_output: Path | None = None,
) -> list[str]:
    if not receipt_path.is_file():
        return ["rvm_receipt_missing"]
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["rvm_receipt_invalid_json"]
    if not isinstance(payload, dict):
        return ["rvm_receipt_invalid_object"]
    issues: list[str] = []
    if payload.get("schema_version") != RVM_RECEIPT_SCHEMA_VERSION or payload.get("status") != "complete":
        issues.append("rvm_receipt_schema_or_status_invalid")
    if payload.get("backend_version") != RVM_BACKEND_VERSION:
        issues.append("rvm_backend_version_mismatch")
    if payload.get("model_sha256") != OFFICIAL_RVM_MOBILENETV3_FP32_SHA256:
        issues.append("rvm_model_sha256_mismatch")
    if payload.get("temporal_recurrence_used") is not True:
        issues.append("rvm_temporal_recurrence_missing")
    if payload.get("alpha_channel_verified") is not True:
        issues.append("rvm_alpha_channel_unverified")
    if int(payload.get("frame_count") or 0) <= 0:
        issues.append("rvm_frame_count_invalid")
    output = expected_output or Path(str(payload.get("output_video") or ""))
    if not output.is_file():
        issues.append("rvm_output_missing")
    elif payload.get("output_sha256") != file_sha256(output):
        issues.append("rvm_output_sha256_mismatch")
    if expected_output is not None and Path(str(payload.get("output_video") or "")).resolve() != expected_output.resolve():
        issues.append("rvm_output_path_mismatch")
    if expected_source is not None:
        if not expected_source.is_file():
            issues.append("rvm_source_missing")
        elif payload.get("source_sha256") != file_sha256(expected_source):
            issues.append("rvm_source_sha256_mismatch")
    return issues


def ensure_rvm_foreground_video(
    source_video: Path,
    output_path: Path,
    receipt_path: Path,
    **render_options: Any,
) -> Path:
    issues = rvm_receipt_issues(
        receipt_path,
        expected_source=source_video,
        expected_output=output_path,
    )
    if not issues:
        return output_path
    return render_rvm_foreground_video(
        source_video,
        output_path,
        receipt_path,
        **render_options,
    )


__all__ = [
    "OFFICIAL_RVM_MOBILENETV3_FP32_SHA256",
    "OFFICIAL_RVM_MOBILENETV3_FP32_URL",
    "RVM_BACKEND_VERSION",
    "RVM_RECEIPT_SCHEMA_VERSION",
    "file_sha256",
    "ensure_official_rvm_model",
    "ensure_rvm_foreground_video",
    "render_rvm_foreground_frame",
    "render_rvm_foreground_video",
    "rvm_receipt_issues",
    "validate_rvm_model",
]
