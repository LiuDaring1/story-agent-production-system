"""Run actual Demo preview after independent Alpha fixture approval."""
from pathlib import Path
import json, subprocess, sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from PIL import Image, ImageDraw
from story_production_v2 import binding, write
from keying_quality import lock_keying_preset
from story_requirements import write_projection
from story_run import record_run

def main():
    root=Path(sys.argv[1]).resolve();project=root/'fixture';status=project/'99_项目状态';keying=status/'keying';runfile=status/'story_run.json';run=json.loads(runfile.read_text())
    lock_keying_preset(keying/'keying_preset.json',machine_qa_path=keying/'keying_machine_qa.json',evidence_manifest_path=keying/'evidence/evidence_manifest.json',review_bundle_path=keying/'review_bundle.json',review_path=keying/'independent_review.json')
    assets=project/'assets';assets.mkdir(exist_ok=True)
    Image.new('RGB',(1920,1080),(195,213,225)).save(assets/'background.png')
    frame=Image.new('RGBA',(1920,1080),(0,0,0,0));d=ImageDraw.Draw(frame);d.rectangle((205,265,1125,787),outline=(133,99,60,255),width=14);frame.save(assets/'frame.png')
    logo=Image.new('RGBA',(150,90),(180,110,40,255));d=ImageDraw.Draw(logo);d.text((15,35),'LOGO',fill='white');logo.save(assets/'logo.png')
    source=Path(__file__).resolve().parents[2]/'skills/story-full-auto/references/video-invariants.md'
    projection=status/'requirements_projection.json'
    write_projection(projection,scope='offline-full-media-fixture',inputs={'confirmed_text':Path(run['inputs']['confirmed_text']['path'])},rule_sources=[(source,'current')],applicability={'accounts':['main','library'],'artifacts':['main_release_video','library_release_video'],'shots':['A']},requirements=[{'requirement_id':'media-invariants','text':'Preserve current source-native geometry, actual alpha, official test logo and both audio sources','source':str(source),'scope':'main/library media', 'requirement':'Preserve current source-native geometry, actual alpha, official test logo and both audio sources'}],executable_checks=[{'parameter':'variant','operator':'equals','expected':'both'}],acceptance_evidence=['real preview frames','independent test review','actual final QA'])
    record_run(run_file=runfile,package='media_render',status='running',artifact_id='requirements_projection',artifact_path=projection,replace=True)
    request={'producer_context':'offline-fixture-producer','requirements_projection':binding(projection),'inputs':{k:binding(p) for k,p in {'preset':keying/'keying_preset.json','background_image':assets/'background.png','story_frame':assets/'frame.png','logo':assets/'logo.png','background_with_subtitles':project/'background_with_formal.mp4','background_without_subtitles':project/'background_without_formal.mp4','body_srt':project/'body.srt','timeline_receipt':status/'timeline.json'}.items()}}
    write(status/'media_request.json',request)
    result=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[2]/'story_pipeline.py'),'media-preview','--run-file',str(runfile),'--request',str(status/'media_request.json')],capture_output=True,text=True);(root/'media-preview.log').write_text(result.stdout+result.stderr)
    if result.returncode:raise RuntimeError(result.stderr)
    write(status/'media_preview_review_request.json',{'artifact':binding(status/'media_preview.json'),'review_output':str(status/'media_preview_review.json'),'producer_context':'offline-fixture-producer','scope':'Synthetic silhouette Demo preview; check actual first/middle/last frames, source geometry, alpha edges, test logo and subtitle. Not real-person consistency or real story acceptance.'})
    print(status/'media_preview_review_request.json')
if __name__=='__main__':main()
