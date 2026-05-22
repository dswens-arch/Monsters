"""
pvp_board.py
------------
MONSTRS PvP — Pillow board compositor (landscape).

Template: battlegame2.png — 4800x3584
Output:   1600x1194 (scale 1/3)

Zones (template space):
  P1 ASA image  (top-left):   x=783-1507   y=852-1576
  P1 text       (top-right):  x=2036-4230  y=815-1614
  P2 ASA image  (bot-right):  x=3302-4026  y=2202-2926
  P2 text       (bot-left):   x=560-2754   y=2172-2970
"""

import io
import os
import urllib.request
from typing import Optional
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont, ImageFilter


TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "battlegame2.png")

OUTPUT_W = 1600
OUTPUT_H = 1194
SCALE    = OUTPUT_W / 4800  # 0.3333

def _s(v): return int(v * SCALE)

# ── Zones (output space) ────────────────────────
P1_IMG  = (_s(783),  _s(852),  _s(1507), _s(1576))
P1_TEXT = (_s(2036), _s(815),  _s(4230), _s(1614))
P2_IMG  = (_s(3302), _s(2202), _s(4026), _s(2926))
P2_TEXT = (_s(560),  _s(2172), _s(2754), _s(2970))

P1_IMG_W = P1_IMG[2] - P1_IMG[0]
P1_IMG_H = P1_IMG[3] - P1_IMG[1]
P2_IMG_W = P2_IMG[2] - P2_IMG[0]
P2_IMG_H = P2_IMG[3] - P2_IMG[1]

# ── Colors ──────────────────────────────────────
COL_WHITE  = (255, 255, 255, 255)
COL_YELLOW = (255, 220, 50,  255)
COL_GREEN  = (100, 255, 140, 255)
COL_GREY   = (225, 225, 225, 255)
COL_RED    = (255, 100, 100, 255)
COL_BLUE   = (110, 180, 255, 255)

# ── Font sizes ──────────────────────────────────
# Text zone is 732px wide on canvas, displays at ~229px on Discord
# GP uses 36px bold on 800px canvas at ~469px Discord display
# Our zone is ~half GP display width so we need ~2x GP canvas sizes
FONT_NAME  = 264
FONT_USER  = 192
FONT_STAT  = 208
FONT_WAIT  = 240
FONT_SMALL = 152


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


def _fetch_nft_image(url: str, size: tuple) -> Optional[Image.Image]:
    try:
        gateways = [
            url,
            url.replace("ipfs.algonode.xyz", "dweb.link"),
            url.replace("ipfs.algonode.xyz", "ipfs.io"),
        ]
        data = None
        for gw_url in gateways:
            try:
                req = urllib.request.Request(gw_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    data = r.read()
                break
            except Exception:
                continue
        if not data:
            return None
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        img.thumbnail(size, Image.LANCZOS)
        canvas = Image.new("RGBA", size, (0, 0, 0, 0))
        offset = ((size[0] - img.width) // 2, (size[1] - img.height) // 2)
        canvas.paste(img, offset, img)
        return canvas
    except Exception as e:
        print(f"[PVP BOARD] NFT fetch failed: {e}")
        return None


def _make_placeholder(size: tuple) -> Image.Image:
    img  = Image.new("RGBA", size, (15, 10, 28, 180))
    draw = ImageDraw.Draw(img)
    font = _load_font(int(size[0] * 0.35))
    draw.text((size[0]//2, size[1]//2), "?",
              font=font, fill=(80, 60, 110, 150), anchor="mm")
    return img


def _draw_text_zone(draw, zone, player: Optional[BoardPlayer], state: str, slot: int):
    x1, y1, x2, y2 = zone
    w   = x2 - x1
    h   = y2 - y1
    cx  = x1 + w // 2
    pad = int(h * 0.08)
    ty  = y1 + pad

    fn  = _load_font(FONT_NAME)
    fu  = _load_font(FONT_USER)
    fst = _load_font(FONT_STAT)
    fw  = _load_font(FONT_WAIT)
    fsl = _load_font(FONT_SMALL)

    if player is None:
        draw.text((cx, y1 + h//2 - int(h*0.10)),
                  "WAITING...", font=fw, fill=COL_WHITE, anchor="mm")
        draw.text((cx, y1 + h//2 + int(h*0.14)),
                  f"Slot {slot} open", font=fsl,
                  fill=(200, 180, 255, 220), anchor="mm")
        return

    # Winner crown
    if player.is_winner and state == "result":
        draw.text((cx, ty), "👑 WINNER!", font=fn, fill=COL_YELLOW, anchor="mt")
        ty += int(FONT_NAME * 1.4)

    # MONSTR name
    draw.text((cx, ty), player.monstr_name.upper(),
              font=fn, fill=COL_WHITE, anchor="mt")
    ty += int(FONT_NAME * 1.3)

    # @username
    draw.text((cx, ty), f"@{player.username}",
              font=fu, fill=COL_YELLOW, anchor="mt")
    ty += int(FONT_USER * 1.6)

    # Divider
    draw.line([(x1 + int(w*0.05), ty), (x2 - int(w*0.05), ty)],
              fill=(180, 160, 220, 130), width=2)
    ty += int(h * 0.07)

    # Stats: ATK / DEF / SPD on one line, evenly spaced
    stats = [
        ("ATK", player.attack,  COL_RED),
        ("DEF", player.defense, COL_BLUE),
        ("SPD", player.speed,   COL_GREEN),
    ]
    seg_w = w // len(stats)
    for i, (label, val, color) in enumerate(stats):
        sx = x1 + i * seg_w + seg_w // 2
        draw.text((sx, ty), f"{label} {val}",
                  font=fst, fill=color, anchor="mt")


def render_board(state: str,
                 p1: Optional[BoardPlayer] = None,
                 p2: Optional[BoardPlayer] = None,
                 status_text: str = "") -> io.BytesIO:

    template = Image.open(TEMPLATE_PATH).convert("RGBA")
    board    = template.resize((OUTPUT_W, OUTPUT_H), Image.LANCZOS)

    # ── NFT images ──────────────────────────────
    for player, zone in [(p1, P1_IMG), (p2, P2_IMG)]:
        if not player:
            continue  # no player — let template show through
        size = (zone[2]-zone[0], zone[3]-zone[1])
        if player.image_url:
            nft = _fetch_nft_image(player.image_url, size)
            if nft:
                board.paste(nft, (zone[0], zone[1]), nft)
        # No image URL — still let template show through

    # ── Winner glow / loser dim ──────────────────
    if state == "result":
        for player, img_zone in [(p1, P1_IMG), (p2, P2_IMG)]:
            if not player: continue
            if player.is_winner:
                glow = Image.new("RGBA", board.size, (0, 0, 0, 0))
                gd   = ImageDraw.Draw(glow)
                gd.rectangle(
                    [img_zone[0]-14, img_zone[1]-14,
                     img_zone[2]+14, img_zone[3]+14],
                    fill=(255, 200, 0, 65)
                )
                board = Image.alpha_composite(
                    board, glow.filter(ImageFilter.GaussianBlur(14))
                )
            else:
                dim = Image.new("RGBA", board.size, (0, 0, 0, 0))
                ImageDraw.Draw(dim).rectangle(list(img_zone), fill=(0, 0, 0, 150))
                board = Image.alpha_composite(board, dim)

    # ── Text zones ──────────────────────────────
    draw = ImageDraw.Draw(board)
    _draw_text_zone(draw, P1_TEXT, p1, state, slot=1)
    _draw_text_zone(draw, P2_TEXT, p2, state, slot=2)

    # ── Status text (bottom center) ──────────────
    if status_text:
        fst = _load_font(int(OUTPUT_W * 0.026))
        draw.text((OUTPUT_W // 2, int(OUTPUT_H * 0.962)),
                  status_text, font=fst, fill=COL_WHITE, anchor="mm")

    buf = io.BytesIO()
    board.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


# ── Convenience wrappers ────────────────────────

def board_waiting(p1: Optional[BoardPlayer] = None) -> io.BytesIO:
    return render_board("waiting", p1, None,
                        "Waiting for opponent..." if p1 else "No active challenge")

def board_active(p1: BoardPlayer, p2: BoardPlayer) -> io.BytesIO:
    return render_board("active", p1, p2, "⚔️ BATTLE IN PROGRESS")

def board_result(p1: BoardPlayer, p2: BoardPlayer) -> io.BytesIO:
    return render_board("result", p1, p2, "🏆 BATTLE COMPLETE")
