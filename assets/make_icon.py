"""Draws assets/icon.png (1024²): a leaning green 'kept clip' cut by a playhead. Run: python assets/make_icon.py"""
from pathlib import Path

from PIL import Image, ImageDraw

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((40, 40, S - 40, S - 40), radius=220, fill=(18, 21, 28, 255))
# timeline lanes
for y, w, c in ((300, 520, (58, 64, 80)), (700, 520, (58, 64, 80))):
    d.rounded_rectangle((252, y, 252 + w, y + 46), radius=23, fill=c)
# the leaning kept segment (parallelogram ≈ 20° lean)
d.polygon([(282, 610), (422, 410), (742, 410), (602, 610)], fill=(47, 191, 113, 255))
# playhead / cut line
d.rounded_rectangle((496, 220, 528, 800), radius=16, fill=(79, 140, 255, 255))
d.polygon([(462, 210), (562, 210), (512, 270)], fill=(79, 140, 255, 255))
img.save(Path(__file__).with_name("icon.png"))
