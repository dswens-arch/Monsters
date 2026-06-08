"""
pvp_board_result.py
-------------------
MONSTRS PvP — Winner board compositor (landscape).

Template: battlegame_winner2.png — 4800x3584
Output:   1600x1194

Zones (template space):
  Winner ASA image (gold box):  x=539-1846   y=1069-2349
  Winner text (right side):     x=2000-4200  y=1069-2349
"""

import io, os, urllib.request
from typing import Optional
from dataclasses import dataclass
from PIL import Image, ImageDraw, ImageFont

RESULT_TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "battlegame_winner2.png")

OUTPUT_W = 1600
OUTPUT_H = 1194
SCALE    = OUTPUT_W / 4800
def _s(v): return int(v * SCALE)

WIN_IMG  = (_s(539),  _s(1069), _s(1846), _s(2349))
WIN_TEXT = (_s(2000), _s(1069), _s(4200), _s(2349))

WIN_IMG_W = WIN_IMG[2]  - WIN_IMG[0]
WIN_IMG_H = WIN_IMG[3]  - WIN_IMG[1]

COL_WHITE  = (255, 255, 255, 255)
COL_YELLOW = (255, 215, 50,  255)
COL_GREEN  = (80,  255, 130, 255)
COL_GREY   = (210, 210, 210, 255)

F_NAME = 58
F_USER = 42
F_INFO = 36


def _font(size):
    for p in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/root/.nix-profile/share/fonts/truetype/DejaVuSans-Bold.ttf",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "DejaVuSans-Bold.ttf"),
    ]:
        if os.path.exists(p):
            try: return ImageFont.truetype(p, size)
            except: pass
    return ImageFont.load_default()


def _t(draw, x, y, txt, font, color):
    """Drop shadow text."""
    draw.text((x+2, y+2), txt, font=font, fill=(0,0,0,255), anchor="lt")
    draw.text((x,   y),   txt, font=font, fill=color,       anchor="lt")


def _fetch(url, size):
    try:
        cid_path = None
        for prefix in ["https://ipfs.io/ipfs/", "https://dweb.link/ipfs/",
                        "https://ipfs.algonode.xyz/ipfs/", "https://gateway.pinata.cloud/ipfs/"]:
            if url.startswith(prefix):
                cid_path = url[len(prefix):]
                break
        if cid_path:
            cid_root = cid_path.split("/")[0]
            if cid_root.startswith("baf"):
                urls = [
                    f"https://ipfs.io/ipfs/{cid_path}",
                    f"https://nftstorage.link/ipfs/{cid_path}",
                    f"https://dweb.link/ipfs/{cid_path}",
                ]
            else:
                urls = [
                    f"https://dweb.link/ipfs/{cid_path}",
                    f"https://ipfs.io/ipfs/{cid_path}",
                    f"https://nftstorage.link/ipfs/{cid_path}",
                ]
        else:
            urls = [url]

        for u in urls:
            try:
                req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = r.read()
                img = Image.open(io.BytesIO(data)).convert("RGBA")
                img.thumbnail(size, Image.LANCZOS)
                c = Image.new("RGBA", size, (0,0,0,0))
                c.paste(img, ((size[0]-img.width)//2, (size[1]-img.height)//2), img)
                return c
            except Exception:
                continue
    except Exception as e:
        print(f"[PVP RESULT] NFT fetch failed: {e}")
    return None


@dataclass
class WinnerInfo:
    monstr_name:  str
    username:     str
    total_rounds: int
    wager_won:    int
    image_url:    Optional[str] = None
    is_draw:      bool = False
    is_algo:      bool = False


def render_result(winner: WinnerInfo) -> io.BytesIO:
    board = Image.open(RESULT_TEMPLATE_PATH).convert("RGBA")
    board = board.resize((OUTPUT_W, OUTPUT_H), Image.LANCZOS)

    # NFT image
    if winner.image_url and not winner.is_draw:
        nft = _fetch(winner.image_url, (WIN_IMG_W, WIN_IMG_H))
        if nft:
            board.paste(nft, (WIN_IMG[0], WIN_IMG[1]), nft)

    # Text
    draw = ImageDraw.Draw(board)
    x1, y1, x2, y2 = WIN_TEXT
    w = x2 - x1
    h = y2 - y1
    gap = 8

    fn = _font(F_NAME)
    fu = _font(F_USER)
    fi = _font(F_INFO)

    if winner.is_draw:
        _t(draw, x1, y1 + (h - F_USER)//2, "DRAW — Wagers Refunded", fu, COL_GREY)
    else:
        total = F_NAME + gap + F_USER + gap + 2 + gap + F_INFO + gap + F_INFO
        ty    = y1 + (h - total) // 2

        _t(draw, x1, ty, winner.monstr_name.upper(), fn, COL_WHITE)
        ty += F_NAME + gap

        _t(draw, x1, ty, f"@{winner.username}", fu, COL_YELLOW)
        ty += F_USER + gap

        draw.line([(x1, ty), (x2 - int(w*0.1), ty)], fill=(255,255,255,60), width=2)
        ty += gap + 2

        _t(draw, x1, ty, f"{winner.total_rounds} rounds", fi, COL_GREY)
        ty += F_INFO + gap

        if winner.is_algo:
            prize = f"+{winner.wager_won/1_000_000:g} ALGO"
        else:
            prize = f"+{winner.wager_won:,} $GOO"
        _t(draw, x1, ty, prize, fi, COL_GREEN)

    buf = io.BytesIO()
    board.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf
