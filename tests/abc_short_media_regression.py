"""Opt-in isolated real-media integration. No external generation or real story.

CLI and formal packaging execute; unavailable upstream independent production
assets are explicit synthetic fixture seams, not claimed production approval.
"""
import argparse
from contextlib import ExitStack
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from PIL import Image, ImageDraw
import release_video as release
from story_production_v2 import binding
from story_scene_windows import prepare_plan, audit_video, frame_templates, presenter_evidence
from story_release_policy import release_safety_requirements


def run(output):
    output=Path(output).resolve(); output.mkdir(parents=True,exist_ok=True)
    def ff(*args):
        command=['ffmpeg','-v','error','-y',*map(str,args)]
        subprocess.run(command,check=True,capture_output=True)
    duration=42.
    bg=output/'background.png';Image.new('RGB',(1920,1080),(20,65,100)).save(bg)
    frame=output/'frame.png';im=Image.new('RGBA',(1920,1080));ImageDraw.Draw(im).rectangle((210,270,1120,782),outline=(240,180,15,255),width=30);im.save(frame)
    safe_png=output/'person_safe.png';im=Image.new('RGBA',(320,180));ImageDraw.Draw(im).rectangle((60,30,130,179),fill=(220,35,55,255));im.save(safe_png)
    risk_png=output/'person_risk.png';im=Image.new('RGBA',(320,180));ImageDraw.Draw(im).rectangle((250,30,319,179),fill=(220,35,55,255));im.save(risk_png)
    person=output/'foreground.webm'
    ff('-loop',1,'-framerate',5,'-t',34,'-i',safe_png,
       '-loop',1,'-framerate',5,'-t',2,'-i',risk_png,
       '-loop',1,'-framerate',5,'-t',duration-36+.2,'-i',safe_png,
       '-filter_complex','[0:v][1:v][2:v]concat=n=3:v=1:a=0,format=yuva420p[v]',
       '-map','[v]','-c:v','libvpx-vp9','-lossless',1,'-pix_fmt','yuva420p','-auto-alt-ref',0,person)
    voice=output/'voice.wav';ff('-f','lavfi','-i','sine=frequency=440:sample_rate=24000','-t',duration,voice)
    bgvideo=output/'story.mp4';ff('-f','lavfi','-i','color=c=0x168a37:s=320x180:r=5','-f','lavfi','-i','sine=frequency=220:sample_rate=24000','-t',duration,'-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',bgvideo)
    srt=output/'speech.srt';srt.write_text('1\n00:00:00,000 --> 00:00:41,000\n\n')
    timeline=output/'timeline.json';timeline.write_text(json.dumps({'audio_duration_seconds':duration}))
    run_file=output/'run.json';run_file.write_text('{}')
    ledger={'production_contract':'story-production/v2','project_dir':str(output),'inputs':{'subtitle_srt':binding(srt)},'artifacts':{'authoritative_timeline_receipt':binding(timeline)}}
    plan=output/'windows.json'
    with patch('story_run.load_run',return_value=ledger),patch('story_timeline.validate_authoritative_timeline_receipt'):
        prepare_plan(
            run_file, plan,
            presenter_foreground=person,
            fixed_anchor_x=300,
            canvas_width=1920,
            source_width=320,
            source_height=180,
            rendered_height=1080,
            person_layout_policy='',
        )
    preset=output/'keying.json';preset.write_text('{}')
    panels={}
    for role in ['main_top','main_bottom','library_top','library_bottom']:
        path=output/f'{role}.png';Image.new('RGB',(1080,416),(115,70,40)).save(path);panels[role]=path
    captured={}
    for role in ['confirmed_text','subtitle_txt','final_word','story_requirements']:
        path=output/f'{role}.txt';path.write_text('synthetic fixture '+role);ledger['inputs'][role]=binding(path)
    ledger['inputs']['audio']=binding(voice)
    projection_payload={
        'requirements':release_safety_requirements(source='isolated short-media fixture'),
        'acceptance_evidence':['presenter_body_overflow_report','release_plan_compliance','decoded_video_execution','shared_frame_derivation','independent_visual_review'],
    }
    projection=output/'projection.json';projection.write_text(json.dumps(projection_payload));ledger['artifacts']['requirements_projection']=binding(projection)
    theme=output/'theme_assets_manifest.json';theme.write_text('{}');ledger['artifacts']['theme_assets_manifest']=binding(theme)
    semantic=output/'semantic.json';semantic.write_text(json.dumps({'production_contract':'story-production/v2','schema_version':'story-v2-artifact-semantics-preparation/v1','inputs':ledger['inputs'],'authoritative_timeline_receipt':binding(timeline)}))
    for role in ['semantic_card_generation_receipt','semantic_card_motion_receipt']:
        path=output/f'{role}.json';path.write_text(json.dumps({'artifact_semantic_plan_sha256':binding(semantic)['sha256']}));ledger['artifacts'][role]=binding(path)
    motion_request=output/'semantic_card_motion_request.json';motion_request.write_text(json.dumps({'artifact_semantic_plan_sha256':binding(semantic)['sha256']}))
    customer=output/'customer_media_receipt.json';customer.write_text('{}');ledger['artifacts']['customer_media_receipt']=binding(customer)
    lock=output/'keying_preset.lock.json';lock.write_text('synthetic-lock')
    logo=output/'logo.png';Image.new('RGBA',(80,50),(250,245,220,255)).save(logo)
    demo=output/'demo.json';demo.write_text('{}')
    raw_preview=output/'raw_preview.json'; raw_preview.write_text(json.dumps({'inputs':{k:binding(v) for k,v in {'preset':preset,'background_image':bg,'story_frame':frame,'logo':logo,'foreground':person}.items()}}))
    windows_review=output/'windows_fixture_review.json';windows_review.write_text(json.dumps({'approved':True,'score':90,'critical_errors':[], 'reviewer_context':'synthetic-independent-fixture','independent_context':True,'artifact_sha256':binding(plan)['sha256'],'fixture_boundary':'fabricated upstream review fixture, not actual independent approval'}))
    # Render through real CLI and formal package; only upstream production
    # approvals are fixture seams. Rendering and decoded ABC QA are not mocked.
    geometry={'bindings':{},'schema_version':'fixture','geometry_sha256':'fixture',
              'presenter':{'a':{'source_crop':[0,0,320,180],'rendered_width':1920,'rendered_height':1080,'x':300,'y':0},
                           'c':{'source_crop':[0,0,320,180],'rendered_width':1920,'rendered_height':1080,'x':-380,'y':0}}}
    demo_scan_geometry={
        'keying_lock_sha256':binding(lock)['sha256'],
        'source_native':True,'source_crop':[0,0,320,180],
        'source_width':320,'source_height':180,
        'rendered_width':1920,'rendered_height':1080,'x':300,'y':0,
    }
    spec={'schema_version':release.V2_RELEASE_SPEC_SCHEMA}
    def execute(*args,**kwargs): kwargs['executor']()
    real_compile=release.compile_v2_release_spec
    def compile_capture(*args,**kwargs):
        value=real_compile(*args,**kwargs);(output/'actual_compiled_spec.json').write_text(json.dumps(value,ensure_ascii=False,indent=2));return value
    def manifests(config,spec,outputs,geometry):
        captured['config']=config
        return {'synthetic_fixture':True,'outputs':[binding(p) for p in outputs]}
    argv=['release_video.py','--story-name','isolated regression','--duration-text','42秒','--age-text','fixture',
          '--bg-video',str(bgvideo),'--bg-image',str(bg),'--output-dir',str(output/'release'),'--variant','main',
          '--audio-mix',str(voice),'--frame-image',str(frame),'--story-box','210,270,910,512',
          '--person-x','300',
          '--keying-preset-json',str(preset),'--run-file',str(run_file),'--release-windows-plan',str(plan),
          '--approved-preview-geometry',str(output/'fixture_geometry.json'),'--artifact-semantic-plan',str(semantic),'--demo-render-manifest',str(demo),'--story-logo',str(logo),'--antipiracy-logo',str(logo),'--producer-context','fixture-producer','--release-windows-review',str(windows_review),'--subtitle-srt',str(srt),'--mix-bg-audio','--preset','ultrafast','--crf','28']
    patches = [
        patch.object(sys,'argv',argv), patch('story_run.load_run',return_value=ledger),
        patch.object(release,'preflight_release_requirements'),
        patch.object(release,'load_keying_preset',return_value={'keyer':'rvm','rvm_foreground_video':str(person),'person_grade':'none','rvm_input_width':320,'rvm_input_height':180}),
        patch.object(release,'compile_v2_release_spec',side_effect=compile_capture),
        patch('story_requirements.validate_run_projection',return_value=projection_payload),
        patch('story_run.validate_theme_assets_manifest',return_value={'artifacts':{'story_frame_png':binding(frame)}}),
        patch('story_timeline.validate_authoritative_timeline_receipt',return_value={'audio_duration_seconds':duration}),
        patch('story_customer_media.validate_customer_media_receipt',return_value={'artifacts':{'product_background_without_subtitles':binding(bgvideo)}}),
        patch('semantic_card_motion.semantic_card_generation_receipt_issues',return_value=[]),
        patch('semantic_card_motion.semantic_card_motion_receipt_issues',return_value=[]),
        patch('story_media_preview.load_approved',return_value=({'preview':binding(raw_preview)},demo_scan_geometry)),
        patch('story_media_preview.validate_preview',return_value=json.loads(raw_preview.read_text())),
        patch.object(release,'keying_preset_lock_issues',return_value=[]),
        patch.object(release,'apply_release_contract_spec',side_effect=lambda c,s:c),
        patch.object(release,'validate_config'), patch.object(release,'compile_release_geometry',return_value=geometry),
        patch.object(release,'geometry_manifest_issues',return_value=[]),
        patch.object(release,'render_static_assets',return_value={'frame':frame,**panels}),
        patch.object(release,'_execute_release_layout',side_effect=execute),
        patch.object(release,'build_release_render_manifest',side_effect=manifests),
        patch.object(release,'release_render_manifest_issues',return_value=[]),
    ]
    with ExitStack() as stack:
        for active_patch in patches:
            stack.enter_context(active_patch)
        release.main()
    good=json.loads((output/'release/main_abc_coverage.json').read_text())
    assert good['passed'] and {x['observed_mode'] for x in good['samples']} == {'a','b','c'}
    # The second actual encode simulates the observed production defect:
    # valid intended plan, renderer received no switching windows.
    bad=output/'all_a_fault.mp4';config=captured['config']
    release.render_main_wide(replace(config,b_windows=(),c_windows=()),frame,bad,
        presenter_geometry=geometry['presenter']['a'],presenter_c_geometry=geometry['presenter']['c'])
    failed=audit_video(bad,plan,frame_templates(config,output/'fault_templates'),output/'all_a_fault_qa.json',presenter=presenter_evidence(config,geometry['presenter']['c']))
    assert not failed['passed'] and any(x['expected_mode']=='b' and x['observed_mode']=='a' for x in failed['samples'])
    # Actual v2 preview uses the window-filter graph, including nonzero seek
    # offsets, and measures each decoded boundary image without forcing a mode.
    previews=[]
    for timestamp,mode in [(14.92,'c'),(15.08,'b'),(32.92,'b'),(33.08,'a')]:
        path=output/f'preview_{timestamp:.2f}_{mode}.png'
        release.render_main_preview_frame(config,frame,path,output,timestamp,mode,
            {**geometry,'production_contract':'story-production/v2'},panels['main_top'],panels['main_bottom'])
        previews.append(binding(path))
    # Missing/blank presenter cannot qualify as C even when both frames vanish.
    blank=output/'blank_fault.mp4';ff('-f','lavfi','-i','color=c=0x144164:s=320x180:r=5','-t',duration,'-c:v','libx264','-preset','ultrafast',blank)
    blank_report=audit_video(blank,plan,frame_templates(config,output/'blank_templates'),output/'blank_fault_qa.json',presenter=presenter_evidence(config,geometry['presenter']['c']))
    assert not blank_report['passed'] and all(x['observed_mode']=='unknown' for x in blank_report['samples'] if x['expected_mode']=='c')
    # Library renderer is deliberately independent of main ABC planning.
    lib=output/'library_window.mp4';tail=output/'tail.png';Image.new('RGBA',(1080,608)).save(tail)
    release.render_library_window_video(bgvideo,None,tail,lib,replace(config,variant='library',b_windows=(),c_windows=()),sample_start=0.,sample_duration=1.)
    probe=subprocess.run(['ffmpeg','-v','error','-i',str(lib),'-f','null','-'],capture_output=True)
    assert probe.returncode==0
    plan_payload=json.loads(plan.read_text())
    added_b=[row for row in plan_payload['presenter_protection']['severe_windows']
             if any(start < row[1] and end > row[0] for start,end in release.parse_b_windows(plan_payload['b_windows']))
             and not any(start < row[1] and end > row[0] for start,end in release.parse_b_windows(plan_payload['base_windows']['b_windows']))]
    assert added_b and all(not row['uncovered'] for row in plan_payload['presenter_protection']['coverage'])
    summary={'passed':True,'duration_seconds':duration,'formal_cli_auto_actual_modes':sorted({x['observed_mode'] for x in good['samples']}),
             'presenter_risk_windows_added_to_b':added_b,
             'valid_plan_all_a_render_detected':True,'blank_c_rejected':True,'actual_preview_boundary_frames':previews,'library_real_window_decode':binding(lib),
             'good_qa':binding(output/'release/main_abc_coverage.json'),'fault_qa':binding(output/'all_a_fault_qa.json'),
             'upstream_fixture_seams':['preflight_release_requirements','story_run.load_run','load_keying_preset','validate_run_projection','validate_authoritative_timeline_receipt','validate_customer_media_receipt','semantic_card_generation_receipt_issues','semantic_card_motion_receipt_issues','load_approved','validate_preview','keying_preset_lock_issues','apply_release_contract_spec identity','validate_config','compile_release_geometry','geometry_manifest_issues','render_static_assets','_execute_release_layout direct executor','build_release_render_manifest','release_render_manifest_issues'],
             'real_components':['compile_v2_release_spec including current file hashes, windows, auto rules and independent-review hash validation','release CLI auto arguments','formal package_release_videos','RVM alpha decode','A/B/C ffmpeg render graph','vertical package encode','decoded frame geometry audit','library window renderer'],
             'not_claimed':'new-story production acceptance or independent approval of synthetic inputs'}
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    print(json.dumps(summary,ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);run(parser.parse_args().output)
