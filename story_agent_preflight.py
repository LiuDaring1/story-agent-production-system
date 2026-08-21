from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from story_agent_runtime import file_sha256


def build_start_preflight_report(
    snapshot: Mapping[str, Any],
    *,
    code_paths: Sequence[Path],
    module_profile: str,
    module_execution_mode: str,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    project_dir = Path(str(snapshot.get("project_dir") or "")).expanduser()
    check("project_exists", project_dir.is_dir(), str(project_dir))
    evidence = snapshot.get("evidence", {}) if isinstance(snapshot.get("evidence"), Mapping) else {}
    manifest_path = Path(str(evidence.get("manifest") or ""))
    check("manifest_readable", manifest_path.is_file(), str(manifest_path))

    supervisor = snapshot.get("supervisor", {}) if isinstance(snapshot.get("supervisor"), Mapping) else {}
    check(
        "supervisor_stopped",
        not bool(supervisor.get("running")),
        f"pid={supervisor.get('pid') or 0}, effective_status={supervisor.get('effective_status') or ''}",
    )
    locks = snapshot.get("locks", {}) if isinstance(snapshot.get("locks"), Mapping) else {}
    job_lock = locks.get("job_lock", {}) if isinstance(locks.get("job_lock"), Mapping) else {}
    check(
        "no_live_job_lock",
        not bool(job_lock.get("owner_alive")),
        f"path={job_lock.get('path') or ''}, pid={job_lock.get('pid') or 0}",
    )
    control = locks.get("control", {}) if isinstance(locks.get("control"), Mapping) else {}
    check(
        "cancel_not_requested",
        not bool(control.get("cancel_requested")),
        f"run_epoch={control.get('run_epoch') or 0}",
    )

    budget = snapshot.get("budget", {}) if isinstance(snapshot.get("budget"), Mapping) else {}
    spent = float(budget.get("spent") or 0.0)
    reserved = float(budget.get("reserved") or 0.0)
    hard_limit = float(budget.get("hard_limit") or 0.0)
    check(
        "hard_budget_available",
        hard_limit > 0 and spent + reserved < hard_limit,
        f"spent={spent:.2f}, reserved={reserved:.2f}, hard_limit={hard_limit:.2f}",
    )
    timing = snapshot.get("timing", {}) if isinstance(snapshot.get("timing"), Mapping) else {}
    deadline_enabled = bool(timing.get("runtime_deadline_enabled", False))
    deadline = float(timing.get("deadline_hours") or 0.0)
    check(
        "fixed_runtime_deadline_disabled",
        not deadline_enabled and deadline == 0.0,
        f"runtime_deadline_enabled={deadline_enabled}, deadline_hours={deadline:.2f}",
    )

    status_dir = project_dir / "99_项目状态"
    check(
        "status_directory_writable",
        status_dir.is_dir() and os.access(status_dir, os.R_OK | os.W_OK),
        str(status_dir),
    )
    check(
        "module_selection_locked",
        bool(module_profile) and bool(module_execution_mode),
        f"profile={module_profile}, execution_mode={module_execution_mode}",
    )

    code_hashes: dict[str, str] = {}
    code_ok = True
    for path in code_paths:
        if not path.is_file():
            code_ok = False
            continue
        code_hashes[str(path)] = file_sha256(path)
    check(
        "observability_recovery_code_present",
        code_ok and len(code_hashes) == len(code_paths),
        f"{len(code_hashes)}/{len(code_paths)} files hashed",
    )

    artifact_progress = (
        snapshot.get("artifact_progress", {})
        if isinstance(snapshot.get("artifact_progress"), Mapping)
        else {}
    )
    invalid_progress = [
        name
        for name, value in artifact_progress.items()
        if isinstance(value, Mapping) and "非法" in str(value.get("detail") or "")
    ]
    check(
        "target_filename_plans_valid",
        not invalid_progress,
        "、".join(invalid_progress) if invalid_progress else "all parsed targets are exact filenames",
    )

    stale_stage_rows = [
        str(row.get("stage") or "")
        for row in snapshot.get("stage_rows", [])
        if isinstance(row, Mapping) and row.get("effective_status") == "stale"
    ]
    check(
        "stage_records_reconciled",
        not stale_stage_rows,
        "、".join(stale_stage_rows) if stale_stage_rows else "no stale passed stage records",
    )

    story_images = (
        snapshot.get("story_images", {})
        if isinstance(snapshot.get("story_images"), Mapping)
        else {}
    )
    stale_images = int(story_images.get("stale_or_unbound_count") or 0)
    check(
        "story_image_lineage_clean",
        stale_images == 0,
        (
            f"current_valid={int(story_images.get('current_lineage_valid_count') or 0)}, "
            f"disk_present={int(story_images.get('physical_expected_named_count') or 0)}, "
            f"stale_or_unbound={stale_images}"
        ),
    )

    return {
        "kind": "story_agent_start_preflight_v1",
        "passed": all(item["passed"] for item in checks),
        "checks": checks,
        "job_id": str(snapshot.get("job_id") or ""),
        "project_dir": str(project_dir),
        "agent_status": str(snapshot.get("agent_status") or ""),
        "ready_stages": snapshot.get("ready_stages", []),
        "current_stage": str(snapshot.get("current_stage") or ""),
        "artifact_progress": artifact_progress,
        "module_profile": module_profile,
        "module_execution_mode": module_execution_mode,
        "code_sha256": code_hashes,
        "provider_calls_made": False,
        "start_authorized": False,
        "requires_user_confirmation": True,
        "note": "该检查只读；即使 passed 也不会启动 supervisor 或调用任何 provider。",
    }
