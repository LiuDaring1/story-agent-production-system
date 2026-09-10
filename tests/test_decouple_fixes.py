"""Regression for candidate task ownership and role retirement (no paid services)."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from tests import test_decoupled_production as fixture_module
from story_managed_package import package
from story_production_v2 import binding, sha
from story_render_task import bind_render_task


class RetirementTests(unittest.TestCase):
    setUp = fixture_module.DecoupledProductionTests.setUp
    package_fixture = fixture_module.DecoupledProductionTests.package_fixture
    def change_music(self, args):
        music = self.root / 'replacement.mp3'
        music.write_bytes(b'user finished mp3')
        args['sources']['music'] = music
        args['inputs']['finished_music'] = binding(music)

    def test_suffix_change_retires_owned_music_and_reentry_is_stable(self):
        args = self.package_fixture()
        first = package(**args)
        old = [i for i in first['artifacts'] if i['role'].endswith(':music')]
        user = Path(old[0]['path']).parent / 'my-music.wav'
        user.write_bytes(b'user-added')
        self.change_music(args)
        with patch('subprocess.Popen', side_effect=AssertionError('no encode')):
            result = package(**args)
            self.assertEqual({k:v for k,v in result.items() if k != "reused"}, {k:v for k,v in package(**args).items() if k != "reused"})
        self.assertEqual(len(result['retired']), 2)
        for item in result['retired']:
            self.assertFalse(Path(item['path']).exists())
            self.assertEqual(sha(item['backup_path']), item['sha256'])
            self.assertNotIn(args['output_root'], Path(item['backup_path']).parents)
        self.assertEqual(user.read_bytes(), b'user-added')

    def test_modified_retired_music_blocks_all_moves(self):
        args = self.package_fixture()
        original = package(**args)
        musics = [Path(i['path']) for i in original['artifacts'] if i['role'].endswith(':music')]
        musics[1].write_bytes(b'user revision')
        before = args['receipt'].read_bytes()
        self.change_music(args)
        with self.assertRaisesRegex(ValueError, 'User changed retired'):
            package(**args)
        self.assertTrue(all(p.exists() for p in musics))
        self.assertEqual(musics[1].read_bytes(), b'user revision')
        self.assertEqual(before, args['receipt'].read_bytes())

    def test_retirement_crash_after_move_keeps_ownership(self):
        args = self.package_fixture()
        package(**args)
        self.change_music(args)
        replace = os.replace
        moved = []
        def crash_after_rename(source, target):
            replace(source, target)
            if '.story-managed-retired' in str(target) and not moved:
                moved.append(str(target))
                raise OSError('crash after successful rename')
        with patch('story_managed_package.os.replace', side_effect=crash_after_rename):
            with self.assertRaisesRegex(OSError, 'crash'):
                package(**args)
        pending = json.loads(args['receipt'].read_text())
        self.assertEqual(len(pending['retired']), 2)
        result = package(**args)
        self.assertEqual(len(result['retired']), 2)
        self.assertTrue(all(i['status'] == 'archived' for i in result['retired']))
        self.assertEqual({k:v for k,v in result.items() if k != "reused"}, {k:v for k,v in package(**args).items() if k != "reused"})


class RenderTaskTests(unittest.TestCase):
    setUp = fixture_module.DecoupledProductionTests.setUp
    run_fixture = fixture_module.DecoupledProductionTests.run_fixture
    def test_verified_binding_rejects_foreign_output_and_implicit_v2(self):
        runfile = self.run_fixture()
        out = runfile.parent.parent / 'media' / 'out.mp4'
        with self.assertRaisesRegex(ValueError, 'explicit --run-file'):
            bind_render_task(outputs=[out])
        with self.assertRaisesRegex(ValueError, 'another project'):
            bind_render_task(runfile, outputs=[self.root / 'foreign.mp4'])
        bind_render_task()  # restore legacy context

    def test_real_child_cancel_owner_resume_and_live_duplicate(self):
        runfile = self.run_fixture()
        project = runfile.parent.parent
        from story_requirements import write_projection
        projection = project / 'projection.json'
        write_projection(projection, scope='media_render', inputs={'audio':self.paths['audio']},
                         rule_sources=[(Path('skills/story-full-auto/references/video-invariants.md').resolve(),'v2')],
                         applicability={'accounts':['main'],'artifacts':['main_release_video']},
                         requirements=[dict(requirement_id='geometry', source='video-invariants', scope='main', requirement='preserve geometry')],
                         executable_checks=[], acceptance_evidence=['qa_release_report'])
        run = json.loads(runfile.read_text())
        run['artifacts']['requirements_projection'] = binding(projection)
        runfile.write_text(json.dumps(run))
        out = project / 'media' / 'out.mp4'
        pool = self.root / 'pool'
        env = {**os.environ, 'STORY_ENCODE_STATE_DIR': str(pool)}
        env.pop('STORY_TASK_ID', None)
        args = ['ffmpeg','-v','error','-y','-re','-f','lavfi','-i','testsrc2=size=64x64:rate=10','-t','3','-c:v','libx264','-preset','ultrafast',str(out)]
        script = 'import sys,json;from pathlib import Path;from release_video import preflight_release_requirements;from assemble_r2v_story import run;preflight_release_requirements(output_dir=Path(sys.argv[3]).parent,variant="main",explicit_run_file=Path(sys.argv[1]),output_scale=1);run(json.loads(sys.argv[2]))'
        command = [sys.executable,'-c',script,str(runfile),json.dumps(args),str(out)]
        process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate()
        self.addCleanup(cleanup)
        state_path = pool / (hashlib.sha256(str(out.resolve()).encode()).hexdigest()+'.json')
        deadline = time.monotonic()+10
        while not state_path.exists() and time.monotonic()<deadline:
            if process.poll() is not None:
                self.fail(process.communicate()[1].decode())
            time.sleep(.05)
        self.assertTrue(state_path.exists())
        state = json.loads(state_path.read_text())
        run = json.loads(runfile.read_text())
        self.assertEqual(state['task'],run['run_id'])
        duplicate = subprocess.run(command,env=env,capture_output=True,timeout=10)
        self.assertNotEqual(duplicate.returncode,0)
        self.assertIn(b'already active',duplicate.stderr)
        def control(run_path, action):
            request=self.root/'control.json'
            request.write_text(json.dumps({'output':str(out),'action':action,'expected_fingerprint':state['fingerprint']}))
            return subprocess.run([sys.executable,'story_pipeline.py','encode-control','--run-file',str(run_path),'--request',str(request)],env=env,capture_output=True,timeout=10)
        other=runfile.with_name('other.json')
        other.write_text(json.dumps({**run,'run_id':'another-task'}))
        self.assertNotEqual(control(other,'cancel').returncode,0)
        self.assertFalse(state_path.with_suffix('.cancel').exists())
        self.assertEqual(control(runfile,'cancel').returncode,0)
        process.communicate(timeout=10)
        self.assertFalse(out.exists())
        self.assertFalse(list(project.glob('.encoding-*')))
        self.assertEqual(control(runfile,'resume').returncode,0)
        result=subprocess.run(command,env=env,capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr.decode())
        finished=json.loads(state_path.read_text())
        self.assertEqual(finished['status'],'completed')
        again=subprocess.run(command,env=env,capture_output=True,timeout=10)
        self.assertEqual(again.returncode,0,again.stderr.decode())
        self.assertEqual(finished,json.loads(state_path.read_text()))
