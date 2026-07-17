from __future__ import annotations

import json
import mimetypes
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from story_video_synthesizer.volcengine_video import CreateTaskResult, QueryTaskResult


DEFAULT_BASE_URL = "https://toapis.com/v1"
DEFAULT_MODEL = "grok-video-3"
DEFAULT_USER_AGENT = "curl/8.7.1"


def build_toapis_task_body(
    *,
    model: str,
    prompt: str,
    image_url: str,
    ratio: str = "16:9",
    seconds: str = "10",
    resolution: str = "720p",
    extra_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt.strip(),
        "images": [image_url],
        "seconds": str(seconds),
        "resolution": resolution.lower(),
        "aspect_ratio": ratio,
    }
    if extra_body:
        body.update(extra_body)
    return body


def extract_toapis_video_url(payload: dict[str, Any]) -> str | None:
    direct = payload.get("video_url") or payload.get("url")
    if isinstance(direct, str) and direct.startswith(("http://", "https://")):
        return direct
    result = payload.get("result")
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    found = extract_toapis_video_url(item)
                    if found:
                        return found
    data = payload.get("data")
    if isinstance(data, dict):
        return extract_toapis_video_url(data)
    return None


class ToAPIsVideoClient:
    """Small provider client for ToAPIs' upload + asynchronous Grok video APIs."""

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL, timeout: int = 120) -> None:
        if not api_key:
            raise ValueError("缺少 API Key，请设置 TOAPIS_API_KEY 环境变量。")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout

    def upload_image(self, image_path: Path) -> str:
        if not image_path.is_file():
            raise FileNotFoundError(f"参考图片不存在：{image_path}")
        if image_path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError(f"ToAPIs 上传图片不能超过 10MB：{image_path}")
        boundary = "----story-agent-" + uuid.uuid4().hex
        mime_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
        body = b"".join(
            [
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="file"; filename="{image_path.name}"\r\n'
                ).encode("utf-8"),
                f"Content-Type: {mime_type}\r\n\r\n".encode(),
                image_path.read_bytes(),
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        payload = self._request(
            "POST",
            "uploads/images",
            body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        data = payload.get("data")
        url = data.get("url") if isinstance(data, dict) else None
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise RuntimeError(f"ToAPIs 图片上传成功但没有返回 URL：{payload}")
        return url

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
        seconds: str = "10",
        size: str = "720P",
        camera_fixed: bool = False,
        watermark: bool = False,
        extra_body: dict[str, Any] | None = None,
    ) -> CreateTaskResult:
        del duration, frames, role, parameter_style, camera_fixed, watermark
        reference_url = image_url or (self.upload_image(image_path) if image_path else "")
        if not reference_url:
            raise ValueError("ToAPIs 图生视频需要参考图片。")
        client_business_id = str((extra_body or {}).get("client_business_id") or "").strip()
        if client_business_id:
            try:
                existing = self.get_task(client_business_id)
            except RuntimeError as exc:
                # ToAPIs currently reports a missing idempotency/business ID as
                # HTTP 400 + task_not_exist rather than a conventional 404.
                message = str(exc)
                if "HTTP 404" not in message and "task_not_exist" not in message:
                    raise
            else:
                existing_id = existing.raw.get("id") or existing.task_id
                return CreateTaskResult(task_id=str(existing_id), raw=existing.raw)
        body = build_toapis_task_body(
            model=model,
            prompt=prompt,
            image_url=reference_url,
            ratio=ratio,
            seconds=seconds,
            resolution=resolution or size,
            extra_body=extra_body,
        )
        payload = self._request("POST", "videos/generations", json.dumps(body, ensure_ascii=False).encode())
        task_id = payload.get("id") or payload.get("task_id")
        if not task_id:
            raise RuntimeError(f"ToAPIs 创建任务成功但没有返回任务 ID：{payload}")
        return CreateTaskResult(task_id=str(task_id), raw=payload)

    def get_task(self, task_id: str) -> QueryTaskResult:
        payload = self._request("GET", f"videos/generations/{task_id}", None)
        status = str(payload.get("status") or "")
        error_obj = payload.get("error")
        error = json.dumps(error_obj, ensure_ascii=False) if error_obj else None
        return QueryTaskResult(
            task_id=task_id,
            status=status,
            video_url=extract_toapis_video_url(payload),
            error=error,
            raw=payload,
        )

    def download(self, url: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        request = Request(url, headers={"User-Agent": "story-agent/1.0"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                output_path.write_bytes(response.read())
            return
        except Exception as first_error:
            process = subprocess.run(
                ["curl", "-L", "--fail", "--retry", "5", "--output", str(output_path), url],
                text=True,
                capture_output=True,
            )
            if process.returncode != 0:
                raise RuntimeError(f"ToAPIs 视频下载失败：{process.stderr.strip()}") from first_error

    def download_task_video(self, task_id: str, output_path: Path) -> None:
        result = self.get_task(task_id)
        if not result.video_url:
            raise RuntimeError(f"ToAPIs 任务 {task_id} 没有返回视频 URL。")
        self.download(result.video_url, output_path)

    def _request(
        self,
        method: str,
        path: str,
        data: bytes | None,
        *,
        content_type: str = "application/json",
    ) -> dict[str, Any]:
        url = self._url(path)
        last_error: Exception | None = None
        for attempt in range(3):
            request = Request(
                url,
                data=data,
                method=method,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": content_type,
                    "Accept": "application/json",
                    # ToAPIs/Cloudflare rejects Python urllib's default signature
                    # with Error 1010 before authentication reaches the API.
                    "User-Agent": DEFAULT_USER_AGENT,
                },
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"ToAPIs HTTP {exc.code}：{detail}") from exc
            except URLError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2**attempt)
        raise RuntimeError(f"ToAPIs 请求失败：{last_error}") from last_error

    def _url(self, path: str) -> str:
        parsed = urlparse(self.base_url)
        clean = path.lstrip("/")
        if parsed.path.rstrip("/").endswith("/v1") and clean.startswith("v1/"):
            clean = clean.removeprefix("v1/")
        return urljoin(self.base_url, clean)
