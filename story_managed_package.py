"""Copy only Agent-owned files; never encode or move customer directories."""
from pathlib import Path
import json
import shutil
import tempfile
import os
import hashlib
import uuid
from story_production_v2 import BASE_ROLES, ADVANCED_ROLES, binding, current, write, validate_managed_receipt, protect_outputs

def _package_locked(*, output_root, receipt, sources, story_name, inputs):
    protect_outputs([output_root, receipt], [*sources.values(), *(v['path'] for v in inputs.values())])
    output_root = Path(output_root).resolve()
    receipt = Path(receipt)
    if not story_name or any((c in story_name for c in '/\\\x00')):
        raise ValueError('Invalid story name')
    needed = set(ADVANCED_ROLES)
    if set(sources) != needed:
        raise ValueError(f'Package roles must equal {sorted(needed)}')
    bound = {k: binding(v) for k, v in sources.items()}
    for role, input_role in [('customer_manuscript', 'final_word'), ('music', 'finished_music')]:
        current(inputs[input_role])
        if bound[role]['sha256'] != inputs[input_role]['sha256']:
            raise ValueError(f'{role} is not confirmed input')
    old = {}
    retired = []
    if receipt.exists():
        old_payload = json.loads(receipt.read_text())
        if old_payload.get('schema_version') != 'story-managed-package/v2':
            raise ValueError('Cannot migrate old package')
        old = {i['path']: i for i in old_payload['artifacts']}
        retired = old_payload.get('retired', [])
    names = {'customer_manuscript': '故事文稿', 'music': '故事配乐', 'demo': '示范表演', 'background_image': '背景图片', 'background_video_with_subtitles': '背景视频（含字幕）', 'background_video_without_subtitles': '背景视频（无字幕）', 'a_only_video': 'A镜无人物背景视频', 'ppt_materials': 'PPT素材清单'}
    planned = []
    for variant, label, roles in [('base', '基础版', BASE_ROLES), ('advanced', '进阶版', ADVANCED_ROLES)]:
        directory = output_root / f'绵羊故事锦囊：{story_name}（{label}）'
        if directory.is_symlink() or output_root not in directory.resolve().parents:
            raise ValueError('Unsafe package directory')
        for role in roles:
            source = Path(bound[role]['path'])
            dest = directory / f'{names[role]}：{story_name}{source.suffix}'
            if dest.is_symlink() or (dest.exists() and str(dest) not in old):
                raise ValueError(f'Unmanaged destination collision: {dest}')
            if dest.exists() and binding(dest)['sha256'] not in {old[str(dest)]['sha256'], old[str(dest)].get('prior_sha256')}:
                raise ValueError(f'User changed managed file; preserve it: {dest}')
            planned.append((variant, role, source, dest))
    # Journal old role/path ownership before any copy or retirement. A suffix change
    # must not erase the old record even if the process dies after the rename.
    planned_paths = {str(dest) for _, _, _, dest in planned}
    backup_root = output_root.parent / '.story-managed-retired' / hashlib.sha256(str(output_root).encode()).hexdigest()
    for old_path, item in old.items():
        if old_path in planned_paths:
            continue
        source = Path(old_path)
        if output_root not in source.resolve().parents or source.is_symlink():
            raise ValueError(f'Unsafe retired path: {source}')
        if not source.is_file() or binding(source)['sha256'] not in {item['sha256'], item.get('prior_sha256')}:
            raise ValueError(f'User changed retired managed file; preserve it: {source}')
        retired.append({'role': item['role'], **binding(source), 'status': 'pending',
                        'backup_path': str(backup_root / uuid.uuid4().hex / source.name)})
    # Preflight every pending retirement before modifying customer files.
    for item in retired:
        if item['status'] == 'archived':
            continue
        source, backup = Path(item['path']), Path(item['backup_path'])
        if output_root not in source.resolve().parents or source.is_symlink():
            raise ValueError('Unsafe retired source')
        if backup_root not in backup.resolve().parents or backup.is_symlink():
            raise ValueError('Unsafe retirement backup')
        if backup.exists():
            if binding(backup)['sha256'] != item['sha256'] or source.exists():
                raise ValueError('Retirement backup conflict; preserve both files')
        elif not source.is_file() or binding(source)['sha256'] != item['sha256']:
            raise ValueError(f'User changed retired managed file; preserve it: {source}')
    pending = {'schema_version': 'story-managed-package/v2', 'status': 'copying', 'retired': retired,
               'artifacts': [{'role': f'{variant}:{role}', 'path': str(dest), 'sha256': bound[role]['sha256'], 'prior_sha256': binding(dest)['sha256'] if dest.exists() else None, 'source': bound[role]} for variant, role, source, dest in planned]}
    write(receipt, pending)
    for item in retired:
        if item['status'] == 'archived':
            continue
        source, backup = Path(item['path']), Path(item['backup_path'])
        if not backup.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            if binding(source)['sha256'] != item['sha256']:
                raise ValueError(f'User changed retired managed file; preserve it: {source}')
            os.replace(source, backup)
        item['status'] = 'archived'
        write(receipt, pending)
    artifacts = []
    for variant, role, source, dest in planned:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists() or binding(dest)['sha256'] != bound[role]['sha256']:
            fd, tmp = tempfile.mkstemp(prefix='.copy-', dir=dest.parent)
            os.close(fd)
            try:
                shutil.copy2(source, tmp)
                if binding(tmp)['sha256'] != bound[role]['sha256']:
                    raise ValueError('Source changed during copy')
                os.replace(tmp, dest)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        artifacts.append({'role': f'{variant}:{role}', **binding(dest), 'source': bound[role]})
    payload = {'schema_version': 'story-managed-package/v2', 'artifacts': artifacts, 'encoding_performed': False, 'status': 'complete', 'retired': retired}
    write(receipt, payload)
    validate_managed_receipt(receipt, inputs)
    return payload


def package(*, output_root, receipt, sources, story_name, inputs):
    from story_run import run_file_lock
    protect_outputs([output_root, receipt], [*sources.values(), *(v['path'] for v in inputs.values())])
    with run_file_lock(Path(receipt)):
        return _package_locked(output_root=output_root, receipt=receipt, sources=sources, story_name=story_name, inputs=inputs)
