"""Content-addressed member lists for operation/encode evidence, not trust caches.

Only lists of 64 or more directory/sequence members are externalized. Old inline
receipts remain readable. A referenced list is always rehashed before expansion.
The store belongs under project status (or the managed encoder state directory),
never inside a deliverable whose members it describes.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from story_hash_cache import sha256_file


def externalize_evidence(value, directory):
    if isinstance(value, list):
        return [externalize_evidence(item, directory) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key == 'members' and isinstance(item, list) and len(item) >= 64:
            data = (json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
            digest = hashlib.sha256(data).hexdigest()
            path = Path(directory).resolve() / (digest + '.json')
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                if sha256_file(path) != digest:
                    raise ValueError(f'Changed member evidence: {path}')
            else:
                # Publish only a complete durable file. Hard-link creation is
                # exclusive; a racing identical writer is safe to verify/reuse.
                import os
                import tempfile
                fd, temporary = tempfile.mkstemp(prefix='.members-', dir=path.parent)
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    try:
                        os.link(temporary, path)
                    except FileExistsError:
                        if sha256_file(path) != digest:
                            raise ValueError(f'Concurrent member evidence mismatch: {path}')
                finally:
                    os.unlink(temporary)
            result['members_manifest'] = {'schema_version': 'story-evidence-members/v1',
                'path': str(path), 'sha256': digest, 'bytes': len(data), 'member_count': len(item)}
        else:
            result[key] = externalize_evidence(item, directory)
    return result


def resolve_evidence(value):
    """Expand compact lists for a legacy consumer; verify even within a new run."""
    if isinstance(value, list):
        return [resolve_evidence(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: resolve_evidence(item) for key, item in value.items() if key != 'members_manifest'}
    if 'members_manifest' in value:
        if 'members' in value:
            raise ValueError('Ambiguous inline and external member evidence')
        ref = value['members_manifest']
        path = Path(ref['path'])
        if ref.get('schema_version') != 'story-evidence-members/v1' or path.is_symlink():
            raise ValueError('Invalid member evidence reference')
        if sha256_file(path) != ref['sha256'] or path.stat().st_size != ref['bytes']:
            raise ValueError(f'Changed member evidence: {path}')
        members = json.loads(path.read_text())
        if not isinstance(members, list) or len(members) != ref['member_count']:
            raise ValueError('Invalid member evidence count')
        result['members'] = members
    return result
