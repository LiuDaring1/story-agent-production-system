"""The v2 closeout consumes existing evidence without inventing legacy flags."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from release_geometry import release_package_receipt_issues


class ReleasePackageV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.roles = {'main_top_panel':'top_plate', 'main_bottom_panel':'bottom_plate',
                      'library_top_panel':'library_top_plate', 'library_bottom_panel':'library_bottom_plate'}
        self.panels = {}
        for i, role in enumerate(self.roles):
            path = self.root / (role + '.png')
            Image.new('RGB', (2304,888), (30+i*40,40,50)).save(path)
            self.panels[role] = self.bind(path)
        self.spec = self.write('spec.json', {'schema_version':'story-confirmed-packaging/v2',
                                           'fields':{'story_name':'fixture','duration_text':'1分00秒'}})
        self.generation = self.write('generation.json', {'schema_version':'story-confirmed-panels/v2',
            'outputs':{name:self.panels[role] for role,name in self.roles.items()}})
        self.package = {f'{name}_{field}': value for name,path in (
            ('main_package_spec',self.spec),('main_package_receipt',self.generation))
            for field,value in self.bind(path).items()}
        self.package['panel_sha256'] = {r:p['sha256'] for r,p in self.panels.items()}
        bindings = {'bindings_schema_version':'story-release-bindings/v2',
                    **{k:'a'*64 for k in ('artifact_semantic_plan_sha256','demo_render_manifest_sha256',
                                         'keying_preset_sha256','keying_lock_sha256')}}
        self.payload = {'production_contract':'story-production/v2','variant':'both',
            'actual_geometry':{'bindings':bindings,'main_package_spec':self.package,
              'main':{k:{'renderer':'imagegen_native_reference'} for k in ('upper_strip','lower_strip')},
              'library':{'video_region':[0,416,1080,608]}},
            'actual_output_geometry':{'center_video_region':[0,416,1080,608]},'outputs':[]}
        self.proofs = []
        for account, name in [('main','主账号发布视频.mp4'),('library','宝库号发布视频.mp4')]:
            target=self.root/name;target.write_bytes(account.encode());out=self.bind(target);self.payload['outputs'].append(out)
            artifact=f'release-{account}-vertical:fixture';operation=f'{account}_vertical_render'
            key=hashlib.sha256(f'{artifact}\0{operation}\0{target}'.encode()).hexdigest()
            self.proofs.append(self.write(f'.release_layout_receipts/{key}.json', {
                'schema_version':'story-release-layout-operation/v1','artifact_id':artifact,'operation':operation,
                'production_eligible':True,'output_path':str(target),'output_sha256':out['sha256'],
                'request_fingerprint':'b'*64,'layout_binding_sha256':'c'*64,
                'input_artifact_hashes':{'top_panel':self.panels[account+'_top_panel']['sha256'],
                 'bottom_panel':self.panels[account+'_bottom_panel']['sha256'],
                 **{k:'a'*64 for k in ('artifact_semantic_plan','demo_render_manifest','keying_preset','keying_preset_lock')}}}))
        # Geometry/output integrity has separate existing regression coverage.
        p=patch('release_geometry.release_render_manifest_issues',return_value=[]);p.start();self.addCleanup(p.stop)
        p=patch('story_materials.validate_panel_binding',return_value=self.package);self.native=p.start();self.addCleanup(p.stop)
    def bind(self,p):return {'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
    def write(self,n,j):
        p=self.root/n;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(j));return p
    def issues(self):return release_package_receipt_issues(self.payload)
    def test_v2_reads_real_panel_paths_and_operation_receipts_without_mutation(self):
        before=copy.deepcopy(self.payload);self.assertEqual(self.issues(),[]);self.assertEqual(self.payload,before)
        self.native.assert_called_once();self.assertNotIn('render_usage_proof',self.package)
    def test_native_generation_validator_failure_is_not_bypassed(self):
        self.native.side_effect=ValueError('Panel reference/native generation evidence missing')
        self.assertTrue(any('native generation' in x for x in self.issues()))
    def test_stale_panel_and_missing_operation_rejected(self):
        Path(self.panels['main_top_panel']['path']).write_bytes(b'changed')
        self.assertTrue(self.issues())
    def test_wrong_operation_panel_or_output_hash_rejected(self):
        for key in ['output_sha256','input_artifact_hashes']:
            with self.subTest(key=key):
                f=self.proofs[0];original=f.read_text();j=json.loads(original)
                if key=='output_sha256':j[key]='x'*64
                else:j[key]['top_panel']='x'*64
                f.write_text(json.dumps(j));self.assertTrue(any('rendered panel usage' in x for x in self.issues()));f.write_text(original)
    def test_missing_operation_receipt_rejected(self):
        self.proofs[0].unlink();self.assertTrue(any('v2_evidence_invalid' in x for x in self.issues()))
    def test_v1_still_requires_legacy_flags_and_four_panels(self):
        self.payload.pop('production_contract');issues=self.issues()
        self.assertIn('release_package_text_integration_not_imagegen_native',issues)
        self.assertIn('release_package_render_usage_proof_missing',issues)
        self.assertIn('release_package_four_panel_set_incomplete',issues)
        self.native.assert_not_called()

if __name__=='__main__':unittest.main()
