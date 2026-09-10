"""Prepare synthetic Alpha upstream input; run REAL existing keying evidence QA.

Does not claim RVM inference quality. Model path is explicit, never story-specific.
"""
from pathlib import Path
import json, subprocess, sys, shutil
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from PIL import Image
from tests.test_keying_quality import _silhouette
from story_production_v2 import binding, write
from story_evidence import write_review_bundle
from keying_quality import write_evidence_assets
from rvm_keying import validate_rvm_model, RVM_RECEIPT_SCHEMA_VERSION, RVM_BACKEND_VERSION


def main():
    root=Path(sys.argv[1]).resolve();model_source=Path(sys.argv[2]).resolve();runtime=Path(sys.argv[3]).resolve()
    model_sha=validate_rvm_model(model_source)
    project=root/'fixture';out=project/'99_项目状态/keying';out.mkdir(parents=True,exist_ok=True)
    model=out/'model.onnx';shutil.copyfile(model_source,model)
    rgba=Image.new('RGBA',(640,360),(0,0,0,0));rgba.alpha_composite(_silhouette().resize((280,315)),(180,20));rgba.save(out/'alpha.png')
    source=out/'greenscreen.mp4';foreground=out/'foreground.webm'
    green=Image.new('RGBA',rgba.size,(0,255,0,255));green.alpha_composite(rgba);green.convert('RGB').save(out/'green.png')
    for image,target,codec in [(out/'green.png',source,'libx264'),(out/'alpha.png',foreground,'libvpx-vp9')]:
        subprocess.run(['ffmpeg','-v','error','-y','-loop','1','-framerate','24','-i',str(image),'-t','3','-c:v',codec,*(['-pix_fmt','yuva420p','-auto-alt-ref','0'] if codec=='libvpx-vp9' else []),str(target)],check=True)
    receipt=out/'rvm_receipt.json'
    write(receipt,{'schema_version':RVM_RECEIPT_SCHEMA_VERSION,'status':'complete','backend_version':RVM_BACKEND_VERSION,'model_sha256':model_sha,'temporal_recurrence_used':True,'alpha_channel_verified':True,'frame_count':72,'output_video':str(foreground),'output_sha256':binding(foreground)['sha256'],'source_sha256':binding(source)['sha256'],'fixture_boundary':'SIMULATED upstream temporal RVM contract. Actual alpha is synthetic. No inference quality claim.','production_eligible':False})
    search=out/'keying_search.json';write(search,{'source_video':str(source),'candidates':[{'id':'offline-alpha'}],'fixture_boundary':'Offline synthetic Alpha'})
    preset=out/'keying_preset.json';qa=out/'keying_machine_qa.json';evidence=out/'evidence/evidence_manifest.json'
    settings={'preset_version':'story-keying-preset/v2','keying_candidate':'offline-alpha','keying_search':str(search),'machine_qa':str(qa),'evidence_manifest':str(evidence),
        'keyer':'rvm','chroma_color':'0x00FF00','chroma_similarity':.1,'chroma_blend':0,'person_grade':'none','person_beauty':'none','person_crop':None,
        'detected_person_bbox':[206,37,231,293],'person_height_ratio':.82,'story_box':[210,270,910,512],
        'rvm_model_path':str(model),'rvm_model_sha256':model_sha,'rvm_runtime_path':str(runtime),
        'rvm_foreground_video':str(foreground),'rvm_foreground_sha256':binding(foreground)['sha256'],'rvm_receipt_path':str(receipt),'rvm_receipt_sha256':binding(receipt)['sha256'],'rvm_input_width':640,'rvm_input_height':360,'rvm_downsample_ratio':.4,'rvm_alpha_choke_pixels':0,
        'presenter_initial_anchor_frame_seconds':0,'presenter_gesture_review_frame_seconds':1.5}
    write(preset,settings)
    candidate,qa,_=write_evidence_assets(out/'green.png',out/'green.png',chroma_color='0x00FF00',similarity=.1,blend=0,output_dir=out/'evidence',candidate_id='offline-alpha',machine_qa_path=qa,preset=settings,preset_path=preset)
    report=json.loads(qa.read_text())
    if not report['passed']:raise ValueError(report['critical_errors'])
    bundle=write_review_bundle(out/'review_bundle.json',[preset,qa,evidence,candidate,out/'evidence/standing_foreground.png',out/'evidence/wide_gesture_foreground.png'])
    write(out/'independent_review_request.json',{'artifact':binding(bundle),'review_output':str(out/'independent_review.json'),'scope':'Synthetic Alpha fixture: inspect actual standing/wide-gesture frames and silhouette edge/transparent background; cannot validate real RVM inference or真人一致性','producer_context':'offline-fixture-producer'})
    runpath=project/'99_项目状态/story_run.json';run=json.loads(runpath.read_text());run['inputs']['greenscreen_video']=binding(source);write(runpath,run)
    print(out/'independent_review_request.json')

if __name__=='__main__':main()
