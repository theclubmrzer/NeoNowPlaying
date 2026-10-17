"""Render demo frames offline (needs only Pillow):  python3 tests/render_demo.py [out_dir]"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import neonp_render as R  # noqa: E402
from PIL import Image  # noqa: E402

out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "out")
os.makedirs(out, exist_ok=True)

art = Image.new("RGB", (300, 300))
px = art.load()
for y in range(300):
    for x in range(300):
        px[x, y] = (int(40 + 200 * x / 300), int(30 + 60 * y / 300), int(150 + 100 * (1 - x / 300)))

cases = [
    ("playing", dict(title="la di die", artist="Nessa Barrett", status="Playing", position=47, length=195), art, 1.3),
    ("long_title", dict(title="A very long track title that needs to scroll", artist="Some Artist & Another Artist feat. Third",
                        status="Playing", position=83, length=263), art, 4.0),
    ("paused_placeholder", dict(title="Montagem Coral", artist="DJ Mix", status="Paused", position=20, length=180), None, 1.0),
    ("cjk", dict(title="夜に駆ける", artist="YOASOBI", status="Playing", position=100, length=260), None, 1.0),
]
r = R.Renderer()
files = []
for name, st, a, t in cases:
    r.prepare(name, a, st["title"], st["artist"])
    f = os.path.join(out, f"{name}.png")
    r.render(st, t, t).save(f)
    files.append(f)
f = os.path.join(out, "idle.png")
r.render_idle(1.0).save(f)
files.append(f)

sheet = Image.new("RGB", (248 * 3 + 8, (58 * 3 + 6) * len(files)), (40, 40, 40))
for i, f in enumerate(files):
    sheet.paste(Image.open(f).resize((248 * 3, 58 * 3), Image.NEAREST), (4, i * (58 * 3 + 6) + 3))
sheet.save(os.path.join(out, "sheet.png"))
print("wrote", out)
