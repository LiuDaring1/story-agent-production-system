"""Real FFmpeg regressions for subtitle and indirect-input cache correctness."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image, ImageDraw
from story_encode import run_encode
from story_production_v2 import sha


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        env = patch.dict(os.environ, {'STORY_ENCODE_STATE_DIR':str(self.root/'pool')})
        env.start(); self.addCleanup(env.stop)

    def frame(self, index, text):
        p = self.root/f'subtitle_{index:05d}.png'
        image=Image.new('RGB',(128,64),'black')
        ImageDraw.Draw(image).text((20,20),text,fill='white')
        image.save(p)
        return p

    def sequence(self):
        return ['ffmpeg','-v','error','-y','-framerate','2','-i',str(self.root/'subtitle_%05d.png'),'-c:v','libx264','-pix_fmt','yuv420p',str(self.root/'subtitle.mp4')]

    def state(self, output):
        return json.loads((self.root/'pool'/(hashlib.sha256(str(output).encode()).hexdigest()+'.json')).read_text())

    def pixels(self,path):
        return subprocess.check_output(['ffmpeg','-v','error','-i',str(path),'-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','-'])

    def test_subtitle_change_reaches_downstream_and_unchanged_reuses(self):
        self.frame(0,'AAA');self.frame(1,'AAA')
        args=self.sequence();out=Path(args[-1]);run_encode(args)
        before=self.pixels(out)
        downstream=self.root/'composition.mp4'
        composite=['ffmpeg','-v','error','-y','-i',str(out),'-vf','pad=160:96:16:16','-c:v','libx264',str(downstream)]
        run_encode(composite);before_composite=self.pixels(downstream)
        with patch('subprocess.Popen',side_effect=AssertionError('unchanged sequence spawned encoder')):
            run_encode(args)
        self.frame(0,'BBB');self.frame(1,'BBB')
        run_encode(args);run_encode(composite)
        self.assertNotEqual(before,self.pixels(out))
        self.assertNotEqual(before_composite,self.pixels(downstream))
        self.assertEqual(len(self.state(out)['dependencies']['sequences'][0]['members']),2)

    def test_sequence_modify_add_delete_and_missing_fail(self):
        self.frame(0,'AAA');self.frame(1,'AAA')
        args=self.sequence();out=Path(args[-1]);run_encode(args)
        previous=self.state(out)['fingerprint']
        for action in [lambda:self.frame(1,'BBB'),lambda:self.frame(2,'CCC'),lambda:(self.root/'subtitle_00002.png').unlink()]:
            action();run_encode(args)
            current=self.state(out)['fingerprint'];self.assertNotEqual(previous,current);previous=current
        good=sha(out)
        for p in self.root.glob('subtitle_*.png'):p.unlink()
        with self.assertRaises(FileNotFoundError):run_encode(args)
        self.assertEqual(good,sha(out))

    def test_concat_reference_changes_reencode_and_missing_fails(self):
        source=self.root/'clip.mp4'
        def clip(color):
            subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i',f'color=c={color}:s=64x64:r=5:d=1','-c:v','libx264',str(source)],check=True)
        clip('red');listing=self.root/'list.txt';listing.write_text("file 'clip.mp4'\n")
        out=self.root/'joined.mp4'
        args=['ffmpeg','-v','error','-y','-f','concat','-safe','0','-i',str(listing),'-c:v','libx264',str(out)]
        run_encode(args);before=self.pixels(out);first=self.state(out)['started_at']
        run_encode(args);self.assertGreater(self.state(out)['started_at'],first)
        self.assertFalse(self.state(out)['dependencies']['reusable'])
        clip('blue');run_encode(args);self.assertNotEqual(before,self.pixels(out))
        source.unlink()
        with self.assertRaises(FileNotFoundError):run_encode(args)

    def test_external_filter_script_change_and_missing(self):
        self.frame(0,'AAA')
        script=self.root/'filter.txt';script.write_text('[0:v]negate[v]')
        out=self.root/'filtered.mp4'
        args=['ffmpeg','-v','error','-y','-loop','1','-i',str(self.root/'subtitle_00000.png'),'-filter_complex_script',str(script),'-map','[v]','-t','0.5','-c:v','libx264','-pix_fmt','yuv420p',str(out)]
        run_encode(args);before=self.pixels(out);first=self.state(out)['started_at']
        run_encode(args);self.assertGreater(self.state(out)['started_at'],first)
        script.write_text('[0:v]hflip[v]');run_encode(args)
        self.assertNotEqual(before,self.pixels(out))
        script.unlink()
        with self.assertRaises(FileNotFoundError):run_encode(args)

    def test_missing_direct_input_never_returns_old_success(self):
        source=self.frame(0,'AAA');out=self.root/'direct.mp4'
        args=['ffmpeg','-v','error','-y','-loop','1','-i',str(source),'-t','0.5','-c:v','libx264','-pix_fmt','yuv420p',str(out)]
        run_encode(args);source.unlink()
        with self.assertRaises(FileNotFoundError):run_encode(args)

    def test_actual_subtitle_renderer_rebuilds_same_path_aaa_to_bbb(self):
        from tests import test_decoupled_production
        from story_render_task import bind_render_task, render_entry
        from release_video import render_subtitle_overlay_video
        helper = test_decoupled_production.DecoupledProductionTests()
        helper.setUp(); self.addCleanup(helper.doCleanups)
        runfile = helper.run_fixture()
        output = runfile.parent.parent / 'media' / 'subtitle.mov'
        output.parent.mkdir()
        srt = self.root / 'subtitle.srt'
        @render_entry
        def execute():
            bind_render_task(runfile, outputs=[output])
            render_subtitle_overlay_video(srt, output, 1, 320, 180, 24, 20, 2, fps=2)
        srt.write_text('1\n00:00:00,000 --> 00:00:01,000\nAAA\n')
        execute(); first=self.pixels(output)
        with patch('subprocess.Popen', side_effect=AssertionError('unchanged production subtitle encoded')):
            execute()
        srt.write_text('1\n00:00:00,000 --> 00:00:01,000\nBBB\n')
        execute()
        self.assertNotEqual(first,self.pixels(output))

    def test_sequence_crosses_printf_minimum_width(self):
        for n,text in [(999,'AAA'),(1000,'AAA')]:
            image=Image.new('RGB',(128,64),'black');ImageDraw.Draw(image).text((10,10),text,fill='white');image.save(self.root/f'wide_{n}.png')
        args=['ffmpeg','-v','error','-y','-start_number','999','-framerate','2','-i',str(self.root/'wide_%03d.png'),'-c:v','libx264','-pix_fmt','yuv420p',str(self.root/'wide.mp4')]
        run_encode(args);before=self.state(Path(args[-1]))['fingerprint']
        image=Image.new('RGB',(128,64),'white');image.save(self.root/'wide_1000.png')
        run_encode(args)
        self.assertNotEqual(before,self.state(Path(args[-1]))['fingerprint'])
        self.assertEqual(len(self.state(Path(args[-1]))['dependencies']['sequences'][0]['members']),2)

    def test_auto_detected_concat_with_media_suffix_never_reuses(self):
        source=self.root/'clip.mp4'
        def clip(color):
            subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i',f'color=c={color}:s=64x64:r=5:d=1','-c:v','libx264',str(source)],check=True)
        clip('red')
        listing=self.root/'disguised.mp4';listing.write_text("ffconcat version 1.0\nfile 'clip.mp4'\n")
        output=self.root/'joined.mp4'
        args=['ffmpeg','-v','error','-y','-i',str(listing),'-c:v','libx264',str(output)]
        run_encode(args);first=self.pixels(output)
        self.assertFalse(self.state(output)['dependencies']['reusable'])
        clip('blue');run_encode(args)
        self.assertNotEqual(first,self.pixels(output))
