"""Bounded human-facing JSON; authoritative data stays in explicit files."""
from pathlib import Path


def compact(value, *, depth=0):
    if isinstance(value, str):
        return value if len(value) <= 600 else value[:600] + '… [truncated; read full JSON]'
    if isinstance(value, list):
        if len(value) > 8:
            return {'count': len(value), 'sample': [compact(x, depth=depth + 1) for x in value[:3]], 'truncated': True}
        return [compact(x, depth=depth + 1) for x in value]
    if isinstance(value, dict):
        if len(value) > 20:
            return {'key_count': len(value), 'sample': {k: compact(value[k], depth=depth + 1) for k in list(value)[:3]}, 'truncated': True}
        if depth >= 4:
            return {'keys': list(value)[:20], 'key_count': len(value), 'truncated': True}
        return {k: compact(v, depth=depth + 1) for k, v in value.items()}
    return value


def result_summary(result, *, operation, run_file, full_result):
    from story_production_v2 import binding
    return {'schema_version': 'story-command-result-summary/v1', 'operation': operation,
        'run_file': str(Path(run_file).resolve()), 'full_result': binding(full_result),
        'result': compact(result)}
