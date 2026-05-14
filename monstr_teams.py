"""
monstr_teams.py
---------------
Battle Points (BP) accounting and tier resolution for the MONSTR Team system.

All BP reads and writes go through this module exclusively.

Tier system:
  🥚 Raw       —     0 BP — 1.00x atk, 0% stun resist
  🔰 Scrapper  —   150 BP — 1.05x atk, 10% stun resist
  ⚔️ Fighter   —   400 BP — 1.10x atk, 20% stun resist
  🔥 Veteran   —   900 BP — 1.18x atk, 32% stun resist
  💀 Warlord   — 2,000 BP — 1.28x atk, 45% stun resist

BP earned per encounter:
  +5   participated
  +15  dealt the kill shot (team's kill shot)
  +3   per critical hit landed
  +5   first encounter of the day bonus
  +10  killed a boss MONSTR
"""

import os
import random
import urllib.request
import json
from datetime import datetime, timezone, timedelta

from supabase import create_client, Client


# ─────────────────────────────────────────────
# TIER CONFIG
# ─────────────────────────────────────────────

TIERS = [
    # (tier_key, label, min_bp, atk_multiplier, stun_resist_pct)
    ("raw",      "🥚 Raw",      0,    1.00, 0),
    ("scrapper", "🔰 Scrapper", 150,  1.05, 10),
    ("fighter",  "⚔️ Fighter",  400,  1.10, 20),
    ("veteran",  "🔥 Veteran",  900,  1.18, 32),
    ("warlord",  "💀 Warlord",  2000, 1.28, 45),
]

# Next-tier thresholds keyed by current tier for progress display
TIER_KEYS = [t[0] for t in TIERS]


def resolve_tier(total_bp: int) -> tuple[str, str, float, int]:
    """
    Returns (tier_key, tier_label, atk_multiplier, stun_resist_pct)
    based on total BP.
    """
    resolved = TIERS[0]
    for tier in TIERS:
        if total_bp >= tier[2]:
            resolved = tier
    return resolved[0], resolved[1], resolved[3], resolved[4]


def next_tier_info(tier_key: str) -> tuple[str, int] | None:
    """
    Returns (next_tier_label, bp_required) or None if already max tier.
    """
    idx = TIER_KEYS.index(tier_key)
    if idx + 1 >= len(TIERS):
        return None
    next_t = TIERS[idx + 1]
    return next_t[1], next_t[2]


# ─────────────────────────────────────────────
# BP VALUES
# ─────────────────────────────────────────────

BP_PARTICIPATED    = 5
BP_KILL_SHOT       = 15
BP_CRIT            = 3
BP_DAILY_BONUS     = 5
BP_BOSS_KILL       = 10

# Holdings-based BP multiplier tiers
HOLDINGS_MULTIPLIERS = [
    (50, 2.00),
    (25, 1.75),
    (15, 1.50),
    (6,  1.25),
    (1,  1.00),
]

def get_holdings_multiplier(holdings: int) -> float:
    """Return BP multiplier based on number of MONSTRs held."""
    for threshold, mult in HOLDINGS_MULTIPLIERS:
        if holdings >= threshold:
            return mult
    return 1.0


# ─────────────────────────────────────────────
# DB HELPERS
# ─────────────────────────────────────────────

def _db() -> Client:
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def get_or_create_team(db, user_id: str) -> dict:
    row = db.table("monstr_teams").select("*").eq("user_id", user_id).execute()
    if row.data:
        return row.data[0]
    db.table("monstr_teams").insert({
        "user_id":   user_id,
        "total_bp":  0,
        "tier":      "raw",
    }).execute()
    return get_or_create_team(db, user_id)


def get_team(db, user_id: str) -> dict | None:
    row = db.table("monstr_teams").select("*").eq("user_id", user_id).execute()
    return row.data[0] if row.data else None


# ─────────────────────────────────────────────
# BP AWARD
# ─────────────────────────────────────────────

def award_bp(
    user_id: str,
    participated: bool,
    got_kill_shot: bool,
    crits: int,
    is_boss: bool,
    holdings: int = 0,
) -> tuple[int, str, str]:
    """
    Award BP to a team after an encounter.

    holdings: number of MONSTRs held — applies the holdings BP multiplier.
    Returns (bp_earned, old_tier_key, new_tier_key).
    Tier upgrade is detected so callers can announce it.
    """
    db = _db()
    team = get_or_create_team(db, user_id)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    last  = team.get("last_encounter_date")

    base_earned = 0
    if participated:
        base_earned += BP_PARTICIPATED
    if got_kill_shot:
        base_earned += BP_KILL_SHOT
    base_earned += crits * BP_CRIT
    if last != today:
        base_earned += BP_DAILY_BONUS          # first encounter today
    if is_boss and got_kill_shot:
        base_earned += BP_BOSS_KILL

    # Apply holdings multiplier
    mult   = get_holdings_multiplier(holdings)
    earned = int(base_earned * mult)

    # Streak
    if last == today:
        new_streak = team["streak_days"]
    elif last and datetime.strptime(last, "%Y-%m-%d").date() == (datetime.now(timezone.utc) - timedelta(days=1)).date():
        new_streak = team["streak_days"] + 1
    else:
        new_streak = 1

    new_bp   = team["total_bp"] + earned
    old_tier = team["tier"]
    new_tier, _, _, _ = resolve_tier(new_bp)

    db.table("monstr_teams").update({
        "total_bp":           new_bp,
        "tier":               new_tier,
        "encounters_played":  team["encounters_played"] + (1 if participated else 0),
        "encounters_won":     team["encounters_won"] + (1 if got_kill_shot else 0),
        "streak_days":        new_streak,
        "last_encounter_date": today,
        "updated_at":         datetime.now(timezone.utc).isoformat(),
    }).eq("user_id", user_id).execute()

    return earned, old_tier, new_tier


# ─────────────────────────────────────────────
# BATTLE MODIFIERS (called from encounter logic)
# ─────────────────────────────────────────────

def get_atk_multiplier(user_id: str) -> float:
    """Return attack multiplier for a player's team tier. Defaults to 1.0 on error."""
    try:
        db = _db()
        team = get_team(db, user_id)
        if not team:
            return 1.0
        bp = team.get("total_bp", 0)
        _, _, mult, _ = resolve_tier(bp)
        return mult
    except Exception as e:
        print(f"[TEAMS] get_atk_multiplier failed for {user_id}: {e}")
        return 1.0


def get_stun_resist(user_id: str) -> int:
    """Return stun resist % for a player's team. Defaults to 0 on error."""
    try:
        db = _db()
        team = get_team(db, user_id)
        if not team:
            return 0
        bp = team.get("total_bp", 0)
        _, _, _, resist = resolve_tier(bp)
        return resist
    except Exception as e:
        print(f"[TEAMS] get_stun_resist failed for {user_id}: {e}")
        return 0


def roll_stun_resist(user_id: str) -> bool:
    """
    Roll whether a stun is resisted.
    Returns True if the stun should be BLOCKED (resisted).
    """
    resist_pct = get_stun_resist(user_id)
    if resist_pct <= 0:
        return False
    return random.randint(1, 100) <= resist_pct


# ─────────────────────────────────────────────
# AVATAR — live image fetch
# ─────────────────────────────────────────────

IPFS_GATEWAYS = [
    "https://ipfs.algonode.xyz/ipfs/",
    "https://cloudflare-ipfs.com/ipfs/",
    "https://dweb.link/ipfs/",
]


def fetch_avatar_url(asa_id: str) -> str | None:
    """
    Fetch latest image URL for a given MONSTR ASA ID via Algorand indexer.
    Returns a gateway URL string, or None on failure.
    """
    try:
        indexer_url = os.getenv("INDEXER_URL", "https://mainnet-idx.algonode.cloud")
        url = f"{indexer_url}/v2/assets/{asa_id}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "X-Indexer-API-Token": os.getenv("INDEXER_TOKEN", ""),
        })
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())

        params = data.get("asset", {}).get("params", {})

        # ARC-19: reserve field holds IPFS CID
        reserve = params.get("reserve", "")
        url_field = params.get("url", "")

        # Try ARC-19 CID from reserve address
        if reserve:
            # Reserve encodes the CID as a base32 multihash for ARC-19
            # We use the url field which may have ipfs://... or template-ipfs://...
            pass

        # Try url field — may be ipfs://<hash> or https:// directly
        if url_field.startswith("ipfs://"):
            cid = url_field.replace("ipfs://", "").split("#")[0]
            return f"{IPFS_GATEWAYS[0]}{cid}"
        elif url_field.startswith("https://"):
            return url_field
        elif url_field.startswith("template-ipfs://"):
            # ARC-19: CID is encoded in reserve address as pubkey of a zero-balance account
            # Use algonode ARC19 endpoint as shortcut
            return f"https://ipfs.algonode.xyz/ipfs/{reserve}#arc3"

        print(f"[AVATAR] No recognisable image URL for ASA {asa_id}")
        return None

    except Exception as e:
        print(f"[AVATAR] Fetch failed for ASA {asa_id}: {e}")
        return None


# ─────────────────────────────────────────────
# HOLDINGS COUNT (live from chain)
# ─────────────────────────────────────────────

MONSTR_CREATOR_ADDRESS = os.getenv("MONSTR_CREATOR_ADDRESS", "")  # set in .env

def fetch_monstr_holdings(wallet_address: str) -> int:
    """
    Returns number of MONSTR NFTs held by wallet_address.
    Counts assets whose creator matches MONSTR_CREATOR_ADDRESS.
    Falls back to 0 on error.
    """
    try:
        indexer_url = os.getenv("INDEXER_URL", "https://mainnet-idx.algonode.cloud")
        url = f"{indexer_url}/v2/accounts/{wallet_address}/assets?include-all=false"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "X-Indexer-API-Token": os.getenv("INDEXER_TOKEN", ""),
        })
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())

        assets = data.get("assets", [])
        # Filter: amount > 0, and asset-id in MONSTR_ASSETS if we have no creator address
        from encounters import MONSTR_ASSETS
        monstr_ids = set(MONSTR_ASSETS.keys())
        count = sum(1 for a in assets if str(a.get("asset-id", "")) in monstr_ids and a.get("amount", 0) > 0)
        return count

    except Exception as e:
        print(f"[HOLDINGS] Fetch failed for {wallet_address[:8]}...: {e}")
        return 0
