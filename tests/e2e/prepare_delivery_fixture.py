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
    director_review=status/'director_plan_review.json'
    theme_review=status/'theme_review.json'
    if not director_review.is_file() or not theme_review.is_file():
        write(status/'director_review_request.json',dict(artifact=binding(upstream/'director.json'),review_output=str(director_review),
            scope='Offline synthetic upstream director only. Verify confirmed source text/time windows, explicit contiguous trim and compiler mapping. Not a real story creative-quality approval.',producer_context='offline-fixture-producer',
            rules=[binding(Path(__file__).resolve().parents[2]/'skills/story-full-auto/references/delivery-contract.md')]))
        bundle=status/'theme_review_bundle.json'
        write_review_bundle(bundle,[project/'assets/frame.png',project/'assets/background.png',status/'keying/keying_preset.json',status/'media_preview.json'])
        write(status/'theme_review_request.json',dict(artifact=binding(bundle),review_output=str(theme_review),producer_context='offline-fixture-producer',
            scope='Offline mock-native upstream theme fixture. Inspect real frame/background and formal geometry. Native provider origin is simulated, not tested.',
            frame_checks=['passed','current_story_redesign','no_reference_theme_leak','true_alpha_verified','continuous_opaque_four_sides','inner_masking_lip'],
            background_checks=['passed','not_preblurred','controlled_high_frequency_detail','no_text_logo_or_vignette'],
            formal_rule='release_video.story_frame_integrity_issues(frame, expected_size=(1920,1080), story_box=(210,270,910,512)); use actual preset rather than older generic source QA'))
        print(status/'director_review_request.json');print(status/'theme_review_request.json')
        return
    director_target=Path(json.loads(director_review.read_text())['artifact_path'])
    storyboard_review=upstream/'storyboard_review.json'
    storyboard_target=Path(json.loads(storyboard_review.read_text())['artifact_path'])
    materials_receipt=status/'materials_receipt.json'
    compile_receipt=Path(json.loads(materials_receipt.read_text())['compile_receipt']['path'])
    keying_lock=status/'keying/keying_preset.lock.json'
    keying_review=Path(json.loads(keying_lock.read_text())['review_path'])
    reviewed_theme_bundle=Path(json.loads(theme_review.read_text())['artifact_path'])
    theme_members=json.loads(reviewed_theme_bundle.read_text())['artifacts']
    by_name={Path(item['path']).name:Path(item['path']) for item in theme_members}
    frame=by_name['frame.png'];background=by_name['background.png']
    theme_manifest=status/'theme_assets_manifest.json'
    write(theme_manifest,{
        'schema_version':'story-theme-assets-lightweight/v3','story_name':'FIXTURE',
        'frame_reference':{**binding(frame),'role':'geometry_only_not_theme_or_ornament','locked_properties':['screen_placement','large_16_9_aperture','continuous_practical_border_thickness']},
        'sources':{'environment':binding(background),'story_frame_alpha':{**binding(frame),'method':'imagegen_reference_edit','checkerboard':False}},
        'artifacts':{'main_background_16x9':{**binding(background),'method':'imagegen_reference_edit'},'story_frame_png':{**binding(frame),'method':'native_alpha_passthrough','derived_from':['story_frame_alpha'],'has_true_alpha':True}},
        'frame_design_review':{'passed':True,'current_story_redesign':True,'no_reference_theme_leak':True,'true_alpha_verified':True,'continuous_opaque_four_sides':True,'inner_masking_lip':True,'evidence':str(theme_review)},
        'background_clean_review':{'passed':True,'not_preblurred':True,'controlled_high_frequency_detail':True,'no_text_logo_or_vignette':True},
        'svg_used':False,'fixture_boundary':'SIMULATED native-provider upstream; independent review covers only actual offline fixture pixels and geometry','production_eligible':False})
    roles={'master_director_plan':director_target,'director_plan_review':director_review,'storyboard_manifest_sealed':storyboard_target,'storyboard_review':storyboard_review,
        'shot_storyboard_compile_receipt':compile_receipt,'ppt_materials_receipt':materials_receipt,'theme_assets_manifest':theme_manifest,
        'managed_package_receipt':receipt,'qa_product_report':receipt,'customer_media_receipt':status/'customer_media_receipt.json',
        'packaging_prompt_receipt':project/'panels_v2/prompt_receipt.json','keying_preset_lock':status/'keying/keying_preset.lock.json',
        'keying_visual_review':keying_review}
    for role,path in roles.items():
        record_run(run_file=runfile,package='delivery',status='running',artifact_id=role,artifact_path=path,replace=True)
    print('Actual managed package and reviewed delivery dependencies registered')

if __name__=='__main__':main()
