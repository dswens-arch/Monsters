"""
pvp_board.py
------------
MONSTRS PvP — Pillow board image compositor.

Layers MONSTR NFT images and player info onto the arcade cabinet template.
Returns a BytesIO PNG ready to post in Discord.

Board states:
  'waiting'  — one or both slots empty, shows placeholder with "Waiting..."
  'active'   — both fighters locked in, battle in progress
  'result'   — battle over, winner highlighted with crown

Template zones (on 3584x4800 canvas):
  P1 NFT image  (yellow, top-left):   x=440-1341   y=987-1889
  P1 text       (green,  top-right):  x=1608-3089  y=1065-1792
  P2 NFT image  (yellow, bot-right):  x=2254-3155  y=2989-3891
  P2 text       (green,  bot-left):   x=510-1991   y=3104-3831
"""

import io
import os
import urllib.request
from typing import Optional
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance


# ─────────────────────────────────────────────
# TEMPLATE PATH
# ─────────────────────────────────────────────

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "battlegame.png")

# Output size — scale down for Discord (keep 4:5 ratio, 3584x4800 → 800x1067)
OUTPUT_W = 800
OUTPUT_H = 1067

# Scale factor from template to output
SCALE = OUTPUT_W / 3584  # ~0.2232

# ─────────────────────────────────────────────
# ZONE COORDINATES (template space → scaled)
# ─────────────────────────────────────────────

def _s(v):
    """Scale a template-space coordinate to output space."""
    return int(v * SCALE)


# NFT image zones (yellow)
P1_IMG  = (_s(440),  _s(987),  _s(1341), _s(1889))   # left, top, right, bottom
P2_IMG  = (_s(2254), _s(2989), _s(3155), _s(3891))

# Text zones (green)
P1_TEXT = (_s(1608), _s(1065), _s(3089), _s(1792))
P2_TEXT = (_s(510),  _s(3104), _s(1991), _s(3831))

# Zone sizes
P1_IMG_W  = P1_IMG[2]  - P1_IMG[0]
P1_IMG_H  = P1_IMG[3]  - P1_IMG[1]
P2_IMG_W  = P2_IMG[2]  - P2_IMG[0]
P2_IMG_H  = P2_IMG[3]  - P2_IMG[1]
P1_TEXT_W = P1_TEXT[2] - P1_TEXT[0]
P1_TEXT_H = P1_TEXT[3] - P1_TEXT[1]
P2_TEXT_W = P2_TEXT[2] - P2_TEXT[0]
P2_TEXT_H = P2_TEXT[3] - P2_TEXT[1]


# ─────────────────────────────────────────────
# COLORS & FONTS
# ─────────────────────────────────────────────

COL_WHITE      = (255, 255, 255, 255)
COL_YELLOW     = (255, 220, 50,  255)
COL_RED        = (255, 60,  60,  255)
COL_GREEN      = (80,  255, 120, 255)
COL_GREY       = (160, 160, 160, 255)
COL_BLACK      = (0,   0,   0,   255)
COL_WAITING_BG = (30,  20,  40,  200)
COL_WIN_GLOW   = (255, 215, 0,   180)

# Font sizes (output space)
FONT_NAME   = int(OUTPUT_W * 0.045)   # ~36px — MONSTR name
FONT_USER   = int(OUTPUT_W * 0.032)   # ~26px — @username
FONT_STAT   = int(OUTPUT_W * 0.026)   # ~21px — stat values
FONT_WAIT   = int(OUTPUT_W * 0.038)   # ~30px — "Waiting..."
FONT_WIN    = int(OUTPUT_W * 0.055)   # ~44px — "WINNER!"


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """Try to load a bold pixel-ish font, fall back to default."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/root/.nix-profile/share/fonts/truetype/DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


# ─────────────────────────────────────────────
# DATA CLASS
# ─────────────────────────────────────────────

@dataclass
class BoardPlayer:
    monstr_name: str          # e.g. "MONSTR #0420"
    username:    str          # Discord display name
    attack:      int
    defense:     int
    speed:       int
    hp:          int
    image_url:   Optional[str] = None   # IPFS image URL
    is_winner:   bool = False


# ─────────────────────────────────────────────
# NFT IMAGE FETCHER
# ─────────────────────────────────────────────

def _fetch_nft_image(url: str, size: tuple[int, int]) -> Optional[Image.Image]:
    """Download MONSTR NFT image and resize to fit zone. Returns None on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        # Fit within zone maintaining aspect ratio
        img.thumbnail(size, Image.LANCZOS)
        # Center on transparent canvas of exact zone size
        canvas = Image.new("RGBA", size, (0, 0, 0, 0))
        offset = ((size[0] - img.width) // 2, (size[1] - img.height) // 2)
        canvas.paste(img, offset, img if img.mode == "RGBA" else None)
        return canvas
    except Exception as e:
        print(f"[PVP BOARD] NFT image fetch failed {url[:40]}...: {e}")
        return None


# ─────────────────────────────────────────────
# PLACEHOLDER IMAGE (when slot is empty)
# ─────────────────────────────────────────────

def _make_placeholder(size: tuple[int, int], text: str = "?") -> Image.Image:
    img    = Image.new("RGBA", size, (20, 15, 35, 220))
    draw   = ImageDraw.Draw(img)
    font   = _load_font(int(size[0] * 0.35))
    draw.text((size[0] // 2, size[1] // 2), text, font=font,
              fill=(80, 60, 100, 180), anchor="mm")
    # Dashed border
    border_col = (100, 80, 140, 200)
    for i in range(0, size[0], 20):
        draw.line([(i, 0), (min(i+10, size[0]), 0)], fill=border_col, width=2)
        draw.line([(i, size[1]-1), (min(i+10, size[0]), size[1]-1)], fill=border_col, width=2)
    for i in range(0, size[1], 20):
        draw.line([(0, i), (0, min(i+10, size[1]))], fill=border_col, width=2)
        draw.line([(size[0]-1, i), (size[0]-1, min(i+10, size[1]))], fill=border_col, width=2)
    return img


# ─────────────────────────────────────────────
# STAT BAR RENDERER
# ─────────────────────────────────────────────

def _draw_stat_bar(draw: ImageDraw.Draw, x: int, y: int, w: int,
                   label: str, value: int, max_val: int = 50,
                   bar_color: tuple = (80, 220, 120, 255),
                   font_small=None, font_stat=None):
    """Draw a labeled stat bar at (x, y) with width w."""
    bar_h     = int(OUTPUT_W * 0.018)   # ~14px
    label_w   = int(w * 0.22)
    bar_w     = int(w * 0.58)
    val_w     = int(w * 0.18)
    gap       = int(w * 0.02)

    # Label
    draw.text((x, y + bar_h // 2), label, font=font_small,
              fill=(220, 220, 220, 255), anchor="lm")

    # Bar background
    bx = x + label_w + gap
    draw.rectangle([bx, y, bx + bar_w, y + bar_h],
                   fill=(40, 30, 55, 200))

    # Bar fill
    fill_w = int(bar_w * (value / max_val))
    if fill_w > 0:
        draw.rectangle([bx, y, bx + fill_w, y + bar_h],
                       fill=bar_color)

    # Value
    vx = bx + bar_w + gap
    draw.text((vx, y + bar_h // 2), str(value), font=font_small,
              fill=COL_WHITE, anchor="lm")


# ─────────────────────────────────────────────
# TEXT ZONE RENDERER
# ─────────────────────────────────────────────

def _draw_player_text(draw: ImageDraw.Draw, zone: tuple,
                      player: Optional[BoardPlayer],
                      state: str, slot: int):
    """
    Draw player info into the green text zone.
    zone = (x1, y1, x2, y2)
    slot = 1 or 2
    """
    x1, y1, x2, y2 = zone
    w = x2 - x1
    h = y2 - y1
    pad = int(w * 0.06)

    font_name  = _load_font(FONT_NAME)
    font_user  = _load_font(FONT_USER)
    font_stat  = _load_font(FONT_STAT)
    font_small = _load_font(int(FONT_STAT * 0.85))
    font_wait  = _load_font(FONT_WAIT)
    font_win   = _load_font(FONT_WIN)

    cx = x1 + w // 2   # center x

    if player is None:
        # Empty slot
        draw.text(
            (cx, y1 + h // 2 - int(h * 0.1)),
            "WAITING...",
            font=font_wait, fill=(255, 255, 255, 240), anchor="mm"
        )
        draw.text(
            (cx, y1 + h // 2 + int(h * 0.12)),
            f"Slot {slot} open",
            font=font_small, fill=(200, 180, 255, 220), anchor="mm"
        )
        return

    ty = y1 + pad

    # WINNER banner
    if player.is_winner and state == "result":
        draw.text(
            (cx, ty),
            "👑 WINNER!",
            font=font_win, fill=COL_YELLOW, anchor="mt"
        )
        ty += int(FONT_WIN * 1.3)

    # MONSTR name
    draw.text((cx, ty), player.monstr_name.upper(),
              font=font_name, fill=COL_WHITE, anchor="mt")
    ty += int(FONT_NAME * 1.3)

    # Username
    draw.text((cx, ty), f"@{player.username}",
              font=font_user, fill=COL_YELLOW, anchor="mt")
    ty += int(FONT_USER * 1.6)

    # Divider
    draw.line([(x1 + pad, ty), (x2 - pad, ty)], fill=(80, 60, 100, 150), width=1)
    ty += int(h * 0.04)

    # HP
    draw.text((cx, ty), f"HP  {player.hp}",
              font=font_stat, fill=COL_GREEN, anchor="mt")
    ty += int(FONT_STAT * 1.5)

    # Stat bars
    bar_w = int(w * 0.85)
    bx    = x1 + (w - bar_w) // 2
    spacing = int(h * 0.095)

    for label, value, color in [
        ("ATK", player.attack,  (255, 90,  90,  255)),
        ("DEF", player.defense, (90,  150, 255, 255)),
        ("SPD", player.speed,   (90,  255, 160, 255)),
    ]:
        _draw_stat_bar(draw, bx, ty, bar_w, label, value,
                       bar_color=color, font_small=font_small, font_stat=font_stat)
        ty += spacing

    # Loser dim overlay hint (text color muted)
    if state == "result" and not player.is_winner:
        # Already drawn — we'll apply dim at image level
        pass


# ─────────────────────────────────────────────
# WINNER GLOW
# ─────────────────────────────────────────────

def _apply_winner_glow(board: Image.Image, img_zone: tuple) -> Image.Image:
    """Draw a golden glow rect behind the winner's NFT zone."""
    x1, y1, x2, y2 = img_zone
    glow = Image.new("RGBA", board.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow)
    pad  = 8
    draw.rectangle(
        [x1 - pad, y1 - pad, x2 + pad, y2 + pad],
        fill=(255, 200, 0, 60)
    )
    glow = glow.filter(ImageFilter.GaussianBlur(radius=12))
    return Image.alpha_composite(board, glow)


def _apply_loser_dim(board: Image.Image, img_zone: tuple) -> Image.Image:
    """Darken the loser's NFT zone."""
    x1, y1, x2, y2 = img_zone
    dim = Image.new("RGBA", board.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(dim)
    draw.rectangle([x1, y1, x2, y2], fill=(0, 0, 0, 140))
    return Image.alpha_composite(board, dim)


# ─────────────────────────────────────────────
# MAIN COMPOSITOR
# ─────────────────────────────────────────────

def render_board(
    state: str,                          # 'waiting' | 'active' | 'result'
    p1: Optional[BoardPlayer] = None,    # top-left fighter
    p2: Optional[BoardPlayer] = None,    # bottom-right fighter
    status_text: str = "",               # e.g. "Round 3 of 20" or "BATTLE COMPLETE"
) -> io.BytesIO:
    """
    Render the PvP board and return a BytesIO PNG.

    p1 = top-left slot (challenger)
    p2 = bottom-right slot (opponent)
    Either can be None for the 'waiting' state.
    """

    # Load and resize template
    template = Image.open(TEMPLATE_PATH).convert("RGBA")
    board    = template.resize((OUTPUT_W, OUTPUT_H), Image.LANCZOS)

    # ── Layer NFT images ──────────────────────

    p1_img_size = (P1_IMG_W, P1_IMG_H)
    p2_img_size = (P2_IMG_W, P2_IMG_H)

    if p1 and p1.image_url:
        nft = _fetch_nft_image(p1.image_url, p1_img_size)
        if nft:
            board.paste(nft, (P1_IMG[0], P1_IMG[1]), nft)
        else:
            ph = _make_placeholder(p1_img_size, "?")
            board.paste(ph, (P1_IMG[0], P1_IMG[1]), ph)
    else:
        ph = _make_placeholder(p1_img_size, "?" if p1 is None else "...")
        board.paste(ph, (P1_IMG[0], P1_IMG[1]), ph)

    if p2 and p2.image_url:
        nft = _fetch_nft_image(p2.image_url, p2_img_size)
        if nft:
            board.paste(nft, (P2_IMG[0], P2_IMG[1]), nft)
        else:
            ph = _make_placeholder(p2_img_size, "?")
            board.paste(ph, (P2_IMG[0], P2_IMG[1]), ph)
    else:
        ph = _make_placeholder(p2_img_size, "?" if p2 is None else "...")
        board.paste(ph, (P2_IMG[0], P2_IMG[1]), ph)

    # ── Winner/loser glow ─────────────────────

    if state == "result":
        if p1 and p1.is_winner:
            board = _apply_winner_glow(board, P1_IMG)
            if p2:
                board = _apply_loser_dim(board, P2_IMG)
        elif p2 and p2.is_winner:
            board = _apply_winner_glow(board, P2_IMG)
            if p1:
                board = _apply_loser_dim(board, P1_IMG)

    # ── Draw text zones ───────────────────────

    draw = ImageDraw.Draw(board)
    _draw_player_text(draw, P1_TEXT, p1, state, slot=1)
    _draw_player_text(draw, P2_TEXT, p2, state, slot=2)

    # ── Status text — above bottom border ────────

    if status_text:
        font_status = _load_font(int(OUTPUT_W * 0.032))
        # 0.88 keeps it above the CREDIT 00 border area
        draw.text(
            (OUTPUT_W // 2, int(OUTPUT_H * 0.88)),
            status_text,
            font=font_status, fill=(255, 255, 255, 255), anchor="mm"
        )

    # ── Output ────────────────────────────────

    buf = io.BytesIO()
    board.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


# ─────────────────────────────────────────────
# CONVENIENCE WRAPPERS
# ─────────────────────────────────────────────

def board_waiting(p1: Optional[BoardPlayer] = None) -> io.BytesIO:
    """No challenger yet, or challenger posted but no opponent."""
    return render_board(
        state="waiting",
        p1=p1,
        p2=None,
        status_text="Waiting for opponent..." if p1 else "No active challenge"
    )


def board_active(p1: BoardPlayer, p2: BoardPlayer, round_info: str = "") -> io.BytesIO:
    """Both fighters locked in, battle resolving."""
    return render_board(
        state="active",
        p1=p1,
        p2=p2,
        status_text=round_info or "⚔️ BATTLE IN PROGRESS"
    )


def board_result(p1: BoardPlayer, p2: BoardPlayer, winner_asa: str) -> io.BytesIO:
    """Battle complete — highlight winner."""
    p1.is_winner = (p1.monstr_name == winner_asa or True if winner_asa == "draw" else False)
    p2.is_winner = (p2.monstr_name == winner_asa or True if winner_asa == "draw" else False)

    # Set properly
    p1.is_winner = winner_asa != "draw" and p1.monstr_name.split("#")[-1].strip() in winner_asa
    p2.is_winner = winner_asa != "draw" and p2.monstr_name.split("#")[-1].strip() in winner_asa

    status = "🏆 BATTLE COMPLETE" if winner_asa != "draw" else "⚔️ DRAW — Wagers Refunded"
    return render_board(state="result", p1=p1, p2=p2, status_text=status)
