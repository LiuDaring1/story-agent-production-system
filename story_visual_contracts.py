"""Scope-specific generation and review contracts, independent of production scope."""
import json
from pathlib import Path
from story_production_v2 import binding, current

ROOT = Path(__file__).resolve().parent / 'assets/references'
PRODUCT_ITEMS = ['背景视频', 'PPT', '配乐', '文稿', '示范视频', '朗读标注']

def compile_visual_scopes(inputs, fields):
    config = json.loads((ROOT / 'visual_scopes.json').read_text())
    requirements = json.loads(current(inputs['story_requirements']).read_text()) if 'story_requirements' in inputs else {}
    product = requirements.get('product_contents', {})
    items = product.get('items', PRODUCT_ITEMS)
    if not isinstance(items, list) or not items or not all(isinstance(x, str) and x.strip() for x in items):
        raise ValueError('Invalid confirmed product contents')
    if product and not product.get('source'):
        raise ValueError('Product contents require confirmation source')
    result = {}
    for scope in ('main', 'library', 'frame'):
        rule = config[scope]
        prompt = rule['prompt']
        if scope == 'main':
            prompt = current(inputs['packaging_prompt']).read_text().format(**fields)
            reference = inputs['packaging_reference']
        elif scope == 'frame':
            provenance = json.loads((ROOT / 'frame_reference_provenance.json').read_text())
            reference = binding(ROOT / provenance['path'])
            if reference['sha256'] != provenance['sha256']:
                raise ValueError('Frame reference hash mismatch')
            prompt += '\n本故事：' + fields['story_name']
        else:
            reference = None
            prompt += '\n故事：{story_name}；类型：{story_type}；时长：{duration_text}；年龄：{age_range}。'.format(**fields)
            prompt += '\n已确认商品包含：' + ' + '.join(items)
        result[scope] = {'prompt': prompt, 'reference': reference, 'rules': binding(ROOT / 'visual_scopes.json'), 'review_checks': rule['review_checks']}
    result['library']['product_contents'] = {'items': items, 'source': product.get('source', config['product_source']), 'external_production': ['PPT', '朗读标注']}
    result['library']['historical_visual_target'] = config['library']['historical_visual_target']
    return result


def confirmed_moral_text(inputs, timeline, body_end):
    """Project the complete confirmed wording using hash-bound tail cues as locator."""
    import unicodedata
    rows = json.loads(current(timeline['timings']).read_text())
    tail = [(index + 1, row) for index, row in enumerate(rows) if float(row['source_end']) > body_end + 1e-6]
    if not tail or any(float(row['source_start']) < body_end - 1e-6 for _, row in tail):
        raise ValueError('MORAL窗口必须对齐确认时间轴行边界')
    source_binding = inputs['confirmed_text']
    source = current(source_binding).read_text(encoding='utf-8-sig')
    keep = lambda char: not char.isspace() and not unicodedata.category(char).startswith('P')
    positions = [i for i, char in enumerate(source) if keep(char)]
    compact = ''.join(source[i] for i in positions)
    locator = ''.join(char for _, row in tail for char in row['line'] if keep(char))
    offset = compact.find(locator)
    if not locator or offset < 0 or compact.find(locator, offset + 1) >= 0:
        raise ValueError('确认文稿寓意定位缺失或有歧义，请明确文本来源')
    left, right = positions[offset], positions[offset + len(locator) - 1] + 1
    while right < len(source) and unicodedata.category(source[right]).startswith('P'):
        right += 1
    return {'text': source[left:right], 'source': source_binding, 'source_span': [left, right],
            'timeline': timeline['timings'], 'source_line_numbers': [index for index, _ in tail]}
