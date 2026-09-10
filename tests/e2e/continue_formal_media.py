"""Continue actual formal assembly/backgrounds after real independent fixture reviews.

Upstream provider responses are simulated. No QA result is manufactured here.
"""
from pathlib import Path
import json
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from story_production_v2 import binding, current, write, validate_independent_approval
from story_artifact_validation import validate_independent_review
from story_run import load_run, record_run


def main():
    root=Path(sys.argv[1]).resolve();project=root/'fixture';status=project/'99_项目状态';cards=project/'cards'
    runfile=status/'story_run.json';run=load_run(runfile)
    r2v_review=status/'r2v_group_visual_review.json';semantic_review=cards/'independent_review.json'
    # Honest pause: neither missing review nor machine-only QA can proceed.
    validate_independent_review('r2v_group_visual_review',r2v_review)
    review=json.loads(semantic_review.read_text())
    validate_independent_approval(review,cards/'semantic_review_bundle.json',producer_context='offline-fixture-producer')
    review_checks=review.get('checks', review.get('findings', {}))
    checks=('text_region_locked','non_text_motion_only','camera_fixed','global_jitter_passed','smooth_motion_passed','no_tremor_passed','ocr_first_frame_passed','ocr_middle_frame_passed','ocr_last_frame_passed','text_stability_passed')
    if any(review_checks.get(key) is not True for key in checks):
        raise ValueError('Independent semantic reviewer must explicitly evaluate every requested check')
    bundle=json.loads((cards/'semantic_review_bundle.json').read_text())
    for item in bundle['artifacts']:current(item)
    request=json.loads((cards/'semantic_card_motion_request.json').read_text())
    item=request['cards'][0]
    # Mock provider contract facts are confined to this offline upstream fixture.
    generation=cards/'semantic_card_generation_receipt.json'
    write(generation,{'schema_version':'story-semantic-card-generation/v1','imagegen_native':True,'post_render_text_overlay':False,'attempt_count':1,
        'fixture_boundary':'SIMULATED native-provider response, not a real ImageGen claim; outside tested deterministic stages','production_eligible':False,
        'artifact_semantic_plan_sha256':request['artifact_semantic_plan_sha256'],'independent_review':binding(semantic_review),
        'cards':[{'card_kind':'title_card','text':'FIXTURE',**binding(cards/'title.png'),'ocr_passed':review_checks['ocr_first_frame_passed']}]})
    motion=cards/'semantic_card_motion_receipt.json'
    write(motion,{'schema_version':'story-semantic-card-motion/v2','request_sha256':binding(cards/'semantic_card_motion_request.json')['sha256'],
        'artifact_semantic_plan_sha256':request['artifact_semantic_plan_sha256'],'fixture_boundary':'SIMULATED provider; all review fields copied from independent fixture reviewer',
        'production_eligible':False,'independent_review':binding(semantic_review),
        'cards':[{**item,'output_video_sha256':binding(cards/'title.mp4')['sha256'],'attempt_count':1,'provider':'offline-synthetic-test-fixture','provider_request_id':'fixture-card-1',**{k:review_checks[k] for k in checks},'visual_review_passed':review['approved']}]})
    artifacts={'authoritative_timeline_receipt':status/'timeline.json','semantic_card_generation_receipt':generation,'semantic_card_motion_receipt':motion,'r2v_provider_group_receipt':status/'mock_provider_group.json','r2v_group_machine_qa':status/'r2v_group_machine_qa.json','r2v_group_visual_review':r2v_review}
    for name,path in artifacts.items():
        record_run(run_file=runfile,package='r2v_visuals',status='running',artifact_id=name,artifact_path=path,replace=True)
    pipeline=Path(__file__).resolve().parents[2]/'story_pipeline.py'
    command=[sys.executable,str(pipeline),'assemble','--run-file',str(runfile),'--plan',str(project/'plan.json'),'--videos-dir',str(project/'videos'),'--title-video',str(cards/'title.mp4'),'--audio',run['inputs']['audio']['path'],'--output',str(project/'master.mp4'),'--decisions',str(status/'assembly.json'),'--clips-dir',str(project/'clips'),'--ppt-plan',str(project/'ppt_plan.json')]
    result=subprocess.run(command,capture_output=True,text=True);(root/'formal-assemble.log').write_text(result.stdout+result.stderr)
    if result.returncode:raise RuntimeError(result.stderr)
    command=[sys.executable,str(pipeline),'backgrounds','--run-file',str(runfile),'--visual-master',str(project/'master.mp4'),'--music',run['inputs']['finished_music']['path'],'--authoritative-timeline-receipt',str(status/'timeline.json'),'--assembly-plan',str(status/'assembly.json'),'--output-with-subtitles',str(project/'background_with_formal.mp4'),'--output-without-subtitles',str(project/'background_without_formal.mp4'),'--body-srt',str(project/'body.srt'),'--render-receipt',str(status/'background_receipt_formal.json'),'--work-dir',str(project/'background_work'),'--width','1920','--height','1080','--fps','24']
    result=subprocess.run(command,capture_output=True,text=True);(root/'formal-backgrounds.log').write_text(result.stdout+result.stderr)
    if result.returncode:raise RuntimeError(result.stderr)
    print('Actual formal assembly and background rendering complete; release/pack/seal still pending.')

if __name__=='__main__':main()
