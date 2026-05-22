"""
pvp_board.py — MONSTRS PvP board compositor.
Clean rebuild. Template: battlegame2.png (4800x3584 landscape).
Output: 1600x1194.
"""

import io, os, urllib.request
from typing import Optional
from dataclasses import dataclass
from PIL import Image, ImageDraw, ImageFont

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "battlegame2.png")
OUTPUT_W, OUTPUT_H = 1600, 1194
SCALE = OUTPUT_W / 4800

def _s(v): return int(v * SCALE)

# Image zones (from guide)
P1_IMG = (_s(783),  _s(852),  _s(1507), _s(1576))
P2_IMG = (_s(3302), _s(2202), _s(4026), _s(2926))

# Text zones — raw output pixels, open dark space next to each image
P1_TEXT = (540,  200, 1580, 580)   # right of P1 image, upper area
P2_TEXT = (20,   640, 1060, 1020)  # left of P2 image, lower area

def _font(size):
    for p in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]:
        if os.path.exists(p):
            try: return ImageFont.truetype(p, size)
            except: pass
    return ImageFont.load_default()

# Font sizes — absolute pixels on the 1600px canvas
# Text zone is ~1040px wide, displays at ~260px on Discord (4x scale)
# So 60px canvas = 15px visible. Need ~24px visible = 96px canvas.
F_NAME = 96
F_USER = 72
F_STAT = 68
F_WAIT = 80

WHITE  = (255, 255, 255, 255)
YELLOW = (255, 215, 50,  255)
GREEN  = (80,  255, 130, 255)
RED    = (255, 90,  90,  255)
BLUE   = (100, 170, 255, 255)
GREY   = (210, 210, 210, 255)

@dataclass
class BoardPlayer:
    monstr_name: str
    username:    str
    attack:      int
    defense:     int
    speed:       int
    hp:          int
    image_url:   Optional[str] = None
    is_winner:   bool = False

def _t(draw, x, y, txt, font, color):
    """Text with drop shadow."""
    draw.text((x+2, y+2), txt, font=font, fill=(0,0,0,255), anchor="lt")
    draw.text((x,   y),   txt, font=font, fill=color,       anchor="lt")

def _fetch(url, size):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        img.thumbnail(size, Image.LANCZOS)
        c = Image.new("RGBA", size, (0,0,0,0))
        c.paste(img, ((size[0]-img.width)//2, (size[1]-img.height)//2), img)
        return c
    except Exception as e:
        print(f"[PVP] NFT fetch failed: {e}")
        return None

def render_board(state, p1=None, p2=None, status_text=""):
    board = Image.open(TEMPLATE_PATH).convert("RGBA")
    board = board.resize((OUTPUT_W, OUTPUT_H), Image.LANCZOS)
    draw  = ImageDraw.Draw(board)

    fn = _font(F_NAME)
    fu = _font(F_USER)
    fs = _font(F_STAT)
    fw = _font(F_WAIT)

    # NFT images
    for player, zone in [(p1, P1_IMG), (p2, P2_IMG)]:
        if player and player.image_url:
            size = (zone[2]-zone[0], zone[3]-zone[1])
            nft  = _fetch(player.image_url, size)
            if nft:
                board.paste(nft, (zone[0], zone[1]), nft)

    # Text zones
    for player, zone, slot in [(p1, P1_TEXT, 1), (p2, P2_TEXT, 2)]:
        x1, y1, x2, y2 = zone
        ty = y1

        if player is None:
            _t(draw, x1, ty,             "WAITING...",       fw, WHITE)
            _t(draw, x1, ty + F_WAIT+8,  f"Slot {slot} open", fu, GREY)
            continue

        if player.is_winner and state == "result":
            _t(draw, x1, ty, "WINNER!", fn, YELLOW)
            ty += F_NAME + 8

        _t(draw, x1, ty, player.monstr_name.upper(), fn, WHITE)
        ty += F_NAME + 8

        _t(draw, x1, ty, f"@{player.username}", fu, YELLOW)
        ty += F_USER + 14

        # Stats on one line
        _t(draw, x1,             ty, f"ATK {player.attack}",  fs, RED)
        _t(draw, x1 + 340,       ty, f"DEF {player.defense}", fs, BLUE)
        _t(draw, x1 + 680,       ty, f"SPD {player.speed}",   fs, GREEN)

    buf = io.BytesIO()
    board.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf

def board_waiting(p1=None):
    return render_board("waiting", p1, None,
                        "Waiting for opponent..." if p1 else "No active challenge")

def board_active(p1, p2):
    return render_board("active", p1, p2, "BATTLE IN PROGRESS")

def board_result(p1, p2):
    return render_board("result", p1, p2, "BATTLE COMPLETE")
