import json
from pathlib import Path

from presenter_layout import BODY_OVERFLOW_POLICY, body_overflow_cache_key, severe_body_overflow_windows
from story_production_v2 import binding


def write_scan_report(
    foreground: Path,
    report_path: Path,
    *,
    samples=None,
    duration=60.0,
    fixed_anchor_x=0,
    fixed_anchor_y=0,
    canvas_width=1920,
    source_width=1920,
    source_height=1080,
    rendered_height=1080,
    person_crop=None,
    person_layout_policy="source-native-fixed-anchor/v2",
):
    samples = samples or [(round(index / 5, 3), 1.0) for index in range(int(duration * 5))]
    kwargs = dict(
        fixed_anchor_x=fixed_anchor_x,
        fixed_anchor_y=fixed_anchor_y,
        canvas_width=canvas_width,
        source_width=source_width,
        source_height=source_height,
        rendered_height=rendered_height,
        person_crop=person_crop,
        person_layout_policy=person_layout_policy,
    )
    key = body_overflow_cache_key(foreground, **kwargs)
    windows = severe_body_overflow_windows(samples, duration=duration)
    import presenter_layout
    scanner = {**binding(Path(presenter_layout.__file__)), "source_relative_path": "presenter_layout.py"}
    payload = {
        "schema_version": BODY_OVERFLOW_POLICY["schema_version"],
        "cache_key": key,
        "source": binding(foreground),
        "scanner_code": scanner,
        "policy": "fixed_anchor_never_scale_or_move; borderline_arm_or_hand_overflow_allowed; unmistakable_torso_excursion_cuts_to_b",
        "body_core_quantiles": list(BODY_OVERFLOW_POLICY["body_core_quantiles"]),
        "warning_visible_threshold": BODY_OVERFLOW_POLICY["visible_threshold"],
        "unmistakable_trigger_threshold": BODY_OVERFLOW_POLICY["trigger_threshold"],
        "duration_seconds": duration,
        "severe_windows": [list(row) for row in windows],
        "samples": [
            {"time": time, "visible_core_fraction": visible}
            for time, visible in samples
        ],
        "severe_samples": [
            {"time": time, "visible_core_fraction": visible}
            for time, visible in samples
            if visible is not None and visible < BODY_OVERFLOW_POLICY["visible_threshold"]
        ],
        "sample_count": len(samples),
        "scan_complete": True,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload))
    return payload
