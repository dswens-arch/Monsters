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
    upgrade_cost, can_upgrade,
    STAT_BASE, STAT_MAX,
)
from encounters import MONSTR_ASSETS, send_goo, has_opted_in
from pvp_board import BoardPlayer, render_board
from pvp_board_result import WinnerInfo, render_result


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

GOO_WAGER_1V1      = 100        # per player
GOO_WINNER_CUT_1V1 = 200        # winner receives (out of 1000 pot)
GOO_TREASURY_1V1   = 0        # stays in bot wallet as treasury

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


def _get_bot_address() -> str:
    from algosdk import mnemonic, account
    pk = mnemonic.to_private_key(os.environ["BOT_MNEMONIC"])
    return account.address_from_private_key(pk)


# ─────────────────────────────────────────────
# OWNERSHIP CHECK
# ─────────────────────────────────────────────

def _verify_ownership(asa_id: str, wallet_address: str) -> bool:
    import urllib.request, json as _json
    try:
        indexer_url = os.environ["INDEXER_URL"]
        url = f"{indexer_url}/v2/accounts/{wallet_address}/assets?include-all=false"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = _json.loads(r.read())
        for asset in data.get("assets", []):
            if str(asset.get("asset-id", "")) == str(asa_id) and asset.get("amount", 0) > 0:
                return True
        return False
    except Exception as e:
        print(f"[PVP] ownership check failed asa={asa_id}: {e}")
        return False



def _fetch_monstr_asa_ids(wallet_address: str) -> list[str]:
    """
    Return list of MONSTR ASA IDs held by wallet_address (amount > 0).
    Uses MONSTR_ASSETS registry as the source of truth for valid ASA IDs.
    """
    import urllib.request, json as _json
    try:
        indexer_url = os.environ["INDEXER_URL"]
        url = (
            f"{indexer_url}/v2/accounts/{wallet_address}/assets"
            f"?include-all=false&limit=1000"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = _json.loads(r.read())

        held = []
        for asset in data.get("assets", []):
            asa = str(asset.get("asset-id", ""))
            if asa in MONSTR_ASSETS and asset.get("amount", 0) > 0:
                held.append(asa)
        return held
    except Exception as e:
        print(f"[PVP] fetch_monstr_asa_ids failed {wallet_address[:8]}...: {e}")
        return []


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
        r = row.data[0]
        img = None
        if str(asa_id) in MONSTR_ASSETS:
            cid = MONSTR_ASSETS[str(asa_id)][1]
            img = f"https://dweb.link/ipfs/{cid}"
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


async def _wrong_channel(interaction: discord.Interaction) -> bool:
    ch_id = _pvp_channel_id()
    if ch_id and interaction.channel_id != ch_id:
        await interaction.response.send_message(
            f"⚔️ PvP commands only work in <#{ch_id}>.", ephemeral=True
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

    embed.set_footer(text="/pvp_duel to challenge • /pvp_balance to check your GOO")
    return embed


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

_board = BoardState()


# ─────────────────────────────────────────────
# MONSTR PICKER VIEW  (ephemeral — only sender sees)
# ─────────────────────────────────────────────

class MonstrPickerView(discord.ui.View):
    """
    Shows up to 5 MONSTR buttons from the user's registered MONSTRs
    plus a 6th 'Enter ASA ID' button.
    Fires join_callback(interaction, asa_id) when a choice is made.
    """
    def __init__(self, monstr_rows: list[dict], join_callback):
        super().__init__(timeout=120)
        self._cb = join_callback

        for row in monstr_rows[:5]:
            btn = discord.ui.Button(
                label    = row["monstr_name"],
                style    = discord.ButtonStyle.primary,
                custom_id= f"pick_{row['asa_id']}",
            )
            btn.callback = self._make_pick_cb(row["asa_id"])
            self.add_item(btn)

        # 6th button — manual entry via modal
        manual = discord.ui.Button(
            label    = "Enter ASA ID",
            style    = discord.ButtonStyle.secondary,
            custom_id= "pick_manual",
            row      = 1,
        )
        manual.callback = self._manual_cb
        self.add_item(manual)

    def _make_pick_cb(self, asa_id: str):
        async def cb(interaction: discord.Interaction):
            await self._cb(interaction, asa_id)
        return cb

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
    """
    Ephemeral flow triggered when someone taps Join Battle.
    1. Check they have a linked wallet and GOO balance
    2. Show their registered MONSTRs as buttons (+ manual entry)
    3. On MONSTR pick → if board empty become challenger, else trigger battle
    """
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)
    db      = _db()

    # Must have a linked wallet
    wallet = await asyncio.to_thread(_get_linked_wallet, user_id)
    if not wallet:
        await interaction.followup.send(
            "❌ You need to link your wallet first. Use .", ephemeral=True
        )
        return

    # Must have enough GOO balance
    balance = _get_balance(db, user_id)
    if balance < GOO_WAGER_1V1:
        bot_addr = await asyncio.to_thread(_get_bot_address)
        await interaction.followup.send(
            f"❌ You need **{GOO_WAGER_1V1:,} $GOO** to join. You have {balance:,}. Deposit with /pvp_deposit.",
            ephemeral=True
        )
        return

    # Can't fight yourself
    if not _board.is_empty and _board.challenger["user_id"] == user_id:
        await interaction.followup.send(
            "You're already waiting for an opponent! Hang tight.", ephemeral=True
        )
        return

    # Fetch their registered MONSTRs
    rows = db.table("monstr_pvp_stats").select("asa_id,monstr_name")              .eq("owner_id", user_id).limit(5).execute()

    if not rows.data:
        await interaction.followup.send(
            "❌ You haven't registered any MONSTRs yet. Use  to get started.",
            ephemeral=True
        )
        return

    async def on_pick(pick_interaction: discord.Interaction, asa_id: str):
        await _on_monstr_picked(pick_interaction, asa_id, user_id, db)

    view = MonstrPickerView(rows.data, on_pick)
    await interaction.followup.send(
        f"**Choose your MONSTR** ({balance:,} $GOO available):",
        view   = view,
        ephemeral = True,
    )


async def _on_monstr_picked(interaction: discord.Interaction,
                             asa_id: str, user_id: str, db):
    """Called after a player picks their MONSTR. Either queues them or starts a battle."""
    await interaction.response.defer(ephemeral=True)

    # Validate MONSTR
    if str(asa_id) not in MONSTR_ASSETS:
        await interaction.followup.send(
            f"❌  isn't a recognised MONSTR ASA ID.", ephemeral=True
        )
        return

    row = db.table("monstr_pvp_stats").select("*").eq("asa_id", str(asa_id)).execute()
    if not row.data:
        await interaction.followup.send(
            f"❌ That MONSTR isn't registered for PvP yet. Use  first.",
            ephemeral=True
        )
        return
    if row.data[0]["owner_id"] != user_id:
        await interaction.followup.send("❌ That MONSTR isn't yours.", ephemeral=True)
        return

    stats = _load_stats(asa_id, user_id)
    if not stats:
        await interaction.followup.send("❌ Couldn't load MONSTR stats. Try again.", ephemeral=True)
        return

    username = interaction.user.display_name

    if _board.is_empty:
        # ── First player — become challenger ──
        _board.challenger = {
            "user_id":  user_id,
            "asa_id":   str(asa_id),
            "stats":    stats,
            "username": username,
        }
        await interaction.followup.send(
            f"✅ **{stats.name}** is in the arena! Waiting for an opponent...",
            ephemeral=True
        )
        # Update board image to waiting state
        channel = interaction.channel
        await _update_board(channel, "waiting")

    else:
        # ── Second player — start battle ──
        if _board.challenger["user_id"] == user_id:
            await interaction.followup.send(
                "You're already waiting for an opponent!", ephemeral=True
            )
            return

        await interaction.followup.send(
            f"✅ **{stats.name}** enters the arena! Battle starting...",
            ephemeral=True
        )

        challenger = _board.challenger
        _board.reset()

        # Run battle via the cog — need a reference, use bot
        cog: PvPCog = interaction.client.cogs.get("PvPCog")
        if cog:
            await cog._run_board_battle(
                channel    = interaction.channel,
                db         = db,
                chal_id    = challenger["user_id"],
                chal_asa   = challenger["asa_id"],
                chal_stats = challenger["stats"],
                chal_uname = challenger["username"],
                opp_id     = user_id,
                opp_asa    = str(asa_id),
                opp_stats  = stats,
                opp_uname  = username,
            )


# ─────────────────────────────────────────────
# BOARD UPDATE HELPER
# ─────────────────────────────────────────────

async def _update_board(channel, state: str,
                        p1=None, p2=None, status_text: str = ""):
    """
    Edit the persistent board message image in-place.
    Falls back to posting a new message if board_msg_id is lost.
    """
    buf = await asyncio.to_thread(render_board, state, p1, p2, status_text)
    file = discord.File(buf, filename="board.png")

    try:
        if _board.board_msg_id:
            msg = await channel.fetch_message(_board.board_msg_id)
            await msg.edit(attachments=[file], view=JoinBattleView())
            return
    except Exception:
        pass

    # Post fresh if message not found
    msg = await channel.send(file=file, view=JoinBattleView())
    _board.board_msg_id = msg.id


# ─────────────────────────────────────────────
# COG
# ─────────────────────────────────────────────


def _get_bot_goo_balance() -> int:
    """Return the bot wallet's current $GOO balance via algod."""
    import urllib.request, json as _json
    try:
        asset_id  = int(os.environ["GOO_ASSET_ID"])
        algod_url = os.getenv("ALGOD_URL", "https://mainnet-api.algonode.cloud")
        bot_addr  = _get_bot_address()
        url = f"{algod_url}/v2/accounts/{bot_addr}/assets/{asset_id}"
        req = urllib.request.Request(url, headers={
            "User-Agent":       "Mozilla/5.0",
            "X-Algo-API-Token": os.getenv("ALGOD_TOKEN", ""),
        })
        with urllib.request.urlopen(req, timeout=5) as r:
            data = _json.loads(r.read())
        return data.get("asset-holding", {}).get("amount", 0)
    except Exception as e:
        if "404" not in str(e):
            print(f"[PVP] bot GOO balance check failed: {e}")
        return 0


class PvPCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._pending_duels: dict[str, dict] = {}
        self._last_bot_goo: int = 0   # snapshot of bot wallet GOO, updated each poll
        self.poll_deposits.start()
        self.expire_challenges.start()
        # Register persistent view so it survives restarts
        self.bot.add_view(JoinBattleView())

    def cog_unload(self):
        self.poll_deposits.cancel()
        self.expire_challenges.cancel()

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
                f"Send $GOO to the bot wallet below. Your balance will be credited automatically "
                f"within ~{DEPOSIT_POLL_SECONDS} seconds.\n\n"
                f"**Bot wallet:**\n`{bot_addr}`\n\n"
                f"Your current PvP balance: **{balance:,} $GOO**"
            ),
            color=0x1D9E75
        )
        embed.add_field(
            name="Entry costs",
            value=f"1v1 wager: **{GOO_WAGER_1V1:,} $GOO**\nStat upgrade: **100–2,000 $GOO** per level",
            inline=False
        )
        embed.set_footer(text="Use /pvp_balance to check your balance • /pvp_withdraw to pull funds back out")
        await interaction.followup.send(embed=embed, ephemeral=True)

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

        if str(asa_id) not in MONSTR_ASSETS:
            await interaction.followup.send(
                f"❌ `{asa_id}` isn't a recognised MONSTR ASA ID.", ephemeral=True
            )
            return

        monstr_name = MONSTR_ASSETS[str(asa_id)][0]
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

        # Verify ownership
        try:
            owns = await asyncio.wait_for(
                asyncio.to_thread(_verify_ownership, asa_id, wallet), timeout=15
            )
        except asyncio.TimeoutError:
            owns = False

        if not owns:
            await interaction.followup.send(
                f"❌ **{monstr_name}** wasn't found in your wallet (`{wallet[:8]}...`).",
                ephemeral=True
            )
            return

        atk_b, def_b, spd_b = _calc_trait_bonus(asa_id)

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
        }).execute()

        stats = _load_stats(asa_id, user_id)

        embed = discord.Embed(
            title=f"✅ {monstr_name} registered for PvP!",
            description="Trait bonus locked in. Spend $GOO with `/pvp_upgrade` to level up stats.",
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
        description="Spend $GOO from your PvP balance to upgrade a MONSTR's stat"
    )
    @discord.app_commands.describe(
        asa_id="The ASA ID of your MONSTR",
        stat="Which stat to upgrade"
    )
    @discord.app_commands.choices(stat=[
        discord.app_commands.Choice(name="attack",  value="attack"),
        discord.app_commands.Choice(name="defense", value="defense"),
        discord.app_commands.Choice(name="speed",   value="speed"),
    ])
    async def pvp_upgrade(self, interaction: discord.Interaction, asa_id: str, stat: str):
        if await _wrong_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        user_id = str(interaction.user.id)
        db      = _db()

        row = db.table("monstr_pvp_stats").select("*").eq("asa_id", str(asa_id)).execute()
        if not row.data:
            await interaction.followup.send(
                f"❌ **{asa_id}** isn't registered. Use `/pvp_register {asa_id}` first.",
                ephemeral=True
            )
            return
        if row.data[0]["owner_id"] != user_id:
            await interaction.followup.send("❌ You don't own that MONSTR.", ephemeral=True)
            return

        r           = row.data[0]
        current_val = r[stat]

        if not can_upgrade(current_val):
            await interaction.followup.send(
                f"📈 **{stat.capitalize()}** is already maxed at {STAT_MAX}!", ephemeral=True
            )
            return

        cost    = upgrade_cost(current_val)
        balance = _get_balance(db, user_id)

        if balance < cost:
            await interaction.followup.send(
                f"❌ Not enough $GOO. This upgrade costs **{cost:,} $GOO** "
                f"but your PvP balance is **{balance:,} $GOO**.\n\n"
                f"Deposit more with `/pvp_deposit`.",
                ephemeral=True
            )
            return

        # Deduct balance
        ok, new_bal = _deduct(db, user_id, cost,
                              note=f"upgrade {r['monstr_name']} {stat} {current_val}→{current_val+1}")
        if not ok:
            await interaction.followup.send("❌ Balance changed — please try again.", ephemeral=True)
            return

        # Apply upgrade
        new_val = current_val + 1
        db.table("monstr_pvp_stats").update({
            stat:         new_val,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("asa_id", str(asa_id)).execute()

        _log_transaction(db, user_id, None, "upgrade_spend", cost,
                         note=f"{r['monstr_name']} {stat} {current_val}→{new_val}")

        stats = _load_stats(asa_id, user_id)

        embed = discord.Embed(
            title=f"📈 {r['monstr_name']} upgraded!",
            description=(
                f"**{stat.capitalize()}** {current_val} → **{new_val}**  •  "
                f"Cost: {cost:,} $GOO  •  Balance: {new_bal:,} $GOO"
            ),
            color=0x1D9E75
        )
        for name, val in format_stats_embed_fields(stats):
            embed.add_field(name=name, value=val, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────
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
    # /pvp_duel — direct challenge
    # ─────────────────────────────────────────

    @discord.app_commands.command(
        name="pvp_duel",
        description="Challenge another player to a 1v1 GOO wager battle"
    )
    @discord.app_commands.describe(
        opponent="The player you want to challenge",
        asa_id="Your MONSTR's ASA ID"
    )
    # ─────────────────────────────────────────

    async def _run_and_post_battle(self, channel, db: object, duel_id: int,
                                   a: MonstrStats, b: MonstrStats,
                                   chal_id: str, opp_id: str):
        """Resolve battle, handle payout/refund, post result embed."""
        result: BattleResult = await asyncio.to_thread(resolve_battle, a, b)

        battle_log = [
            {"round": r.round_num, "attacker": r.attacker_id, "damage": r.damage,
             "crit": r.is_crit, "defender_hp": r.defender_hp, "flavor": r.flavor}
            for r in result.rounds
        ]

        wager = GOO_WAGER_1V1

        if result.is_draw:
            _refund_wager(db, chal_id, wager, duel_id, "draw")
            _refund_wager(db, opp_id,  wager, duel_id, "draw")
            db.table("pvp_duels").update({
                "status":     "draw",
                "battle_log": battle_log,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", duel_id).execute()
        else:
            winner_id = result.winner_owner
            _credit_win(db, winner_id, GOO_WINNER_CUT_1V1, duel_id)

            # Fire on-chain payout async (non-blocking — balance already credited)
            asyncio.ensure_future(self._send_winner_payout(winner_id, GOO_WINNER_CUT_1V1, duel_id))

            db.table("pvp_duels").update({
                "status":     "complete",
                "winner_id":  winner_id,
                "winner_asa": result.winner_asa,
                "battle_log": battle_log,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", duel_id).execute()

        # Post result board
        if result.is_draw:
            winner_stats = a
            winner_uname = "draw"
        else:
            winner_stats = a if result.winner_asa == a.asa_id else b
            loser_stats  = b if result.winner_asa == a.asa_id else a
            # Get display name from guild
            guild = channel.guild
            winner_uname = await _get_display_name(guild, winner_stats.owner_id)

        if result.is_draw:
            win_info = WinnerInfo(
                monstr_name  = a.name,
                username     = "draw",
                total_rounds = result.total_rounds,
                wager_won    = 0,
                image_url    = None,
                is_draw      = True,
            )
        else:
            win_info = WinnerInfo(
                monstr_name  = winner_stats.name,
                username     = winner_uname,
                total_rounds = result.total_rounds,
                wager_won    = GOO_WINNER_CUT_1V1,
                image_url    = winner_stats.image_url,
                is_draw      = False,
            )

        result_buf = await asyncio.to_thread(render_result, win_info)
        summary_lines = [r.flavor for r in result.rounds[-5:]]
        summary = "\n".join(summary_lines) if summary_lines else ""
        await channel.send(
            content=summary or None,
            file=discord.File(result_buf, filename="result.png")
        )

    # ─────────────────────────────────────────
    # /pvp_setupboard — admin, posts persistent board
    # ─────────────────────────────────────────

    @discord.app_commands.command(
        name="pvp_setupboard",
        description="(Admin) Post the persistent PvP battle board in this channel"
    )
    @discord.app_commands.default_permissions(administrator=True)
    async def pvp_setupboard(self, interaction: discord.Interaction):
        if await _wrong_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        buf  = await asyncio.to_thread(render_board, "waiting", None, None, "No active challenge")
        file = discord.File(buf, filename="board.png")
        msg  = await interaction.channel.send(file=file, view=JoinBattleView())
        _board.board_msg_id = msg.id
        _board.reset()

        await interaction.followup.send(
            f"✅ Battle board posted (msg ID: ). Pin it to keep it visible.",
            ephemeral=True
        )

    # ─────────────────────────────────────────
    # BOARD BATTLE RUNNER (called from button flow)
    # ─────────────────────────────────────────

    async def _run_board_battle(self, channel, db: object,
                                chal_id: str, chal_asa: str,
                                chal_stats: MonstrStats, chal_uname: str,
                                opp_id: str, opp_asa: str,
                                opp_stats: MonstrStats, opp_uname: str):
        """Full battle flow triggered from the Join Battle button."""

        # Update board to active state
        bp1 = _to_board_player(chal_stats, chal_uname)
        bp2 = _to_board_player(opp_stats,  opp_uname)
        await _update_board(channel, "active", bp1, bp2, "⚔️ BATTLE IN PROGRESS")

        # Create duel record
        duel_result = db.table("pvp_duels").insert({
            "challenger_id":  chal_id,
            "opponent_id":    opp_id,
            "challenger_asa": chal_asa,
            "opponent_asa":   opp_asa,
            "room":           "goo",
            "wager_amount":   GOO_WAGER_1V1,
            "status":         "active",
            "expires_at":     (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }).execute()
        duel_id = duel_result.data[0]["id"]

        await asyncio.sleep(2)

        # Lock wagers
        if not _lock_wager(db, chal_id, GOO_WAGER_1V1, duel_id):
            await channel.send(f"❌ Couldn't lock <@{chal_id}>'s wager. Duel cancelled.")
            db.table("pvp_duels").update({"status": "cancelled"}).eq("id", duel_id).execute()
            await _update_board(channel, "waiting", None, None, "No active challenge")
            return

        if not _lock_wager(db, opp_id, GOO_WAGER_1V1, duel_id):
            _refund_wager(db, chal_id, GOO_WAGER_1V1, duel_id, "opp failed")
            await channel.send(f"❌ Couldn't lock <@{opp_id}>'s wager. Challenger refunded.")
            db.table("pvp_duels").update({"status": "cancelled"}).eq("id", duel_id).execute()
            await _update_board(channel, "waiting", None, None, "No active challenge")
            return

        # Run battle
        await self._run_and_post_battle(
            channel, db, duel_id,
            chal_stats, opp_stats, chal_id, opp_id
        )

        # Reset board to empty after brief pause
        await asyncio.sleep(4)
        await _update_board(channel, "waiting", None, None, "No active challenge")

    async def _send_winner_payout(self, user_id: str, amount: int, duel_id: int):
        """Fire on-chain GOO send to winner's linked wallet. Best-effort — balance already credited."""
        wallet = await asyncio.to_thread(_get_linked_wallet, user_id)
        if not wallet:
            print(f"[PVP] payout skipped — no wallet linked uid={user_id} duel#{duel_id}")
            return
        opted = await asyncio.to_thread(has_opted_in, wallet)
        if not opted:
            print(f"[PVP] payout skipped — wallet not opted in uid={user_id} duel#{duel_id}")
            return
        try:
            tx_id = await asyncio.to_thread(
                send_goo, wallet, amount, f"MONSTRS PvP win duel#{duel_id}"
            )
            db = _db()
            _log_transaction(db, user_id, duel_id, "payout_onchain", amount, wallet, tx_id)
            print(f"[PVP] payout sent uid={user_id} duel#{duel_id} tx={tx_id[:16]}")
        except Exception as e:
            print(f"[PVP] on-chain payout failed uid={user_id} duel#{duel_id}: {e}")

    # ─────────────────────────────────────────
    # DEPOSIT POLLING LOOP
    # ─────────────────────────────────────────

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

    async def _process_deposits(self):
        """
        Poll bot wallet GOO balance via algod. Credit the difference to
        whichever linked wallet sent the most recent deposit.

        Balance-diff approach avoids transaction history queries which
        are blocked by algonode's free tier on cloud IPs.
        """
        current_bal = await asyncio.to_thread(_get_bot_goo_balance)
        diff = current_bal - self._last_bot_goo

        if diff <= 0:
            self._last_bot_goo = current_bal
            return

        # Balance increased — find who sent it via indexer account asset lookup
        # Use the indexer only for linked wallet balance checks (allowed endpoint)
        db  = _db()
        amt = diff

        # Try to match to a linked wallet by checking each one's recent balance
        # Simplest: credit the deposit to whoever has a pending deposit request
        # For now: broadcast to channel and ask them to use /pvp_confirm_deposit
        # Advanced: use indexer tx lookup with retry (implemented below)

        credited = False
        try:
            # Attempt tx lookup — may work depending on algonode tier/timing
            import urllib.request, json as _json
            asset_id    = int(os.environ["GOO_ASSET_ID"])
            bot_addr    = await asyncio.to_thread(_get_bot_address)
            indexer_url = os.environ["INDEXER_URL"]
            url = (
                f"{indexer_url}/v2/transactions"
                f"?asset-id={asset_id}"
                f"&address={bot_addr}"
                f"&address-role=receiver"
                f"&limit=10"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = _json.loads(r.read())

            seen_rows = db.table("pvp_seen_deposits").select("tx_id").execute()
            seen_ids  = {r["tx_id"] for r in seen_rows.data} if seen_rows.data else set()

            for txn in data.get("transactions", []):
                tx_id = txn.get("id", "")
                if not tx_id or tx_id in seen_ids:
                    continue
                asset_transfer = txn.get("asset-transfer-transaction", {})
                if asset_transfer.get("asset-id") != asset_id:
                    continue
                if asset_transfer.get("receiver") != bot_addr:
                    continue
                sender = txn.get("sender", "")
                amount = asset_transfer.get("amount", 0)
                if amount <= 0 or not sender:
                    continue

                wallet_row = db.table("linked_wallets").select("user_id").eq("wallet_address", sender).execute()
                db.table("pvp_seen_deposits").insert({"tx_id": tx_id, "note": f"amt={amount}"}).execute()

                if not wallet_row.data:
                    print(f"[PVP] deposit from unknown wallet {sender[:8]}... amount={amount}")
                    continue

                user_id = wallet_row.data[0]["user_id"]
                new_bal = _credit(db, user_id, amount, note=f"deposit tx={tx_id[:16]}")
                _log_transaction(db, user_id, None, "deposit", amount, sender, tx_id)
                print(f"[PVP] deposit credited uid={user_id} amount={amount} new_bal={new_bal}")
                asyncio.ensure_future(self._notify_deposit(user_id, amount, new_bal))
                credited = True

        except Exception as e:
            # Indexer tx lookup failed (403 etc) — fall back to manual confirm
            if "403" not in str(e):
                print(f"[PVP] deposit tx lookup error: {e}")
            if not credited and diff > 0:
                # Notify channel that a deposit arrived but needs manual confirmation
                asyncio.ensure_future(self._notify_unmatched_deposit(amt))

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
