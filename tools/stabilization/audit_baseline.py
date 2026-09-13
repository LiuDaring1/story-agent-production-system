"""Read-only crosscheck; paths are explicit CLI inputs, never production defaults."""
import argparse, csv, hashlib, json, subprocess
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--project',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
def sha(p):
    with p.open('rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()
s=a.project/'99_项目状态';h=s/'系统优化交接_20260913';run=json.loads((s/'story_run.json').read_text())
checks=[]
manifest=h/'交接文件SHA256.json'
for item in json.loads(manifest.read_text())['files']:
    path=Path(item['path']);actual=sha(path) if path.is_file() else None
    checks.append({'path':str(path),'expected':item['sha256'],'actual':actual,'match':actual==item['sha256']})
latest={}
for ev in run.get('observability',{}).get('events',[]):
    if ev.get('event')=='operation_fact':latest[ev['operation_id']]=ev
requests=run.get('observability',{}).get('requests',{})
csvcounts={f.name:sum(1 for x in csv.DictReader(f.open())) for f in h.glob('*.csv')}
selected=[]
for rel in ['abc_scene_bug_20260913/release_windows_plan.json','abc_scene_bug_20260913/release_windows_independent_review.json','abc_scene_bug_20260913/formal_abc_machine_evidence.json','abc_scene_bug_20260913/formal_abc_independent_review.json','abc_scene_bug_20260913/invalid_artifact_backup/release_render_manifest_both_all_A.json','abc_scene_bug_20260913/invalid_artifact_backup/release_geometry_manifest_both_all_A.json','code_fix_recovery.md','abc_scene_bug_20260913/root_cause_evidence.md','abc_scene_bug_20260913/repair_process_ledger.md','external_disconnect_recovery_20260913.md']:
    f=s/rel;selected.append({'path':str(f),'sha256':sha(f),'bytes':f.stat().st_size})
report={'story_ledger':{'path':str(s/'story_run.json'),'sha256':sha(s/'story_run.json'),'bytes':(s/'story_run.json').stat().st_size},'handoff_files':checks,'current_counts':{'inputs':len(run['inputs']),'artifacts':len(run['artifacts']),'operations':len(latest),'requests':len(requests)},'handoff_csv_counts':csvcounts,'running_operations':[{'operation_id':k,'operation':v.get('operation'),'started_at':v.get('started_at')} for k,v in latest.items() if v.get('status')=='running'],'read_evidence':selected,'historical_files_modified':False}
a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps({'counts':report['current_counts'],'csv_counts':csvcounts,'handoff_mismatches':[x for x in checks if not x['match']],'running':report['running_operations']},ensure_ascii=False))
