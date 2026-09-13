"""Short real media and injected storage faults for owned recovery, no paid calls."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import story_encode
from story_encode import run_encode, reconcile_encode_operations
from story_render_task import function_code_version, render_code_scope
from story_work_observation import append_fact, summarize
import story_run


class StableEncodeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        env = patch.dict(os.environ, {'STORY_ENCODE_STATE_DIR': str(self.root / 'pool'), 'STORY_ENCODE_CONCURRENCY': '1'})
        env.start(); self.addCleanup(env.stop)

    def command(self, output, color='red', seconds='0.2'):
        return ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'color=c={color}:s=64x64:r=10',
                '-t', seconds, '-c:v', 'libx264', '-preset', 'ultrafast', str(output)]

    def receipt(self, output):
        return self.root / 'pool' / (hashlib.sha256(str(output).encode()).hexdigest() + '.json')

    def ledger(self):
        project = self.root / 'project'; project.mkdir(exist_ok=True)
        for name in ('text.txt', 'input.mp4', 'audio.mp3'):
            (self.root/name).write_bytes(b'fixture')
        ledger = project / 'story_run.json'
        story_run.init_run(run_file=ledger, confirmed_text=self.root/'text.txt', subtitle_txt=self.root/'text.txt',
                           greenscreen_video=self.root/'input.mp4', audio=self.root/'audio.mp3', project_dir=project)
        return ledger

    def test_code_location_and_unrelated_main_edit_preserve_library_code_identity(self):
        source = Path(__file__).resolve().parents[1] / 'release_video.py'
        copied = self.root / 'another_checkout' / source.name; copied.parent.mkdir(); copied.write_bytes(source.read_bytes())
        names = ['render_main_wide', 'render_library_window_video']
        first = [function_code_version(source, [name]) for name in names]
        self.assertEqual(first, [function_code_version(copied, [name]) for name in names])
        text = copied.read_text(); pos = text.index('    filters = filters_prefix + [', text.index('def render_main_wide('))
        text = text[:pos] + '    scoped_regression_change = True\n' + text[pos:]; copied.write_text(text)
        changed = [function_code_version(copied, [name]) for name in names]
        self.assertNotEqual(first[0], changed[0]); self.assertEqual(first[1], changed[1])
        helper = self.root / 'helper.py'; helper.write_text('x=1\n')
        bound = function_code_version(copied, [names[1]], dependencies=[helper]); helper.write_text('x=2\n')
        self.assertNotEqual(bound, function_code_version(copied, [names[1]], dependencies=[helper]))

    def test_actual_encode_counts_first_resume_move_code_parameter_and_input(self):
        output = self.root / 'result.mp4'
        source = self.root / 'source.png'
        from PIL import Image
        Image.new('RGB',(64,64),'red').save(source)
        helper = self.root/'first'/'render.py'; helper.parent.mkdir(); helper.write_text('version=1\n')
        moved = self.root/'second'/'render.py'; moved.parent.mkdir(); moved.write_bytes(helper.read_bytes())
        args = ['ffmpeg','-v','error','-y','-loop','1','-i',str(source),'-vf','scale=80:80,pad=96:96:8:8','-t','0.2','-c:v','libx264','-pix_fmt','yuv420p',str(output)]
        encodes = []
        original = subprocess.Popen
        def spawn(cmd, *a, **kw):
            if Path(str(cmd[-1])).name.startswith('.encoding-'):
                encodes.append(cmd)
            return original(cmd, *a, **kw)
        def execute(code):
            with render_code_scope([code]) as version:
                run_encode(args, code_version=version)
        with patch('subprocess.Popen', side_effect=spawn):
            execute(helper); self.assertEqual(len(encodes), 1)
            before = output.read_bytes()
            execute(helper); execute(moved); self.assertEqual(len(encodes), 1)
            moved.write_text('version=2\n'); execute(moved); self.assertEqual(len(encodes), 2)
            args[args.index('-t')+1] = '0.3'; execute(moved); self.assertEqual(len(encodes), 3)
            Image.new('RGB',(64,64),'blue').save(source); execute(moved); self.assertEqual(len(encodes), 4)
            self.assertNotEqual(before, output.read_bytes())
            output.write_bytes(b'not the managed video')
            with self.assertRaisesRegex(ValueError,'preserve it'):
                execute(moved)
            self.assertEqual(len(encodes), 4)

    def test_owned_encode_outlives_wait_deadline_and_still_decodes(self):
        output = self.root/'deadline.mp4'
        run_encode(self.command(output), timeout=0)
        state = json.loads(self.receipt(output).read_text())
        self.assertTrue(state['wait_deadline_exceeded']); self.assertEqual(state['status'], 'completed')
        subprocess.run(['ffmpeg','-v','error','-i',str(output),'-f','null','-'],check=True)

    def test_preparation_disk_fault_closes_state_without_publication(self):
        output = self.root/'disk.mp4'
        with patch('story_encode.shutil.disk_usage', side_effect=OSError('simulated disconnected output volume')):
            with self.assertRaises(OSError):run_encode(self.command(output))
        state = json.loads(self.receipt(output).read_text())
        self.assertEqual(state['status'],'failed'); self.assertFalse(output.exists())
        run_encode(self.command(output)); self.assertEqual(json.loads(self.receipt(output).read_text())['status'],'completed')

    def test_publish_failure_preserves_old_video_and_cleans_partial(self):
        output = self.root/'publish.mp4'; run_encode(self.command(output)); original_bytes = output.read_bytes()
        real_replace = os.replace
        def replace(src,dst):
            if Path(dst)==output: raise OSError('simulated unmounted output volume')
            return real_replace(src,dst)
        with patch('story_encode.os.replace', side_effect=replace):
            with self.assertRaises(OSError):run_encode(self.command(output,color='blue'))
        self.assertEqual(output.read_bytes(),original_bytes)
        self.assertEqual(json.loads(self.receipt(output).read_text())['status'],'failed')
        self.assertEqual(list(self.root.glob('.encoding-*')),[])
        run_encode(self.command(output,color='blue')); self.assertNotEqual(output.read_bytes(),original_bytes)

    def test_completed_encode_closes_orphan_operation_only_with_hash_and_free_lock(self):
        ledger=self.ledger(); output=self.root/'orphan.mp4'; run_encode(self.command(output))
        receipt=self.receipt(output); state=json.loads(receipt.read_text())
        state.update(ledger=str(ledger),operation_id='orphan'); receipt.write_text(json.dumps(state))
        append_fact(ledger,{'operation_id':'orphan','status':'running','execution_mode':'first_execution','started_at':'2026-09-13T00:00:00+00:00','operation':'ffmpeg'})
        with receipt.with_suffix('.lock').open('a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertEqual(reconcile_encode_operations(ledger),[])
        with patch('story_work_observation.recover_operation',side_effect=OSError('simulated ledger volume loss')):
            self.assertEqual(reconcile_encode_operations(ledger),[])
        self.assertEqual(reconcile_encode_operations(ledger),['orphan'])
        self.assertEqual(summarize(story_run.load_run(ledger))['operations'][0]['status'],'complete')
        self.assertEqual(reconcile_encode_operations(ledger),[])

    def test_orphan_queue_and_corrupt_completed_output_close_failed(self):
        ledger=self.ledger(); pool=story_encode.state_directory()
        for name,state in [('queued',{'status':'waiting'}),('corrupt',{'status':'completed','output':{'path':str(self.root/'bad.mp4'),'sha256':'0'*64}})]:
            (self.root/'bad.mp4').write_bytes(b'corrupt')
            append_fact(ledger,{'operation_id':name,'status':'running','execution_mode':'first_execution','started_at':'2026-09-13T00:00:00+00:00','operation':'ffmpeg'})
            (pool/f'{name}.json').write_text(json.dumps({**state,'ledger':str(ledger),'operation_id':name}))
        self.assertEqual(set(reconcile_encode_operations(ledger)),{'queued','corrupt'})
        self.assertEqual({x['status'] for x in summarize(story_run.load_run(ledger))['operations']},{'failed'})

    def test_file_filter_and_dynamic_source_never_claim_reusable(self):
        from story_encode_dependencies import _closed_filter_graph
        for graph in ["movie=/tmp/movie.mp4[v]", "[0:v]drawtext=textfile=/tmp/text[v]", "[0:v]curves=psfile=/tmp/p.acv[v]", "[0:v]sendcmd=f=/tmp/cmd[v]"]:
            self.assertFalse(_closed_filter_graph(graph),graph)
        self.assertTrue(_closed_filter_graph("[0:v]scale=64:64,format=rgba[v];[v][1:v]overlay=x='mod(t*2,10)':y=0[o]"))

    def test_formal_v2_layout_delegates_reuse_to_owned_encoder(self):
        from tests.test_decoupled_production import DecoupledProductionTests
        from story_render_task import bind_render_task, render_entry
        from release_video import _execute_release_layout
        from story_module_adapters import LocalReleaseLayoutAdapter
        from story_video_synthesizer.media import run_command
        helper=DecoupledProductionTests(); helper.setUp(); self.addCleanup(helper.doCleanups)
        ledger=helper.run_fixture(); project=ledger.parent.parent
        outputs={role:project/f'{role}.mp4' for role in ('main','library')}
        counts=[]; original=subprocess.Popen
        def spawn(args,*a,**kw):
            if Path(str(args[-1])).name.startswith('.encoding-'):counts.append(args)
            return original(args,*a,**kw)
        main_revision=[False]
        real_code=function_code_version
        def version(path,names,**kwargs):
            value=real_code(path,names,**kwargs)
            return value+('-main-fix' if main_revision[0] and names==['render_main_wide'] else '')
        @render_entry
        def execute():
            bind_render_task(ledger,outputs=outputs.values())
            for role,output in outputs.items():
                _execute_release_layout(LocalReleaseLayoutAdapter(),artifact_id=f'release-{role}:fixture',
                    operation='main_wide_render' if role=='main' else 'library_window_render',
                    input_artifacts=({'role':'ledger','path':str(ledger),'sha256':hashlib.sha256(ledger.read_bytes()).hexdigest()},),layout_binding={'fixture':'short-media'},output_target=output,attempt_id='fixture',
                    executor=lambda output=output:run_command(self.command(output)))
        with patch('subprocess.Popen',side_effect=spawn),patch('story_render_task.function_code_version',side_effect=version):
            execute();self.assertEqual(len(counts),2)
            execute();self.assertEqual(len(counts),2)
            library_bytes=outputs['library'].read_bytes()
            main_revision[0]=True;execute();self.assertEqual(len(counts),3)
            self.assertEqual(library_bytes,outputs['library'].read_bytes())
        operations=summarize(story_run.load_run(ledger))['operations']
        self.assertEqual(sum(x['execution_mode']=='resume_reuse' for x in operations),3)
        for output in outputs.values():
            subprocess.run(['ffmpeg','-v','error','-i',str(output),'-f','null','-'],check=True)

    def test_failure_to_store_completed_receipt_recovers_published_hash_without_encode(self):
        output=self.root/'published.mp4'; real_write=story_encode._write_receipt
        def write(path,state):
            if state.get('status')=='completed':raise OSError('simulated receipt storage loss')
            return real_write(path,state)
        with patch('story_encode._write_receipt',side_effect=write):
            with self.assertRaises(OSError):run_encode(self.command(output))
        self.assertTrue(output.is_file())
        with patch('subprocess.Popen',side_effect=AssertionError('valid published output re-encoded')):
            run_encode(self.command(output))
        self.assertEqual(json.loads(self.receipt(output).read_text())['status'],'completed')

    def test_output_directory_disappearing_while_queued_is_not_recreated(self):
        from contextlib import contextmanager
        destination=self.root/'mounted-output';destination.mkdir()
        absent=self.root/'simulated-detached';output=destination/'output.mp4'
        real_slot=story_encode.encode_slot
        @contextmanager
        def disappeared(**kwargs):
            with real_slot(**kwargs) as fds:
                destination.rename(absent)
                yield fds
        with patch('story_encode.encode_slot',disappeared):
            with self.assertRaises(OSError):run_encode(self.command(output))
        self.assertFalse(destination.exists());self.assertFalse((absent/'output.mp4').exists())
        self.assertEqual(json.loads(self.receipt(output).read_text())['status'],'failed')
        absent.rename(destination);run_encode(self.command(output));self.assertTrue(output.is_file())

    def test_large_sequence_receipt_externalizes_and_revalidates_members(self):
        from PIL import Image
        from story_evidence_store import resolve_evidence
        image=Image.new('RGB',(32,32),'blue')
        for i in range(64):image.save(self.root/f'frame_{i:03d}.png')
        output=self.root/'sequence.mp4'
        args=['ffmpeg','-v','error','-y','-framerate','64','-i',str(self.root/'frame_%03d.png'),'-t','0.2','-c:v','libx264','-pix_fmt','yuv420p',str(output)]
        run_encode(args)
        raw=json.loads(self.receipt(output).read_text())
        self.assertIn('members_manifest',raw['inputs'])
        self.assertIn('members_manifest',raw['dependencies']['files'])
        self.assertIn('members_manifest',raw['dependencies']['sequences'][0])
        with patch('subprocess.Popen',side_effect=AssertionError('unchanged referenced sequence encoded')):run_encode(args)
        original=output.read_bytes()
        manifest=Path(raw['inputs']['members_manifest']['path']);manifest.write_text('[]\n')
        with self.assertRaisesRegex(ValueError,'Changed member evidence'):run_encode(args)
        self.assertEqual(original,output.read_bytes())

    def test_encoder_build_bytes_change_invalidates_same_command(self):
        import shlex, shutil
        tool=self.root/'ffmpeg-wrapper'
        tool.write_text('#!/bin/sh\nexec '+shlex.quote(shutil.which('ffmpeg'))+' "$@"\n');tool.chmod(0o755)
        output=self.root/'build.mp4';args=self.command(output);args[0]=str(tool)
        run_encode(args)
        original_state=json.loads(self.receipt(output).read_text())
        self.assertIn('ffmpeg version',original_state['encoder_tool']['version_output'])
        with patch('subprocess.Popen',side_effect=AssertionError('same build encoded')):run_encode(args)
        tool.write_text(tool.read_text()+'# changed build fixture\n')
        run_encode(args)
        state=json.loads(self.receipt(output).read_text())
        self.assertNotEqual(original_state['fingerprint'],state['fingerprint'])
        self.assertNotEqual(original_state['encoder_tool']['sha256'],state['encoder_tool']['sha256'])
