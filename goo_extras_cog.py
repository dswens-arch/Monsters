"""
goo_extras_cog.py
-----------------
Two features for the MONSTRS $GOO Warden bot:

  1. /tip — Admin-only command to send $GOO from the bot hot wallet to any linked Discord user.
  2. Downbad sale watcher — Listens for Downbad Marketplace posts in the sales channel,
     parses the buyer's Algorand address from the embed, and sends 10,000 $GOO to any
     buyer whose wallet is linked in Supabase. Posts a claim prompt if the buyer isn't linked.

Environment variables required (shared with encounters.py):
  SUPABASE_URL
  SUPABASE_KEY          (service_role)
  BOT_MNEMONIC
  GOO_ASSET_ID
  ALGOD_URL             (default: https://mainnet-api.algonode.cloud)
  ALGOD_TOKEN           (default: "")

New env var:
  DOWNBAD_SALES_CHANNEL_ID   — Discord channel ID where Downbad posts sale notifications
"""

import os
import re
import asyncio

import discord
from discord.ext import commands
from supabase import create_client, Client


# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────

SALE_REWARD_AMOUNT = 10_000          # $GOO units (not microunits — match existing send_goo scale)
DOWNBAD_BOT_NAME   = "Downbad Marketplace"
MONSTRS_COLLECTION = "MONSTRS"


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def get_supabase() -> Client:
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def send_goo(to_address: str, amount: int, note: str = "MONSTRS GOO reward") -> str:
    """
    Send $GOO from the bot hot wallet. Returns tx_id. Raises on failure.
    Retries up to 3 times with backoff to handle AlgoNode free-tier rate limits.
    """
    import time
    from algosdk import mnemonic, transaction, account
    from algosdk.v2client import algod

    client = algod.AlgodClient(
        algod_token=os.getenv("ALGOD_TOKEN", ""),
        algod_address=os.getenv("ALGOD_URL", "https://mainnet-api.algonode.cloud"),
    )
    mn = os.environ["BOT_MNEMONIC"]
    private_key = mnemonic.to_private_key(mn)
    bot_address = account.address_from_private_key(private_key)
    asset_id = int(os.environ["GOO_ASSET_ID"])

    last_error = None
    for attempt in range(3):
        try:
            if attempt > 0:
                wait = 2 ** attempt  # 2s, 4s
                print(f"[WALLET] Rate limit hit, retrying in {wait}s (attempt {attempt + 1}/3)...")
                time.sleep(wait)

            params = client.suggested_params()
            txn = transaction.AssetTransferTxn(
                sender=bot_address,
                sp=params,
                receiver=to_address,
                amt=amount,
                index=asset_id,
                note=note.encode(),
            )
            signed = txn.sign(private_key)
            tx_id = client.send_transaction(signed)
            return tx_id

        except Exception as e:
            last_error = e
            if "403" not in str(e) and "429" not in str(e):
                raise  # Non-rate-limit error — don't retry

    raise last_error


def get_linked_wallet(discord_id: str) -> str | None:
    """Return the linked Algorand wallet address for a Discord user, or None."""
    try:
        db = get_supabase()
        row = (
            db.table("linked_wallets")
            .select("wallet_address")
            .eq("user_id", discord_id)
            .execute()
        )
        if row.data and row.data[0].get("wallet_address"):
            return row.data[0]["wallet_address"]
        return None
    except Exception as e:
        print(f"[GOO_EXTRAS] wallet lookup failed for {discord_id}: {e}")
        return None


def find_wallet_by_address(algo_address: str) -> str | None:
    """
    Given a full Algorand address, look up which Discord user has it linked.
    Returns discord_id string or None.
    """
    try:
        db = get_supabase()
        row = (
            db.table("linked_wallets")
            .select("user_id")
            .eq("wallet_address", algo_address)
            .execute()
        )
        if row.data and row.data[0].get("user_id"):
            return row.data[0]["user_id"]
        return None
    except Exception as e:
        print(f"[GOO_EXTRAS] reverse wallet lookup failed: {e}")
        return None


def parse_buyer_address_from_embed(embed: discord.Embed) -> str | None:
    """
    Extract the full Algorand address from a Downbad sale embed.

    Downbad formats the Buyer field as either:
      theonetwo.algo\n(https://downbad.farm/account/FULLADDRESS)
    or:
      QZE6L...ATKOY\n(https://downbad.farm/account/FULLADDRESS)

    We pull the address from the URL in both cases.
    """
    for field in embed.fields:
        if field.name and field.name.strip().lower() == "buyer":
            # Extract address from the downbad.farm/account/ URL
            match = re.search(
                r"https://downbad\.farm/account/([A-Z2-7]{58})",
                field.value or "",
            )
            if match:
                return match.group(1)
    return None


def is_monstrs_sale(embed: discord.Embed) -> bool:
    """Return True if this Downbad embed is for a MONSTRS collection sale."""
    for field in embed.fields:
        if field.name and field.name.strip().lower() == "collection":
            return MONSTRS_COLLECTION.lower() in (field.value or "").lower()
    # Also check embed title as fallback
    if embed.title and "MONSTR" in embed.title.upper():
        return True
    return False


def get_monstr_number(embed: discord.Embed) -> str:
    """Pull MONSTR #XXXX from the embed title for display purposes."""
    if embed.title:
        return embed.title.strip()
    return "a MONSTR"


# ─────────────────────────────────────────────
# COG
# ─────────────────────────────────────────────

class GooExtrasCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sales_channel_id = int(os.environ.get("DOWNBAD_SALES_CHANNEL_ID", "1431659216468443207"))

    # ── /tip ──────────────────────────────────

    @discord.app_commands.command(
        name="tip",
        description="[ADMIN] Send $GOO from the bot wallet to a Discord user's linked wallet"
    )
    @discord.app_commands.describe(
        user="Discord user to tip",
        amount="Amount of $GOO to send",
        note="Optional note attached to the transaction"
    )
    async def tip(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: int,
        note: str = "MONSTRS $GOO tip",
    ):
        await interaction.response.defer(ephemeral=True)

        if amount <= 0:
            await interaction.followup.send("⚠️ Amount must be greater than zero.", ephemeral=True)
            return

        # Look up recipient's linked wallet
        wallet = await asyncio.to_thread(get_linked_wallet, str(user.id))

        if not wallet:
            await interaction.followup.send(
                f"❌ **{user.display_name}** doesn't have a linked wallet yet.\n"
                f"They need to use `/link` first.",
                ephemeral=True,
            )
            return

        # Send on-chain
        try:
            tx_id = await asyncio.to_thread(send_goo, wallet, amount, note)
            print(f"[TIP] {interaction.user} tipped {amount} GOO to {user} ({wallet[:8]}...) TxID: {tx_id}")
            # Ephemeral confirm for admin
            await interaction.followup.send(
                f"✅ **{amount:,} $GOO** sent to {user.mention}\n"
                f"Wallet: `{wallet[:8]}...{wallet[-4:]}`\n"
                f"TxID: `{tx_id}`",
                ephemeral=True,
            )
            # Public channel notification
            await interaction.channel.send(
                f"🧪 {user.mention} just received a **{amount:,} $GOO** tip from the Warden! 🧟"
            )
        except Exception as e:
            print(f"[TIP] send failed: {e}")
            await interaction.followup.send(
                f"❌ Transaction failed: `{e}`",
                ephemeral=True,
            )

    # ── Downbad sale watcher ──────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Only watch the designated sales channel
        if message.channel.id != self.sales_channel_id:
            return

        # Only process messages from the Downbad Marketplace app/bot
        if message.author.name != DOWNBAD_BOT_NAME and not (
            message.author.bot and DOWNBAD_BOT_NAME.lower() in message.author.name.lower()
        ):
            return

        # Must have embeds
        if not message.embeds:
            return

        for embed in message.embeds:
            # Confirm it's a MONSTRS collection sale
            if not is_monstrs_sale(embed):
                continue

            monstr_name = get_monstr_number(embed)

            # Extract buyer's full Algorand address from the embed
            buyer_address = parse_buyer_address_from_embed(embed)

            if not buyer_address:
                print(f"[SALE] Could not parse buyer address from embed for {monstr_name}")
                continue

            print(f"[SALE] {monstr_name} sold — buyer address: {buyer_address[:8]}...")

            # Check if that address is linked to a Discord user
            discord_id = await asyncio.to_thread(find_wallet_by_address, buyer_address)

            if discord_id:
                # Linked — send the reward
                try:
                    tx_id = await asyncio.to_thread(
                        send_goo,
                        buyer_address,
                        SALE_REWARD_AMOUNT,
                        f"MONSTRS secondary sale reward — {monstr_name}",
                    )
                    print(f"[SALE] Sent {SALE_REWARD_AMOUNT} GOO to <@{discord_id}> for {monstr_name} TxID: {tx_id}")
                    await message.channel.send(
                        f"🧪 **{monstr_name}** just found a new home!\n"
                        f"<@{discord_id}> earned **10,000 $GOO** for the pickup. Welcome to the pack. 🧟"
                    )
                except Exception as e:
                    print(f"[SALE] GOO send failed for {monstr_name}: {e}")
            else:
                # Not linked — post a claim prompt
                await message.channel.send(
                    f"🧪 **{monstr_name}** just sold!\n"
                    f"Buyer — **10,000 $GOO** is waiting for you. "
                    f"Link your wallet with `/link` to claim your reward. 🧟"
                )


# ─────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────

async def setup(bot: commands.Bot):
    await bot.add_cog(GooExtrasCog(bot))
