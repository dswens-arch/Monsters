"""
bot.py
------
MONSTRS Encounters Bot — entry point.
Loads the encounters cog and syncs slash commands on startup.
"""

import os
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])  # your server ID


class MonstrsBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Load cogs
        await self.load_extension("encounters")
        await self.load_extension("pvp_cog")

        # Sync slash commands to your guild instantly (vs global which takes up to 1hr)
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        print(f"[BOT] Slash commands synced to guild {GUILD_ID}")

    async def on_ready(self):
        print(f"[BOT] Logged in as {self.user} (ID: {self.user.id})")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="for MONSTRs 👾"
            )
        )


async def main():
    bot = MonstrsBot()
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
