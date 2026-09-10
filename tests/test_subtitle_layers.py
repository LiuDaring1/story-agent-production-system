import subprocess
import tempfile
import unittest
from pathlib import Path
from PIL import Image
from story_subtitle_layers import crop_subtitle_layers, cropped_overlay_chain
from story_video_synthesizer.pipeline import SynthesisConfig, _render_subtitle_images, _subtitle_overlay_chain
from story_video_synthesizer.subtitles import SubtitleCue


class SubtitleLayerTests(unittest.TestCase):
    def test_alpha_padding_crop_preserves_pixels_and_empty_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); path=root/'cue.png'
            source=Image.new('RGBA',(64,64));source.putpixel((17,19),(255,255,255,128));source.putpixel((25,29),(255,255,255,255));source.save(path)
            cropped,x,y=crop_subtitle_layers([path],width=64,height=64)[0]
            rebuilt=Image.new('RGBA',(64,64));rebuilt.paste(Image.open(cropped),(x,y))
            self.assertEqual(rebuilt.tobytes(),source.tobytes())
            self.assertEqual(x%2,0);self.assertEqual(y%2,0)
            source=Image.new('RGBA',(64,64));source.save(path)
            self.assertEqual(crop_subtitle_layers([path],width=64,height=64),[(path,0,0)])

    def test_decoded_equivalence_including_overlap_gaps_and_boundaries(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);cfg=SynthesisConfig(root,root,root,root,root,width=320,height=180)
            cues=[SubtitleCue(1,'甲',.1,.5),SubtitleCue(2,'乙',.5,1),SubtitleCue(3,'丙',1.2,1.7)]
            images=_render_subtitle_images(cues,root/'png',cfg)
            layers=crop_subtitle_layers(images,width=320,height=180)
            outputs=[]
            for name,paths,graph in [('full',images,_subtitle_overlay_chain(cues,1)),('crop',[x[0] for x in layers],cropped_overlay_chain(cues,layers))]:
                cmd=['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=320x180:rate=30']
                for path in paths:cmd+=['-loop','1','-i',str(path)]
                cmd+=['-filter_complex_threads','1','-filter_complex',graph,'-map','[v]','-t','2','-pix_fmt','yuv420p','-f','framemd5','-']
                outputs.append(subprocess.check_output(cmd))
            self.assertEqual(*outputs)
