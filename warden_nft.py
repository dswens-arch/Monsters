"""
warden_nft.py
─────────────
Guillotoons X MONSTRS — Warden Tier NFT system.

Two responsibilities:
  1. Special moves in battle — checked once per player per encounter on first hit,
     cached on EncounterState. Each tier NFT unlocks a unique damaging move (no stun).
     Holding multiple NFTs stacks the chances.

  2. Auto-distribution — when a player reaches a tier for the first time, Warden
     sends the corresponding NFT to their linked wallet. 69 supply per tier, forever.

Special moves (pure damage, no stun):
  Scrapper  → Spite Strike    (+15% base damage, 12% trigger chance)
  Fighter   → Iron Volley     (+25% base damage, 12% trigger chance)
  Veteran   → Killshot        (+40% base damage, 12% trigger chance)
  Warlord   → Reign of Ruin   (+60% base damage, 12% trigger chance)

Each NFT rolls independently — holding all four gives four separate chances per attack.

Environment variables required:
  SUPABASE_URL
  SUPABASE_KEY          (service_role)
  BOT_MNEMONIC          (25-word hot wallet mnemonic — same as GOO wallet)
  ALGOD_URL             (default: https://mainnet-api.algonode.cloud)
  ALGOD_TOKEN           (default: "")
  WARDEN_ANNOUNCE_CHANNEL_ID   (channel for NFT award announcements)
"""

import os
import random
import asyncio
import logging
import urllib.request
import json

from supabase import create_client

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# TIER NFT CONFIG
# ─────────────────────────────────────────────

TIER_NFTS = {
    "scrapper": {
        "asset_id":    3574696537,
        "move_name":   "Spite Strike",
        "move_emoji":  "😤",
        "dmg_bonus":   0.15,   # +15% on top of base damage
        "trigger_pct": 0.12,   # 12% chance per attack
        "supply":      69,
    },
    "fighter": {
        "asset_id":    3574701634,
        "move_name":   "Iron Volley",
        "move_emoji":  "🔩",
        "dmg_bonus":   0.25,
        "trigger_pct": 0.12,
        "supply":      69,
    },
    "veteran": {
        "asset_id":    3574705357,
        "move_name":   "Killshot",
        "move_emoji":  "🎯",
        "dmg_bonus":   0.40,
        "trigger_pct": 0.12,
        "supply":      69,
    },
    "warlord": {
        "asset_id":    None,       # TODO: fill in after mint
        "move_name":   "Reign of Ruin",
        "move_emoji":  "💀",
        "dmg_bonus":   0.60,
        "trigger_pct": 0.12,
        "supply":      69,
    },
}

# Reverse lookup: asset_id → tier key
ASSET_TO_TIER = {
    v["asset_id"]: k
    for k, v in TIER_NFTS.items()
    if v["asset_id"] is not None
}


# ─────────────────────────────────────────────
# WALLET CHECK — which tier NFTs does this wallet hold?
# ─────────────────────────────────────────────

def fetch_held_tier_nfts(wallet_address: str) -> set[str]:
    """
    Returns a set of tier keys the wallet currently holds, e.g. {'scrapper', 'veteran'}.
    Called once per player per encounter on their first attack.
    Synchronous — wrap in asyncio.to_thread() at the call site.
    """
    try:
        algod_url = os.getenv("ALGOD_URL", "https://mainnet-api.algonode.cloud")
        url = f"{algod_url}/v2/accounts/{wallet_address}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "X-Algo-API-Token": os.getenv("ALGOD_TOKEN", ""),
        })
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read())

        held_asset_ids = {
            a["asset-id"]
            for a in data.get("assets", [])
            if a.get("amount", 0) > 0
        }

        tiers_held = {
            ASSET_TO_TIER[aid]
            for aid in held_asset_ids
            if aid in ASSET_TO_TIER
        }

        if tiers_held:
            logger.info(f"[WARDEN NFT] {wallet_address[:8]}... holds tier NFTs: {tiers_held}")
        return tiers_held

    except Exception as e:
        logger.warning(f"[WARDEN NFT] fetch_held_tier_nfts failed for {wallet_address[:8]}...: {e}")
        return set()


# ─────────────────────────────────────────────
# SPECIAL MOVE ROLLER
# ─────────────────────────────────────────────

def roll_special_moves(held_tiers: set[str], base_damage: int) -> list[tuple[str, str, int]]:
    """
    Each held tier NFT rolls independently for its special move.
    Returns a list of (tier_key, move_name, bonus_damage) for every move that triggered.
    Most attacks return an empty list. Multiple can fire in one hit.

    Args:
        held_tiers:  set of tier keys the attacker holds, e.g. {'scrapper', 'fighter'}
        base_damage: the damage already calculated for this attack

    Returns:
        list of (tier_key, move_name, bonus_damage) — empty if nothing triggered
    """
    triggered = []
    for tier_key in held_tiers:
        cfg = TIER_NFTS.get(tier_key)
        if not cfg:
            continue
        if random.random() < cfg["trigger_pct"]:
            bonus = int(base_damage * cfg["dmg_bonus"])
            triggered.append((tier_key, cfg["move_name"], bonus))
    return triggered


# ─────────────────────────────────────────────
# NFT AWARD — send tier NFT on first tier-up
# ─────────────────────────────────────────────

def _get_supabase():
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def _check_opted_in_to_asset(wallet_address: str, asset_id: int) -> bool:
    """Returns True if the wallet has opted into the given ASA."""
    try:
        algod_url = os.getenv("ALGOD_URL", "https://mainnet-api.algonode.cloud")
        url = f"{algod_url}/v2/accounts/{wallet_address}/assets/{asset_id}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "X-Algo-API-Token": os.getenv("ALGOD_TOKEN", ""),
        })
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
        return True
    except Exception as e:
        if "404" in str(e) or "HTTP Error 404" in str(e):
            return False
        logger.warning(f"[WARDEN NFT] opt-in check error: {e}")
        return False


def _transfer_nft(recipient: str, asset_id: int) -> str:
    """
    Transfers 1 unit of the tier NFT from the bot hot wallet to recipient.
    Returns confirmed transaction ID.
    Synchronous — wrap in asyncio.to_thread() at the call site.
    """
    from algosdk import mnemonic, account, transaction
    from algosdk.v2client import algod

    client = algod.AlgodClient(
        algod_token=os.getenv("ALGOD_TOKEN", ""),
        algod_address=os.getenv("ALGOD_URL", "https://mainnet-api.algonode.cloud"),
    )
    mn = os.environ["BOT_MNEMONIC"]
    private_key = mnemonic.to_private_key(mn)
    sender = account.address_from_private_key(private_key)

    sp = client.suggested_params()
    txn = transaction.AssetTransferTxn(
        sender=sender,
        sp=sp,
        receiver=recipient,
        amt=1,
        index=asset_id,
        note=b"Guillotoons X MONSTRS | Warden Tier NFT",
    )
    signed = txn.sign(private_key)
    tx_id = client.send_transaction(signed)
    transaction.wait_for_confirmation(client, tx_id, wait_rounds=8)
    return tx_id


async def award_tier_nft(discord_id: str, tier: str, bot=None):
    """
    Call this immediately after a player's tier changes for the first time.
    Handles all guards — duplicate, supply, opt-in — then sends on-chain.

    Args:
        discord_id:  Discord user ID as string
        tier:        tier key, e.g. 'scrapper', 'fighter', 'veteran', 'warlord'
        bot:         discord.py Bot instance (for announcements)
    """
    cfg = TIER_NFTS.get(tier)
    if not cfg:
        return  # 'raw' or unknown — no NFT

    asset_id = cfg["asset_id"]
    if asset_id is None:
        logger.info(f"[WARDEN NFT] {tier} ASA not configured yet — skipping award")
        return

    try:
        db = _get_supabase()

        # 1. Duplicate guard — already awarded this tier NFT?
        existing = await asyncio.to_thread(
            lambda: db.table("warden_nft_awards")
                .select("id")
                .eq("discord_id", discord_id)
                .eq("tier", tier)
                .execute()
        )
        if existing.data:
            logger.info(f"[WARDEN NFT] {discord_id} already has {tier} NFT — skipping")
            return

        # 2. Supply check — 69 hard cap
        supply_row = await asyncio.to_thread(
            lambda: db.table("warden_nft_supply")
                .select("claimed, total_supply")
                .eq("tier", tier)
                .single()
                .execute()
        )
        supply = supply_row.data
        if supply["claimed"] >= supply["total_supply"]:
            logger.info(f"[WARDEN NFT] {tier} supply exhausted ({supply['claimed']}/69)")
            await _notify_supply_exhausted(tier, bot)
            return

        # 3. Wallet lookup
        wallet_row = await asyncio.to_thread(
            lambda: db.table("linked_wallets")
                .select("wallet_address")
                .eq("user_id", discord_id)
                .execute()
        )
        if not wallet_row.data or not wallet_row.data[0].get("wallet_address"):
            logger.warning(f"[WARDEN NFT] No wallet for {discord_id} — cannot send {tier} NFT")
            return

        recipient_wallet = wallet_row.data[0]["wallet_address"]

        # 4. Opt-in check — if not opted in, queue for retry and DM the user
        opted_in = await asyncio.to_thread(_check_opted_in_to_asset, recipient_wallet, asset_id)
        if not opted_in:
            logger.info(f"[WARDEN NFT] {recipient_wallet[:8]}... not opted into ASA {asset_id} — queuing")
            queue_pending_nft(discord_id, tier)
            await _notify_opt_in_required(discord_id, tier, asset_id, bot)
            return

        # 5. Send on-chain
        tx_id = await asyncio.wait_for(
            asyncio.to_thread(_transfer_nft, recipient_wallet, asset_id),
            timeout=30
        )
        logger.info(f"[WARDEN NFT] Sent {tier} NFT (ASA {asset_id}) → {recipient_wallet[:8]}... | tx: {tx_id}")

        # 6. Log the award
        await asyncio.to_thread(
            lambda: db.table("warden_nft_awards").insert({
                "discord_id":     discord_id,
                "wallet_address": recipient_wallet,
                "tier":           tier,
                "asset_id":       asset_id,
            }).execute()
        )

        # 7. Increment claimed count
        # NOTE: this RPC returns void — postgrest throws a JSON parse error on the
        # empty 204 response even though the UPDATE executed successfully. We catch
        # and ignore it; the supply count is updated in the DB regardless.
        try:
            await asyncio.to_thread(
                lambda: db.rpc("increment_nft_claimed", {"tier_name": tier}).execute()
            )
        except Exception:
            pass

        # 8. Announce
        remaining = supply["total_supply"] - supply["claimed"] - 1
        await _announce_award(discord_id, tier, cfg, tx_id, remaining, bot)

    except asyncio.TimeoutError:
        logger.error(f"[WARDEN NFT] Timeout sending {tier} NFT to {discord_id}")
    except Exception as e:
        logger.error(f"[WARDEN NFT] award_tier_nft failed for {discord_id}/{tier}: {e}", exc_info=True)


# ─────────────────────────────────────────────
# RETROACTIVE NFT SEND — any tier
# ─────────────────────────────────────────────

# Tiers in progression order — used to determine which tier NFTs a player is owed
TIER_ORDER = ["scrapper", "fighter", "veteran", "warlord"]

async def retroactive_nft_send(bot, channel, tier: str = "scrapper"):
    """
    Admin utility — finds all players at or above `tier` who haven't received
    that tier's NFT yet, and sends it to them.

    Wire to /retroactivenft admin command. Safe to run multiple times —
    duplicate guard in award_tier_nft prevents double-sends.

    Args:
        bot:     discord.py Bot instance
        channel: channel to post progress updates to
        tier:    which tier NFT to distribute (default: 'scrapper')
    """
    if tier not in TIER_NFTS:
        await channel.send(f"❌ Unknown tier `{tier}`. Valid: {', '.join(TIER_ORDER)}")
        return

    cfg = TIER_NFTS[tier]
    if cfg["asset_id"] is None:
        await channel.send(f"❌ `{tier}` ASA not configured yet — cannot send.")
        return

    db = _get_supabase()

    try:
        # Players at this tier or higher
        eligible_tiers = TIER_ORDER[TIER_ORDER.index(tier):]
        teams = await asyncio.to_thread(
            lambda: db.table("monstr_teams")
                .select("user_id, tier")
                .in_("tier", eligible_tiers)
                .execute()
        )

        # Who already has this tier's NFT
        awarded = await asyncio.to_thread(
            lambda: db.table("warden_nft_awards")
                .select("discord_id")
                .eq("tier", tier)
                .execute()
        )
        already_awarded = {r["discord_id"] for r in awarded.data}

        eligible = [
            r["user_id"] for r in teams.data
            if str(r["user_id"]) not in already_awarded
        ]

        if not eligible:
            await channel.send(f"✅ No players are missing the **{tier.capitalize()}** NFT — all caught up.")
            return

        await channel.send(
            f"🔄 Retroactive **{tier.capitalize()}** drop — **{len(eligible)}** player(s) found. Starting..."
        )

        for discord_id in eligible:
            await award_tier_nft(str(discord_id), tier, bot)
            await asyncio.sleep(2)  # pace on-chain sends

        await channel.send(
            f"✅ Retroactive **{tier.capitalize()}** drop complete — **{len(eligible)}** processed."
        )

    except Exception as e:
        logger.error(f"[WARDEN NFT] retroactive_nft_send({tier}) failed: {e}", exc_info=True)
        await channel.send(f"❌ Retroactive drop error: {e}")


# ─────────────────────────────────────────────
# PENDING QUEUE — retry failed opt-in sends
# ─────────────────────────────────────────────

def queue_pending_nft(discord_id: str, tier: str):
    """
    Store a failed NFT send (opt-in not done yet) for later retry.
    Called when award_tier_nft hits the opt-in wall.
    """
    try:
        db = _get_supabase()
        db.table("warden_nft_pending").upsert({
            "discord_id": discord_id,
            "tier":       tier,
        }, on_conflict="discord_id,tier").execute()
        logger.info(f"[WARDEN NFT] Queued pending {tier} NFT for {discord_id}")
    except Exception as e:
        logger.error(f"[WARDEN NFT] queue_pending_nft failed: {e}")


async def process_pending_nfts():
    """
    Retry loop — call this on a background task every 30 minutes.
    Checks all pending NFTs, attempts to send any where opt-in is now done.
    """
    try:
        db = _get_supabase()
        pending = await asyncio.to_thread(
            lambda: db.table("warden_nft_pending").select("discord_id, tier").execute()
        )
        if not pending.data:
            return

        logger.info(f"[WARDEN NFT] Retrying {len(pending.data)} pending NFT(s)...")

        for row in pending.data:
            discord_id = row["discord_id"]
            tier = row["tier"]
            cfg = TIER_NFTS.get(tier)
            if not cfg or not cfg["asset_id"]:
                continue

            # Check if wallet is now opted in
            try:
                wallet_row = await asyncio.to_thread(
                    lambda: db.table("linked_wallets")
                        .select("wallet_address")
                        .eq("user_id", discord_id)
                        .execute()
                )
                if not wallet_row.data or not wallet_row.data[0].get("wallet_address"):
                    continue

                wallet = wallet_row.data[0]["wallet_address"]
                opted_in = await asyncio.to_thread(
                    _check_opted_in_to_asset, wallet, cfg["asset_id"]
                )
                if not opted_in:
                    continue  # still not opted in — leave in queue

                # Opted in now — remove from pending and send
                await asyncio.to_thread(
                    lambda: db.table("warden_nft_pending")
                        .delete()
                        .eq("discord_id", discord_id)
                        .eq("tier", tier)
                        .execute()
                )
                logger.info(f"[WARDEN NFT] Opt-in detected for {discord_id}/{tier} — sending now")
                await award_tier_nft(discord_id, tier)

            except Exception as e:
                logger.warning(f"[WARDEN NFT] Retry failed for {discord_id}/{tier}: {e}")

    except Exception as e:
        logger.error(f"[WARDEN NFT] process_pending_nfts error: {e}", exc_info=True)


# ─────────────────────────────────────────────
# DISCORD NOTIFICATIONS
# ─────────────────────────────────────────────

async def _announce_award(discord_id, tier, cfg, tx_id, remaining, bot):
    announce_channel_id = int(os.getenv("WARDEN_ANNOUNCE_CHANNEL_ID", "0"))
    if not bot or not announce_channel_id:
        return

    channel = bot.get_channel(announce_channel_id)
    if not channel:
        return

    tier_display = tier.capitalize()
    emoji = cfg["move_emoji"]

    await channel.send(
        f"{emoji} **Guillotoons X MONSTRS drop!**\n"
        f"<@{discord_id}> just reached **{tier_display}** and earned a rare NFT.\n"
        f"They've unlocked the **{cfg['move_name']}** special move in battle.\n"
        f"Only **{remaining}** remaining in the {tier_display} edition (69 total, forever).\n"
        f"🔗 [View on Allo](https://allo.info/tx/{tx_id})"
    )


OPTIN_URL = "https://www.wen.tools/bulk-asset-manager?tab=optin&ids=3574705357,3574701634,3574696537"

async def _notify_opt_in_required(discord_id, tier, asset_id, bot):
    if not bot:
        return
    try:
        user = await bot.fetch_user(int(discord_id))
        tier_display = tier.capitalize()
        cfg = TIER_NFTS[tier]
        await user.send(
            f"🎖️ Congrats on reaching **{tier_display}** in the Warden system!\n\n"
            f"You've earned a **Guillotoons X MONSTRS** NFT, but your wallet hasn't opted "
            f"into the asset yet.\n\n"
            f"**Opt in here (takes 30 seconds):**\n"
            f"{OPTIN_URL}\n\n"
            f"Once you opt in, your NFT will be sent automatically within 30 minutes — no need to do anything else.\n\n"
            f"Holding it will also unlock the **{cfg['move_name']}** special move during encounters."
        )
    except Exception as e:
        logger.warning(f"[WARDEN NFT] DM failed for {discord_id}: {e}")


async def _notify_supply_exhausted(tier, bot):
    announce_channel_id = int(os.getenv("WARDEN_ANNOUNCE_CHANNEL_ID", "0"))
    if not bot or not announce_channel_id:
        return
    channel = bot.get_channel(announce_channel_id)
    if channel:
        await channel.send(
            f"💀 The **{tier.capitalize()}** Guillotoons X MONSTRS edition is **gone**. "
            f"All 69 have been claimed. That design will never be sent again."
        )
