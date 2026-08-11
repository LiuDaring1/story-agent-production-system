from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any


class VideoProviderConfigError(ValueError):
    pass


def resolve_row_generation_seconds(
    row: dict[str, Any],
    *,
    model: str,
    fallback_seconds: str | int | float,
    min_seconds: float | None = None,
    max_seconds: float | None = None,
) -> str:
    """Resolve a per-row whole-second request for Grok Video 1.5.

    The row's ``generation_duration`` wins over ``duration`` and the CLI/config
    fallback.  Older providers deliberately keep their historical global
    ``--seconds`` behavior, so this helper returns the fallback unchanged for
    non-Grok models.
    """

    if str(model).strip().lower() != "grok-video-1.5":
        return str(fallback_seconds)
    raw = _first_nonempty_row_value(row, "generation_duration", "duration")
    value = fallback_seconds if raw is None else raw
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Grok Video 1.5 每镜 seconds 不是数字：{value!r}") from exc
    if not math.isfinite(numeric):
        raise ValueError(f"Grok Video 1.5 每镜 seconds 不是有限数字：{value!r}")
    minimum = max(1, math.ceil(float(1 if min_seconds is None else min_seconds)))
    maximum = math.floor(float(15 if max_seconds is None else max_seconds))
    if maximum < minimum:
        raise ValueError(f"Grok Video 1.5 seconds 范围无效：{minimum}–{maximum}")
    seconds = max(minimum, min(maximum, math.ceil(numeric)))
    return str(seconds)


def _first_nonempty_row_value(row: dict[str, Any], *keys: str) -> Any:
    """Return the first non-blank CSV value, including numeric zero."""

    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


@dataclass(frozen=True)
class VideoProviderAdapter:
    name: str
    runner: Path
    model: str
    base_url: str
    api_key_env: str
    estimated_cost_cny_per_clip: float
    estimated_cost_cny_per_second: float = 0.0
    default_seconds: float | None = None
    min_seconds: float | None = None
    max_seconds: float | None = None
    default_resolution: str = ""
    default_ratio: str = ""

    def estimate_cost(self, seconds: float | None = None) -> float:
        """Return a conservative CNY estimate for one generated clip.

        Existing providers only expose a per-clip estimate.  New providers may
        expose a per-second rate as well; when a duration is supplied that rate
        is used, otherwise the configured per-clip estimate remains the
        backward-compatible fallback.  ``default_seconds`` is deliberately
        used only when no legacy per-clip estimate is present.
        """

        rate = float(self.estimated_cost_cny_per_second or 0.0)
        if rate > 0 and seconds is not None:
            value = float(seconds)
            if value < 0:
                raise ValueError("生成时长不能为负数")
            return round(rate * value, 4)
        if self.estimated_cost_cny_per_clip:
            return round(float(self.estimated_cost_cny_per_clip), 4)
        if rate > 0 and self.default_seconds is not None:
            return round(rate * float(self.default_seconds), 4)
        return 0.0

    def runner_args(self) -> list[str]:
        args: list[str] = []
        if self.base_url:
            args.extend(["--base-url", self.base_url])
        if self.model:
            args.extend(["--model", self.model])
        if self.api_key_env:
            args.extend(["--api-key-env", self.api_key_env])
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
        estimated_cost_cny_per_second=float(raw.get("estimated_cost_cny_per_second", 0.0)),
        default_seconds=_optional_float(raw.get("default_seconds")),
        min_seconds=_optional_float(raw.get("min_seconds")),
        max_seconds=_optional_float(raw.get("max_seconds")),
        default_resolution=str(raw.get("default_resolution", "")).strip(),
        default_ratio=str(raw.get("default_ratio", "")).strip(),
    )


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)
