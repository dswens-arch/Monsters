"""
pvp_board_result.py
-------------------
MONSTRS PvP — Winner board compositor (landscape).

Template: battlegame_winner2.png — 4800x3584
Output:   1600x1194 (scale 1/3)

Zones (template space):
  Winner ASA image (gold box):  x=539-1846   y=1069-2349
  Winner text (right side):     x=2000-4200  y=1069-2349
"""

import io
import os
import urllib.request
from typing import Optional
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont


RESULT_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "battlegame_winner2.png")

OUTPUT_W = 1600
OUTPUT_H = 1194
SCALE    = OUTPUT_W / 4800

def _s(v): return int(v * SCALE)

WIN_IMG  = (_s(539),  _s(1069), _s(1846), _s(2349))
WIN_TEXT = (_s(2000), _s(1069), _s(4200), _s(2349))

WIN_IMG_W  = WIN_IMG[2]  - WIN_IMG[0]
WIN_IMG_H  = WIN_IMG[3]  - WIN_IMG[1]
WIN_TEXT_W = WIN_TEXT[2] - WIN_TEXT[0]
WIN_TEXT_H = WIN_TEXT[3] - WIN_TEXT[1]

COL_WHITE  = (255, 255, 255, 255)
COL_YELLOW = (255, 220, 50,  255)
COL_GREEN  = (100, 255, 140, 255)
COL_GREY   = (225, 225, 225, 255)

FONT_NAME  = int(OUTPUT_W * 0.048)
FONT_USER  = int(OUTPUT_W * 0.034)
FONT_INFO  = int(OUTPUT_W * 0.028)


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/root/.nix-profile/share/fonts/truetype/DejaVuSans-Bold.ttf",
    ]:
        if os.path.exists(path):
            try: return ImageFont.truetype(path, size)
            except: continue
    return ImageFont.load_default()


def _fetch_nft_image(url: str, size: tuple) -> Optional[Image.Image]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        img.thumbnail(size, Image.LANCZOS)
        canvas = Image.new("RGBA", size, (0, 0, 0, 0))
        offset = ((size[0] - img.width) // 2, (size[1] - img.height) // 2)
        canvas.paste(img, offset, img)
        return canvas
    except Exception as e:
        print(f"[PVP BOARD] winner NFT fetch failed: {e}")
        return None


@dataclass
class WinnerInfo:
    monstr_name:  str
    username:     str
    total_rounds: int
    wager_won:    int
    image_url:    Optional[str] = None
    is_draw:      bool = False


def render_result(winner: WinnerInfo) -> io.BytesIO:
    template = Image.open(RESULT_TEMPLATE_PATH).convert("RGBA")
    board    = template.resize((OUTPUT_W, OUTPUT_H), Image.LANCZOS)

    # ── NFT image ────────────────────────────────
    img_size = (WIN_IMG_W, WIN_IMG_H)
    if winner.image_url and not winner.is_draw:
        nft = _fetch_nft_image(winner.image_url, img_size)
        if nft:
            board.paste(nft, (WIN_IMG[0], WIN_IMG[1]), nft)

    # ── Text zone ────────────────────────────────
    draw = ImageDraw.Draw(board)
    x1, y1, x2, y2 = WIN_TEXT
    w  = x2 - x1
    h  = y2 - y1
    cx = x1 + w // 2

    fn   = _load_font(FONT_NAME)
    fu   = _load_font(FONT_USER)
    fi   = _load_font(FONT_INFO)

    if winner.is_draw:
        draw.text((cx, y1 + h//2), "DRAW — Wagers Refunded",
                  font=fu, fill=COL_GREY, anchor="mm")
    else:
        # Vertically center the block of text
        total_h = (FONT_NAME + int(FONT_NAME*0.3) +
                   FONT_USER + int(FONT_USER*0.5) +
                   10 +  # divider gap
                   FONT_INFO * 2)
        ty = y1 + (h - total_h) // 2

        # MONSTR name
        draw.text((cx, ty), winner.monstr_name.upper(),
                  font=fn, fill=COL_WHITE, anchor="mt")
        ty += int(FONT_NAME * 1.3)

        # @username
        draw.text((cx, ty), f"@{winner.username}",
                  font=fu, fill=COL_YELLOW, anchor="mt")
        ty += int(FONT_USER * 1.5)

        # Divider
        draw.line([(x1 + int(w*0.08), ty), (x2 - int(w*0.08), ty)],
                  fill=(180, 160, 220, 130), width=2)
        ty += int(h * 0.06)

        # Rounds
        draw.text((cx, ty), f"{winner.total_rounds} rounds",
                  font=fi, fill=COL_GREY, anchor="mt")
        ty += int(FONT_INFO * 1.5)

        # Payout
        draw.text((cx, ty), f"+{winner.wager_won:,} $GOO",
                  font=fi, fill=COL_GREEN, anchor="mt")

    buf = io.BytesIO()
    board.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf
