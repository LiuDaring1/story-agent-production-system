from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class VideoProviderConfigError(ValueError):
    pass


@dataclass(frozen=True)
class VideoProviderAdapter:
    name: str
    runner: Path
    model: str
    base_url: str
    api_key_env: str
    estimated_cost_cny_per_clip: float

    def runner_args(self) -> list[str]:
        args: list[str] = []
        if self.base_url:
            args.extend(["--base-url", self.base_url])
        if self.model:
            args.extend(["--model", self.model])
        return args


def resolve_video_provider(config: dict[str, Any], root: Path, override: str = "") -> VideoProviderAdapter:
    video_api = config.get("video_api")
    if not isinstance(video_api, dict):
        raise VideoProviderConfigError("pipeline_config.json 缺少 video_api 配置")
    name = (override or str(video_api.get("provider", ""))).strip()
    if not name:
        raise VideoProviderConfigError("未配置图生视频 provider")
    adapters = video_api.get("adapters")
    if not isinstance(adapters, dict):
        adapters = {}
    raw = adapters.get(name)
    # Backward-compatible migration for the original Qingyun-only config.
    if not isinstance(raw, dict) and name == "qingyun_api":
        raw = {
            "runner": "run_image_video_jobs.py",
            "model": video_api.get("model", ""),
            "base_url": video_api.get("base_url", ""),
            "api_key_env": video_api.get("api_key_env", "QINGYUN_API_KEY"),
            "estimated_cost_cny_per_clip": video_api.get("estimated_cost_cny_per_clip", 0.0),
        }
    if not isinstance(raw, dict):
        raise VideoProviderConfigError(f"未找到 provider 适配器：{name}")
    runner_value = str(raw.get("runner", "")).strip()
    if not runner_value:
        raise VideoProviderConfigError(f"provider {name} 未配置 runner")
    runner = Path(runner_value).expanduser()
    if not runner.is_absolute():
        runner = root / runner
    if not runner.is_file():
        raise VideoProviderConfigError(f"provider {name} runner 不存在：{runner}")
    return VideoProviderAdapter(
        name=name,
        runner=runner,
        model=str(raw.get("model", "")).strip(),
        base_url=str(raw.get("base_url", "")).strip(),
        api_key_env=str(raw.get("api_key_env", "")).strip(),
        estimated_cost_cny_per_clip=float(raw.get("estimated_cost_cny_per_clip", 0.0)),
    )
