from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from product_package import create_package_dirs
from story_module_adapters import LocalProductPackageAdapter, MockProductPackageAdapter
from story_module_ports import (
    ModuleCapabilities,
    ModuleFailureCode,
    ModuleIdentity,
    ProductPackagePort,
    ProductPackageRequest,
    ProductPackageResult,
    product_package_payload,
    validate_product_package_payload,
)
from story_module_registry import build_product_package_registry


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas" / "module_ports" / "v1" / "product_package_port.schema.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def schema_issues(payload: dict) -> list[str]:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    issues: list[str] = []
    for field in schema["required"]:
        if field not in payload:
            issues.append(f"missing:{field}")
    if payload.get("schema_version") != schema["properties"]["schema_version"]["const"]:
        issues.append("schema_version")
    if payload.get("kind") not in schema["properties"]["kind"]["enum"]:
        issues.append("kind")
        return issues
    branch = next(
        item["then"]
        for item in schema["allOf"]
        if item["if"]["properties"]["kind"]["const"] == payload["kind"]
    )
    for field in branch["required"]:
        if field not in payload:
            issues.append(f"missing:{field}")
    for field, rule in branch.get("properties", {}).items():
        if field not in payload:
            continue
        value = payload[field]
        allowed = rule.get("type")
        allowed = allowed if isinstance(allowed, list) else [allowed]
        matches = (
            ("null" in allowed and value is None)
            or ("boolean" in allowed and type(value) is bool)
            or ("string" in allowed and isinstance(value, str))
            or ("object" in allowed and isinstance(value, dict))
            or ("array" in allowed and isinstance(value, list))
        )
        if not matches:
            issues.append(f"type:{field}")
            continue
        item_type = rule.get("items", {}).get("type") if isinstance(value, list) else None
        if item_type == "string" and any(not isinstance(item, str) for item in value):
            issues.append(f"items:{field}")
        if item_type == "object" and any(not isinstance(item, dict) for item in value):
            issues.append(f"items:{field}")
        if item_type == "object":
            item_rule = rule["items"]
            for index, item in enumerate(value):
                if not isinstance(item, dict):
                    continue
                for name in item_rule.get("required", []):
                    if name not in item:
                        issues.append(f"missing:{field}[{index}].{name}")
                for name, nested_rule in item_rule.get("properties", {}).items():
                    if name not in item:
                        continue
                    if nested_rule.get("type") == "string" and not isinstance(item[name], str):
                        issues.append(f"type:{field}[{index}].{name}")
                    if nested_rule.get("type") == "boolean" and type(item[name]) is not bool:
                        issues.append(f"type:{field}[{index}].{name}")
    return issues


def package_request(root: Path) -> ProductPackageRequest:
    root.mkdir(parents=True, exist_ok=True)
    source = root / "source.txt"
    source.write_text("package fixture\n", encoding="utf-8")
    target = root / "products" / "base" / "customer.txt"
    return ProductPackageRequest(
        "product-package:fixture",
        "copy_product_packages",
        "base_and_advanced",
        root / "products",
        (
            {
                "package_variant": "base",
                "source_path": str(source),
                "source_sha256": sha256(source),
                "destination_path": str(target),
            },
        ),
        (target,),
        "attempt-1",
    )


def package_sources(root: Path) -> dict[str, Path]:
    names = (
        "story_docx", "music", "annotation_docx", "demo_video", "background_image",
        "bg_with_sub", "bg_no_sub", "ppt_with_sub", "ppt_no_sub", "a_only_video",
    )
    suffixes = {
        "story_docx": ".docx", "music": ".mp3", "annotation_docx": ".docx",
        "demo_video": ".mp4", "background_image": ".png", "bg_with_sub": ".mp4",
        "bg_no_sub": ".mp4", "ppt_with_sub": ".pptx", "ppt_no_sub": ".pptx",
        "a_only_video": ".mp4",
    }
    sources: dict[str, Path] = {}
    for index, name in enumerate(names, start=1):
        path = root / "sources" / f"{name}{suffixes[name]}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"fixture-{index}\n".encode())
        os.utime(path, ns=(1_700_000_000_000_000_000 + index, 1_700_000_000_000_000_000 + index))
        sources[name] = path
    return sources


class ProtocolOnlyFake:
    identity = ModuleIdentity(
        "product_package", "story-product-package-port/v1", "protocol-fake", "protocol-fake/v1"
    )
    capabilities = ModuleCapabilities(provider="test", deterministic=True)

    def __init__(self) -> None:
        self.requests: list[ProductPackageRequest] = []

    def execute(self, request: ProductPackageRequest, *, executor):
        self.requests.append(request)
        return executor(request)


class ProductPackagePortTests(unittest.TestCase):
    def test_schema_and_python_validator_have_parity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = package_request(Path(directory))
            result = MockProductPackageAdapter().execute(
                request, executor=lambda _request: self.fail("mock called executor")
            )
            for kind, value in (
                ("product_package_request", request),
                ("product_package_result", result),
            ):
                payload = product_package_payload(kind, value)
                self.assertEqual(validate_product_package_payload(payload), [])
                self.assertEqual(schema_issues(payload), [])
                for mutation in ("missing", "wrong_type"):
                    invalid = copy.deepcopy(payload)
                    field = next(name for name in invalid if name not in {"kind", "schema_version"})
                    if mutation == "missing":
                        invalid.pop(field)
                    else:
                        invalid[field] = "false" if isinstance(invalid[field], bool) else False
                    self.assertEqual(bool(validate_product_package_payload(invalid)), bool(schema_issues(invalid)))
            invalid_binding = product_package_payload("product_package_request", request)
            invalid_binding["source_artifacts"][0].pop("source_sha256")
            self.assertTrue(validate_product_package_payload(invalid_binding))
            self.assertTrue(schema_issues(invalid_binding))

    def test_production_adapter_delegates_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = package_request(Path(directory))
            target = request.output_targets[0]
            calls: list[ProductPackageRequest] = []

            def executor(actual: ProductPackageRequest) -> ProductPackageResult:
                calls.append(actual)
                target.parent.mkdir(parents=True, exist_ok=True)
                source = Path(str(actual.source_artifacts[0]["source_path"]))
                target.write_bytes(source.read_bytes())
                return ProductPackageResult(
                    True, actual.operation, actual.package_variant,
                    ({
                        "package_variant": "base",
                        "source_path": str(source),
                        "source_sha256": sha256(source),
                        "path": str(target),
                        "sha256": sha256(target),
                        "production_eligible": True,
                    },),
                    actual.attempt_id, "executor/v1", True,
                )

            expected = executor(request)
            target.unlink()
            calls.clear()
            result = LocalProductPackageAdapter().execute(request, executor=executor)
            self.assertEqual(calls, [request])
            self.assertEqual(result, expected)

    def test_mock_never_calls_executor_or_requested_customer_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = package_request(Path(directory))
            result = MockProductPackageAdapter().execute(
                request,
                executor=lambda _request: self.fail("mock called production executor"),
            )
            self.assertTrue(result.success)
            self.assertFalse(result.production_eligible)
            self.assertFalse(any(path.exists() for path in request.output_targets))
            self.assertTrue(all("_mock_product_package" in item["path"] for item in result.output_artifacts))

            forbidden_root = ROOT / "forbidden_mock_customer_root"
            forbidden_target = forbidden_root / "base" / "customer.txt"
            forbidden_item = dict(request.source_artifacts[0])
            forbidden_item["destination_path"] = str(forbidden_target)
            forbidden = replace(
                request,
                output_root=forbidden_root,
                source_artifacts=(forbidden_item,),
                output_targets=(forbidden_target,),
            )
            rejected = MockProductPackageAdapter().execute(
                forbidden, executor=lambda _request: self.fail("mock called production executor")
            )
            self.assertFalse(rejected.success)
            self.assertEqual(rejected.failure.code, ModuleFailureCode.CONFIGURATION_ERROR)
            self.assertFalse(forbidden_root.exists())

    def test_missing_or_stale_source_fails_before_executor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for mode in ("missing", "stale"):
                request = package_request(root / mode)
                source = Path(str(request.source_artifacts[0]["source_path"]))
                if mode == "missing":
                    source.unlink()
                else:
                    source.write_text("tampered\n", encoding="utf-8")
                result = LocalProductPackageAdapter().execute(
                    request, executor=lambda _request: self.fail("invalid request reached executor")
                )
                self.assertFalse(result.success)
                self.assertEqual(result.failure.code, ModuleFailureCode.INVALID_INPUT)
                self.assertFalse(any(path.exists() for path in request.output_targets))

    def test_create_package_dirs_uses_protocol_and_preserves_layout_copy2_and_source_map(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = package_sources(root)
            fake = ProtocolOnlyFake()
            self.assertIsInstance(fake, ProductPackagePort)
            base, advanced, source_map = create_package_dirs(
                "通用故事", root / "products",
                sources["story_docx"], sources["music"], sources["annotation_docx"],
                sources["demo_video"], sources["background_image"], sources["bg_with_sub"],
                sources["bg_no_sub"], sources["ppt_with_sub"], sources["ppt_no_sub"],
                sources["a_only_video"], product_package_port=fake,
            )
            self.assertEqual(base.name, "绵羊故事锦囊：通用故事（基础版）")
            self.assertEqual(advanced.name, "绵羊故事锦囊：通用故事（进阶版）")
            base_names = {
                "故事文稿：通用故事.docx", "故事配乐：通用故事.mp3", "朗读标注：通用故事.docx",
                "示范表演：通用故事.mp4", "背景图片：通用故事.png",
            }
            advanced_names = base_names | {
                "背景视频：通用故事（含字幕）.mp4", "背景视频：通用故事（无字幕）.mp4",
                "故事PPT：通用故事（含字幕）.pptx", "故事PPT：通用故事（无字幕）.pptx",
                "A镜无人物背景视频：通用故事.mp4",
            }
            self.assertEqual({path.name for path in base.iterdir()}, base_names)
            self.assertEqual({path.name for path in advanced.iterdir()}, advanced_names)
            self.assertEqual(len(fake.requests), 1)
            self.assertEqual(len(source_map), len(base_names) + len(advanced_names))
            for key, source in source_map.items():
                variant, filename = key.split(":", 1)
                target = (base if variant == "base" else advanced) / filename
                self.assertEqual(target.read_bytes(), source.read_bytes())
                self.assertEqual(target.stat().st_mtime_ns, source.stat().st_mtime_ns)

    def test_existing_directories_are_backed_up_before_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = package_sources(root)
            output_root = root / "products"
            base = output_root / "绵羊故事锦囊：通用故事（基础版）"
            advanced = output_root / "绵羊故事锦囊：通用故事（进阶版）"
            for path in (base, advanced):
                path.mkdir(parents=True, exist_ok=True)
                (path / "old.txt").write_text("old\n", encoding="utf-8")
            backup_root = root / "backups"
            with patch("product_package.time.strftime", return_value="20260818_120000"):
                create_package_dirs(
                    "通用故事", output_root,
                    sources["story_docx"], sources["music"], sources["annotation_docx"],
                    sources["demo_video"], sources["background_image"], sources["bg_with_sub"],
                    sources["bg_no_sub"], sources["ppt_with_sub"], sources["ppt_no_sub"],
                    backup_root=backup_root,
                )
            self.assertTrue((backup_root / f"{base.name}_旧版_20260818_120000" / "old.txt").is_file())
            self.assertTrue((backup_root / f"{advanced.name}_旧版_20260818_120000" / "old.txt").is_file())
            self.assertFalse((base / "old.txt").exists())
            self.assertFalse((advanced / "old.txt").exists())

    def test_local_registry_exposes_only_the_protocol_contract(self) -> None:
        port = build_product_package_registry().product_package()
        self.assertIsInstance(port, ProductPackagePort)
        self.assertEqual(port.identity.port_name, "product_package")
        self.assertFalse(port.capabilities.external)
        self.assertFalse(port.capabilities.paid)
        self.assertFalse(port.capabilities.supported["owns_manifest"])
        self.assertFalse(port.capabilities.supported["owns_currentness"])


if __name__ == "__main__":
    unittest.main()
