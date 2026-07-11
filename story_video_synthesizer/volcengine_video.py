from __future__ import annotations

import base64
import csv
import json
import mimetypes
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "https://api.qingyuntop.top/v1"
DEFAULT_MODEL = "grok-video-3-10s"
DEFAULT_SECONDS = "10"
DEFAULT_SIZE = "720P"
VIDEO_CREATE_HOSTS = {"api.qingyuntop.top", "yunwu.ai"}


TERMINAL_STATUSES = {"succeeded", "success", "completed", "done", "failed", "cancelled", "canceled", "expired"}
SUCCESS_STATUSES = {"succeeded", "success", "completed", "done"}


@dataclass(frozen=True)
class CreateTaskResult:
    task_id: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class QueryTaskResult:
    task_id: str
    status: str
    video_url: str | None
    error: str | None
    raw: dict[str, Any]


class QingyunVideoClient:
    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL, timeout: int = 120) -> None:
        if not api_key:
            raise ValueError("缺少 API Key，请设置 QINGYUN_API_KEY 环境变量或传入 --api-key。")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self.provider = "video_create" if urlparse(self.base_url).netloc in VIDEO_CREATE_HOSTS else "qingyun_legacy"

    def create_task(
        self,
        *,
        model: str,
        prompt: str,
        image_path: Path | None = None,
        image_url: str | None = None,
        ratio: str = "16:9",
        duration: float = 10.0,
        resolution: str | None = None,
        frames: int | None = None,
        role: str = "first_frame",
        parameter_style: str = "prompt",
        seconds: str = DEFAULT_SECONDS,
        size: str = DEFAULT_SIZE,
        camera_fixed: bool = False,
        watermark: bool = False,
        extra_body: dict[str, Any] | None = None,
    ) -> CreateTaskResult:
        if image_url:
            raise ValueError("青云视频接口当前按本地图片文件上传 input_reference，不支持 image_url。")
        if image_path is None:
            raise ValueError("青云视频接口需要 input_reference 图片文件。")
        body = build_create_task_body(
            model=model,
            prompt=prompt,
            image_path=image_path,
            image_url=image_url,
            ratio=ratio,
            duration=duration,
            resolution=resolution,
            frames=frames,
            role=role,
            parameter_style=parameter_style,
            seconds=seconds,
            size=size,
            camera_fixed=camera_fixed,
            watermark=watermark,
            extra_body=extra_body,
        )
        if self.provider == "video_create":
            payload = self._request_json("POST", self._api_path("video/create"), adapt_body_for_video_create(body, image_path))
        else:
            payload = self._request_multipart("POST", self._api_path("videos"), body, image_path)
        task_id = extract_task_id(payload)
        if not task_id:
            raise RuntimeError(f"创建任务成功但没有返回任务 ID：{payload}")
        return CreateTaskResult(task_id=task_id, raw=payload)

    def get_task(self, task_id: str) -> QueryTaskResult:
        if self.provider == "video_create":
            payload = self._request_json("GET", self._api_path(f"video/query?id={quote(task_id)}"), None)
        else:
            payload = self._request_json("GET", self._api_path(f"videos/{task_id}"), None)
        payload_obj = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        status = str(payload_obj.get("status") or payload.get("status") or "")
        video_url = extract_video_url(payload)
        error_obj = payload_obj.get("error") or payload.get("error")
        error = json.dumps(error_obj, ensure_ascii=False) if error_obj else None
        return QueryTaskResult(task_id=task_id, status=status, video_url=video_url, error=error, raw=payload)

    def download(self, url: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        request = Request(url, headers={"User-Agent": "story-video-synthesizer/1.0"})
        last_error: Exception | None = None
        for _attempt in range(3):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    output_path.write_bytes(response.read())
                return
            except Exception as exc:
                last_error = exc
                time.sleep(1)

        process = subprocess.run(
            [
                "curl",
                "-L",
                "--fail",
                "--retry",
                "5",
                "--retry-delay",
                "2",
                "--output",
                str(output_path),
                url,
            ],
            text=True,
            capture_output=True,
        )
        if process.returncode != 0:
            detail = process.stderr.strip() or process.stdout.strip()
            raise RuntimeError(f"下载失败：{detail}") from last_error

    def download_task_video(self, task_id: str, output_path: Path) -> None:
        if self.provider == "video_create":
            raise RuntimeError("video/create 接口需要等待查询结果返回 video_url 后再下载。")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        url = urljoin(self.base_url, self._api_path(f"videos/{task_id}/content"))
        request = Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "video/mp4,application/json",
                "User-Agent": "story-video-synthesizer/1.0",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                content_type = response.headers.get("Content-Type", "")
                data = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(_format_http_error(exc.code, url, detail)) from exc

        if "application/json" in content_type:
            payload = json.loads(data.decode("utf-8"))
            video_url = extract_video_url(payload)
            if not video_url:
                raise RuntimeError(f"下载接口没有返回视频内容或下载地址：{payload}")
            self.download(video_url, output_path)
            return
        output_path.write_bytes(data)

    def _request_json(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        url = urljoin(self.base_url, path)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        last_error: Exception | None = None
        for attempt in range(3):
            request = Request(
                url,
                data=data,
                method=method,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                friendly = _format_http_error(exc.code, url, detail)
                raise RuntimeError(friendly) from exc
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2)
                    continue
        raise RuntimeError(f"请求失败 {url}: {last_error}") from last_error

    def _request_multipart(self, method: str, path: str, fields: dict[str, Any], image_path: Path) -> dict[str, Any]:
        url = urljoin(self.base_url, path)
        boundary = "----story-video-" + uuid.uuid4().hex
        body = encode_multipart_form(fields, image_path, boundary)
        request = Request(
            url,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(_format_http_error(exc.code, url, detail)) from exc
        except URLError as exc:
            raise RuntimeError(f"请求失败 {url}: {exc}") from exc

    def _api_path(self, path: str) -> str:
        parsed = urlparse(self.base_url)
        base_has_v1 = parsed.path.rstrip("/").endswith("/v1")
        clean_path = path.lstrip("/")
        if base_has_v1 and clean_path.startswith("v1/"):
            return clean_path.removeprefix("v1/")
        if not base_has_v1 and not clean_path.startswith("v1/"):
            return f"v1/{clean_path}"
        return clean_path


def image_to_data_url(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"图片不存在：{path}")
    mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def adapt_body_for_video_create(body: dict[str, Any], image_path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": body.get("model"),
        "prompt": body.get("prompt"),
        "aspect_ratio": body.get("aspect_ratio", "16:9"),
        "size": body.get("size", DEFAULT_SIZE),
        "images": [image_to_data_url(image_path)],
    }
    if body.get("seconds"):
        payload["seconds"] = body["seconds"]
    return {key: value for key, value in payload.items() if value is not None and value != ""}


def extract_task_id(payload: dict[str, Any]) -> str:
    for source in (payload, payload.get("data")):
        if not isinstance(source, dict):
            continue
        value = source.get("id") or source.get("task_id") or source.get("taskId")
        if value:
            return str(value)
    return ""


def _format_http_error(status_code: int, url: str, detail: str) -> str:
    if "no available platform found for model" in detail or "no usable platform found" in detail:
        return (
            "视频 API 调用失败：青云当前分组没有可用的 Grok 视频上游，"
            "请稍后重试、切换到有 grok-video-3-10s 权限/余额的分组，或减少批量重跑数量。"
            f" 原始响应：HTTP {status_code} {url}: {detail}"
        )
    try:
        payload = json.loads(detail)
    except json.JSONDecodeError:
        return f"HTTP {status_code} {url}: {detail}"

    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return f"HTTP {status_code} {url}: {detail}"

    code = str(error.get("code") or "")
    message = str(error.get("message") or "")
    request_id = ""
    if "Request id:" in message:
        request_id = message.split("Request id:", 1)[1].strip()
        message = message.split("Request id:", 1)[0].strip()

    if code == "AccountOverdueError":
        suffix = f" 请求 ID：{request_id}" if request_id else ""
        return f"视频 API 调用失败：账号余额不足或存在欠费，请充值/处理账单后重试。{suffix}"
    if code == "local_quota_not_enough":
        return (
            "视频 API 调用失败：当前 API 分组或 Grok 上游负载已饱和，请稍后重试，"
            "或减少本轮批量提交数量。"
        )
    if code:
        return f"视频 API 调用失败：{code}。{message or detail}"
    return f"HTTP {status_code} {url}: {detail}"


def build_create_task_body(
    *,
    model: str,
    prompt: str,
    image_path: Path | None = None,
    image_url: str | None = None,
    ratio: str = "16:9",
    duration: float = 10.0,
    resolution: str | None = None,
    frames: int | None = None,
    role: str = "first_frame",
    parameter_style: str = "prompt",
    seconds: str = DEFAULT_SECONDS,
    size: str = DEFAULT_SIZE,
    camera_fixed: bool = False,
    watermark: bool = False,
    extra_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt.strip(),
        "seconds": str(seconds),
        "aspect_ratio": normalize_aspect_ratio(ratio),
        "size": normalize_size(size),
        "watermark": str(watermark).lower(),
    }
    if image_path:
        body["input_reference"] = str(image_path)
    if image_url:
        body["input_reference"] = image_url
    if extra_body:
        body.update(extra_body)
    return body


def normalize_size(value: str) -> str:
    value = value.strip()
    if value.lower() == "720p":
        return "720P"
    if value.lower() == "1080p":
        return "1080P"
    return value


def normalize_aspect_ratio(value: str) -> str:
    value = value.strip()
    if value in {"16x9", "16/9"}:
        return "16:9"
    if value in {"9x16", "9/16"}:
        return "9:16"
    return value


def encode_multipart_form(fields: dict[str, Any], image_path: Path, boundary: str) -> bytes:
    if not image_path.exists():
        raise FileNotFoundError(f"图片不存在：{image_path}")
    chunks: list[bytes] = []
    for name, value in fields.items():
        if name == "input_reference":
            continue
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )

    mime_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
    chunks.extend(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            (
                f'Content-Disposition: form-data; name="input_reference"; '
                f'filename="{image_path.name}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"),
            image_path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    return b"".join(chunks)


def append_prompt_options(
    prompt: str,
    *,
    resolution: str | None,
    duration: float,
    frames: int | None,
    camera_fixed: bool,
    watermark: bool,
) -> str:
    parts = [prompt.strip()]
    if resolution:
        parts.extend(["--resolution", resolution])
    if frames is not None:
        parts.extend(["--frames", str(frames)])
    else:
        parts.extend(["--duration", format_seconds(duration)])
    parts.extend(["--camerafixed", str(camera_fixed).lower(), "--watermark", str(watermark).lower()])
    return " ".join(part for part in parts if part)


def format_seconds(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def duration_to_supported_frames(duration: float, *, fps: int = 24, min_frames: int = 29, max_frames: int = 289) -> int:
    target = round(duration * fps)
    target = max(min_frames, min(max_frames, target))
    candidates = list(range(min_frames, max_frames + 1, 4))
    return min(candidates, key=lambda value: (abs(value - target), value))


def extract_video_url(payload: dict[str, Any]) -> str | None:
    direct_keys = ("video_url", "url")
    for key in direct_keys:
        value = payload.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value

    content = payload.get("content")
    if isinstance(content, dict):
        return extract_video_url(content)
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            for key in direct_keys:
                value = item.get(key)
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    return value

    result = payload.get("result") or payload.get("output")
    if isinstance(result, dict):
        return extract_video_url(result)
    data = payload.get("data")
    if isinstance(data, dict):
        return extract_video_url(data)
    return None


def read_jobs_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def write_jobs_csv(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def poll_until_done(
    client: QingyunVideoClient,
    task_id: str,
    *,
    interval: int = 10,
    max_wait_seconds: int = 1800,
    max_poll_errors: int = 12,
) -> QueryTaskResult:
    deadline = time.monotonic() + max_wait_seconds
    last_result: QueryTaskResult | None = None
    consecutive_errors = 0
    while time.monotonic() < deadline:
        try:
            result = client.get_task(task_id)
        except Exception:
            consecutive_errors += 1
            if consecutive_errors > max_poll_errors:
                raise
            time.sleep(min(interval * consecutive_errors, 60))
            continue
        consecutive_errors = 0
        last_result = result
        if result.status in TERMINAL_STATUSES:
            return result
        time.sleep(interval)
    if last_result is not None:
        return last_result
    raise TimeoutError(f"等待任务超时：{task_id}")


VolcengineVideoClient = QingyunVideoClient
