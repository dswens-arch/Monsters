"""
pvp_board_result.py
-------------------
MONSTRS PvP — Winner board compositor.

Template zones (on 3584x4800 canvas):
  Winner NFT image (gold box interior): x=943-2641  y=1198-2894  (1698x1696)
  Text area (below WINNER! text):       x=400-3184  y=3600-4500  (open space)
"""

import io
import os
import urllib.request
from typing import Optional
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont


RESULT_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "battlegame_winner.png")

OUTPUT_W = 800
OUTPUT_H = 1067
SCALE    = OUTPUT_W / 3584

def _s(v): return int(v * SCALE)

# Zones
WIN_IMG  = (_s(943),  _s(1198), _s(2641), _s(2894))
WIN_TEXT = (_s(400),  _s(3600), _s(3184), _s(4500))

WIN_IMG_W  = WIN_IMG[2]  - WIN_IMG[0]
WIN_IMG_H  = WIN_IMG[3]  - WIN_IMG[1]
WIN_TEXT_W = WIN_TEXT[2] - WIN_TEXT[0]
WIN_TEXT_H = WIN_TEXT[3] - WIN_TEXT[1]

# Colors
COL_WHITE  = (255, 255, 255, 255)
COL_YELLOW = (255, 220, 50,  255)
COL_GREEN  = (80,  255, 120, 255)
COL_GREY   = (210, 210, 210, 255)

FONT_NAME  = int(OUTPUT_W * 0.050)
FONT_USER  = int(OUTPUT_W * 0.036)
FONT_SMALL = int(OUTPUT_W * 0.026)


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/root/.nix-profile/share/fonts/truetype/DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
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
    attack:       int
    defense:      int
    speed:        int
    hp:           int
    total_rounds: int
    wager_won:    int
    image_url:    Optional[str] = None
    is_draw:      bool = False


def render_result(winner: WinnerInfo) -> io.BytesIO:
    template = Image.open(RESULT_TEMPLATE_PATH).convert("RGBA")
    board    = template.resize((OUTPUT_W, OUTPUT_H), Image.LANCZOS)

    # ── NFT image into gold box ───────────────
    img_size = (WIN_IMG_W, WIN_IMG_H)
    if winner.image_url and not winner.is_draw:
        nft = _fetch_nft_image(winner.image_url, img_size)
        if nft:
            board.paste(nft, (WIN_IMG[0], WIN_IMG[1]), nft)

    # ── Text below WINNER! ────────────────────
    draw = ImageDraw.Draw(board)
    x1, y1, x2, y2 = WIN_TEXT
    w  = x2 - x1
    h  = y2 - y1
    cx = x1 + w // 2
    pad = int(h * 0.10)
    ty  = y1 + pad

    font_name  = _load_font(FONT_NAME)
    font_user  = _load_font(FONT_USER)
    font_small = _load_font(FONT_SMALL)

    if winner.is_draw:
        draw.text((cx, y1 + h // 2), "DRAW — Wagers Refunded",
                  font=font_user, fill=COL_GREY, anchor="mm")
    else:
        # MONSTR name
        draw.text((cx, ty), winner.monstr_name.upper(),
                  font=font_name, fill=COL_WHITE, anchor="mt")
        ty += int(FONT_NAME * 1.3)

        # @username
        draw.text((cx, ty), f"@{winner.username}",
                  font=font_user, fill=COL_YELLOW, anchor="mt")
        ty += int(FONT_USER * 1.5)

        # Thin divider
        draw.line([(x1 + int(w*0.15), ty), (x2 - int(w*0.15), ty)],
                  fill=(200, 200, 200, 100), width=1)
        ty += int(h * 0.08)

        # Stats + payout on one line each
        draw.text((cx, ty),
                  f"ATK {winner.attack}  ·  DEF {winner.defense}  ·  SPD {winner.speed}",
                  font=font_small, fill=COL_GREY, anchor="mt")
        ty += int(FONT_SMALL * 1.6)

        draw.text((cx, ty),
                  f"{winner.total_rounds} rounds  ·  +{winner.wager_won:,} $GOO",
                  font=font_small, fill=COL_GREEN, anchor="mt")

    buf = io.BytesIO()
    board.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf
