"""Frame renderer (248x58) for the Stream Deck Neo infobar.

This module does not depend on StreamController - only on Pillow - so it can be
tested standalone (see README).
"""
import colorsys
import glob
import hashlib
import math
import os

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

W, H = 248, 58
ART = 50                  # cover art side length
PAD = 4                   # vertical margin of the cover art
MARGIN_L = 12             # default side margins (the Neo bezel clips the edges)
MARGIN_R = 14

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_REG = os.path.join(HERE, "assets", "fonts", "DejaVuSans.ttf")
FONT_BOLD = os.path.join(HERE, "assets", "fonts", "DejaVuSans-Bold.ttf")

_CJK_GLOBS = [
    "/usr/share/fonts/**/NotoSansCJK*Regular*",
    "/usr/share/fonts/**/NotoSansCJK*.ttc",
    "/usr/share/fonts/**/NotoSansJP*",
    "/usr/share/fonts/**/*WenQuanYi*",
    "/usr/share/fonts/**/*DroidSansFallback*",
    "/run/host/fonts/**/NotoSansCJK*Regular*",
    "/run/host/fonts/**/NotoSansCJK*.ttc",
    "/run/host/fonts/**/NotoSansJP*",
    "/run/host/fonts/**/*WenQuanYi*",
    "/run/host/fonts/**/*DroidSansFallback*",
    "/run/host/share/fonts/**/NotoSansCJK*",
]


def _find_cjk():
    for pattern in _CJK_GLOBS:
        try:
            hits = sorted(glob.glob(pattern, recursive=True))
        except Exception:
            hits = []
        if hits:
            return hits[0]
    return None


def _needs_cjk(text):
    for ch in text:
        o = ord(ch)
        if (0x2E80 <= o <= 0x9FFF) or (0xAC00 <= o <= 0xD7AF) or (0xF900 <= o <= 0xFAFF) \
                or (0xFF00 <= o <= 0xFFEF):
            return True
    return False


def fmt_time(sec):
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def extract_accent(img):
    """Most vivid colour of the cover, lifted to a readable brightness."""
    small = img.convert("RGB").resize((24, 24), Image.BILINEAR)
    best, best_w = None, -1.0
    r_sum = g_sum = b_sum = w_sum = 0.0
    for (r, g, b) in small.getdata():
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        w = (s ** 1.5) * (0.25 + v)
        r_sum += r * w
        g_sum += g * w
        b_sum += b * w
        w_sum += w
    if w_sum < 0.5:  # almost grey
        return (230, 230, 240)
    r, g, b = r_sum / w_sum, g_sum / w_sum, b_sum / w_sum
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    s = max(s, 0.55)
    v = max(v, 0.85)
    r, g, b = colorsys.hsv_to_rgb(h, min(s, 1.0), min(v, 1.0))
    return (int(r * 255), int(g * 255), int(b * 255))


def parse_hex(value, default=(255, 79, 163)):
    try:
        v = value.strip().lstrip("#")
        if len(v) == 3:
            v = "".join(c * 2 for c in v)
        return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))
    except Exception:
        return default


def make_placeholder(title, artist, size=ART):
    """Nice placeholder cover (hash-based gradient + first letter)."""
    seed = hashlib.md5(f"{title}|{artist}".encode("utf-8", "ignore")).digest()
    hue = seed[0] / 255.0
    hue2 = (hue + 0.12 + seed[1] / 255.0 * 0.12) % 1.0
    c1 = colorsys.hsv_to_rgb(hue, 0.65, 0.85)
    c2 = colorsys.hsv_to_rgb(hue2, 0.75, 0.40)
    big = size * 2
    grad = Image.new("RGB", (big, big))
    px = grad.load()
    for y in range(big):
        for x in range(big):
            t = (x + y) / (2 * big - 2)
            px[x, y] = tuple(int((c1[i] * (1 - t) + c2[i] * t) * 255) for i in range(3))
    letter = "♪"
    for ch in (title or ""):
        if ch.isalnum():
            letter = ch.upper()
            break
    d = ImageDraw.Draw(grad)
    path = FONT_BOLD
    if _needs_cjk(letter):
        path = _find_cjk() or FONT_BOLD
    try:
        f = ImageFont.truetype(path, int(big * 0.52))
    except Exception:
        f = ImageFont.load_default()
    d.text((big / 2, big / 2 + 2), letter, font=f, fill=(255, 255, 255), anchor="mm")
    return grad.resize((size, size), Image.LANCZOS)


def _cover(img, w, h):
    img = img.convert("RGB")
    sw, sh = img.size
    scale = max(w / sw, h / sh)
    nw, nh = max(1, round(sw * scale)), max(1, round(sh * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def _rounded_mask(size, radius, ss=4):
    big = Image.new("L", (size * ss, size * ss), 0)
    ImageDraw.Draw(big).rounded_rectangle((0, 0, size * ss - 1, size * ss - 1), radius * ss, fill=255)
    return big.resize((size, size), Image.LANCZOS)


class Renderer:
    def __init__(self):
        self._fonts = {}
        self._cjk = _find_cjk()
        self._art_mask = _rounded_mask(ART, 7)
        self._fade_cache = {}
        self._prep_key = None
        self._bg = None
        self._art = None
        self._accent = (255, 79, 163)

    # ---------- fonts ----------
    def font(self, size, bold=False, text=""):
        path = FONT_BOLD if bold else FONT_REG
        if text and _needs_cjk(text) and self._cjk:
            path = self._cjk
        key = (path, size)
        f = self._fonts.get(key)
        if f is None:
            try:
                f = ImageFont.truetype(path, size)
            except Exception:
                f = ImageFont.load_default()
            self._fonts[key] = f
        return f

    # ---------- per-track preparation (cached) ----------
    def prepare(self, key, art_img, title, artist, accent_mode="auto", accent_hex="#ff4fa3"):
        full_key = (key, accent_mode, accent_hex, art_img is not None)
        if full_key == self._prep_key:
            return
        self._prep_key = full_key
        have_real_art = art_img is not None
        base = art_img if have_real_art else make_placeholder(title, artist)
        self._art = _cover(base, ART, ART)
        if accent_mode == "fixed" or not have_real_art:
            if accent_mode == "fixed":
                self._accent = parse_hex(accent_hex)
            else:
                # colour from the placeholder gradient
                self._accent = extract_accent(self._art)
        else:
            self._accent = extract_accent(base)
        # background: blurred, darkened cover with a gradient under the text
        bg = _cover(base, W, H).filter(ImageFilter.GaussianBlur(10))
        bg = ImageEnhance.Brightness(bg).enhance(0.30)
        ramp = Image.new("L", (W, 1))
        ramp.putdata([int(60 + 120 * (x / (W - 1))) for x in range(W)])
        ramp = ramp.resize((W, H))
        bg = Image.composite(Image.new("RGB", (W, H), (8, 8, 11)), bg, ramp)
        self._bg = bg

    # ---------- scrolling text ----------
    def _fade_mask(self, w, h, left):
        key = (w, h, left)
        m = self._fade_cache.get(key)
        if m is None:
            m = Image.new("L", (w, h), 255)
            d = ImageDraw.Draw(m)
            fl, fr = (9 if left else 0), 12
            for i in range(fl):
                d.line([(i, 0), (i, h)], fill=int(255 * i / fl))
            for i in range(fr):
                d.line([(w - 1 - i, 0), (w - 1 - i, h)], fill=int(255 * i / fr))
            self._fade_cache[key] = m
        return m

    def _text_layer(self, text, font, avail, color, t, hold=1.8, speed=30.0, gap=40):
        asc, desc = font.getmetrics()
        h = asc + desc + 2
        layer = Image.new("RGBA", (avail, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        tw = int(font.getlength(text))
        if tw <= avail:
            d.text((0, 0), text, font=font, fill=color)
            return layer, False
        cycle = tw + gap
        period = cycle / speed + hold
        tt = t % period
        off = 0.0 if tt < hold else (tt - hold) * speed
        d.text((-off, 0), text, font=font, fill=color)
        d.text((-off + cycle, 0), text, font=font, fill=color)
        mask = self._fade_mask(avail, h, off > 0.5)
        layer.putalpha(ImageChops.multiply(layer.getchannel("A"), mask))
        return layer, True

    # ---------- frames ----------
    def render_idle(self, t):
        img = Image.new("RGB", (W, H), (8, 8, 11))
        d = ImageDraw.Draw(img)
        accent = (120, 120, 135)
        for i in range(5):
            hgt = 4 + 3 * (0.5 + 0.5 * math.sin(t * 0.8 + i * 1.1))
            x = 18 + i * 8
            d.rounded_rectangle((x, 29 - hgt, x + 4, 29 + hgt), 2, fill=accent)
        f = self.font(15, True)
        d.text((72, 29), "Nothing playing", font=f, fill=(190, 190, 200), anchor="lm")
        return img

    def render(self, st, t_track, t_global, show_eq=True, show_times=True,
               margin_l=MARGIN_L, margin_r=MARGIN_R):
        """st: dict with title, artist, status, position, length."""
        TEXT_X = margin_l + ART + 8
        TEXT_R = W - margin_r
        img = self._bg.copy()
        accent = self._accent
        playing = st.get("status") == "Playing"

        # cover
        art = self._art
        if not playing:
            art = Image.blend(art, Image.new("RGB", art.size, (0, 0, 0)), 0.55)
        img.paste(art, (margin_l, (H - ART) // 2), self._art_mask)
        d = ImageDraw.Draw(img, "RGBA")
        if not playing:
            cx, cy = margin_l + ART // 2, H // 2
            d.rounded_rectangle((cx - 7, cy - 8, cx - 3, cy + 8), 1, fill=(255, 255, 255, 235))
            d.rounded_rectangle((cx + 3, cy - 8, cx + 7, cy + 8), 1, fill=(255, 255, 255, 235))

        # equalizer
        title_avail = TEXT_R - TEXT_X
        if show_eq:
            n, bw, gp = 4, 3, 2
            ex0 = TEXT_R - (n * bw + (n - 1) * gp)
            base_y, max_h = 19, 14
            for i in range(n):
                if playing:
                    a = 0.5 + 0.5 * math.sin(t_global * (3.1 + i * 1.3) + i * 1.7)
                    b = 0.65 + 0.35 * math.sin(t_global * 1.7 + i)
                    hh = 3 + (max_h - 3) * a * b
                else:
                    hh = 3
                x = ex0 + i * (bw + gp)
                d.rounded_rectangle((x, base_y - hh, x + bw - 1, base_y), 1, fill=accent + (255,))
            title_avail = ex0 - 7 - TEXT_X

        # title / artist
        title = st.get("title") or "—"
        artist = st.get("artist") or ""
        ft = self.font(15, True, title)
        fa = self.font(12, False, artist)
        layer, _ = self._text_layer(title, ft, title_avail, (255, 255, 255, 255), t_track)
        img.paste(layer, (TEXT_X, 2), layer)
        if artist:
            layer2, _ = self._text_layer(artist, fa, TEXT_R - TEXT_X, (205, 205, 215, 255), t_track + 0.7)
            img.paste(layer2, (TEXT_X, 21), layer2)

        # progress bar + times
        pos = max(0.0, float(st.get("position") or 0))
        length = float(st.get("length") or 0)
        fs = self.font(10)
        row_y = 47
        x0, x1 = TEXT_X, TEXT_R
        if show_times:
            left_txt = fmt_time(pos)
            right_txt = fmt_time(length) if length > 0 else "LIVE"
            d.text((TEXT_X, row_y), left_txt, font=fs, fill=(175, 175, 188, 255), anchor="lm")
            d.text((TEXT_R, row_y), right_txt, font=fs, fill=(175, 175, 188, 255), anchor="rm")
            lw = int(fs.getlength(left_txt))
            rw = int(fs.getlength(right_txt))
            x0 = TEXT_X + max(lw, 24) + 6
            x1 = TEXT_R - max(rw, 24) - 6
        d.rounded_rectangle((x0, row_y - 2, x1, row_y + 2), 2, fill=(255, 255, 255, 55))
        if length > 0:
            frac = min(1.0, pos / length)
            fx = x0 + (x1 - x0) * frac
            if fx - x0 >= 1:
                d.rounded_rectangle((x0, row_y - 2, fx, row_y + 2), 2, fill=accent + (255,))
            d.ellipse((fx - 3, row_y - 3, fx + 3, row_y + 3), fill=(255, 255, 255, 255))
        return img

    def is_animating(self, st, show_eq, margin_l=MARGIN_L, margin_r=MARGIN_R):
        """Will frames differ without a state change? (lets us skip USB writes)"""
        return True if st.get("status") == "Playing" else self._needs_scroll(st, show_eq, margin_l, margin_r)

    def _needs_scroll(self, st, show_eq, margin_l=MARGIN_L, margin_r=MARGIN_R):
        TEXT_X = margin_l + ART + 8
        TEXT_R = W - margin_r
        title = st.get("title") or "—"
        artist = st.get("artist") or ""
        avail = TEXT_R - TEXT_X - (4 * 3 + 3 * 2 + 7 if show_eq else 0)
        return (self.font(15, True, title).getlength(title) > avail or
                (artist and self.font(12, False, artist).getlength(artist) > TEXT_R - TEXT_X))
