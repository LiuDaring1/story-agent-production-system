"""Actual media approval and encoding, followed by native-v2 release preview."""
from pathlib import Path
import json,subprocess,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from story_production_v2 import binding,write,validate_independent_approval

def call(root,operation,runfile,request):
    path=runfile.parent/(operation+'_request.json');write(path,request)
    result=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[2]/'story_pipeline.py'),operation,'--run-file',str(runfile),'--request',str(path)],capture_output=True,text=True)
    (root/(operation+'.log')).write_text(result.stdout+result.stderr)
    if result.returncode:raise RuntimeError(result.stderr)

def main():
    root=Path(sys.argv[1]).resolve();project=root/'fixture';status=project/'99_项目状态';runfile=status/'story_run.json'
    call(root,'media-approve',runfile,{'preview':str(status/'media_preview.json'),'review':str(status/'media_preview_review.json'),'output':str(status/'approved_demo.json')})
    request=json.loads((status/'media_request.json').read_text());request['approved_demo']=binding(status/'approved_demo.json')
    call(root,'media',runfile,request)
    print('Actual Demo/A-only render and validated customer media receipt complete')
if __name__=='__main__':main()
