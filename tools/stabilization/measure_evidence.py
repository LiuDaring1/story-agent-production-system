"""Identical offline member/hash workload; run once against each source version."""
import argparse, contextlib, hashlib, io, json, sys, time
from pathlib import Path
from unittest.mock import patch
p=argparse.ArgumentParser();p.add_argument('--source',required=True,type=Path);p.add_argument('--fixture',required=True,type=Path);p.add_argument('--output',required=True,type=Path);a=p.parse_args()
sys.path.insert(0,str(a.source))
import story_run
from story_work_observation import operation_observation
from story_production_v2 import binding
root=a.output;root.mkdir(parents=True,exist_ok=True)
fixture=a.fixture;fixture.mkdir(parents=True,exist_ok=True)
frames=fixture/'frames';frames.mkdir(exist_ok=True)
for i in range(5000):
    file=frames/f'subtitle_{i:05}.png'
    if not file.exists():file.write_bytes(b'fixed-member-bytes-'+str(i).encode())
for name in ['text.txt','video.mp4','audio.mp3']:
    if not (fixture/name).exists():(fixture/name).write_bytes(b'fixed-input')
large=fixture/'hash-fixture.bin'
if not large.exists():large.write_bytes(b'0123456789abcdef'*(1024*1024))
ledger=root/'story_run.json'
story_run.init_run(run_file=ledger,confirmed_text=fixture/'text.txt',subtitle_txt=fixture/'text.txt',greenscreen_video=fixture/'video.mp4',audio=fixture/'audio.mp3',project_dir=root)
started=time.perf_counter()
for _ in range(3):
    with operation_observation(ledger,'fixture','deterministic',artifacts=[frames]):pass
observation_seconds=time.perf_counter()-started
output=io.StringIO()
with patch('sys.argv',['story_run.py','record','--run-file',str(ledger),'--package','director_plan','--status','running']),contextlib.redirect_stdout(output):story_run.main()
(root/'cli-output.json').write_text(output.getvalue())
real_open=Path.open;read_count=0;read_bytes=0
class Tracked:
    def __init__(self,f):self.f=f
    def __enter__(self):return self
    def __exit__(self,*exc):self.f.close()
    def read(self,*args):
        global read_bytes
        b=self.f.read(*args);read_bytes+=len(b);return b
    def __getattr__(self,k):return getattr(self.f,k)
def opened(path,*args,**kw):
    global read_count
    f=real_open(path,*args,**kw)
    if path.resolve()==large.resolve() and args and args[0]=='rb':
        read_count+=1;return Tracked(f)
    return f
try:
    from story_hash_cache import hash_cache_scope
except ImportError:
    hash_cache_scope=contextlib.nullcontext
with patch.object(Path,'open',opened):
    started=time.perf_counter()
    with hash_cache_scope():
        hashes=[story_run.file_sha256(large) for _ in range(5)]
    hash_seconds=time.perf_counter()-started
report={'source':str(a.source),'cli_output_bytes':len(output.getvalue().encode()),'ledger_bytes':ledger.stat().st_size,'member_evidence_bytes':sum(x.stat().st_size for x in (root/'evidence_members').glob('*.json')),'member_manifest_count':len(list((root/'evidence_members').glob('*.json'))),'observation_wall_seconds':observation_seconds,'hash_workload':{'size_bytes':large.stat().st_size,'calls':5,'actual_file_reads':read_count,'read_bytes':read_bytes,'wall_seconds':hash_seconds,'digest':hashes[0]},'limits':'5000 deterministic member files, 3 operations, 5 hash consumers; not a full-story finalize benchmark'}
(root/'measurement.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps(report,ensure_ascii=False))
