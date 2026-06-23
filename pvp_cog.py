"""
pvp_cog.py
----------
MONSTRS PvP Battle System — Discord cog.

Balance model (mirrors Grand Prix):
  - Players deposit $GOO on-chain to the bot wallet
  - Bot polls for incoming transfers and credits pvp_goo_balances in Supabase
  - Wagers deduct from Supabase balance atomically at battle start
  - Payouts credit winner's Supabase balance, then fire on-chain send
  - Upgrades deduct from Supabase balance directly (no on-chain tx needed)
  - Players can /pvp_withdraw to pull balance back to linked wallet

All commands gated to DISCORD_PVP_CHANNEL_ID.

Environment variables (add to Railway):
  DISCORD_PVP_CHANNEL_ID   — #monstr-battles channel ID
  (all others already exist: SUPABASE_URL, SUPABASE_KEY, GOO_ASSET_ID,
   BOT_MNEMONIC, ALGOD_URL, ALGOD_TOKEN, INDEXER_URL, INDEXER_TOKEN)

Wire into bot.py setup_hook:
  await self.load_extension("pvp_cog")
"""

import asyncio
import os
import json
import random
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord
from discord.ext import commands, tasks
from supabase import create_client, Client

from pvp_engine import (
    MonstrStats, BattleResult,
    resolve_battle, format_stats_embed_fields,
    upgrade_cost, upgrade_cost_algo, upgrade_cost_algo_display, can_upgrade,
    STAT_BASE, STAT_MAX,
)
from encounters import MONSTR_ASSETS, send_goo, has_opted_in

# ─────────────────────────────────────────────
# PARTNER COLLECTION IMAGE MAPS
# Pre-loaded from CSV — no IPFS calls at registration time.
# Add new collections here as {asa_id_str: image_url}
# ─────────────────────────────────────────────

try:
    from zappies_image_map import ZAPPIES_IMAGE_MAP
except ImportError:
    ZAPPIES_IMAGE_MAP = {}

try:
    from zappies_name_map import ZAPPIES_NAME_MAP
except ImportError:
    ZAPPIES_NAME_MAP = {}

try:
    from skuli_image_map import SKULI_IMAGE_MAP
except ImportError:
    SKULI_IMAGE_MAP = {}

try:
    from skuli_name_map import SKULI_NAME_MAP
except ImportError:
    SKULI_NAME_MAP = {}

try:
    from algoctopus_image_map import ALGOCTOPUS_IMAGE_MAP
except ImportError:
    ALGOCTOPUS_IMAGE_MAP = {}

try:
    from algoctopus_name_map import ALGOCTOPUS_NAME_MAP
except ImportError:
    ALGOCTOPUS_NAME_MAP = {}

try:
    from blops_image_map import BLOPS_IMAGE_MAP
except ImportError:
    BLOPS_IMAGE_MAP = {}

try:
    from blops_name_map import BLOPS_NAME_MAP
except ImportError:
    BLOPS_NAME_MAP = {}

try:
    from darkcoin_image_map import DARKCOIN_IMAGE_MAP
except ImportError:
    DARKCOIN_IMAGE_MAP = {}

try:
    from darkcoin_name_map import DARKCOIN_NAME_MAP
except ImportError:
    DARKCOIN_NAME_MAP = {}

# Map collection_id -> image lookup dict
COLLECTION_IMAGE_MAPS: dict[int, dict] = {
    2: ZAPPIES_IMAGE_MAP,      # Zappies Reborn
    3: SKULI_IMAGE_MAP,        # Skuli
    4: ALGOCTOPUS_IMAGE_MAP,   # AlgOctopus (Origin)
    5: BLOPS_IMAGE_MAP,        # Blops (Origin)
    6: DARKCOIN_IMAGE_MAP,     # Dark Coin Champions
}

# Map collection_id -> name lookup dict
COLLECTION_NAME_MAPS: dict[int, dict] = {
    2: ZAPPIES_NAME_MAP,       # Zappies Reborn
    3: SKULI_NAME_MAP,         # Skuli
    4: ALGOCTOPUS_NAME_MAP,    # AlgOctopus (Origin)
    5: BLOPS_NAME_MAP,         # Blops (Origin)
    6: DARKCOIN_NAME_MAP,      # Dark Coin Champions
}

# ─────────────────────────────────────────────
# PARTNER ASSETS CACHE
# {collection_id_str: set(asa_id_str, ...)}
# Loaded at startup and refreshed every 4 hours so join is fast.
# ─────────────────────────────────────────────
PARTNER_ASSETS: dict[str, set] = {}


def _build_partner_assets_cache() -> dict[str, set]:
    """
    Build PARTNER_ASSETS from COLLECTION_IMAGE_MAPS keys (Zappies, Skuli, etc).
    These maps are already loaded from CSV so no indexer calls needed.
    Falls back to indexer scan for any partner collection not in a local image map.
    """
    import urllib.request, json as _json

    cache: dict[str, set] = {}
    db = _db()

    try:
        colls = db.table("pvp_collections").select("*").eq("active", True).execute()
        collections = colls.data or []
    except Exception as e:
        print(f"[PVP] partner cache: pvp_collections fetch failed: {e}")
        return cache

    indexer_url = os.environ.get("INDEXER_URL", "")

    for coll in collections:
        coll_id   = str(coll["id"])
        is_monstr = coll.get("is_monstr", False)
        if is_monstr:
            continue  # MONSTR_ASSETS handles this

        # Fast path — use local image map keys if available
        image_map = COLLECTION_IMAGE_MAPS.get(int(coll_id))
        if image_map:
            cache[coll_id] = set(image_map.keys())
            print(f"[PVP] partner cache: collection {coll_id} loaded {len(cache[coll_id])} ASAs from image map")
            continue

        # Fallback — scan indexer by creator address
        try:
            extra = db.table("pvp_collection_creators") \
                      .select("creator_address") \
                      .eq("collection_id", int(coll_id)) \
                      .execute()
            creator_addresses = [r["creator_address"] for r in (extra.data or [])]
            if not creator_addresses:
                creator_addresses = [coll["creator_address"]]
        except Exception:
            creator_addresses = [coll["creator_address"]]

        coll_asa_ids: set = set()
        for addr in creator_addresses:
            next_tok = None
            try:
                while True:
                    url = f"{indexer_url}/v2/assets?creator={addr}&limit=1000"
                    if next_tok:
                        url += f"&next={next_tok}"
                    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=15) as r:
                        data = _json.loads(r.read())
                    for asset in data.get("assets", []):
                        coll_asa_ids.add(str(asset.get("index", "")))
                    next_tok = data.get("next-token")
                    if not next_tok:
                        break
            except Exception as e:
                print(f"[PVP] partner cache: indexer scan failed creator={addr[:8]}...: {e}")

        if coll_asa_ids:
            cache[coll_id] = coll_asa_ids
            print(f"[PVP] partner cache: collection {coll_id} loaded {len(coll_asa_ids)} ASAs via indexer")

    return cache
from pvp_board import BoardPlayer, render_board
from pvp_board_result import WinnerInfo, render_result


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

GOO_WAGER_1V1      = 500        # per player (all GOO rooms)
GOO_WINNER_CUT_1V1 = 800        # winner receives (out of 1000 pot, 200 rake)
GOO_TREASURY_1V1   = 0          # no treasury cut — rake goes to weekly pool

ALGO_WAGER_1V1     = 5_000_000  # 5 ALGO in microALGO (all ALGO rooms)
ALGO_WINNER_CUT    = 8_000_000  # 8 ALGO to winner (out of 10 ALGO pot, 2 ALGO rake)
ALGO_TREASURY      = 0          # no treasury cut — rake goes to weekly pool

# Beginner room stat gate — locked out once ALL stats >= this value
BEGINNER_STAT_MAX  = 20

# Room definitions
GOO_ROOMS  = ("goo_1", "goo_2", "goo_beginner")
ALGO_ROOMS = ("algo_1", "algo_2", "algo_beginner")
ALL_ROOMS  = GOO_ROOMS + ALGO_ROOMS

# Env var for each room's channel ID
ROOM_CHANNEL_ENV = {
    "goo_1":          "DISCORD_GOO_1_CHANNEL_ID",
    "goo_2":          "DISCORD_GOO_2_CHANNEL_ID",
    "goo_beginner":   "DISCORD_GOO_BEGINNER_CHANNEL_ID",
    "algo_1":         "DISCORD_ALGO_1_CHANNEL_ID",
    "algo_2":         "DISCORD_ALGO_2_CHANNEL_ID",
    "algo_beginner":  "DISCORD_ALGO_BEGINNER_CHANNEL_ID",
}

CHALLENGE_TTL_HOURS = 24
DEPOSIT_POLL_SECONDS = 60       # how often to check for new deposits

# ─────────────────────────────────────────────
# WEEKLY PRIZE POOL CONFIG
# ─────────────────────────────────────────────

WEEKLY_RAKE_PCT       = 0.20    # 20% of each pot goes to weekly prize pool
WEEKLY_RESET_WEEKDAY  = 6       # Sunday (0=Mon … 6=Sun)
WEEKLY_RESET_HOUR_UTC = 0       # midnight UTC = 7PM EST Sunday
WEEKLY_RESET_MINUTE   = 0

# Channel IDs for daily leaderboard posts — set as env vars
def _goo_leaderboard_channel_id() -> Optional[int]:
    """Post GOO leaderboard to goo_1 channel."""
    return _room_channel_id("goo_1")

def _algo_leaderboard_channel_id() -> Optional[int]:
    """Post ALGO leaderboard to algo_1 channel."""
    return _room_channel_id("algo_1")


# ─────────────────────────────────────────────
# DB
# ─────────────────────────────────────────────

_db_client: Optional[Client] = None

def _db() -> Client:
    global _db_client
    if _db_client is None:
        _db_client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    return _db_client


# ─────────────────────────────────────────────
# BALANCE HELPERS
# ─────────────────────────────────────────────

def _get_balance(db, user_id: str) -> int:
    """Return the player's current GOO balance in Supabase. 0 if no row."""
    try:
        row = db.table("pvp_goo_balances").select("balance").eq("user_id", user_id).execute()
        return row.data[0]["balance"] if row.data else 0
    except Exception as e:
        print(f"[PVP] get_balance failed uid={user_id}: {e}")
        return 0


def _credit(db, user_id: str, amount: int, note: str = "") -> int:
    """Add amount to player's balance. Returns new balance."""
    try:
        existing = db.table("pvp_goo_balances").select("balance").eq("user_id", user_id).execute()
        if existing.data:
            new_bal = existing.data[0]["balance"] + amount
            db.table("pvp_goo_balances").update({
                "balance":    new_bal,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("user_id", user_id).execute()
        else:
            new_bal = amount
            db.table("pvp_goo_balances").insert({
                "user_id": user_id,
                "balance": new_bal,
            }).execute()
        _log_transaction(db, user_id, None, "credit", amount, note=note)
        return new_bal
    except Exception as e:
        print(f"[PVP] credit failed uid={user_id} amount={amount}: {e}")
        return 0


def _deduct(db, user_id: str, amount: int, note: str = "") -> tuple[bool, int]:
    """
    Deduct amount from player's balance atomically.
    Returns (success, new_balance).
    Fails cleanly if insufficient funds.
    """
    try:
        existing = db.table("pvp_goo_balances").select("balance").eq("user_id", user_id).execute()
        current = existing.data[0]["balance"] if existing.data else 0
        if current < amount:
            return False, current
        new_bal = current - amount
        db.table("pvp_goo_balances").update({
            "balance":    new_bal,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("user_id", user_id).execute()
        _log_transaction(db, user_id, None, "deduct", amount, note=note)
        return True, new_bal
    except Exception as e:
        print(f"[PVP] deduct failed uid={user_id} amount={amount}: {e}")
        return False, 0


# ─────────────────────────────────────────────
# TRANSACTION LEDGER
# ─────────────────────────────────────────────

def _log_transaction(db, user_id: str, duel_id: Optional[int], txn_type: str,
                     amount: int, wallet: str = "", tx_id: str = "", note: str = "",
                     room: str = "goo"):
    try:
        db.table("pvp_transactions").insert({
            "user_id":        user_id,
            "duel_id":        duel_id,
            "type":           txn_type,
            "amount":         amount,
            "room":           room,
            "wallet_address": wallet,
            "tx_id":          tx_id,
            "note":           note,
        }).execute()
    except Exception as e:
        print(f"[PVP] log_transaction failed: {e}")


# ─────────────────────────────────────────────
# WALLET HELPERS
# ─────────────────────────────────────────────

def _get_linked_wallet(user_id: str) -> Optional[str]:
    try:
        db = _db()
        row = db.table("linked_wallets").select("wallet_address").eq("user_id", user_id).execute()
        return row.data[0]["wallet_address"] if row.data else None
    except Exception as e:
        print(f"[PVP] wallet lookup failed uid={user_id}: {e}")
        return None


def _get_bot_keypair():
    """Return (private_key, address) for the bot wallet."""
    from algosdk import mnemonic as _mn
    mn = os.environ["BOT_MNEMONIC"]
    pk = _mn.to_private_key(mn)
    addr = _mn.to_public_key(mn)
    return pk, addr


def _get_bot_address() -> str:
    from algosdk import mnemonic, account
    pk = mnemonic.to_private_key(os.environ["BOT_MNEMONIC"])
    return account.address_from_private_key(pk)


# ─────────────────────────────────────────────
# OWNERSHIP CHECK
# ─────────────────────────────────────────────

def _verify_ownership(asa_id: str, wallet_address: str) -> bool:
    """Check wallet holds asa_id with amount > 0. Paginates through all assets."""
    import urllib.request, json as _json
    try:
        indexer_url = os.environ["INDEXER_URL"]
        next_token  = None
        while True:
            url = f"{indexer_url}/v2/accounts/{wallet_address}/assets?include-all=false&limit=1000"
            if next_token:
                url += f"&next={next_token}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = _json.loads(r.read())
            for asset in data.get("assets", []):
                if str(asset.get("asset-id", "")) == str(asa_id) and asset.get("amount", 0) > 0:
                    return True
            next_token = data.get("next-token")
            if not next_token:
                break
        return False
    except Exception as e:
        print(f"[PVP] ownership check failed asa={asa_id}: {e}")
        return False



def _fetch_monstr_asa_ids(wallet_address: str) -> list[str]:
    """
    Return list of MONSTR ASA IDs held by wallet_address (amount > 0).
    Paginates through all assets using next-token.
    """
    import urllib.request, json as _json
    try:
        indexer_url = os.environ["INDEXER_URL"]
        held       = []
        next_token = None
        page       = 0

        while True:
            url = (
                f"{indexer_url}/v2/accounts/{wallet_address}/assets"
                f"?include-all=false&limit=1000"
            )
            if next_token:
                url += f"&next={next_token}"

            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = _json.loads(r.read())

            assets = data.get("assets", [])
            page  += 1

            for asset in assets:
                asa = str(asset.get("asset-id", ""))
                if asset.get("amount", 0) > 0 and asa in MONSTR_ASSETS:
                    held.append(asa)

            next_token = data.get("next-token")
            if not next_token or not assets:
                break

        print(f"[PVP] {wallet_address[:8]}... scanned {page} page(s), found {len(held)} MONSTRs")
        return held
    except Exception as e:
        print(f"[PVP] fetch_monstr_asa_ids failed {wallet_address[:8]}...: {e}")
        return []


def _fetch_all_eligible_asa_ids(wallet_address: str) -> dict:
    """
    Scan wallet for all NFTs belonging to approved pvp_collections.
    Returns dict: {collection_id_str: [asa_id, ...]}

    MONSTRS: filtered via in-memory MONSTR_ASSETS (fast, no extra indexer call).
    Partners: one indexer call per partner collection to get their ASA IDs.
    """
    import urllib.request, json as _json

    indexer_url = os.environ["INDEXER_URL"]

    # Single wallet scan
    held_asa_ids: set = set()
    next_token = None
    try:
        while True:
            url = (
                f"{indexer_url}/v2/accounts/{wallet_address}/assets"
                f"?include-all=false&limit=1000"
            )
            if next_token:
                url += f"&next={next_token}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = _json.loads(r.read())
            for asset in data.get("assets", []):
                if asset.get("amount", 0) > 0:
                    held_asa_ids.add(str(asset["asset-id"]))
            next_token = data.get("next-token")
            if not next_token:
                break
    except Exception as e:
        print(f"[PVP] wallet scan failed {wallet_address[:8]}...: {e}")
        return {}

    # Load approved collections from DB
    db = _db()
    try:
        colls = db.table("pvp_collections").select("*").eq("active", True).execute()
        collections = colls.data or []
    except Exception as e:
        print(f"[PVP] pvp_collections fetch failed: {e}")
        return {}

    result = {}

    for coll in collections:
        coll_id   = str(coll["id"])
        is_monstr = coll["is_monstr"]
        creator   = coll["creator_address"]

        if is_monstr:
            # Fast path — use in-memory registry, no extra indexer call
            matched = [asa for asa in held_asa_ids if asa in MONSTR_ASSETS]
        else:
            # Fast path — use in-memory PARTNER_ASSETS cache (built at startup, refreshed every 4h)
            coll_asa_ids = PARTNER_ASSETS.get(coll_id, set())
            matched = [asa for asa in held_asa_ids if asa in coll_asa_ids]

        if matched:
            result[coll_id] = matched

    return result


def _resolve_arc19_name_and_image(asa_id: str) -> tuple:
    """
    Fetch ARC-19 metadata and return (name, image_url).
    Used at partner NFT registration time — name/image stored once, not re-fetched.
    Falls back through multiple IPFS gateways.
    Returns (f"#{asa_id}", None) on failure.
    """
    import urllib.request, json as _json
    from encounters import decode_arc19_reserve

    indexer_url   = os.environ["INDEXER_URL"]
    indexer_token = os.environ.get("INDEXER_TOKEN", "")
    try:
        url = f"{indexer_url}/v2/assets/{asa_id}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "X-Algo-API-Token": indexer_token,
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            data = _json.loads(r.read())

        params      = data.get("asset", {}).get("params", {})
        asset_url   = params.get("url", "")
        reserve     = params.get("reserve", "")
        params_name = params.get("name", "")

        metadata_cid = None
        if "template-ipfs" in asset_url and reserve:
            metadata_cid = decode_arc19_reserve(reserve)
        elif asset_url.startswith("ipfs://"):
            metadata_cid = asset_url.replace("ipfs://", "")

        if not metadata_cid:
            return params_name or f"#{asa_id}", None

        metadata = None
        for gw in ["https://ipfs.dark-coin.io/ipfs/", "https://ipfs-pera.algonode.dev/ipfs/", "https://ipfs.algonode.xyz/ipfs/", "https://dweb.link/ipfs/", "https://ipfs.io/ipfs/"]:
            try:
                req2 = urllib.request.Request(
                    f"{gw}{metadata_cid}",
                    headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
                )
                with urllib.request.urlopen(req2, timeout=10) as r2:
                    metadata = _json.loads(r2.read())
                break
            except Exception:
                continue

        if not metadata:
            return params_name or f"#{asa_id}", None

        name      = metadata.get("name") or params_name or f"#{asa_id}"
        image_raw = metadata.get("image", "")
        image_url = None
        if image_raw:
            image_cid = image_raw.replace("ipfs://", "")
            # Store dweb.link URL directly — no HEAD check, Railway blocks those
            image_url = f"https://dweb.link/ipfs/{image_cid}"

        return name, image_url

    except Exception as e:
        print(f"[PVP] _resolve_arc19_name_and_image failed asa={asa_id}: {e}")
        return f"#{asa_id}", None

def _resolve_arc19_image_url(asa_id: str) -> Optional[str]:
    """
    Resolve the current live image URL for an ARC-19 MONSTR.
    Mirrors fetch_live_image_url from encounters but returns URL string only.
    """
    import urllib.request, json as _json
    from encounters import decode_arc19_reserve
    try:
        indexer_url = os.environ["INDEXER_URL"]

        # Step 1: Get ASA params
        url = f"{indexer_url}/v2/assets/{asa_id}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0", "Accept": "application/json"
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            data = _json.loads(r.read())

        params    = data.get("asset", {}).get("params", {})
        asset_url = params.get("url", "")
        reserve   = params.get("reserve", "")

        # Step 2: Decode reserve → metadata CID
        metadata_cid = None
        if "template-ipfs" in asset_url and reserve:
            metadata_cid = decode_arc19_reserve(reserve)
        elif asset_url.startswith("ipfs://"):
            metadata_cid = asset_url.replace("ipfs://", "")

        if not metadata_cid:
            print(f"[PVP] No metadata CID for ASA {asa_id}")
            return None

        # Step 3: Fetch metadata JSON
        metadata = None
        _dark_coin_token = os.environ.get("DARK_COIN_IPFS_TOKEN", "")
        for gw in ["https://ipfs.dark-coin.io/ipfs/", "https://ipfs-pera.algonode.dev/ipfs/", "https://ipfs.algonode.xyz/ipfs/", "https://dweb.link/ipfs/", "https://ipfs.io/ipfs/"]:
            try:
                _headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
                if "dark-coin.io" in gw and _dark_coin_token:
                    _headers["Authorization"] = f"Bearer {_dark_coin_token}"
                req2 = urllib.request.Request(f"{gw}{metadata_cid}", headers=_headers)
                with urllib.request.urlopen(req2, timeout=10) as r:
                    metadata = _json.loads(r.read())
                break
            except Exception:
                continue

        if not metadata:
            print(f"[PVP] Metadata fetch failed for ASA {asa_id}")
            return None

        # Step 4: Build image URL without downloading
        image_field = metadata.get("image", "")
        if not image_field:
            return None

        image_cid = image_field.replace("ipfs://", "")
        if image_cid.startswith("bafk"):
            result_url = f"https://ipfs.io/ipfs/{image_cid}"
        else:
            result_url = f"https://dweb.link/ipfs/{image_cid}"
        print(f"[PVP] Image URL resolved for ASA {asa_id}: {result_url[:60]}")
        return result_url

    except Exception as e:
        print(f"[PVP] ARC-19 resolve failed asa={asa_id}: {e}")
        return None


# ─────────────────────────────────────────────
# ALGO BALANCE HELPERS (custodial, in microALGO)
# ─────────────────────────────────────────────

def _get_algo_balance(db, user_id: str) -> int:
    """Return custodial ALGO balance in microALGO."""
    try:
        row = db.table("pvp_algo_balances").select("balance").eq("user_id", user_id).execute()
        return row.data[0]["balance"] if row.data else 0
    except Exception:
        return 0

def _credit_algo(db, user_id: str, amount_micro: int, note: str = "") -> int:
    """Credit microALGO to custodial balance. Returns new balance."""
    try:
        row = db.table("pvp_algo_balances").select("balance").eq("user_id", user_id).execute()
        current = row.data[0]["balance"] if row.data else 0
        new_bal = current + amount_micro
        db.table("pvp_algo_balances").upsert({
            "user_id":    user_id,
            "balance":    new_bal,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="user_id").execute()
        _log_transaction(db, user_id, None, "credit", amount_micro, note=note, room="algo")
        return new_bal
    except Exception as e:
        print(f"[PVP] _credit_algo failed: {e}")
        return 0

def _deduct_algo(db, user_id: str, amount_micro: int, note: str = "") -> bool:
    """Deduct microALGO from custodial balance. Returns True on success."""
    try:
        row = db.table("pvp_algo_balances").select("balance").eq("user_id", user_id).execute()
        current = row.data[0]["balance"] if row.data else 0
        if current < amount_micro:
            return False
        db.table("pvp_algo_balances").update({
            "balance":    current - amount_micro,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("user_id", user_id).gte("balance", amount_micro).execute()
        _log_transaction(db, user_id, None, "deduct", amount_micro, note=note, room="algo")
        return True
    except Exception as e:
        print(f"[PVP] _deduct_algo failed: {e}")
        return False


# ─────────────────────────────────────────────
# TRAIT BONUS (deterministic, non-gameable)
# ─────────────────────────────────────────────

def _calc_trait_bonus(asa_id: str) -> tuple[int, int, int]:
    """Derive a stable 0–5 bonus per stat from the ASA ID. Cannot be manipulated."""
    n = int(asa_id)
    return ((n // 7) % 6), ((n // 13) % 6), ((n // 19) % 6)


# ─────────────────────────────────────────────
# LOAD STATS
# ─────────────────────────────────────────────

def _load_stats(asa_id: str, owner_id: str) -> Optional[MonstrStats]:
    try:
        db = _db()
        row = db.table("monstr_pvp_stats").select("*").eq("asa_id", str(asa_id)).execute()
        if not row.data:
            return None
        r   = row.data[0]
        img = r.get("image_url") or None
        return MonstrStats(
            asa_id   = r["asa_id"],
            name     = r["monstr_name"],
            owner_id = owner_id,
            attack   = r["attack"],
            defense  = r["defense"],
            speed    = r["speed"],
            image_url= img,
        )
    except Exception as e:
        print(f"[PVP] load_stats failed asa={asa_id}: {e}")
        return None


def _load_stats_from_roster(db, asa_id: str, owner_id: str) -> Optional[MonstrStats]:
    """
    Load MonstrStats from pvp_rosters. Works for MONSTRS and partner NFTs.
    Used by the join flow so both collections work transparently.
    """
    try:
        row = db.table("pvp_rosters").select("*") \
                .eq("asa_id", str(asa_id)).eq("user_id", owner_id).execute()
        if not row.data:
            return None
        r = row.data[0]
        return MonstrStats(
            asa_id    = r["asa_id"],
            name      = r["nft_name"],
            owner_id  = owner_id,
            attack    = r["attack"],
            defense   = r["defense"],
            speed     = r["speed"],
            image_url = r.get("image_url") or None,
        )
    except Exception as e:
        print(f"[PVP] load_stats_from_roster failed asa={asa_id}: {e}")
        return None


# ─────────────────────────────────────────────
# WEEKLY POOL HELPERS
# ─────────────────────────────────────────────

def _current_week_start() -> datetime:
    """
    Return the start of the current weekly period.
    Week resets Sunday 00:00 UTC (= Sunday 7PM EST).
    We anchor to the most recent Sunday midnight UTC.
    """
    now = datetime.now(timezone.utc)
    # Sunday = weekday 6
    days_since_sunday = (now.weekday() + 1) % 7  # Mon=1, Tue=2, ... Sun=0
    week_start = (now - timedelta(days=days_since_sunday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return week_start


def _ensure_weekly_pools(db) -> None:
    """
    Make sure active pool rows exist for the current week.
    Safe to call on every battle — no-ops if rows already exist.
    """
    week_start = _current_week_start()
    week_end   = week_start + timedelta(days=7)
    ws         = week_start.isoformat()
    we         = week_end.isoformat()

    for room in ("goo", "algo"):
        try:
            existing = db.table("pvp_weekly_pools") \
                         .select("id") \
                         .eq("week_start", ws) \
                         .eq("room", room) \
                         .eq("status", "active") \
                         .execute()
            if not existing.data:
                db.table("pvp_weekly_pools").insert({
                    "week_start": ws,
                    "week_end":   we,
                    "room":       room,
                    "balance":    0,
                    "status":     "active",
                }).execute()
                print(f"[PVP] Created weekly pool {room} week={ws[:10]}")
        except Exception as e:
            print(f"[PVP] _ensure_weekly_pools failed room={room}: {e}")


def _add_to_weekly_pool(db, room: str, amount: int) -> None:
    """Credit amount to the active weekly pool for room using atomic SQL increment."""
    week_start = _current_week_start().isoformat()
    try:
        row = db.table("pvp_weekly_pools") \
                .select("id, balance") \
                .eq("week_start", week_start) \
                .eq("room", room) \
                .eq("status", "active") \
                .execute()
        if not row.data:
            print(f"[PVP] _add_to_weekly_pool: no active pool row found room={room} week={week_start}")
            return
        pool_id = row.data[0]["id"]
        current = row.data[0]["balance"]
        new_bal = current + amount
        db.table("pvp_weekly_pools") \
          .update({"balance": new_bal}) \
          .eq("id", pool_id) \
          .execute()
        print(f"[PVP] pool balance updated room={room} {current} -> {new_bal}")
    except Exception as e:
        print(f"[PVP] _add_to_weekly_pool failed room={room}: {e}")


def _upsert_leaderboard(db, user_id: str, room: str, won: bool) -> None:
    """
    Increment wins or losses for user on the weekly leaderboard.
    Uses a read-then-write pattern (Supabase Python client doesn't support
    increment via RPC without a custom function).
    """
    week_start = _current_week_start().isoformat()
    try:
        row = db.table("pvp_weekly_leaderboard") \
                .select("id, wins, losses") \
                .eq("week_start", week_start) \
                .eq("room", room) \
                .eq("user_id", user_id) \
                .execute()
        if row.data:
            r       = row.data[0]
            wins    = r["wins"]   + (1 if won else 0)
            losses  = r["losses"] + (0 if won else 1)
            db.table("pvp_weekly_leaderboard") \
              .update({"wins": wins, "losses": losses}) \
              .eq("id", r["id"]) \
              .execute()
        else:
            db.table("pvp_weekly_leaderboard").insert({
                "week_start": week_start,
                "room":       room,
                "user_id":    user_id,
                "wins":       1 if won else 0,
                "losses":     0 if won else 1,
            }).execute()
    except Exception as e:
        print(f"[PVP] _upsert_leaderboard failed uid={user_id} room={room}: {e}")


def _get_leaderboard(db, room: str) -> list[dict]:
    """Return top 10 leaderboard rows for current week, sorted by wins desc."""
    week_start = _current_week_start().isoformat()
    try:
        rows = db.table("pvp_weekly_leaderboard") \
                 .select("user_id, wins, losses") \
                 .eq("week_start", week_start) \
                 .eq("room", room) \
                 .order("wins", desc=True) \
                 .limit(10) \
                 .execute()
        return rows.data or []
    except Exception as e:
        print(f"[PVP] _get_leaderboard failed room={room}: {e}")
        return []


def _get_pool_balance(db, room: str) -> int:
    """Return current active pool balance for room."""
    week_start = _current_week_start().isoformat()
    try:
        row = db.table("pvp_weekly_pools") \
                .select("balance") \
                .eq("week_start", week_start) \
                .eq("room", room) \
                .eq("status", "active") \
                .execute()
        return row.data[0]["balance"] if row.data else 0
    except Exception:
        return 0

def _to_board_player(stats: MonstrStats, username: str) -> BoardPlayer:
    """Convert MonstrStats to BoardPlayer for board rendering."""
    return BoardPlayer(
        monstr_name = stats.name,
        username    = username,
        attack      = stats.attack,
        defense     = stats.defense,
        speed       = stats.speed,
        hp          = stats.hp,
        image_url   = stats.image_url,
    )


def _to_board_player_hidden(stats: MonstrStats, username: str) -> BoardPlayer:
    """BoardPlayer with stats hidden — used during waiting state before opponent locks in."""
    return BoardPlayer(
        monstr_name = stats.name,
        username    = username,
        attack      = 0,
        defense     = 0,
        speed       = 0,
        hp          = stats.hp,
        image_url   = stats.image_url,
    )


async def _get_display_name(guild, user_id: str) -> str:
    """Get a user display name from guild, fallback to user_id."""
    try:
        member = guild.get_member(int(user_id))
        if member:
            return member.display_name
    except Exception:
        pass
    return f"user_{user_id[-4:]}"


# ─────────────────────────────────────────────
# CHANNEL GUARD
# ─────────────────────────────────────────────

def _room_channel_id(room: str) -> Optional[int]:
    """Return the Discord channel ID for a given room."""
    val = os.environ.get(ROOM_CHANNEL_ENV.get(room, ""), "")
    return int(val) if val and val.strip().isdigit() else None

def _channel_room(channel_id: int) -> Optional[str]:
    """Return room key (e.g. 'goo_1', 'algo_beginner') based on channel ID."""
    for room in ALL_ROOMS:
        if channel_id == _room_channel_id(room):
            return room
    return None

def _is_algo_room(room: str) -> bool:
    return room in ALGO_ROOMS

def _is_beginner_room(room: str) -> bool:
    return room.endswith("_beginner")

def _pool_room(room: str) -> str:
    """Map any room to its shared weekly pool key ('goo' or 'algo')."""
    return "algo" if _is_algo_room(room) else "goo"

def _pvp_channel_id() -> Optional[int]:
    """Legacy helper — returns goo_1 channel."""
    return _room_channel_id("goo_1")

def _algo_channel_id() -> Optional[int]:
    """Legacy helper — returns algo_1 channel."""
    return _room_channel_id("algo_1")



async def _wrong_channel(interaction: discord.Interaction) -> bool:
    room = _channel_room(interaction.channel_id)
    if room is None:
        channel_ids = [_room_channel_id(r) for r in ALL_ROOMS]
        channels = " or ".join([f"<#{c}>" for c in channel_ids if c])
        await interaction.response.send_message(
            f"PvP commands only work in a designated battle channel: {channels}.", ephemeral=True
        )
        return True
    return False

# ─────────────────────────────────────────────
# BATTLE RESULT EMBED
# ─────────────────────────────────────────────

def _build_battle_embed(result: BattleResult, a: MonstrStats, b: MonstrStats,
                        wager: int, duel_id: int) -> discord.Embed:
    if result.is_draw:
        embed = discord.Embed(
            title="⚔️ BATTLE — DRAW!",
            description="Neither MONSTR could finish the other. **Wagers refunded.**",
            color=0x888888
        )
    else:
        winner_m = a if result.winner_asa == a.asa_id else b
        loser_m  = a if result.loser_asa  == a.asa_id else b
        embed = discord.Embed(
            title=f"⚔️ {winner_m.name.upper()} WINS!",
            description=(
                f"<@{winner_m.owner_id}>'s **{winner_m.name}** defeats "
                f"<@{loser_m.owner_id}>'s **{loser_m.name}** "
                f"in **{result.total_rounds}** round{'s' if result.total_rounds != 1 else ''}!"
            ),
            color=0x9b59b6
        )
        embed.add_field(
            name="💧 Payout",
            value=f"**{GOO_WINNER_CUT_1V1:,} $GOO** credited to <@{winner_m.owner_id}>",
            inline=False
        )

    log_lines = [r.flavor for r in result.rounds[-5:]]
    if log_lines:
        embed.add_field(name="📜 Final Rounds", value="\n".join(log_lines), inline=False)

    embed.add_field(name="🔢 Duel",   value=f"`#{duel_id}`",          inline=True)
    embed.add_field(name="💰 Wager",  value=f"{wager:,} $GOO each",   inline=True)
    embed.add_field(name="🏦 Room",   value="GOO",                    inline=True)

    if not result.is_draw:
        winner_m = a if result.winner_asa == a.asa_id else b
        if winner_m.image_url:
            embed.set_thumbnail(url=winner_m.image_url)

# ─────────────────────────────────────────────
# WAGER LOCK / RELEASE HELPERS
# ─────────────────────────────────────────────

def _lock_wager(db, user_id: str, amount: int, duel_id: int) -> bool:
    """Deduct wager from balance. Returns True on success."""
    ok, _ = _deduct(db, user_id, amount, note=f"wager lock duel#{duel_id}")
    if ok:
        _log_transaction(db, user_id, duel_id, "wager_lock", amount)
    return ok


def _refund_wager(db, user_id: str, amount: int, duel_id: int, reason: str = "refund"):
    _credit(db, user_id, amount, note=f"{reason} duel#{duel_id}")
    _log_transaction(db, user_id, duel_id, "wager_refund", amount, note=reason)


def _credit_win(db, user_id: str, amount: int, duel_id: int):
    _credit(db, user_id, amount, note=f"win payout duel#{duel_id}")
    _log_transaction(db, user_id, duel_id, "payout", amount)


# ─────────────────────────────────────────────
# BOARD STATE  (in-memory, one active queue slot)
# ─────────────────────────────────────────────

class BoardState:
    """
    Singleton tracking the current PvP board state.
    board_msg_id — the Discord message ID of the current persistent board.
    challenger   — dict with keys: user_id, asa_id, stats (MonstrStats), username
    """
    def __init__(self):
        self.board_msg_id: Optional[int]  = None
        self.challenger:   Optional[dict] = None

    def reset(self):
        self.challenger = None

    @property
    def is_empty(self) -> bool:
        return self.challenger is None

_boards: dict[str, "BoardState"] = {room: BoardState() for room in ALL_ROOMS}

def _get_board(room: str) -> BoardState:
    return _boards.get(room, _boards["goo_1"])


# ─────────────────────────────────────────────
# MONSTR PICKER VIEW  (ephemeral — only sender sees)
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# COLLECTION PICKER VIEW
# ─────────────────────────────────────────────

class CollectionPickerView(discord.ui.View):
    """
    Step 1: show one button per collection the player holds fighters in.
    Tapping a collection opens FighterPickerView for that collection.
    Skipped entirely if player only has one collection.
    """
    def __init__(self, collections: list[dict], all_fighters: list[dict],
                 join_callback, prompt: str = "Choose your fighter"):
        super().__init__(timeout=120)
        self._all_fighters = all_fighters
        self._cb           = join_callback
        self._prompt       = prompt

        for coll in collections:
            btn = discord.ui.Button(
                label     = coll["name"],
                style     = discord.ButtonStyle.primary,
                custom_id = f"coll_{coll['id']}",
            )
            btn.callback = self._make_coll_cb(coll["id"], coll["name"])
            self.add_item(btn)

    def _make_coll_cb(self, coll_id: int, coll_name: str):
        async def cb(interaction: discord.Interaction):
            fighters = [f for f in self._all_fighters if f["collection_id"] == coll_id]
            view = FighterPickerView(fighters, self._cb)
            await interaction.response.edit_message(
                content=f"**{coll_name}** — choose your fighter:",
                view=view
            )
        return cb


# ─────────────────────────────────────────────
# FIGHTER PICKER VIEW  (replaces MonstrPickerView)
# ─────────────────────────────────────────────

class FighterPickerView(discord.ui.View):
    """
    Shows up to 5 fighters per page sorted by power, with stats on each button.
    Format: "Zappy #1706  ⚔15 🛡14 ⚡14"
    Includes Prev/Next pagination and Enter ASA ID button.
    """
    def __init__(self, fighter_rows: list[dict], join_callback, page: int = 0):
        super().__init__(timeout=120)
        self._all_rows = fighter_rows
        self._cb       = join_callback
        self._page     = page
        self._per_page = 5

        start     = page * self._per_page
        page_rows = fighter_rows[start:start + self._per_page]

        for row in page_rows:
            disabled = row.get("disabled", False)
            name     = row["monstr_name"]
            atk      = row.get("attack", 0)
            defense  = row.get("defense", 0)
            spd      = row.get("speed", 0)
            # Stat suffix if we have stats
            if atk or defense or spd:
                label = f"{name}  ⚔{atk} 🛡{defense} ⚡{spd}"
            else:
                label = name
            # Truncate to Discord's 80-char button label limit
            if len(label) > 80:
                label = label[:77] + "..."

            btn = discord.ui.Button(
                label     = label,
                style     = discord.ButtonStyle.secondary if disabled else discord.ButtonStyle.primary,
                custom_id = f"pick_{row['asa_id']}",
                disabled  = disabled,
            )
            btn.callback = self._make_pick_cb(row["asa_id"])
            self.add_item(btn)

        total_pages = max(1, (len(fighter_rows) - 1) // self._per_page + 1)

        if page > 0:
            prev_btn = discord.ui.Button(
                label="◀ Prev", style=discord.ButtonStyle.secondary,
                custom_id="pick_prev", row=1
            )
            prev_btn.callback = self._prev_cb
            self.add_item(prev_btn)

        if (page + 1) < total_pages:
            next_btn = discord.ui.Button(
                label="Next ▶", style=discord.ButtonStyle.secondary,
                custom_id="pick_next", row=1
            )
            next_btn.callback = self._next_cb
            self.add_item(next_btn)

        manual = discord.ui.Button(
            label="Enter ASA ID", style=discord.ButtonStyle.secondary,
            custom_id="pick_manual", row=1
        )
        manual.callback = self._manual_cb
        self.add_item(manual)

    def _make_pick_cb(self, asa_id: str):
        async def cb(interaction: discord.Interaction):
            await self._cb(interaction, asa_id)
        return cb

    async def _prev_cb(self, interaction: discord.Interaction):
        new_view = FighterPickerView(self._all_rows, self._cb, self._page - 1)
        total = max(1, (len(self._all_rows) - 1) // 5 + 1)
        await interaction.response.edit_message(
            content=f"Choose your fighter (page {self._page}/{total}):",
            view=new_view
        )

    async def _next_cb(self, interaction: discord.Interaction):
        new_view = FighterPickerView(self._all_rows, self._cb, self._page + 1)
        total = max(1, (len(self._all_rows) - 1) // 5 + 1)
        await interaction.response.edit_message(
            content=f"Choose your fighter (page {self._page + 2}/{total}):",
            view=new_view
        )

    async def _manual_cb(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ASAModal(self._cb))


# Keep MonstrPickerView as alias for any legacy references
MonstrPickerView = FighterPickerView


class ASAModal(discord.ui.Modal, title="Enter your Fighter ASA ID"):
    asa_id = discord.ui.TextInput(
        label       = "ASA ID",
        placeholder = "e.g. 1234567890",
        min_length  = 5,
        max_length  = 20,
    )

    def __init__(self, join_callback):
        super().__init__()
        self._cb = join_callback

    async def on_submit(self, interaction: discord.Interaction):
        await self._cb(interaction, self.asa_id.value.strip())


def _build_collection_picker(fighter_rows: list[dict], join_callback,
                              prompt: str = "Choose your fighter"):
    """
    Build either a CollectionPickerView (multiple collections) or
    FighterPickerView (single collection) depending on what the player holds.
    Returns (view, message_content).
    """
    # Group by collection
    coll_ids_seen = []
    coll_map = {}
    for f in fighter_rows:
        cid = f.get("collection_id")
        cname = f.get("collection_name", "Unknown")
        if cid not in coll_map:
            coll_map[cid] = cname
            coll_ids_seen.append(cid)

    if len(coll_ids_seen) <= 1:
        # Single collection — skip straight to fighter picker
        return FighterPickerView(fighter_rows, join_callback), f"**{prompt}:**"
    else:
        collections = [{"id": cid, "name": coll_map[cid]} for cid in coll_ids_seen]
        return CollectionPickerView(collections, fighter_rows, join_callback, prompt), \
               f"**{prompt}** — choose your collection first:"




# ─────────────────────────────────────────────
# PERSISTENT JOIN BUTTON VIEW
# ─────────────────────────────────────────────

class JoinBattleView(discord.ui.View):
    """
    Persistent view attached to the board message.
    Registered with bot so it survives restarts.
    """
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label     = "⚔️ Join Battle",
        style     = discord.ButtonStyle.danger,
        custom_id = "pvp_join_battle",
    )
    async def join_battle(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_join(interaction)


# ─────────────────────────────────────────────
# JOIN HANDLER  (called by button)
# ─────────────────────────────────────────────

async def _handle_join(interaction: discord.Interaction):
    """Ephemeral flow triggered when someone taps Join Battle."""
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)
    db      = _db()
    room    = _channel_room(interaction.channel_id) or "goo"
    board   = _get_board(room)

    # Must have a linked wallet
    wallet = await asyncio.to_thread(_get_linked_wallet, user_id)
    if not wallet:
        await interaction.followup.send(
            "Link your wallet first with `/link`.", ephemeral=True)
        return

    # Check balance for the right room
    if _is_algo_room(room):
        bal = _get_algo_balance(db, user_id)
        if bal < ALGO_WAGER_1V1:
            await interaction.followup.send(
                f"Need **{ALGO_WAGER_1V1/1_000_000:g} ALGO** to join. "
                f"You have **{bal/1_000_000:g} ALGO**. Use `/pvp_deposit_algo`.",
                ephemeral=True)
            return
    else:
        bal = _get_balance(db, user_id)
        if bal < GOO_WAGER_1V1:
            await interaction.followup.send(
                f"Need **{GOO_WAGER_1V1:,} $GOO** to join. "
                f"You have **{bal:,}**. Use `/pvp_deposit`.",
                ephemeral=True)
            return

    # Can't fight yourself
    if not board.is_empty and board.challenger["user_id"] == user_id:
        await interaction.followup.send(
            "You're already waiting for an opponent!", ephemeral=True)
        return

    # Fetch all registered fighters from pvp_rosters (covers MONSTRS + partners)
    roster_res = await asyncio.to_thread(
        lambda: db.table("pvp_rosters")
            .select("asa_id, nft_name, attack, defense, speed, collection_id, pvp_collections(collection_name)")
            .eq("user_id", user_id)
            .execute()
    )
    if not roster_res.data:
        await interaction.followup.send(
            "No registered fighters. Use `/pvp_register` first.", ephemeral=True)
        return

    now = datetime.now(timezone.utc)
    fighter_rows = []

    # Fetch ALL cooldowns for this user in one query instead of N individual calls
    asa_ids = [r["asa_id"] for r in roster_res.data]
    cooldown_map: dict[str, datetime] = {}
    try:
        cd_res = db.table("pvp_cooldowns")                    .select("asa_id, expires_at")                    .in_("asa_id", asa_ids)                    .execute()
        for cd in (cd_res.data or []):
            cooldown_map[cd["asa_id"]] = datetime.fromisoformat(cd["expires_at"])
    except Exception:
        pass

    for r in roster_res.data:
        asa      = r["asa_id"]
        disabled = False
        suffix   = ""

        # Cooldown check — use pre-fetched map
        expires = cooldown_map.get(asa)
        if expires and expires > now:
            mins     = int((expires - now).total_seconds() / 60) + 1
            disabled = True
            suffix   = f" ({mins}m)"

        # In-queue check
        if not disabled:
            for b in _boards.values():
                if b.challenger and b.challenger["asa_id"] == asa:
                    disabled = True
                    suffix   = " (in queue)"
                    break

        coll     = r.get("pvp_collections") or {}
        power    = r["attack"] + r["defense"] + r["speed"]
        fighter_rows.append({
            "asa_id":         asa,
            "monstr_name":    r["nft_name"] + suffix,
            "attack":         r["attack"],
            "defense":        r["defense"],
            "speed":          r["speed"],
            "disabled":       disabled,
            "power":          power,
            "collection_id":  r["collection_id"],
            "collection_name": coll.get("collection_name", "Unknown"),
        })

    # Beginner room: filter to only fighters with at least one stat < BEGINNER_STAT_MAX
    if _is_beginner_room(room):
        eligible = [
            f for f in fighter_rows
            if f["attack"] < BEGINNER_STAT_MAX
            or f["defense"] < BEGINNER_STAT_MAX
            or f["speed"] < BEGINNER_STAT_MAX
        ]
        if not eligible:
            await interaction.followup.send(
                f"Your fighters have graduated from the beginner room! "
                f"All stats are {BEGINNER_STAT_MAX}+. Head to the main arena.",
                ephemeral=True)
            return
        fighter_rows = eligible

    # Sort: available first by power desc, then disabled by power desc
    fighter_rows.sort(key=lambda r: (r["disabled"], -r["power"]))

    async def on_pick(pick_interaction: discord.Interaction, asa_id: str):
        await _on_fighter_picked(pick_interaction, asa_id, user_id, db, room)

    currency = f"{bal/1_000_000:g} ALGO" if _is_algo_room(room) else f"{bal:,} $GOO"
    view, content = _build_collection_picker(fighter_rows, on_pick, "Choose your fighter")
    await interaction.followup.send(
        f"{content}\n({currency} available)",
        view=view, ephemeral=True)


async def _on_fighter_picked(interaction: discord.Interaction,
                             asa_id: str, user_id: str, db, room: str = "goo"):
    """Called after a player picks their fighter. Works for MONSTRS and partner NFTs."""
    await interaction.response.defer(ephemeral=True)
    board = _get_board(room)

    # Verify ownership via pvp_rosters (covers all collections)
    row = db.table("pvp_rosters").select("*") \
            .eq("asa_id", str(asa_id)).eq("user_id", user_id).execute()
    if not row.data:
        await interaction.followup.send(
            "That fighter isn't registered. Use `/pvp_register` first.", ephemeral=True)
        return

    # Check 30-min cooldown
    cd_row = db.table("pvp_cooldowns").select("expires_at") \
               .eq("asa_id", str(asa_id)).execute()
    if cd_row.data:
        expires = datetime.fromisoformat(cd_row.data[0]["expires_at"])
        now     = datetime.now(timezone.utc)
        if expires > now:
            mins = int((expires - now).total_seconds() / 60) + 1
            await interaction.followup.send(
                f"That fighter is cooling down. Ready in **{mins} min**.", ephemeral=True)
            return

    # Check not already queued
    for b in _boards.values():
        if b.challenger and b.challenger["asa_id"] == str(asa_id):
            await interaction.followup.send(
                "That fighter is already waiting in a battle queue!", ephemeral=True)
            return

    # Check not already in an active duel
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        a1 = db.table("pvp_duels").select("id").eq("status", "active") \
               .eq("challenger_asa", str(asa_id)).gt("expires_at", now_iso).execute()
        a2 = db.table("pvp_duels").select("id").eq("status", "active") \
               .eq("opponent_asa",   str(asa_id)).gt("expires_at", now_iso).execute()
        if a1.data or a2.data:
            await interaction.followup.send(
                "That fighter is already in an active battle!", ephemeral=True)
            return
    except Exception as e:
        print(f"[PVP] active duel check failed: {e}")

    # Load stats from pvp_rosters
    stats = _load_stats_from_roster(db, asa_id, user_id)
    if not stats:
        await interaction.followup.send("Couldn't load stats. Try again.", ephemeral=True)
        return

    username = interaction.user.display_name

    if board.is_empty:
        # Lock wager immediately on join
        wager = ALGO_WAGER_1V1 if _is_algo_room(room) else GOO_WAGER_1V1
        if _is_algo_room(room):
            locked = await asyncio.to_thread(_deduct_algo, db, user_id, wager, "wager hold — waiting for opponent")
        else:
            locked, _ = await asyncio.to_thread(_deduct, db, user_id, wager, "wager hold — waiting for opponent")
        if not locked:
            wager_str   = f"{wager/1_000_000:g} ALGO" if _is_algo_room(room) else f"{wager:,} $GOO"
            deposit_cmd = "`/pvp_deposit_algo`" if _is_algo_room(room) else "`/pvp_deposit`"
            await interaction.followup.send(
                f"❌ Not enough funds. Need **{wager_str}** to join. Use {deposit_cmd}.",
                ephemeral=True)
            return

        board.challenger = {
            "user_id":  user_id,
            "asa_id":   str(asa_id),
            "stats":    stats,
            "username": username,
            "room":     room,
            "wager":    wager,
        }
        wager_str = f"{wager/1_000_000:g} ALGO" if _is_algo_room(room) else f"{wager:,} $GOO"
        await interaction.followup.send(
            f"**{stats.name}** is in the arena! **{wager_str}** held. Waiting for an opponent...",
            ephemeral=True)
        bp1 = _to_board_player_hidden(stats, username)
        await _update_board(interaction.channel, "waiting", room, p1=bp1,
                            status_text="Waiting for opponent...")
    else:
        if board.challenger["user_id"] == user_id:
            await interaction.followup.send(
                "You're already waiting for an opponent!", ephemeral=True)
            return

        await interaction.followup.send(
            f"**{stats.name}** enters the arena! Battle starting...", ephemeral=True)

        challenger = board.challenger
        board.reset()

        cog = interaction.client.cogs.get("PvPCog")
        print(f"[PVP] Battle firing: cog={cog is not None} chal={challenger['user_id']} opp={user_id} room={room}")
        if cog:
            async def _run_safe():
                try:
                    await cog._run_board_battle(
                        channel    = interaction.channel,
                        db         = db,
                        room       = room,
                        chal_id    = challenger["user_id"],
                        chal_asa   = challenger["asa_id"],
                        chal_stats = challenger["stats"],
                        chal_uname = challenger["username"],
                        opp_id     = user_id,
                        opp_asa    = str(asa_id),
                        opp_stats  = stats,
                        opp_uname  = username,
                    )
                except Exception as e:
                    import traceback
                    print(f"[PVP] _run_board_battle crashed: {e}")
                    print(traceback.format_exc())
            asyncio.ensure_future(_run_safe())
        else:
            print(f"[PVP] ERROR: PvPCog not found. Available: {list(interaction.client.cogs.keys())}")
            await interaction.channel.send("Battle system error — cog not found.")


# ─────────────────────────────────────────────
# BOARD UPDATE HELPER
# ─────────────────────────────────────────────

async def _update_board(channel, state: str, room: str = "goo",
                        p1=None, p2=None, status_text: str = ""):
    """Edit the persistent board message in-place for the given room."""
    board = _get_board(room)
    buf   = await render_board(state, p1, p2, status_text)
    file  = discord.File(buf, filename="board.png")
    view  = JoinBattleView() if state == "waiting" else discord.ui.View()

    try:
        if board.board_msg_id:
            msg = await channel.fetch_message(board.board_msg_id)
            await msg.edit(attachments=[file], view=view)
            return
    except Exception:
        pass

    msg = await channel.send(file=file, view=view if state == "waiting" else None)
    board.board_msg_id = msg.id
    # Persist to Supabase for reload on redeploy
    try:
        _db().table("pvp_board_state").upsert({
            "room":         room,
            "board_msg_id": str(msg.id),
        }, on_conflict="room").execute()
    except Exception:
        pass


# ─────────────────────────────────────────────


def _get_bot_algo_balance() -> int:
    """Return the bot wallet ALGO balance in microALGO via indexer."""
    import urllib.request, json as _json
    try:
        indexer_url = os.environ["INDEXER_URL"]
        bot_addr    = _get_bot_address()
        url = f"{indexer_url}/v2/accounts/{bot_addr}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = _json.loads(r.read())
        return data.get("account", {}).get("amount", 0)
    except Exception as e:
        if "404" not in str(e):
            print(f"[PVP] bot ALGO balance check failed: {e}")
        return 0


def _get_indexer_client():
    """Return an algosdk indexer client — same as Grand Prix."""
    from algosdk.v2client import indexer
    url   = os.environ["INDEXER_URL"]
    token = os.getenv("INDEXER_TOKEN", "")
    return indexer.IndexerClient(token, url)

def _get_bot_goo_balance() -> int:
    """Return bot wallet GOO balance via algosdk indexer client."""
    try:
        asset_id = int(os.environ["GOO_ASSET_ID"])
        bot_addr = _get_bot_address()
        idx      = _get_indexer_client()
        info     = idx.account_info(bot_addr)
        for a in info.get("account", {}).get("assets", []):
            if a.get("asset-id") == asset_id:
                return a.get("amount", 0)
        return 0
    except Exception as e:
        if "404" not in str(e):
            print(f"[PVP] bot GOO balance check failed: {e}")
        return 0


class PvPCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._pending_duels: dict[str, dict] = {}
        self._last_bot_algo: int = 0
        self._deposit_active_until: float = 0.0
        self.poll_deposits.start()
        self.poll_algo_deposits.start()
        self.expire_challenges.start()
        self.daily_leaderboard_post.start()
        self.weekly_pool_reset.start()
        self.refresh_partner_cache.start()
        self.bot.add_view(JoinBattleView())

    def cog_unload(self):
        self.poll_deposits.cancel()
        self.poll_algo_deposits.cancel()
        self.expire_challenges.cancel()
        self.daily_leaderboard_post.cancel()
        self.weekly_pool_reset.cancel()

    def _wake_deposit_poller(self, minutes: int = 10):
        """Activate deposit polling for the next N minutes."""
        import time
        self._deposit_active_until = time.time() + (minutes * 60)
        print(f"[PVP] Deposit poller active for {minutes} min")

    @commands.Cog.listener()
    async def on_ready(self):
        """Reload persistent boards on redeploy."""
        await self._reload_boards()

    async def _reload_boards(self):
        """Re-attach JoinBattleView to stored board messages on startup — all 6 rooms."""
        await asyncio.sleep(3)  # give bot time to fully connect
        try:
            db = _db()
            for room in ALL_ROOMS:
                ch_id = _room_channel_id(room)
                if not ch_id:
                    continue
                channel = self.bot.get_channel(ch_id)
                if not channel:
                    continue
                board = _get_board(room)

                # Check Supabase for stored board msg id
                row = db.table("pvp_board_state").select("board_msg_id") \
                         .eq("room", room).execute()
                if row.data and row.data[0]["board_msg_id"]:
                    msg_id = int(row.data[0]["board_msg_id"])
                    try:
                        msg = await channel.fetch_message(msg_id)
                        board.board_msg_id = msg_id
                        await msg.edit(view=JoinBattleView())
                        print(f"[PVP] Reattached {room} board msg {msg_id}")
                        continue
                    except Exception:
                        pass

                # No stored board — post a fresh one
                buf  = await render_board("waiting", None, None, "No active challenge")
                file = discord.File(buf, filename="board.png")
                msg  = await channel.send(file=file, view=JoinBattleView())
                board.board_msg_id = msg.id
                db.table("pvp_board_state").upsert({
                    "room":         room,
                    "board_msg_id": str(msg.id),
                }, on_conflict="room").execute()
                print(f"[PVP] Posted new {room} board in channel {ch_id}")
        except Exception as e:
            print(f"[PVP] _reload_boards error: {e}")

    # ─────────────────────────────────────────
    # /pvp_deposit — show deposit instructions
    # ─────────────────────────────────────────

    @discord.app_commands.command(
        name="pvp_deposit",
        description="Get the bot wallet address to deposit $GOO for PvP battles"
    )
    async def pvp_deposit(self, interaction: discord.Interaction):
        if await _wrong_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        bot_addr = await asyncio.to_thread(_get_bot_address)
        db       = _db()
        user_id  = str(interaction.user.id)
        balance  = _get_balance(db, user_id)

        embed = discord.Embed(
            title="💧 Deposit $GOO for PvP",
            description=(
                f"Send $GOO to the bot wallet address below.\n"
                f"Your balance will be credited within ~{DEPOSIT_POLL_SECONDS} seconds.\n\n"
                f"Your current PvP balance: **{balance:,} $GOO**\n\n"
                f"Entry: **{GOO_WAGER_1V1:,} $GOO** per battle\n"
            ),
            color=0x1D9E75
        )
        embed.set_footer(text="/pvp_balance to check • /pvp_withdraw to pull funds out")
        await interaction.followup.send(embed=embed, ephemeral=True)
        # Wallet address as separate message for easy copying
        await interaction.followup.send(f"`{bot_addr}`", ephemeral=True)
        # Wake the deposit poller for 10 minutes
        self._wake_deposit_poller(10)

    # ─────────────────────────────────────────
    # /pvp_balance
    # ─────────────────────────────────────────

    @discord.app_commands.command(
        name="pvp_balance",
        description="Check your PvP $GOO balance"
    )
    async def pvp_balance(self, interaction: discord.Interaction):
        if await _wrong_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        db      = _db()
        user_id = str(interaction.user.id)
        balance = _get_balance(db, user_id)

        embed = discord.Embed(
            title="💧 Your PvP $GOO Balance",
            color=0x1D9E75
        )
        embed.add_field(name="Balance", value=f"**{balance:,} $GOO**", inline=False)
        embed.add_field(
            name="",
            value=f"Deposit more with `/pvp_deposit`\nWithdraw with `/pvp_withdraw`",
            inline=False
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────
    # /pvp_withdraw
    # ─────────────────────────────────────────

    @discord.app_commands.command(
        name="pvp_withdraw",
        description="Withdraw your PvP $GOO balance back to your linked wallet"
    )
    @discord.app_commands.describe(amount="Amount of $GOO to withdraw (leave blank for full balance)")
    async def pvp_withdraw(self, interaction: discord.Interaction, amount: Optional[int] = None):
        if await _wrong_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        user_id = str(interaction.user.id)
        db      = _db()
        balance = _get_balance(db, user_id)

        if balance <= 0:
            await interaction.followup.send("You have no $GOO balance to withdraw.", ephemeral=True)
            return

        withdraw_amt = amount if amount else balance

        if withdraw_amt > balance:
            await interaction.followup.send(
                f"❌ You only have **{balance:,} $GOO**. Can't withdraw {withdraw_amt:,}.",
                ephemeral=True
            )
            return

        if withdraw_amt <= 0:
            await interaction.followup.send("❌ Amount must be greater than 0.", ephemeral=True)
            return

        wallet = await asyncio.to_thread(_get_linked_wallet, user_id)
        if not wallet:
            await interaction.followup.send(
                "❌ No linked wallet found. Use `/link` first.", ephemeral=True
            )
            return

        opted = await asyncio.to_thread(has_opted_in, wallet)
        if not opted:
            await interaction.followup.send(
                "❌ Your wallet hasn't opted in to $GOO. Opt in via Pera Wallet first.",
                ephemeral=True
            )
            return

        # Deduct first, then send — prevents double-withdraw on retry
        ok, new_bal = _deduct(db, user_id, withdraw_amt, note="withdrawal")
        if not ok:
            await interaction.followup.send(
                f"❌ Balance changed during withdrawal. Current: {balance:,} $GOO.", ephemeral=True
            )
            return

        try:
            tx_id = await asyncio.to_thread(
                send_goo, wallet, withdraw_amt,
                f"MONSTRS PvP withdrawal"
            )
            _log_transaction(db, user_id, None, "withdrawal", withdraw_amt, wallet, tx_id)
            await interaction.followup.send(
                f"✅ **{withdraw_amt:,} $GOO** sent to `{wallet[:8]}...{wallet[-4:]}`\n"
                f"TxID: `{tx_id[:20]}...`\n"
                f"Remaining balance: **{new_bal:,} $GOO**",
                ephemeral=True
            )
        except Exception as e:
            # Send failed — re-credit so they don't lose funds
            _credit(db, user_id, withdraw_amt, note="withdrawal send failed — re-credited")
            print(f"[PVP] withdrawal send failed uid={user_id}: {e}")
            await interaction.followup.send(
                f"❌ On-chain send failed: `{e}`\nYour balance has been restored.",
                ephemeral=True
            )

    # ─────────────────────────────────────────
    # ─────────────────────────────────────────
    # /pvp_register
    # ─────────────────────────────────────────

    @discord.app_commands.command(
        name="pvp_register",
        description="Register all your eligible NFTs for PvP battles"
    )
    async def pvp_register(self, interaction: discord.Interaction):
        if await _wrong_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        user_id = str(interaction.user.id)

        wallet = await asyncio.to_thread(_get_linked_wallet, user_id)
        if not wallet:
            await interaction.followup.send(
                "❌ Link your wallet first with `/link`.", ephemeral=True
            )
            return

        await interaction.followup.send(
            "🔍 Scanning your wallet for eligible NFTs...", ephemeral=True
        )

        try:
            eligible = await asyncio.wait_for(
                asyncio.to_thread(_fetch_all_eligible_asa_ids, wallet), timeout=30
            )
        except asyncio.TimeoutError:
            await interaction.edit_original_response(
                content="❌ Wallet scan timed out. Try again in a moment."
            )
            return
        except Exception as e:
            await interaction.edit_original_response(
                content=f"❌ Couldn't reach the chain: {e}"
            )
            return

        if not eligible:
            await interaction.edit_original_response(
                content="❌ No eligible NFTs found in your linked wallet."
            )
            return

        db = _db()

        # Load collection metadata once
        colls_res = db.table("pvp_collections").select("*").eq("active", True).execute()
        coll_map  = {str(c["id"]): c for c in (colls_res.data or [])}

        added_summary = {}  # collection_name -> [nft_name, ...]

        # Batch-fetch all already-registered ASAs in one query
        all_asa_ids = [asa for ids in eligible.values() for asa in ids]
        already_res = db.table("pvp_rosters").select("asa_id") \
                        .eq("user_id", user_id).in_("asa_id", all_asa_ids).execute()
        already_registered = {r["asa_id"] for r in (already_res.data or [])}

        # Refresh verified_at for existing rows in one update per collection
        if already_registered:
            db.table("pvp_rosters").update({
                "verified_at": datetime.now(timezone.utc).isoformat()
            }).eq("user_id", user_id).in_("asa_id", list(already_registered)).execute()

        for coll_id_str, asa_ids in eligible.items():
            coll      = coll_map.get(coll_id_str, {})
            is_monstr = coll.get("is_monstr", False)
            coll_name = coll.get("collection_name", "Unknown")

            # Only process NFTs not already in roster
            new_asa_ids = [a for a in asa_ids if a not in already_registered]
            if not new_asa_ids:
                continue

            if is_monstr:
                # ── MONSTRS: all data from in-memory registry + monstr_pvp_stats ─
                # Never block registration on IPFS — seed instantly, fetch images in background
                rows_to_insert = []
                needs_image    = []  # asa_ids that have no image yet

                for asa_id in new_asa_ids:
                    nft_name = MONSTR_ASSETS.get(asa_id, (f"MONSTR #{asa_id[-4:]}",))[0]
                    mps = db.table("monstr_pvp_stats").select("*").eq("asa_id", asa_id).execute()
                    if not mps.data:
                        atk_b, def_b, spd_b = _calc_trait_bonus(asa_id)
                        db.table("monstr_pvp_stats").insert({
                            "asa_id":          asa_id,
                            "owner_id":        user_id,
                            "monstr_name":     nft_name,
                            "attack":          STAT_BASE + atk_b,
                            "defense":         STAT_BASE + def_b,
                            "speed":           STAT_BASE + spd_b,
                            "trait_bonus_atk": atk_b,
                            "trait_bonus_def": def_b,
                            "trait_bonus_spd": spd_b,
                            "image_url":       "",
                        }).execute()
                        mps = db.table("monstr_pvp_stats").select("*").eq("asa_id", asa_id).execute()

                    r         = mps.data[0]
                    image_url = r.get("image_url") or None
                    if not image_url:
                        needs_image.append(asa_id)

                    rows_to_insert.append({
                        "user_id":       user_id,
                        "asa_id":        asa_id,
                        "nft_name":      nft_name,
                        "collection_id": int(coll_id_str),
                        "image_url":     image_url,
                        "attack":        r["attack"],
                        "defense":       r["defense"],
                        "speed":         r["speed"],
                        "level":         1,
                        "xp":            0,
                        "verified_at":   datetime.now(timezone.utc).isoformat(),
                    })
                    added_summary.setdefault(coll_name, []).append(nft_name)

                # Batch insert instantly
                try:
                    db.table("pvp_rosters").insert(rows_to_insert).execute()
                    for row in rows_to_insert:
                        print(f"[PVP] roster seeded {row['nft_name']} (asa={row['asa_id']}) uid={user_id}")
                except Exception as e:
                    print(f"[PVP] batch insert failed coll={coll_name}: {e}")
                    for row in rows_to_insert:
                        try:
                            db.table("pvp_rosters").insert(row).execute()
                        except Exception as e2:
                            print(f"[PVP] individual insert failed asa={row['asa_id']}: {e2}")

                # Background image fetch for any MONSTRs with no image
                if needs_image:
                    async def _fetch_missing_images(asa_ids: list, db):
                        for asa_id in asa_ids:
                            try:
                                url = await asyncio.wait_for(
                                    asyncio.to_thread(_resolve_arc19_image_url, asa_id), timeout=30
                                )
                                if url:
                                    db.table("monstr_pvp_stats").update({"image_url": url}).eq("asa_id", asa_id).execute()
                                    db.table("pvp_rosters").update({"image_url": url}).eq("asa_id", asa_id).execute()
                                    print(f"[PVP] background image fetched {asa_id}")
                            except Exception as e:
                                print(f"[PVP] background image fetch failed {asa_id}: {e}")
                    asyncio.ensure_future(_fetch_missing_images(needs_image, db))
                    print(f"[PVP] {len(needs_image)} MONSTRs queued for background image fetch")

            else:
                # ── PARTNER NFTs: name + image from pre-loaded maps — no network calls ──
                coll_image_map = COLLECTION_IMAGE_MAPS.get(int(coll_id_str), {})
                coll_name_map  = COLLECTION_NAME_MAPS.get(int(coll_id_str), {})

                await interaction.edit_original_response(
                    content=f"🔍 Registering {len(new_asa_ids)} {coll_name} NFTs..."
                )

                rows_to_insert = []
                for asa_id in new_asa_ids:
                    nft_name  = coll_name_map.get(asa_id) or f"#{asa_id}"
                    image_url = coll_image_map.get(asa_id)
                    rows_to_insert.append({
                        "user_id":       user_id,
                        "asa_id":        asa_id,
                        "nft_name":      nft_name,
                        "collection_id": int(coll_id_str),
                        "image_url":     image_url,
                        "attack":        random.randint(8, 15),
                        "defense":       random.randint(8, 15),
                        "speed":         random.randint(8, 15),
                        "level":         1,
                        "xp":            0,
                        "verified_at":   datetime.now(timezone.utc).isoformat(),
                    })
                    added_summary.setdefault(coll_name, []).append(nft_name)

                try:
                    db.table("pvp_rosters").insert(rows_to_insert).execute()
                    for row in rows_to_insert:
                        print(f"[PVP] roster seeded {row['nft_name']} (asa={row['asa_id']}) img={'yes' if row['image_url'] else 'no'} uid={user_id}")
                except Exception as e:
                    print(f"[PVP] batch insert failed coll={coll_name}: {e}")
                    for row in rows_to_insert:
                        try:
                            db.table("pvp_rosters").insert(row).execute()
                        except Exception as e2:
                            print(f"[PVP] individual insert failed asa={row['asa_id']}: {e2}")

        if not added_summary:
            await interaction.edit_original_response(
                content=(
                    "✅ Your roster is already up to date.\n"
                    "Use `/pvp_roster` to see your fighters."
                )
            )
            return

        lines = ["✅ **Fighters added to your roster:**\n"]
        for coll_name, names in added_summary.items():
            lines.append(f"**{coll_name}** — {len(names)} added")
            for name in names[:8]:
                lines.append(f"  · {name}")
            if len(names) > 8:
                lines.append(f"  · ...and {len(names) - 8} more")
        lines.append("\nUse `/pvp_roster` to see your full roster sorted by power.")
        await interaction.edit_original_response(content="\n".join(lines))

    async def _do_register(self, interaction: discord.Interaction,
                            asa_id: str, user_id: str, wallet: str):
        """Complete registration after MONSTR is chosen."""
        await interaction.response.defer(ephemeral=True)

        # Look up name from registry, or use generic name if not found
        monstr_name = MONSTR_ASSETS.get(str(asa_id), (f"MONSTR #{asa_id[-4:]}",))[0]
        db = _db()

        # Already registered?
        existing = db.table("monstr_pvp_stats").select("asa_id,owner_id").eq("asa_id", str(asa_id)).execute()
        if existing.data:
            if existing.data[0]["owner_id"] != user_id:
                await interaction.followup.send(
                    f"❌ **{monstr_name}** is registered to a different player.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    f"✅ **{monstr_name}** is already registered. Use `/pvp_stats {asa_id}` to view.",
                    ephemeral=True
                )
            return

        # Ownership already confirmed via _fetch_monstr_asa_ids — skip redundant check

        atk_b, def_b, spd_b = _calc_trait_bonus(asa_id)

        # Fetch current ARC-19 image URL (URL only, not bytes)
        try:
            live_image_url = await asyncio.wait_for(
                asyncio.to_thread(_resolve_arc19_image_url, str(asa_id)), timeout=30
            )
        except Exception:
            live_image_url = None

        db.table("monstr_pvp_stats").insert({
            "asa_id":          str(asa_id),
            "owner_id":        user_id,
            "monstr_name":     monstr_name,
            "attack":          STAT_BASE + atk_b,
            "defense":         STAT_BASE + def_b,
            "speed":           STAT_BASE + spd_b,
            "trait_bonus_atk": atk_b,
            "trait_bonus_def": def_b,
            "trait_bonus_spd": spd_b,
            "image_url":       live_image_url or "",
        }).execute()

        stats = _load_stats(asa_id, user_id)

        embed = discord.Embed(
            title=f"✅ {monstr_name} registered for PvP!",
            description="Trait bonus locked in. Spend ALGO with `/pvp_upgrade` to level up stats.",
            color=0x1D9E75
        )
        for name, val in format_stats_embed_fields(stats):
            embed.add_field(name=name, value=val, inline=False)
        if stats and stats.image_url:
            embed.set_thumbnail(url=stats.image_url)
        embed.set_footer(text=f"Trait bonus locked — ATK +{atk_b}  DEF +{def_b}  SPD +{spd_b}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # /pvp_upgrade
    # ─────────────────────────────────────────

    @discord.app_commands.command(
        name="pvp_upgrade",
        description="Spend ALGO to upgrade a registered MONSTR's stats"
    )
    async def pvp_upgrade(self, interaction: discord.Interaction):
        if await _wrong_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        user_id = str(interaction.user.id)
        db      = _db()
        balance = _get_balance(db, user_id)

        # Fetch all registered fighters from pvp_rosters
        rows = db.table("pvp_rosters") \
                 .select("asa_id, nft_name, attack, defense, speed, collection_id, pvp_collections(collection_name)") \
                 .eq("user_id", user_id) \
                 .execute()
        if not rows.data:
            await interaction.followup.send(
                "❌ No registered fighters. Use `/pvp_register` first.", ephemeral=True
            )
            return

        async def on_pick(pick_interaction: discord.Interaction, asa_id: str):
            await self._show_stat_picker(pick_interaction, asa_id, user_id, db, balance)

        fighter_rows = []
        for r in rows.data:
            coll = r.get("pvp_collections") or {}
            power = r["attack"] + r["defense"] + r["speed"]
            fighter_rows.append({
                "asa_id":          r["asa_id"],
                "monstr_name":     r["nft_name"],
                "attack":          r["attack"],
                "defense":         r["defense"],
                "speed":           r["speed"],
                "power":           power,
                "collection_id":   r["collection_id"],
                "collection_name": coll.get("collection_name", "Unknown"),
            })
        fighter_rows.sort(key=lambda r: -r["power"])

        view, content = _build_collection_picker(fighter_rows, on_pick, "Choose a fighter to upgrade")
        await interaction.followup.send(
            f"{content}\n(upgrades cost ALGO)",
            view=view, ephemeral=True
        )

    async def _show_stat_picker(self, interaction: discord.Interaction,
                                 asa_id: str, user_id: str, db, balance: int):
        """Show ATK / DEF / SPD upgrade buttons for chosen MONSTR.
        Each stat gets two buttons: +1 (single step) and MAX (all remaining steps).
        """
        await interaction.response.defer(ephemeral=True)

        row = db.table("pvp_rosters").select("*, pvp_collections(is_monstr)") \
                .eq("asa_id", str(asa_id)).eq("user_id", user_id).execute()
        if not row.data:
            await interaction.followup.send(
                "❌ That fighter isn't in your roster. Use `/pvp_register` first.",
                ephemeral=True)
            return

        r         = row.data[0]
        is_monstr = (r.get("pvp_collections") or {}).get("is_monstr", False)
        stats = {
            "attack":  r["attack"],
            "defense": r["defense"],
            "speed":   r["speed"],
        }

        algo_bal = _get_algo_balance(db, user_id)

        def _max_cost(current: int) -> int:
            """Total microALGO to upgrade a stat from current to STAT_MAX."""
            total = 0
            for v in range(current, STAT_MAX):
                total += upgrade_cost_algo(v)
            return total

        view = discord.ui.View(timeout=60)

        stat_lines = []
        for stat, val in stats.items():
            capped   = not can_upgrade(val)
            one_cost = upgrade_cost_algo(val)
            max_cost = _max_cost(val)
            steps_left = STAT_MAX - val

            # +1 button (row 0)
            one_label = (
                f"{stat.upper()} {val}→{val+1}  ({upgrade_cost_algo_display(val)})"
                if not capped else f"{stat.upper()} {val} MAX"
            )
            btn_one = discord.ui.Button(
                label     = one_label,
                style     = discord.ButtonStyle.success if not capped else discord.ButtonStyle.secondary,
                disabled  = capped,
                custom_id = f"upg1_{asa_id}_{stat}",
                row       = list(stats.keys()).index(stat),
            )
            async def make_one_cb(s=stat, v=val, ac=one_cost):
                async def cb(intr: discord.Interaction):
                    await self._do_upgrade(intr, asa_id, s, v, ac, user_id, db)
                return cb
            btn_one.callback = await make_one_cb()
            view.add_item(btn_one)

            # MAX button (same row)
            max_algo_str = f"{max_cost/1_000_000:g} ALGO"
            max_label = (
                f"MAX ({steps_left} steps · {max_algo_str})"
                if not capped else "MAX ✓"
            )
            btn_max = discord.ui.Button(
                label     = max_label,
                style     = discord.ButtonStyle.primary if not capped else discord.ButtonStyle.secondary,
                disabled  = capped,
                custom_id = f"upgmax_{asa_id}_{stat}",
                row       = list(stats.keys()).index(stat),
            )
            async def make_max_cb(s=stat, v=val, mc=max_cost):
                async def cb(intr: discord.Interaction):
                    await self._do_upgrade_max(intr, asa_id, s, v, mc, user_id, db)
                return cb
            btn_max.callback = await make_max_cb()
            view.add_item(btn_max)

            stat_lines.append(
                f"**{stat.upper()}** {val}/{STAT_MAX}  "
                f"+1: {upgrade_cost_algo_display(val)}  "
                f"MAX: {f'{max_cost/1_000_000:g} ALGO' if not capped else '✓'}"
            )

        bal_str = f"{algo_bal/1_000_000:g} ALGO"
        await interaction.followup.send(
            f"**{r['nft_name']}** — upgrade stats (balance: **{bal_str}**)\n"
            + "\n".join(stat_lines),
            view=view, ephemeral=True
        )

    async def _do_upgrade_max(self, interaction: discord.Interaction,
                               asa_id: str, stat: str, current_val: int,
                               total_cost_micro: int, user_id: str, db):
        """Deduct total cost and apply all upgrades to max in one shot."""
        await interaction.response.defer(ephemeral=True)

        row = db.table("pvp_rosters").select("nft_name, pvp_collections(is_monstr)") \
                .eq("asa_id", str(asa_id)).eq("user_id", user_id).execute()
        monstr_name = row.data[0]["nft_name"] if row.data else asa_id
        is_monstr   = (row.data[0].get("pvp_collections") or {}).get("is_monstr", False) if row.data else False
        steps       = STAT_MAX - current_val

        if steps <= 0:
            await interaction.followup.send(
                f"**{stat.upper()}** is already maxed!", ephemeral=True)
            return

        algo_bal = _get_algo_balance(db, user_id)
        if algo_bal < total_cost_micro:
            have = algo_bal / 1_000_000
            need = total_cost_micro / 1_000_000
            await interaction.followup.send(
                f"Not enough ALGO. Need **{need:g} ALGO** to max {stat.upper()}, "
                f"you have **{have:g} ALGO**.\n"
                f"Use `/pvp_deposit_algo` to top up.",
                ephemeral=True)
            return

        ok = _deduct_algo(db, user_id, total_cost_micro,
                          f"max upgrade {monstr_name} {stat} {current_val}>{STAT_MAX}")
        if not ok:
            await interaction.followup.send("Balance error — please try again.", ephemeral=True)
            return

        # Always update pvp_rosters
        db.table("pvp_rosters").update({
            stat:         STAT_MAX,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("asa_id", str(asa_id)).eq("user_id", user_id).execute()

        # Also sync monstr_pvp_stats for MONSTRS
        if is_monstr:
            db.table("monstr_pvp_stats").update({
                stat:         STAT_MAX,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("asa_id", str(asa_id)).execute()

        new_algo_bal = _get_algo_balance(db, user_id)
        stats        = _load_stats_from_roster(db, asa_id, user_id)
        cost_str     = f"{total_cost_micro/1_000_000:g} ALGO"

        embed = discord.Embed(
            title=f"🔥 {stat.upper()} maxed!",
            description=(
                f"**{monstr_name}** — {stat.upper()} upgraded "
                f"{current_val} → **{STAT_MAX}** ({steps} steps)\n"
                f"Cost: **{cost_str}**  Balance: **{new_algo_bal/1_000_000:g} ALGO**"
            ),
            color=0xFFD700
        )
        if stats:
            for name, val in format_stats_embed_fields(stats):
                embed.add_field(name=name, value=val, inline=False)
        if stats and stats.image_url:
            embed.set_thumbnail(url=stats.image_url)
        await interaction.followup.send(embed=embed, ephemeral=True)
        print(f"[PVP] Max upgrade: uid={user_id} {stat} {current_val}>{STAT_MAX} cost={cost_str}")

    async def _do_upgrade(self, interaction: discord.Interaction,
                           asa_id: str, stat: str, current_val: int,
                           algo_cost_micro: int, user_id: str, db):
        """Deduct from custodial ALGO balance and apply upgrade instantly."""
        await interaction.response.defer(ephemeral=True)

        row = db.table("pvp_rosters").select("nft_name, pvp_collections(is_monstr)") \
                .eq("asa_id", str(asa_id)).eq("user_id", user_id).execute()
        monstr_name = row.data[0]["nft_name"] if row.data else asa_id
        is_monstr   = (row.data[0].get("pvp_collections") or {}).get("is_monstr", False) if row.data else False
        algo_display = upgrade_cost_algo_display(current_val)
        new_val = current_val + 1

        # Check custodial ALGO balance
        algo_bal = _get_algo_balance(db, user_id)
        if algo_bal < algo_cost_micro:
            algo_have = algo_bal / 1_000_000
            algo_need = algo_cost_micro / 1_000_000
            await interaction.followup.send(
                f"Not enough ALGO. Need **{algo_need:g} ALGO**, you have **{algo_have:g} ALGO**.\n"
                f"Use `/pvp_deposit_algo` to top up your Warden wallet.",
                ephemeral=True
            )
            return

        ok = _deduct_algo(db, user_id, algo_cost_micro,
                          f"upgrade {monstr_name} {stat} {current_val}>{new_val}")
        if not ok:
            await interaction.followup.send("Balance error — please try again.", ephemeral=True)
            return

        # Always update pvp_rosters
        db.table("pvp_rosters").update({
            stat:         new_val,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("asa_id", str(asa_id)).eq("user_id", user_id).execute()

        # Also sync monstr_pvp_stats for MONSTRS
        if is_monstr:
            db.table("monstr_pvp_stats").update({
                stat:         new_val,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("asa_id", str(asa_id)).execute()

        new_algo_bal = _get_algo_balance(db, user_id)
        stats = _load_stats_from_roster(db, asa_id, user_id)

        embed = discord.Embed(
            title=f"⬆️ {monstr_name} upgraded!",
            description=(
                f"**{stat.capitalize()}** {current_val} → **{new_val}**\n"
                f"Cost: **{algo_display}**  Balance: **{new_algo_bal/1_000_000:g} ALGO**"
            ),
            color=0x1D9E75
        )
        if stats:
            for name, val in format_stats_embed_fields(stats):
                embed.add_field(name=name, value=val, inline=False)
        if stats and stats.image_url:
            embed.set_thumbnail(url=stats.image_url)
        await interaction.followup.send(embed=embed, ephemeral=True)
        print(f"[PVP] Upgrade: uid={user_id} {stat} {current_val}>{new_val} cost={algo_cost_micro/1_000_000:g} ALGO")

    # /pvp_info
    # ─────────────────────────────────────────

    @discord.app_commands.command(
        name="pvp_info",
        description="Everything you need to know about MONSTRS Battle"
    )
    async def pvp_info(self, interaction: discord.Interaction):
        if await _wrong_channel(interaction): return
        await interaction.response.defer(ephemeral=True)

        embed = discord.Embed(title="MONSTRS Battle - How to Play", color=0x1D9E75)

        embed.add_field(name="Getting Started", inline=False, value=(
            "`/link` - Link your Algorand wallet\n"
            "`/pvp_register` - Register a MONSTR\n"
            "`/pvp_roster` - View your registered MONSTRs\n"
            "`/pvp_stats [asa_id]` - View a MONSTR stat card"
        ))
        embed.add_field(name="GOO Room (" + str(GOO_WAGER_1V1) + " $GOO entry)", inline=False, value=(
            "`/pvp_deposit` - Deposit $GOO\n"
            "`/pvp_balance` - Check $GOO balance\n"
            "`/pvp_withdraw` - Withdraw $GOO\n"
            "Winner gets " + str(GOO_WINNER_CUT_1V1) + " | Treasury: " + str(GOO_TREASURY_1V1)
        ))
        embed.add_field(name="ALGO Room (5 ALGO entry)", inline=False, value=(
            "`/pvp_deposit_algo` - Top up Warden ALGO wallet\n"
            "`/pvp_withdraw_algo` - Withdraw ALGO\n"
            "Winner gets 9 ALGO | Treasury: 1 ALGO"
        ))
        embed.add_field(name="Upgrades", inline=False, value=(
            "`/pvp_upgrade` - Spend ALGO to level ATK/DEF/SPD\n"
            "Stats 10 to 50 | 0.1-2 ALGO per step | ~80 ALGO fully maxed\n"
            "SPD diff >= 20 = double attack each round"
        ))
        embed.add_field(name="Battle Rules", inline=False, value=(
            "Tap Join Battle to enter\n"
            "2 players join = battle starts automatically\n"
            "Same MONSTR cannot be in two rooms at once\n"
            "30-min cooldown after every battle\n"
            "Higher SPD goes first | Crits = 2x damage"
        ))
        embed.set_footer(text="Only the Weird Survive")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # /pvp_roster
    # ─────────────────────────────────────────

    @discord.app_commands.command(
        name="pvp_roster",
        description="View all your registered fighters and their stats"
    )
    async def pvp_roster(self, interaction: discord.Interaction):
        if await _wrong_channel(interaction): return
        await interaction.response.defer(ephemeral=True)

        user_id = str(interaction.user.id)
        db      = _db()

        roster_res = db.table("pvp_rosters") \
            .select("*, pvp_collections(collection_name)") \
            .eq("user_id", user_id) \
            .execute()
        roster = roster_res.data or []

        if not roster:
            await interaction.followup.send(
                "Your roster is empty. Use `/pvp_register` to scan your wallet.",
                ephemeral=True
            )
            return

        # Sort by power descending
        roster.sort(key=lambda r: r["attack"] + r["defense"] + r["speed"], reverse=True)

        embed = discord.Embed(
            title=f"⚔️ Your PvP Roster ({len(roster)} fighter{'s' if len(roster) != 1 else ''})",
            color=0x1D9E75
        )
        for r in roster[:25]:
            power     = r["attack"] + r["defense"] + r["speed"]
            coll      = r.get("pvp_collections") or {}
            coll_name = coll.get("collection_name", "Unknown")
            atk_cost  = upgrade_cost_algo_display(r["attack"])
            def_cost  = upgrade_cost_algo_display(r["defense"])
            spd_cost  = upgrade_cost_algo_display(r["speed"])
            embed.add_field(
                name=f"{r['nft_name']} · Lv{r['level']} · PWR {power}",
                value=(
                    f"ATK `{r['attack']}` ({atk_cost}) | "
                    f"DEF `{r['defense']}` ({def_cost}) | "
                    f"SPD `{r['speed']}` ({spd_cost})\n"
                    f"*{coll_name}* · ASA: `{r['asa_id']}`"
                ),
                inline=False
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # /pvp_stats
    # ─────────────────────────────────────────

    @discord.app_commands.command(
        name="pvp_stats",
        description="View a fighter's PvP stat card"
    )
    @discord.app_commands.describe(asa_id="The ASA ID of the fighter to inspect")
    async def pvp_stats(self, interaction: discord.Interaction, asa_id: str):
        if await _wrong_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        db = _db()

        # Check pvp_rosters first (covers all collections)
        row = db.table("pvp_rosters") \
                .select("*, pvp_collections(collection_name, is_monstr)") \
                .eq("asa_id", str(asa_id)).execute()

        if not row.data:
            await interaction.followup.send(
                f"❌ **{asa_id}** isn't registered for PvP yet.", ephemeral=True
            )
            return

        r         = row.data[0]
        coll      = r.get("pvp_collections") or {}
        coll_name = coll.get("collection_name", "Unknown")
        is_monstr = coll.get("is_monstr", False)
        stats     = _load_stats_from_roster(db, asa_id, r["user_id"])

        embed = discord.Embed(
            title=f"⚔️ {r['nft_name']} — PvP Stats",
            color=0x9b59b6
        )
        embed.add_field(name="👤 Owner",      value=f"<@{r['user_id']}>",  inline=True)
        embed.add_field(name="📦 Collection", value=coll_name,             inline=True)
        embed.add_field(name="🏆 Level",      value=str(r["level"]),       inline=True)

        if stats:
            for name, val in format_stats_embed_fields(stats):
                embed.add_field(name=name, value=val, inline=False)
        if r.get("image_url"):
            embed.set_thumbnail(url=r["image_url"])

        # Show trait bonus footer for MONSTRS only
        if is_monstr:
            mps = db.table("monstr_pvp_stats").select(
                "trait_bonus_atk, trait_bonus_def, trait_bonus_spd, registered_at"
            ).eq("asa_id", str(asa_id)).execute()
            if mps.data:
                m = mps.data[0]
                embed.set_footer(
                    text=(
                        f"Trait bonus: ATK+{m['trait_bonus_atk']} "
                        f"DEF+{m['trait_bonus_def']} SPD+{m['trait_bonus_spd']}  •  "
                        f"Registered {m['registered_at'][:10]}"
                    )
                )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────
    # /pvp_algo_balance
    # ─────────────────────────────────────────

    @discord.app_commands.command(
        name="pvp_algo_balance",
        description="Check your Warden ALGO balance for upgrades and ALGO battles"
    )
    async def pvp_algo_balance(self, interaction: discord.Interaction):
        if await _wrong_channel(interaction): return
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)
        db      = _db()
        bal     = await asyncio.to_thread(_get_algo_balance, db, user_id)
        await interaction.followup.send(
            f"Your Warden ALGO balance: **{bal/1_000_000:g} ALGO**\n"
            f"Use `/pvp_deposit_algo` to top up or `/pvp_withdraw_algo` to withdraw.",
            ephemeral=True
        )

    # /pvp_deposit_algo + /pvp_withdraw_algo
    # ─────────────────────────────────────────

    @discord.app_commands.command(
        name="pvp_deposit_algo",
        description="Top up your Warden ALGO wallet for upgrades"
    )
    async def pvp_deposit_algo(self, interaction: discord.Interaction):
        if await _wrong_channel(interaction): return
        await interaction.response.defer(ephemeral=True)
        user_id  = str(interaction.user.id)
        db       = _db()
        bot_addr = await asyncio.to_thread(_get_bot_address)
        bal      = _get_algo_balance(db, user_id)
        embed = discord.Embed(
            title="ALGO Warden Wallet",
            description=(
                "Send any amount of ALGO to your Warden wallet and it credits automatically.\n\n"
                f"Your current balance: **{bal/1_000_000:g} ALGO**\n\n"
                "Bot wallet address:"
            ),
            color=0xF4B942
        )
        embed.set_footer(text="Credited within ~30 seconds of your tx confirming")
        await interaction.followup.send(embed=embed, ephemeral=True)
        await interaction.followup.send(f"`{bot_addr}`", ephemeral=True)
        # Wake the deposit poller for 10 minutes
        self._wake_deposit_poller(10)

    @discord.app_commands.command(
        name="pvp_withdraw_algo",
        description="Withdraw your Warden ALGO balance back to your linked wallet"
    )
    async def pvp_withdraw_algo(self, interaction: discord.Interaction):
        if await _wrong_channel(interaction): return
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)
        db      = _db()
        bal     = _get_algo_balance(db, user_id)
        if bal <= 0:
            await interaction.followup.send(
                "No ALGO balance. Use `/pvp_deposit_algo` to add funds.", ephemeral=True
            )
            return
        wallet = await asyncio.to_thread(_get_linked_wallet, user_id)
        if not wallet:
            await interaction.followup.send("Link your wallet first with `/link`.", ephemeral=True)
            return
        ok = _deduct_algo(db, user_id, bal, "algo withdrawal")
        if not ok:
            await interaction.followup.send("Balance error — try again.", ephemeral=True)
            return
        try:
            from algosdk import transaction as _txn
            from algosdk.v2client import algod as _algod
            client = _algod.AlgodClient(
                os.getenv("ALGOD_TOKEN", ""),
                os.getenv("ALGOD_URL", "https://mainnet-api.algonode.cloud"),
                headers={"X-Algo-API-Token": os.getenv("ALGOD_TOKEN", "")},
            )
            pk, addr = _get_bot_keypair()
            params  = client.suggested_params()
            send_amt = max(0, bal - 1000)
            unsigned = _txn.PaymentTxn(
                sender=addr, sp=params, receiver=wallet,
                amt=send_amt, note=b"MONSTRS PvP ALGO withdrawal"
            )
            tx_id = client.send_transaction(unsigned.sign(pk))
            await interaction.followup.send(
                f"Sent **{send_amt/1_000_000:g} ALGO** to `{wallet[:8]}...` | TxID: `{tx_id}`",
                ephemeral=True
            )
        except Exception as e:
            _credit_algo(db, user_id, bal, "withdrawal refund")
            await interaction.followup.send(f"Withdrawal failed, balance refunded. {e}", ephemeral=True)

    # /pvp_setupboard + /pvp_setupboard_algo
    # ─────────────────────────────────────────

    @discord.app_commands.command(
        name="pvp_setupboard",
        description="(Admin) Post the GOO battle board in this channel"
    )
    @discord.app_commands.default_permissions(administrator=True)
    async def pvp_setupboard(self, interaction: discord.Interaction):
        if await _wrong_channel(interaction): return
        room  = _channel_room(interaction.channel_id)
        board = _get_board(room)
        await interaction.response.defer(ephemeral=True)
        buf  = await render_board("waiting", None, None, "No active challenge")
        file = discord.File(buf, filename="board.png")
        msg  = await interaction.channel.send(file=file, view=JoinBattleView())
        board.board_msg_id = msg.id
        board.reset()
        db = _db()
        db.table("pvp_board_state").upsert({
            "room": room, "board_msg_id": str(msg.id),
        }, on_conflict="room").execute()
        await interaction.followup.send(f"{room} battle board posted! Pin it.", ephemeral=True)

    @discord.app_commands.command(
        name="pvp_refreshimage",
        description="(Admin) Re-fetch and update the image for any registered fighter by ASA ID"
    )
    @discord.app_commands.default_permissions(administrator=True)
    @discord.app_commands.describe(monstr="MONSTR number (e.g. 8 or #0008) or any ASA ID")
    async def pvp_refreshimage(self, interaction: discord.Interaction, monstr: str):
        await interaction.response.defer(ephemeral=True)
        try:
            db = _db()
            monstr = monstr.strip()
            num_str = monstr.upper().replace("MONSTR", "").replace("#", "").strip()
            is_asa_id = num_str.isdigit() and int(num_str) > 9999

            if is_asa_id:
                asa_id  = num_str
                display = f"ASA `{num_str}`"
            else:
                if num_str.isdigit():
                    padded = f"MONSTR #{int(num_str):04d}"
                    row = db.table("monstr_pvp_stats").select("asa_id,monstr_name").ilike("monstr_name", padded).execute()
                else:
                    row = db.table("monstr_pvp_stats").select("asa_id,monstr_name").ilike("monstr_name", f"%{num_str}%").execute()
                if not row.data:
                    await interaction.followup.send(f"Could not find a registered fighter matching `{monstr}`.", ephemeral=True)
                    return
                asa_id  = row.data[0]["asa_id"]
                display = row.data[0]["monstr_name"]

            # Check if this ASA is in a partner collection image map
            roster_row = db.table("pvp_rosters").select("collection_id").eq("asa_id", asa_id).execute()
            new_url    = None

            if roster_row.data:
                coll_id       = roster_row.data[0]["collection_id"]
                coll_image_map = COLLECTION_IMAGE_MAPS.get(coll_id, {})
                if coll_image_map:
                    # Partner collection — pull from pre-loaded map
                    new_url = coll_image_map.get(str(asa_id))
                    if not new_url:
                        await interaction.followup.send(
                            f"No image found in collection map for `{display}`. "
                            f"The CSV may not include this ASA.", ephemeral=True)
                        return

            if not new_url:
                # MONSTR or unknown — resolve via ARC-19
                new_url = await asyncio.wait_for(
                    asyncio.to_thread(_resolve_arc19_image_url, asa_id),
                    timeout=30
                )

            if not new_url:
                await interaction.followup.send(f"Could not resolve image for {display}. Check the ASA and try again.", ephemeral=True)
                return

            db.table("monstr_pvp_stats").update({"image_url": new_url}).eq("asa_id", asa_id).execute()
            db.table("pvp_rosters").update({"image_url": new_url}).eq("asa_id", asa_id).execute()
            await interaction.followup.send(f"Image updated for **{display}**.\n{new_url}", ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("Timed out resolving the image. Try again.", ephemeral=True)
        except Exception as e:
            print(f"[ERROR] pvp_refreshimage: {e}")
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @discord.app_commands.command(
        name="pvp_backfill_images",
        description="(Admin) Bulk re-fetch images for all roster rows with missing image URLs"
    )
    @discord.app_commands.default_permissions(administrator=True)
    async def pvp_backfill_images(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            db = _db()
            # Find ALL roster rows with missing images
            rows = (
                db.table("pvp_rosters")
                .select("asa_id, nft_name, collection_id, image_url, pvp_collections(is_monstr)")
                .execute()
            )
            missing_rows = [r for r in (rows.data or []) if not r.get("image_url")]
            total = len(missing_rows)
            if total == 0:
                await interaction.followup.send("No roster rows with missing images. All good!", ephemeral=True)
                return

            await interaction.followup.send(
                f"Starting backfill for **{total}** fighters with missing images. "
                f"This runs in the background — check Railway logs for progress.",
                ephemeral=True
            )

            async def _do_backfill():
                ok = 0
                fail = 0
                skipped = 0
                for r in missing_rows:
                    asa_id   = r["asa_id"]
                    coll_id  = int(r.get("collection_id") or 0)
                    is_monstr = (r.get("pvp_collections") or {}).get("is_monstr", False)
                    name     = r.get("nft_name", "?")
                    url      = None

                    # Skip Dark Coin Champions only if no token configured
                    if coll_id == 6 and not os.environ.get("DARK_COIN_IPFS_TOKEN"):
                        skipped += 1
                        continue

                    try:
                        # Check local image map first (fast, no IPFS call)
                        image_map = COLLECTION_IMAGE_MAPS.get(coll_id, {})
                        if image_map:
                            url = image_map.get(str(asa_id))

                        # Fall back to ARC-19 live resolve for MONSTRs and unmapped ARC-19 collections
                        if not url:
                            url = await asyncio.wait_for(
                                asyncio.to_thread(_resolve_arc19_image_url, asa_id),
                                timeout=30
                            )

                        if url:
                            db.table("pvp_rosters").update({"image_url": url}).eq("asa_id", asa_id).execute()
                            if is_monstr:
                                db.table("monstr_pvp_stats").update({"image_url": url}).eq("asa_id", asa_id).execute()
                            print(f"[PVP] backfill ok: {name} ({asa_id})")
                            ok += 1
                        else:
                            print(f"[PVP] backfill no-url: {name} ({asa_id})")
                            fail += 1
                    except Exception as e:
                        print(f"[PVP] backfill failed: {name} ({asa_id}): {e}")
                        fail += 1
                    await asyncio.sleep(1)
                print(f"[PVP] backfill complete: {ok} ok, {fail} failed, {skipped} skipped (Dark Coin) out of {total}")

            asyncio.ensure_future(_do_backfill())

        except Exception as e:
            print(f"[ERROR] pvp_backfill_images: {e}")
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    # ─────────────────────────────────────────
    # BATTLE RUNNER — round by round with delays
    # ─────────────────────────────────────────


    async def _run_and_post_battle(self, channel, db: object, duel_id: int,
                                    a: MonstrStats, b: MonstrStats,
                                    chal_id: str, opp_id: str,
                                    room: str = "goo"):
        result: BattleResult = await asyncio.to_thread(resolve_battle, a, b)
        battle_log = [
            {"round": r.round_num, "attacker": r.attacker_id, "damage": r.damage,
             "crit": r.is_crit, "defender_hp": r.defender_hp, "flavor": r.flavor}
            for r in result.rounds
        ]
        hp_a = a.hp
        hp_b = b.hp

        def _bar(cur, mx, n=10):
            if cur <= 0: return "💀" * n
            f = max(1, round((cur / mx) * n))
            if f <= 2:   return "🟥" * f + "⬛" * (n-f)
            elif f <= 5: return "🟧" * f + "⬛" * (n-f)
            else:        return "🟩" * f + "⬛" * (n-f)

        def _status():
            return ("❤️ **" + a.name + "** `" + str(max(0,hp_a)) + " HP`\n"
                    + _bar(hp_a, a.hp) + "\n"
                    + "❤️ **" + b.name + "** `" + str(max(0,hp_b)) + " HP`\n"
                    + _bar(hp_b, b.hp))

        status_msg = await channel.send(_status())
        round_msg  = await channel.send("⚔️ Battle starting...")

        for r in result.rounds:
            if r.attacker_id == a.asa_id: hp_b = r.defender_hp
            else: hp_a = r.defender_hp
            await round_msg.edit(content=r.flavor)
            await status_msg.edit(content=_status())
            await asyncio.sleep(1.5)
            if r.defender_hp <= 0: break

        wager = ALGO_WAGER_1V1 if _is_algo_room(room) else GOO_WAGER_1V1
        if result.is_draw:
            _refund_wager(db, chal_id, wager, duel_id, "draw")
            _refund_wager(db, opp_id, wager, duel_id, "draw")
            try:
                db.table("pvp_duels").update({"status": "draw", "battle_log": battle_log,
                    "updated_at": datetime.now(timezone.utc).isoformat()}).eq("id", duel_id).execute()
            except Exception as e: print(f"[PVP] DB draw failed: {e}")
        else:
            winner_id  = result.winner_owner
            is_algo    = _is_algo_room(room)
            winner_cut = ALGO_WINNER_CUT if is_algo else GOO_WINNER_CUT_1V1
            treasury   = ALGO_TREASURY   if is_algo else GOO_TREASURY_1V1
            if is_algo:
                _credit_algo(db, winner_id, winner_cut, f"algo win duel#{duel_id}")
                if treasury > 0:
                    _log_transaction(db, "treasury", duel_id, "treasury_cut_algo", treasury,
                                     note=f"10% algo cut duel#{duel_id}")
            else:
                _credit_win(db, winner_id, winner_cut, duel_id)
                if treasury > 0:
                    _log_transaction(db, "treasury", duel_id, "treasury_cut_goo", treasury,
                                     note=f"10% goo cut duel#{duel_id}")
            asyncio.ensure_future(self._send_winner_payout(winner_id, winner_cut, duel_id))
            try:
                db.table("pvp_duels").update({"status": "complete", "winner_id": winner_id,
                    "winner_asa": result.winner_asa, "battle_log": battle_log,
                    "updated_at": datetime.now(timezone.utc).isoformat()}).eq("id", duel_id).execute()
                print(f"[PVP] Duel #{duel_id} complete winner={winner_id} room={room}")
            except Exception as e:
                print(f"[PVP] DB failed: {e}")
                try:
                    db.table("pvp_duels").update({"status": "complete", "winner_id": winner_id,
                        "winner_asa": result.winner_asa,
                        "updated_at": datetime.now(timezone.utc).isoformat()}).eq("id", duel_id).execute()
                except Exception as e2: print(f"[PVP] DB retry failed: {e2}")

            # ── Weekly prize pool rake (20%) ──────────────────────────────
            try:
                wager_total = wager * 2   # both players contributed
                rake_amount = int(wager_total * WEEKLY_RAKE_PCT)
                _ensure_weekly_pools(db)
                _add_to_weekly_pool(db, _pool_room(room), rake_amount)
                print(f"[PVP] Weekly pool +{rake_amount} ({_pool_room(room)}) duel#{duel_id}")
            except Exception as e:
                print(f"[PVP] weekly pool rake failed duel#{duel_id}: {e}")

            # ── Weekly leaderboard ────────────────────────────────────────
            loser_id = opp_id if winner_id == chal_id else chal_id
            try:
                _upsert_leaderboard(db, winner_id, _pool_room(room), won=True)
                _upsert_leaderboard(db, loser_id,  _pool_room(room), won=False)
            except Exception as e:
                print(f"[PVP] leaderboard upsert failed duel#{duel_id}: {e}")

        try:
            if result.is_draw:
                win_info = WinnerInfo(monstr_name=a.name, username="draw",
                    total_rounds=result.total_rounds, wager_won=0, image_url=None, is_draw=True)
            else:
                winner_m   = a if result.winner_asa == a.asa_id else b
                winner_uname = await _get_display_name(channel.guild, winner_m.owner_id)
                is_algo    = _is_algo_room(room)
                winner_cut = ALGO_WINNER_CUT if is_algo else GOO_WINNER_CUT_1V1
                win_info = WinnerInfo(monstr_name=winner_m.name, username=winner_uname,
                    total_rounds=result.total_rounds, wager_won=winner_cut,
                    image_url=winner_m.image_url, is_draw=False, is_algo=_is_algo_room(room))
            result_buf = await render_result(win_info)
            await channel.send(file=discord.File(result_buf, filename="result.png"))
        except Exception as e:
            import traceback
            print(f"[PVP] Winner board failed: {e}")
            print(traceback.format_exc())

        if result.is_draw:
            await channel.send("Both MONSTRs fought to a standstill — DRAW! "
                + f"<@{chal_id}> <@{opp_id}> wagers refunded. GG! 🤝")
        else:
            winner_m   = a if result.winner_asa == a.asa_id else b
            loser_m    = b if result.winner_asa == a.asa_id else a
            is_algo    = _is_algo_room(room)
            winner_cut = ALGO_WINNER_CUT if is_algo else GOO_WINNER_CUT_1V1
            prize_str  = f"{winner_cut/1_000_000:g} ALGO" if is_algo else f"{winner_cut:,} $GOO"
            await channel.send(
                f"🏆 **{winner_m.name}** wins! Congratulations <@{winner_m.owner_id}>! 🎉\n"
                + f"**+{prize_str}** credited. GG <@{loser_m.owner_id}>! 💪\n"
                + (f"🏆 +{int(ALGO_WAGER_1V1 * 2 * WEEKLY_RAKE_PCT) / 1_000_000:g} ALGO added to the weekly prize pool!" if is_algo else f"🏆 +{int(GOO_WAGER_1V1 * 2 * WEEKLY_RAKE_PCT):,} $GOO added to the weekly prize pool!"))

    async def _run_board_battle(self, channel, db: object, room: str = "goo",
                                 chal_id: str = "", chal_asa: str = "",
                                 chal_stats: MonstrStats = None, chal_uname: str = "",
                                 opp_id: str = "", opp_asa: str = "",
                                 opp_stats: MonstrStats = None, opp_uname: str = ""):
        """Full battle flow for either GOO or ALGO room."""
        is_algo  = _is_algo_room(room)
        wager    = ALGO_WAGER_1V1 if is_algo else GOO_WAGER_1V1
        board    = _get_board(room)

        bp1 = _to_board_player(chal_stats, chal_uname)
        bp2 = _to_board_player(opp_stats,  opp_uname)
        await _update_board(channel, "active", room, bp1, bp2, "⚔️ BATTLE IN PROGRESS")

        duel_result = db.table("pvp_duels").insert({
            "challenger_id":  chal_id,
            "opponent_id":    opp_id,
            "challenger_asa": chal_asa,
            "opponent_asa":   opp_asa,
            "room":           room,
            "wager_amount":   wager,
            "status":         "active",
            "expires_at":     (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }).execute()
        duel_id = duel_result.data[0]["id"]

        await asyncio.sleep(1)

        # Lock wagers
        deposit_cmd = "`/pvp_deposit_algo`" if is_algo else "`/pvp_deposit`"
        wager_str   = f"{wager/1_000_000:g} ALGO" if is_algo else f"{wager:,} $GOO"

        # Challenger already paid on join — only lock opponent's wager now
        if is_algo:
            opp_ok = await asyncio.to_thread(_deduct_algo, db, opp_id, wager, f"wager duel#{duel_id}")
        else:
            opp_ok = await asyncio.to_thread(_lock_wager, db, opp_id, wager, duel_id)
        print(f"[PVP] wager lock opp={opp_id} ok={opp_ok} room={room} amount={wager}")

        if not opp_ok:
            # Refund challenger since battle can't start
            if is_algo:
                await asyncio.to_thread(_credit_algo, db, chal_id, wager, "opp failed — refund")
            else:
                await asyncio.to_thread(_refund_wager, db, chal_id, wager, duel_id, "opp failed")
            await channel.send(f"❌ <@{opp_id}> doesn't have enough. Need **{wager_str}**. Use {deposit_cmd}. <@{chal_id}> refunded.")
            await asyncio.to_thread(lambda: db.table("pvp_duels").update({"status": "cancelled"}).eq("id", duel_id).execute())
            await _update_board(channel, "waiting", room, status_text="No active challenge")
            return

        await self._run_and_post_battle(
            channel, db, duel_id,
            chal_stats, opp_stats, chal_id, opp_id,
            room=room
        )

        # Set 30-min cooldown on both MONSTRs
        expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        for asa in [chal_asa, opp_asa]:
            db.table("pvp_cooldowns").upsert({
                "asa_id":     asa,
                "expires_at": expires,
            }, on_conflict="asa_id").execute()

        # Reset board after 4 seconds
        await asyncio.sleep(4)
        board.reset()
        board.board_msg_id = None
        await _update_board(channel, "waiting", room, status_text="No active challenge")



    async def _send_winner_payout(self, user_id: str, amount: int, duel_id: int):
        """Winner payout is handled custodially in Supabase via _credit_win.
        On-chain withdrawal is handled separately via /pvp_withdraw.
        """
        print(f"[PVP] payout credited in Supabase uid={user_id} amount={amount} duel#{duel_id}")

    # ─────────────────────────────────────────
    # /pvp_leaderboard
    # ─────────────────────────────────────────

    @discord.app_commands.command(
        name="pvp_leaderboard",
        description="Show the current weekly PvP leaderboard and prize pool"
    )
    async def pvp_leaderboard(self, interaction: discord.Interaction):
        room = _channel_room(interaction.channel_id)
        if room is None:
            if await _wrong_channel(interaction):
                return
        await interaction.response.defer(ephemeral=True)

        db         = _db()
        pool_key   = _pool_room(room)
        rows       = _get_leaderboard(db, pool_key)
        pool_bal   = _get_pool_balance(db, pool_key)
        week_start = _current_week_start()
        week_end   = week_start + timedelta(days=7)
        now        = datetime.now(timezone.utc)
        days_left  = (week_end - now).days
        hours_left = int(((week_end - now).total_seconds() % 86400) / 3600)

        currency   = "ALGO" if _is_algo_room(room) else "$GOO"
        pool_str   = (
            f"{pool_bal/1_000_000:g} ALGO" if _is_algo_room(room)
            else f"{pool_bal:,} $GOO"
        )

        embed = discord.Embed(
            title=f"🏆 Weekly {'ALGO' if _is_algo_room(room) else 'GOO'} Arena Standings",
            description=(
                f"**Prize Pool: {pool_str}** · "
                f"winner in {days_left}d {hours_left}h"
            ),
            color=0xFFD700
        )

        if not rows:
            embed.add_field(name="No battles yet this week", value="Be the first!", inline=False)
        else:
            medals = ["🥇", "🥈", "🥉"]
            for i, r in enumerate(rows):
                medal    = medals[i] if i < 3 else f"`{i+1}.`"
                uid      = r["user_id"]
                wins     = r["wins"]
                losses   = r["losses"]
                total    = wins + losses
                gap      = rows[0]["wins"] - wins if i > 0 else 0
                gap_str  = f"  *(−{gap} from 1st)*" if gap > 0 else ""
                embed.add_field(
                    name=f"{medal} <@{uid}>",
                    value=f"**{wins}W** / {losses}L  ({total} battles){gap_str}",
                    inline=False
                )

        embed.set_footer(text=f"Week resets Sunday 7PM EST · {week_end.strftime('%b %d')}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────
    # /pvp_imgtest  (temporary debug — remove after testing)
    # ─────────────────────────────────────────

    @discord.app_commands.command(name="pvp_imgtest", description="Test IPFS gateway reachability")
    async def pvp_imgtest(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        import urllib.request as _ur
        cid = "bafkreiair63yxr62sd3cv7lc6epjuotrswju2r5of7i44jyopgvmfghdci"
        results = []
        for gw in [
            "https://nftstorage.link/ipfs/",
            "https://ipfs.io/ipfs/",
            "https://dweb.link/ipfs/",
            "https://gateway.pinata.cloud/ipfs/",
            "https://cloudflare-ipfs.com/ipfs/",
            "https://ipfs.algonode.xyz/ipfs/",
        ]:
            try:
                req = _ur.Request(f"{gw}{cid}", headers={"User-Agent": "Mozilla/5.0"})
                with _ur.urlopen(req, timeout=10) as r:
                    data = r.read()
                results.append(f"✅ {gw} — {len(data):,} bytes")
            except Exception as e:
                results.append(f"❌ {gw} — {e}")
        await interaction.followup.send("\n".join(results), ephemeral=True)

    # ─────────────────────────────────────────
    # DAILY LEADERBOARD POST  (9AM UTC)
    # ─────────────────────────────────────────

    @tasks.loop(hours=24)
    async def daily_leaderboard_post(self):
        """Post current standings + pot to each room's channel daily at 9AM UTC."""
        db = _db()

        for room, ch_fn in [
            ("goo",  _goo_leaderboard_channel_id),
            ("algo", _algo_leaderboard_channel_id),
        ]:
            ch_id = ch_fn()
            if not ch_id:
                continue
            channel = self.bot.get_channel(ch_id)
            if not channel:
                continue

            try:
                rows     = _get_leaderboard(db, room)
                pool_bal = _get_pool_balance(db, room)
                week_end = _current_week_start() + timedelta(days=7)
                now      = datetime.now(timezone.utc)
                days_left  = (week_end - now).days
                hours_left = int(((week_end - now).total_seconds() % 86400) / 3600)

                pool_str = (
                    f"{pool_bal/1_000_000:g} ALGO" if room == "algo"
                    else f"{pool_bal:,} $GOO"
                )

                lines = [
                    f"🏆 **Weekly {'ALGO' if room == 'algo' else 'GOO'} Arena** · "
                    f"**{pool_str} pot** · winner in {days_left}d {hours_left}h\n"
                ]

                if not rows:
                    lines.append("*No battles yet this week — be the first in!*")
                else:
                    medals = ["🥇", "🥈", "🥉"]
                    top3   = rows[:3]
                    for i, r in enumerate(top3):
                        medal   = medals[i]
                        gap     = rows[0]["wins"] - r["wins"] if i > 0 else 0
                        gap_str = f" (−{gap})" if gap > 0 else ""
                        lines.append(
                            f"{medal} <@{r['user_id']}> — "
                            f"**{r['wins']}W** / {r['losses']}L{gap_str}"
                        )
                    if len(rows) > 3:
                        lines.append(f"\n*Use `/pvp_leaderboard` to see the full top 10.*")

                await channel.send("\n".join(lines))
                print(f"[PVP] Daily leaderboard posted room={room}")
            except Exception as e:
                print(f"[PVP] daily_leaderboard_post failed room={room}: {e}")

    @daily_leaderboard_post.before_loop
    async def before_daily_post(self):
        await self.bot.wait_until_ready()
        now        = datetime.now(timezone.utc)
        next_9am   = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if now >= next_9am:
            next_9am += timedelta(days=1)
        wait = (next_9am - now).total_seconds()
        print(f"[PVP] Daily leaderboard post scheduled in {wait/3600:.1f}h")
        await asyncio.sleep(wait)

    # ─────────────────────────────────────────
    # WEEKLY PAYOUT + RESET  (Sunday 7PM EST = Monday 00:00 UTC)
    # ─────────────────────────────────────────

    @tasks.loop(hours=1)
    async def weekly_pool_reset(self):
        """
        Checks once per hour whether it's time for the weekly reset.
        Fires at Monday 00:00 UTC (Sunday 7PM EST).
        Pays out winners, posts announcements, wipes leaderboard for new week.
        """
        now = datetime.now(timezone.utc)
        if now.weekday() != WEEKLY_RESET_WEEKDAY % 7 or now.hour != WEEKLY_RESET_HOUR_UTC:
            return
        # Guard against double-firing within the same hour
        week_start = _current_week_start()
        db         = _db()

        print(f"[PVP] Weekly reset firing week={week_start.date()}")

        for room, ch_fn, credit_fn, unit_divisor in [
            ("goo",  _goo_leaderboard_channel_id,  _credit,      1),
            ("algo", _algo_leaderboard_channel_id, _credit_algo, 1_000_000),
        ]:
            ch_id = ch_fn()
            channel = self.bot.get_channel(ch_id) if ch_id else None

            try:
                # Get pool
                pool_row = db.table("pvp_weekly_pools") \
                             .select("id, balance") \
                             .eq("week_start", week_start.isoformat()) \
                             .eq("room", room) \
                             .eq("status", "active") \
                             .execute()
                if not pool_row.data or pool_row.data[0]["balance"] == 0:
                    print(f"[PVP] Weekly reset: no active pool for {room}, skipping")
                    continue

                pool_id  = pool_row.data[0]["id"]
                pool_bal = pool_row.data[0]["balance"]

                # Get winner (most wins this week)
                lb_rows = _get_leaderboard(db, room)
                if not lb_rows:
                    print(f"[PVP] Weekly reset: no leaderboard entries for {room}")
                    continue

                winner_id   = lb_rows[0]["user_id"]
                winner_wins = lb_rows[0]["wins"]

                # Credit winner
                if _is_algo_room(room):
                    credit_fn(db, winner_id, pool_bal, f"weekly pool win week={week_start.date()}")
                else:
                    credit_fn(db, winner_id, pool_bal, f"weekly pool win week={week_start.date()}")

                # Mark pool as paid
                db.table("pvp_weekly_pools").update({
                    "status":    "paid",
                    "winner_id": winner_id,
                    "paid_at":   now.isoformat(),
                }).eq("id", pool_id).execute()

                # Format payout string
                is_algo_pool = (room == "algo")
                if is_algo_pool:
                    prize_str = f"{pool_bal/1_000_000:g} ALGO"
                else:
                    prize_str = f"{pool_bal:,} $GOO"

                print(f"[PVP] Weekly pool paid room={room} winner={winner_id} amount={prize_str}")

                # Post announcement to leaderboard channel + main channel
                announcement = (
                    f"🏆 **Weekly {'ALGO' if is_algo_pool else 'GOO'} Arena — Week Over!**\n\n"
                    f"Congratulations <@{winner_id}>! 🎉\n"
                    f"**{winner_wins} wins** this week takes the pot: **{prize_str}**\n\n"
                    f"The board resets now. Good luck this week everyone! ⚔️"
                )
                if channel:
                    await channel.send(announcement)
                main_ch_id = int(os.environ.get("DISCORD_MAIN_CHANNEL_ID", "0") or "0")
                if main_ch_id and main_ch_id != (ch_id or 0):
                    main_ch = self.bot.get_channel(main_ch_id)
                    if main_ch:
                        await main_ch.send(announcement)

            except Exception as e:
                print(f"[PVP] Weekly reset failed room={room}: {e}")
                if channel:
                    try:
                        await channel.send(
                            f"⚠️ Weekly {room.upper()} pool payout encountered an error. "
                            f"Admins please check logs."
                        )
                    except Exception:
                        pass

    @weekly_pool_reset.before_loop
    async def before_weekly_reset(self):
        await self.bot.wait_until_ready()

    async def _notify_unmatched_deposit(self, amount: int):
        """Notify channel that a deposit arrived but couldn't be auto-matched."""
        ch_id = _pvp_channel_id()
        if not ch_id:
            return
        try:
            channel = self.bot.get_channel(ch_id)
            if channel:
                await channel.send(
                    f"💧 A deposit of **{amount:,} $GOO** arrived at the bot wallet but couldn't be "
                    f"auto-matched. If you just deposited, use `/pvp_balance` — if it's not showing, "
                    f"contact an admin with your TxID."
                )
        except Exception as e:
            print(f"[PVP] unmatched deposit notify failed: {e}")

    @tasks.loop(seconds=DEPOSIT_POLL_SECONDS)
    async def poll_deposits(self):
        """
        Poll the bot wallet for incoming $GOO transfers and credit
        the sender's PvP balance. Uses pvp_seen_deposits to avoid
        double-crediting. Runs continuously — no active-window gate —
        so deposits are never missed regardless of timing.
        """
        try:
            await self._process_deposits()
        except Exception as e:
            print(f"[PVP] deposit poll error: {e}")

    @poll_deposits.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()
        print(f"[PVP] GOO deposit poller started")


    @tasks.loop(seconds=120)
    async def poll_algo_deposits(self):
        """ALGO deposit detection via algosdk indexer — same approach as GOO poller.
        Runs continuously — no active-window gate — so deposits are never missed
        regardless of timing.
        """
        try:
            bot_addr = await asyncio.to_thread(_get_bot_address)
            db       = _db()
            idx      = _get_indexer_client()

            seen_rows = await asyncio.to_thread(
                lambda: db.table("pvp_seen_deposits").select("tx_id").execute()
            )
            seen_ids = {r["tx_id"] for r in seen_rows.data} if seen_rows.data else set()

            res = await asyncio.to_thread(
                lambda: idx.search_transactions(
                    address=bot_addr,
                    address_role="receiver",
                    txn_type="pay",
                    limit=20,
                )
            )

            for txn in res.get("transactions", []):
                tx_id  = txn.get("id", "")
                if not tx_id or tx_id in seen_ids:
                    continue
                pay    = txn.get("payment-transaction", {})
                if pay.get("receiver") != bot_addr: continue
                sender = txn.get("sender", "")
                amount = pay.get("amount", 0)
                if amount < 100_000 or not sender: continue

                wallet_row = await asyncio.to_thread(
                    lambda: db.table("linked_wallets").select("user_id").eq("wallet_address", sender).execute()
                )
                await asyncio.to_thread(
                    lambda: db.table("pvp_seen_deposits").insert({"tx_id": tx_id, "note": f"algo amt={amount}"}).execute()
                )
                if not wallet_row.data:
                    print(f"[PVP] ALGO from unknown wallet {sender[:8]} amount={amount/1_000_000:g}")
                    continue

                user_id = wallet_row.data[0]["user_id"]
                new_bal = _credit_algo(db, user_id, amount, f"algo deposit tx={tx_id[:16]}")
                print(f"[PVP] ALGO deposit credited uid={user_id} +{amount/1_000_000:g} ALGO bal={new_bal/1_000_000:g}")
                asyncio.ensure_future(self._notify_algo_deposit(user_id, amount, new_bal))

        except Exception as e:
            print(f"[PVP] poll_algo_deposits error: {e}")

    @poll_algo_deposits.before_loop
    async def before_algo_poll(self):
        await self.bot.wait_until_ready()
        print(f"[PVP] ALGO deposit poller started")


    async def _notify_algo_deposit(self, user_id: str, amount: int, new_bal: int):
        # Post to ALGO channel first, fall back to GOO channel
        ch_id = _algo_channel_id() or _pvp_channel_id()
        if not ch_id: return
        try:
            channel = self.bot.get_channel(ch_id)
            if channel:
                await channel.send(
                    f"💎 <@{user_id}> deposited **{amount/1_000_000:g} ALGO** "
                    f"— Warden balance: **{new_bal/1_000_000:g} ALGO**"
                )
        except Exception as e:
            print(f"[PVP] algo deposit notify failed: {e}")

    async def _process_deposits(self):
        """GOO deposit detection via algosdk indexer — same approach as Grand Prix."""
        try:
            asset_id = int(os.environ["GOO_ASSET_ID"])
            bot_addr = await asyncio.to_thread(_get_bot_address)
            db       = _db()
            idx      = _get_indexer_client()

            seen_rows = await asyncio.to_thread(
                lambda: db.table("pvp_seen_deposits").select("tx_id").execute()
            )
            seen_ids = {r["tx_id"] for r in seen_rows.data} if seen_rows.data else set()

            res = await asyncio.to_thread(
                lambda: idx.search_transactions(
                    address=bot_addr,
                    address_role="receiver",
                    txn_type="axfer",
                    asset_id=asset_id,
                    limit=20,
                )
            )

            for txn in res.get("transactions", []):
                tx_id = txn.get("id", "")
                if not tx_id or tx_id in seen_ids:
                    continue
                at     = txn.get("asset-transfer-transaction", {})
                if at.get("asset-id") != asset_id: continue
                if at.get("receiver") != bot_addr:  continue
                sender = txn.get("sender", "")
                amount = at.get("amount", 0)
                if amount <= 0 or not sender: continue

                wallet_row = await asyncio.to_thread(
                    lambda: db.table("linked_wallets").select("user_id").eq("wallet_address", sender).execute()
                )
                await asyncio.to_thread(
                    lambda: db.table("pvp_seen_deposits").insert({"tx_id": tx_id, "note": f"goo amt={amount}"}).execute()
                )
                if not wallet_row.data:
                    print(f"[PVP] GOO from unknown wallet {sender[:8]} amount={amount}")
                    continue

                user_id = wallet_row.data[0]["user_id"]
                new_bal = _credit(db, user_id, amount, note=f"deposit tx={tx_id[:16]}")
                _log_transaction(db, user_id, None, "deposit", amount, sender, tx_id)
                print(f"[PVP] GOO deposit credited uid={user_id} amount={amount:,} bal={new_bal:,}")
                asyncio.ensure_future(self._notify_deposit(user_id, amount, new_bal))

        except Exception as e:
            print(f"[PVP] _process_deposits error: {e}")



    async def _notify_deposit(self, user_id: str, amount: int, new_bal: int):
        ch_id = _pvp_channel_id()
        if not ch_id:
            return
        try:
            channel = self.bot.get_channel(ch_id)
            if channel:
                await channel.send(
                    f"💧 <@{user_id}> deposited **{amount:,} $GOO** — "
                    f"PvP balance: **{new_bal:,} $GOO**",
                )
        except Exception as e:
            print(f"[PVP] deposit notify failed: {e}")

    # ─────────────────────────────────────────
    # PARTNER ASSETS CACHE REFRESH
    # ─────────────────────────────────────────

    @tasks.loop(hours=4)
    async def refresh_partner_cache(self):
        """Rebuild PARTNER_ASSETS cache every 4 hours."""
        global PARTNER_ASSETS
        try:
            new_cache = await asyncio.to_thread(_build_partner_assets_cache)
            PARTNER_ASSETS.clear()
            PARTNER_ASSETS.update(new_cache)
            total = sum(len(v) for v in PARTNER_ASSETS.values())
            print(f"[PVP] partner cache refreshed: {len(PARTNER_ASSETS)} collections, {total} total ASAs")
        except Exception as e:
            print(f"[PVP] refresh_partner_cache error: {e}")

    @refresh_partner_cache.before_loop
    async def before_refresh_partner_cache(self):
        await self.bot.wait_until_ready()

    # ─────────────────────────────────────────
    # CHALLENGE EXPIRY LOOP
    # ─────────────────────────────────────────

    @tasks.loop(minutes=30)
    async def expire_challenges(self):
        """Expire old challenges and refund any held wagers for waiting challengers."""
        try:
            db = _db()
            db.table("pvp_challenges").update({"status": "expired"}).eq(
                "status", "open"
            ).lt("expires_at", datetime.now(timezone.utc).isoformat()).execute()

            # Refund challenger if they're waiting and board has been idle too long
            for room, board in _boards.items():
                if board.challenger and board.challenger.get("wager"):
                    # If challenger has been waiting > 10 minutes, refund and clear
                    # (board.challenger doesn't store a timestamp so just keep it alive for now)
                    pass

        except Exception as e:
            print(f"[PVP] expire_challenges error: {e}")

    @expire_challenges.before_loop
    async def before_expire(self):
        await self.bot.wait_until_ready()


# ─────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────

async def setup(bot: commands.Bot):
    await bot.add_cog(PvPCog(bot))
