import json
import tempfile
import unittest
from pathlib import Path
from PIL import Image, ImageDraw
from story_visual_contracts import compile_visual_scopes
from story_packaging_defaults import defaults
from story_production_v2 import binding
from story_frame_alpha import validate_native_frame_alpha
from artifact_semantic_plan import moral_card_display_text

class VisualScopeTests(unittest.TestCase):
    def test_product_does_not_follow_agent_delivery_or_main_style(self):
        config = defaults()
        fields = dict(story_name='测试故事', story_type='寓言', age_range='6岁', duration_text='2分钟', theme_style='主账号专用简洁平面')
        scopes = compile_visual_scopes({k:config[k] for k in ('packaging_reference','packaging_prompt')}, fields)
        self.assertIn('主账号专用简洁平面', scopes['main']['prompt'])
        for role in ('library','frame'):
            self.assertNotIn('主账号专用简洁平面', scopes[role]['prompt'])
        self.assertIn('朗读标注', scopes['library']['product_contents']['items'])
        self.assertIn('PPT', scopes['library']['product_contents']['items'])
        self.assertNotIn('simple_layout', scopes['library']['review_checks'])
        self.assertEqual(scopes['frame']['reference']['sha256'], json.loads(Path('assets/references/frame_reference_provenance.json').read_text())['sha256'])

    def test_confirmed_moral_keeps_complete_sentence(self):
        text='小朋友们，只有愿意帮助别人，才能在困难时收获真正的友谊！'
        self.assertEqual(moral_card_display_text(text), text)

    def test_alpha_samples_and_blending_preserved(self):
        image=Image.new('RGBA',(100,80),(255,0,255,0))
        draw=ImageDraw.Draw(image)
        draw.rectangle((10,10,90,70),fill=(100,80,60,255))
        draw.rectangle((20,20,80,60),fill=(0,0,0,0))
        image.putpixel((19,40),(100,80,60,128))
        before=image.tobytes()
        report=validate_native_frame_alpha(image)
        self.assertEqual(report['semitransparent_pixels'],1)
        self.assertEqual(image.tobytes(),before)
        composite=Image.alpha_composite(Image.new('RGBA',image.size,(20,40,60,255)),image)
        self.assertEqual(composite.getpixel((50,40)),(20,40,60,255))
        self.assertEqual(composite.getpixel((19,40)),(60,60,60,255))
        with self.assertRaises(ValueError):validate_native_frame_alpha(image.convert('RGB'))
        opaque=image.copy();opaque.putalpha(255)
        with self.assertRaises(ValueError):validate_native_frame_alpha(opaque)
        with self.assertRaises(ValueError):validate_native_frame_alpha(Image.new('RGBA',(20,20)))

    def test_export_consumes_native_alpha_without_chroma_or_erosion(self):
        from story_project import export_frame_from_source
        from unittest.mock import patch
        image=Image.new('RGBA',(1920,1080),(255,0,255,0))
        draw=ImageDraw.Draw(image)
        draw.rectangle((200,200,1600,900),fill=(100,80,60,255))
        draw.rectangle((240,240,1560,860),fill=(0,0,0,0))
        image.putpixel((239,500),(100,80,60,128))
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'source.png';target=Path(temp)/'frame.png';image.save(source)
            with patch('story_project.fit_frame_to_window',side_effect=lambda frame,window:frame), patch('story_project.chroma_to_alpha',side_effect=AssertionError('native alpha must not key')):
                export_frame_from_source(source,target,(220,220,1360,660))
            with Image.open(target) as actual:
                self.assertEqual(actual.tobytes(),image.tobytes())

    def test_registered_native_alpha_lineage_rejects_opaque_png(self):
        from test_story_run import StoryRunLedgerTests
        from story_run import validate_theme_assets_manifest
        with tempfile.TemporaryDirectory() as temp:
            manifest = StoryRunLedgerTests().write_v3_theme_manifest(Path(temp))
            payload=json.loads(manifest.read_text())
            frame=payload['artifacts']['story_frame_png']
            source=payload['sources'].pop('story_frame_magenta')
            source.update(path=frame['path'],sha256=frame['sha256'])
            payload['sources']['story_frame_alpha']=source
            frame.update(method='native_alpha_passthrough',derived_from=['story_frame_alpha'])
            payload['frame_design_review']['true_alpha_verified']=True
            manifest.write_text(json.dumps(payload))
            validate_theme_assets_manifest(manifest,require_v3=True)
            path=Path(frame['path'])
            with Image.open(path) as original:opaque=original.convert('RGB')
            opaque.save(path)
            source['sha256']=frame['sha256']=binding(path)['sha256']
            manifest.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError,'alpha channel'):
                validate_theme_assets_manifest(manifest,require_v3=True)

    def test_compiled_scope_tampering_and_library_review_omission_rejected(self):
        from story_materials import bind_packaging, validate_packaging, validate_panel_binding
        from types import SimpleNamespace
        config=defaults()
        inputs={k:config[k] for k in ('packaging_reference','packaging_prompt')}
        fields=dict(story_name='测试故事',story_type='寓言',age_range='6岁',duration_text='2分钟',theme_style='简洁')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);spec=root/'spec.json'
            payload=bind_packaging(inputs=inputs,output=root/'prompt.txt',receipt=spec,fields=fields)
            validate_packaging(spec)
            self.assertEqual(payload['output_scope'], 'main')
            self.assertEqual(set(payload['scope_prompt_outputs']), {'main', 'library', 'frame'})
            for scope, item in payload['scope_prompt_outputs'].items():
                self.assertEqual(Path(item['path']).read_text(), payload['visual_scopes'][scope]['prompt'])
            self.assertNotIn('已确认商品包含', Path(payload['scope_prompt_outputs']['main']['path']).read_text())
            self.assertIn('朗读标注', Path(payload['scope_prompt_outputs']['library']['path']).read_text())
            generation={'schema_version':'story-confirmed-panels/v2','prompt_receipt_sha256':binding(spec)['sha256'],
                        'reference_attached':True,'imagegen_native':True,'request_id':'offline-fixture',
                        'checks':{x:True for x in payload['visual_scopes']['main']['review_checks']},
                        'scope_checks':{'main':{x:True for x in payload['visual_scopes']['main']['review_checks']},'library':{'simple_layout':True}}}
            generated=root/'generation.json';generated.write_text(json.dumps(generation))
            with self.assertRaisesRegex(ValueError,'library.text_matches_confirmed_prompt'):
                validate_panel_binding(SimpleNamespace(),spec,generated)
            payload['visual_scopes']['library']['prompt']='错误继承主账号'
            spec.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError,'scope rules'):
                validate_packaging(spec)

    def test_theme_writer_consumes_frame_scope_and_allows_real_alpha(self):
        from story_project import build_theme_asset_imagegen_request, build_theme_asset_handoff
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            names=('main_plate','main_plate_top','main_plate_bottom','library_plate','library_plate_top','library_plate_bottom','main_bg','frame_source','frame_a','main_package_spec','main_package_receipt')
            outputs={name:root/name for name in names}
            outputs['main_package_spec'].write_text(json.dumps(dict(reference_asset='main.png',reference_sha256='a'*64,fixed_prompt_version='test',fixed_prompt='main only',frame_reference_asset='frame.png',frame_reference_sha256='b'*64,frame_story_box=[100,200,900,500])))
            result=build_theme_asset_imagegen_request(manifest={'story':{'name':'新故事','story_type':'寓言','age_range':'6岁'}},output_paths=outputs,theme='水彩',duration_text='2分钟',config={})
            handoff = build_theme_asset_handoff(root/'request.md')
            self.assertIn('优先真实Alpha', handoff)
            self.assertNotIn('禁止棋盘格、白底或直接透明输出', handoff)
            self.assertIn('仅设计A镜框体',result)
            self.assertIn('优先真实Alpha',result)
            self.assertNotIn('禁止棋盘格、白底、渐变底、透明预览或直接透明输出',result)
