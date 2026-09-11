import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from PIL import Image
from keying_quality import production_keying_fingerprint
from release_geometry import canonical_sha256
from unittest.mock import patch

from release_video import (compile_v2_release_spec, compile_release_geometry,
    load_release_contract_spec, build_release_render_manifest, sha256_path)
from release_geometry import binding_payload, geometry_manifest_issues
from tests import test_release_geometry as fixture


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
        self.timeline = self.write('timeline.json', {})
        self.projection = self.write('projection.json', {})
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
        self.logo = self.root / 'logo.png'; Image.new('RGBA',(150,90),(220,150,20,255)).save(self.logo)
        self.run_file = self.write('run.json', {})
        self.ledger = {'production_contract': 'story-production/v2', 'project_dir': str(self.project),
                      'inputs': inputs, 'artifacts': {
                          'requirements_projection': self.bind(self.projection),
                          'authoritative_timeline_receipt': self.bind(self.timeline),
                          'semantic_card_generation_receipt': self.bind(self.generation),
                          'semantic_card_motion_receipt': self.bind(self.motion),
                          'customer_media_receipt': self.bind(self.customer_media_receipt)}}
        self.config = replace(self.config, artifact_semantic_plan=self.semantic_path,
                              output_dir=self.project / "release", b_windows=(), c_windows=(), story_box=(210, 270, 910, 512), demo_render_manifest=self.approved,
                              keyer='rvm', story_logo=self.logo, antipiracy_logo=self.logo,
                              subtitle_srt=Path(inputs['subtitle_srt']['path']),
                              audio_mix=Path(inputs['audio']['path']), mix_bg_audio=True,
                              release_producer_context='producer')
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
            patch('story_requirements.validate_run_projection', return_value={}),
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
        self.projection.write_text('{}')
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
        review_path = self.write('review.json', review)
        self.config = replace(self.config, approved_preview_geometry=geometry_path,
                              approved_preview_review=review_path)
        formal = self.compile(spec, preview=False)
        self.assertEqual(formal['formal_render_binding_sha256'], preview['formal_render_binding_sha256'])
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
        self.config = replace(self.config, b_windows=((1.0, 2.0),))
        with self.assertRaisesRegex(ValueError, 'reviewed source plan'):
            self.spec()
        windows = {'schema_version': 'story-project-release-windows-plan/v1',
                   'inputs': {'subtitle_srt': self.ledger['inputs']['subtitle_srt'],
                              'authoritative_timeline_receipt': self.bind(self.timeline)},
                   'rules': [], 'b_windows': '1-2', 'c_windows': ''}
        path = self.write('windows.json', windows)
        review = self.write('windows_review.json', {'approved': True, 'score': 94, 'critical_errors': [],
            'reviewer_context': 'independent', 'independent_context': True, 'artifact_sha256': sha256_path(path)})
        self.config = replace(self.config, release_windows_plan=path, release_windows_review=review)
        spec = self.spec()
        self.assertEqual(spec['release_windows_evidence']['plan']['sha256'], sha256_path(path))
        self.config = replace(self.config, b_windows=((1.0, 2.1),))
        with self.assertRaisesRegex(ValueError, 'differ from reviewed source'):
            self.spec()
