"""Run actual final machine QA and native receipt validation on fixture releases."""
from pathlib import Path
import json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from story_production_v2 import binding
from story_artifact_validation import validate_artifact_semantics
from story_run import load_run
from continue_media_render import call

def main():
    root=Path(sys.argv[1]).resolve();project=root/'fixture';status=project/'99_项目状态';release=project/'04_发布视频';runfile=status/'story_run.json'
    videos={'main_release_video':binding(release/'主账号发布视频.mp4'),'library_release_video':binding(release/'宝库号发布视频.mp4')}
    request={'releases':videos,'output':str(status/'qa_release_report.json'),'evidence_dir':str(status/'final_qa'),'demo':binding(project/'03_产品素材/media/demo.mp4'),'logo':binding(project/'assets/logo.png'),'gesture_times':[1.5]}
    call(root,'release-qa',runfile,request)
    validate_artifact_semantics('qa_release_report',status/'qa_release_report.json',registered_artifacts=videos,registered_inputs=load_run(runfile)['inputs'])
    validate_artifact_semantics('release_package_receipt',release/'release_render_manifest_both.json')
    print('Actual dual-release machine QA and native-v2 receipt validation passed')
if __name__=='__main__':main()
