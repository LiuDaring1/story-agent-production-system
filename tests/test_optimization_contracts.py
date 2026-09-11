import sys
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from story_production_v2 import binding, write, sha, current
sys.path.insert(0, str(Path(__file__).parent))


class MaterialsDirectoryTests(unittest.TestCase):
    def test_directory_delivery_reuse_optional_zip_and_drift(self):
        from tests.decoupled_material_fixture import material_fixture
        from story_materials import export_materials, validate_materials
        from story_managed_package import package
        from story_production_v2 import ADVANCED_ROLES, validate_managed_receipt
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); args = material_fixture(root / 'inputs')
            args['archive_output'] = root / 'optional.zip'
            with patch('story_video_synthesizer.media.probe_duration', return_value=22):
                receipt = export_materials(**args)
                mtime = (args['output'] / 'manifest.json').stat().st_mtime_ns
                self.assertTrue(export_materials(**args)['reused'])
            self.assertEqual(mtime, (args['output'] / 'manifest.json').stat().st_mtime_ns)
            self.assertTrue(args['output'].is_dir())
            self.assertEqual(len(list((args['output'] / 'images').iterdir())), 3)
            self.assertTrue(args['archive_output'].is_file())
            sources = {}
            for role in ADVANCED_ROLES:
                p = root / (role + '.bin'); p.write_bytes(role.encode()); sources[role] = p
            sources.update(customer_manuscript=args['inputs']['final_word']['path'], music=args['inputs']['finished_music']['path'], ppt_materials=args['output'])
            kw = dict(output_root=root / 'delivery', receipt=root / 'pack.json', sources=sources, story_name='测试', inputs=args['inputs'])
            result = package(**kw)
            target = next(x for x in result['artifacts'] if x['role'] == 'advanced:ppt_materials')
            self.assertTrue(Path(target['path']).is_dir())
            extra = Path(target['path']).parent / '用户PPT.pptx'; extra.write_bytes(b'user')
            self.assertTrue(package(**kw)['reused'])
            self.assertEqual(extra.read_bytes(), b'user')
            validate_managed_receipt(kw['receipt'], args['inputs'])
            (args['output'] / 'images' / Path(receipt['rows'][0]['archive_path']).name).write_bytes(b'drift')
            with self.assertRaises(ValueError): validate_materials(args['receipt'])
            with self.assertRaises(ValueError): package(**kw)

    def test_interrupted_archive_recovers_without_regenerating_folder(self):
        from tests.decoupled_material_fixture import material_fixture
        from story_materials import export_materials, validate_materials
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);args=material_fixture(root)
            with patch('story_video_synthesizer.media.probe_duration',return_value=22):
                first=export_materials(**args)
                mtime=(args['output']/'manifest.json').stat().st_mtime_ns
                args['archive_output']=root/'optional.zip'
                second=export_materials(**args)
                self.assertTrue(second['reused'])
                # Simulate interruption after replacing ZIP but before writing its receipt.
                write(args['receipt'],first)
                export_materials(**args)
                self.assertEqual(mtime,(args['output']/'manifest.json').stat().st_mtime_ns)
                validate_materials(args['receipt'])

    def test_legacy_zip_read_only_and_input_protection(self):
        import zipfile
        from tests.decoupled_material_fixture import material_fixture
        from story_materials import export_materials, validate_materials
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); args=material_fixture(root)
            with patch('story_video_synthesizer.media.probe_duration', return_value=22): result=export_materials(**args)
            legacy={k:v for k,v in result.items() if k!='directory'}
            legacy['schema_version']='story-ppt-materials/v2'
            zpath=root/'legacy.zip'
            with zipfile.ZipFile(zpath,'w') as z:
                for member in result['directory']['members']:
                    if member['relative_path']!='manifest.json': z.write(args['output']/member['relative_path'],member['relative_path'])
                z.writestr('manifest.json',json.dumps(legacy))
            legacy['archive']=binding(zpath); old=root/'old.json'; write(old,legacy)
            before=sha(zpath); validate_materials(old,args['inputs']); self.assertEqual(before,sha(zpath))
            args['output']=Path(args['inputs']['final_word']['path']).parent
            with self.assertRaises(ValueError): export_materials(**args)


class WorkReuseTests(unittest.TestCase):
    def test_preparation_single_batch_and_scope_invalidation(self):
        from story_review_preparation import prepare_review
        from story_evidence import write_review_bundle
        with tempfile.TemporaryDirectory() as d:
            r=Path(d); rule=r/'rules';rule.write_text('existing rules')
            image=r/'image.png';image.write_bytes(b'fixture');dep=r/'plan';dep.write_text('plan')
            kw=dict(project_id='project-a',items=[dict(scope='shot-1',path=str(image),dependencies=[str(dep)])],rules=[str(rule)],output=r/'packet')
            first=prepare_review(**kw); self.assertFalse(first['preparation_reused'])
            self.assertTrue(prepare_review(**kw)['preparation_reused'])
            bundle=r/'bundle';write_review_bundle(bundle,[image,dep,rule,Path(first['items'][0]['scope_definition']['path'])]);review=r/'review'
            write(review,dict(approved=True,score=90,critical_errors=[],independent_context=True,reviewer_context='unit-fixture-reviewer',artifact_sha256=sha(bundle)))
            prior=json.loads((r/'packet').read_text()); prior['items'][0]['approval']=dict(review=binding(review),bundle=binding(bundle),producer_context='unit-fixture-producer');write(r/'prior',prior)
            kw.update(previous=r/'prior',output=r/'new')
            self.assertEqual(prepare_review(**kw)['items'][0]['review_action'],'reuse_evidence')
            dep.write_text('changed');self.assertEqual(prepare_review(**kw)['items'][0]['review_action'],'review_current_artifact')
            kw['project_id']='new-story';self.assertEqual(prepare_review(**kw)['items'][0]['review_action'],'review_current_artifact')
            kw['repairs']=[dict(scope='shot-1',defect_code='action')]
            with self.assertRaisesRegex(ValueError,'Incomplete repair'):prepare_review(**kw)
            kw['repairs'][0].update(requirement_source=str(rule),requirement_scope='shot',evidence='frame contact absent',delivery_impact='required action absent',retry_strategy='explicit contact',root_cause='ambiguous action')
            self.assertEqual(prepare_review(**kw)['repair_groups'],{'action':['shot-1']})

    def test_simple_environment_alias_and_conservative_fallback(self):
        from story_asset_efficiency import asset_generation_plan
        master=dict(asset_id='master',contains_characters=[],camera_contract=dict(view_from_zone_id='a',view_target_zone_id='b',view_background_zone_ids=['c'],camera_angle='level',shot_size='wide'))
        view=dict(asset_id='view',derived_from_asset_id='master',contains_characters=[],view_from_zone_id='a',view_target_zone_id='b',view_background_zone_ids=['c'])
        setup=dict(setup_id='one',environment_view_asset_id='view',camera_origin_zone_id='a',look_target_zone_id='b',background_zone_ids=['c'],camera_angle='level',shot_size='wide')
        group=dict(environment_asset_id='master',asset_strategy='single_view_reuse_master',spatial_complexity='simple',camera_setups=[setup])
        plan=dict(story_id='test',assets=[master,view],shots=[dict(shot_id='s',reference_asset_ids=['view'])],continuity_groups=[group])
        self.assertEqual(asset_generation_plan(plan)['unique_generations'],1)
        group.pop('asset_strategy'); self.assertEqual(asset_generation_plan(plan)['unique_generations'],2)
        group['asset_strategy']='single_view_reuse_master';group['camera_setups'].append(copy.deepcopy(setup))
        with self.assertRaisesRegex(ValueError,'single-view'):asset_generation_plan(plan)

    def test_unreachable_derived_asset_does_not_keep_parent_alive(self):
        from story_asset_efficiency import asset_generation_plan
        director = dict(
            story_id='test',
            assets=[
                dict(asset_id='unused-master'),
                dict(asset_id='unused-view', derived_from_asset_id='unused-master'),
                dict(asset_id='used', derived_from_asset_id='used-master'),
                dict(asset_id='used-master'),
            ],
            shots=[dict(shot_id='s1', reference_asset_ids=['used'])],
            continuity_groups=[],
        )
        rows = {row['asset_id']: row for row in asset_generation_plan(director)['assets']}
        self.assertEqual(rows['unused-view']['operation'], 'omit_unconsumed')
        self.assertEqual(rows['unused-master']['operation'], 'omit_unconsumed')
        self.assertEqual(rows['used']['operation'], 'generate')
        self.assertEqual(rows['used-master']['operation'], 'generate')
        self.assertEqual(rows['used-master']['consumers'], ['derive:used'])

    def test_closed_subtitle_graph_reuses_only_current_sequence_and_parameters(self):
        from story_encode_dependencies import snapshot
        from story_subtitle_layers import cropped_overlay_chain
        from story_video_synthesizer.subtitles import SubtitleCue
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'video.mp4';source.write_bytes(b'video')
            image=root/'one.png';image.write_bytes(b'image1')
            cue=SubtitleCue(1,'text',0,1)
            graph=cropped_overlay_chain([cue],[(image,2,4)])
            cmd=['ffmpeg','-i',str(source),'-loop','1','-i',str(image),'-filter_complex',graph,'-map','[v]',str(root/'output.mp4')]
            first=snapshot(cmd);self.assertTrue(first['reusable'])
            image.write_bytes(b'image2');self.assertNotEqual(first['files'],snapshot(cmd)['files'])
            cmd[cmd.index('-filter_complex')+1] += ';movie=/tmp/unbound.png[v2]'
            self.assertFalse(snapshot(cmd)['reusable'])

    def test_formal_queue_deferral_records_wait_without_failure(self):
        import os
        from story_encode import run_encode, encode_slot
        from story_render_task import _task
        from story_work_observation import summarize
        from tests.test_story_run import StoryRunLedgerTests
        from story_run import init_run,load_run
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);project,text,video,audio,runfile=StoryRunLedgerTests().fixture(root)
            init_run(run_file=runfile,project_dir=project,confirmed_text=text,greenscreen_video=video,audio=audio)
            run=load_run(runfile);out=project/'later.mp4'
            token=_task.set(dict(task=run['run_id'],ledger=runfile,project=project.resolve(),protected=[]))
            try:
                with patch.dict(os.environ,{'STORY_ENCODE_STATE_DIR':str(root/'pool')}):
                    with encode_slot(limit=1):
                        with self.assertRaises(TimeoutError):run_encode(['ffmpeg','-f','lavfi','-i','color=red:size=32x32','-t','0.1',str(out)],wait_seconds=.01)
                fact=summarize(load_run(runfile))['operations'][0]
                self.assertEqual(fact['status'],'deferred')
                self.assertGreater(fact['wait_seconds'],0)
            finally:_task.reset(token)

    def test_unconsumed_missing_asset_does_not_block_real_bundle(self):
        from tests.decoupled_material_fixture import material_fixture
        from shot_storyboard_pipeline import create_asset_bundle
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);args=material_fixture(root)
            director=json.loads(args['director'].read_text())
            unused=copy.deepcopy(director['assets'][0]);unused['asset_id']='unused-design'
            unused['path']=str(root/'never-generated.png')
            director['assets'].append(unused);write(args['director'],director)
            result=create_asset_bundle(args['director'],root/'consumed-bundle')
            self.assertNotIn('unused-design',{a['asset_id'] for a in result['assets']})
            self.assertEqual(next(a for a in result['generation_plan']['assets'] if a['asset_id']=='unused-design')['operation'],'omit_unconsumed')

    def test_live_facts_failure_reuse_unknown_not_zero(self):
        from story_work_observation import operation_observation, summarize
        from tests.test_story_run import StoryRunLedgerTests
        from story_run import init_run,load_run
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);project,text,video,audio,runfile=StoryRunLedgerTests().fixture(root)
            init_run(run_file=runfile,project_dir=project,confirmed_text=text,greenscreen_video=video,audio=audio)
            with operation_observation(runfile,'copy','deterministic',execution_mode='resume_reuse'): pass
            with self.assertRaises(RuntimeError):
                with operation_observation(runfile,'encode','encode'): raise RuntimeError('injected')
            summary=summarize(load_run(runfile))
            self.assertEqual(summary['observed_operations'],2)
            self.assertEqual(summary['execution_modes']['resume_reuse'],1)
            self.assertIsNone(summary['total_model_requests'])
            self.assertIsNone(summary['operations'][0]['total_tokens'])
            self.assertEqual(summary['operations'][1]['status'],'failed')

if __name__=='__main__':unittest.main()
