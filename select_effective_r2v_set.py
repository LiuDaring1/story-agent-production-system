#!/usr/bin/env python3
"""Select one effective clip per shot from a base R2V run and targeted repair."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON 顶层必须是对象：{path}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="从基础与定点修复结果选择有效 R2V 镜头集")
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--base-dir", required=True, type=Path)
    parser.add_argument("--base-receipt", required=True, type=Path)
    parser.add_argument("--repair-dir", required=True, type=Path)
    parser.add_argument("--repair-receipt", required=True, type=Path)
    parser.add_argument("--repair-shots", required=True, nargs="+")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()

    plan_path = args.plan.expanduser()
    base_dir = args.base_dir.expanduser()
    repair_dir = args.repair_dir.expanduser()
    output_dir = args.output_dir.expanduser()
    manifest_path = args.manifest.expanduser()
    plan = load_json(plan_path)
    shots = plan.get("shots")
    if not isinstance(shots, list) or not shots:
        raise ValueError("计划缺少 shots")
    expected = [str(row.get("shot_id") or "") for row in shots if isinstance(row, dict)]
    if any(not value for value in expected) or len(set(expected)) != len(expected):
        raise ValueError("计划镜头 ID 缺失或重复")
    repair_shots = set(args.repair_shots)
    if not repair_shots.issubset(set(expected)):
        raise ValueError("repair-shots 包含计划外镜头")

    base_receipt = load_json(args.base_receipt.expanduser())
    repair_receipt = load_json(args.repair_receipt.expanduser())
    base_rows = {
        str(row.get("filename") or ""): row
        for row in base_receipt.get("shots", [])
        if isinstance(row, dict)
    }
    repair_rows = {
        str(row.get("filename") or ""): row
        for row in repair_receipt.get("shots", [])
        if isinstance(row, dict)
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    selected: list[dict[str, Any]] = []
    combined_receipt_rows: list[dict[str, Any]] = []
    for shot_id in expected:
        filename = f"{shot_id}.mp4"
        round_name = "repair_round1" if shot_id in repair_shots else "base_round0"
        source_dir = repair_dir if shot_id in repair_shots else base_dir
        source = source_dir / filename
        if not source.is_file() or source.stat().st_size <= 0:
            raise FileNotFoundError(f"有效镜头源文件缺失：{source}")
        target = output_dir / filename
        shutil.copy2(source, target)
        digest = sha256_path(target)
        if digest != sha256_path(source):
            raise RuntimeError(f"复制后哈希不一致：{shot_id}")
        receipt_row = (repair_rows if shot_id in repair_shots else base_rows).get(filename)
        if receipt_row is None:
            raise ValueError(f"{shot_id} 缺少所选轮次供应商回执")
        if receipt_row.get("status") != "downloaded":
            raise ValueError(f"{shot_id} 所选回执不是 downloaded")
        if str(receipt_row.get("output_sha256") or "") != digest:
            raise ValueError(f"{shot_id} 所选视频哈希与回执不一致")
        selected.append({
            "shot_id": shot_id,
            "selected_round": round_name,
            "source_path": str(source),
            "output_path": str(target),
            "sha256": digest,
            "provider_task_id": str(receipt_row.get("task_id") or ""),
        })
        combined_receipt_rows.append(receipt_row)

    combined_receipt = {
        "schema_version": "story-r2v-effective-receipt-v1",
        "provider": str(repair_receipt.get("provider") or base_receipt.get("provider") or ""),
        "model": str(repair_receipt.get("model") or base_receipt.get("model") or ""),
        "shots": combined_receipt_rows,
    }
    receipt_path = output_dir / "r2v_effective_receipt.json"
    receipt_path.write_text(json.dumps(combined_receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    payload = {
        "schema_version": "story-r2v-effective-selection-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "plan_path": str(plan_path),
        "plan_sha256": sha256_path(plan_path),
        "base_receipt_path": str(args.base_receipt.expanduser()),
        "base_receipt_sha256": sha256_path(args.base_receipt.expanduser()),
        "repair_receipt_path": str(args.repair_receipt.expanduser()),
        "repair_receipt_sha256": sha256_path(args.repair_receipt.expanduser()),
        "effective_receipt_path": str(receipt_path),
        "effective_receipt_sha256": sha256_path(receipt_path),
        "repair_shots": sorted(repair_shots),
        "selected": selected,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output_dir": str(output_dir),
        "manifest": str(manifest_path),
        "manifest_sha256": sha256_path(manifest_path),
        "effective_receipt": str(receipt_path),
        "clips": len(selected),
        "repairs": len(repair_shots),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
