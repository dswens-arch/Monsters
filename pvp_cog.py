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
from pvp_board import BoardPlayer, render_board
from pvp_board_result import WinnerInfo, render_result


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

GOO_WAGER_1V1      = 500        # per player
GOO_WINNER_CUT_1V1 = 900        # winner receives (out of 1000 pot)
GOO_TREASURY_1V1   = 100        # stays in bot wallet as treasury

ALGO_WAGER_1V1     = 5_000_000  # 5 ALGO in microALGO
ALGO_WINNER_CUT    = 9_000_000  # 9 ALGO to winner
ALGO_TREASURY      = 1_000_000  # 1 ALGO treasury

CHALLENGE_TTL_HOURS = 24
DEPOSIT_POLL_SECONDS = 30       # how often to check for new deposits


# ─────────────────────────────────────────────
# DB
# ─────────────────────────────────────────────

def _db() -> Client:
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


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
                     amount: int, wallet: str = "", tx_id: str = "", note: str = ""):
    try:
        db.table("pvp_transactions").insert({
            "user_id":        user_id,
            "duel_id":        duel_id,
            "type":           txn_type,
            "amount":         amount,
            "room":           "goo",
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


# ─────────────────────────────────────────────
# ARC-19 IMAGE URL RESOLVER
# ─────────────────────────────────────────────

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
        for gw in ["https://ipfs.algonode.xyz/ipfs/", "https://ipfs.io/ipfs/", "https://dweb.link/ipfs/"]:
            try:
                req2 = urllib.request.Request(f"{gw}{metadata_cid}",
                    headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
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
        img = r.get("image_url") or None  # stored at registration time
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


# ─────────────────────────────────────────────
# BOARD HELPERS
# ─────────────────────────────────────────────

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

def _pvp_channel_id() -> Optional[int]:
    val = os.environ.get("DISCORD_PVP_CHANNEL_ID", "")
    return int(val) if val else None

def _algo_channel_id() -> Optional[int]:
    val = os.environ.get("DISCORD_ALGO_CHANNEL_ID", "")
    return int(val) if val else None

def _channel_room(channel_id: int) -> Optional[str]:
    """Return 'goo' or 'algo' based on channel."""
    if channel_id == _pvp_channel_id(): return "goo"
    if channel_id == _algo_channel_id(): return "algo"
    return None



async def _wrong_channel(interaction: discord.Interaction) -> bool:
    room = _channel_room(interaction.channel_id)
    if room is None:
        goo_id  = _pvp_channel_id()
        algo_id = _algo_channel_id()
        channels = " or ".join([f"<#{c}>" for c in [goo_id, algo_id] if c])
        await interaction.response.send_message(
            f"PvP commands only work in {channels}.", ephemeral=True
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

_board      = BoardState()  # GOO room
_board_algo = BoardState()  # ALGO room

def _get_board(room: str) -> BoardState:
    return _board_algo if room == "algo" else _board


# ─────────────────────────────────────────────
# MONSTR PICKER VIEW  (ephemeral — only sender sees)
# ─────────────────────────────────────────────

class MonstrPickerView(discord.ui.View):
    """
    Shows up to 5 MONSTRs per page with prev/next navigation.
    Fires join_callback(interaction, asa_id) when a choice is made.
    """
    def __init__(self, monstr_rows: list[dict], join_callback, page: int = 0):
        super().__init__(timeout=120)
        self._all_rows = monstr_rows
        self._cb       = join_callback
        self._page     = page
        self._per_page = 5

        start = page * self._per_page
        page_rows = monstr_rows[start:start + self._per_page]

        for row in page_rows:
            disabled = row.get("disabled", False)
            btn = discord.ui.Button(
                label    = row["monstr_name"],
                style    = discord.ButtonStyle.secondary if disabled else discord.ButtonStyle.primary,
                custom_id= f"pick_{row['asa_id']}",
                disabled = disabled,
            )
            btn.callback = self._make_pick_cb(row["asa_id"])
            self.add_item(btn)

        # Nav row
        total_pages = (len(monstr_rows) - 1) // self._per_page + 1

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
        new_view = MonstrPickerView(self._all_rows, self._cb, self._page - 1)
        total = (len(self._all_rows) - 1) // 5 + 1
        await interaction.response.edit_message(
            content=f"Choose a MONSTR (page {self._page}/{total}):",
            view=new_view
        )

    async def _next_cb(self, interaction: discord.Interaction):
        new_view = MonstrPickerView(self._all_rows, self._cb, self._page + 1)
        total = (len(self._all_rows) - 1) // 5 + 1
        await interaction.response.edit_message(
            content=f"Choose a MONSTR (page {self._page + 2}/{total}):",
            view=new_view
        )

    async def _manual_cb(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ASAModal(self._cb))


class ASAModal(discord.ui.Modal, title="Enter your MONSTR ASA ID"):
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
    if room == "algo":
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

    # Fetch their registered MONSTRs
    rows = db.table("monstr_pvp_stats").select("asa_id,monstr_name") \
             .eq("owner_id", user_id).execute()
    if not rows.data:
        await interaction.followup.send(
            "No registered MONSTRs. Use `/pvp_register` first.", ephemeral=True)
        return

    # Check cooldowns and in-use for each MONSTR — grey out unavailable ones
    now = datetime.now(timezone.utc)
    # Fetch full stats for sorting by power (ATK+DEF+SPD)
    full_rows = db.table("monstr_pvp_stats").select("asa_id,monstr_name,attack,defense,speed")                   .eq("owner_id", user_id).execute()
    stat_map = {r["asa_id"]: r for r in (full_rows.data or [])}

    monstr_rows = []
    for r in rows.data:
        asa      = r["asa_id"]
        disabled = False
        suffix   = ""
        try:
            cd = db.table("pvp_cooldowns").select("expires_at").eq("asa_id", asa).execute()
            if cd.data:
                expires = datetime.fromisoformat(cd.data[0]["expires_at"])
                if expires > now:
                    mins = int((expires - now).total_seconds() / 60) + 1
                    disabled = True
                    suffix = f" ({mins}m)"
        except Exception:
            pass
        if not disabled:
            for b in [_board, _board_algo]:
                if b.challenger and b.challenger["asa_id"] == asa:
                    disabled = True
                    suffix = " (in queue)"
                    break
        sr = stat_map.get(asa, {})
        power = sr.get("attack", 0) + sr.get("defense", 0) + sr.get("speed", 0)
        monstr_rows.append({
            "asa_id":      asa,
            "monstr_name": r["monstr_name"] + suffix,
            "disabled":    disabled,
            "power":       power,
        })

    # Sort: available first by power desc, then disabled by power desc
    monstr_rows.sort(key=lambda r: (r["disabled"], -r["power"]))

    async def on_pick(pick_interaction: discord.Interaction, asa_id: str):
        await _on_monstr_picked(pick_interaction, asa_id, user_id, db, room)

    currency = f"{bal/1_000_000:g} ALGO" if room == "algo" else f"{bal:,} $GOO"
    view = MonstrPickerView(monstr_rows, on_pick)
    await interaction.followup.send(
        f"**Choose your MONSTR** ({currency} available):",
        view=view, ephemeral=True)


async def _on_monstr_picked(interaction: discord.Interaction,
                             asa_id: str, user_id: str, db, room: str = "goo"):
    """Called after a player picks their MONSTR."""
    await interaction.response.defer(ephemeral=True)
    board = _get_board(room)

    # Check MONSTR registration
    row = db.table("monstr_pvp_stats").select("*").eq("asa_id", str(asa_id)).execute()
    if not row.data:
        await interaction.followup.send(
            "That MONSTR isn't registered. Use `/pvp_register` first.", ephemeral=True)
        return
    if row.data[0]["owner_id"] != user_id:
        await interaction.followup.send("That MONSTR isn't yours.", ephemeral=True)
        return

    # Check 30-min cooldown
    cd_row = db.table("pvp_cooldowns").select("expires_at") \
               .eq("asa_id", str(asa_id)).execute()
    if cd_row.data:
        from datetime import datetime, timezone
        expires = datetime.fromisoformat(cd_row.data[0]["expires_at"])
        now     = datetime.now(timezone.utc)
        if expires > now:
            mins = int((expires - now).total_seconds() / 60) + 1
            await interaction.followup.send(
                f"That MONSTR is cooling down. Ready in **{mins} min**.", ephemeral=True)
            return

    # Check MONSTR not already queued (in-memory board state)
    for b in [_board, _board_algo]:
        if b.challenger and b.challenger["asa_id"] == str(asa_id):
            await interaction.followup.send(
                "That MONSTR is already waiting in a battle queue!", ephemeral=True)
            return

    # Check MONSTR not already in an active duel (Supabase)
    try:
        a1 = db.table("pvp_duels").select("id").eq("status", "active").eq("challenger_asa", str(asa_id)).execute()
        a2 = db.table("pvp_duels").select("id").eq("status", "active").eq("opponent_asa",   str(asa_id)).execute()
        if (a1.data or a2.data):
            await interaction.followup.send(
                "That MONSTR is already in an active battle!", ephemeral=True)
            return
    except Exception as e:
        print(f"[PVP] active duel check failed: {e}")


    stats = _load_stats(asa_id, user_id)
    if not stats:
        await interaction.followup.send("Couldn't load stats. Try again.", ephemeral=True)
        return

    username = interaction.user.display_name

    if board.is_empty:
        board.challenger = {
            "user_id":  user_id,
            "asa_id":   str(asa_id),
            "stats":    stats,
            "username": username,
            "room":     room,
        }
        await interaction.followup.send(
            f"**{stats.name}** is in the arena! Waiting for an opponent...",
            ephemeral=True)
        bp1 = _to_board_player(stats, username)
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
        if cog:
            asyncio.ensure_future(cog._run_board_battle(
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
            ))
        else:
            await interaction.channel.send("Battle system error — cog not found.")


# ─────────────────────────────────────────────
# BOARD UPDATE HELPER
# ─────────────────────────────────────────────

async def _update_board(channel, state: str, room: str = "goo",
                        p1=None, p2=None, status_text: str = ""):
    """Edit the persistent board message in-place for the given room."""
    board = _get_board(room)
    buf   = await asyncio.to_thread(render_board, state, p1, p2, status_text)
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


def _get_bot_goo_balance() -> int:
    """Return bot wallet GOO balance via algod (asset holding)."""
    import urllib.request, json as _json
    try:
        asset_id  = int(os.environ["GOO_ASSET_ID"])
        algod_url = os.getenv("ALGOD_URL", "https://mainnet-api.algonode.cloud")
        bot_addr  = _get_bot_address()
        url = f"{algod_url}/v2/accounts/{bot_addr}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "X-Algo-API-Token": os.getenv("ALGOD_TOKEN", ""),
        })
        with urllib.request.urlopen(req, timeout=8) as r:
            data = _json.loads(r.read())
        for asset in data.get("account", {}).get("assets", []):
            if asset.get("asset-id") == asset_id:
                return asset.get("amount", 0)
        return 0
    except Exception as e:
        if "403" not in str(e) and "404" not in str(e):
            print(f"[PVP] bot GOO balance check failed: {e}")
        return 0


class PvPCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._pending_duels: dict[str, dict] = {}
        self._last_bot_goo:  int = 0
        self._last_bot_algo: int = 0
        self._wallet_goo_cache: dict = {}
        self.poll_deposits.start()
        self.poll_algo_deposits.start()
        self.expire_challenges.start()
        # Register persistent view so it survives restarts
        self.bot.add_view(JoinBattleView())

    def cog_unload(self):
        self.poll_deposits.cancel()
        self.poll_algo_deposits.cancel()
        self.expire_challenges.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        """Reload persistent boards on redeploy."""
        await self._reload_boards()

    async def _reload_boards(self):
        """Re-attach JoinBattleView to stored board messages on startup."""
        await asyncio.sleep(3)  # give bot time to fully connect
        try:
            db = _db()
            for room, board, env_key in [
                ("goo",  _board,      "DISCORD_PVP_CHANNEL_ID"),
                ("algo", _board_algo, "DISCORD_ALGO_CHANNEL_ID"),
            ]:
                ch_val = os.environ.get(env_key, "")
                if not ch_val.strip().isdigit():
                    continue
                ch_id = int(ch_val)
                channel = self.bot.get_channel(ch_id)
                if not channel:
                    continue

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
                buf  = await asyncio.to_thread(render_board, "waiting", None, None, "No active challenge")
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
        description="Register a MONSTR for PvP battles"
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
            "🔍 Checking your wallet for MONSTRs...", ephemeral=True
        )

        try:
            asa_ids = await asyncio.wait_for(
                asyncio.to_thread(_fetch_monstr_asa_ids, wallet), timeout=20
            )
        except Exception as e:
            await interaction.edit_original_response(
                content="❌ Couldn't reach the chain. Try again in a moment."
            )
            return

        if not asa_ids:
            await interaction.edit_original_response(
                content="❌ No MONSTRs found in your linked wallet."
            )
            return

        # Build picker rows from on-chain holdings (top 5)
        monstr_rows = [
            {
                "asa_id":      asa,
                "monstr_name": MONSTR_ASSETS.get(asa, (f"MONSTR ...{asa[-4:]}",))[0]
            }
            for asa in asa_ids[:5]
        ]

        async def on_pick(pick_interaction: discord.Interaction, asa_id: str):
            await self._do_register(pick_interaction, asa_id, user_id, wallet)

        view = MonstrPickerView(monstr_rows, on_pick)
        await interaction.edit_original_response(
            content="**Choose a MONSTR to register for PvP:**",
            view=view
        )

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

        # Fetch their registered MONSTRs
        rows = db.table("monstr_pvp_stats").select("asa_id,monstr_name,attack,defense,speed") \
                 .eq("owner_id", user_id).execute()
        if not rows.data:
            await interaction.followup.send(
                "❌ You haven't registered any MONSTRs for PvP yet.\n\nUse `/pvp_register` to register one first, then `/pvp_upgrade` to level it up.", ephemeral=True
            )
            return

        # Show MONSTR picker
        async def on_pick(pick_interaction: discord.Interaction, asa_id: str):
            await self._show_stat_picker(pick_interaction, asa_id, user_id, db, balance)

        monstr_rows = [{"asa_id": r["asa_id"], "monstr_name": r["monstr_name"]} for r in rows.data]
        view = MonstrPickerView(monstr_rows, on_pick)
        await interaction.followup.send(
            f"**Choose a MONSTR to upgrade** (upgrades cost ALGO):",
            view=view, ephemeral=True
        )

    async def _show_stat_picker(self, interaction: discord.Interaction,
                                 asa_id: str, user_id: str, db, balance: int):
        """Show ATK / DEF / SPD upgrade buttons for chosen MONSTR."""
        await interaction.response.defer(ephemeral=True)

        row = db.table("monstr_pvp_stats").select("*").eq("asa_id", str(asa_id)).execute()
        if not row.data or row.data[0]["owner_id"] != user_id:
            await interaction.followup.send("❌ That MONSTR isn't registered for PvP yet. Use `/pvp_register` first, then come back to upgrade.", ephemeral=True)
            return

        r = row.data[0]
        stats = {
            "attack":  r["attack"],
            "defense": r["defense"],
            "speed":   r["speed"],
        }

        view = discord.ui.View(timeout=60)

        for stat, val in stats.items():
            capped      = not can_upgrade(val)
            algo_cost   = upgrade_cost_algo(val)
            cost_label  = upgrade_cost_algo_display(val)
            label       = f"{stat.upper()} {val}→{val+1}  ({cost_label})" if not capped else f"{stat.upper()} {val} MAX"
            enabled     = not capped  # ALGO paid on-chain, always show enabled if not maxed
            btn = discord.ui.Button(
                label    = label,
                style    = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary,
                disabled = not enabled,
                custom_id= f"upgrade_{asa_id}_{stat}",
            )
            async def make_cb(s=stat, v=val, ac=algo_cost):
                async def cb(intr: discord.Interaction):
                    await self._do_upgrade(intr, asa_id, s, v, ac, user_id, db)
                return cb
            btn.callback = await make_cb()
            view.add_item(btn)

        await interaction.followup.send(
            f"**{r['monstr_name']}** — choose a stat to upgrade (costs paid in ALGO)\n"
            f"ATK {r['attack']} | DEF {r['defense']} | SPD {r['speed']}",
            view=view, ephemeral=True
        )

    async def _do_upgrade(self, interaction: discord.Interaction,
                           asa_id: str, stat: str, current_val: int,
                           algo_cost_micro: int, user_id: str, db):
        """Deduct from custodial ALGO balance and apply upgrade instantly."""
        await interaction.response.defer(ephemeral=True)

        row = db.table("monstr_pvp_stats").select("monstr_name").eq("asa_id", str(asa_id)).execute()
        monstr_name = row.data[0]["monstr_name"] if row.data else asa_id
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

        db.table("monstr_pvp_stats").update({
            stat:         new_val,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("asa_id", str(asa_id)).execute()

        new_algo_bal = _get_algo_balance(db, user_id)
        stats = _load_stats(asa_id, user_id)

        embed = discord.Embed(
            title=f"MONSTR upgraded!",
            description=(
                f"**{stat.capitalize()}** {current_val} to **{new_val}**\n"
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
        print(f"[PVP] Upgrade: uid={user_id} {stat} {current_val}>{new_val}")

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
        description="View all your registered MONSTRs and their stats"
    )
    async def pvp_roster(self, interaction: discord.Interaction):
        if await _wrong_channel(interaction): return
        await interaction.response.defer(ephemeral=True)

        user_id = str(interaction.user.id)
        db      = _db()

        rows = db.table("monstr_pvp_stats").select("*").eq("owner_id", user_id).execute()
        if not rows.data:
            await interaction.followup.send(
                "You have no registered MONSTRs. Use `/pvp_register` to add one.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"Your MONSTRS Roster ({len(rows.data)} registered)",
            color=0x1D9E75
        )
        for r in rows.data:
            atk_cost = upgrade_cost_algo_display(r["attack"])
            def_cost = upgrade_cost_algo_display(r["defense"])
            spd_cost = upgrade_cost_algo_display(r["speed"])
            embed.add_field(
                name=r["monstr_name"],
                value=(
                    f"ATK `{r['attack']}` ({atk_cost}) | "
                    f"DEF `{r['defense']}` ({def_cost}) | "
                    f"SPD `{r['speed']}` ({spd_cost})\n"
                    f"ASA: `{r['asa_id']}`"
                ),
                inline=False
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # /pvp_stats
    # ─────────────────────────────────────────

    @discord.app_commands.command(
        name="pvp_stats",
        description="View a MONSTR's PvP stat card"
    )
    @discord.app_commands.describe(asa_id="The ASA ID of the MONSTR to inspect")
    async def pvp_stats(self, interaction: discord.Interaction, asa_id: str):
        if await _wrong_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        db  = _db()
        row = db.table("monstr_pvp_stats").select("*").eq("asa_id", str(asa_id)).execute()
        if not row.data:
            await interaction.followup.send(
                f"❌ **{asa_id}** isn't registered for PvP yet.", ephemeral=True
            )
            return

        r     = row.data[0]
        stats = _load_stats(asa_id, r["owner_id"])

        embed = discord.Embed(title=f"🧟 {r['monstr_name']} — PvP Stats", color=0x9b59b6)
        embed.add_field(name="👤 Owner", value=f"<@{r['owner_id']}>", inline=False)
        for name, val in format_stats_embed_fields(stats):
            embed.add_field(name=name, value=val, inline=False)
        if stats and stats.image_url:
            embed.set_thumbnail(url=stats.image_url)
        embed.set_footer(
            text=f"Trait bonus: ATK+{r['trait_bonus_atk']} DEF+{r['trait_bonus_def']} SPD+{r['trait_bonus_spd']}  •  Registered {r['registered_at'][:10]}"
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
                os.getenv("ALGOD_URL", "https://mainnet-api.algonode.cloud")
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
        await interaction.response.defer(ephemeral=True)
        buf  = await asyncio.to_thread(render_board, "waiting", None, None, "No active challenge")
        file = discord.File(buf, filename="board.png")
        msg  = await interaction.channel.send(file=file, view=JoinBattleView())
        _board.board_msg_id = msg.id
        _board.reset()
        await interaction.followup.send("GOO battle board posted! Pin it.", ephemeral=True)

    @discord.app_commands.command(
        name="pvp_setupboard_algo",
        description="(Admin) Post the ALGO battle board in this channel"
    )
    @discord.app_commands.default_permissions(administrator=True)
    async def pvp_setupboard_algo(self, interaction: discord.Interaction):
        if await _wrong_channel(interaction): return
        await interaction.response.defer(ephemeral=True)
        buf  = await asyncio.to_thread(render_board, "waiting", None, None, "No active challenge")
        file = discord.File(buf, filename="board.png")
        msg  = await interaction.channel.send(file=file, view=JoinBattleView())
        _board_algo.board_msg_id = msg.id
        _board_algo.reset()
        await interaction.followup.send("ALGO battle board posted! Pin it.", ephemeral=True)

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

        wager = GOO_WAGER_1V1
        if result.is_draw:
            _refund_wager(db, chal_id, wager, duel_id, "draw")
            _refund_wager(db, opp_id, wager, duel_id, "draw")
            try:
                db.table("pvp_duels").update({"status": "draw", "battle_log": battle_log,
                    "updated_at": datetime.now(timezone.utc).isoformat()}).eq("id", duel_id).execute()
            except Exception as e: print(f"[PVP] DB draw failed: {e}")
        else:
            winner_id  = result.winner_owner
            is_algo    = room == "algo"
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

        try:
            if result.is_draw:
                win_info = WinnerInfo(monstr_name=a.name, username="draw",
                    total_rounds=result.total_rounds, wager_won=0, image_url=None, is_draw=True)
            else:
                winner_m   = a if result.winner_asa == a.asa_id else b
                winner_uname = await _get_display_name(channel.guild, winner_m.owner_id)
                is_algo    = room == "algo"
                winner_cut = ALGO_WINNER_CUT if is_algo else GOO_WINNER_CUT_1V1
                win_info = WinnerInfo(monstr_name=winner_m.name, username=winner_uname,
                    total_rounds=result.total_rounds, wager_won=winner_cut,
                    image_url=winner_m.image_url, is_draw=False)
            result_buf = await asyncio.to_thread(render_result, win_info)
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
            is_algo    = room == "algo"
            winner_cut = ALGO_WINNER_CUT if is_algo else GOO_WINNER_CUT_1V1
            prize_str  = f"{winner_cut/1_000_000:g} ALGO" if is_algo else f"{winner_cut:,} $GOO"
            await channel.send(
                f"🏆 **{winner_m.name}** wins! Congratulations <@{winner_m.owner_id}>! 🎉\n"
                + f"**+{prize_str}** credited. GG <@{loser_m.owner_id}>! 💪")

    async def _run_board_battle(self, channel, db: object, room: str = "goo",
                                 chal_id: str = "", chal_asa: str = "",
                                 chal_stats: MonstrStats = None, chal_uname: str = "",
                                 opp_id: str = "", opp_asa: str = "",
                                 opp_stats: MonstrStats = None, opp_uname: str = ""):
        """Full battle flow for either GOO or ALGO room."""
        is_algo  = room == "algo"
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
        def lock(uid): return _deduct_algo(db, uid, wager, f"wager duel#{duel_id}") if is_algo else _lock_wager(db, uid, wager, duel_id)
        def refund(uid, note=""): 
            if is_algo: _credit_algo(db, uid, wager, note)
            else: _refund_wager(db, uid, wager, duel_id, note)
        deposit_cmd = "`/pvp_deposit_algo`" if is_algo else "`/pvp_deposit`"
        wager_str   = f"{wager/1_000_000:g} ALGO" if is_algo else f"{wager:,} $GOO"

        if not lock(chal_id):
            await channel.send(f"❌ <@{chal_id}> doesn't have enough. Need **{wager_str}**. Use {deposit_cmd}.")
            db.table("pvp_duels").update({"status": "cancelled"}).eq("id", duel_id).execute()
            await _update_board(channel, "waiting", room, status_text="No active challenge")
            return

        if not lock(opp_id):
            refund(chal_id, "opp failed")
            await channel.send(f"❌ <@{opp_id}> doesn't have enough. Need **{wager_str}**. Use {deposit_cmd}. Challenger refunded.")
            db.table("pvp_duels").update({"status": "cancelled"}).eq("id", duel_id).execute()
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
        double-crediting.
        """
        try:
            await self._process_deposits()
        except Exception as e:
            print(f"[PVP] deposit poll error: {e}")

    @poll_deposits.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()
        # Snapshot current bot wallet GOO balance so we only credit NEW deposits
        self._last_bot_goo = await asyncio.to_thread(_get_bot_goo_balance)
        print(f"[PVP] deposit poller started — bot wallet GOO balance: {self._last_bot_goo:,}")


    @tasks.loop(seconds=30)
    async def poll_algo_deposits(self):
        """Balance-diff approach — same as GOO poller. Avoids tx history queries blocked by algonode."""
        try:
            current = await asyncio.to_thread(_get_bot_algo_balance)
            diff    = current - self._last_bot_algo
            self._last_bot_algo = current

            if diff < 100_000:  # less than 0.1 ALGO — ignore dust/fees
                return

            print(f"[PVP] ALGO balance increased by {diff/1_000_000:g} ALGO — attempting tx lookup")
            db = _db()

            # Try tx lookup — may work depending on algonode tier
            import urllib.request, json as _json
            try:
                bot_addr    = await asyncio.to_thread(_get_bot_address)
                indexer_url = os.environ["INDEXER_URL"]
                url = (
                    f"{indexer_url}/v2/transactions"
                    f"?address={bot_addr}&address-role=receiver&tx-type=pay&limit=10"
                )
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    data = _json.loads(r.read())

                seen_rows = db.table("pvp_seen_deposits").select("tx_id").execute()
                seen_ids  = {r["tx_id"] for r in seen_rows.data} if seen_rows.data else set()
                credited  = False

                for txn in data.get("transactions", []):
                    tx_id  = txn.get("id", "")
                    sender = txn.get("sender", "")
                    amount = txn.get("payment-transaction", {}).get("amount", 0)
                    if not tx_id or tx_id in seen_ids or amount < 100_000:
                        continue
                    wallet_row = db.table("linked_wallets").select("user_id").eq("wallet_address", sender).execute()
                    db.table("pvp_seen_deposits").insert({"tx_id": tx_id, "note": f"algo amt={amount}"}).execute()
                    if not wallet_row.data:
                        print(f"[PVP] ALGO from unknown wallet {sender[:8]} amount={amount/1_000_000:g}")
                        continue
                    user_id = wallet_row.data[0]["user_id"]
                    new_bal = _credit_algo(db, user_id, amount, f"algo deposit tx={tx_id[:16]}")
                    print(f"[PVP] ALGO credited uid={user_id} +{amount/1_000_000:g} ALGO bal={new_bal/1_000_000:g}")
                    asyncio.ensure_future(self._notify_algo_deposit(user_id, amount, new_bal))
                    credited = True

                if not credited:
                    print(f"[PVP] ALGO deposit of {diff/1_000_000:g} arrived but no tx matched — may need manual credit")

            except Exception as e:
                print(f"[PVP] ALGO tx lookup failed ({e}) — deposit of {diff/1_000_000:g} ALGO needs manual credit")

        except Exception as e:
            print(f"[PVP] poll_algo_deposits error: {e}")

    @poll_algo_deposits.before_loop
    async def before_algo_poll(self):
        await self.bot.wait_until_ready()
        self._last_bot_algo = await asyncio.to_thread(_get_bot_algo_balance)
        print(f"[PVP] ALGO poller started — bot wallet: {self._last_bot_algo/1_000_000:g} ALGO")

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
        """GOO deposit detection via balance-diff + wallet scanning fallback."""
        current_bal = await asyncio.to_thread(_get_bot_goo_balance)
        diff = current_bal - self._last_bot_goo
        print(f"[PVP] GOO poll: current={current_bal:,} last={self._last_bot_goo:,} diff={diff:,}")

        if diff <= 0:
            self._last_bot_goo = current_bal
            return

        print(f"[PVP] GOO balance up {diff:,} — scanning for depositor")
        db  = _db()
        credited = False

        # Try tx history first
        try:
            import urllib.request, json as _json
            asset_id    = int(os.environ["GOO_ASSET_ID"])
            bot_addr    = await asyncio.to_thread(_get_bot_address)
            indexer_url = os.environ["INDEXER_URL"]
            url = (
                f"{indexer_url}/v2/transactions"
                f"?asset-id={asset_id}&address={bot_addr}&address-role=receiver&limit=10"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = _json.loads(r.read())

            seen_rows = db.table("pvp_seen_deposits").select("tx_id").execute()
            seen_ids  = {r["tx_id"] for r in seen_rows.data} if seen_rows.data else set()

            for txn in data.get("transactions", []):
                tx_id = txn.get("id", "")
                if not tx_id or tx_id in seen_ids: continue
                at = txn.get("asset-transfer-transaction", {})
                if at.get("asset-id") != asset_id: continue
                if at.get("receiver") != bot_addr: continue
                sender = txn.get("sender", "")
                amount = at.get("amount", 0)
                if amount <= 0 or not sender: continue

                wallet_row = db.table("linked_wallets").select("user_id").eq("wallet_address", sender).execute()
                db.table("pvp_seen_deposits").insert({"tx_id": tx_id, "note": f"amt={amount}"}).execute()
                if not wallet_row.data:
                    print(f"[PVP] deposit from unknown wallet {sender[:8]} amount={amount}")
                    continue
                user_id = wallet_row.data[0]["user_id"]
                new_bal = _credit(db, user_id, amount, note=f"deposit tx={tx_id[:16]}")
                _log_transaction(db, user_id, None, "deposit", amount, sender, tx_id)
                print(f"[PVP] GOO deposit credited uid={user_id} amount={amount} bal={new_bal}")
                asyncio.ensure_future(self._notify_deposit(user_id, amount, new_bal))
                credited = True

        except Exception as e:
            if "403" not in str(e):
                print(f"[PVP] tx lookup error: {e}")

        # Fallback: scan each linked wallet's GOO balance to find who sent it
        if not credited:
            try:
                import urllib.request, json as _json
                asset_id    = int(os.environ["GOO_ASSET_ID"])
                indexer_url = os.environ["INDEXER_URL"]
                wallets     = db.table("linked_wallets").select("user_id,wallet_address").execute()

                for w in (wallets.data or []):
                    wallet_addr = w["wallet_address"]
                    user_id     = w["user_id"]
                    cache_key   = f"goo_bal_{user_id}"

                    # Get current GOO balance of this wallet
                    try:
                        url = f"{indexer_url}/v2/accounts/{wallet_addr}/assets/{asset_id}"
                        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req, timeout=6) as r:
                            data = _json.loads(r.read())
                        cur_wallet_bal = data.get("asset-holding", {}).get("amount", 0)
                    except Exception:
                        continue

                    # Compare to cached balance
                    prev = self._wallet_goo_cache.get(wallet_addr, cur_wallet_bal)
                    self._wallet_goo_cache[wallet_addr] = cur_wallet_bal

                    if prev > cur_wallet_bal:
                        # Their balance decreased — they sent GOO
                        sent = prev - cur_wallet_bal
                        if abs(sent - diff) < diff * 0.1:  # within 10% of what we received
                            new_bal = _credit(db, user_id, diff, note="deposit wallet-scan")
                            print(f"[PVP] GOO deposit matched via wallet scan uid={user_id} amount={diff} bal={new_bal}")
                            asyncio.ensure_future(self._notify_deposit(user_id, diff, new_bal))
                            credited = True
                            break

            except Exception as e:
                print(f"[PVP] wallet scan fallback error: {e}")

        self._last_bot_goo = current_bal



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
    # CHALLENGE EXPIRY LOOP
    # ─────────────────────────────────────────

    @tasks.loop(minutes=30)
    async def expire_challenges(self):
        try:
            db = _db()
            db.table("pvp_challenges").update({"status": "expired"}).eq(
                "status", "open"
            ).lt("expires_at", datetime.now(timezone.utc).isoformat()).execute()
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
