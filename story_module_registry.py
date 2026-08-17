from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from story_module_adapters import ExistingVideoGeneratorAdapter, ProductionKeyerAdapter
from story_module_ports import KeyerPort, VideoGeneratorPort, module_payload
from video_provider_adapter import resolve_video_provider


ROOT = Path(__file__).resolve().parent


class ModuleRegistry:
    """Explicit adapter registry; it deliberately performs no smart routing."""

    def __init__(self) -> None:
        self._ports: dict[str, object] = {}

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


def load_pipeline_config(root: Path = ROOT) -> dict[str, Any]:
    return json.loads((root / "pipeline_config.json").read_text(encoding="utf-8"))


def build_default_registry(
    config: dict[str, Any] | None = None,
    root: Path = ROOT,
    *,
    video_provider_override: str = "",
) -> ModuleRegistry:
    registry = ModuleRegistry()
    provider = resolve_video_provider(config or load_pipeline_config(root), root, video_provider_override)
    registry.register("video_generator", ExistingVideoGeneratorAdapter(provider))
    registry.register("keyer", ProductionKeyerAdapter())
    return registry


def build_keyer_registry() -> ModuleRegistry:
    """Build the local-only registry without reading video credentials/config."""

    registry = ModuleRegistry()
    registry.register("keyer", ProductionKeyerAdapter())
    return registry


def main() -> None:
    parser = argparse.ArgumentParser(description="Story Agent module port diagnostics (read-only)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List selected module adapters and capabilities")
    describe = sub.add_parser("describe", help="Describe one selected module adapter")
    describe.add_argument("port_name", choices=["video_generator", "keyer"])
    args = parser.parse_args()
    registry = build_default_registry()
    payload = registry.list_descriptions() if args.command == "list" else registry.describe(args.port_name)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
