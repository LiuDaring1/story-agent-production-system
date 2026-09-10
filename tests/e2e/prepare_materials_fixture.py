"""Prepare deterministic mock upstreams; stop at each real independent review gate."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from PIL import Image
from tests.test_story_r2v_skill import valid_plan
from story_production_v2 import binding, current, write
from shot_storyboard_pipeline import create_asset_bundle, build_storyboard_manifest, seal_storyboard_manifest, compile_consumers


def prepare(root, phase):
    root = root.resolve()
    work = root / 'materials_upstream'
    work.mkdir(exist_ok=True)
    ledger = json.loads((root / '99_项目状态/story_run.json').read_text())
    audio = current(ledger['inputs']['audio'])
    source_plan = json.loads((root / 'plan.json').read_text())
    source_shot = source_plan['shots'][0]
    director_path = work / 'director.json'
    assets = work / 'asset_bundle.json'
    asset_review = work / 'asset_review.json'
    planned = work / 'storyboard_planned.json'
    sealed = work / 'storyboard_sealed.json'
    storyboard_review = work / 'storyboard_review.json'
    if phase == 'assets':
        plan = valid_plan()
        plan['story_id'] = 'offline-materials-fixture'
        from story_video_synthesizer.media import probe_duration
        plan['source_audio'] = {**binding(audio), 'duration_seconds': probe_duration(audio)}
        plan['policies']['duration_choices'] = [6, 10]
        asset = plan['assets'][2]
        asset.update(**binding(root / 'assets/background.png'), appearance_summary='Deterministic mock-provider empty colored environment; no narrative quality claim')
        plan['assets'] = [asset]
        plan['continuity_groups'][0].update(location='offline synthetic color background', anchors=['uniform color field'])
        shot = plan['shots'][0]
        shot.update(assembly_trim={'anchor':'start'}, provider_seconds=6, shot_id='S001', source_start=source_shot['source_start'], source_end=source_shot['source_end'], story_text=source_shot['story_text'], reference_asset_ids=['environment-a'], initial_visible_characters=[], entering_characters=[], exiting_characters=[], entry_state={}, exit_state={}, prop_state_transitions=[], visual_focus='synthetic environment for deterministic materials integration', prompt='Mock provider: empty synthetic environment; no people, props, text or narrative action.')
        for key in ('opening_frame', 'closing_frame'):
            shot[key].update(visual_focus='synthetic empty environment', subject_layout='no subjects', eyeline='no characters', action_phase='no narrative action', state_summary={})
        shot['performance_beats'] = [{'start_second':0,'end_second':6,'source_text':source_shot['story_text'],'performance':'No narrative action; deterministic mock fixture','visible_characters':[],'camera':'locked wide'}]
        write(director_path, plan)
        result = create_asset_bundle(director_path, assets)
        write(work/'mock_provider_provenance.json', {'provider':'offline_mock','paid_generation':False,'source':binding(root/'assets/background.png'),'purpose':'deterministic pipeline integration only; not visual storytelling quality'})
        return {'waiting_for':'independent_asset_review','review_path':str(asset_review),'artifact_path':str(assets),'artifact_sha256':result['asset_bundle_sha256']}
    if phase == 'storyboard':
        result = build_storyboard_manifest(director_path, assets, asset_review, work/'images', planned)
        for row in result['entries']:
            image_path = Path(row['image_path'])
            with Image.open(root/'assets/background.png') as image:
                image.convert('RGB').resize((1280,720)).save(image_path)
        result = seal_storyboard_manifest(planned,sealed)
        return {'waiting_for':'independent_storyboard_review','review_path':str(storyboard_review),'artifact_path':str(sealed),'artifact_sha256':result['storyboard_bundle_sha256']}
    if phase == 'compile':
        previous = json.loads((root/'ppt_plan.json').read_text())
        previous.update(music_path=ledger['inputs']['finished_music']['path'],music_sha256=ledger['inputs']['finished_music']['sha256'])
        for row in previous['slides']:
            if row['shot_id']=='TITLE':
                row.update(poster_path=str(root/'cards/title.png'),poster_sha256=binding(root/'cards/title.png')['sha256'],duration_seconds=source_shot['source_start'],word_text='FIXTURE')
            else:
                row['duration_seconds']=source_shot['source_end']-source_shot['source_start']
        write(work/'ppt_previous.json',previous)
        compile_consumers(sealed,storyboard_review,work/'r2v_plan.json',work/'jobs.csv',work/'compile_receipt.json',work/'ppt_previous.json',work/'ppt_compiled.json')
        request = {'director':str(director_path),'plan':str(work/'ppt_compiled.json'),'compile_receipt':str(work/'compile_receipt.json'),'output':str(root/'03_产品素材/PPT素材'),'receipt':str(root/'99_项目状态/materials_receipt.json')}
        write(work/'materials_request.json',request)
        return request

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--phase',choices=['assets','storyboard','compile'],required=True)
    args=parser.parse_args()
    print(json.dumps(prepare(args.root,args.phase),ensure_ascii=False,indent=2))
