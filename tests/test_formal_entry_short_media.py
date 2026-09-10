"""Real short-media QA/confirmed-timeline integration; no approval mocks.

This deliberately does not claim upstream generation or final independent review.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import wave
import numpy as np
from story_production_v2 import INPUTS, PACKAGES, VERSION, binding
from story_run import init_run

ROOT = Path(__file__).resolve().parents[1]

class FormalEntryShortMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(os.environ.get('STORY_E2E_EVIDENCE', cls.temp.name))
        cls.root.mkdir(parents=True, exist_ok=True)
        cls.project = cls.root/'fixture'; cls.project.mkdir(exist_ok=True)
        cls.runfile = cls.project/'99_项目状态/story_run.json'
        for name, frequency in [('voice',431),('music',719)]:
            t = np.arange(48000*3)/48000
            signal = .25*np.sin(2*np.pi*frequency*t)*(0.65+.35*np.sin(2*np.pi*3.7*t))
            with wave.open(str(cls.root/f'{name}.wav'), 'wb') as f:
                f.setnchannels(1); f.setsampwidth(2); f.setframerate(48000)
                f.writeframes((signal*32767).astype('<i2').tobytes())
        cls.video = cls.project/'fixture.mp4'
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=0xaaccee:s=360x480:r=24:d=3',
                        '-i',str(cls.root/'voice.wav'),'-i',str(cls.root/'music.wav'),
                        '-filter_complex','[1:a][2:a]amix=inputs=2:weights=1 0.22:normalize=0[a]',
                        '-map','0:v','-map','[a]','-c:v','libx264','-crf','20','-preset','medium','-c:a','aac','-t','3',str(cls.video)],check=True)
        cls.library=cls.project/'library.mp4'; cls.library.write_bytes(cls.video.read_bytes())
        cls.txt=cls.root/'text.txt'; cls.txt.write_text('测试原文。\n')
        cls.srt=cls.root/'text.srt'; cls.srt.write_text('1\n00:00:00,000 --> 00:00:02,500\n测试原文。\n')
        if cls.runfile.exists(): cls.runfile.unlink()  # test fixture only
        run=init_run(run_file=cls.runfile,confirmed_text=cls.txt,subtitle_txt=cls.txt,greenscreen_video=cls.video,audio=cls.root/'voice.wav',project_dir=cls.project)
        # Upstream INPUT fixture, no generated receipts or passed review fabricated.
        run['production_contract']=VERSION
        run['work_packages']={p:{'status':'pending','blocker':''} for p in PACKAGES}
        for role in INPUTS:
            run['inputs'].setdefault(role,binding(cls.txt))
        run['inputs']['subtitle_srt']=binding(cls.srt)
        run['inputs']['finished_music']=binding(cls.root/'music.wav')
        cls.runfile.write_text(json.dumps(run))
        cls.inputs=run['inputs']
    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()
    def cli(self, operation, request):
        p=self.project/f'{operation}_request.json';p.write_text(json.dumps(request))
        result=subprocess.run([sys.executable,str(ROOT/'story_pipeline.py'),operation,'--run-file',str(self.runfile),'--request',str(p)],capture_output=True,text=True)
        (self.root/f'{operation}.log').write_text(result.stdout+result.stderr)
        return result
    def test_01_confirmed_srt_real_entry_and_drift(self):
        from story_timeline import validate_authoritative_timeline_receipt
        receipt=self.project/'99_项目状态/timeline.json'
        result=self.cli('timeline',{'receipt_path':str(receipt),'timings_path':str(self.project/'99_项目状态/timings.json')})
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(validate_authoritative_timeline_receipt(receipt)['source_kind'],'confirmed_user_srt')
        original=self.srt.read_bytes()
        try:
            self.srt.write_bytes(original+b'\n')
            with self.assertRaises(ValueError): validate_authoritative_timeline_receipt(receipt)
        finally: self.srt.write_bytes(original)
    def test_02_real_dual_release_machine_qa_and_hash_rejection(self):
        request={'releases':{'main_release_video':binding(self.video),'library_release_video':binding(self.library)},'output':str(self.project/'99_项目状态/qa_release_report.json'),'evidence_dir':str(self.project/'99_项目状态/final_qa')}
        result=self.cli('release-qa',request)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        report=json.loads(Path(request['output']).read_text())
        from story_artifact_validation import validate_artifact_semantics
        validate_artifact_semantics('qa_release_report', Path(request['output']), registered_artifacts=request['releases'], registered_inputs=self.inputs)
        self.assertTrue(report['passed']); self.assertEqual(len(report['results']),2)
        self.assertTrue(all(r['audio_role_fit']['passed'] for r in report['results']))
        request['releases']['main_release_video']['sha256']='0'*64
        self.assertNotEqual(self.cli('release-qa',request).returncode,0)
    def test_03_audio_role_failure_is_real_machine_result(self):
        bad=self.project/'voice_only.mp4'
        subprocess.run(['ffmpeg','-v','error','-y','-i',str(self.video),'-i',str(self.root/'voice.wav'),'-map','0:v','-map','1:a','-c:v','copy','-c:a','aac','-t','3',str(bad)],check=True)
        request={'releases':{'main_release_video':binding(bad),'library_release_video':binding(self.library)},'output':str(self.project/'99_项目状态/qa_missing_music.json'),'evidence_dir':str(self.project/'99_项目状态/negative_qa')}
        result=self.cli('release-qa',request)
        self.assertNotEqual(result.returncode,0)
        report=json.loads(Path(request['output']).read_text())
        self.assertFalse(report['passed'])
        self.assertIn('main_release_video: narration/music fit failed',report['issues'])

    def test_04_media_preflight_reuses_release_mask_rule(self):
        from PIL import Image, ImageDraw
        from story_candidate_cli import validate_media_frame
        preset=self.project/'frame_preset.json';preset.write_text(json.dumps({'story_box':[210,270,910,512]}))
        frame_path=self.project/'test_frame.png'
        for width,valid in [(8,False),(14,True)]:
            frame=Image.new('RGBA',(1920,1080),(0,0,0,0));ImageDraw.Draw(frame).rectangle((205,265,1125,787),outline=(133,99,60,255),width=width);frame.save(frame_path)
            if valid: validate_media_frame(preset,frame_path)
            else:
                with self.assertRaisesRegex(ValueError,'frame_masking_lip_too_thin'):
                    validate_media_frame(preset,frame_path)

    def test_05_formal_entrypoints_offer_original_help(self):
        for operation in ('assemble','backgrounds','release','timeline','release-qa'):
            p=subprocess.run([sys.executable,str(ROOT/'story_pipeline.py'),operation,'--help'],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)

if __name__=='__main__': unittest.main()
