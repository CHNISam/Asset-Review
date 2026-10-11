"""Synthetic, code-drawn production material for a concert hall asset.

Nothing here is real project imagery: every picture is drawn with Pillow so
the demo and the end-to-end check can run anywhere without private files.
The set follows the asset folder convention:

    music-hall/design/<view>/<view>_vN.png   design revisions
    music-hall/model|uv|texture|engine/...    stage evidence
"""

from __future__ import annotations

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1600, 1000
SS = 2  # supersampling for clean edges

PALETTES = {
    1: {"sky": ((250, 228, 206), (255, 246, 236)), "wall": (226, 214, 196), "roof": (120, 96, 84), "trim": (176, 150, 122)},
    2: {"sky": ((214, 226, 246), (250, 244, 236)), "wall": (236, 226, 210), "roof": (74, 112, 120), "trim": (188, 160, 120)},
    3: {"sky": ((206, 220, 244), (255, 240, 222)), "wall": (240, 232, 218), "roof": (78, 132, 126), "trim": (196, 156, 92)},
}


def _canvas(top: tuple[int, int, int], bottom: tuple[int, int, int]) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (W * SS, H * SS), top)
    draw = ImageDraw.Draw(image)
    for y in range(0, H * SS, 2):
        t = y / (H * SS)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.rectangle((0, y, W * SS, y + 2), fill=color)
    return image, draw


def _font(size: int):
    return ImageFont.load_default(size=size * SS)


def _s(*values: float) -> list[int]:
    return [int(v * SS) for v in values]


def _finish(image: Image.Image, path: Path, size: tuple[int, int] = (W, H)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.resize(size, Image.Resampling.LANCZOS).save(path, optimize=True)


def _ground(draw: ImageDraw.ImageDraw, color=(214, 206, 190), line=(196, 186, 168)) -> None:
    draw.rectangle(_s(0, 780, W, H), fill=color)
    draw.line(_s(0, 780, W, 780), fill=line, width=3 * SS)
    for x in range(-200, W + 200, 90):
        draw.line(_s(x, 1000, W / 2 + (x - W / 2) * 0.35, 790), fill=line, width=SS)


def _tree(draw: ImageDraw.ImageDraw, x: float, base: float, scale: float, color=(126, 150, 112)) -> None:
    draw.rectangle(_s(x - 5 * scale, base - 70 * scale, x + 5 * scale, base), fill=(118, 98, 80))
    for dx, dy, r in ((0, -120, 52), (-34, -92, 40), (34, -92, 40), (0, -80, 44)):
        draw.ellipse(_s(x + dx * scale - r * scale, base + dy * scale - r * scale, x + dx * scale + r * scale, base + dy * scale + r * scale), fill=color)


def _facade(draw: ImageDraw.ImageDraw, version: int, *, wall, roof, trim, glow=(255, 214, 150), shade=None) -> None:
    """Front elevation of the hall. v1 flat parapet, v2 gable, v3 barrel vault with ribs."""
    left, right, base, top = 330, 1270, 780, 430
    cx = (left + right) / 2
    shade = shade or tuple(max(0, c - 26) for c in wall)
    # wings
    draw.rectangle(_s(left - 170, 560, left, base), fill=shade)
    draw.rectangle(_s(right, 560, right + 170, base), fill=shade)
    for wx in (left - 130, left - 70, right + 40, right + 100):
        draw.rounded_rectangle(_s(wx, 610, wx + 34, 720), radius=14 * SS, fill=glow)
    # main volume
    draw.rectangle(_s(left, top, right, base), fill=wall)
    if version == 1:
        draw.rectangle(_s(left - 14, top - 34, right + 14, top), fill=roof)
    elif version == 2:
        draw.polygon(_s(left - 30, top, cx, top - 210, right + 30, top), fill=roof)
        draw.polygon(_s(left + 40, top - 8, cx, top - 168, right - 40, top - 8), fill=tuple(min(255, c + 30) for c in roof))
    else:
        draw.chord(_s(left - 40, top - 260, right + 40, top + 240), 180, 360, fill=roof)
        for i in range(1, 8):
            angle = math.pi + math.pi * i / 8
            x = cx + (right + 40 - cx) * math.cos(angle)
            y = top - 10 + 250 * math.sin(angle)
            draw.line(_s(cx, top - 10, x, y), fill=tuple(min(255, c + 40) for c in roof), width=4 * SS)
        draw.chord(_s(cx - 120, top - 120, cx + 120, top + 120), 180, 360, fill=glow)
    # rose window
    rose_y = top + 120
    draw.ellipse(_s(cx - 92, rose_y - 92, cx + 92, rose_y + 92), fill=trim)
    draw.ellipse(_s(cx - 78, rose_y - 78, cx + 78, rose_y + 78), fill=glow)
    for i in range(12):
        a = 2 * math.pi * i / 12
        draw.line(_s(cx, rose_y, cx + 78 * math.cos(a), rose_y + 78 * math.sin(a)), fill=trim, width=4 * SS)
    draw.ellipse(_s(cx - 20, rose_y - 20, cx + 20, rose_y + 20), fill=trim)
    # tall windows
    for i in range(4):
        for side in (-1, 1):
            x = cx + side * (170 + i * 92)
            draw.rounded_rectangle(_s(x - 26, 520, x + 26, 700), radius=26 * SS, fill=glow)
            draw.line(_s(x, 524, x, 698), fill=trim, width=3 * SS)
    # colonnade + entrance
    draw.rectangle(_s(cx - 230, 690, cx + 230, 706), fill=trim)
    for i in range(7):
        x = cx - 210 + i * 70
        draw.rectangle(_s(x - 9, 706, x + 9, base - 22), fill=tuple(min(255, c + 14) for c in wall))
    draw.rounded_rectangle(_s(cx - 54, 716, cx + 54, base - 22), radius=40 * SS, fill=glow)
    for step in range(3):
        draw.rectangle(_s(cx - 260 - step * 24, base - 22 + step * 8, cx + 260 + step * 24, base - 14 + step * 8), fill=trim)
    # cornice
    draw.rectangle(_s(left, top, right, top + 14), fill=trim)
    if version >= 2:
        for i in range(24):
            x = left + 20 + i * ((right - left - 40) / 23)
            draw.rectangle(_s(x - 4, top + 14, x + 4, top + 26), fill=trim)


def design_front(path: Path, version: int) -> None:
    p = PALETTES[version]
    image, draw = _canvas(*p["sky"])
    draw.ellipse(_s(1180, 120, 1300, 240), fill=(255, 250, 238))
    _ground(draw)
    for x, s in ((150, 1.1), (250, 0.8), (1380, 1.0), (1470, 0.85)):
        _tree(draw, x, 790, s)
    _facade(draw, version, wall=p["wall"], roof=p["roof"], trim=p["trim"])
    _finish(image, path)


def design_side(path: Path, version: int = 1) -> None:
    p = PALETTES[3]
    image, draw = _canvas(*p["sky"])
    _ground(draw)
    left, right, base, top = 180, 1420, 780, 470
    draw.rectangle(_s(left, top, right, base), fill=p["wall"])
    draw.chord(_s(left + 60, top - 200, right - 60, top + 200), 180, 360, fill=p["roof"])
    draw.pieslice(_s(right - 140, top - 60, right + 120, base + 200), 270, 360, fill=tuple(c - 18 for c in p["wall"]))
    for i in range(9):
        x = left + 90 + i * 130
        draw.rounded_rectangle(_s(x, 540, x + 60, 720), radius=30 * SS, fill=(255, 214, 150))
        draw.rectangle(_s(x - 22, 500, x - 8, base), fill=p["trim"])
    draw.rectangle(_s(left, top, right, top + 14), fill=p["trim"])
    for x, s in ((90, 1.0), (1500, 1.1)):
        _tree(draw, x, 790, s)
    _finish(image, path)


def design_top(path: Path, version: int = 1, *, small: bool = False) -> None:
    image, draw = _canvas((236, 234, 226), (230, 228, 220))
    for x in range(0, W, 40):
        draw.line(_s(x, 0, x, H), fill=(226, 224, 216), width=SS)
    for y in range(0, H, 40):
        draw.line(_s(0, y, W, y), fill=(226, 224, 216), width=SS)
    draw.rounded_rectangle(_s(420, 230, 1180, 690), radius=60 * SS, fill=(212, 200, 182), outline=(150, 130, 108), width=4 * SS)
    draw.pieslice(_s(1040, 300, 1320, 620), 270, 450, fill=(212, 200, 182), outline=(150, 130, 108), width=4 * SS)
    draw.rounded_rectangle(_s(520, 300, 1080, 620), radius=40 * SS, fill=(236, 226, 206))
    for row in range(6):
        y = 340 + row * 44
        draw.arc(_s(560, y - 120, 1060, y + 200), 200, 340, fill=(170, 150, 126), width=5 * SS)
    draw.rectangle(_s(300, 440, 420, 480), fill=(204, 196, 180))
    draw.polygon(_s(120, 360, 300, 380, 300, 540, 120, 560), fill=(222, 214, 198))
    for x, y in ((180, 230), (240, 760), (1320, 200), (1400, 780), (760, 820), (900, 150)):
        draw.ellipse(_s(x - 46, y - 46, x + 46, y + 46), fill=(150, 174, 134))
    _finish(image, path, (200, 125) if small else (W, H))


def design_detail(path: Path, version: int = 1) -> None:
    p = PALETTES[3]
    image, draw = _canvas((246, 238, 226), (238, 228, 212))
    cx, cy = 800, 470
    draw.ellipse(_s(cx - 330, cy - 330, cx + 330, cy + 330), fill=p["trim"])
    draw.ellipse(_s(cx - 290, cy - 290, cx + 290, cy + 290), fill=(255, 220, 160))
    for i in range(24):
        a = 2 * math.pi * i / 24
        draw.line(_s(cx, cy, cx + 290 * math.cos(a), cy + 290 * math.sin(a)), fill=p["trim"], width=6 * SS)
    for ring in (90, 180):
        draw.ellipse(_s(cx - ring, cy - ring, cx + ring, cy + ring), outline=p["trim"], width=8 * SS)
    for i in range(18):
        x = 140 + i * 18
        draw.rounded_rectangle(_s(x, 900 - (i % 6) * 40 - 200, x + 12, 900), radius=6 * SS, fill=(196, 160, 96))
        draw.rounded_rectangle(_s(W - x - 12, 900 - (i % 6) * 40 - 200, W - x, 900), radius=6 * SS, fill=(196, 160, 96))
    _finish(image, path)


def stage_model(path: Path, *, wire: bool = False) -> None:
    image, draw = _canvas((58, 60, 66), (34, 35, 40))
    for x in range(-W, W * 2, 80):
        draw.line(_s(x, 1000, W / 2 + (x - W / 2) * 0.3, 640), fill=(70, 72, 80), width=SS)
    for y in (700, 760, 840, 940):
        draw.line(_s(0, y, W, y), fill=(70, 72, 80), width=SS)
    grey = dict(wall=(178, 178, 182), roof=(132, 134, 140), trim=(156, 156, 162), glow=(200, 200, 205), shade=(150, 150, 155))
    shadow = Image.new("L", (W * SS, H * SS), 0)
    ImageDraw.Draw(shadow).ellipse(_s(200, 740, 1400, 840), fill=150)
    image.paste((24, 24, 28), mask=shadow.filter(ImageFilter.GaussianBlur(40 * SS)))
    _facade(draw, 3, **grey)
    if wire:
        for x in range(330, 1271, 47):
            draw.line(_s(x, 430, x, 780), fill=(96, 170, 255), width=SS)
        for y in range(430, 781, 35):
            draw.line(_s(330, y, 1270, y), fill=(96, 170, 255), width=SS)
    draw.text(tuple(_s(40, 36)), "Blender · clay" if not wire else "Blender · wireframe · 18.4k tris", fill=(200, 200, 205), font=_font(26))
    _finish(image, path)


def stage_uv(path: Path) -> None:
    image = Image.new("RGB", (W * SS, H * SS), (40, 42, 48))
    draw = ImageDraw.Draw(image)
    size = 860 * SS
    ox, oy = (W * SS - size) // 2, (H * SS - size) // 2
    cell = size // 16
    for i in range(16):
        for j in range(16):
            color = (232, 232, 236) if (i + j) % 2 == 0 else (196, 98, 70)
            draw.rectangle((ox + i * cell, oy + j * cell, ox + (i + 1) * cell, oy + (j + 1) * cell), fill=color)
    rng = random.Random(7)  # nosec B311 - decorative demo pattern
    for _ in range(14):
        x = ox + rng.randint(20, size - 260)
        y = oy + rng.randint(20, size - 200)
        w, h = rng.randint(120, 260), rng.randint(80, 200)
        draw.rounded_rectangle((x, y, x + w, y + h), radius=10 * SS, outline=(255, 214, 92), width=3 * SS)
    _finish(image, path)


def stage_texture(path: Path) -> None:
    p = PALETTES[3]
    image, draw = _canvas((196, 214, 238), (250, 232, 210))
    _ground(draw, color=(200, 190, 172), line=(184, 172, 152))
    _facade(draw, 3, wall=(232, 220, 198), roof=(86, 146, 132), trim=(198, 150, 76), glow=(255, 206, 128))
    rng = random.Random(3)  # nosec B311 - decorative demo pattern
    for _ in range(2600):
        x, y = rng.randint(330, 1270), rng.randint(440, 776)
        draw.point(_s(x, y), fill=(214, 200, 176))
    _finish(image, path)


def stage_engine(path: Path, shot: int = 1) -> None:
    image, draw = _canvas((126, 160, 214), (246, 206, 168)) if shot == 1 else _canvas((40, 52, 92), (208, 128, 96))
    draw.rectangle(_s(0, 0, W, 60), fill=(0, 0, 0))
    draw.rectangle(_s(0, H - 60, W, H), fill=(0, 0, 0))
    _ground(draw, color=(150, 142, 128) if shot == 1 else (78, 70, 70), line=(134, 126, 112) if shot == 1 else (64, 58, 58))
    glow = (255, 198, 110) if shot == 2 else (255, 214, 150)
    _facade(draw, 3, wall=(226, 214, 192) if shot == 1 else (150, 136, 128), roof=(80, 136, 124) if shot == 1 else (52, 86, 84), trim=(190, 146, 76), glow=glow)
    for x, s in ((140, 1.2), (1460, 1.1)):
        _tree(draw, x, 790, s, color=(98, 128, 92) if shot == 1 else (52, 70, 60))
    draw.ellipse(_s(1430, 80, 1550, 200), outline=(255, 255, 255), width=3 * SS)
    draw.ellipse(_s(1484, 134, 1496, 146), fill=(255, 120, 80))
    draw.text(tuple(_s(40, 80)), "Godot 4.4 · 60 fps · Forward+", fill=(255, 255, 255), font=_font(24))
    draw.rounded_rectangle(_s(620, 860, 980, 916), radius=14 * SS, fill=(20, 20, 24))
    draw.text(tuple(_s(648, 872)), "Enter the Music Hall  [E]", fill=(255, 236, 200), font=_font(26))
    _finish(image, path)


def create_design_set(root: Path) -> list[Path]:
    """Design revisions as an artist would deliver them, including one obvious draft."""
    hall = root / "music-hall" / "design"
    files = []
    for version in (1, 2, 3):
        target = hall / "front" / f"front_v{version}.png"
        design_front(target, version)
        files.append(target)
    for target, painter in (
        (hall / "side" / "side_v1.png", design_side),
        (hall / "top" / "top_v1.png", design_top),
        (hall / "detail" / "detail_v1.png", design_detail),
    ):
        painter(target)
        files.append(target)
    draft = hall / "top" / "top_v2_tmp.png"
    design_top(draft, small=True)
    files.append(draft)
    return files


def create_stage_set(root: Path) -> dict[str, list[Path]]:
    hall = root / "music-hall"
    outputs = {
        "model": [hall / "model" / "clay_front.png", hall / "model" / "wireframe.png"],
        "uv": [hall / "uv" / "uv_layout.png"],
        "texture": [hall / "texture" / "material_pass.png"],
        "engine": [hall / "engine" / "godot_day.png", hall / "engine" / "godot_dusk.png"],
    }
    stage_model(outputs["model"][0])
    stage_model(outputs["model"][1], wire=True)
    stage_uv(outputs["uv"][0])
    stage_texture(outputs["texture"][0])
    stage_engine(outputs["engine"][0], 1)
    stage_engine(outputs["engine"][1], 2)
    return outputs


DEMO_PROJECT = "示例项目"
DEMO_SCENE = "音乐厅广场"
DEMO_ASSET = "music-hall"


def seed_demo(root: Path) -> dict:
    """Register the design set and declare the concert hall asset (idempotent)."""
    from . import production
    from .app import scan_collection
    from .storage import add_collection

    create_design_set(root)
    row = add_collection(str(root), "示例项目美术", f"{DEMO_PROJECT} / {DEMO_SCENE} / 设计")
    scan_collection(row["slug"], force=True)
    if DEMO_ASSET not in production.asset_keys():
        production.create_asset(
            DEMO_PROJECT, "音乐厅", key=DEMO_ASSET, scene=DEMO_SCENE, kind="environment",
            brief="小镇中心的音乐厅。拱顶 + 玫瑰窗是识别特征；立面要在黄昏时有温暖的灯光层次。",
            views=[{"key": "front", "label": "正面"}, {"key": "side", "label": "侧面"}, {"key": "top", "label": "俯视"},
                   {"key": "detail", "label": "局部细节", "required": False}],
            source_collection=row["slug"], source_prefix="music-hall", actor="agent",
        )
    production.discover(DEMO_ASSET, actor="agent")
    return {"collection": row["slug"], "path": row["path"], "asset": DEMO_ASSET}
