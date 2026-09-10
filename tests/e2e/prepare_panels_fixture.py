"""Prepare simulated native panel inputs and request actual independent fixture review."""
from pathlib import Path
import json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from PIL import Image, ImageDraw, ImageFont
from story_production_v2 import binding,write
from story_packaging_defaults import defaults
from story_materials import bind_packaging
from story_evidence import write_review_bundle

def main():
    root=Path(sys.argv[1]).resolve();project=root/'fixture';out=project/'panels_v2';out.mkdir(exist_ok=True)
    runpath=project/'99_项目状态/story_run.json';run=json.loads(runpath.read_text());config=defaults()
    for role in ('packaging_prompt','packaging_reference'):run['inputs'][role]=config[role]
    write(runpath,run)
    spec=out/'prompt_receipt.json'
    result=bind_packaging(inputs={k:run['inputs'][k] for k in ('packaging_prompt','packaging_reference')},output=out/'prompt.txt',receipt=spec,fields={'story_name':'FIXTURE','story_type':'测试故事','age_range':'6岁','duration_text':'3秒','theme_style':'离线合成夹具'})
    font=ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc',100)
    panels={}
    for name,lines,color in [('top_plate',['测试故事','FIXTURE'],(222,231,237)),('bottom_plate',['完整版时长：3秒','适合年龄：6岁','适用于朗诵比赛、故事表演、少儿口才、技能比拼'],(222,231,237)),('library_top_plate',['FIXTURE','故事宝库 · 测试故事 · 3秒 · 6岁'],(237,215,173)),('library_bottom_plate',['背景视频 · PPT · 配乐','文稿 · 示范视频 · 朗读标注'],(237,215,173))]:
        image=Image.new('RGB',(2304,888),color);draw=ImageDraw.Draw(image)
        for i,line in enumerate(lines):
            size=58 if len(line)>24 else (140 if line=='FIXTURE' else 100)
            draw.text((1152,220+i*210),line,font=ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc',size),anchor='mm',fill=(44,53,62))
        if name.startswith('library_'):
            draw.rectangle((60,60,2244,828),outline=(153,108,48),width=12)
            draw.line((200,620,2104,620),fill=(153,108,48),width=6)
        path=out/(name+'.png');image.save(path);panels[name]=binding(path)
    bundle=write_review_bundle(out/'review_bundle.json',[spec,*[Path(i['path']) for i in panels.values()]])
    write(out/'independent_review_request.json',{'artifact':binding(bundle),'review_output':str(out/'independent_review.json'),'producer_context':'offline-fixture-producer','scope':'Synthetic native-provider upstream test fixture only. Evaluate text, 2304x888 panel geometry and main/library distinction; cannot validate actual ImageGen native generation or user-approved real-story visual design.',
        'requested_checks':['text_matches_confirmed_prompt','no_reference_story_leak','top_bottom_coherent','simple_layout'], 'scope_checks':{k:v['review_checks'] for k,v in result['visual_scopes'].items() if k in ('main','library')},'outputs':panels})
    print(out/'independent_review_request.json')
if __name__=='__main__':main()
