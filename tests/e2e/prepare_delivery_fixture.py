"""Finish real directory packaging, and prepare independent final fixture reviews."""
from pathlib import Path
import json,sys,subprocess
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from story_production_v2 import binding,write,current
from story_run import load_run,record_run
from story_evidence import write_review_bundle


def main():
    root=Path(sys.argv[1]).resolve();project=root/'fixture';status=project/'99_项目状态';runfile=status/'story_run.json'
    run=load_run(runfile);media=json.loads((status/'customer_media_receipt.json').read_text())
    sources={'customer_manuscript':run['inputs']['final_word']['path'],'music':run['inputs']['finished_music']['path'],
        'demo':media['artifacts']['product_demo']['path'], 'background_image':str(project/'assets/background.png'),
        'background_video_with_subtitles':media['artifacts']['product_background_with_subtitles']['path'],
        'background_video_without_subtitles':media['artifacts']['product_background_without_subtitles']['path'],
        'a_only_video':media['artifacts']['product_a_only_background']['path'], 'ppt_materials':str(project/'03_产品素材/PPT素材')}
    request=status/'pack_request.json';receipt=status/'managed_package_receipt.json'
    write(request,dict(output_root=str(project/'05_客户资料包'),receipt=str(receipt),sources=sources,story_name='FIXTURE'))
    cmd=[sys.executable,str(Path(__file__).resolve().parents[2]/'story_pipeline.py'),'pack','--run-file',str(runfile),'--request',str(request)]
    result=subprocess.run(cmd,capture_output=True,text=True);(root/'formal-pack.log').write_text(result.stdout+result.stderr)
    if result.returncode:raise RuntimeError(result.stderr)
    upstream=project/'materials_upstream'
    roles={'master_director_plan':upstream/'director.json','storyboard_manifest_sealed':upstream/'storyboard_sealed.json','storyboard_review':upstream/'storyboard_review.json',
        'shot_storyboard_compile_receipt':upstream/'compile_receipt.json','ppt_materials_receipt':status/'materials_receipt.json',
        'managed_package_receipt':receipt,'qa_product_report':receipt,'customer_media_receipt':status/'customer_media_receipt.json',
        'packaging_prompt_receipt':project/'panels_v2/prompt_receipt.json','keying_preset_lock':status/'keying/keying_preset.lock.json',
        'keying_visual_review':status/'keying/independent_review.json'}
    # The actual keying lock path is owned by the preset; do not guess alternatives.
    preset=json.loads((status/'keying/keying_preset.json').read_text())
    for role,path in roles.items():
        record_run(run_file=runfile,package='delivery',status='running',artifact_id=role,artifact_path=path,replace=True)
    write(status/'director_review_request.json',dict(artifact=binding(upstream/'director.json'),review_output=str(status/'director_plan_review.json'),
        scope='Offline synthetic upstream director only. Verify confirmed source text/time windows, explicit contiguous trim and compiler mapping. Not a real story creative-quality approval.',producer_context='offline-fixture-producer',
        rules=[binding(Path(__file__).resolve().parents[2]/'skills/story-full-auto/references/delivery-contract.md')]))
    bundle=status/'theme_review_bundle.json'
    write_review_bundle(bundle,[project/'assets/frame.png',project/'assets/background.png',status/'keying/keying_preset.json',status/'media_preview.json'])
    write(status/'theme_review_request.json',dict(artifact=binding(bundle),review_output=str(status/'theme_review.json'),producer_context='offline-fixture-producer',
        scope='Offline mock-native upstream theme fixture. Inspect real frame/background and formal geometry. Native provider origin is simulated, not tested.',
        frame_checks=['passed','current_story_redesign','no_reference_theme_leak','true_alpha_verified','continuous_opaque_four_sides','inner_masking_lip'],
        background_checks=['passed','not_preblurred','controlled_high_frequency_detail','no_text_logo_or_vignette'],
        formal_rule='release_video.story_frame_integrity_issues(frame, expected_size=(1920,1080), story_box=(210,270,910,512)); use actual preset rather than older generic source QA'))
    print(status/'director_review_request.json');print(status/'theme_review_request.json')

if __name__=='__main__':main()
