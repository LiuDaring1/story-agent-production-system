"""Actual native-v2 dual release preview/render. Reviews are external and required."""
from pathlib import Path
import json,subprocess,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from story_production_v2 import binding,write,validate_independent_approval

def main():
    root=Path(sys.argv[1]).resolve();mode=sys.argv[2];project=root/'fixture';status=project/'99_项目状态';panels=project/'panels_v2';runfile=status/'story_run.json';run=json.loads(runfile.read_text())
    review=json.loads((panels/'independent_review.json').read_text());validate_independent_approval(review,panels/'review_bundle.json',producer_context='offline-fixture-producer')
    request=json.loads((panels/'independent_review_request.json').read_text())
    generation=panels/'generation_receipt.json'
    write(generation,{'schema_version':'story-confirmed-panels/v2','prompt_receipt_sha256':binding(panels/'prompt_receipt.json')['sha256'],'reference_attached':True,'imagegen_native':True,'request_id':'offline-mock-panels-v2','producer_context':'offline-fixture-producer','checks':review['checks'],'scope_checks':review['scope_checks'],'review':binding(panels/'independent_review.json'),'review_bundle':binding(panels/'review_bundle.json'),'outputs':request['outputs'],'fixture_boundary':'SIMULATED upstream native-provider response; all quality findings come from actual independent fixture review','production_eligible':False})
    mix=project/'release_mix.wav'
    if not mix.exists():
        subprocess.run(['ffmpeg','-v','error','-y','-i',run['inputs']['audio']['path'],'-i',run['inputs']['finished_music']['path'],'-filter_complex','[0:a][1:a]amix=inputs=2:weights=1 0.22:normalize=0[a]','-map','[a]','-c:a','pcm_s16le','-t','3',str(mix)],check=True)
    command=[sys.executable,str(Path(__file__).resolve().parents[2]/'story_pipeline.py'),'release','--run-file',str(runfile),'--story-name','FIXTURE','--story-type','测试故事','--duration-text','3秒','--age-text','6岁','--bg-video',str(project/'master.mp4'),'--output-dir',str(project/'04_发布视频'),'--variant','both','--bg-image',str(project/'assets/background.png'),'--keying-preset-json',str(status/'keying/keying_preset.json'),'--audio-mix',str(mix),'--story-logo',str(project/'assets/logo.png'),'--antipiracy-logo',str(project/'assets/logo.png'),'--frame-image',str(project/'assets/frame.png'),'--story-box','210,270,910,512','--main-top-panel',str(panels/'top_plate.png'),'--main-bottom-panel',str(panels/'bottom_plate.png'),'--library-top-panel',str(panels/'library_top_plate.png'),'--library-bottom-panel',str(panels/'library_bottom_plate.png'),'--main-package-spec',str(panels/'prompt_receipt.json'),'--main-package-receipt',str(generation),'--artifact-semantic-plan',str(project/'cards/artifact_semantic_plan.json'),'--demo-render-manifest',str(status/'approved_demo.json'),'--subtitle-srt',run['inputs']['subtitle_srt']['path'],'--producer-context','offline-fixture-producer','--crf','20','--preset','medium','--tail-seconds','0.5']
    preview=status/'release_preview';geometry=preview/'release_geometry_manifest_both.json'
    if mode=='preview':command+=['--preview-dir',str(preview),'--preview-times','0,1.5,2.8']
    elif mode=='render':command+=['--approved-preview-geometry',str(geometry),'--approved-preview-review',str(status/'release_preview_review.json')]
    else:raise ValueError('preview or render')
    result=subprocess.run(command,capture_output=True,text=True);(root/f'release-{mode}.log').write_text(result.stdout+result.stderr)
    if result.returncode:raise RuntimeError(result.stderr)
    if mode=='preview':
        write(status/'release_preview_review_request.json',{'artifact':binding(geometry),'frames':[binding(p) for p in sorted(preview.glob('*.png'))],'review_output':str(status/'release_preview_review.json'),'producer_context':'offline-fixture-producer','scope':'Actual native-v2 release preview using synthetic fixture upstream: inspect dual-account panels, center video, silhouette, logo/subtitles. No real-story or actual service quality claim.'})
        print(status/'release_preview_review_request.json')
    else:print(project/'04_发布视频/release_render_manifest_both.json')
if __name__=='__main__':main()
