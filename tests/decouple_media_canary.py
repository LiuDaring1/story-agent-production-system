"""Offline v2 success-chain canary. Synthetic inputs/review fixtures, real render/QA.
Run explicitly with --model (installed pinned RVM weights), --runtime and --output.
No download, service call or real story input is used.
"""
from pathlib import Path
import argparse
import json
import os
import subprocess
import sys
import time
from PIL import Image, ImageDraw
from story_production_v2 import binding, sha, write, ADVANCED_ROLES


def run_canary(root, model, runtime):
    from story_run import init_run, load_run
    from story_timeline import write_authoritative_timeline_receipt
    from story_requirements import write_projection
    from tests.test_keying_quality import _locked_fixture
    from keying_quality import lock_keying_preset, keying_preset_lock_issues
    from production_keying import production_keying_fingerprint, production_keying_contract
    from story_evidence import write_review_bundle
    from rvm_keying import render_rvm_foreground_video
    root.mkdir(parents=True, exist_ok=True)
    env = {**os.environ,'STORY_ENCODE_STATE_DIR':str(root/'pool')}
    env.pop('STORY_TASK_ID',None)
    commands=[]
    def command(args, label):
        started=time.monotonic()
        result=subprocess.run([str(a) for a in args],env=env,capture_output=True,timeout=180)
        (root/(label+'.log')).write_bytes(result.stdout+b'\n'+result.stderr)
        commands.append({'label':label,'command':[str(a) for a in args],'returncode':result.returncode,'seconds':time.monotonic()-started})
        if result.returncode:
            raise RuntimeError(f'{label}: {result.stderr.decode()[-2500:]}')
        return result
    project=root/'project'; project.mkdir()
    status=project/'99_项目状态'; status.mkdir()
    key=project/'key'; key.mkdir()
    preset, lock, candidate, hand, green = _locked_fixture(key,keyer='rvm')
    # Illustrated synthetic presenter, not a user or historical performer.
    source_image=key/'synthetic.png'
    picture=Image.new('RGB',(320,180),(0,200,0));draw=ImageDraw.Draw(picture)
    draw.ellipse((205,15,245,55),fill=(210,160,120));draw.rectangle((190,52,255,140),fill=(40,60,200));draw.rectangle((175,60,270,76),fill=(210,160,120));draw.rectangle((197,137,214,179),fill=(40,40,60));draw.rectangle((232,137,250,179),fill=(40,40,60));picture.save(source_image)
    command(['ffmpeg','-v','error','-y','-loop','1','-i',source_image,'-t','2','-r','25','-c:v','libx264','-pix_fmt','yuv420p',green],'synthetic-green')
    voice=root/'voice.wav';music=root/'finished.wav'
    for p,f,d in [(voice,440,2),(music,220,3)]:
        command(['ffmpeg','-v','error','-y','-f','lavfi','-i',f'sine=frequency={f}:duration={d}:sample_rate=48000',p],p.stem)
    foreground=key/'foreground.webm';rvm_receipt=key/'rvm.json'
    render_rvm_foreground_video(green,foreground,rvm_receipt,model_path=model,runtime_path=runtime,width=320,height=180,fps=25,downsample_ratio=.4)
    data=json.loads(preset.read_text());data.update(rvm_model_path=str(model),rvm_runtime_path=str(runtime),rvm_foreground_video=str(foreground),rvm_foreground_sha256=sha(foreground),rvm_receipt_path=str(rvm_receipt),rvm_receipt_sha256=sha(rvm_receipt),person_height_ratio=1.0,person_grade='none',person_beauty='none')
    write(preset,data)
    # Upstream search/QA/reviewer records are explicit synthetic contract fixtures.
    # Actual model receipt above is produced by the local RVM implementation.
    evidence=key/'evidence_manifest.json';qa=key/'keying_machine_qa.json'
    renderer={'preset_sha256':sha(preset),'filter_fingerprint':production_keying_fingerprint(data),'filter_contract':production_keying_contract(data)}
    e=json.loads(evidence.read_text());e.update(renderer,fixture_only=True);write(evidence,e)
    q=json.loads(qa.read_text());q.update(renderer,evidence_manifest_sha256=sha(evidence),fixture_only=True);write(qa,q)
    bundle=write_review_bundle(key/'keying_review_bundle.json',[preset,qa,evidence,hand])
    review=key/'keying_review_review.json';write(review,dict(approved=True,score=95,critical_errors=[],artifact_sha256=sha(bundle),fixture_only=True))
    lock_keying_preset(preset,machine_qa_path=qa,evidence_manifest_path=evidence,review_bundle_path=bundle,review_path=review)
    assert not keying_preset_lock_issues(preset)
    txt=root/'subtitle.txt';txt.write_text('测试字幕\n',encoding='utf-8')
    timings=root/'timings.json';write(timings,[dict(index=1,line='测试字幕',source_start=0,source_end=2,duration=2,timeline_start=0,timeline_end=2)])
    metadata=root/'alignment.json';write(metadata,dict(model='synthetic-fixture-alignment',timed_char_count=4,alignment_mode='whisper',fixture_only=True))
    fullsrt=root/'full.srt';timeline=root/'timeline.json'
    write_authoritative_timeline_receipt(receipt_path=timeline,source_kind='whisper_confirmed_line_timings',timings_path=timings,alignment_metadata_path=metadata,subtitle_txt=txt,authoritative_audio=voice,alignment_audio=voice,output_srt=fullsrt)
    background=root/'background.png';Image.new('RGB',(1920,1080),(110,150,180)).save(background)
    frame=root/'frame.png';Image.new('RGBA',(1920,1080),(0,0,0,0)).save(frame)
    logo=root/'logo.png';Image.new('RGBA',(150,60),(220,70,20,255)).save(logo)
    word=root/'final.docx'
    import zipfile
    with zipfile.ZipFile(word,'w') as z:z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>测试字幕。</w:t></w:r></w:p></w:body></w:document>')
    requirement=root/'requirements.txt';requirement.write_text('保留时间轴、字幕、Logo、完整 Alpha 画布及已审几何。')
    prompt=root/'prompt.txt';prompt.write_text('确认参考 {story_name}')
    runfile=status/'story_run.json'
    init_run(run_file=runfile,project_dir=project,confirmed_text=txt,greenscreen_video=green,audio=voice,subtitle_txt=txt,production_inputs={'subtitle_srt':fullsrt,'final_word':word,'finished_music':music,'story_requirements':requirement,'packaging_reference':background,'packaging_prompt':prompt})
    run=load_run(runfile)
    projection=root/'projection.json'
    write_projection(projection,scope='media_render',inputs={k:Path(v['path']) for k,v in run['inputs'].items()},rule_sources=[(Path('skills/story-full-auto/references/video-invariants.md').resolve(),'candidate-v2')],applicability={'artifacts':['product_demo','main_release_video','library_release_video'],'accounts':['main','library']},requirements=[dict(requirement_id='video',source='video-invariants',scope='media',requirement=requirement.read_text())],executable_checks=[],acceptance_evidence=['customer_media_receipt'])
    run['artifacts']['requirements_projection']=binding(projection);write(runfile,run)
    visual=project/'visual.mp4'
    command(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=0x406080:s=1920x1080:r=25:d=2','-c:v','libx264','-preset','ultrafast',visual],'visual')
    assembly=root/'assembly.json';write(assembly,{'shots':[{'shot_id':'shot-001','authoritative_line_start':1,'authoritative_line_end':1}]})
    withsub=project/'with.mp4';without=project/'without.mp4';body=project/'body.srt'
    background_command = [sys.executable,'render_customer_backgrounds.py','--run-file',runfile,'--visual-master',visual,'--music',music,'--authoritative-timeline-receipt',timeline,'--assembly-plan',assembly,'--output-with-subtitles',withsub,'--output-without-subtitles',without,'--body-srt',body,'--render-receipt',status/'backgrounds.json','--work-dir',project/'background-work']
    # Slow preset makes the short real render cancellable without fake processes.
    background_command += ['--preset','veryslow']
    child=subprocess.Popen([str(a) for a in background_command],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    target=project/'background-work'/'customer_subtitled_silent.mp4'
    import hashlib
    state_path=root/'pool'/(hashlib.sha256(str(target).encode()).hexdigest()+'.json')
    deadline=time.monotonic()+30
    try:
        while time.monotonic()<deadline:
            if state_path.exists() and json.loads(state_path.read_text()).get('status')=='running':break
            if child.poll() is not None:raise RuntimeError('Background render ended before cancellation observation')
            time.sleep(.01)
        state=json.loads(state_path.read_text())
        assert state['task']==run['run_id']
        # The shared controller is exactly what story_pipeline encode-control calls.
        from unittest.mock import patch
        from story_encode import control_encode
        with patch.dict(os.environ,{'STORY_ENCODE_STATE_DIR':str(root/'pool')}):
            try:
                control_encode(target,action='cancel',expected_fingerprint=state['fingerprint'],expected_task='unrelated-task')
            except ValueError:pass
            else:raise AssertionError('Unrelated task cancelled actual background renderer')
            control_encode(target,action='cancel',expected_fingerprint=state['fingerprint'],expected_task=run['run_id'])
        stdout,stderr=child.communicate(timeout=20)
        (root/'backgrounds-cancel.log').write_bytes(stdout+b'\n'+stderr)
        assert child.returncode!=0 and not target.exists()
        assert json.loads(state_path.read_text())['status']=='cancelled'
        control_request=root/'resume-request.json'
        write(control_request,dict(output=str(target),action='resume',expected_fingerprint=state['fingerprint']))
        command([sys.executable,'story_pipeline.py','encode-control','--run-file',runfile,'--request',control_request],'backgrounds-resume')
    finally:
        if child.poll() is None:
            child.terminate();child.communicate(timeout=10)
    command(background_command,'backgrounds')
    assert json.loads(state_path.read_text())['status']=='completed'

    request={'producer_context':'synthetic-producer','inputs':{k:binding(p) for k,p in dict(preset=preset,background_image=background,story_frame=frame,logo=logo,background_with_subtitles=withsub,background_without_subtitles=without,body_srt=body,timeline_receipt=timeline).items()},'requirements_projection':binding(projection)}
    def operation(name,r):
        p=root/(name+'-request.json');write(p,r)
        result=command([sys.executable,'story_pipeline.py',name,'--run-file',runfile,'--request',p],name)
        return json.loads(result.stdout)
    original_inputs={k:sha(v['path']) for k,v in run['inputs'].items()}
    preview=operation('media-preview',request)
    previewpath=status/'media_preview.json'
    approval=root/'preview-review.json'
    write(approval,dict(independent_context=True,reviewer_context='synthetic-independent-fixture',approved=True,score=95,critical_errors=[],artifact_sha256=sha(previewpath),fixture_only=True))
    approved=status/'approved-demo.json'
    operation('media-approve',dict(preview=str(previewpath),review=str(approval),output=str(approved)))
    result=operation('media',{**request,'approved_demo':binding(approved)})
    pool_records={p.name:p.read_bytes() for p in (root/'pool').glob('*.json') if 'fingerprint' in p.read_text()}
    assert pool_records and all(json.loads(v)['task']==run['run_id'] for v in pool_records.values())
    material=root/'fixture-materials.zip'
    with zipfile.ZipFile(material,'w') as z:z.writestr('README.txt','Synthetic upstream PPT material fixture; no PPTX.')
    sources=dict(customer_manuscript=str(word),music=str(music),demo=result['demo']['path'],background_image=str(background),background_video_with_subtitles=str(withsub),background_video_without_subtitles=str(without),a_only_video=result['a_only_video']['path'],ppt_materials=str(material))
    assert set(sources)==set(ADVANCED_ROLES)
    packrequest=dict(output_root=str(project/'customer'),receipt=str(status/'pack.json'),sources=sources,story_name='隔离夹具')
    packed=operation('pack',packrequest)
    extra=Path(packed['artifacts'][0]['path']).parent/'用户添加.txt';extra.write_bytes(b'keep in place')
    operation('pack',packrequest)
    assert extra.read_bytes()==b'keep in place'
    assert pool_records=={p.name:p.read_bytes() for p in (root/'pool').glob('*.json') if 'fingerprint' in p.read_text()}
    assert original_inputs=={k:sha(v['path']) for k,v in run['inputs'].items()}
    report={'passed':True,'fixture_only':True,'human_visual_acceptance':False,'commands':commands,'media':result,'preview_frame_count':len(preview['artifacts']),'actual_background_entry_cancel_resume':True,'real_rvm_receipt':binding(rvm_receipt),'encode_receipts':len(pool_records),'all_encodes_owned_by_run':True,'music_duration':3,'narration_duration':2,'music_duration_gate_removed':True,'source_hashes_unchanged':True,'repack_no_encode':True,'user_file_preserved':binding(extra),'limitations':['Synthetic upstream timeline, keying review and Demo independent-review receipts; no real story or human visual approval.','PPT materials is an upstream ZIP fixture; export correctness covered separately.','No full dual-account release canary in this test.']}
    write(root/'report.json',report)
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--model',type=Path,required=True);p.add_argument('--runtime',type=Path,required=True);a=p.parse_args()
    print(json.dumps(run_canary(a.output.resolve(),a.model.resolve(),a.runtime.resolve()),ensure_ascii=False,indent=2))
