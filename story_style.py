"""Pure current visual style preferences; explicit user choices take precedence."""
from __future__ import annotations

from pathlib import Path


STYLE_PRESETS: dict[str, dict[str, str]] = {
    "3d_cartoon": {
        "label": "3D卡通",
        "goal": "16:9、明亮温暖、儿童友好的 3D 卡通分镜图片",
        "prompt": (
            "高质量 3D 卡通儿童故事视觉，圆润可爱的角色比例，柔和体积光，"
            "干净明亮的色彩，材质细腻但不过度真实，适合儿童观看。"
        ),
    },
    "chinese_2d_storybook": {
        "label": "中国风2D绘本",
        "goal": "16:9、明亮温暖、儿童友好的中国风 2D 绘本分镜图片",
        "prompt": (
            "中国风 2D 绘本卡通，带一点国画水彩和工笔设色的感觉；线条温柔清晰，"
            "人物表情可爱克制，色彩以温暖米白、青绿、朱红、黛蓝和木色点缀；"
            "乡村屋舍、灶台、田埂、竹篱、水缸、卷轴等元素要有中国民间故事气质。"
            "避免厚重写实、阴暗古装剧质感、过度 3D 塑料感和网游仙侠风。"
        ),
    },
    "ink_wash_picturebook": {
        "label": "水墨绘本",
        "goal": "16:9、明亮温暖、儿童友好的水墨绘本分镜图片",
        "prompt": (
            "轻水墨儿童绘本风，宣纸肌理、淡墨轮廓、浅彩点染，留白清爽，"
            "角色仍然要亲切可爱、动作读得清楚；适合神话、民间故事、古代寓言。"
            "避免灰暗、抽象、成人艺术海报感和难以看清表情的过度写意。"
        ),
    },
}

DEFAULT_STYLE_KEY = "3d_cartoon"


def image_style_options() -> list[str]:
    return ["自动", *[preset["label"] for preset in STYLE_PRESETS.values()]]


def resolve_image_style(style: str | None = None, *, story_type: str = "", story_title: str = "", story: str = "") -> dict[str, str]:
    requested = (style or "").strip()
    if not requested or requested == "自动":
        key = infer_image_style_key(story_type=story_type, story_title=story_title, story=story)
    else:
        key = _style_key_from_label_or_key(requested)
    preset = STYLE_PRESETS.get(key, STYLE_PRESETS[DEFAULT_STYLE_KEY]).copy()
    preset["key"] = key
    return preset


def infer_image_style_key(*, story_type: str = "", story_title: str = "", story: str = "") -> str:
    """Return the stable default when the user has not selected a style.

    Story genre and story text are deliberately ignored.  A myth, idiom or
    historical story may still be produced as 3D cartoon, so keywords cannot
    safely express visual intent.  Explicit selections are handled by
    ``resolve_image_style`` before this function is called.
    """
    del story_type, story_title, story
    return DEFAULT_STYLE_KEY


def _style_key_from_label_or_key(value: str) -> str:
    if value in STYLE_PRESETS:
        return value
    for key, preset in STYLE_PRESETS.items():
        if value == preset["label"]:
            return key
    return DEFAULT_STYLE_KEY
