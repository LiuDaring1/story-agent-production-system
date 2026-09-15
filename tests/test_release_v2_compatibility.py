import copy
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from PIL import Image
from keying_quality import production_keying_fingerprint
from release_geometry import canonical_sha256
from unittest.mock import patch

from release_video import (compile_v2_release_spec, compile_release_geometry,
    load_release_contract_spec, build_release_render_manifest, sha256_path,
    validate_main_preview_mode_coverage)
from release_geometry import binding_payload, geometry_manifest_issues
from tests import test_release_geometry as fixture
from tests.release_safety_fixture import write_scan_report
from story_release_policy import release_safety_requirements


class ReleaseV2CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = fixture._config(self.root)
        self.config.bg_video.write_bytes(b'fixture reviewed music background')
        self.project = self.root / 'project'
        self.semantic_path = self.project / '03_产品素材/theme_assets/semantic_cards/artifact_semantic_plan.json'
        self.semantic_path.parent.mkdir(parents=True)
        inputs = {}
        for role in ('confirmed_text', 'subtitle_txt', 'subtitle_srt', 'audio', 'final_word', 'story_requirements'):
            path = self.root / role; path.write_text(role); inputs[role] = self.bind(path)
        self.timeline = self.write('timeline.json', {'audio_duration_seconds': 60.0})
        self.projection_payload = {
            'requirements': release_safety_requirements(source='unit fixture'),
            'acceptance_evidence': [
                'presenter_body_overflow_report', 'release_plan_compliance',
                'decoded_video_execution', 'shared_frame_derivation',
                'independent_visual_review',
            ],
        }
        self.projection = self.write('projection.json', self.projection_payload)
        self.semantic = {'schema_version': 'story-v2-artifact-semantics-preparation/v1',
                         'production_contract': 'story-production/v2', 'inputs': copy.deepcopy(inputs),
                         'authoritative_timeline_receipt': self.bind(self.timeline)}
        self.semantic_path.write_text(json.dumps(self.semantic))
        evidence = {'artifact_semantic_plan_sha256': sha256_path(self.semantic_path)}
        self.generation = self.write('generation.json', evidence)
        self.motion = self.write('motion.json', evidence)
        self.request = self.write('semantic_card_motion_request.json', evidence)
        self.approved = self.write('approved_demo.json', {'schema_version': 'story-approved-demo/v2'})
        self.preview_path = self.write('preview.json', {})
        self.customer_media_receipt = self.write('customer_media_receipt.json', {'fixture': True})
        self.theme_manifest = self.write('theme_assets_manifest.json', {'fixture': True})
        self.logo = self.root / 'logo.png'; Image.new('RGBA',(150,90),(220,150,20,255)).save(self.logo)
        self.run_file = self.write('run.json', {})
        self.ledger = {'production_contract': 'story-production/v2', 'project_dir': str(self.project),
                      'inputs': inputs, 'artifacts': {
                          'requirements_projection': self.bind(self.projection),
                          'authoritative_timeline_receipt': self.bind(self.timeline),
                          'semantic_card_generation_receipt': self.bind(self.generation),
                          'semantic_card_motion_receipt': self.bind(self.motion),
                          'customer_media_receipt': self.bind(self.customer_media_receipt),
                          'theme_assets_manifest': self.bind(self.theme_manifest)}}
        self.config = replace(self.config, artifact_semantic_plan=self.semantic_path,
                              output_dir=self.project / "release", b_windows=((1.0, 2.0),), c_windows=((3.0, 4.0),), story_box=(210, 270, 910, 512), demo_render_manifest=self.approved,
                              keyer='rvm', story_logo=self.logo, antipiracy_logo=self.logo,
                              frame_image_b=None, person_layout_policy='source-native-fixed-anchor/v2',
                              person_height=1080, person_y=0,
                              subtitle_srt=Path(inputs['subtitle_srt']['path']),
                              audio_mix=Path(inputs['audio']['path']), mix_bg_audio=True,
                              release_producer_context='producer')
        from story_scene_windows import prepare_plan
        def scan(source, **kwargs):
            report_path = kwargs.pop('report_path')
            return write_scan_report(source, report_path, duration=60., **kwargs)
        self.windows = self.project / 'windows.json'
        with patch('story_run.load_run', return_value=self.ledger), \
             patch('story_timeline.validate_authoritative_timeline_receipt'), \
             patch('presenter_layout.scan_rvm_body_overflow', side_effect=scan):
            prepare_plan(
                self.run_file, self.windows,
                presenter_foreground=self.config.person_greenscreen,
                fixed_anchor_x=self.config.person_x,
                fixed_anchor_y=self.config.person_y,
                source_width=1920, source_height=1080,
                rendered_height=1080,
                person_layout_policy=self.config.person_layout_policy,
                b_windows='1-2', c_windows='3-4',
            )
        self.windows_review = self.write('windows_review.json', {
            'approved': True, 'score': 94, 'critical_errors': [],
            'reviewer_context': 'independent', 'independent_context': True,
            'artifact_sha256': sha256_path(self.windows)})
        self.config = replace(self.config, release_windows_plan=self.windows,
                              release_windows_review=self.windows_review)
        self.demo = fixture.ReleaseGeometryTests._demo_geometry(self.config)
        self.demo['production_keying_filter_fingerprint'] = production_keying_fingerprint(json.loads(self.config.keying_preset_path.read_text()))
        self.demo.pop('geometry_sha256')
        self.demo['geometry_sha256'] = canonical_sha256(self.demo)
        self.raw_preview = {'inputs': {k: self.bind(p) for k, p in {
            'preset': self.config.keying_preset_path, 'background_image': self.config.bg_image,
            'story_frame': self.config.frame_image, 'logo': self.logo,
            'foreground': self.config.person_greenscreen}.items()}}
        self.approval = {'preview': self.bind(self.preview_path)}
        self.patches = [
            patch('story_run.load_run', return_value=self.ledger),
            patch('story_requirements.validate_run_projection', return_value=self.projection_payload),
            patch('story_run.validate_theme_assets_manifest', return_value={
                'artifacts': {'story_frame_png': self.bind(self.config.frame_image)}}),
            patch('story_timeline.validate_authoritative_timeline_receipt', return_value={}),
            patch('semantic_card_motion.semantic_card_generation_receipt_issues', return_value=[]),
            patch('semantic_card_motion.semantic_card_motion_receipt_issues', return_value=[]),
            patch('story_media_preview.load_approved', return_value=(self.approval, self.demo)),
            patch('story_media_preview.validate_preview', return_value=self.raw_preview),
            patch('story_customer_media.validate_customer_media_receipt', return_value={
                'artifacts': {'product_background_without_subtitles': self.bind(self.config.bg_video)}}),
            patch('release_video.keying_preset_lock_issues', return_value=[]),
        ]
        for p in self.patches: p.start(); self.addCleanup(p.stop)

    def bind(self, p): return {'path': str(p), 'sha256': sha256_path(p)}
    def write(self, name, value):
        p = self.root / name; p.write_text(json.dumps(value)); return p
    def spec(self): return compile_v2_release_spec(self.config, self.run_file)

    def attach_release_review_evidence(self, geometry_path, review):
        from story_scene_windows import frame_templates
        from story_release_policy import RELEASE_REVIEW_EVIDENCE_SCHEMA
        plan = json.loads(self.windows.read_text())
        templates = frame_templates(self.config, self.root / 'review_frame_derivation')
        prepared = [{
            'time_seconds': row['time_seconds'],
            'mode': row['expected_mode'],
            'reasons': row['reasons'],
            'frame': self.bind(self.config.frame_image),
        } for row in plan['review_samples']]
        evidence = {
            'schema_version': RELEASE_REVIEW_EVIDENCE_SCHEMA,
            'applicable_requirements': self.bind(self.projection),
            'plan': self.bind(self.windows),
            'presenter_overflow_report': plan['presenter_protection']['report'],
            'frame_derivation': templates['derivation_receipt'],
            'prepared_frames': prepared,
        }
        self.write('release_review_evidence.json', evidence)
        review['checked_frames'] = prepared
        review['release_safety_checks'] = {
            key: True for key in (
                'plan_compliance', 'presenter_risk_intervals', 'shared_frame_identity',
                'switch_boundaries', 'transparent_aperture',
            )
        }
        return review

    def test_real_v2_bindings_need_no_legacy_contract_and_load_roundtrips(self):
        with patch('release_video.load_current_artifact_semantic_plan', side_effect=AssertionError('legacy path')):
            spec = self.spec()
        self.assertEqual(spec['project_dir'], str(self.project))
        self.assertNotIn('story_contract_sha256', spec)
        self.assertNotIn('contract_schema_version', spec)
        path = self.write('spec.json', spec)
        self.assertEqual(load_release_contract_spec(path), spec)
        bindings = binding_payload(spec, compiled_spec_sha256='a'*64,
            semantic_plan_path=self.semantic_path, keying_preset_path=self.config.keying_preset_path,
            keying_filter_fingerprint='b'*64)
        self.assertNotIn('story_contract_sha256', bindings)
        self.assertEqual(bindings['requirements_projection_sha256'], sha256_path(self.projection))
        self.assertEqual(bindings['artifact_semantic_plan_sha256'], sha256_path(self.semantic_path))

    def test_stale_projection_semantic_evidence_and_wrong_demo_inputs_fail(self):
        self.projection.write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'Changed binding'):
            self.spec()
        self.projection.write_text(json.dumps(self.projection_payload))
        self.request.write_text(json.dumps({'artifact_semantic_plan_sha256': '0'*64}))
        with self.assertRaisesRegex(ValueError, 'plan hash'):
            self.spec()
        self.request.write_text(json.dumps({'artifact_semantic_plan_sha256': sha256_path(self.semantic_path)}))
        other = self.root / 'other-logo'; other.write_bytes(b'official logo')
        self.config = replace(self.config, story_logo=other)
        with self.assertRaisesRegex(ValueError, 'approved Demo input: logo'):
            self.spec()

    def test_wrong_semantic_timeline_or_input_and_chroma_fallback_fail(self):
        self.semantic['inputs']['audio'] = self.bind(self.logo)
        self.semantic_path.write_text(json.dumps(self.semantic))
        with self.assertRaisesRegex(ValueError, 'input mismatch: audio'):
            self.spec()
        self.config = replace(self.config, keyer='colorkey')
        with self.assertRaisesRegex(ValueError, 'requires RVM'):
            self.spec()

    def test_formal_entry_rejects_independent_b_frame_and_scan_geometry_drift(self):
        self.config = replace(self.config, frame_image_b=self.logo)
        with self.assertRaisesRegex(ValueError, 'forbids --frame-image-b'):
            self.spec()
        self.config = replace(self.config, frame_image_b=None, person_x=self.config.person_x + 1)
        with self.assertRaisesRegex(ValueError, 'does not bind the actual'):
            self.spec()

    def test_approved_demo_transform_cannot_disagree_with_scan_geometry(self):
        self.demo['rendered_height'] = self.config.person_height - 1
        with self.assertRaisesRegex(ValueError, 'approved/actual A-shot transform: rendered_height'):
            self.spec()

    def test_release_requires_current_narration_music_background_and_mix(self):
        wrong_audio = self.root / 'wrong-audio'; wrong_audio.write_text('wrong')
        self.config = replace(self.config, audio_mix=wrong_audio)
        with self.assertRaisesRegex(ValueError, 'audio_mix differs'):
            self.spec()
        self.config = replace(self.config, audio_mix=Path(self.ledger['inputs']['audio']['path']), mix_bg_audio=False)
        with self.assertRaisesRegex(ValueError, 'must mix'):
            self.spec()
        self.config = replace(self.config, mix_bg_audio=True, bg_video=self.root / 'wrong-background')
        self.config.bg_video.write_text('wrong')
        with self.assertRaisesRegex(ValueError, 'bg_video differs'):
            self.spec()

    def test_bound_media_change_invalidates_preview_geometry(self):
        spec = self.spec()
        preview = self.compile(spec)
        original_sha = preview['formal_render_binding_sha256']
        narration = Path(self.ledger['inputs']['audio']['path'])
        narration.write_text('new authoritative narration')
        self.ledger['inputs']['audio'] = self.bind(narration)
        self.semantic['inputs']['audio'] = self.bind(narration)
        self.semantic_path.write_text(json.dumps(self.semantic))
        evidence = {'artifact_semantic_plan_sha256': sha256_path(self.semantic_path)}
        for path in (self.generation, self.motion, self.request):
            path.write_text(json.dumps(evidence))
        self.ledger['artifacts']['semantic_card_generation_receipt'] = self.bind(self.generation)
        self.ledger['artifacts']['semantic_card_motion_receipt'] = self.bind(self.motion)
        changed = self.spec()
        self.assertNotEqual(changed['release_parameters_sha256'], spec['release_parameters_sha256'])
        self.assertNotEqual(self.compile(changed)['formal_render_binding_sha256'], original_sha)

    def compile(self, spec, preview=True):
        with (patch('release_video.load_current_artifact_semantic_plan', side_effect=AssertionError('legacy loader')),
             patch('release_video.required_package_asset_binding', return_value={}),
             patch('release_video.load_preview_demo_geometry_for_release_review', return_value=(self.approval, self.demo))):
            return compile_release_geometry(self.config, spec, preview=preview)

    def test_geometry_manifest_uses_v2_bindings_and_preserves_abc(self):
        spec = self.spec(); geometry = self.compile(spec)
        self.assertEqual(geometry_manifest_issues(geometry), [])
        self.assertEqual(geometry['presenter']['a']['x'], self.config.person_x)
        self.assertEqual(geometry['presenter']['a']['initial_anchor_basis'], 'reviewed_initial_anchor_x')
        self.assertEqual(geometry['main']['story_region_a'], [210,270,910,512])
        self.assertEqual(geometry['presenter']['c']['source_crop'], self.demo['source_crop'])
        self.assertEqual(geometry['presenter']['c']['x'], self.demo['x'])
        manifest = build_release_render_manifest(self.config, spec, [], geometry)
        self.assertEqual(manifest['production_contract'], 'story-production/v2')
        self.assertNotIn('story_contract_sha256', manifest)
        with self.assertRaisesRegex(ValueError, 'stale'):
            altered = copy.deepcopy(spec); altered['variants'][0]['regions'][0]['width'] = .5
            self.compile(altered)
        self.config = replace(self.config, detected_person_bbox=(500,100,500,900))
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.compile(spec)

    def test_formal_requires_independent_review_of_current_geometry(self):
        spec = self.spec()
        with self.assertRaisesRegex(ValueError, 'independently approved'):
            self.compile(spec, preview=False)

    def test_current_independent_geometry_review_allows_formal_and_tamper_fails(self):
        spec = self.spec(); preview = self.compile(spec)
        geometry_path = self.write('reviewed_geometry.json', preview)
        review = {'approved': True, 'score': 90, 'critical_errors': [],
                  'reviewer_context': 'independent-reviewer', 'independent_context': True,
                  'artifact_sha256': sha256_path(geometry_path)}
        self.attach_release_review_evidence(geometry_path, review)
        review_path = self.write('review.json', review)
        self.config = replace(self.config, approved_preview_geometry=geometry_path,
                              approved_preview_review=review_path)
        formal = self.compile(spec, preview=False)
        self.assertEqual(formal['formal_render_binding_sha256'], preview['formal_render_binding_sha256'])
        evidence_path = self.root / 'release_review_evidence.json'
        evidence = json.loads(evidence_path.read_text())
        alternate_projection = self.write('alternate_projection.json', self.projection_payload)
        evidence['applicable_requirements'] = self.bind(alternate_projection)
        evidence_path.write_text(json.dumps(evidence))
        with self.assertRaisesRegex(ValueError, 'another applicable-requirements'):
            self.compile(spec, preview=False)
        evidence['applicable_requirements'] = self.bind(self.projection)
        evidence_path.write_text(json.dumps(evidence))
        review['reviewer_context'] = 'producer'; review_path.write_text(json.dumps(review))
        with self.assertRaisesRegex(ValueError, 'Independent review context'):
            self.compile(spec, preview=False)
        review['reviewer_context'] = 'independent-reviewer'; review_path.write_text(json.dumps(review))
        geometry_path.write_text(json.dumps({**preview, 'forged': True}))
        with self.assertRaisesRegex(ValueError, 'current artifact'):
            self.compile(spec, preview=False)

    def test_unrelated_ledger_progress_does_not_invalidate_bound_inputs(self):
        spec = self.spec()
        self.ledger['artifacts']['unrelated_materials_receipt'] = {'path': '/not-consumed', 'sha256': 'f'*64}
        self.assertEqual(self.spec(), spec)
        self.config = replace(self.config, output_dir=self.root / 'outside-project')
        with self.assertRaisesRegex(ValueError, 'outside current project'):
            self.spec()

    def test_explicit_windows_must_match_current_reviewed_source(self):
        self.config = replace(self.config, release_windows_plan=None, release_windows_review=None)
        with self.assertRaisesRegex(ValueError, 'reviewed A/B/C windows plan'):
            self.spec()
        self.config = replace(self.config, b_windows=((1.0, 2.0),), c_windows=((3.0, 4.0),),
                              release_windows_plan=self.windows, release_windows_review=self.windows_review)
        spec = self.spec()
        self.assertEqual(spec['release_windows_evidence']['plan']['sha256'], sha256_path(self.windows))
        self.config = replace(self.config, b_windows=((1.0, 2.1),))
        with self.assertRaisesRegex(ValueError, 'differ from reviewed source'):
            self.spec()

    def test_legacy_or_handwritten_windows_without_scan_cannot_bypass_v2(self):
        path = self.write('legacy_windows.json', {
            'schema_version': 'story-project-release-windows-plan/v1',
            'inputs': {'subtitle_srt': self.ledger['inputs']['subtitle_srt'],
                       'authoritative_timeline_receipt': self.bind(self.timeline)},
            'rules': [], 'b_windows': '1-2', 'c_windows': '3-4',
        })
        self.config = replace(self.config, release_windows_plan=path)
        with self.assertRaisesRegex(ValueError, 'lacks current presenter protection'):
            self.spec()

    def test_long_main_release_rejects_empty_or_unmeasured_abc_windows(self):
        self.config = replace(self.config, b_windows=(), c_windows=((3.0, 4.0),))
        windows = json.loads(self.windows.read_text())
        windows['b_windows'] = ''
        path = self.write('missing_b_windows.json', windows)
        review_payload = {'approved': True, 'score': 94,
            'critical_errors': [], 'reviewer_context': 'independent', 'independent_context': True,
            'artifact_sha256': sha256_path(path)}
        review = self.write('missing_b_windows_review.json', review_payload)
        self.config = replace(self.config, release_windows_plan=path, release_windows_review=review)
        with self.assertRaisesRegex(ValueError, 'segments do not match|non-empty reviewed B and C windows'):
            self.spec()
        self.timeline.write_text('{}')
        self.ledger['artifacts']['authoritative_timeline_receipt'] = self.bind(self.timeline)
        self.semantic['authoritative_timeline_receipt'] = self.bind(self.timeline)
        self.semantic_path.write_text(json.dumps(self.semantic))
        evidence = {'artifact_semantic_plan_sha256': sha256_path(self.semantic_path)}
        for evidence_path in (self.generation, self.motion, self.request):
            evidence_path.write_text(json.dumps(evidence))
        self.ledger['artifacts']['semantic_card_generation_receipt'] = self.bind(self.generation)
        self.ledger['artifacts']['semantic_card_motion_receipt'] = self.bind(self.motion)
        windows['inputs']['authoritative_timeline_receipt'] = self.bind(self.timeline)
        path.write_text(json.dumps(windows))
        review_payload['artifact_sha256'] = sha256_path(path); review.write_text(json.dumps(review_payload))
        with self.assertRaisesRegex(ValueError, 'valid authoritative duration'):
            self.spec()

    def test_preview_samples_must_cover_a_and_every_b_c_window(self):
        validate_main_preview_mode_coverage(
            [1.5, 3.5, 5.0], ((1.0, 2.0),), ((3.0, 4.0),))
        with self.assertRaisesRegex(ValueError, 'B2'):
            validate_main_preview_mode_coverage(
                [1.5, 3.5, 5.0], ((1.0, 2.0), (6.0, 7.0)), ((3.0, 4.0),))
        with self.assertRaisesRegex(ValueError, 'A'):
            validate_main_preview_mode_coverage(
                [1.5, 3.5], ((1.0, 2.0),), ((3.0, 4.0),))

    def test_windows_plan_joins_existing_preview_review_and_changed_plan_invalidates(self):
        self.config = replace(self.config, release_windows_review=None)
        spec = compile_v2_release_spec(self.config, self.run_file, preview=True)
        preview = self.compile(spec)
        geometry_path = self.write('merged_review_geometry.json', preview)
        review_path = self.write('merged_review.json', {'approved': True, 'score': 90, 'critical_errors': [],
            'reviewer_context': 'independent-merged', 'independent_context': True,
            'artifact_sha256': sha256_path(geometry_path)})
        merged_review = json.loads(review_path.read_text())
        self.attach_release_review_evidence(geometry_path, merged_review)
        review_path.write_text(json.dumps(merged_review))
        self.config = replace(self.config, approved_preview_geometry=geometry_path, approved_preview_review=review_path)
        formal = self.compile(spec, preview=False)
        self.assertEqual(formal['formal_render_binding_sha256'], preview['formal_render_binding_sha256'])
        payload = json.loads(self.windows.read_text());payload['rule_note']='changed current plan'
        self.windows.write_text(json.dumps(payload))
        with self.assertRaisesRegex(ValueError, 'stale|mismatch'):
            self.compile(spec, preview=False)

    def test_auto_preparation_reaches_real_compiler_and_missing_arguments_fail(self):
        from story_scene_windows import prepare_plan
        from release_video import parse_b_windows
        self.timeline.write_text(json.dumps({'audio_duration_seconds':42.0}))
        self.config.subtitle_srt.write_text('1\n00:00:00,000 --> 00:00:41,000\ntext\n')
        self.ledger['inputs']['subtitle_srt'] = self.bind(self.config.subtitle_srt)
        self.ledger['artifacts']['authoritative_timeline_receipt'] = self.bind(self.timeline)
        self.semantic['inputs']['subtitle_srt'] = self.bind(self.config.subtitle_srt)
        self.semantic['authoritative_timeline_receipt'] = self.bind(self.timeline)
        self.semantic_path.write_text(json.dumps(self.semantic))
        for path in (self.generation,self.motion,self.request):
            path.write_text(json.dumps({'artifact_semantic_plan_sha256':sha256_path(self.semantic_path)}))
        self.ledger['artifacts']['semantic_card_generation_receipt'] = self.bind(self.generation)
        self.ledger['artifacts']['semantic_card_motion_receipt'] = self.bind(self.motion)
        def scan(source, **kwargs):
            report_path = kwargs.pop('report_path')
            return write_scan_report(source, report_path, duration=42., **kwargs)
        plan_path=self.project/'auto_windows.json'
        with patch('presenter_layout.scan_rvm_body_overflow', side_effect=scan):
            plan=prepare_plan(
                self.run_file,plan_path,presenter_foreground=self.config.person_greenscreen,
                fixed_anchor_x=self.config.person_x,fixed_anchor_y=self.config.person_y,
                source_width=1920,source_height=1080,rendered_height=1080,
                person_layout_policy=self.config.person_layout_policy,
            )
        self.config=replace(self.config,release_windows_plan=plan_path,release_windows_review=None,
            b_windows=parse_b_windows(plan['b_windows']),c_windows=parse_b_windows(plan['c_windows']))
        spec=compile_v2_release_spec(self.config,self.run_file,preview=True)
        self.assertEqual(spec['layout_parameters']['b_windows'],[[15.,33.]])
        self.assertEqual(spec['layout_parameters']['c_windows'],[[0.,15.]])
        self.config=replace(self.config,b_windows=())
        with self.assertRaisesRegex(ValueError,'differ from reviewed source'):
            compile_v2_release_spec(self.config,self.run_file,preview=True)

    def test_formal_cli_auto_cannot_skip_presenter_scan(self):
        import release_video
        argv = [
            'release_video.py', '--story-name', 'fixture', '--duration-text', '60秒',
            '--age-text', 'fixture', '--bg-video', str(self.config.bg_video),
            '--output-dir', str(self.config.output_dir), '--variant', 'main',
            '--keying-preset-json', str(self.config.keying_preset_path),
            '--run-file', str(self.run_file),
        ]
        with patch.object(sys, 'argv', argv), \
             patch('release_video.preflight_release_requirements'), \
             patch('release_video.load_keying_preset', return_value={
                 'keyer':'rvm', 'rvm_foreground_video':str(self.config.person_greenscreen),
                 'person_grade':'none', 'person_layout_policy':'source-native-fixed-anchor/v2',
                 'rvm_input_width':1920, 'rvm_input_height':1080,
             }), \
             patch('story_scene_windows.prepare_plan', side_effect=RuntimeError('presenter scan required')) as prepare:
            with self.assertRaisesRegex(RuntimeError, 'presenter scan required'):
                release_video.main()
        prepare.assert_called_once()

    def test_library_compiler_is_not_subject_to_main_windows_gate(self):
        self.config=replace(self.config,variant='library',b_windows=(),c_windows=(),
            release_windows_plan=None,release_windows_review=None)
        self.assertIsNone(self.spec()['release_windows_evidence'])
