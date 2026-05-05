# MONSTRS Encounters Bot — Deploy Guide

## Files

| File | Purpose |
|---|---|
| `bot.py` | Entry point — loads cog, syncs commands |
| `encounters.py` | All encounter logic, scheduler, slash commands |
| `encounters_setup.sql` | Run once in Supabase to create tables |
| `requirements.txt` | Python dependencies |
| `.env.example` | Copy to `.env` and fill in your keys |
| `railway.toml` | Railway deployment config |

---

## Step 1 — Create the Discord Bot

1. Go to https://discord.com/developers/applications
2. Click **New Application** → name it (e.g. "MONSTRS Encounters")
3. Go to **Bot** tab → click **Add Bot**
4. Under **Privileged Gateway Intents**, enable:
   - Server Members Intent
   - Message Content Intent
5. Click **Reset Token** → copy your bot token → paste into `.env` as `DISCORD_BOT_TOKEN`
6. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: `Send Messages`, `Embed Links`, `Read Message History`, `View Channels`
7. Open the generated URL → invite the bot to your server

---

## Step 2 — Get Your IDs

Right-click your server icon → **Copy Server ID** → paste as `DISCORD_GUILD_ID`
Right-click your encounters channel → **Copy Channel ID** → paste as `DISCORD_ENCOUNTERS_CHANNEL_ID`

(Enable Developer Mode first: User Settings → Advanced → Developer Mode)

---

## Step 3 — Set Up Supabase

1. Create a project at https://supabase.com
2. Go to **SQL Editor** → paste contents of `encounters_setup.sql` → run it
3. Go to **Settings → API**:
   - Copy **Project URL** → paste as `SUPABASE_URL`
   - Copy **service_role** key → paste as `SUPABASE_KEY`

---

## Step 4 — Add Your MONSTR Assets

Open `encounters.py` and update the `MONSTR_ASSETS` dict at the top:

```python
MONSTR_ASSETS = {
    "ASA_ID": ("Name", "IPFS_HASH"),
    "ASA_ID": ("Name", "IPFS_HASH"),
    # etc
}
```

The MONSTRS team should be able to provide a CSV or list of ASA IDs and IPFS hashes.

---

## Step 5 — Deploy to Railway

1. Push all files to a GitHub repo (make sure `.env` is in `.gitignore`)
2. Go to https://railway.app → **New Project → Deploy from GitHub repo**
3. Select your repo
4. Go to **Variables** tab → add all values from `.env.example` with your real values
5. Railway will build and deploy automatically

---

## Step 6 — Verify

Once the bot is online:
- Check your encounters channel — you should see the bot appear as online
- Run `/goo` in your server to confirm slash commands are working
- Watch the logs in Railway for the scheduled encounter times being printed

---

## Adjust Encounter Windows

Default schedule (UTC):
- AM: random time between 10am–1pm
- PM: random time between 6pm–9pm

To change, edit `_schedule_slot()` in `encounters.py`:

```python
if slot == "am":
    hour = random.randint(10, 13)  # ← change these
else:
    hour = random.randint(18, 21)  # ← and these
```

---

## Slash Commands

| Command | Description |
|---|---|
| `/attack` | Attack the active MONSTR |
| `/attack @friend` | Attack with a teammate for bonus GOO |
| `/goo` | Check your $GOO balance |
