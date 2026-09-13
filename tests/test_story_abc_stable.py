import json
import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image, ImageDraw
from story_scene_windows import segments, coverage_samples, observed_frame_mode, prepare_plan, validate_coverage_report
from story_production_v2 import binding
from release_video import parse_b_windows, validate_main_scene_ending


class ABCStableTests(unittest.TestCase):
    def test_invalid_windows_and_nonfinite_fail(self):
        for b,c in [(((0,31),),()), (((0,10),(9,12)),()), (((0,10),),((9,12),)), (((float('nan'),12),),())]:
            with self.assertRaises(ValueError): segments(30,b,c)
        with self.assertRaises(argparse.ArgumentTypeError): parse_b_windows('nan-2')

    def test_samples_are_actual_segments_and_both_switch_sides(self):
        samples=coverage_samples(50,((15,33),),((0,15),))
        self.assertEqual({s['expected_mode'] for s in samples},{'a','b','c'})
        for t in [14.92,15.08,32.92,33.08]:
            self.assertIn(t,[s['time_seconds'] for s in samples])

    def test_actual_pixels_distinguish_a_b_c_not_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); templates={}; images={}
            for mode,box in [('a',(170,250,1160,810)),('b',(120,80,1790,1000))]:
                image=Image.new('RGBA',(1920,1080)); ImageDraw.Draw(image).rectangle(box,outline=(240,180,10,255),width=40)
                path=root/f'{mode}.png';image.save(path);templates[mode]=binding(path)
                actual=Image.new('RGB',image.size,(20,55,99));actual.paste(image,mask=image.getchannel('A'));images[mode]=actual
            images['c']=Image.new('RGB',(1920,1080),(20,55,99))
            person=Image.new('RGBA',(1920,1080));ImageDraw.Draw(person).rectangle((900,200,1100,900),fill=(210,25,35,255))
            images['c'].paste(person,mask=person.getchannel('A'))
            for mode, actual in images.items(): self.assertEqual(observed_frame_mode(actual,templates,person)['observed_mode'],mode)
            for color in [(0,0,0),(20,55,99)]:
                self.assertEqual(observed_frame_mode(Image.new('RGB',(1920,1080),color),templates,person)['observed_mode'],'unknown')
            # A valid expected B label cannot turn an A image into B evidence.
            self.assertNotEqual(observed_frame_mode(images['a'],templates)['observed_mode'],'b')

    def test_auto_preparation_reuses_current_evidence_and_refuses_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); srt=root/'speech.srt'; srt.write_text('1\n00:00:00,000 --> 00:00:48,000\ntext\n')
            timeline=root/'timeline.json';timeline.write_text(json.dumps({'audio_duration_seconds':50.}))
            run={'production_contract':'story-production/v2','project_dir':str(root),'inputs':{'subtitle_srt':binding(srt)},'artifacts':{'authoritative_timeline_receipt':binding(timeline)}}
            with patch('story_run.load_run',return_value=run), patch('story_timeline.validate_authoritative_timeline_receipt'):
                plan=root/'plan.json';prepare_plan(root/'run.json',plan);first=plan.read_bytes()
                prepare_plan(root/'run.json',plan);self.assertEqual(first,plan.read_bytes())
                self.assertEqual({x['mode'] for x in json.loads(first)['segments']},{'a','b','c'})
                srt.write_text('changed')
                with self.assertRaises(ValueError): prepare_plan(root/'run.json',plan)

    def test_explicit_c_ending_requires_actual_subtitle_free_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            srt=Path(directory)/'speech.srt'
            srt.write_text('1\n00:00:00,000 --> 00:00:40,000\ntext\n')
            with self.assertRaisesRegex(ValueError,'ending must be A'):
                validate_main_scene_ending([(0,32,'a'),(32,42,'c')],42,srt)
            srt.write_text('1\n00:00:00,000 --> 00:00:30,000\ntext\n')
            validate_main_scene_ending([(0,30,'a'),(30,42,'c')],42,srt)
            with self.assertRaisesRegex(ValueError,'subtitle-free C'):
                validate_main_scene_ending([(0,28,'a'),(28,42,'c')],42,srt)

    def test_missing_decoded_coverage_is_not_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'coverage.json';path.write_text(json.dumps({'passed':True}))
            with self.assertRaises(ValueError): validate_coverage_report(path,Path(directory)/'video.mp4')

    def test_relocated_rule_resolves_same_bytes_without_trusting_old_path(self):
        import story_scene_windows
        from story_scene_windows import current_rule
        source=Path(story_scene_windows.__file__).resolve()
        item={**binding(source),'path':'/missing/former-checkout/story_scene_windows.py','source_relative_path':'story_scene_windows.py'}
        self.assertEqual(current_rule(item),source)
        with self.assertRaises(ValueError): current_rule({**item,'sha256':'0'*64})
