from pathlib import Path
import json
import zipfile
from PIL import Image
from tests.test_story_r2v_skill import valid_two_shot_plan
from story_production_v2 import binding, sha, write
from shot_storyboard_pipeline import create_asset_bundle, build_storyboard_manifest, seal_storyboard_manifest, compile_consumers

def material_fixture(root):
    root.mkdir(parents=True, exist_ok=True)
    director = valid_two_shot_plan()
    for i, a in enumerate(director['assets']):
        p = root / f'asset{i}.png'
        Image.new('RGB', (32, 32), (i * 20, 60, 70)).save(p)
        a.update(path=str(p), sha256=sha(p))
    text = '相同的一句话。'
    for i, shot in enumerate(director['shots']):
        shot.update(story_text=text, source_start=2 + 10 * i, source_end=12 + 10 * i)
    director['source_audio']['duration_seconds'] = 22
    d = root / 'director.json'
    write(d, director)
    bundle = root / 'assets.json'
    b = create_asset_bundle(d, bundle)
    review = root / 'asset_review.json'
    write(review, dict(approved=True, score=93, critical_errors=[], artifact_sha256=b['asset_bundle_sha256']))
    planned = root / 'planned.json'
    plan = build_storyboard_manifest(d, bundle, review, root / 'images', planned)
    for i, e in enumerate(plan['entries']):
        Image.new('RGB', (1600, 900), (30 * i, 100, 130)).save(e['image_path'])
    sealed = root / 'sealed.json'
    s = seal_storyboard_manifest(planned, sealed)
    sr = root / 'storyboard_review.json'
    write(sr, dict(approved=True, score=93, critical_errors=[], artifact_sha256=s['storyboard_bundle_sha256']))
    title = root / 'title.png'
    Image.new('RGB', (1600, 900)).save(title)
    word = root / 'word.docx'
    with zipfile.ZipFile(word, 'w') as z:
        z.writestr('word/document.xml', f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>开场。{text}{text}</w:t></w:r></w:p></w:body></w:document>')
    music = root / 'music.wav'
    music.write_bytes(b'original-wave-input')
    audio = root / 'voice.wav'
    audio.write_bytes(b'original-narration')
    previous = root / 'previous.json'
    write(previous, {'music_path': str(music), 'music_sha256': sha(music), 'slides': [{'shot_id': 'TITLE', 'poster_path': str(title), 'poster_sha256': sha(title), 'duration_seconds': 2, 'subtitle': '', 'word_text': '开场。'}, *({'shot_id': shot['shot_id'], 'duration_seconds': 10, 'word_start': 3 + i * len(text)} for i, shot in enumerate(director['shots']))]})
    ppt = root / 'compiled.json'
    receipt = root / 'compile.json'
    compile_consumers(sealed, sr, root / 'r2v.json', root / 'jobs.csv', receipt, previous, ppt)
    return dict(director=d, plan=ppt, compile_receipt=receipt, inputs={r: binding(p) for r, p in [('final_word', word), ('finished_music', music), ('audio', audio)]}, output=root / 'materials.zip', receipt=root / 'materials.json')
