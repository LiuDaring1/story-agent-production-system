from pathlib import Path
import csv

base = Path("/Users/baiyanglin/Desktop/故事剪辑：企鹅寄冰/02_图生视频/music")
base.mkdir(parents=True, exist_ok=True)
(base / "suno_downloads").mkdir(parents=True, exist_ok=True)

segments = [
    {
        "segment": 1,
        "label": "炎热求冰",
        "start_sec": "0.00",
        "end_sec": "36.50",
        "duration_sec": "36.50",
        "story_range": "从“企鹅寄冰 大家好 我是绵羊姐姐”到“请它寄块冰来”",
        "title": "01 Penguin Ice - Hot Africa Request",
        "style_prompt": "Warm whimsical children story underscore for a hot African summer and a funny lion king asking for ice. Soft piano anchor with light pizzicato strings, playful clarinet, tiny tuba-like low plucks for comic heat, and airy glockenspiel sparkles. Starts cozy and curious, gently bouncy without rushing. Low volume background, spacious mix for narration, never competing with the storytelling voice. 72 BPM, instrumental only, no vocals, no lyrics, no heavy percussion, no heavy elements.",
        "target_audio_filename": "01_story_music.mp3",
        "notes": "开场与狮子怕热、听说南极有冰合并；情绪同属轻喜剧铺垫。建议裁剪 36.50 秒，前后各 0.5-0.8 秒淡入淡出。",
    },
    {
        "segment": 2,
        "label": "南极寄冰与远行",
        "start_sec": "36.50",
        "end_sec": "72.80",
        "duration_sec": "36.30",
        "story_range": "从“好多天以后 企鹅收到了信”到“又上了飞机”",
        "title": "02 Penguin Ice - Snowy Parcel Journey",
        "style_prompt": "Bright storybook travel underscore for a penguin in snowy Antarctica preparing an ice parcel and sending it across the world. Soft piano remains the anchor, with gentle pizzicato strings, light flute, celesta glints like ice crystals, and quiet brushed sleigh-bell texture used sparingly. The music feels cool, fresh, and lightly adventurous, slowly moving forward. Low volume background, clean spacious mix for narration, never overpowering. 78 BPM, instrumental only, no vocals, no lyrics, no heavy percussion, no heavy elements.",
        "target_audio_filename": "02_story_music.mp3",
        "notes": "企鹅收信、装冰、轮船飞机远行放在同一段，靠同一旅行感承接。与第 1 段可 0.8 秒交叉淡化。",
    },
    {
        "segment": 3,
        "label": "一袋水与误会",
        "start_sec": "72.80",
        "end_sec": "109.20",
        "duration_sec": "36.40",
        "story_range": "从“过了很多天 狮子大王收到了箱子”到“企鹅也糊涂了”",
        "title": "03 Penguin Ice - Melted Mystery",
        "style_prompt": "Gentle comic mystery children story underscore for the lion finding a bag of water and the penguin becoming confused. Soft piano anchor with muted pizzicato strings, quirky clarinet questions, small bassoon or tuba plucks, and occasional celesta drops like melting ice. The mood shifts from curious surprise to mild frustration, then puzzled stillness, always restrained. Extremely low volume throughout, spacious mix for narration, no dramatic hit. 76 BPM, instrumental only, no vocals, no lyrics, no heavy percussion, no heavy elements.",
        "target_audio_filename": "03_story_music.mp3",
        "notes": "水/冰误会是主要戏剧段，但不能做得太紧张；用轻喜剧悬疑而非强冲突。结尾留一点悬念空间。",
    },
    {
        "segment": 4,
        "label": "提问与探索奥秘",
        "start_sec": "109.20",
        "end_sec": "135.07",
        "duration_sec": "25.87",
        "story_range": "从“小朋友们 你们知道这到底是怎么回事吗”到“不同的地方 就有不同的奥秘”",
        "title": "04 Penguin Ice - Wonder and Discovery",
        "style_prompt": "Tender wonder ending for a children science story, inviting kids to think about why ice becomes water and how different places hold different mysteries. Soft piano anchor returns warmly with quiet accordion pads, delicate glockenspiel, gentle strings, and a little airy flute. Calm, reassuring, curious, gradually fading into peaceful discovery. Extremely low volume background with clear space for narration, never competing with the voice. 66 BPM, instrumental only, no vocals, no lyrics, no heavy percussion, no heavy elements.",
        "target_audio_filename": "04_story_music.mp3",
        "notes": "结尾文字较短但情绪从误会转向科普启发，单独成段更自然。建议从第 3 段用 1.0 秒交叉淡入，末尾 2 秒渐弱。",
    },
]

md = """# 《企鹅寄冰》Suno 故事配乐提示词

- 旁白总时长：135.07 秒
- 音乐世界：现代儿童科普/动物故事，西式儿童故事配器
- 锚点乐器：soft piano，四段都保留，保证拼接后统一
- 统一要求：Advanced / Custom Mode；Model: V5.5；Lyrics: `[Instrumental]`；低音量、留旁白空间、无歌词无人声、无重鼓点

"""

settings = {
    1: (72, 18, 55),
    2: (78, 20, 55),
    3: (76, 22, 50),
    4: (66, 15, 60),
}

for s in segments:
    bpm, weirdness, influence = settings[s["segment"]]
    md += f"## 🎵 第{s['segment']}段：{s['label']}\n\n"
    md += f"**时间：** {s['start_sec']} - {s['end_sec']} 秒（约 {s['duration_sec']} 秒）\n\n"
    md += f"**对应文本：** {s['story_range']}\n\n"
    md += "**Style（粘贴到 Style 框）：**\n\n"
    md += s["style_prompt"] + "\n\n"
    md += "**Lyrics 框：**\n\n[Instrumental]\n\n"
    md += "**参考设置：**\n"
    md += f"- 模型：V5.5\n- BPM 建议：{bpm}\n- Weirdness：{weirdness}\n- Style Influence：{influence}\n"
    md += f"- Song Title：{s['title']}\n- 下载后命名：`{s['target_audio_filename']}`\n\n---\n\n"

md += """## 🎯 整体设计思路

这个故事分成 4 段：炎热求冰、南极寄冰与远行、水/冰误会、提问与探索奥秘。前两段都是轻松童趣，但一个偏炎热喜剧、一个偏冰雪旅行，所以保留分段；第三段是主要误会与困惑；第四段虽然较短，但情绪从剧情悬念转为温暖科普收束，单独配乐会更干净。全片用 soft piano 做锚点，配合轻拨弦、木管、钟琴和少量手风琴，形成统一的西式儿童故事音乐世界。整体情绪从调皮炎热，到清凉旅行，再到轻喜剧疑问，最后落在温柔好奇和探索感。

## 🛠 Suno 操作建议

- 每段生成 2 个候选，优先选择旋律不抢、鼓点最轻、结构变化最少的版本。
- 第 2 段最容易被 Suno 做成过度冒险或节奏太强；如果太满，Remix 加：`softer, more spacious, less rhythmic, quieter celesta`。
- 第 3 段最容易变成夸张悬疑；如果太戏剧化，Remix 加：`gentle comic mystery, no cinematic hits, very restrained`。
- 第 4 段要比其他段更低音量，结尾可裁出 2 秒淡出，避免旁白最后一句被音乐顶住。
- 拼接建议：第 1→2 段交叉淡化约 0.8 秒，第 2→3 段约 0.6 秒，第 3→4 段约 1.0 秒。最终混音建议音乐整体压到旁白下方约 -22 至 -18 LUFS 感知范围，峰值留足余量。
"""

(base / "story_suno_prompts.md").write_text(md, encoding="utf-8")

fieldnames = [
    "segment",
    "label",
    "start_sec",
    "end_sec",
    "duration_sec",
    "story_range",
    "title",
    "style_prompt",
    "target_audio_filename",
    "notes",
]
with (base / "story_music_plan.csv").open("w", newline="", encoding="utf-8-sig") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(segments)

print(base / "story_suno_prompts.md")
print(base / "story_music_plan.csv")
print(base / "suno_downloads")
