from pathlib import Path
import json
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from story_production_v2 import *
from story_managed_package import package
from story_run import init_run, load_run, record_run, dependency_staleness, finalize_run
from story_materials import bind_packaging, validate_packaging, material_rows

class DecoupledProductionTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = {}
        for role in INPUTS:
            ext = {'final_word': '.docx', 'subtitle_txt': '.txt', 'subtitle_srt': '.srt', 'finished_music': '.wav'}.get(role, '.bin')
            p = self.root / (role + ext)
            p.write_bytes(role.encode())
            self.paths[role] = p
        self.inputs = bind_inputs(self.paths)

    def run_fixture(self):
        run = self.root / 'project' / '99_项目状态' / 'story_run.json'
        init_run(run_file=run, project_dir=run.parent.parent, confirmed_text=self.paths['confirmed_text'], greenscreen_video=self.paths['greenscreen_video'], audio=self.paths['audio'], subtitle_txt=self.paths['subtitle_txt'], production_inputs={k: v for k, v in self.paths.items() if k not in {'confirmed_text', 'greenscreen_video', 'audio', 'subtitle_txt'}})
        return run

    def package_fixture(self):
        sources = {}
        for role in ADVANCED_ROLES:
            if role in {'music', 'customer_manuscript'}:
                sources[role] = self.paths['finished_music' if role == 'music' else 'final_word']
            else:
                p = self.root / (role + '.bin')
                p.write_bytes(role.encode())
                sources[role] = p
        return dict(output_root=self.root / 'customer', receipt=self.root / 'status' / 'pack.json', sources=sources, story_name='测试', inputs=self.inputs)

    def test_v2_inputs_no_external_generated_content(self):
        run = load_run(self.run_fixture())
        self.assertEqual(set(run['work_packages']), set(PACKAGES))
        self.assertNotIn('music', run['work_packages'])
        self.assertEqual(set(run['inputs']), set(INPUTS))
        self.assertTrue(set(EXCLUDED).isdisjoint(set(__import__('story_run').CODEX_NATIVE_REQUIRED_ARTIFACTS) - set(EXCLUDED)))

    def test_missing_explicit_input_is_rejected(self):
        with self.assertRaises(ValueError):
            bind_inputs({k: v for k, v in self.paths.items() if k != 'final_word'})

    def test_word_music_copy_and_repack_preserves_user_file_without_encoder(self):
        args = self.package_fixture()
        with patch('subprocess.run', side_effect=AssertionError('pack must not execute media tools')):
            first = package(**args)
            dest = Path(first['artifacts'][0]['path']).parent / '用户补充.docx'
            dest.write_bytes(b'user')
            second = package(**args)
        self.assertEqual(dest.read_bytes(), b'user')
        self.assertEqual({k:v for k,v in first.items() if k != "reused"}, {k:v for k,v in second.items() if k != "reused"})
        self.assertTrue(second["reused"])
        for item in second['artifacts']:
            if item['role'].endswith(':music'):
                self.assertEqual(Path(item['path']).suffix, '.wav')
                self.assertEqual(sha(item['path']), self.inputs['finished_music']['sha256'])
            if item['role'].endswith(':customer_manuscript'):
                self.assertEqual(sha(item['path']), self.inputs['final_word']['sha256'])

    def test_interrupted_repackage_accepts_prior_and_target_hashes(self):
        import shutil
        args = self.package_fixture()
        package(**args)
        args['sources']['demo'].write_bytes(b'updated demo')
        args['sources']['background_image'].write_bytes(b'updated background')
        original = shutil.copy2
        calls = []
        def fail_second(source, target):
            calls.append(source)
            if len(calls) == 2:
                raise OSError('injected interruption')
            return original(source, target)
        with patch('story_managed_package.shutil.copy2', side_effect=fail_second):
            with self.assertRaises(OSError):
                package(**args)
        result = package(**args)
        self.assertEqual(result['status'], 'complete')
        for item in result['artifacts']:
            self.assertEqual(item['sha256'], sha(item['source']['path']))

    def test_user_edit_of_managed_file_is_not_overwritten(self):
        args = self.package_fixture()
        p = package(**args)
        dest = Path(p['artifacts'][0]['path'])
        dest.write_bytes(b'user revised')
        with self.assertRaisesRegex(ValueError, 'User changed'):
            package(**args)
        self.assertEqual(dest.read_bytes(), b'user revised')

    def test_changed_input_invalidates_only_actual_consumers(self):
        run = self.run_fixture()
        for role in ('final_word', 'finished_music'):
            p = self.root / (role + '.receipt')
            p.write_text(role)
            record_run(run_file=run, package='product_assets', status='running', artifact_id=role + '_copy', artifact_path=p, input_hashes={role: self.inputs[role]['sha256']})
        self.paths['final_word'].write_bytes(b'new word')
        self.assertEqual(set(dependency_staleness(load_run(run))), {'final_word_copy'})

    def test_changed_upstream_artifact_detected_without_status_rewrite(self):
        run = self.run_fixture()
        p = self.root / 'upstream'
        p.write_text('original')
        record_run(run_file=run, package='product_assets', status='running', artifact_id='up', artifact_path=p)
        q = self.root / 'down'
        q.write_text('down')
        record_run(run_file=run, package='product_assets', status='running', artifact_id='down', artifact_path=q, input_hashes={'up': sha(p)})
        p.write_text('changed')
        self.assertIn('down', dependency_staleness(load_run(run)))

    def test_checklist_needs_only_agent_owned_roles(self):
        args = self.package_fixture()
        pack = package(**args)
        items = pack['artifacts']
        for role in ('main_release_video', 'library_release_video'):
            p = self.root / (role + '.mp4')
            p.write_bytes(role.encode())
            items.append({'role': role, **binding(p)})
        p = self.root / 'checklist.json'
        write(p, {'production_contract': VERSION, 'status': 'complete', 'missing': [], 'artifacts': items})
        self.assertEqual(len(validate_checklist(p)['artifacts']), len(DELIVERY_ROLES))

    def test_prompt_exact_template_and_reference_hash(self):
        self.paths['story_requirements'].write_text('{}')
        self.paths['packaging_prompt'].write_text('顶部：{story_type}《{story_name}》。下部：{duration_text}，{age_range}。风格：{theme_style}。固定内容。')
        self.inputs = bind_inputs(self.paths)
        out = self.root / 'prompt.txt'
        receipt = self.root / 'prompt.json'
        fields = dict(story_name='新故事', story_type='寓言', duration_text='2分30秒', age_range='6岁以上', theme_style='水彩')
        bind_packaging(inputs=self.inputs, fields=fields, output=out, receipt=receipt)
        self.assertEqual(out.read_text(), self.paths['packaging_prompt'].read_text().format(**fields))
        validate_packaging(receipt, self.inputs)
        self.paths['packaging_reference'].write_bytes(b'changed')
        with self.assertRaises(ValueError):
            validate_packaging(receipt, self.inputs)

    def test_material_text_exact_punctuation_and_order(self):
        word = self.paths['final_word']
        with zipfile.ZipFile(word, 'w') as z:
            z.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>开场。小兔说：“好！”结尾。</w:t></w:r></w:p></w:body></w:document>')
        director = self.root / 'director.json'
        write(director, {'shots': [{'shot_id': 's1', 'story_text': '小兔说：“好！”', 'source_start': 1, 'source_end': 2}]})
        image = self.root / 'image.png'
        image.write_bytes(b'image')
        rows = [dict(shot_id=k, word_text=t, duration_seconds=1, poster_path=str(image)) for k, t in [('TITLE', '开场。'), ('s1', ''), ('MORAL', '结尾。')]]
        with patch('static_ppt_contract.validate_plan', return_value=(['TITLE', 's1', 'MORAL'], rows)):
            result = material_rows(director, self.root / 'plan', word)
            self.assertEqual([r['start_seconds'] for r in result], [0, 1, 2])
            self.assertEqual(result[1]['text'], '小兔说：“好！”')
            self.assertEqual(result[0]['display_text'], '开场')
            rows[0]['word_text'] = '猜测内容'
            with self.assertRaisesRegex(ValueError, 'verbatim'):
                material_rows(director, self.root / 'plan', word)

    def test_no_ppt_required_to_enter_formal_media_checks(self):
        from story_candidate_cli import render_media
        run = load_run(self.run_fixture())
        r = {'inputs': {}}
        for key in ('preset', 'background_image', 'story_frame', 'logo', 'background_with_subtitles', 'background_without_subtitles', 'body_srt', 'timeline_receipt'):
            p = self.root / key
            p.write_bytes(b'fixture')
            r['inputs'][key] = binding(p)
        with patch('story_timeline.validate_authoritative_timeline_receipt', side_effect=ValueError('formal authority required')):
            with self.assertRaisesRegex(ValueError, 'formal authority'):
                render_media(run, r)
if __name__ == '__main__':
    unittest.main()

class RealMaterialsChainTests(unittest.TestCase):

    def test_compiler_export_and_actual_directory_manifest(self):
        from tests.decoupled_material_fixture import material_fixture
        from story_materials import export_materials, validate_materials
        with tempfile.TemporaryDirectory() as d:
            args = material_fixture(Path(d))
            original_word = sha(args['inputs']['final_word']['path'])
            original_music = sha(args['inputs']['finished_music']['path'])
            with patch('story_video_synthesizer.media.probe_duration', return_value=22):
                result = export_materials(**args)
            self.assertEqual([r['shot_id'] for r in result['rows']], ['TITLE', 'shot-001', 'shot-002'])
            self.assertEqual([r['word_start'] for r in result['rows']], [0, 3, 10])
            validate_materials(args['receipt'], args['inputs'])
            self.assertEqual(original_word, sha(args['inputs']['final_word']['path']))
            self.assertEqual(original_music, sha(args['inputs']['finished_music']['path']))
            directory = args['output']
            manifest_path = directory / 'manifest.json'
            manifest = json.loads(manifest_path.read_text())
            manifest['rows'][0]['text'] = '偷偷修改'
            write(manifest_path, manifest)
            result['directory'] = binding(directory)
            write(args['receipt'], result)
            with self.assertRaisesRegex(ValueError, 'Packaged manifest'):
                validate_materials(args['receipt'], args['inputs'])

class V2FinalizationTests(unittest.TestCase):

    def test_v2_finalizes_without_external_deliverables_and_rejects_missing_review(self):
        """Exercise real delivery gates over a synthetic legacy-independent upstream fixture."""
        from test_story_run import StoryRunLedgerTests
        from tests.decoupled_material_fixture import material_fixture
        from story_materials import export_materials
        from story_run import atomic_write_json
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            helper = StoryRunLedgerTests()
            project, text, video, audio, runfile = helper.fixture(root)
            init_run(run_file=runfile, project_dir=project, confirmed_text=text, greenscreen_video=video, audio=audio)
            helper.record_finalize_profile(root, runfile)
            run = load_run(runfile)
            run['production_contract'] = VERSION
            run['work_packages'] = {k: {'status': 'done', 'blocker': ''} for k in PACKAGES}
            for role in EXCLUDED:
                run['artifacts'].pop(role, None)
            args = material_fixture(root / 'materials')
            args['inputs']['audio'] = run['inputs']['audio']
            stack.enter_context(patch('story_video_synthesizer.media.probe_duration', return_value=22))
            material = export_materials(**args)
            run['inputs'].update(args['inputs'])
            for role in ('subtitle_srt', 'story_requirements', 'packaging_reference', 'packaging_prompt'):
                p = root / (role + '.txt')
                p.write_text('{}' if role == 'story_requirements' else 'confirmed')
                run['inputs'][role] = binding(p)
            prompt_receipt = root / 'prompt_receipt.json'
            bind_packaging(inputs=run['inputs'], fields=dict(story_name='测试', story_type='寓言', age_range='6岁', duration_text='22秒', theme_style='水彩'), output=root / 'compiled_prompt.txt', receipt=prompt_receipt)
            old_checklist = json.loads(Path(run['artifacts']['final_delivery_checklist']['path']).read_text())
            from product_quality import _role_for_product_file
            sources = {_role_for_product_file(Path(i['path']).name): Path(i['path']) for i in old_checklist['artifacts'] if '进阶版' in i['path']}
            sources = {k: v for k, v in sources.items() if k in ADVANCED_ROLES}
            sources.update(customer_manuscript=Path(run['inputs']['final_word']['path']), music=Path(run['inputs']['finished_music']['path']), ppt_materials=args['output'])
            packfile = root / 'pack.json'
            pack = package(output_root=project / 'v2customer', receipt=packfile, sources=sources, story_name='测试', inputs=run['inputs'])
            checklist = Path(run['artifacts']['final_delivery_checklist']['path'])
            write(checklist, dict(production_contract=VERSION, status='complete', missing=[], artifacts=[*pack['artifacts'], *({'role': r, **run['artifacts'][r]} for r in ('main_release_video', 'library_release_video'))]))
            for role, path in [('managed_package_receipt', packfile), ('qa_product_report', packfile), ('ppt_materials_receipt', args['receipt']), ('packaging_prompt_receipt', prompt_receipt), ('final_delivery_checklist', checklist)]:
                run['artifacts'][role] = {'package': 'delivery', **binding(path), 'input_sha256s': {}}
            cm = Path(run['artifacts']['customer_media_receipt']['path'])
            payload = json.loads(cm.read_text())
            payload['music_source'] = run['inputs']['finished_music']
            write(cm, payload)
            run['artifacts']['customer_media_receipt'].update(binding(cm))
            qa = Path(run['artifacts']['qa_release_report']['path'])
            payload = json.loads(qa.read_text())
            payload['audio_contract']['music_bed'] = run['inputs']['finished_music']
            write(qa, payload)
            run['artifacts']['qa_release_report'].update(binding(qa))
            reviewpath = Path(run['artifacts']['final_delivery_review']['path'])
            review = json.loads(reviewpath.read_text())
            bundle = Path(review['artifact_path'])
            write(bundle, dict(artifacts=[*json.loads(checklist.read_text())['artifacts'], binding(checklist), binding(qa)]))
            review['artifact_sha256'] = sha(bundle)
            write(reviewpath, review)
            run['artifacts']['final_delivery_review'].update(binding(reviewpath))
            run['artifacts']['shot_storyboard_compile_receipt'].update(binding(args['compile_receipt']))
            release_path = Path(run['artifacts']['release_package_receipt']['path'])
            write(release_path, {'actual_geometry': {'main_package_spec': {'main_package_spec_sha256': sha(prompt_receipt)}}})
            run['artifacts']['release_package_receipt'].update(binding(release_path))
            atomic_write_json(runfile, run)
            for target in ('story_run.validate_compile_receipt', 'story_run.validate_theme_assets_manifest', 'story_artifact_validation.validate_release_package_receipt'):
                stack.enter_context(patch(target, return_value={}))
            for target in ('story_run.semantic_card_generation_receipt_issues', 'story_run.semantic_card_motion_receipt_issues'):
                stack.enter_context(patch(target, return_value=[]))
            stack.enter_context(patch('story_artifact_validation.probe_duration', return_value=2))
            result = finalize_run(run_file=runfile, required_artifacts=[])
            self.assertTrue(result['finalized_at'])
            for role in EXCLUDED:
                self.assertNotIn(role, result['artifacts'])
            review['score'] = 84
            write(reviewpath, review)
            run = load_run(runfile)
            run['artifacts']['final_delivery_review'].update(binding(reviewpath))
            atomic_write_json(runfile, run)
            with self.assertRaises(RuntimeError):
                finalize_run(run_file=runfile, required_artifacts=[])

class IndependentProvenanceTests(unittest.TestCase):
    def test_self_review_and_missing_critical_errors_fail(self):
        from story_production_v2 import validate_independent_approval
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / 'bundle.json'
            artifact.write_text('{}')
            review = dict(approved=True, score=90, artifact_sha256=sha(artifact))
            with self.assertRaises(ValueError):
                validate_independent_approval(review, artifact, producer_context='producer')
            review.update(critical_errors=[], reviewer_context='producer', independent_context=True)
            with self.assertRaises(ValueError):
                validate_independent_approval(review, artifact, producer_context='producer')
            review['reviewer_context'] = 'independent-reviewer'
            validate_independent_approval(review, artifact, producer_context='producer')
            review.pop('critical_errors')
            with self.assertRaises(ValueError):
                validate_independent_approval(review, artifact, producer_context='producer')

class PreviewAndOutputProtectionTests(unittest.TestCase):
    def test_preview_review_and_release_geometry_share_current_hashes(self):
        from release_geometry import compile_demo_presenter_geometry
        from story_media_preview import approve_preview, load_approved
        from demo_quality import load_preview_demo_geometry_for_release_review
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=root/'foreground.webm';source.write_bytes(b'fixture foreground')
            preset=root/'preset.json';preset.write_text('{}')
            geometry=compile_demo_presenter_geometry(1920,1080,1920,1080,person_crop=None,detected_bbox=None,person_height_ratio=1,crop_mode='source-native',crop_bottom_ratio=0,vertical_alignment='center',keying_preset_sha256=sha(preset),keying_lock_sha256='a'*64,source_greenscreen_sha256=sha(source),production_keying_filter_fingerprint='b'*64)
            frames=[]
            for i in range(5):
                p=root/f'frame{i}.png';p.write_bytes(f'frame{i}'.encode());frames.append(binding(p))
            preview=root/'preview.json';write(preview,{'schema_version':'story-media-preview/v2','producer_context':'producer','project':str(root),'presenter_geometry':geometry,'inputs':{'preset':binding(preset),'foreground':binding(source)},'run_inputs':{},'artifacts':frames})
            review=root/'review.json';write(review,{'approved':True,'score':90,'critical_errors':[],'independent_context':True,'reviewer_context':'reviewer','artifact_sha256':sha(preview)})
            approved=root/'approved.json';approve_preview(preview,review,approved,root)
            self.assertEqual(load_preview_demo_geometry_for_release_review(approved,root)[1],geometry)
            source.write_bytes(b'changed')
            with self.assertRaises(ValueError):load_approved(approved,root)
    def test_material_and_prompt_writers_cannot_overwrite_inputs(self):
        from story_materials import export_materials
        from tests.decoupled_material_fixture import material_fixture
        with tempfile.TemporaryDirectory() as directory:
            args=material_fixture(Path(directory));source=Path(args['inputs']['final_word']['path']);before=source.read_bytes();args['output']=source
            with self.assertRaisesRegex(ValueError,'protected input'):export_materials(**args)
            self.assertEqual(source.read_bytes(),before)
            with self.assertRaisesRegex(ValueError,'protected input'):
                bind_packaging(inputs=args['inputs'],fields={},output=source,receipt=Path(directory)/'receipt')
            self.assertEqual(source.read_bytes(),before)
    def test_library_panels_must_be_in_independent_review_bundle(self):
        from story_materials import validate_panel_binding
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);reference=root/'reference.png';reference.write_bytes(b'ref');template=root/'template.txt';template.write_text('{story_name} {duration_text} {theme_style}')
            inputs={'packaging_prompt':binding(template),'packaging_reference':binding(reference)}
            spec=root/'spec.json';bind_packaging(inputs=inputs,fields=dict(story_name='测试',story_type='寓言',age_range='6岁',duration_text='3秒',theme_style='水彩'),output=root/'prompt.txt',receipt=spec)
            top=root/'top.png';top.write_bytes(b'top');bottom=root/'bottom.png';bottom.write_bytes(b'bottom')
            bundle=root/'bundle.json';write(bundle,{'artifacts':[binding(top),binding(spec)]})
            review=root/'review.json';write(review,dict(approved=True,score=90,critical_errors=[],independent_context=True,reviewer_context='reviewer',artifact_sha256=sha(bundle)))
            generation=root/'generation.json';write(generation,{'schema_version':'story-confirmed-panels/v2','producer_context':'producer','prompt_receipt_sha256':sha(spec),'reference_attached':True,'imagegen_native':True,'request_id':'offline-fixture','scope_checks':{scope:{key:True for key in rule['review_checks']} for scope,rule in json.loads(spec.read_text())['visual_scopes'].items() if scope in ('main','library')},'review':binding(review),'review_bundle':binding(bundle),'outputs':{'library_top_plate':binding(top),'library_bottom_plate':binding(bottom)}})
            config=SimpleNamespace(main_top_panel=None,main_bottom_panel=None,library_top_panel=top,library_bottom_panel=bottom,duration_text='3秒',story_name='测试')
            with self.assertRaisesRegex(ValueError,'reviewed current output'):validate_panel_binding(config,spec,generation)
            write(bundle, {'artifacts': [binding(top), binding(bottom), binding(spec)]})
            review_data = json.loads(review.read_text()); review_data['artifact_sha256'] = sha(bundle)
            write(review, review_data)
            generation_data = json.loads(generation.read_text())
            generation_data.update(review=binding(review), review_bundle=binding(bundle))
            # No aggregate checks/simple_layout: only main's scoped check applies.
            write(generation, generation_data)
            validate_panel_binding(config, spec, generation)
