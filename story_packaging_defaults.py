"""Fixed packaging configuration and project story-information projection."""
import json
import math
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from story_production_v2 import binding, current, write

CONFIG = Path(__file__).resolve().parent / 'assets/references/main_vertical_package_defaults.json'

def defaults():
    config = json.loads(CONFIG.read_text())
    result = {'version': config['version'], 'config': binding(CONFIG), 'theme_style': config['theme_style']}
    for role, key in [('packaging_reference', 'reference'), ('packaging_prompt', 'prompt')]:
        item = binding(CONFIG.parent / config[key])
        if item['sha256'] != config[key + '_sha256']:
            raise ValueError('System packaging configuration hash mismatch: ' + key)
        result[role] = item
    return result

def prepare_inputs(paths, project):
    paths = dict(paths)
    missing = [k for k in ('packaging_reference', 'packaging_prompt') if paths.get(k) is None]
    if not missing:
        try:
            has_info = 'story_info' in json.loads(Path(paths['story_requirements']).read_text())
        except (ValueError, OSError):
            has_info = False
        if not has_info:
            return paths, None  # Compatibility for pre-fix explicit candidate inputs.
    config = defaults()
    for key in missing:
        paths[key] = Path(config[key]['path'])
    source = Path(paths['story_requirements'])
    requirements = json.loads(source.read_text())
    info = requirements.setdefault('story_info', {})
    sources = info.setdefault('sources', {})
    if not info.get('story_name'):
        with zipfile.ZipFile(paths['final_word']) as doc:
            tree = ET.fromstring(doc.read('word/document.xml'))
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        titles = []
        for p in tree.findall('.//w:p', ns):
            style = p.find('w:pPr/w:pStyle', ns)
            if style is not None and style.get('{'+ns['w']+'}val') in {'Title', '标题'}:
                titles.append(''.join(p.itertext()).strip())
        if len(titles) != 1 or not titles[0]:
            raise ValueError('Missing unambiguous confirmed story title; never infer from directory')
        info['story_name'] = titles[0]
        sources['story_name'] = {'kind': 'final_word_title', **binding(paths['final_word'])}
    for key in ('story_name', 'story_type', 'age_range'):
        if not str(info.get(key, '')).strip() or not sources.get(key):
            raise ValueError('Missing confirmed story information/source: ' + key)
    if not info.get('theme_style'):
        info['theme_style'] = config['theme_style']
        sources['theme_style'] = {'kind': 'system_default', 'version': config['version'], **config['config']}
    target = Path(project) / '99_项目状态/story_requirements.json'
    if target.resolve() != source.resolve():
        if target.exists():
            if json.loads(target.read_text()) != requirements:
                raise FileExistsError('Conflicting project story information: ' + str(target))
        else:
            write(target, requirements)
    elif requirements != json.loads(source.read_text()):
        write(target, requirements)
    paths['story_requirements'] = target
    return paths, config

def packaging_fields(inputs, timeline_receipt):
    from story_timeline import validate_authoritative_timeline_receipt
    from story_video_synthesizer.media import probe_duration
    info = json.loads(current(inputs['story_requirements']).read_text())['story_info']
    timeline_path = current(timeline_receipt)
    timeline = validate_authoritative_timeline_receipt(timeline_path, expected_inputs=inputs)
    # The timeline binds the complete authoritative audio, including trailing silence.
    duration = probe_duration(current(timeline['authoritative_audio']))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Invalid authoritative duration')
    seconds = int(duration + 0.5)
    minutes, seconds = divmod(seconds, 60)
    label = (f'{minutes}分{seconds}秒' if seconds else f'{minutes}分钟') if minutes else f'{seconds}秒'
    fields = {key: info[key] for key in ('story_name', 'story_type', 'age_range', 'theme_style')}
    fields['duration_text'] = label
    return fields
