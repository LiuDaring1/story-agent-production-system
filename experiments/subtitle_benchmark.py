"""Offline same-settings comparison; never selects a production backend automatically."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import resource
import re
import subprocess
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from story_subtitle_layers import crop_subtitle_layers, cropped_overlay_chain
from story_video_synthesizer.pipeline import SynthesisConfig, _render_subtitle_images, _subtitle_overlay_chain
from story_video_synthesizer.subtitles import SubtitleCue


def measured(args):
    start = time.monotonic()
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    _, stderr = process.communicate()
    if process.returncode:
        raise RuntimeError(stderr.decode()[-4000:])
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {'wall_seconds': time.monotonic()-start, 'peak_rss_bytes_cumulative_child_max': usage.ru_maxrss if sys.platform == 'darwin' else usage.ru_maxrss*1024,
            'block_inputs_cumulative': usage.ru_inblock, 'block_outputs_cumulative': usage.ru_oublock}


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--cues',type=int,default=57);p.add_argument('--seconds',type=float,default=6);a=p.parse_args()
    root=a.output.resolve();root.mkdir(parents=True,exist_ok=True)
    cfg=SynthesisConfig(root,root/'text',root/'voice',root/'music',root,width=1920,height=1080,fps=30,x264_preset='medium',x264_crf=20)
    cues=[SubtitleCue(i+1,f'字幕内容 第{i+1}行',i*a.seconds/a.cues,(i+1)*a.seconds/a.cues) for i in range(a.cues)]
    start=time.monotonic();images=_render_subtitle_images(cues,root/'images',cfg); preparation=time.monotonic()-start
    source=root/'source.mp4'
    measured(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=1920x1080:rate=30','-t',str(a.seconds),'-c:v','libx264','-preset','medium','-crf','20',str(source)])
    crop_started=time.monotonic()
    layers=crop_subtitle_layers(images,width=cfg.width,height=cfg.height)
    crop_preparation=time.monotonic()-crop_started
    crops=[layer[0] for layer in layers]
    results={}
    for name,paths,graph in [('existing',images,_subtitle_overlay_chain(cues,1)),('cropped',crops,cropped_overlay_chain(cues,layers))]:
        cmd=['ffmpeg','-v','error','-y','-i',str(source)]
        for path in paths:cmd+=['-loop','1','-i',str(path)]
        cmd+=['-filter_complex_threads','1','-filter_complex',graph,'-map','[v]','-t',str(a.seconds),'-an','-c:v','libx264','-preset','medium','-crf','20','-pix_fmt','yuv420p',str(root/f'{name}.mp4')]
        # /usr/bin/time records each encoder separately, unlike cumulative RUSAGE_CHILDREN.
        stats=root/f'{name}.time.txt'
        results[name]=measured(['/usr/bin/time','-l','-o',str(stats),*cmd])
        results[name]['native_resource_report']=str(stats)
        native=stats.read_text()
        results[name]['peak_rss_bytes']=int(re.search(r'(\d+)\s+maximum resident set size',native)[1])
        results[name]['peak_memory_footprint_bytes']=int(re.search(r'(\d+)\s+peak memory footprint',native)[1])
        results[name]['output_bytes']=(root/f'{name}.mp4').stat().st_size
        subprocess.run(['ffmpeg','-v','error','-i',str(root/f'{name}.mp4'),'-f','framemd5',str(root/f'{name}.framemd5')],check=True)
    same=(root/'existing.framemd5').read_bytes()==(root/'cropped.framemd5').read_bytes()
    if not same:
        comparison = subprocess.run(['ffmpeg','-v','info','-i',str(root/'existing.mp4'),'-i',str(root/'cropped.mp4'),'-lavfi','psnr','-f','null','-'],capture_output=True,text=True,check=True)
        (root/'decoded_psnr.txt').write_text(comparison.stderr)
    result={'settings':{'width':1920,'height':1080,'fps':30,'preset':'medium','crf':20,'seconds':a.seconds,'cues':a.cues},'cue_sha256':hashlib.sha256(json.dumps([asdict(c) for c in cues],ensure_ascii=False).encode()).hexdigest(),'preparation_seconds':preparation,'crop_preparation_seconds':crop_preparation,'results':results,'all_decoded_frames_identical':same,'production_enabled':same,'scope':'offline synthetic video; no audio path changed; no paid generation; native time reports per-process RSS and block IO, cumulative fields labelled explicitly'}
    (root/'report.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
if __name__=='__main__':main()
