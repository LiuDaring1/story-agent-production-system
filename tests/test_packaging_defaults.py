import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from story_packaging_defaults import defaults, prepare_inputs, packaging_fields
from story_production_v2 import binding
from story_materials import bind_packaging, validate_packaging

class PackagingDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.requirements = self.root/'brief.json'
        self.info = {'story_name':'新故事','story_type':'成语故事','age_range':'8岁以上','expected_duration_seconds':202,'sources':{k:'current conversation' for k in ('story_name','story_type','age_range')}}
        self.requirements.write_text(json.dumps({'story_info':self.info}))

    def test_default_assets_persisted_and_story_saved(self):
        paths, config = prepare_inputs({'story_requirements':self.requirements}, self.root/'project')
        self.assertEqual(binding(paths['packaging_reference'])['sha256'], config['packaging_reference']['sha256'])
        info=json.loads(paths['story_requirements'].read_text())['story_info']
        self.assertEqual(info['theme_style'],'典雅端庄、简洁清爽、上下协调')
        self.assertEqual(info['age_range'],'8岁以上')
        self.assertEqual(info['expected_duration_seconds'],202)
        # Recovery reads the saved project record; no conversation recollection.
        retry,_=prepare_inputs({'story_requirements':self.requirements},self.root/'project')
        self.assertEqual(retry,paths)
        restored,_=prepare_inputs(paths,self.root/'project')
        self.assertEqual(restored,paths)
        self.assertNotIn('theme_style',json.loads(self.requirements.read_text())['story_info'])

    def test_missing_identity_is_not_guessed_from_directory(self):
        self.info.pop('age_range');self.requirements.write_text(json.dumps({'story_info':self.info}))
        with self.assertRaisesRegex(ValueError,'age_range'):
            prepare_inputs({'story_requirements':self.requirements},self.root/'旧故事')

    def test_duration_changes_recompile_without_old_story_fields(self):
        paths,_=prepare_inputs({'story_requirements':self.requirements},self.root/'project')
        inputs={k:binding(v) for k,v in paths.items()}
        audio=self.root/'audio.wav';audio.write_bytes(b'audio');inputs['audio']=binding(audio)
        timeline=self.root/'timeline.json';timeline.write_text('{}');tb=binding(timeline)
        with patch('story_timeline.validate_authoritative_timeline_receipt',return_value={'authoritative_audio':binding(audio)}), patch('story_video_synthesizer.media.probe_duration',return_value=202.4):
            fields=packaging_fields(inputs,tb)
            self.assertEqual(fields['duration_text'],'3分22秒')
            p=bind_packaging(inputs=inputs,output=self.root/'prompt.txt',receipt=self.root/'receipt.json',timeline_receipt=tb)
            self.assertEqual(p['fields']['story_name'],'新故事')
            validate_packaging(self.root/'receipt.json',inputs)
        with patch('story_timeline.validate_authoritative_timeline_receipt',return_value={'authoritative_audio':binding(audio)}), patch('story_video_synthesizer.media.probe_duration',return_value=210):
            self.assertEqual(packaging_fields(inputs,tb)['duration_text'],'3分30秒')
            with self.assertRaises(ValueError):validate_packaging(self.root/'receipt.json',inputs)
            bind_packaging(inputs=inputs,output=self.root/'prompt.txt',receipt=self.root/'receipt.json',timeline_receipt=tb)
        self.assertNotIn('《旧故事》',(self.root/'prompt.txt').read_text())

    def test_tampered_config_rejected(self):
        c=defaults();self.assertTrue(Path(c['packaging_reference']['path']).is_file())
        with patch('story_packaging_defaults.CONFIG',self.root/'missing.json'):
            with self.assertRaises(FileNotFoundError):defaults()

    def test_real_init_without_packaging_arguments(self):
        from story_run import init_run, load_run
        files={}
        for role,ext in [('confirmed_text','.txt'),('subtitle_txt','.txt'),('subtitle_srt','.srt'),('audio','.wav'),('greenscreen_video','.mp4'),('final_word','.docx'),('finished_music','.mp3')]:
            files[role]=self.root/(role+ext);files[role].write_bytes(b'fixture')
        project=self.root/'new';run=project/'99_项目状态/story_run.json'
        init_run(run_file=run,project_dir=project,confirmed_text=files['confirmed_text'],subtitle_txt=files['subtitle_txt'],greenscreen_video=files['greenscreen_video'],audio=files['audio'],production_inputs={**{k:files[k] for k in ('subtitle_srt','final_word','finished_music')},'story_requirements':self.requirements})
        saved=load_run(run)
        self.assertEqual(saved['production_contract'],'story-production/v2')
        self.assertIn('packaging_config',saved)
        self.assertEqual(json.loads(Path(saved['inputs']['story_requirements']['path']).read_text())['story_info']['story_name'],'新故事')

    def test_hash_tampering_fails(self):
        original=defaults();config=json.loads(Path(original['config']['path']).read_text())
        for key in ('reference','prompt'):
            f=self.root/config[key];f.write_bytes(b'tampered')
        manifest=self.root/'defaults.json';manifest.write_text(json.dumps(config))
        with patch('story_packaging_defaults.CONFIG',manifest):
            with self.assertRaisesRegex(ValueError,'hash mismatch'):defaults()
