from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from story_module_adapters import (
    ExistingVideoGeneratorAdapter,
    MockKeyerAdapter,
    MockVideoGeneratorAdapter,
    ProductionKeyerAdapter,
)
from story_module_ports import KeyerPort, VideoGeneratorPort, module_payload
from video_provider_adapter import resolve_video_provider


ROOT = Path(__file__).resolve().parent
MODULE_PROFILE_ENV = "STORY_MODULE_PROFILE"
MODULE_PROFILE_REQUIRED_ENV = "STORY_MODULE_PROFILE_REQUIRED"
PRODUCTION_PROFILE = "production-default"
MODULE_PROFILE_ALIASES = {"default": PRODUCTION_PROFILE, "production": PRODUCTION_PROFILE}
ALLOWED_MODULE_PROFILES = frozenset({PRODUCTION_PROFILE, "mock-video", "mock-keyer"})


class ModuleRegistry:
    """Explicit adapter registry; it deliberately performs no smart routing."""

    def __init__(self, *, profile_name: str = "") -> None:
        self._ports: dict[str, object] = {}
        self.profile_name = profile_name

    def register(self, port_name: str, adapter: object) -> None:
        identity = getattr(adapter, "identity", None)
        if identity is None or identity.port_name != port_name:
            raise ValueError(f"adapter identity does not match port {port_name}")
        self._ports[port_name] = adapter

    def get(self, port_name: str) -> object:
        try:
            return self._ports[port_name]
        except KeyError as exc:
            raise KeyError(f"module port is not registered: {port_name}") from exc

    def video_generator(self) -> VideoGeneratorPort:
        return self.get("video_generator")  # type: ignore[return-value]

    def keyer(self) -> KeyerPort:
        return self.get("keyer")  # type: ignore[return-value]

    def describe(self, port_name: str) -> dict[str, Any]:
        adapter = self.get(port_name)
        return {
            "identity": module_payload("identity", adapter.identity),
            "capabilities": module_payload("capabilities", adapter.capabilities),
        }

    def list_descriptions(self) -> dict[str, dict[str, Any]]:
        return {name: self.describe(name) for name in sorted(self._ports)}

    def selection_profile(self) -> str:
        if self.profile_name not in ALLOWED_MODULE_PROFILES:
            raise RuntimeError("module registry has no allowlisted subprocess selection profile")
        return self.profile_name


def load_pipeline_config(root: Path = ROOT) -> dict[str, Any]:
    return json.loads((root / "pipeline_config.json").read_text(encoding="utf-8"))


def build_default_registry(
    config: dict[str, Any] | None = None,
    root: Path = ROOT,
    *,
    video_provider_override: str = "",
) -> ModuleRegistry:
    registry = ModuleRegistry(profile_name=PRODUCTION_PROFILE)
    provider = resolve_video_provider(config or load_pipeline_config(root), root, video_provider_override)
    registry.register("video_generator", ExistingVideoGeneratorAdapter(provider))
    registry.register("keyer", ProductionKeyerAdapter())
    return registry


def normalize_module_profile(profile: str) -> str:
    normalized = MODULE_PROFILE_ALIASES.get(profile.strip().lower(), profile.strip().lower())
    if normalized not in ALLOWED_MODULE_PROFILES:
        raise ValueError(f"unsupported module profile: {profile or '<missing>'}")
    return normalized


def resolve_module_profile(explicit: str = "") -> str:
    required_raw = os.environ.get(MODULE_PROFILE_REQUIRED_ENV, "").strip()
    selected_raw = explicit.strip() or os.environ.get(MODULE_PROFILE_ENV, "").strip()
    if required_raw and not selected_raw:
        raise RuntimeError("required module profile selection is missing; refusing production fallback")
    selected = normalize_module_profile(selected_raw or PRODUCTION_PROFILE)
    if required_raw and selected != normalize_module_profile(required_raw):
        raise RuntimeError("module profile does not match required parent selection")
    return selected


def export_module_profile(profile: str) -> str:
    selected = normalize_module_profile(profile)
    os.environ[MODULE_PROFILE_ENV] = selected
    os.environ[MODULE_PROFILE_REQUIRED_ENV] = selected
    return selected


def build_registry_for_profile(
    profile: str = "",
    config: dict[str, Any] | None = None,
    root: Path = ROOT,
    *,
    video_provider_override: str = "",
) -> ModuleRegistry:
    selected = resolve_module_profile(profile)
    registry = ModuleRegistry(profile_name=selected)
    if selected == "mock-video":
        registry.register("video_generator", MockVideoGeneratorAdapter())
    else:
        provider = resolve_video_provider(config or load_pipeline_config(root), root, video_provider_override)
        registry.register("video_generator", ExistingVideoGeneratorAdapter(provider))
    registry.register("keyer", MockKeyerAdapter() if selected == "mock-keyer" else ProductionKeyerAdapter())
    return registry


def build_keyer_registry(profile: str = "") -> ModuleRegistry:
    """Build the local-only registry without reading video credentials/config."""

    selected = resolve_module_profile(profile)
    registry = ModuleRegistry(profile_name=selected)
    registry.register("keyer", MockKeyerAdapter() if selected == "mock-keyer" else ProductionKeyerAdapter())
    return registry


def main() -> None:
    parser = argparse.ArgumentParser(description="Story Agent module port diagnostics (read-only)")
    parser.add_argument("--profile", default="", help="Allowlisted adapter selection profile")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List selected module adapters and capabilities")
    describe = sub.add_parser("describe", help="Describe one selected module adapter")
    describe.add_argument("port_name", choices=["video_generator", "keyer"])
    args = parser.parse_args()
    registry = build_registry_for_profile(args.profile)
    payload = registry.list_descriptions() if args.command == "list" else registry.describe(args.port_name)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
