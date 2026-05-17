"""
encounters.py
-------------
MONSTRS Encounters bot — full module.

Features:
  - 2 randomized encounters/day (AM + PM windows)
  - 10 minute encounter window
  - 5 minute warning ping before each encounter
  - Critical hits (5% chance, 2x message hype, same damage)
  - Team bonus for tagging a friend
  - Auto on-chain GOO payout to linked wallets after each encounter
  - Pending GOO held in Supabase for unlinked players, paid on /link
  - Weekly leaderboard (damage / kill shots / participation) — resets Monday
  - Boss encounters: random, once per 30 days, 1hr warning, 3x pool + HP
  - /link, /balance slash commands

Payout structure (standard):
  - Damage Pool:   3,000 GOO  (proportional by damage)
  - Kill Shot:       750 GOO
  - First Strike:    500 GOO
  - Team Bonus:      750 GOO (split across valid pairs, folds in if unused)

Boss multiplier: 3x everything = 15,000 GOO total

Environment variables:
  DISCORD_BOT_TOKEN
  DISCORD_GUILD_ID
  DISCORD_ENCOUNTERS_CHANNEL_ID
  SUPABASE_URL
  SUPABASE_KEY             (service_role)
  BOT_MNEMONIC             (25-word hot wallet mnemonic)
  GOO_ASSET_ID             (Algorand ASA ID for $GOO)
  ALGOD_URL                (default: https://mainnet-api.algonode.cloud)
  ALGOD_TOKEN              (default: "")
  INDEXER_URL              (default: https://mainnet-idx.algonode.cloud)
  INDEXER_TOKEN            (default: "")
"""

import asyncio
import random
import os
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands, tasks
from supabase import create_client, Client

from monstr_teams import (
    award_bp, roll_stun_resist, get_atk_multiplier,
    get_or_create_team, get_team, resolve_tier, next_tier_info,
    fetch_avatar_url, fetch_monstr_holdings, TIERS,
)


# ─────────────────────────────────────────────
# SUPABASE
# ─────────────────────────────────────────────

def get_supabase() -> Client:
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


# ─────────────────────────────────────────────
# ALGORAND — GOO PAYOUT
# ─────────────────────────────────────────────

def send_goo(to_address: str, amount: int, note: str = "MONSTRS GOO reward") -> str:
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
    # Fire and forget — don't wait for confirmation
    # TxID is logged; failed sends fall back to pending_goo automatically
    return tx_id

def has_opted_in(wallet_address: str) -> bool:
    from algosdk.v2client import algod
    import urllib.request
    import json
    try:
        asset_id = int(os.environ["GOO_ASSET_ID"])
        algod_url = os.getenv("ALGOD_URL", "https://mainnet-api.algonode.cloud")
        url = f"{algod_url}/v2/accounts/{wallet_address}/assets/{asset_id}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "X-Algo-API-Token": os.getenv("ALGOD_TOKEN", ""),
        })
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
        print(f"[WALLET] opt-in confirmed for {wallet_address[:8]}...")
        return True
    except Exception as e:
        if "404" in str(e) or "HTTP Error 404" in str(e):
            print(f"[WALLET] {wallet_address[:8]}... not opted in to GOO")
        else:
            print(f"[WALLET] opt-in check failed: {e}")
        return False


# ─────────────────────────────────────────────
# MONSTR ASSET REGISTRY — 2,001 MONSTRs
# ─────────────────────────────────────────────

MONSTR_ASSETS = {
    "3294386711": ("MONSTR #0675", "QmQ6CYKgY3Z2E7VMyUL3aMH4Db1WmYVb2u3HqheZTu237r"),
    "3294888288": ("MONSTR #1667", "QmWgJwy8pVsA76oEujbL4rBMRCGeGTSUb1T7akb9LDDSUP"),
    "3294310373": ("MONSTR #0032", "QmYpAg2CfQTdWVGuTiCXPhZgaZNveMXnnzAo3p8oiPaWaJ"),
    "3294770815": ("MONSTR #1001", "QmTqwVmMHDFX9Repe5ZHC7o9qXxgPLtV87n5Ubgp9ZShYw"),
    "3294770929": ("MONSTR #1002", "QmSykSZiV5GAQB5DkLhtQZqxm2FL7ctuFMXG2DLWpH7DjQ"),
    "3294771022": ("MONSTR #1003", "QmRKb4cTTKSbaRQR8411P4udjFZYU3CHQj9Yo9C75KcqQA"),
    "3294771086": ("MONSTR #1004", "QmXNFRtnPcyquzSsM3sku2HLSn6RCSU8JjeRXw72GN43Ve"),
    "3294771220": ("MONSTR #1005", "QmaibjaKVYxTS7DEG2KvG2T8KU5Ao7UMZtVSEyG9fz5zhf"),
    "3294771321": ("MONSTR #1006", "QmUvR66k22vs93JJdAU4sWmkRixgd5w1FEzzbDrfY2uUPb"),
    "3294771541": ("MONSTR #1008", "Qmc76u2TucuUahSfqP8A8uuBDcu85uSXaTDwS6ks5chHQz"),
    "3294771646": ("MONSTR #1009", "QmNtniaG6ebtotCD5NBx7g1a8YZ26795ZVgExaSbBhcjRD"),
    "3294771772": ("MONSTR #1010", "QmPLRdXuRM89vzyM4MnishKyp3AozoQFoXpb7HpeAR6obA"),
    "3294771952": ("MONSTR #1011", "QmTZRgB2dZbe71RUpaBZ7EZeKpcZMydN2di3v8bPUpVHMm"),
    "3294772041": ("MONSTR #1012", "QmcYSmvJzrFynmfGa8aiwpfqCZAb7t3CKaesHXTYvZtgHH"),
    "3294772090": ("MONSTR #1013", "QmWbGdqdB9u8Xze9EpFcW8BzFYvE7MpUwkVSvwh9hyo4tm"),
    "3294772252": ("MONSTR #1014", "QmSrXrVsVpFwDo492NZYC1eiFUqfAqSLmrAiRD5yjTdWiq"),
    "3294772386": ("MONSTR #1015", "QmY8Gujvg4NyC8iNnDdaqBwppb7T9wEWhjbTHuPyi6Tpfz"),
    "3294772472": ("MONSTR #1016", "QmWbcdqUbRYWHNLHhysfwpTJnHrNKbG2khtFpwawjQkKWQ"),
    "3294772609": ("MONSTR #1017", "QmYQkMgZ2RPVcPGiiUtPY3LDVPjqKTALvNunLWsKiAgrN7"),
    "3294772743": ("MONSTR #1018", "QmQJj3BekusZsKzcGqSv92siPF1e67N6S5zv5EZd26273S"),
    "3294772825": ("MONSTR #1019", "QmXRV1yeehXn2WRvyuPvVxtzNZFZyFaEGGVePfcCa9NojV"),
    "3294772900": ("MONSTR #1020", "QmcQoREBEncdN6UnkEXWrQr31m6NuPEA6nbJ6E2C4ePZmx"),
    "3294773031": ("MONSTR #1021", "QmRNc8Xy7pgyqteCmRXHvz2cZJnKL6eyukxTBvgJqhzR1t"),
    "3294773127": ("MONSTR #1022", "QmUgJPwr9dvXEV5Nw3BurZK1YfrZnqN3TT2DTTpqQD4tsE"),
    "3294773220": ("MONSTR #1023", "QmQZHQVd5LoiLpbs4v2fC1GbyqkLhPvN6s6PcsA4rGbaAF"),
    "3294773368": ("MONSTR #1024", "QmQTqcsANRXXH5gEagoJnDgKoh3mRL1T2eYoKNEuU6LhRq"),
    "3294773450": ("MONSTR #1025", "QmZhQ1SbPwb2sqXn2zG5SHUx8wauQZeCps96A51XhCzaKe"),
    "3294773715": ("MONSTR #1027", "QmR6XNFWpPWQwpCqSPpmPXceBTbeseMBYnqemKYssELfFb"),
    "3294773858": ("MONSTR #1028", "Qmbw4iWkYgvbWXVSGgEMC343AokKVB2xGpqhHzK1GfuuFP"),
    "3294773910": ("MONSTR #1029", "QmNvJAkPfeBNRxbZSX2jZopoxQhTAat8xTQo3JwV9jfvgz"),
    "3294774087": ("MONSTR #1030", "QmcXHtbzV3eXsin8UVt5t4ZS8ftmmdvEESbXzYCy56VW8B"),
    "3294774235": ("MONSTR #1031", "QmVn9sqyG9zvVzi2S5UNk1Xy7MGMZLxqvmAX5j1zr47hZW"),
    "3294774428": ("MONSTR #1032", "QmQN1zDGPRGwZrNW73djPLpkcggEqNpzpPNGxHBgCu5K51"),
    "3294774462": ("MONSTR #1033", "QmSybg6EHbGq7jfqG7HUzDeJXPAR3xGY6MxnNJbJKCeGEs"),
    "3294774571": ("MONSTR #1034", "Qmf1p36ev5SAz5sKYEdxYX3TUVUMzPB7aG71fd8ei5XaW2"),
    "3294774866": ("MONSTR #1035", "QmUgKaJUQjcQisuCH7gsqmtiGCAZAZhXhzGGzY3jSiijFU"),
    "3294774997": ("MONSTR #1036", "QmbXYTdcyuhtuhFKUMnC9AoiHccrpr5JwriyCMpFwEuS9X"),
    "3294775117": ("MONSTR #1037", "QmULb5SZr7eGKBS9zQTZwWopBMQvDZncQeRG96kkWcwjsZ"),
    "3294775180": ("MONSTR #1038", "QmeybLo29m2xRdVk8SACWZAorNf3aruDgSNMEAMY9bD8DQ"),
    "3294775497": ("MONSTR #1041", "QmbKyfMx23GYSHdcXHUy7EMhu7Zgg8izqGfpiKquaKnzry"),
    "3294775585": ("MONSTR #1042", "QmfKgd3U2HeNrjRxBCnXF8WQxqv3nZnoFwZP5GCfEMaGAs"),
    "3294775842": ("MONSTR #1043", "QmaMUXzkyAxEdDMbqPLDXmSfDxGxjvvPGjVVmiy69685mK"),
    "3294776155": ("MONSTR #1044", "QmQW7MhbQkffC67kZy3fMemLu4s8LiAxZq5g579uRfZA4a"),
    "3294776295": ("MONSTR #1045", "QmWy8AuRGxSvHcSRT7vt3anp96ErLxzmH6pb2QDrMqU6oY"),
    "3294776349": ("MONSTR #1046", "QmU3mFMNiwqMEpt8MoyutKSDe9jWF8PCUeMF2ViEzeXbYh"),
    "3294776556": ("MONSTR #1047", "QmRLZjamBdwpNFys8uugUW6ZnAYRVgNt6pizLVSDw9Adqp"),
    "3294776728": ("MONSTR #1048", "QmZfrJgFVTSHoJNLuGWPpmzXHbzrhrKoBmu6mxUk8n9odV"),
    "3294776783": ("MONSTR #1049", "Qmd9qYRD9HLvNKC1JvtDS4UKL2WRqRhjtZhcgTd1wiBctj"),
    "3294776897": ("MONSTR #1050", "QmeRnUwU7JZbAKKY9PQyuLEa7JDnSrZwRimdu5swpUTe81"),
    "3294776946": ("MONSTR #1051", "QmNZXQ1JkktpYP5Q7A35tDqsK7cFwS7GiS2uzuZjYN79vz"),
    "3294776998": ("MONSTR #1052", "QmeWQbapFf133PvXLc6jNuaHyuF9oFhUXcWiCpL7KVWLAr"),
    "3294777068": ("MONSTR #1053", "QmNeie4tsTxD5e52TL4YuLmMUZRKrZtKZqri3hT1wXT82V"),
    "3294777184": ("MONSTR #1054", "QmTRz4reNDZrzHoEEQRBXxFyyq9vuWa8RLV5RnMDPenkPz"),
    "3294777280": ("MONSTR #1055", "QmegaWSyv2mhogj48Mf1MfKY1D3ujLLLrbvZbK9Qu6VLuB"),
    "3294777334": ("MONSTR #1056", "QmTzK2E2nu2qjvkqGJo39rWtuDacaoRd99jDEanZr9bbAy"),
    "3294777358": ("MONSTR #1057", "Qme89cp4Zp3iu6DcEa8nejgx51i9NRRBCfQe1sZo6js2cu"),
    "3294777443": ("MONSTR #1058", "QmRovDS4JveGRL6LY6V9pFwV6Kh7RsvdKUP8fxZxZz6gdb"),
    "3294777531": ("MONSTR #1059", "QmcjXmmrfJamacMrhek4BN9tnJBwW9ZhftUbZVouZQVDUh"),
    "3294777580": ("MONSTR #1060", "QmeH5AEBF2xcXeu9ruJXrV1gL6JJJ9NkoszazHWBfRWjq8"),
    "3294777635": ("MONSTR #1061", "QmSfG2XmAh6KyRXuRQhWbjQ97dcki8AYcDXUrJrL4eWBPV"),
    "3294777747": ("MONSTR #1062", "QmZ4ew5or6prKfCEPDhdssqqD7cvJQ2rNZdaiaQ3s34VYm"),
    "3294777790": ("MONSTR #1063", "QmYKkiu97B1dhABqQJNQCBWrMfLEyY9YFnPttkAqpXCa8Y"),
    "3294777913": ("MONSTR #1064", "Qmbq3wvfJRsnQ6WoQesAVm4cYvtYoypVx4uGtkxxNGQSSX"),
    "3294778161": ("MONSTR #1065", "QmWVXyowqwWJ8BFMZuG4ESeXYRoXyCjGJarLdVYptNqTEe"),
    "3294778254": ("MONSTR #1066", "QmXSXG25bYungVYHujYQAX2NgfgXAMcEvKFLa5zNy5FWZp"),
    "3294778322": ("MONSTR #1067", "QmS2pyGS7DytrZ37KuGNaLmJaKoaUJENDBHjAsHfvQoLRX"),
    "3294778375": ("MONSTR #1068", "Qma1LJDwLsMax8ePce47vfYae843pj87QaHRLenp7cLXjA"),
    "3294778501": ("MONSTR #1070", "QmTEE6SyKu97h89LHTxgxKwERtuc9xUFfBT5DBbcGZFmFu"),
    "3294778553": ("MONSTR #1071", "QmXDTgG6eV71LnPaEH58GpLzbhfV3mXbowQGXQi1D6nqAf"),
    "3294778796": ("MONSTR #1074", "QmWGmRPpNFEusRZVTZXyunfmGqaCzpSBwZZ393x1D7v2LP"),
    "3294778917": ("MONSTR #1075", "QmdkvfKkGjY2kXkom4TDFcPjyZyagA6GWLetJx51K8JEWs"),
    "3294778967": ("MONSTR #1076", "QmRVoZEhyRETFfA8S4pGgUFvFjugKRQBL1STs6dcDpFZvq"),
    "3294779150": ("MONSTR #1077", "QmY2c9yVmEyhLM8FjxfBc3pYHzfwubgdpLEpG7JwUR59fC"),
    "3294779213": ("MONSTR #1078", "QmZwGxfn9hwK4uQBFU5as6PwoGQVe15ykFhEgeGGUvPNj1"),
    "3294779312": ("MONSTR #1079", "QmQ2ddEDtKAYqjzWfRPgp5PhW8u2GB5oxGECHdBjeoCtXn"),
    "3294779349": ("MONSTR #1080", "QmSVjrhJGWq27J8r88bhZ8YXxtkUjHFmvb676b3cvYFXvU"),
    "3294779372": ("MONSTR #1081", "QmaX7xjYri1P7u55eejDdZyUPZVCcFpxupAhz3GRUSriAb"),
    "3294779436": ("MONSTR #1082", "QmRQYQe4sNqENNRtBYcuKiCeH3FEg5HCiQoQNtaZuYN41c"),
    "3294779470": ("MONSTR #1083", "QmVBohgCiowGpY7u5fK4ZVcYoeQBsgCKbHeKkpsC26DHe8"),
    "3294779502": ("MONSTR #1084", "QmW71R8QLwontW42XKsK9F4Wj7GsacXeFgxFKLRnsp2PGm"),
    "3294779552": ("MONSTR #1085", "QmanNoe9WnbHyDKf1W6u6mngAzi3gzctTcFc2VHEPhPZAD"),
    "3294779697": ("MONSTR #1086", "QmUdThaC4JANTtidvgpwNW9k6TgmmSSXxpuD2jSFtsAViH"),
    "3294779798": ("MONSTR #1087", "QmYZDT7mQXNA7GBqFyiJDMPop3xdzuwg5pPj8Z221Z5hwT"),
    "3294779848": ("MONSTR #1088", "QmYjyuMfhqWASHtpow8rmg3LdzFBpsGDzbGica6mCJE2UC"),
    "3294780040": ("MONSTR #1089", "QmeLtXm28GvGNY175gsMGXERFPmgpwtKdsSvsQG1sdLR88"),
    "3294780145": ("MONSTR #1090", "QmTx7NsYdcQLvDhgTwyjwdLwtzTHP8fJ3zEnupkkU2cyq8"),
    "3294780401": ("MONSTR #1092", "QmbzpVhFhdTK4UpskFkp98ju2kxHmZktNUQKix7QZhbBLf"),
    "3294780497": ("MONSTR #1093", "Qmf8v52md5SebPbEXwVZvwRBCtEYMHhy8VwBJwC8c4t3Yg"),
    "3294780567": ("MONSTR #1094", "QmPwG8tL5YGMKqZUr8PRFaE6fxLseAPJcWYXpjFTxAJX67"),
    "3294780737": ("MONSTR #1095", "QmSVrc14nW385MYqGK1QJPL7Z8CoTdJ3qvzb7oD1UaeDhs"),
    "3294780875": ("MONSTR #1096", "QmejNMtxvGcBpwM6LZiE3cEb5jiSaTerxKPgHkCns1td96"),
    "3294780997": ("MONSTR #1097", "QmfXM4NrxYR3EEKTnma7V2zJySVzrQC4KSZKS2Uo1UouTP"),
    "3294781102": ("MONSTR #1099", "Qmag29g3rcSf2ZrbPXAx2AGzqyyMMa7rUi1Su4o1tRgYak"),
    "3294781250": ("MONSTR #1100", "QmS3PJk8J6Lh4LUdnrD4G8rN8nTxBeT2QsovA21SBvUCDF"),
    "3294781360": ("MONSTR #1101", "QmdprDyQ56hvrVEjeEbfFjnxQZMrQprUkpad3RUo6RoLqn"),
    "3294781635": ("MONSTR #1102", "QmUpy7hipjfeK4sE67Hm3etsdUwiuKnybxcsAkqz4HT3gy"),
    "3294782805": ("MONSTR #1103", "QmZcBF8DiSu1q1F6ttP7ZaU9oKqVvq2HDHa48BvBi83GUe"),
    "3294786144": ("MONSTR #1105", "QmUzc7PDTV43vGJag1crYrZxmRr2ybvG64ru1ngN3nSmbK"),
    "3294786220": ("MONSTR #1106", "QmXvCLZYKs5DVWz3Qz99aNmhY1ZdXH6eC6QiVTfeA3LZ1S"),
    "3294786559": ("MONSTR #1107", "QmUTzpkBymWeeSP6xo3P6TY3rUT6c8nc37sUDgbRLwdsAH"),
    "3294786827": ("MONSTR #1108", "QmddNvrrmW21oV89u1sRcDTJmD5BZ15Mss3HkaZFwMc2EA"),
    "3294786902": ("MONSTR #1109", "QmezajSonQmXrgwjZ8diL2wahs5Ysw2TeShW3FoVagMrAK"),
    "3294787130": ("MONSTR #1110", "QmWqA96TT7C5pzMdc6XGo37GwcR4osSBnrbWGE7EtbSQZb"),
    "3294787301": ("MONSTR #1111", "QmRykQZPJkQvnGDWRrHt2uwp4M5iaaKiFLPoax84J9N85L"),
    "3294787475": ("MONSTR #1112", "QmVTghEBZPnnubsHFUAGrPuuqGLT5p41QzJqhEDBbRvWsM"),
    "3294787633": ("MONSTR #1113", "QmaLdAptppUJgyBtmRDRjk1QFQfWt2S7Q4F4vtmUKxr6He"),
    "3294787744": ("MONSTR #1114", "Qmb3kst7o3GRBjtY3gDXhenpKZr2RRDMoeDAP6FTJS1t5z"),
    "3294788025": ("MONSTR #1115", "QmcuhsetvgkPe2JgKsoaXdSAdKgDvZS8S3Mt6FP1qrJ7Ja"),
    "3294788170": ("MONSTR #1116", "Qmeyp4L1Naxr1vRvUqsmToWHqjnwRFUnLD7yeN94MXVnbS"),
    "3294788412": ("MONSTR #1118", "QmdVJvfapZS31UnB7FE8nNBsnV79Gk3NZzNgmdQCG6wXQL"),
    "3294788628": ("MONSTR #1119", "QmPfN2wLxdwj4uQEi1LLg7dv6kJN3hKZVPhMiPCoCKrR5R"),
    "3294789703": ("MONSTR #1120", "Qme9h6uHNeK4Pg9ne4fnzDZu4nhDn4tnVUjiMtomwqhxFA"),
    "3294790791": ("MONSTR #1122", "QmPRFa8ZB9RpS4q2ZBGdDcbTBMyPQquW2gmtoNx1F654Qd"),
    "3294790930": ("MONSTR #1123", "QmYJPPdpTyzrvrg2uRYByWjdn44gZJzxZ6VTKfJPaZr3aJ"),
    "3294791005": ("MONSTR #1124", "QmcLReFmuvoD7fTHPTCyS6HQqZjervyieoHsbvxr8regHa"),
    "3294791104": ("MONSTR #1125", "QmQ8DsrfE3h2zSeEJe5XD6rMw8YVsZ12B36ChupC7m4fKN"),
    "3294793122": ("MONSTR #1127", "QmPZkgj8sdHZEYxSoagHRwLuAhBxrEZbrEWmyi3AdeUZkk"),
    "3294791472": ("MONSTR #1126", "QmZvWFMqSDEQsUG3fiu8PrPukgDEz5WGTTwKkZoptGZwT2"),
    "3294793658": ("MONSTR #1128", "QmeFCPnkS8xTERUcziJNPJwzCp8ERgVAaraoPDLZQjTkaF"),
    "3294793716": ("MONSTR #1129", "QmPUBLFggS2XaFsi7UK1DQpvHw3frHRWvH2PYSnJLD5kix"),
    "3294794040": ("MONSTR #1130", "QmP1spQbKc1XvgzeKKF3QYCroNCa36dhapHAPh2PghQ2Ju"),
    "3294796081": ("MONSTR #1131", "QmcLVPHfXvg1JyAQCUNc8h9Xa38iyXuAH8rM9xvfNV1GG7"),
    "3294796467": ("MONSTR #1132", "QmPnAdgU2ynn72U77hjfd9tLqVywhJC5yHD7VBH7FuKP5q"),
    "3294796735": ("MONSTR #1133", "QmbtguZUbv56Ah4JzY2L9EjTLCffVreHL2Gfbc5mWMjChL"),
    "3294796934": ("MONSTR #1134", "QmebMmFJtCRjHcXs4r5sxFL6tukLdt86i7iZ11pqy4Rkzx"),
    "3294797089": ("MONSTR #1135", "QmS1bf3SDQ4Y7tLuiEd8XBxncx8nbMV1FtmCBNtbvV9w8h"),
    "3294797229": ("MONSTR #1136", "QmdrokeA5wy3BacF346nbUUdWEwS28D8DaE9bnMLiYGmMG"),
    "3294797416": ("MONSTR #1138", "Qmcp3VvCUA9bSrVmK3N5qqSpZgeFoLE16z4YpJDNqDx5bV"),
    "3294798078": ("MONSTR #1139", "QmaxsJMkJHzxXPSDWakDxwKVhQMUVbdE7heDiCD11cbriW"),
    "3294798169": ("MONSTR #1140", "QmeJ6uqn2HeYrWd2w4T4Dyr9ndrF5R2pyCJ84i9pTTZWRZ"),
    "3294798277": ("MONSTR #1141", "QmWaQzWpXTKacDPSUL7U7aepQ85MfDaY8GF35tQTLwqXmu"),
    "3294798635": ("MONSTR #1142", "QmcqPMnz1Yt2pJhkLnnVFu6h5JiDeHmwnH5Abq8SXQEFeA"),
    "3294798726": ("MONSTR #1143", "QmTwhuXyvckxWjMsU5pSgo9iZ28HyGjB6ZGV5C64U9xXWR"),
    "3294798821": ("MONSTR #1144", "QmWnhYMjVYq3v7viBqFFzNj16QRaX8R4XgyUmHZarVeDf9"),
    "3294798936": ("MONSTR #1145", "QmNeFdJqNHTc2n1N6xH1cTX6NZvNv5RqzmKn4kkVCW8JfD"),
    "3294801531": ("MONSTR #1146", "QmY1L8FZo1aaHJEZ6jvpJMo599Nf25iVovaRSeJQTKPkP2"),
    "3294801626": ("MONSTR #1147", "QmVtBJ1Md7QdnCjf7MgheNbh6zBVprr27GA6PVV153wiRe"),
    "3294801843": ("MONSTR #1148", "QmSQ9sJTpkcGyVEBVuFKH3gmM4fczvdJGPeXX5WSgw6LLx"),
    "3294802187": ("MONSTR #1149", "QmU59iw8M5Fiw4gmBWiEuZJHmrvAchnQpECnWkWT92aevU"),
    "3294802254": ("MONSTR #1150", "QmVJLix649Q5RudtSa88YYyCF3mdGUEm5byWaPRtY6QEgm"),
    "3294802367": ("MONSTR #1151", "QmSm9skv3uCNiMMtd8XzukBPjsGaVfUVo5kjgsnKHCqFWC"),
    "3294802563": ("MONSTR #1152", "QmbYiUW7N3JAHwAN571q7F6BYCoicfVkAYyKhjU9aRfbZ7"),
    "3294802810": ("MONSTR #1153", "QmZqjyu1FZi6v1foipKWKdz3LxLYhRzE2eGUq1S5CgG9DV"),
    "3294803111": ("MONSTR #1155", "QmayDvSNnwfRZCfaVjEVT5csy76E6KJ6684ixKqtaXfA57"),
    "3294803132": ("MONSTR #1156", "QmaH5f8yFiL2vjo8e4pvrFU4SapkTbL8e2RWpUBTFFfDCy"),
    "3294803244": ("MONSTR #1157", "QmcdXjVKBT2mfwhQrauWxDV8fAiYAVEtUaGBEJhbPZUKPD"),
    "3294803377": ("MONSTR #1158", "QmcemeDWRW4UAz1zSFdb9B7Y3us8Ar4rhUytviP6JWFXGM"),
    "3294803711": ("MONSTR #1159", "QmUY61iHYZ72dMbt1akga1NJNiX31C2pj3sTeNzKHbcybC"),
    "3294803848": ("MONSTR #1160", "QmR9co42KMjwr8wZoUd51DaWxfjmPMvtEG5qnqUZXHV34Q"),
    "3294804066": ("MONSTR #1161", "QmdTxR1tWaUbihTbcCx19fNH2qLhD4q1iCpUbBe4GvufuX"),
    "3294804190": ("MONSTR #1162", "QmePzTB7z8yeaD3SDbUAbgsF7hXcZ4hJRssu8uqSampXVf"),
    "3294804266": ("MONSTR #1163", "QmTg3oFTo5cN6nNGZyhLXcdnDnqeaDuXPrYoiSBMGhzjUf"),
    "3294804574": ("MONSTR #1164", "Qmduauzo45TGNkXYJphThN466sXshXCsf7rmStF4yNKHFA"),
    "3294804663": ("MONSTR #1165", "QmXgh3kQYRFE1FYSoyiaQBzXdHYk2A5saTb1jM6yJ9smUc"),
    "3294805623": ("MONSTR #1166", "QmUdcvyz6Dpx6epu6bqefSduBeQg2uWQPszFjNb859HzUa"),
    "3294805902": ("MONSTR #1167", "QmQYn82XoNjiSUa9dEi4GnNZ5y4eXiMGKtbA9YKFsJuUNs"),
    "3294806098": ("MONSTR #1168", "QmZvo1QoUkgKgGTTjzrFomrZJsBZsnAcT6HxAgSVFo1f8n"),
    "3294806223": ("MONSTR #1169", "QmRrP9PDWRso9nD6eRQBunfVx2iQWqhvbYifpXMmJj3PHk"),
    "3294806451": ("MONSTR #1170", "QmfP8HqhpzxwEmo7iedNxfHnm2wK2Gmjw6VhJJa3Z6vMJY"),
    "3294806531": ("MONSTR #1171", "QmfCNQQo2bRakhvJXH5RmGiE3zPZkJn249j24XbaUdxGbr"),
    "3294806622": ("MONSTR #1172", "QmSPZ2N25ZBMVxMkF41BXrGFBjwdrMTeomwvMU8DJydiYv"),
    "3294806669": ("MONSTR #1173", "QmPAgeGfLVe1SkzNMq3rJjUTFCVC4wSz31bLFBwBx2QRpa"),
    "3294806824": ("MONSTR #1174", "QmVXRQpbLCKsLiVuxKpUG1znJ7pXVuPwdPLJicPAcrD3KX"),
    "3294806889": ("MONSTR #1175", "QmZjPTeNxENAHebS2vYkQYB7NTHkgT2DfoSqepdN1DGrtY"),
    "3294806989": ("MONSTR #1176", "QmasH4Bh54cGsqriHjXzscrihq9cstkSFM6rUCkAzqadcJ"),
    "3294807080": ("MONSTR #1177", "QmdinuzzuqiFoqvHXAqSedg4L631kjaZzBHAeeAJPWfWm5"),
    "3294807479": ("MONSTR #1178", "QmQbM2BMkx28pTmtXaUy1Unb3fGYuLTJoGXmLNQmV9Dg5J"),
    "3294807559": ("MONSTR #1179", "QmaeF8ALVY7QghEb5biFHimyJ3kUhZsBLhmLb9yo1u2aP6"),
    "3294807687": ("MONSTR #1180", "QmXyVTpgQFaFTBRgyGTrWP3eF9BpJnT4zkhXWAZc4mEanZ"),
    "3294807844": ("MONSTR #1181", "Qmev4GDbPQBP1TSDbvL8daDMNefUn9D6SwYbjnJ54krL8j"),
    "3294807918": ("MONSTR #1182", "QmeegvawJkBaZVG5d2NVJu5w6axe8cyzv5QZYky3cY8Ue1"),
    "3294808069": ("MONSTR #1183", "QmQLJ9TxSbWuPsBRFAQLGSXQgX7HVpZVosNibamEL3Yw7F"),
    "3294808275": ("MONSTR #1184", "QmWSojG1J3iAVBNhit5azLo4TPvkn7qP4HE1xNG9Ni9F2j"),
    "3294808402": ("MONSTR #1185", "QmToZonDeiShTrRrkg6BnKTiCoz8yhpcDaaG2PML5DsSBk"),
    "3294808549": ("MONSTR #1186", "QmbhCnwYDNPfEyMVmQ6rNsuvpt2iKDAB5y9E8Dfn24FY6D"),
    "3294808917": ("MONSTR #1187", "QmNbSjM3TushnupYAchA2H79o4w9scusARdFBWCuU31rCz"),
    "3294809014": ("MONSTR #1188", "QmSTexsRQ3uYfofXfo1A19LpAeevce7GUbgzf4x9SorEQG"),
    "3294809127": ("MONSTR #1189", "QmPmnHe545cqH44JeP8yVeyDJAAbFS6D4aVVuJw8s6aYTg"),
    "3294809224": ("MONSTR #1190", "QmR7cL1kaeiJq2ndGNMaG5pK2Y9SRei1KTecvLUBsVSkrr"),
    "3294809372": ("MONSTR #1191", "QmY5sz92dqX3MUGqE4aZFT4o4iAtZbgiaDbKHyRzZUGbjM"),
    "3294809563": ("MONSTR #1192", "QmU7uyvW9nJvauSQzss9AK2TvU2BYCHqtvyaGcgwSUHTHL"),
    "3294809653": ("MONSTR #1193", "QmPbfEJ2GZ43SDvT7T2JUkzxK7nVmCe2aW2fgEwcfDQLr3"),
    "3294809942": ("MONSTR #1194", "QmQddtKKby6dhnnSxjvNYiChgCtZyyMdfze2TBUJsdZu8A"),
    "3294810555": ("MONSTR #1195", "QmVqaSH65218M4SjcU6H1UeVjt2L6ucCtJQLC18Xq8e2yi"),
    "3294810647": ("MONSTR #1196", "QmfSqzobRZwfq6A1KekAVVbcF7dWsgCpy7evhKe6SrbWcT"),
    "3294810800": ("MONSTR #1197", "Qmdv8ckzFUUasrYpPeaMiWAq1KtsTpvXCzrWkHRRStckP9"),
    "3294810920": ("MONSTR #1198", "QmVB3nNj9ToRj1xXmSSEuT2vA9a1fybhExFVy1KaP4zr8M"),
    "3294811207": ("MONSTR #1199", "QmYCGMv777w7fvER4bssz9H7asdCYpYFLzBcp3iChUaRXX"),
    "3294811360": ("MONSTR #1200", "QmULUGxw5mT4x7scTZf6SdVDxcvmEq95Ud76h91MGSH6j5"),
    "3294811513": ("MONSTR #1201", "QmbijbEoEKJFQ4LrEcbw1VzzbCqwGxreQUfTtMinQohQAb"),
    "3294811733": ("MONSTR #1202", "QmVjWWoSq3GcjyTRLDdqhRYTehw1F2GRF1a6xBpsWWcQLv"),
    "3294811820": ("MONSTR #1203", "QmVb5yTKvgvTvL7GmD2DsWDzuQZsfudTxa7LW55Vciz7sm"),
    "3294811911": ("MONSTR #1204", "QmQAWLkCz9rJ9TRE5KwHE798F5JKPT9CYR6jcPcxRbEG1A"),
    "3294812382": ("MONSTR #1205", "QmVSLEpBKmi3j8J4YhuFoqb1tEqHAmYG81YS6SvrU9ApCx"),
    "3294812474": ("MONSTR #1206", "QmV6itqwKrC1FK6hHEazhSUT9ZruQGPzrAd2AP8iRkDdH2"),
    "3294812571": ("MONSTR #1207", "QmcqWiEVBRorCFBKojtJzf8DnbhKGowWPYo4yxXnSYsbde"),
    "3294812793": ("MONSTR #1208", "QmVMMYDnJiBUNztkpaeRddAKuCq6hAoKqZpTwMFWCQZpoS"),
    "3294812896": ("MONSTR #1209", "QmdL9xGz7YvnhfJko16mEMYuxvg18Lp34d42tPtbMTvvLT"),
    "3294813148": ("MONSTR #1210", "QmYDG11vKNMiz1CnovUtvyfDXc6paCCRRhZmpQTK3JGZTt"),
    "3294813436": ("MONSTR #1211", "QmRGZhgWMFBooAn5M9tVkE3eeNykBn79Hrsc51GDeigg5C"),
    "3294814010": ("MONSTR #1212", "QmeQEt9dG7wxBGQBrqyfZYDi639gkV3fvTMM34YdaWYVV1"),
    "3294814100": ("MONSTR #1213", "QmbdyPBvL2nwQcZLpzxMmdwx8uKxuZrVUJk7pxMHMSedgd"),
    "3294814227": ("MONSTR #1214", "QmbiKSFHhj9kUhCysE9dqitFmQCWxsKTYWwueEbXuYWuvX"),
    "3294814505": ("MONSTR #1215", "QmY2cyNJnXCEiA71asZ5Jcp2nvVSpe5ZSPEXbzmPnj7juj"),
    "3294814745": ("MONSTR #1216", "QmdoK8miw6cH5YYyot93ZnrqBiHK2fNUmte11L8jmzhzze"),
    "3294815176": ("MONSTR #1218", "QmYuAiA8cGL9zCwJwbiQG13jDLKDyk7pUFrhC3QoRVb9zy"),
    "3294815464": ("MONSTR #1219", "QmecGkAVugDgHbTbfPqmTievbxrexScMzFvxS8NjDKCZBY"),
    "3294815759": ("MONSTR #1221", "QmSDeMxhSJoPiFj4qQEQy7uBnKhuLEEi2Wdv7GwjcF5ByU"),
    "3294815985": ("MONSTR #1222", "QmZs79BraQKGps6W8M5PoA4Lfkm158T9BkNJMXekMgBGfe"),
    "3294816135": ("MONSTR #1223", "Qmeq7fcUAeUuNQqKKw4gJaU6iqVwFgJk8YXWpKgFUPQSj7"),
    "3294816312": ("MONSTR #1224", "Qme6M8mYRNaPKJ5RyMFVcyD7EnZf7zzKD5SDNqwBwrm6fU"),
    "3294816728": ("MONSTR #1225", "QmegivuoHFhEkgk5RN1zEMFApk9DnwMQ61BJHXboFB9Zck"),
    "3294816881": ("MONSTR #1226", "QmdcRS84F7m45Vwd1vFVuRsYKpNzjriGcRCbQXRTQbnpDv"),
    "3294816978": ("MONSTR #1227", "QmaMKF8YdCUZJLuDFi22U7EKrXB6zNbsYYfK2M2sW4No35"),
    "3294817337": ("MONSTR #1228", "QmPtfU5fc8nmkfUMU2S2goRCWEeKYGoBjRatxWMVkGViiT"),
    "3294817604": ("MONSTR #1229", "QmZhsenTf8hpjXiqRAsWEJRs26vhn3PJAekJJeD3qSrc8H"),
    "3294817840": ("MONSTR #1230", "QmbK4w5P9gJNUkwcHwLYD143F2cSt999f7P93kvA8DX8M7"),
    "3294818386": ("MONSTR #1231", "QmZGocyrpsvPb893TzdCHKMGShQsFLUtXEYS4u7kYpWQ5K"),
    "3294819173": ("MONSTR #1233", "QmYv8wBvmJsdNaWgatukgHNMQMfDXaSnwigdUdJzUcTJed"),
    "3294819408": ("MONSTR #1234", "QmeHCVx3hxUvqLum1yoHN5okvHiaPvkEpJ2ak4XQyza3Qg"),
    "3294819724": ("MONSTR #1235", "QmVZbHbHUyYsGis8jjXAGTGRTULfYVzm4Ab3Ckv9pbk3Dy"),
    "3294820262": ("MONSTR #1236", "QmWMejbXWTSXcMPhedENV2npWSERpiw9qafTmNnJBX6FgV"),
    "3294820543": ("MONSTR #1237", "QmbFChikrAt3w8tyABLfdx4fhXGpJwRrhwkoHbZDg8nhHn"),
    "3294820731": ("MONSTR #1238", "QmZPY9438PjVwM2f1fu37kYvs1SwXk2txLhewDsHeDvgUc"),
    "3294821311": ("MONSTR #1240", "QmTrsZwLwWq6uynVJ24baK7oaRNcm166PDjHjmxQMRCmEB"),
    "3294821460": ("MONSTR #1241", "Qmf85t9CU4ft1jzWTjxG5a7kGk8DmNmYdxbgvgindLHqZX"),
    "3294821701": ("MONSTR #1243", "QmSpqVJP572WrYRMWXW2JUAmedfotdnZZvzRKLm97BbRgn"),
    "3294821778": ("MONSTR #1244", "QmUyoYaZnwu2CETyTokoaMp5aBHdy84i5SvHCgexZPSxmt"),
    "3294821873": ("MONSTR #1245", "QmZTyLJnxEkaruKr4k7xFH4ARyG33JWYdcJ7XickZqRZvG"),
    "3294822047": ("MONSTR #1246", "QmUsZkxzBQmDdtn8NZwJ634GRps824uxZ5wmKibawMBTyX"),
    "3294822194": ("MONSTR #1247", "QmT5tKcChuQdbc26NsEb2tCD7kqgNAd9ZZzoc5V7z8qnPc"),
    "3294822239": ("MONSTR #1248", "QmSnmvEY16iBgBEL6A74DNcnKX8NJf2qqVPqJJjFMmM1qo"),
    "3294822407": ("MONSTR #1249", "QmTqp6yeq18LJ3BWn1qWpeyHt7D6DFntHCu42TUmkhGvhx"),
    "3294822522": ("MONSTR #1250", "QmUeTqktW9CwaYXJKn3APeNwKSU3FGH3JkiY6d13emHHtN"),
    "3294822935": ("MONSTR #1251", "QmRna4JaWdkxWztHmrgSQT9DE4YvkcTiqAbCZRQd3TwEiU"),
    "3294823167": ("MONSTR #1252", "QmYKm7ZZQousawJGRCpdoB3qsTHC3TxBq92QCm9kKCjemA"),
    "3294823522": ("MONSTR #1253", "QmbbZV7wkt8WabpcAhx3g8MmJYPYEDvtrFvFU8gRAx4ahj"),
    "3294823651": ("MONSTR #1254", "Qmf8eM1ARxh34Xoh9WXKwCDj74yDig9KXL7TT1oxBgHpF4"),
    "3294823924": ("MONSTR #1255", "QmfLgbjtTZnNmG36yKUXpFeMAumxJzw3wDh1cug1SF2on3"),
    "3294824012": ("MONSTR #1256", "QmbPZBP1BHz91oprDxCTT7ibMM89BCc1g5tWaKPspDJYkt"),
    "3294824371": ("MONSTR #1257", "QmToMoZkH6zJWUKZnkNeRxYjujJ4SxiJ1fywVbaDQvu7sE"),
    "3294824674": ("MONSTR #1258", "QmRWqyGe5krf7BqaJRaVkbdcaFyuagSnWRb14QFcCLuzXX"),
    "3294824849": ("MONSTR #1259", "QmXvnSvGxnvDTzJRaNUfU4MneoeyHAqHNACkreJjWzWYMs"),
    "3294824996": ("MONSTR #1260", "QmadKdEfBB8MPMzcvU22QgH5XFHG5xX8oTn3tqNK4EcPvv"),
    "3294825499": ("MONSTR #1261", "QmRVAUFXSLPeTVJ8jnZfuocJzrdun8KR7VPtpJm6UpTYxh"),
    "3294825586": ("MONSTR #1262", "Qmd6zsYJ9RoJyFrBBe1uexQZtE7U8hRjAwKJYZ117CgGpS"),
    "3294825867": ("MONSTR #1263", "QmeV1hK1pV6EbaMwALRBFzrmSHKsRyMi1apsBF4CR9t662"),
    "3294826120": ("MONSTR #1264", "QmTvcm3StVVhrM1opjkX9UA3Uvwed7MesecFMGCSmgKP1u"),
    "3294826807": ("MONSTR #1267", "Qmcm5fUaWgLUboCDNq5G6DF3WKVT44fhac9yktZwjM3vz1"),
    "3294826861": ("MONSTR #1268", "QmZLzKmGXAteUJVW6TpMrs1rza1F3A2oURp3Te1SYVmM9G"),
    "3294826969": ("MONSTR #1269", "QmUoE8hjX8uYP2QDNWzurGRavdga5vmK5Qz5xdj2Apvi1i"),
    "3294827091": ("MONSTR #1270", "QmaYCGBpkPFMSioZxKGJC3Yp8hCRdkxBiGexuCKnLxAth5"),
    "3294827232": ("MONSTR #1271", "QmPMFwD1yuNGRv9s99h5mkCN3WuVV4zyziG25mei7A4z2J"),
    "3294827317": ("MONSTR #1272", "QmeoktUp4RvAWaaAQJBbf86EoWUDM2dc34DJ2DAxrAXK8u"),
    "3294827445": ("MONSTR #1273", "Qme3EFLLPEpECNeNGH259BNkKHAw6wYk66oyQtnx66MNPf"),
    "3294827572": ("MONSTR #1274", "QmNbW5faoTBWYmqxXbjGVA68EmQmvvPnpSQNNppCrYodki"),
    "3294827673": ("MONSTR #1275", "QmRxWZX8e5cSszQDynBNmtBZEWz3gu3m4z2DRQf3JWV6Bq"),
    "3294828011": ("MONSTR #1276", "QmVu7Y7cJMxXrJgEXaLdpyGHBwBLVQi5gua5hYUfzWX9wf"),
    "3294828466": ("MONSTR #1277", "QmVCBfMoskUtWA13L5WtKjChrzbLnCPEMbngey1HK2UhRV"),
    "3294828654": ("MONSTR #1278", "QmeSQeffHY75WxvffuAh2i6APH5g6FSVWng6cryVWLoeVU"),
    "3294828948": ("MONSTR #1279", "QmZNSBrGqLzHCNp9FPZnd8YVuCWfJmpWkwYgRmGuk6jZff"),
    "3294829147": ("MONSTR #1280", "QmUFehEVS2zbfyJGHthVQNDFDoRSRW3UU1YoHFdy4zD5yT"),
    "3294829288": ("MONSTR #1281", "QmQFGB31AQqtGEWVMbpLJKT14P57ts2o7iVGPBFgx5CrQn"),
    "3294829394": ("MONSTR #1282", "QmY92u8G4yYgLKrGozETLp5SMZrSmMUCvdygGxZuy8VRQ4"),
    "3294829542": ("MONSTR #1283", "QmYzovo2LUwf6JkiCo3gBKfSAjWmSbtVfoF9K1YAin2qhG"),
    "3294829957": ("MONSTR #1284", "QmPxvhX9cxsHQnnNUkq3aSjEbuKQhXdzKZerxN5B9u1cDu"),
    "3294830344": ("MONSTR #1285", "QmUuxzMyBdruxbMWd3PW2FFaGV1QN3TyH3gFrdmpUja7DQ"),
    "3294830479": ("MONSTR #1286", "QmXoRsbShQHH4nH4pTE1DR24M8F12ya7QVPyJiaf2H1SpP"),
    "3294830620": ("MONSTR #1287", "QmZPT96NGvaiqiNKir7y4RYkZbHK22cneBKvcA1BEZdhR6"),
    "3294830972": ("MONSTR #1288", "QmTmZ38JfFECqaD4UT6uuUfMkb5A3rji8gp5QxtxuqmgWT"),
    "3294831045": ("MONSTR #1289", "QmYMKDrjWJoJqTRbNLsJksDUDkyjxKr2xpmyBo9WwJuNMf"),
    "3294831150": ("MONSTR #1290", "QmVwHCZGxkS6vhmReD93d14zQanELT5UajbuRLdPGtzpxa"),
    "3294831229": ("MONSTR #1291", "Qmdhz7L2wggtfVbAyuRnAKjmzeEmVQ3GBcnN6JB4KG8XP2"),
    "3294831501": ("MONSTR #1292", "QmchZNqDd5vGDpi4HQixDYD7U32g3HhKHkX87qvv8H9EKy"),
    "3294831579": ("MONSTR #1293", "QmQ83D1UxpTCJSQNwYTZ78rG6ip5ZzSAtJHwsj4Rv1vwVn"),
    "3294831681": ("MONSTR #1294", "QmWNy7kA2mZZATJPjYAkyxDdBsGvjpr6evDjBXeEoDnoF6"),
    "3294831812": ("MONSTR #1295", "QmdAhLxQv4nAaxKHYyXyrNPM4fT3JStLFZYd1ua1B9doJP"),
    "3294832011": ("MONSTR #1296", "QmZpXWBLJPUmPRvkVnSEowDqAspJMMituSiw8FAzJvQdSF"),
    "3294832172": ("MONSTR #1297", "Qmchde54UXDS6LVpXtiAEkxzzcwrj4MPUwyjZLATeYMKzN"),
    "3294832260": ("MONSTR #1298", "QmfBcjaGMWLcQHpaboXYno1hYh8wt5Qc2NjhbfW9dqZw1j"),
    "3294832394": ("MONSTR #1299", "QmXikKJAj4U66vk2jXx7csUErdyCcaxvdYhCSMYiAfJFTa"),
    "3294832480": ("MONSTR #1300", "QmWPFdJAHP9vqnQAQJEgLuLGyEdhdGhVKxUNmoDGib2mBM"),
    "3294832584": ("MONSTR #1301", "QmcDpFVg8Duaug6TU218mwE3e3vvfdW5CSR6czzSNGUcm1"),
    "3294832709": ("MONSTR #1302", "Qmb6MiebRWk3GQvGqUYmNp195vsw5tBM7UN8crzj3GY8D9"),
    "3294832951": ("MONSTR #1303", "QmeSRHzMUDHvTaNXnQ9qqTcuyJTgYfL22cUuek4bLHZPoH"),
    "3294833059": ("MONSTR #1304", "QmS1s7RaoRLahUw6J7EeRWdonub7XkLt8j3rGhWNNxF7bx"),
    "3294833173": ("MONSTR #1305", "QmPyfRYVf1qJ4eFeR5PPJ2qCbuxfR3aJQLXW7hzp6VWjkN"),
    "3294833311": ("MONSTR #1306", "QmRFdiawtU3grBXD9YG5akMiikWocPqjdTdnLuA1LYFM2N"),
    "3294833366": ("MONSTR #1307", "Qmdq7NbkgkWfaeBnVwpRbA2azuYDCE2G2ZVvzDR4uPuxAv"),
    "3294833540": ("MONSTR #1308", "Qmcm3CNZqDQyXuRxWsP5RdbMAGK1UTbL2GeFNR3X6FxXNr"),
    "3294833642": ("MONSTR #1309", "Qmd2GKYJLEcTmMJxs77c1r8MTQm7Vifh9cixnjmHscdBdj"),
    "3294833842": ("MONSTR #1310", "QmQWvM5EcxvPKHghnnWAJTiyJ9vJfQgJykgcEKNNhbX3X3"),
    "3294833923": ("MONSTR #1311", "QmQSX47RDFz1B9nmv9sjjksAgjzTZ1S7nz2kSJH5kqwoQq"),
    "3294834014": ("MONSTR #1312", "QmTdsiBrNgPoKBtPvDHtQ1jcKuCZnAc6etVfALX3PtRDMU"),
    "3294834382": ("MONSTR #1313", "QmPwJHNDCy444kJ2vYe1rRNZH1uBDKWSZAM3fJr1FBdNPG"),
    "3294834532": ("MONSTR #1314", "QmczisMjdodp6GZ8PRnoxAkngSs1T1gxzFyutHVVCyjbtA"),
    "3294835303": ("MONSTR #1318", "Qmbz6g2swXhzcFAm5buGNcXMpfcdWxA8tMgfBCDdeZraki"),
    "3294835776": ("MONSTR #1320", "QmTusTV2vZsBFy1o5btcguGFRo7Xsi92eHSxgv7sZ2rLQF"),
    "3294835903": ("MONSTR #1321", "QmdFbwJCot8X4RngjmQYxhpYnP9X7BJnTVTkKMiAD9ru7z"),
    "3294836030": ("MONSTR #1322", "QmZ65TW1KADSCdfTBCLK1fmnB9CtL4711ZUguS5zpBjQar"),
    "3294836484": ("MONSTR #1323", "QmaFo6FHkmYAxWxCuD8rtzcoNyaCSsgxYuHLBSP8CsGTmX"),
    "3294836684": ("MONSTR #1325", "QmVAeu8Z5jugSnX5DQiZZ34UYLaNG9mXyKR92RJ1nCoafg"),
    "3294836747": ("MONSTR #1326", "QmaHN1Ls6yJWJebrmJB2c3gV1objRq65XWVxe6aJfi7VmY"),
    "3294837410": ("MONSTR #1328", "QmXmYwMxBNMAbcy1EkuXv33ngmj2KNN4WabVELHNR3iQYx"),
    "3294837456": ("MONSTR #1329", "QmYZtdRGSdu4saBURGnjHqY311Ls2KwEi4EfMUy9ccfX1j"),
    "3294837619": ("MONSTR #1330", "QmUtNehW2ndDF5KeT1Hxgw9MLKxemvx1tK2pceDugEE1mu"),
    "3294837781": ("MONSTR #1331", "QmVW6PKxn6Q7EpTTXD9HbHXJALYT4pL81JRFTExHzMixaQ"),
    "3294837858": ("MONSTR #1332", "QmUZEWe6SrTPk3tnJKfsiQZJZp57sUcYJAexYyBy1A6x5g"),
    "3294838024": ("MONSTR #1333", "QmQmA1v4Q8BGEUVd5Sbx3rjx19zVjxJoAGufYVGN6Exeoj"),
    "3294838194": ("MONSTR #1334", "QmfSpZk6QNJhbrA5yqxEVcfZM7tvsE8qzSbGzLoyTtp8Qy"),
    "3294838277": ("MONSTR #1335", "QmVvJ4SNdwodcXnDdYKvRPbAqGEECyVCMZ3xPr4jzPE2yT"),
    "3294838321": ("MONSTR #1336", "QmfFFUwC3jDwPefhtfQ95bu9vpM4qmQJxF4Us8Mhn43bnZ"),
    "3294839044": ("MONSTR #1337", "QmPqWDm9APzPuZW1w72A8xBh9uRrTWZPgUaPvssgLQ9dWN"),
    "3294839319": ("MONSTR #1338", "QmSjuwQDYY1e6bxE28H9p1LVMCVKjyE78qS3waQTpZDAt6"),
    "3294839931": ("MONSTR #1339", "QmVKWPqrk2dSEjRbB9YWtr5uuSoVSEQFmoiKxcpJ6hyYAj"),
    "3294840081": ("MONSTR #1340", "QmcAQiL85E4tVS4zJiSYeQr3ct9fAa47wdX7zuK9TZMd1q"),
    "3294840490": ("MONSTR #1343", "QmNjYXMweBFW6qicLtR3RKThLD2FvdCJEiteuvc6jmREoi"),
    "3294840672": ("MONSTR #1345", "QmbCB5CaSA7yexAKwXQNc1HLTvV8DrS6bGBnKNf81Dr2JQ"),
    "3294840732": ("MONSTR #1346", "QmTbcdbkCbohNXe6hr8HssjBUbA45afPPdp2mY4qwQcTeD"),
    "3294840866": ("MONSTR #1347", "QmZkjCbaAwEaPQhVTNdxnQJrojgYfSjsftQZ8ydxv4jYrd"),
    "3294840928": ("MONSTR #1348", "QmRZsLpXNkWutxkofeju6ZaNc7r3roPbK7ethtZnVdQSNG"),
    "3294841012": ("MONSTR #1349", "QmRJMVn8uoFPsgqWCzhUcET6Bb1Dh1FFas6bpj93VnVu8w"),
    "3294841520": ("MONSTR #1352", "Qmek2HwYo9Kz72L8s7QePenzgeEpXRWKGRfjkxRoGp2oqk"),
    "3294841622": ("MONSTR #1353", "QmeBX79cj7dMG4L5uYvmTGNGnTTkP53jRCCrnuaiCodbq5"),
    "3294841700": ("MONSTR #1354", "QmdH3fEdPBa3j7VyASzpRn5EyfGRUDnYAzXJXcVzZXe2c2"),
    "3294841764": ("MONSTR #1355", "QmSEsWGhq7ttvtekbYVtrby3X93BwNxQvzudD9Lwz6dvRY"),
    "3294841859": ("MONSTR #1356", "QmXfcntb36AYP2EvSA4TSK7JoKUSLDnD9USjbNhhWhKKj5"),
    "3294841948": ("MONSTR #1357", "QmR9rWbXjzh4YNYijhBYLtyXHxknLisFhSkkPsVTQAdR13"),
    "3294842082": ("MONSTR #1358", "QmcTzLiJQvf2taBpXtY5GzjE2TbHEGoRjUUSijtjz4ZoSR"),
    "3294842176": ("MONSTR #1359", "Qmcgf75yLAjy7REqaRa3GeU2YQwc7TnrEMXUHjdr5QLSVK"),
    "3294842309": ("MONSTR #1360", "QmcxRW6fkT3bdfdB7drprRYGWKeeZRtvgzmVj8uwqGJW4J"),
    "3294842456": ("MONSTR #1361", "QmQpC9oaaq9jq3KGC9aGxP92q2dqK7rv2okFafh5eM7EuP"),
    "3294842566": ("MONSTR #1362", "QmVr2ADYZZzy6fHGnTx491ifZLJBVXbekn8fYu97opwimU"),
    "3294842709": ("MONSTR #1363", "QmdyRXacubSnx83aFMsDbA7yz8XeQEVH3LZz9DntREcoFG"),
    "3294842820": ("MONSTR #1364", "QmXW87tnpkxRrTTpWm2ieUKZBtTN6biavYBteqZGz52jwv"),
    "3294842925": ("MONSTR #1365", "QmdnfdFW5LzS9g5nA6FEThSKLX49eyBtWTSqKT9XWr8YJz"),
    "3294843067": ("MONSTR #1366", "QmSY6Miuxs9SLxSV9Zj26Xh1p71jdTnVrL4vQkrCd1NnkD"),
    "3294843407": ("MONSTR #1367", "QmSRxA19DPii2M5NZ63Kx7XSrqDv4zg8E4KssfjjtjwChq"),
    "3294843534": ("MONSTR #1368", "QmPoShgwpsscLPBz1JufvgKEmpntevub8PjsqfEYdEVcuw"),
    "3294843604": ("MONSTR #1369", "QmdVahK8wvysqaB55FK4bMnTQ79emmEHVMxQVSLhFAcLcH"),
    "3294843688": ("MONSTR #1370", "QmYfjnx2n429kJfDhW5Hm7N9uB7a3aJknnhYLmkcNMptMs"),
    "3294843872": ("MONSTR #1371", "QmaMxHW56ZYwG255EXTJchFsuJemF6ZssFghutLzRzkfwM"),
    "3294843915": ("MONSTR #1372", "QmVARNRqr8wVAooJdyyx7f8uWHiwYapwz8qZ5zxAg5Lxr7"),
    "3294844329": ("MONSTR #1375", "QmemUJMiia6EUCqSJNsiwUAUwVzPjcF46SoLu8qUaAzq8k"),
    "3294844562": ("MONSTR #1376", "QmPQe1gmg24AhGzayK9ibFR1WTfdKCcu4X938G55g2E9Gt"),
    "3294844733": ("MONSTR #1377", "QmaxhjEKvcivQLy7T4QV6J1mgcvandBLU6yBCuBriwt7P7"),
    "3294844822": ("MONSTR #1378", "QmPjoRndRkZ7ED7S3BSHJbH6Bb2EZWQuBHwrdBkK15Boaa"),
    "3294844911": ("MONSTR #1379", "QmSDaTAR6VGV22P6bEbEasCuzC3qZqoKJ5VSYDszzs9DaP"),
    "3294845052": ("MONSTR #1380", "QmRYu8GNovg7iTRAtN7mz68VbWosvDf1skvQohiBB35ngy"),
    "3294845127": ("MONSTR #1381", "QmXycF3NSTB48kfuk815J1kjP1wtz73EKeWnuBzYQ4mSBR"),
    "3294845260": ("MONSTR #1382", "QmWWH4hARcjrWoLpjeztkQgN77i99LQ5CaJyM9LbxwUB5C"),
    "3294845338": ("MONSTR #1383", "QmYacGuigJdXrRVuYMwxzSUqPuardp1figuBiPr8gLoxuv"),
    "3294845495": ("MONSTR #1384", "QmSYTA86APeiCUzc9H5ze3b6T4w2t5jfP7XVJ4Sd81mbLe"),
    "3294845545": ("MONSTR #1385", "QmW1RfAPJ8z7gEDMiHC7Ue2aMPYpfjs7myWYjn7pXDWqFq"),
    "3294845715": ("MONSTR #1386", "QmW8q7eFFtL4taebX7VSRnJxgudzwynPjZy8JnGifegyXG"),
    "3294845864": ("MONSTR #1387", "QmUKBcSev46Cy6cJkohsph9U5pt3qdwKL3Pr35RuPQzWvR"),
    "3294845936": ("MONSTR #1388", "QmXDKYs4f7eRjWz1QYKKAtopxs5eSJ8sGjUsCkwiv9eywS"),
    "3294846093": ("MONSTR #1389", "QmWMWSw6QZM8vur5jCw8g2tKyZEJxPNiU5hepbqvQNfrud"),
    "3294846636": ("MONSTR #1390", "QmX69CJLswV5h7SyvXjymgm2xNesXxLgQxdXWSrKBvkDcA"),
    "3294846734": ("MONSTR #1391", "QmbtV1amfna1vsFrAdyDajVB7zgCXYGbosfKL1DVQh8i4k"),
    "3294847021": ("MONSTR #1393", "QmTNNdsqUFNCVXQTziTndEf2K8inp3SWwyVrU8ZhJdzccU"),
    "3294847179": ("MONSTR #1394", "QmeG45mfhWjKJFyr7czWZivGkQvGwA2AhUq5sEKBK1bhUP"),
    "3294847287": ("MONSTR #1395", "QmZBTz15XiCeerLfLdqWZV4ErD9yyta5Zsb8NVqN2kWTDV"),
    "3294847571": ("MONSTR #1397", "QmP9AiWGkAhMxEEf7mWSgonbkAJHYRG88yDLmp58JDXD3V"),
    "3294847672": ("MONSTR #1398", "QmVSwUjfT1k55YHBGpoZZXk18XSNmsN587e676ijpRX8mD"),
    "3294847755": ("MONSTR #1399", "QmRH18r1TdGcZzxXUiSLPAM8wEc2kenKyRS2YthRtFwC7o"),
    "3294847814": ("MONSTR #1400", "Qmcw1rWUUB3S6qiBGJCXDLwkfku8yFKKyvHnWUKZPdWLFc"),
    "3294847912": ("MONSTR #1401", "QmQKfSTAUYD8jeiJYg8eHayjQjnu9jJF5HE7nKEm8c3qcK"),
    "3294848001": ("MONSTR #1402", "QmT4juXBNQkMBwxBUnYZEQNADyCwKu4j5ECbChuNjn9Kxx"),
    "3294848332": ("MONSTR #1406", "Qmci4XuP41Vt4aQcTn61KUDRXT5XHdpMgZCfmUhnExQq8b"),
    "3294848099": ("MONSTR #1403", "QmdFEPqAZdyuoTM97cVeMad5GRyUz6kFDYdVot89vrAAyb"),
    "3294848188": ("MONSTR #1404", "QmZ6gwbqbHkeYbidFiKAwyD8SKNb2Ti24gD96T93vtJBBB"),
    "3294848277": ("MONSTR #1405", "QmQbE7oZFcPFVYUFS7vZgW4CNrgDVhJTjTCGMvtYAieTy1"),
    "3294848449": ("MONSTR #1407", "QmTdLvspjqxGkH6hAMCHQV5KfsxoAay3JVxT9RRi9ScVJB"),
    "3294848573": ("MONSTR #1408", "QmbqiSVndk7wW9rwdv6JGXEzUPDErxX1arZSg33hyH2oov"),
    "3294848665": ("MONSTR #1409", "QmUeFQZjonGDcE8uZXRaPA2QQFvgtKZSWMJLNx3jJfi71Y"),
    "3294848758": ("MONSTR #1410", "QmeaRHdoLmqv461DwYdc4CJ3WB111JanysFEDgomkDe2y7"),
    "3294848833": ("MONSTR #1411", "QmdMymKXWgyWeabYrx1erTQb5m7qPrGdLdzCTHYJ53kEVA"),
    "3294848894": ("MONSTR #1412", "QmdJinfgF1LCZk1kThqLTdmrSVLrc835T2Mk9ubTDHGsjf"),
    "3294849229": ("MONSTR #1416", "QmVQwUuX3puPejghwr27a42jBUcM6y3P6s1rafDrPRsA2M"),
    "3294848971": ("MONSTR #1413", "Qma8odtrj2T5mEzRRxpGXW8kMoFqZ7qvqXg1CbTuBjnGnV"),
    "3294849322": ("MONSTR #1417", "QmRgqueYoF5jL4KYQvmbE8v6ZDt3DX6dCRaZua8cMwEsxP"),
    "3294849486": ("MONSTR #1418", "QmQoRxqaAZkFRpYdm1uvvSbrRiC4L5Sg9cyYFQ3ppAUa1o"),
    "3294849666": ("MONSTR #1419", "QmSAEk2uhKj4ZPLDA5AVCGTFcjn2YkKpXSFR2ewGp5Aq4v"),
    "3294849733": ("MONSTR #1420", "QmU3PdLqU4ejNJ284RQ3X2mWrcuNZEDviiJR6H2ki9sgXc"),
    "3294850211": ("MONSTR #1424", "QmNSYKqosz159SDFozbjMCGf4LeB4iChHSWFpQ1q2xKyXy"),
    "3294849814": ("MONSTR #1421", "QmPFeCyz2A1UtniwVRAhFxNNLMqefnx4t3RdZK1KSzY6bd"),
    "3294849913": ("MONSTR #1422", "QmXQPqhTPsmKzWX2oeuFBYZ3AqnVhL7ZwVvVryzD4u8EiK"),
    "3294850155": ("MONSTR #1423", "QmT7i5hJCxye9W7RkdtGWXpJ7mR5T6NprhzV68jbi3riw7"),
    "3294850320": ("MONSTR #1425", "QmVxyS8Ebr9SopNK1fk47dHdzK8oB1TdNfmi43o3Lb1z8b"),
    "3294850388": ("MONSTR #1426", "QmSpezTVjQXMAV621romp9C6JQ8w7ekGYnPkxCMmbK8wvf"),
    "3294850507": ("MONSTR #1428", "QmbvLCFn7CaYKJ6oy49LVReuMVLdFYDY2yCGMkdFUdXheN"),
    "3294850609": ("MONSTR #1430", "Qme6dRCjLw6xVbC83hBZRVkJXaHUejQ1uobwHz2Ci1o7eH"),
    "3294850661": ("MONSTR #1431", "QmXThDELpaPEvrcBbtQtMF2nWa6mM73ae9shLVGtNju9ok"),
    "3294850783": ("MONSTR #1432", "QmQCPAHQabnu18UTGEF9XLRUwdu8nGtYK4husc3D63rk4v"),
    "3294850934": ("MONSTR #1433", "QmaPoSovhftWn214qS4dYWGZEecH9nCnne2HdjV7iSLmm2"),
    "3294851016": ("MONSTR #1434", "QmbDn7Zn7GEj99cbJ8gELG6Wju1jjy6nab1A3o6HEMoUeH"),
    "3294851080": ("MONSTR #1435", "QmRKa4ZdguTLJ1hEWuEVvEtYTebrRGawaJ8wRw4EyAoNuP"),
    "3294851164": ("MONSTR #1436", "QmXwZi9zKSeNxums53oJDpo34YfvZSjALfRTjjH8sLunHE"),
    "3294851220": ("MONSTR #1437", "QmbgjzcSryXBmymHZu7561HSpjb8y8FdPMgwxzyPoBxGcg"),
    "3294851354": ("MONSTR #1438", "QmbvnNxqSWAkMd6jnsJwPFhuYinWscjhydQHVkVyChCBSZ"),
    "3294851462": ("MONSTR #1439", "QmUUAZmyQ1LzgnutWm7WYiE3qxbWynvMFmoB6EbRb18NKA"),
    "3294851929": ("MONSTR #1443", "QmVjudwhyKniJoyjkPih8URhjBEk2ZAbkpyw29avyM4VTu"),
    "3294852060": ("MONSTR #1444", "QmTGWXQ22jfCGRme2ZtNCRY3DdVgSC8Y7D78GMorE68txM"),
    "3294852239": ("MONSTR #1446", "QmQuzHWqbQdrXbFuTK9hWj6npqXjVt3Vp8jGmimKwM6rQX"),
    "3294852295": ("MONSTR #1447", "QmX2mC5hYsznHFMKBdsR87yMdqpXZXQaugq9ewsEsP1tP9"),
    "3294852532": ("MONSTR #1448", "QmcCunV8TY72y153aygnkNuKP7NZGo3nG7aLjYesGJHypa"),
    "3294852658": ("MONSTR #1449", "Qmdy6SZQox3baoVqgcwQuJBo77hWMHyhTP3FBJovcfVECt"),
    "3294852779": ("MONSTR #1450", "QmY8F1iJ3GWZQig5sezvrAvLvopypM7bD59FMKtDbGPWT1"),
    "3294852847": ("MONSTR #1451", "QmSQG2g4TfGUbd6d5zEadcmPXR6gGA5c8iqSVPxwYsi81g"),
    "3294853029": ("MONSTR #1452", "QmTGZAJ7zmuA3m5GufYHSnqnnh5ZPpkeJT5tY5cLUVNig3"),
    "3294853219": ("MONSTR #1453", "QmWnzveh5retUUEgBbJdkSxK8CaTjFsSSXc2S8cjizCQbY"),
    "3294853455": ("MONSTR #1454", "QmT4Aiz2sLXVVry5frRbuMDjd9tjRfFhAKHfsoFiQCuvA1"),
    "3294853670": ("MONSTR #1455", "QmXCYvpV6jxhyWgGxNpGbjCP7pGHzQkQXi2QLK64Uqihp1"),
    "3294853731": ("MONSTR #1456", "QmS18wd4D3WEsvbVopsBifxNpzkav33TDLCW3FgvCu4z7D"),
    "3294853842": ("MONSTR #1457", "QmUUEKEXbQs5rXjtSvufTwjzHtVqWY8C1HbrX5yazHA4Nv"),
    "3294853885": ("MONSTR #1458", "QmXcGqZSdZqWmYgjg67oMp4t7CF4mFBeTuwxPK6SKRo9L8"),
    "3294854067": ("MONSTR #1459", "QmarKUvxdjnQYBR6wf7JkEKKD2xB3PPaiprgCTCWEkWTTm"),
    "3294854407": ("MONSTR #1460", "QmPAk98xwCGeiZL7V5wuiHMSLmy8Gzd1By8hPeNzVjSYVG"),
    "3294854496": ("MONSTR #1461", "QmP6TDqb8jv1ji6puwzLWr1Yf8DC3tVKkDFduH4sfT6bHW"),
    "3294854648": ("MONSTR #1462", "QmR1i8ZmzzCtW7sLP4MULyv868ngGEXohhxKp7QZn1eoPx"),
    "3294854891": ("MONSTR #1463", "QmQVsMQHr94JzDXqfobkuhh2PbqRwB7Cmf3uPtGjNM4Q1A"),
    "3294854934": ("MONSTR #1464", "QmZDu7Cm4BK3i29RszZ7oxdEpgucbfNVn1onMoQ9yhoaCP"),
    "3294855463": ("MONSTR #1467", "QmPXWsfS6Bwp9ts73RFYEwYfBzBmhTVN2KeNvGaS81JWzB"),
    "3294855617": ("MONSTR #1468", "QmNpadTdRS8frfdUQuVe9DYCkxBYi9oZpXrvNDSRs538j6"),
    "3294855747": ("MONSTR #1469", "QmPsnY8qPoJXAHWCqAWaZYkUTW5NEuaR7eQUYFmc4YMBt7"),
    "3294855856": ("MONSTR #1470", "QmdPKa8e4fDFU1zVuhVCrLmg7TjhCtZ7jWXc2cf68h3txo"),
    "3294855950": ("MONSTR #1471", "QmNuhR1NG9oWCu5Q1DxbGYeLEUVH3EPizxSarGRguvxgDh"),
    "3294856067": ("MONSTR #1472", "QmQKbrdqxZUaGW1gC9LJbiBSUtAZQcYskSMqueoN5imzvC"),
    "3294856179": ("MONSTR #1473", "QmW55LVRyWLZKourWZ9jHqT7KoPFX3SRh32LLPneF9U3Tb"),
    "3294856230": ("MONSTR #1474", "QmXynVAmLY6EmFbS4BpRCrPDHZHvxqpSXdCGDFvS71jWv7"),
    "3294856294": ("MONSTR #1475", "QmaNtaV4curUjVovtEQdsEw3hQJHZ9ztYr8q4v6kmF79wk"),
    "3294856338": ("MONSTR #1476", "QmZ31biTM5vN4vxTLW7TJcXmSrqJkkPTBQFm9xvmqmAPex"),
    "3294856588": ("MONSTR #1477", "Qma5JxJXYofPpuKcGqJXdQMdCUnvH8s2sedHH3AaZR5vCq"),
    "3294856713": ("MONSTR #1478", "QmQmFGGQ5D3HSrBh2LY2jfNzYxBWNrFqehaK5EjHUxRvu9"),
    "3294856833": ("MONSTR #1479", "QmaQNZ44TX7rifSVYvitThvo1KLd9aBfcTioQFMw1TvkJn"),
    "3294856967": ("MONSTR #1480", "QmXq52KLVaWPRGPHiNb8QfQPxqQSgdcDmaoj6hNZjugrJR"),
    "3294857036": ("MONSTR #1481", "QmbPKhvePASurLqKJp6MtLPBwsxZPwsDQ9qGUstu2jgYwV"),
    "3294857123": ("MONSTR #1482", "Qme2EixuLHcwfBkZ1sgmRfr6niUWGLPPfj1prAuf8YWhSQ"),
    "3294857239": ("MONSTR #1483", "QmRvekLeHeaYuyME29rX1HWLi8iV5SUTd1RJbinhjbdkzy"),
    "3294857472": ("MONSTR #1484", "QmUCiTim91XdqfqQHK7rVtvvRZSQnH36E9cB9F8DF89aBX"),
    "3294857576": ("MONSTR #1485", "QmdBttJMAezV59YGPhgXiNsWDk3Ka54nosahfnsPWf2jU4"),
    "3294857838": ("MONSTR #1486", "QmTEskojdbmVhc2wufEiLCnqKjHdC72xQQgykvjdFELGgF"),
    "3294858088": ("MONSTR #1487", "QmXBNduXEWTFQ8mMUkbzHs7N6zFh6D7r9iSxthNNyzbiLD"),
    "3294858149": ("MONSTR #1488", "QmYvaLzdWS1idLtwqtnEgg7a7g7t4ARurBEgya98hh2T2T"),
    "3294858296": ("MONSTR #1489", "QmQXr7fj8r75mnbWPfYJs2mn6mJRSitXiNeJoumpqKhsAH"),
    "3294858558": ("MONSTR #1492", "QmZN5noMXV4Ftjw22Y2qhUt2GwzbeDSJNFmTJph7kyJFPf"),
    "3294859097": ("MONSTR #1495", "QmdmLaUNkU5RC9asB3VfTn1kdNcZfvj9dwTtAg7TnayonG"),
    "3294859198": ("MONSTR #1496", "QmcDjGGiEDLHk27X1vCRZmuLCEWsNDCCbf8ws3R5ccHi6f"),
    "3294859334": ("MONSTR #1497", "QmenD6DW462FHczEJtxUbBgXD79G6k8Sho1VwiubfH9anm"),
    "3294859426": ("MONSTR #1498", "QmZBoymZxzt75FcMaVMY7mEYvcbKeW6ASyNNyCv6PJJJw7"),
    "3294859508": ("MONSTR #1499", "QmNeWcQygRJFiGTuVYU697qAonhoWyL527Hdgxk8gR5PeU"),
    "3294859755": ("MONSTR #1500", "QmfTTChx1oN8Cx6Zer3gMdXn9N7hiQaF23jwxuEP6KSbAV"),
    "3294859886": ("MONSTR #1501", "QmYV2BjqAL6bNcYvV1jpSN3h1CFaVRdkxtTmx9e5rY72g9"),
    "3294859940": ("MONSTR #1502", "QmYMt97X1Pv3DXA2EQCWb2AACy1SjisghYBQ8744NHTpcU"),
    "3294859991": ("MONSTR #1503", "QmRGgVUY5KoXkWQpdvUQavumpVnw8EZ3dDmWAfvtrA8U3d"),
    "3294860128": ("MONSTR #1504", "QmSgq5hDiZK7UMtG8PQPQtXxrWGYzkjU7v2C8h4YYQSmFQ"),
    "3294860214": ("MONSTR #1505", "QmQe153TGWfSsbsb9M7V73RrA2oFWaPxw832x8MbP7VPpv"),
    "3294860380": ("MONSTR #1506", "QmSSdnzVLitx5jcp3xUCn9rhoxHEM5ysMMYPdKi5Qmt47d"),
    "3294860480": ("MONSTR #1507", "QmYnxsBtBb8tTupgnwLdRyTvVXgSgWp6ujfgx6Zwucv1w6"),
    "3294860551": ("MONSTR #1508", "QmaUHXx8pZC7h4Uj8MVWYaDAMLZ93woNF6CCVpohSXJtaA"),
    "3294860633": ("MONSTR #1509", "QmP5ppgCw22Km4skKAVvSuWp3LEEsEouz6bMdarJH18Utu"),
    "3294860742": ("MONSTR #1510", "QmRcGXSd6tsqTZR9jyWWr1MEJYNJSMYWmT3CzgGjwzWgGg"),
    "3294860959": ("MONSTR #1511", "QmeUXj2xouAj5zuybZKTfFMXaNMP1eWFg8Y7JWLUA16dy9"),
    "3294861020": ("MONSTR #1512", "QmerRqSVAAZCsPybhX4GbW6NW96Gz3RVTThwJbJgCmWscZ"),
    "3294861192": ("MONSTR #1513", "QmPFJuPJYRiCx55JfncPyn7BS9Bo152hNZ3bsioxHbJFRr"),
    "3294861393": ("MONSTR #1514", "QmfP9pj6QuTYkVLCg5CUk5xYeAsyXQuRKLMaTV1A9X6Dib"),
    "3294861551": ("MONSTR #1515", "QmS6Ptofg3PVRNp4RpFvsiQF4QQeTiPWysFVHNss4Tncdv"),
    "3294861687": ("MONSTR #1516", "QmTnm2iH92JgtKk8oC2nGuc4ak7BEvZqVAkpYMJ4ryKybT"),
    "3294862009": ("MONSTR #1517", "Qmdo3iteCWpcbS25io3LojGQJzqqBm2sLiXePCK16XRZvN"),
    "3294862133": ("MONSTR #1518", "QmVPD3EEPeqGujVgRLMo6taPX9V1T8UFjvsNXjrTmHtk2U"),
    "3294862250": ("MONSTR #1519", "QmSL2DR3gsWLDQrCpJzcT6hpMgbFNMyybe3GRSsqwJgjd3"),
    "3294862546": ("MONSTR #1521", "QmYYjsVFnwrHD8HZeUkuWgyWjF4Spunw1Z41DsRDhQW3M8"),
    "3294862606": ("MONSTR #1522", "QmSW4cFpUysrTEWMh7eShqv1XsSmJekTUyWJxUXaRTm3PV"),
    "3294862685": ("MONSTR #1523", "Qmc8H7YVPDDc8apiLbNGyFJnMhcSrPqpQAsV97TVKei7Ja"),
    "3294862828": ("MONSTR #1524", "QmXMiuAd8xdcGUqseUfzmRydjPCi2tSdNxr85fC5T9EFu2"),
    "3294862923": ("MONSTR #1525", "Qmbnn3okMiPCmRYXWaQZVc4xy748ozNmu1j3PPS2y4ZKSv"),
    "3294862991": ("MONSTR #1526", "QmRz3G3nYAhoPo7Zt2V1Jp1yTHmULaWwqbMjunyTG99h2S"),
    "3294863107": ("MONSTR #1527", "QmZbj9Rnng756eTuV9j5ApPw8ujYN7MhmVJFmWNM8cUnDr"),
    "3294863241": ("MONSTR #1528", "QmZsT4t2v1XuqBAtQQ8bP8Br8YWqc8yanbsnGYFNGe1dSQ"),
    "3294863362": ("MONSTR #1529", "QmQfA3WS5sE2c3zbT8Zr3ZMwbzSYT8TzYMD1mqhRtduK9u"),
    "3294863460": ("MONSTR #1530", "QmWwDsSLi1Xba93QjXvEqGFRXqVuDEe3QSMmsw3KBua6VV"),
    "3294863637": ("MONSTR #1532", "QmXvA4UMhJNYK5ZSkxfFcUN7E1hmdPS8k1wawK1XhXKTYK"),
    "3294863851": ("MONSTR #1533", "QmRvjXeX29FfTrAYL8mg59a8i8McWw3Fx7BoGYEM7ADKko"),
    "3294864044": ("MONSTR #1534", "QmYTJjzbTw1zrd6pS5FzfjGVtxJoQPnRxptkLDTWPkhD3X"),
    "3294864161": ("MONSTR #1535", "QmeA3B2uJtGowvQYkwSap938oXVVa4qSnNYD6fWAHjesKP"),
    "3294864260": ("MONSTR #1536", "Qmc6W8W2PNdSi36pgaTEKY7mNeNUssR2Rhd3A2HCkU5K5d"),
    "3294864413": ("MONSTR #1537", "Qmd7623HLU1Wx3rZaEp44Qe9i5eR2Y4SQmvbcBb4ocEUj2"),
    "3294864594": ("MONSTR #1538", "QmNUuYWjeH9fDAAGUVofjJsKmMnq7gYKHZkfb5busHp7kU"),
    "3294864738": ("MONSTR #1539", "QmXvZFK4y1EBprAyTAk7CPi2DBAkwADmzM3YWtAuRP9DRp"),
    "3294864810": ("MONSTR #1540", "QmbsC3Cr6TsGcxetfDTMmHJD6sEkJeUPvRGDem4p7ohRSw"),
    "3294865017": ("MONSTR #1542", "QmTfvx1ApNcRSMg1eEy17kodJ4TCJEKVttKYVr1fRhXMTE"),
    "3294865284": ("MONSTR #1543", "QmasyDJjDxie2BfdMzE4ZJG5KCCKt31vyWotRJZpKb1X2h"),
    "3294865426": ("MONSTR #1544", "QmdywYBfifa5LMEgCbaSk4CcmmyjT49XVkotCeCuCT196c"),
    "3294865524": ("MONSTR #1545", "QmcHU9woHbh8ih4nGnrza2rt2rfrsRzD7UNZPBk5tPhjhJ"),
    "3294865847": ("MONSTR #1546", "QmcS2nNG65vCGCt5bzwZYou84oE4MexaikTiLju658rfrc"),
    "3294866015": ("MONSTR #1547", "QmTYQVsm92AtV6kAVVzGXe8YE8S4k1izKr5trwHgMJ5pp6"),
    "3294866096": ("MONSTR #1548", "QmVWFZ2j5W2xUBNfE1iEfziRxQtmrbBa6YfbdhCv7MwDGm"),
    "3294866197": ("MONSTR #1549", "QmY4W86waD9gwJ778NqsX5GnF79bx6diHp8kXNNxcjGd9i"),
    "3294866263": ("MONSTR #1550", "QmbBbQ6EFhXxW3HURHM6h5ZSUsdBbo7oBtfadcVyEMPgcn"),
    "3294866393": ("MONSTR #1551", "QmThrvXZkLWyKYVPnvGEUub3F7fcXkvKbo4x88z1fqGZGV"),
    "3294866521": ("MONSTR #1552", "QmPygy8KvYbsz6kTdLrr2xcW1ZkWiadej1VaK5fYQtPigT"),
    "3294866763": ("MONSTR #1553", "QmRD4e3zE3dZPBUYY7khFoUkPCp4nB7uCrXdXr2BpmE4hq"),
    "3294869063": ("MONSTR #1554", "QmcQGfBgSZDwWG234wkp3m1Lbr1uXXYg9J5n2QVBsx1wNs"),
    "3294870553": ("MONSTR #1555", "QmY38nVqEHnGbuAuRi832W3CrznE7EX7X4onzF3bDeCkDG"),
    "3294870787": ("MONSTR #1556", "QmR3CJu43rQuT8MjUFQZzWYEYWiQrBVr3m42paKJzSCeFr"),
    "3294871108": ("MONSTR #1558", "QmaqEx77zu3jHJ4c268MSD65QSPHbcsBS3F4jjbk4wxaAA"),
    "3294871289": ("MONSTR #1559", "QmVnzdxP6262Q23rGhEqa9NNavNAKUs6WZr3hoDZsxVJcJ"),
    "3294871353": ("MONSTR #1560", "Qmaqnt56GanfaZtRG6GHUwm1k3L8BzJtfMMt81eqieyzPd"),
    "3294871483": ("MONSTR #1561", "QmVmNh59GBCi5v4Gbp4awJmd1KyuqtVojm1rgbpLp7coDj"),
    "3294872441": ("MONSTR #1563", "QmVMPLnh3Nrrbukkg6Fd3B4UFqEZPL3Eqk9uKjzTqiTgGQ"),
    "3294872955": ("MONSTR #1566", "QmVs1nAw2LRm1qzGsQPjhjEBdteTjU7mxqExj61fLtaKGH"),
    "3294873056": ("MONSTR #1567", "QmTgGYxiJzQSw4ULqwXU7SMDsJvUF52t616RhP4AY5Ze6r"),
    "3294873153": ("MONSTR #1568", "QmUtW4uWoMBygFq4iVuqqUk31dP757a5DSJcM5D6xjVk1k"),
    "3294873275": ("MONSTR #1569", "QmZZhDfoYEfM5zxVap4GYonwjnaF1JTENB2oQ1SkRg3Y3H"),
    "3294873385": ("MONSTR #1570", "QmZQNRpNUK5NCgssnenEdbcDShRkZhXnYfKTrc3W24p9Sp"),
    "3294873496": ("MONSTR #1571", "QmPct6ip4H7zaePGt13pBdUhFrYyz5oWXqVcJS2yhb6xrh"),
    "3294873674": ("MONSTR #1572", "QmZBXBpUcBUuP3eC6EPXavJj827gQGw998foKPZtePxQtY"),
    "3294873793": ("MONSTR #1573", "QmNzfS8y9tveZvxcMxUMS5rWATJFbwSACaCrxMs8VVnYQK"),
    "3294873887": ("MONSTR #1574", "QmZiNqCwMQt6SYRACVYYRWHfuz1KFu7KfDCAyLLnUecf5p"),
    "3294873970": ("MONSTR #1575", "QmSFkC2wDbXKqQ9ehrB2dgmCJcG3etabm9vSmgqsp2aNHE"),
    "3294874042": ("MONSTR #1576", "QmXuiJ6JbeumoynnfZZZayrYRw8pwKuYmmxEz42XW8CNbF"),
    "3294874135": ("MONSTR #1577", "QmdviosrpBZdiMvVxkWGJeL8D4aYja4jxEy8sgcnZ5jDUT"),
    "3294874310": ("MONSTR #1578", "QmZ5QgEfeiFt2PEmizXpefhNECXHcUUgpAoz83A4xYbi5C"),
    "3294874469": ("MONSTR #1579", "QmUjamWgn2zix8F7T6WXgKXQdRKhaPATqVRjoW8Uht4u1j"),
    "3294874620": ("MONSTR #1580", "QmUA3yrapuyP6y3FdkF7GgYf8FFcmBcDuSGcfhkQo4kH1i"),
    "3294874733": ("MONSTR #1581", "QmRmi37y8kJPBRfj79QNwbsuZya1GY5KhFFAWrPfF1Vpxz"),
    "3294874820": ("MONSTR #1582", "QmPiRhvnMseAx8su5MdXjgSrgU42963b79BC2fo4URLKBg"),
    "3294874963": ("MONSTR #1583", "QmRk6aWoEwCDAjme8QU4NYtL6okJgnSYPFBkvEu3TEQUQQ"),
    "3294875064": ("MONSTR #1584", "QmYruQMdn4expSgNEVkqnabaZzj1Ev4HwXxLY8ADpGCgFu"),
    "3294875181": ("MONSTR #1585", "QmPVqH2QokZFE1Rg6nm8MKxu7YdWVbnx1mvrvjJW3RuWMo"),
    "3294875288": ("MONSTR #1586", "QmPrUdYCiamQjAUojT1K7oTk1Li1EcJaWrbiRSKDEcyXKd"),
    "3294875478": ("MONSTR #1588", "QmPJ2TNHx5DoPiFurWGsLhriaqKEZ9yxQ6Nj1ytqfcbvwV"),
    "3294875565": ("MONSTR #1589", "QmUeQMSTpoMCmnrTnAbQQ7NeTqKhDcANKaeKSWTAdXARpC"),
    "3294875720": ("MONSTR #1590", "QmRQycX4RFo88i5UtndMLmb9qdAb5VzTRB3Qo2qvwHE7mF"),
    "3294876140": ("MONSTR #1592", "Qmdx92skRFenPgiiwRQFJFTaYVUdroJcsQxg1X79hQxooK"),
    "3294876204": ("MONSTR #1593", "QmbirgL5cbgeZqnqpKExdJT9Hn4YbyzKjA5xpqfZyKRKp2"),
    "3294876563": ("MONSTR #1595", "QmSc7PcfUbrD6MFXKfxKcm6Fa5nuSrHtWobbcGme4WvmTJ"),
    "3294876647": ("MONSTR #1596", "QmYwibQJZe9pWwE5SiE3QfMfsm9s23ziBTJoxKvXtC3Arr"),
    "3294876838": ("MONSTR #1597", "Qma9iTg79dmRyjdHwnTSspE74rreyYLNdgMjvxS2hUJgVT"),
    "3294876909": ("MONSTR #1598", "QmTHM5yu9AP7jxKTim5rhJMmVeFh9V6sRWU3NpnMtc6aqT"),
    "3294877095": ("MONSTR #1599", "QmccM2uG74fpjNy5VACBfXXYRHZQ1HLi5LYaMd1osBei6H"),
    "3294877188": ("MONSTR #1600", "QmQ5ayFgNPqvpjATvfAVxuyLEu2bomn2MJfYNyyjzE9UsQ"),
    "3294877604": ("MONSTR #1602", "QmS7kG8MpxezMcxf92xagXM6ejfhCnfDxBtuYRTcwBQfTg"),
    "3294877668": ("MONSTR #1603", "QmPeR2H3grwKzEvj164qeE4vCcNYYiiQxUXjJMHbLYDgss"),
    "3294877769": ("MONSTR #1604", "QmPDs5Uggiz8L5qahCRo9nADgnmCvQFGjif5JPigvXXWLm"),
    "3294877934": ("MONSTR #1605", "Qmf6xrbDk8YwBUxXyFiD4dNhU8q2cW8QX5SpZZr3XfbJCU"),
    "3294878036": ("MONSTR #1606", "QmSJ9wsVcsPXA8Ekn8ARsYtQTSu68r8s76kkeAUHeWYeTx"),
    "3294878174": ("MONSTR #1607", "QmQzGLdNCpApM6uvcHCtPGmCgBKgneZPJgbcaKGCD9uboV"),
    "3294878388": ("MONSTR #1608", "QmY1WiYqGyWcRdBzAMCnyYMbeEeZ3bZWcFqbQ27BRmHkqm"),
    "3294878628": ("MONSTR #1609", "QmdQMt5tYd2pkWLuW3KoixsuRhYRnJr16ofbGkDxgwEHn6"),
    "3294878825": ("MONSTR #1611", "QmSpmQBwdDPZZSwZWVMPJ7nogsUndhE2goGaUhTY42FrAh"),
    "3294879138": ("MONSTR #1612", "QmTga4Y7rwaJKCFFYEYDZPnC4SkNtGNFYTmFgu1Sy4kPkJ"),
    "3294879279": ("MONSTR #1613", "QmR7hS9UMADUPjrVzJDQz4gjarwrUa1LfJzA3LUuthrNRX"),
    "3294879515": ("MONSTR #1614", "QmXBkZeYB8HizQ35XER4Gueybdt89czxpqWhsr6caHv36S"),
    "3294879540": ("MONSTR #1615", "Qmb3FTZPg4WkGehdLyptBr1iPUyjpNAc3Xqbi9Fn3zsCRX"),
    "3294879660": ("MONSTR #1616", "QmPGyNgEbyEn4SFNE7qb1WpjtwN1CfJd3fGZm1x4Ts1J2u"),
    "3294879792": ("MONSTR #1617", "QmdJ1Jb7wbbeG6o88WyeF79VNm4FUrcZFoGzdm3iwT9ttu"),
    "3294879871": ("MONSTR #1618", "QmUxv4BjunWXju8FZJ1XAeiADiVNvJX5whLDxETwFPqRsV"),
    "3294880031": ("MONSTR #1619", "QmZPEiKZYBzPKmBGn5Qh4udZbzAjwLVfDRdYQbsV88H638"),
    "3294880280": ("MONSTR #1620", "QmZNJKgn84GG44zJNqEpaMHpfyAtFAKjsqesVngRWxVd1c"),
    "3294880483": ("MONSTR #1621", "QmVyv4VgK6m3gUL9zogDnGvg6BhG3BtwMwGyHohczr9ALA"),
    "3294880638": ("MONSTR #1622", "Qmbwy5EMnRiCeSbHKA4sAiXzV4kPj6cJLhu3So7tgoKKsP"),
    "3294880805": ("MONSTR #1623", "QmepD19GfEKTgwduowZmScKW2WwV2CWkVxE4kiPsCKS5b3"),
    "3294880922": ("MONSTR #1624", "QmRJkndVoLZbEdJk5kWLQHDCgdQm9HBmrpLdoCMLVhBi4j"),
    "3294881029": ("MONSTR #1625", "QmUxwsmvuRjYEAAR4oBYEY2uUiBmQf9414tx4JQ9zu2BcV"),
    "3294881180": ("MONSTR #1626", "QmYG6ep5H96wg6Ak5M8DnAny1qc5qNbaTbP9tt5rXz4tgM"),
    "3294881285": ("MONSTR #1627", "QmawP41rVSPMcN7WnLXarpeU94CjkBjSmuqQHwP8fzjQ5g"),
    "3294881398": ("MONSTR #1628", "QmdCgYEqR8DG147woyojjtcPc3KmBPGDYr9MfjyXNvt1Ry"),
    "3294881511": ("MONSTR #1629", "QmaDNeaGyXvgNpt7byfHiJAZr1DrPNPgcbjPpbu5xkEffq"),
    "3294881592": ("MONSTR #1630", "QmUmoCoc2HbUnK8Gt1EsvWQf3f4VRUs25E9ZkQimrSpTqm"),
    "3294882110": ("MONSTR #1632", "QmPqTWy4UZwWVt6so4kDZ2ZPUvmJnCnNq5rNkKSWi3XpcX"),
    "3294882575": ("MONSTR #1633", "QmVejqKLbBQL5rcMPT1ptr1XM6seWjMkgQxXK9cR3dRWf2"),
    "3294882745": ("MONSTR #1634", "QmYJoh4yzyEKybUErFV76FZrkZJnh5xpMGM2Kt7HStZazJ"),
    "3294882828": ("MONSTR #1635", "QmSQEDrfYiXQd58FqFaGU1wCZHBFEcFupfZb4MkY1j4HVq"),
    "3294882975": ("MONSTR #1636", "QmYCmqkFYHPNDZnvs1pAj74F1B7QHRMSH9Fh84LnE5AWvp"),
    "3294883093": ("MONSTR #1637", "QmTY9RqokJ2QCJjfypgvKUWie5bL7wwr77hNbQ34DqFE4g"),
    "3294883246": ("MONSTR #1638", "QmfL7wTFDTTDBmFmj3eFnC2yJ74cbn8TY7Ky26sErHW2G9"),
    "3294883362": ("MONSTR #1639", "QmNwJtQEryW7pNHdhSyGbzdTaoM6m8AxNRcszqytc6qsYU"),
    "3294883526": ("MONSTR #1640", "QmPxto3nwTesRqDLA2cmKzMzhMbrCqUY4YTVCjewYPapg3"),
    "3294883855": ("MONSTR #1641", "QmQmdyTQSZ4Kb2ru4JqErPcKGP9rvqKta12xHaJ7PQpsQa"),
    "3294883943": ("MONSTR #1642", "QmPiY7Gds4nHxxXqz1R21FRHpjSR4WQgdUM7db7TBSugDw"),
    "3294884007": ("MONSTR #1643", "QmdnKGX4DSuQbF5wcz2Nq7Y6uPvGZPs8YGdxqAqhiUq5ui"),
    "3294884067": ("MONSTR #1644", "QmV5uQJiLppZNpbQJWuMYFgyfnDwoNmwLWEwthubpf9V5R"),
    "3294884255": ("MONSTR #1646", "QmUPmQyZJhNP3Z7ksvTx1HNCTZLQVED2mkvXxnmp1A72yy"),
    "3294884312": ("MONSTR #1647", "QmWBVb5CdBL7Ekf39UbmjijChMFAxQ9qGhE5utSu5GyLi2"),
    "3294884387": ("MONSTR #1648", "QmbaXBmiwbM3sUuTf667i6LGXMsmJ3vaQLMxWKP6932WSL"),
    "3294884526": ("MONSTR #1649", "QmVRXaZHHvhGkFbwBBnxsvoHV4qpGhcwXgqiSQPH9C79gL"),
    "3294884663": ("MONSTR #1650", "Qmcnuxxpvk3x3LSLDAQEBYEi5yap58QjXVpb3BLvPJKFMq"),
    "3294884892": ("MONSTR #1651", "QmdrAcLe6ZydQEZ2yPSyxo1n7cf7XLQkRvmj2WKYgRGVMk"),
    "3294885050": ("MONSTR #1652", "Qmcjr8zptvyB4rQ6ciZ2c6FkSwFBMjb1kAwALkWyNTdq5c"),
    "3294885477": ("MONSTR #1653", "QmZfQVbiDESkudE3ognCs62iTjmXWtfKWnb9hR8VyKzHc7"),
    "3294885599": ("MONSTR #1654", "Qma5yRizeN2yP1Sm1Px64PphujFhFSefJEMDsem2N7jqhx"),
    "3294885773": ("MONSTR #1655", "QmediHa6nP8tmZDuHF8tm5HQRhPAmm9a61F6xs1zNK7ju2"),
    "3294886321": ("MONSTR #1657", "Qmbinqj8btTb2ZupUh4q68PnehVNnKe32ToCSgRK26vBGZ"),
    "3294886453": ("MONSTR #1658", "QmRGy1uFpffPiCYSnktdvgR91srjJhEDnS9eNsLSVDNv4a"),
    "3294886589": ("MONSTR #1659", "QmcARvCJxDq7zesRgtFDAtYqTEWXYqiR913QNVCni5qdRt"),
    "3294887445": ("MONSTR #1662", "QmfLuaKwvjUoUeLqz7NxFYszhQy5Rzo5piixyxK9i2j5GE"),
    "3294887699": ("MONSTR #1664", "QmeaaU3s7Zo9LryGtW2zKFAove8ZB8mf6mkXpxJdMoHBBR"),
    "3294887835": ("MONSTR #1665", "QmVzhfBXpUtLadzeMrJcZf2eDhBVQCAngFGdx8Ypqx25Wa"),
    "3294887903": ("MONSTR #1666", "QmfY3Pgt9JEZe6WrpDcHv5HcX38NWw4WkAWUyptom4V1rs"),
    "3294888478": ("MONSTR #1668", "QmPqDopDz3rLkyciVNSfFBhw6yPJ9z8mav6GBGQ3EntQU8"),
    "3294888621": ("MONSTR #1669", "QmTfgJRh9pEedN8WiePPYYxiESJzvuUNVoCKvz45Uy15En"),
    "3294888766": ("MONSTR #1670", "QmT43RQig54rfQ3BZAMjsCsVSf2PNhmuMbqrdDw3xVpyvu"),
    "3294889025": ("MONSTR #1672", "Qmbi2bNXL21PSHDrdmHpxF7JiUD1hxqTb7JoKjzfd1WU4m"),
    "3294889469": ("MONSTR #1674", "QmXhtFH1NJnToDwEJFmKhB6Xe9Q6Wi5PUhz2E251Qgjzx1"),
    "3294889760": ("MONSTR #1676", "QmTDPyW6kxdP8FEwnQmLJ67n5iNXji65eHK1DC3f6H7Msy"),
    "3294889885": ("MONSTR #1677", "QmbmTwKuKVbvamDnmshTfsDKwcoftP6K7tkkTQKZeziUX8"),
    "3294890044": ("MONSTR #1678", "QmXKiqaxqq1D2rU4Ugdf3aX4BWrMLNzNr7JesknSZtDRwE"),
    "3294890098": ("MONSTR #1679", "QmWzpXNCS2M1x8kouqZwn4RhYnNjM6yjzPJuderTyB4pQw"),
    "3294890173": ("MONSTR #1680", "Qmf5zc71BSTg78MKT54wMbtCM9UaFb1ir56dDv7bA6CCKi"),
    "3294890347": ("MONSTR #1681", "QmbPe5v16pZQgsRQGt8pJ7j3LjfD7F3bsjRSjEmHJCMPwq"),
    "3294890561": ("MONSTR #1682", "QmXUMKzqDPocFyB2RjMHRkkg3ZcceuSiiksYWwhUCpqTnh"),
    "3294890808": ("MONSTR #1683", "QmSu9w1QLawmnN3cbt8mJAARf4HVNeixKZP5X9TeYUwepB"),
    "3294890967": ("MONSTR #1684", "QmRjpRRDRZi2RnVeJiyoREhJjRj1uurCJRcy71bSDcaiwZ"),
    "3294891060": ("MONSTR #1685", "QmRrT8DeyhLwLL1zJQHgRQekVC3x4QBAdcFUQFeg22pg6Y"),
    "3294891238": ("MONSTR #1686", "QmRchq81T2dERxgQzc5ydXWFGACEQvnwpHAFwDaCDiZfmd"),
    "3294891343": ("MONSTR #1687", "QmcE9ehd4cTEHzY8Js31C5xe5zExK5YEuS5Lx6akWAGA8u"),
    "3294891456": ("MONSTR #1688", "Qmc5xq6hmxsGhL11aPs5W2RUYJoULQg3Cp3KqnT5P9inp7"),
    "3294891554": ("MONSTR #1689", "QmTiHscquv7NFS7Q9mPXm1yXkiifYfPLSHtGodB95qDy5u"),
    "3294891639": ("MONSTR #1690", "QmXsiCjju8XLDA4YynVwB63iWnFYPDomRHyY236rcNmznV"),
    "3294891921": ("MONSTR #1691", "QmYHADoXUVYpyTJxmTCCjJFq61APqEkjAWwsK7ivswxH2n"),
    "3294892335": ("MONSTR #1695", "QmR8jbXuLLdDZgv1bHMmjXmMQmef4CBvvyX4EaDWFi4emt"),
    "3294892059": ("MONSTR #1692", "Qmd1naQe6GTxrezqQCCAEVJgPA7nJSSDJLXCAUWn7VwNLM"),
    "3294892131": ("MONSTR #1693", "QmQ7SH1vvNh3Urx2KLRBhUNQyV86kXo4aLLE4EMx1n1tmg"),
    "3294892235": ("MONSTR #1694", "QmXH4VVF3Rhwt1xjG2gf5LPJnZgNAHDB7pzCy3DGFE6GY1"),
    "3294892615": ("MONSTR #1697", "QmXBcan9vMAnzF2wiA7YEohSSjGjewPdGBQTDvmp1F5Lsa"),
    "3294892458": ("MONSTR #1696", "QmQz5ZapTZSsgckQdKm5WBNft3D6T71Y8bcYErgYoKHtPt"),
    "3294892879": ("MONSTR #1698", "QmfXHe5kDZr8GCowx8Kzfts4Gb6ks9cnCbzonhHaXx12Fs"),
    "3294893038": ("MONSTR #1699", "QmcV9gsQAFGUrwbMztQ88cE9wjGX5gW2pEawrfV4a6PGDx"),
    "3294893203": ("MONSTR #1700", "QmefVPR68F78q6itiqWhEUr6kvj3qyFuDEvks57aB7R6Wr"),
    "3294893360": ("MONSTR #1701", "QmX85S5nTJRa4zDJZrGM4XNUffa9c3SVB7EiGM1cvGmaUb"),
    "3294893556": ("MONSTR #1702", "QmRtgpk54FBz2y2bpHkCMemt8fdf5yCLmfrBkTAYnEDSEh"),
    "3294893765": ("MONSTR #1703", "QmVwLq4jLKBsf1WeKqhG76GbNuefceqEKC125mpzY82GKL"),
    "3294893918": ("MONSTR #1704", "QmUEDBUQEcbha8Kwgb13gEP6KU2S8H37nvFiYsX1eJdTYS"),
    "3294894011": ("MONSTR #1705", "QmfPgb2ft1sRiZLjSs3JCugwdpEKFS3GU72747Dz6Mi8jo"),
    "3294894200": ("MONSTR #1706", "QmTRvFQvc9B7K33zmow3KmUGxo59uajwhSPYgzBV6VzXBf"),
    "3294894281": ("MONSTR #1707", "QmZRWr2Mg8JCuC5JqLY1pt81VayXEb6wc5bPwmPZUYWsKN"),
    "3294894412": ("MONSTR #1708", "QmdasskqHoXVNJLf2hjuJkCASuJWcSvZxdnJj2Kgbyg2xv"),
    "3294894504": ("MONSTR #1709", "QmS9q7UqRL1em1y4N9xPYuYgTkwnaiJdiYg2KpTFmYZxQp"),
    "3294894684": ("MONSTR #1710", "QmTyXipXZPxrwsHMacARRQ47WH2R7Y7vZYgmaTdohNvxLz"),
    "3294894796": ("MONSTR #1711", "Qmewu2nQ3fiFAF5xxgsfkUJgTnCehF7GM1ZH3ntkPtcfWX"),
    "3294895082": ("MONSTR #1713", "QmQ2SPU7o4E1z6Rm3ry8Csm4fmUhDGAVvTEK5xtZEaQD1p"),
    "3294895192": ("MONSTR #1714", "QmXFJrFNysfYDtpXdnZVjPJsJZdXkoJFn5HbWfbEv6fm14"),
    "3294895295": ("MONSTR #1715", "QmeygKjvTGyQ2LhKzEq4dAsYduZyUApRPDeLsWx5toV4M9"),
    "3294895538": ("MONSTR #1716", "Qmbyan8yq5zNepJ6Q1wgaLnRssjtPpe3JovPaKvZQm8i4R"),
    "3294895638": ("MONSTR #1717", "QmeLLt7x2xGfQThXbJ1L2DiUfifZskUMatEAEBDaeR4Nny"),
    "3294895735": ("MONSTR #1718", "QmZrgtupBYbpaHkLczUGW1zJd3wzwJeGdtowYkDxfBTV5e"),
    "3294895902": ("MONSTR #1719", "QmTatbT2DCydYeUv3XD6tUPzzpUAcvKUDBafndEWFv5dDV"),
    "3294896098": ("MONSTR #1720", "QmRzXjj48osY8aAWzvjoJ5nX3HE9iHLRALNBWaqZ8aeADw"),
    "3294896258": ("MONSTR #1722", "QmRGnd2S9q1UB7epTSaJdQhxpEAbhrFiBLgQsemTWLiEd4"),
    "3294896514": ("MONSTR #1724", "QmZskBQto7Gy5ZQaCSPt2rhsFKwJpwUApjuE4AXCmiHFza"),
    "3294896603": ("MONSTR #1725", "QmfAJx41wKZqACYNLNsr5Qpv7WB1MPC3S2wqUBbQAdR55r"),
    "3294896760": ("MONSTR #1726", "QmZxcpN2jhupyT9n7bJQpDhsk5XAcDZ6q57kjxffQSeHuy"),
    "3294896959": ("MONSTR #1727", "QmcyXGhcUF2DqSRwW2pyL5cZjdWXxd75aoCNi989ENoqiC"),
    "3294897095": ("MONSTR #1728", "QmVcWTRp59arRaPTELdm6smH2VUbeMcr7PUFQcEFZtzAUe"),
    "3294897143": ("MONSTR #1729", "QmWz3wPVBiMmRpwc99xFyJHwNJxkuH7zBGhoExegNXKSoB"),
    "3294897235": ("MONSTR #1730", "QmYVfWeYDoe3wy4y8R4uqu3tQ93QWV7tzGi6znL7XjS9eV"),
    "3294897313": ("MONSTR #1731", "QmTLyedzWKvFq8yAEuTwaQhJHdy42JNU9TDiE7bZgCVGa6"),
    "3294897349": ("MONSTR #1732", "QmXwwPS3ptqG8bSnt2NmGLYiaYpnsT67c4rXAfa6zMiCRm"),
    "3294897566": ("MONSTR #1734", "Qmbdw3EG1RfoDwJhE1eR28i5qAj9QFQDyuazjwGmBwVMuc"),
    "3294897629": ("MONSTR #1735", "QmXF8ZrBJqcLfDeET2b7j5qTtWvx5UQYyDGoVz8mUuKjbu"),
    "3294897777": ("MONSTR #1736", "QmeaxJTCuHx1p99cGHMPvv6PCF4pLfxdCx4PhYQRap5YiW"),
    "3294897894": ("MONSTR #1737", "QmVVmCtLDWNYvvBmmEJ9VATsNaHUQcuaetoXgWfBZYpiJT"),
    "3294898126": ("MONSTR #1739", "QmfQk6Z6EGBK6kTdWgYtZ26BnaAGCuCCvf5Pxejgd9cxVk"),
    "3294898249": ("MONSTR #1740", "QmNXGcthgxCGHmjPQRW5WzdCw68Wt5aTifXmezVD1EQMxg"),
    "3294898380": ("MONSTR #1741", "QmQ829XxW9PW7yUZT6aRxHpTikcPqekd8gqpgWYSZpMxVP"),
    "3294898642": ("MONSTR #1743", "QmZSUGavZu8V4j4Gs3XuKkM5yfEugRBwmvRBeS73wZKKuV"),
    "3294898770": ("MONSTR #1744", "QmTBWKpdt9yg6L1yhUAHh6yqNmqURSKvAwCFuT9VD3SVh2"),
    "3294898842": ("MONSTR #1745", "Qmaq2JGQ9Wk5crwJzkYqbcdushQh6XAcZkr84DawJ2BjFU"),
    "3294898927": ("MONSTR #1746", "QmZzL8WSUYqJZKgabvU2sV4qBBhraqpGP8B1oDCXrN6y3j"),
    "3294899012": ("MONSTR #1747", "QmYCxwS2HVRF8rr77sL3tisGs28xrp8dUqw8LVP6j2CW6f"),
    "3294899068": ("MONSTR #1748", "QmQfPmgaziCf4s2UJx1qhWiw1XjdGebYRJdYNgcwowVXW5"),
    "3294899163": ("MONSTR #1749", "QmXwyXQdmGanEbSaimisxt6EBWMLdkbb9zip1FzDGpxZxQ"),
    "3294899336": ("MONSTR #1750", "QmUfTVAbdb2iMXM2h4cd7HYKCHhyJsyiWBiFPvBDpxGU6D"),
    "3294899413": ("MONSTR #1751", "QmQmu3CtHdBVNju6h8LULB2zTcaZeHUuCdD3fajphb4LT2"),
    "3294899476": ("MONSTR #1752", "QmerrQds4ypFnWDg3A2wBhdK7dEa3953r6mGcxDRCn5Qi3"),
    "3294899562": ("MONSTR #1753", "QmZ2WyJt7qu9SLmgqrfGVcXmXuq2XBr5SkQ9kLfwn3iPPN"),
    "3294899669": ("MONSTR #1754", "QmRrafa16kAXGXZpTZP6a3RTRnEhLCCW9CDQQabecKDmHU"),
    "3294899920": ("MONSTR #1755", "QmPixNvQicSWw4LKZ1HjvyEvJbKV6Deah2EdGxaa85w8Gb"),
    "3294899979": ("MONSTR #1756", "QmeQgBWNp6mPF6F3YwjBF6CGkf19k18HT7mobSjhPvUchE"),
    "3294900079": ("MONSTR #1757", "QmQpySzdZsrAXaL41BLPSVQPes9rqtCN7jSSv4RJbNSgsH"),
    "3294900142": ("MONSTR #1758", "QmTVwgQPWfypvjsen6c9jxqZoa5SMYuFuxdirwrd9R1y2c"),
    "3294900252": ("MONSTR #1759", "QmNdrhZsPsri5JdNmuaHVbYrWjxcE8cErKuthP6wFY94zS"),
    "3294900353": ("MONSTR #1760", "QmabrP5a1wapMLyR5EmUN82xEtgqhNL5DDYq31yT6Q2Jbb"),
    "3294900503": ("MONSTR #1761", "QmU8qzkNheTqxCY3hkzNeUq1sc3gLZ29LJj3fSuPj7HXsJ"),
    "3294900819": ("MONSTR #1763", "QmcYcXLp1yAUKCezCYF1s3Cgiru6bHqhEgYwUzkyhkw43q"),
    "3294900990": ("MONSTR #1764", "QmVCmrNxXbETmNXvwidVazQKXE5RkgT274pEm17rRxabhu"),
    "3294901126": ("MONSTR #1765", "QmRfrdpMr9EELT4Zf4BAfYuvM5c6vEgUvLH1a3BPUqsrEe"),
    "3294901181": ("MONSTR #1766", "QmU9BZMS2R1PX2ETTsbGyxFN9PckoBtFtGUs9AYwtvjR2F"),
    "3294901260": ("MONSTR #1767", "QmbmQYqwweDmAzrnwi1tD123DbqQgky5HjZEe83V4pqdg3"),
    "3294901326": ("MONSTR #1768", "QmYGgfP283JH1HH8kkVCQAzh1i2k6ZpNV4av3o6FWxAaWx"),
    "3294901540": ("MONSTR #1770", "Qmby8fmoVSZkGW8Mq1ssY4okdtNgrrWUfHALTpSXhB3Jtt"),
    "3294901418": ("MONSTR #1769", "QmUGzn2bZAneifxQDdKBeMnuh1D4DxBHmjAvQuWm19ziPw"),
    "3294901636": ("MONSTR #1771", "QmQoqfkWMWkfDpVaXnpSvnkDxSMULWhg8uhb61txVPJ7d2"),
    "3294901859": ("MONSTR #1772", "QmY521YW6wYHeVQFVNm7mX24Y9FAzLWB45JihjUq2V5vf7"),
    "3294901973": ("MONSTR #1773", "QmWgTWcgFxAN2SYrA3AapTYpmX9SMWSkJb1zimzk86ZsLi"),
    "3294903990": ("MONSTR #1774", "QmRuEPKHd4H5UiMLG98dypmQP9jyzo6iqrRfbTP69nA9bV"),
    "3294904423": ("MONSTR #1775", "QmeKGWAuhdGjiWXLzvYLmQWtF8mULTGRbq2jPoggynd2rM"),
    "3294904905": ("MONSTR #1776", "QmeunBKoD2QKpjxPMv3jc1dpWXPYzVkSLwULumcznU9ky1"),
    "3294905041": ("MONSTR #1777", "QmPdvNEoK5H1WiaMyAQvvRvyqWMMZYWtEbbwR1bANG435o"),
    "3294905166": ("MONSTR #1778", "QmPgWrF2sjM83F3jeXe4oV1vaBQdWbzztA73Riqzh24gCs"),
    "3294905246": ("MONSTR #1779", "QmRbHnvVTqZVkUubSUV9NEipKo59WNTnZ6FfLyMRtrbKKD"),
    "3294905341": ("MONSTR #1780", "QmT487b2LJG5A3Yk6kg64z2oUUmPzQzS1cexDTG6RRsyWc"),
    "3294905472": ("MONSTR #1781", "QmZCyKv78sKMFdirWNitGthtZgEegx7BmQUEfZU64TL2wz"),
    "3294905562": ("MONSTR #1782", "QmWv67H346EFn8BSPQW6st7SEhhsMwnHuFApwNZrntd7Jh"),
    "3294905683": ("MONSTR #1783", "QmWU3TF6xTZUj6VPF3TiVux1oToHtVAQDDBoXS5mBsWKLB"),
    "3294905832": ("MONSTR #1784", "Qmd83jJ6tUYpyVkaMudGy9jd8a66HFAgSeaBJQFd6ygwtd"),
    "3294905942": ("MONSTR #1785", "QmRq4ZNxZLhfqGFzEZ72KFHhizijNmXZSDvUun5njzkzN6"),
    "3294906007": ("MONSTR #1786", "QmQtubUCSe6P3M2qfX3qCGiZteHFzbsw7j98Hg9qtKLoZx"),
    "3294906163": ("MONSTR #1787", "QmTd5q3en3VXwtJDW2k9caV5sj7hM1CbW5W1bMyDiNg3LW"),
    "3294906606": ("MONSTR #1790", "QmfWADcDhA5LHFzAQMwMRBqFEq3BnRFtYFtpjqWRQMwSth"),
    "3294906934": ("MONSTR #1792", "QmPkouuhxXFT7TEKECShaEq3cgCZcUmVnrC2LCDi2da6fd"),
    "3294907006": ("MONSTR #1793", "QmXVia3vSECtvgyzj2UwAXuDWxhUmaC5vefyorFq34jwXW"),
    "3294907113": ("MONSTR #1794", "QmYmS8QD6yfafqHj8HkDzGGXGx13kLJAX5cP43Kz4oU3Ek"),
    "3294907177": ("MONSTR #1795", "QmTtkpW6z9Eoafj1nEF26asd31mcfXxJpF2nD79eRT5o3g"),
    "3294907319": ("MONSTR #1796", "Qmbi36dFo61pMUQB35nj85H6wV7wZDE524SiMKzZWvSHSu"),
    "3294907474": ("MONSTR #1797", "QmSagKgFibDf4k1JwzJVpeDiyP2TkVrsp6yUQ7QKmBLhmz"),
    "3294907661": ("MONSTR #1798", "QmcgxBSsW9fE4Hp6D4AfoysvgsQhrJ1YRHVgFNdwTcjXUz"),
    "3294907817": ("MONSTR #1799", "QmYNzerabfPMvskKpvHDeLzxUgHiX9b6BN6aXxZHLXNPyQ"),
    "3294907961": ("MONSTR #1800", "Qmaf9KZ8kfUvasQCzFbTkU78rbaxTLPDHeJGwyacV7RJYn"),
    "3294908083": ("MONSTR #1801", "QmVrjdaLSvrFZCJMXzu2hieTNMNrkBWXbe9BcKGLcghBTz"),
    "3294908205": ("MONSTR #1802", "Qmcw8Dj9y1S9wfUCwUUz4ZEsuxngEnGbGSFsfmvxP3hk62"),
    "3294908326": ("MONSTR #1803", "QmQZrsDwchiUxTcoFmVgNcYgAiJ4UZY9ZiB4oieZ5HKR8c"),
    "3294908475": ("MONSTR #1804", "QmPRjqUSJZEN1A3QriDe9w4jCJSv1TXctr72JcXtWWRzW9"),
    "3294908694": ("MONSTR #1805", "QmVtosu7WchfNabqBaFykpEvPiDpSzLXsgZJ92AJ2TDHL3"),
    "3294908874": ("MONSTR #1806", "Qmcys621VEBixgSUeoeLMTMK8JdjSafRoLRzREYcGZvG7v"),
    "3294908962": ("MONSTR #1807", "Qmf4RSmfrWVGTnuGA4ggTueDy43ocRUtJ5EjbehkuNSxSR"),
    "3294909201": ("MONSTR #1808", "QmeeDDisKJ6WG9ySeWAJLEAcSGYBrFDYcJBZ4hfFj88mpP"),
    "3294909384": ("MONSTR #1809", "QmZLkmS7FAAxbyNLogWronxuQMt4N6PJjhAHUjD29JrR1M"),
    "3294909481": ("MONSTR #1810", "QmZ5xsLg6qf9t7iYq8stbq3DCtmAGNTXi7yBa1Svn3R133"),
    "3294909588": ("MONSTR #1811", "QmPYH8zjoxZSREhYNXQt84akhDca28ABiC27gZpKqVQFM2"),
    "3294914800": ("MONSTR #1814", "QmQfvSGD6sYoGe7Pauc8KQfLvgxW9JoEznsK8ehCt5VR6s"),
    "3294915221": ("MONSTR #1815", "QmXKLmULX2YHNMGeHvjkoDJj63Qfq28gVvcaVFEfVL2KG3"),
    "3294915358": ("MONSTR #1816", "QmdQGv1UHRQkn912Rnvv9X1unw1nveXdBzgaP7vYotzhLz"),
    "3294915477": ("MONSTR #1817", "Qma7Md2AJcoikbyNg7NYsffrB9F1mmnK52nunwDX5smGdo"),
    "3294915773": ("MONSTR #1818", "QmXXsG1JDRWjNoez8ZL19DhSzE94PJB1eaWNxokBsnEzEV"),
    "3294915852": ("MONSTR #1819", "QmWaMxs8QPgCXBU4okuZZCbSa2Kif4ZSVGt4AVizVfeCU3"),
    "3294915895": ("MONSTR #1820", "QmcgBCifYs78HPsbY5XixX4JC57Wfm8sb9yyLt1N8iUiPJ"),
    "3294916018": ("MONSTR #1821", "QmQMQuEqPXamoQi1rxH47ZB2QhaKs2bVGxQuSeJ4RKCwUy"),
    "3294916232": ("MONSTR #1822", "QmcGBmdR7N41BG2dtig1imJ8kz61CdVa74G3y5wyJUzjQF"),
    "3294916306": ("MONSTR #1823", "QmSLab9SpsUCLnKWejzsC5sgdzr6pfX9JeLLJCZf1cVwwC"),
    "3294916391": ("MONSTR #1824", "QmXK8ntAqvNgtMaMam9ViQyktnDDtEoci4KuABRLsbTehC"),
    "3294916557": ("MONSTR #1825", "QmTf5o818sG9QM4xiAjt3AZ8iXn9PoXUQTU5nq1TvKZhzp"),
    "3294916703": ("MONSTR #1826", "QmSW7ibWiXTjyGvDgDjtSiyqyDWh9i91eXEcLcqUq9AFDa"),
    "3294916892": ("MONSTR #1827", "QmfT8DrXczvY5HAHnDgP7U8i7AsHbvZvrndvqfzT2k2BUz"),
    "3294917198": ("MONSTR #1828", "QmRp42C5kcUPokTNGgfxUTXzEAZ5tcftpHhUiEA1NY1Gwx"),
    "3294917371": ("MONSTR #1829", "QmaFRfpyKSjaemRMpBKg9XapGz4Tq68FBR2yY8E1vgQM9U"),
    "3294917533": ("MONSTR #1830", "QmaM6BpERXoQvc6rd4zGcgg21ute1bt5ou3BgQi3VmjBRP"),
    "3294917614": ("MONSTR #1831", "QmU6f5MtnTsRQiHPSyWZuhwUJ6ECrCXzx7KaeQWyToHxxw"),
    "3294917773": ("MONSTR #1832", "QmX55n1g1CDMv2Po3zzctY7hdDYTvZkAEfE9eoDS6ZCj9h"),
    "3294918244": ("MONSTR #1835", "QmNW76chrEU9zt5Tsd7iQRcYRLvpAKkcQq8wb86aUFDbo8"),
    "3294918320": ("MONSTR #1836", "QmXEnC3ySvJaPbgFFiAbHyEjSn1h8Aud4MWRHz4i2pg2jn"),
    "3294918856": ("MONSTR #1839", "QmaDVmwsB3dBsjXxSkCFTbdjV2TTwhjh26mzZHtEfVhgw4"),
    "3294918924": ("MONSTR #1840", "QmR6zw8CbvSuVZB8P9thiVQhntjEnH1c1ktjRe4ZQqnNeM"),
    "3294919217": ("MONSTR #1841", "QmZYvLQM6KLoooAKFo5rVeHuVVs6wviGUA5JRvwRDumhf5"),
    "3294919288": ("MONSTR #1842", "QmdBf8Qp8ioW35ZH8qGMjLQrzzHC13u6y9LWR7qSPwZ5RF"),
    "3294919353": ("MONSTR #1843", "QmZWxWUNKR6Paf4T6VxukcRC3uF1HYkkX72LZvaTwSrSrX"),
    "3294919426": ("MONSTR #1844", "QmTMrZ3payvGS1zBxqEjMt7SuE3Ue39xkhUXn7q5tCbw9b"),
    "3294919577": ("MONSTR #1845", "QmRW64F4FPrfU9agwrqVmwrpy2FREbZazf5ampE6LirnJo"),
    "3294919731": ("MONSTR #1846", "QmSyRYuyZzk2HGyYgZARp8daPoHMLDVKj4UL4QF67Sc5Tf"),
    "3294919863": ("MONSTR #1847", "QmSxW8tXaj9SxV5Q2RsdEtmXcwq4URZmz4S1LbG7WNFhdh"),
    "3294919949": ("MONSTR #1848", "QmUfbiaPtUcjqHuXEjiGeevCpixyPLnq7qeMydHoGiLwep"),
    "3294920055": ("MONSTR #1849", "QmQcVBpT4jFCAcv5JqZv2QyAzSqpgfB9wSv6scggmcwp27"),
    "3294920111": ("MONSTR #1850", "QmPHHFnpsvwWvnLL7jeGGE3v8i6wvK39SCFEEwr6bz1HTX"),
    "3294920421": ("MONSTR #1851", "QmNMXfug9gioj7H1HkvxpbUaUMPRrDq49K9o4vqNpgyrGt"),
    "3294920602": ("MONSTR #1852", "QmcmVU31YitHvoi4AWm97rzbpJ5UQwrMvKA9XzCiCLjC6C"),
    "3294920645": ("MONSTR #1853", "QmfD4AznxJLcKQ9WRM8u8jfKT26SvzVcPvzGeSUt4STQJ2"),
    "3294920763": ("MONSTR #1854", "QmTTJ4kV4hY9k9s4fHQjZvGQfcU2Bd85UGsrWU4YJEJCpJ"),
    "3294920872": ("MONSTR #1855", "QmXQeHq5ZfrYVVXPhM3qrx6U3U58ffcg8mHmhjqsw1ZQJW"),
    "3294920980": ("MONSTR #1856", "QmVCydvB25pkRydeu1rxSidCkN5F2ez4ien1m4dVT8W4g7"),
    "3294921031": ("MONSTR #1857", "QmYBGQ44tySbKJ1AVTcmYbuiY8xhZCxaKRLHsBr9FTpB6g"),
    "3294921222": ("MONSTR #1858", "QmNVtwnXSYonxrw9tjsdBYK5uAhRPL7RWgsZY7FKJixdCa"),
    "3294921426": ("MONSTR #1859", "QmfMd1RYxBkg43PZakT9p8Hm3HEEb1M1LNw2CX5E2nUHVJ"),
    "3294921659": ("MONSTR #1861", "QmaapA6xeQeZsSBSh8JzLcoWBnq3Y6YFcpJ1EV31WZjSB2"),
    "3294921844": ("MONSTR #1864", "Qmf31oj8nHjbXKPnoCEB2N4T3MjaDuLAXktscpuEeuKdzg"),
    "3294921961": ("MONSTR #1865", "QmQFVpy2EnnwrdcBzz2J8nSKcHZHCsdEdqzhjP82xmuC3v"),
    "3294922078": ("MONSTR #1866", "QmeFQFiqZ8UNinuNnVggZdqYSPugjCH34WU1BPunPjC4LT"),
    "3294922312": ("MONSTR #1867", "QmX1EZDqT9HnqRmxLtDcY9Rm6SVvoBFX1AmTVgQcS11irT"),
    "3294922372": ("MONSTR #1868", "QmUjujA5avcYGg1YxMzky4dDFBcHvHwJv7SC9wCxVF5nm5"),
    "3294922481": ("MONSTR #1869", "QmRnoLAoYZjxVkirCSAvGyEi4ZDGEzppYWUMKtqj4VxuKS"),
    "3294922693": ("MONSTR #1870", "QmbwwHLqdFjWTpvbjE9mHoLaH46YWQWdjVbzjMTNLKZGKU"),
    "3294922830": ("MONSTR #1871", "QmVtggxRCvLecRBvHC5MrC9V2b7JcMnNRK1Cvtp3rvPdWr"),
    "3294922948": ("MONSTR #1872", "QmPtia36jhNhMGV6r1Mu3ErmkoW4MSRq9kHs7xo5ukKoBh"),
    "3294922972": ("MONSTR #1873", "QmdxJvBDVEK6xoXvd8iPdnuSyVvn91aSGV8eHkn6Rvw3qp"),
    "3294923102": ("MONSTR #1874", "QmUmxr89xdqfuUjPVYcd1seGWva1WaUHriZjBMXyQSg1v5"),
    "3294923305": ("MONSTR #1875", "QmXtmbcbP48TFHrwZgsJPYZtKh2uMycTtdkCk87q9P2BAV"),
    "3294923352": ("MONSTR #1876", "QmQ2mXWwrj2Wvn97Csbeokh6qyyHRcJevsBEB6VofRDzrC"),
    "3294923433": ("MONSTR #1877", "QmahL6FfPFZttZ9yypG9Y3N5ZxYHf3JYGTwVQPo9Xp1bQa"),
    "3294923533": ("MONSTR #1878", "QmQk8g3G7bMMFg813owMW7p3V3Vzts72caoWSWTFCiHJoh"),
    "3294923829": ("MONSTR #1880", "QmP9rm23ognuServ2i4A7AJcc59Sf8mbKziC8i2GNAHQUR"),
    "3294923942": ("MONSTR #1881", "QmcKNy2zjM38TCXNwE56ApX1fGVfWLkpcoq7MkXm32KWte"),
    "3294924064": ("MONSTR #1882", "QmRBf4qdiAH9oWTQuJS7o2RaARgkQBpPePHJk9hMyr15o8"),
    "3294924152": ("MONSTR #1883", "Qmb3G1tYFJ1x9qpF33BnFwBPi3rG8nZWujaLEyEHnV3RBv"),
    "3294924300": ("MONSTR #1884", "Qmf8p4x31eSbLXDijoEKHNpg2UBNGt8DzRLV73SgnoySgd"),
    "3294924527": ("MONSTR #1885", "QmbjjeX3WaMLAzXwutjEyJUP7DNJ8QweDDEEfATpzoNtYa"),
    "3294924620": ("MONSTR #1886", "QmYeyTMSgJ7whB5GzBfHsXgp6PdFe2M1bvogxVSvfbuAU2"),
    "3294925030": ("MONSTR #1888", "QmfA4J1QVXKaA5nzYsbC9aKoavqeXcTvtiASL849qzo1Nu"),
    "3294925318": ("MONSTR #1889", "QmUZTvMQMqKeGdEtkDSZNHLD4m4YZXFZ4tjQ4woDr6VZ9c"),
    "3294925404": ("MONSTR #1890", "Qme7K8NnZUkJiNzqmmWQ845nf5efY9LeWQpAAtwAXvjiD4"),
    "3294925457": ("MONSTR #1891", "Qmcw4HBLtbZuA3KSn9S7ksJ3Sc7SRinF3u9xMCsdTaqjsP"),
    "3294925549": ("MONSTR #1892", "QmWVFnWUwASoU1DwRmdT9uph23wZP33rnPptvjVSZ2o9Qx"),
    "3294925616": ("MONSTR #1893", "QmaJdqm9VuUe87xJC3CND9jTEWDNACEYh278Te5Qtco9xn"),
    "3294925816": ("MONSTR #1894", "QmeYFdYSvezrKa4KKqHayg3PszwP6FLbDuDhkaTrNS3iAM"),
    "3294925910": ("MONSTR #1895", "QmSuDw8ZMWSQg13CdqRHyHoiUxNxtkKE8BA4PjY7QXgcs9"),
    "3294926224": ("MONSTR #1898", "QmVsymdYTVhUAoVAn4HrNmYinHUy8nrLDNAPjGYRsRiWZs"),
    "3294926416": ("MONSTR #1900", "QmV59v7TzTEA9tAYWZ4z9Gas6SqpBKXB3dwbcM8qHZuLCz"),
    "3294926588": ("MONSTR #1901", "QmTNxEbxH9SaX9gaCARECv9w5aZcdxqGLxE64zRAZfADjU"),
    "3294926833": ("MONSTR #1903", "QmcTkYtZAL9scwJvynozPoAVLC1YNSaQSA48YVgZ5VAnRr"),
    "3294927140": ("MONSTR #1905", "QmVYZ9Y2f7Ls39FfGdVrsQSBRah4igUV97sZTiy3zRZnHo"),
    "3294927302": ("MONSTR #1906", "QmY1cVuTB3hBuGTRempZyFrVszThTV9JkThWk4xAWKJCQ3"),
    "3294927530": ("MONSTR #1907", "QmNqCLpkmTrxZuKcEo7bNvy8oWmmo9FQSRBkVm7XRLFuE5"),
    "3294927714": ("MONSTR #1909", "QmaKLvfTKJfugkFTnC3PehqHadJbCBDu6dHCkXHMi3P5UX"),
    "3294927804": ("MONSTR #1910", "QmP7xrdzRbD35r1Fczk2gzFGRo6fmy7jFpKZmADdUJbApY"),
    "3294927907": ("MONSTR #1911", "QmXqzTQUaT3PZiimkJFU2VNxfqSpMyLAiAinysKPUHzPhU"),
    "3294927954": ("MONSTR #1912", "QmaTLbC6JZfyUzzRTJY5tEEt9kMDDS3HML8Pn7iMnxohC6"),
    "3294928941": ("MONSTR #1913", "QmTnr7EtFTvt2AgBBG8MDfeyKH5MsDHDWaUUdpo5Q5Hpih"),
    "3294929042": ("MONSTR #1914", "QmUH2s3XLemYiSsWtvtU9Nz6mRU8duxm2qN6UEiTYXX5un"),
    "3294929197": ("MONSTR #1915", "QmSHmmkzSBB2hYpzkGafVAFVZN3Z3AFLPPWZmSShDb2iE5"),
    "3294929328": ("MONSTR #1916", "Qmb6yzoJqM5Dfd2b2x6YGANWsHanScu1siX7Fyxaa7cWxP"),
    "3294929475": ("MONSTR #1917", "QmfBCc872wEp9NsNZuNydy6nacWV676YHbsKnYsn7qMGnq"),
    "3294929565": ("MONSTR #1918", "QmY5yLCbyfGKGgwpZGTwMvr8ug3whhSGVYNqUDFAE5PRr7"),
    "3294929710": ("MONSTR #1919", "QmUD3JvXsPK3trqaFv6FLicExy93wfx9KSKc9WzcXi5TnM"),
    "3294929778": ("MONSTR #1920", "QmWqDcTQpr3JQLmEApUfQXhz52ht4QZzNvvbwPdR1qDkRJ"),
    "3294929894": ("MONSTR #1921", "QmbShajGAEP2mphnWpFxTXqMcDZ6RBkr4uhjUGoh5eCMUB"),
    "3294930026": ("MONSTR #1922", "QmZrXaHRZ3SkZYcmDmeeHjXyggWbVQeKro3EDUT2cpWDbH"),
    "3294930115": ("MONSTR #1923", "QmacjNY4wSBwd4a9Czqe9CqsgFiPYbEujEfCFhjfX7q35f"),
    "3294930302": ("MONSTR #1924", "QmRSA73u349MGLPpTYZSYPt9GJXJdztU3GcJDh789bzywZ"),
    "3294930394": ("MONSTR #1925", "QmQyxSNSmu9mt36WxmyKmnCEX57Dd8HqSE4x1ZmqVy4QW1"),
    "3294930489": ("MONSTR #1926", "QmUznhNrp47AZCWX7DBbdQfrLK97dwQvBXqrZHnw8wcvB7"),
    "3294930625": ("MONSTR #1927", "QmQTyp4RzCxLzb6TYUp1tusNGoqfMzCnTQz3trPqjR84dQ"),
    "3294930755": ("MONSTR #1928", "QmPygVWjyqc6NpVNtYW3PEdf5mgzEKVTjoaVJcTcj4DdmF"),
    "3294930840": ("MONSTR #1929", "QmTpWnV4PyoBxa9SmqjvCjTqw3pfWbJtHy2BRA3gL6XJUj"),
    "3294931606": ("MONSTR #1935", "QmU2uJoYPEassXxsj4HUhpKcXEgd1i1zM12JW5DFQDZwTa"),
    "3294930961": ("MONSTR #1930", "Qme9cMaM8eiteeEAVyyF2CXfE4TEHw8j1PcFYCQqsAZFhW"),
    "3294931038": ("MONSTR #1931", "QmSS1qoNxpRE7vZshHQZB5QkGmTfWT6XH3cuGQWi5xtxAX"),
    "3294931340": ("MONSTR #1932", "QmcsM1QH3tAJDUWkQTYbTFLxaPxJUaiVcmJemu2k7QXqMB"),
    "3294931402": ("MONSTR #1933", "QmbFtAtpqTVqWTVzh7Ub5BFkNh3VZvn9xx8fEqVnHKqR1X"),
    "3294931533": ("MONSTR #1934", "QmXicPhVoHhtiwmaTHPAZW2WZHq5j3SopqkvXVdbo1VSj7"),
    "3294931927": ("MONSTR #1938", "QmfZtNWGZpkiQN7PmvY4SJ6Y2X4XCy3CTHq2UYB1UDxLFi"),
    "3294932034": ("MONSTR #1939", "QmaAv74VCKiHuPqdR9Q2i9aWf4GzF8k6uBs7CPTQbTQCWU"),
    "3294932233": ("MONSTR #1940", "QmRWGTK2uNY9h63BducgdRB8JgqHKqFRmGeqnmopZ8L68D"),
    "3294932350": ("MONSTR #1941", "QmZZZ6bXT58w39sfeaybgjfEriKUDL7r7ZspmHdUrSJdkG"),
    "3294932395": ("MONSTR #1942", "Qmbn5yM7zU2yDQzUgCPhPVdqVf8bPSqQZFjQukLgKGxuxr"),
    "3294932485": ("MONSTR #1943", "QmS34i7VjnftvbKKnu9xAVcFWLokYKqxsWhRoSpHiU8E8s"),
    "3294932516": ("MONSTR #1944", "QmdgepETJ94SRDzmMgWG3GxEJy49rdQP7AKzZSj1SeJZCE"),
    "3294932661": ("MONSTR #1945", "QmSsq1tyAWxahaKySD8w6T63GsJMAXgPdP7TZZb8an19Dt"),
    "3294932778": ("MONSTR #1946", "QmaDKgFhrUSTSSaspaFK2ZCwB2veLKocxjRkGaCJpRFmAw"),
    "3294932875": ("MONSTR #1947", "QmVZxdeSWPMqRQQzaQR5tC7iZVM1D33FL7tjY3DiBRy6XM"),
    "3294932995": ("MONSTR #1948", "QmT6jmxAaL1eyqTZhvnwmZ2ZeKrbNg9NxnvxmgAbhoBiXi"),
    "3294933571": ("MONSTR #1951", "Qmd4KPK9FuihH1inadeTgfkGAWom6eJPqxMuTowGUBXoQ5"),
    "3294933050": ("MONSTR #1949", "QmYUeitp7QqMQKuTkac4RDATL7FQieCPzUPJy18v4yxvEc"),
    "3294933426": ("MONSTR #1950", "Qmd5iL3R6yAkXivsEZZAWfnLokL9QFvbZGRnxisCvgNBBP"),
    "3294933683": ("MONSTR #1952", "QmRvc8STmJDv7CQFnp2RhfNExixYEZRjgnvxM8JWH6xNHe"),
    "3294933767": ("MONSTR #1953", "QmYKs17DAQ7yV4ZQfRi54u4dUWgPSTrpa6FtQ27zcWCnW6"),
    "3294933858": ("MONSTR #1954", "QmeLXRLF572DGyuDFBYPnnRE9aiFkbvMJgHVLba6cst4XD"),
    "3294934079": ("MONSTR #1955", "QmYPyRK53cEXGXKPRwPtzLkS7jBnFjTAZcRoBieiXV38of"),
    "3294934163": ("MONSTR #1956", "Qmdc7xxrbJkzrx7ndTDaf9FbXvvbzWxzzEsaeytS5sxbPg"),
    "3294934253": ("MONSTR #1957", "QmZint4wCFX5RC5hfFFd7sn1PosSuf2N7CDNEec2HhN8TQ"),
    "3294934414": ("MONSTR #1958", "QmZWbaEbvk6mHoRAcpUHGCFze854Dey6EDhP18eN85JHzn"),
    "3294934596": ("MONSTR #1959", "QmZdGb6XxT9g1CDjrLUaqxQrKDGtkFMsXWcvckdfBvMYL3"),
    "3294934868": ("MONSTR #1961", "QmRseStMVAe5N9DqVUnMUbmUpaApE2U8cedUQo4MPvchTy"),
    "3294934920": ("MONSTR #1962", "QmYpF6xAUrR1xh2hF6WL67x5mwtwcivYgqy1Mm1miQYYns"),
    "3294935222": ("MONSTR #1963", "QmP9zQJH2KpvoH5CPPhgtqz7f6J6MiRANaWrVyURvnHvud"),
    "3294935350": ("MONSTR #1964", "Qmd1FgZWQeR5QkDd8ZZbvEQr7fd2KnJnTWQXDuzc3zPGud"),
    "3294935787": ("MONSTR #1966", "QmYG53mk9UNPsrbgPbsKzXgLZoJ5pUPjKtSSfjuSwzuNHi"),
    "3294935945": ("MONSTR #1967", "QmRLwCjkFWKRCqWDCY6PzMgCGpigQA7AwVnf5uuWCseJNE"),
    "3294936089": ("MONSTR #1968", "QmVFx1bS43S1AFoASSv5NE8pEWMTu4XNPDjSEEsS5iej5A"),
    "3294936143": ("MONSTR #1969", "Qmbi6GQbWTLmFvE4j1B29WMwdbw1hTwZWPs97GEWXyXyzj"),
    "3294936660": ("MONSTR #1971", "QmV9vvJCsBWUCguyvBiyJkC2bMtf8QrooEu6eL8oHfU1X3"),
    "3294936824": ("MONSTR #1972", "QmXFSqicEFXLAuip31dYto2Q86AiGWjrgXxaGzx19yAuuG"),
    "3294936915": ("MONSTR #1973", "QmQU7QA3kUaPqoFSBrWwv9tvr3qULFCCPHvPL1jYRZiMuE"),
    "3294937243": ("MONSTR #1975", "QmPwbAdU74NxwPqBGMAsVWfDjbWDRHwi3xfRmiaXfN7jxE"),
    "3294937508": ("MONSTR #1976", "QmeH7dPvpFs3rji4nXUoywB7qND6AFX9utoxJXiSytMRZR"),
    "3294937628": ("MONSTR #1977", "QmdkVPtYege3dSaoWvtmSsnCyoi1CvhWnFfV9SXZjvMLhV"),
    "3294937676": ("MONSTR #1978", "QmbxpBiNq2YLrSTMr7R9F59xh7p2XXouoS9dKBpCctxLaU"),
    "3294937912": ("MONSTR #1979", "Qmaayv3mX1sBupzkyYTsu8ciMrbCfqyxAsshUU9Rhvi8QE"),
    "3294937978": ("MONSTR #1980", "QmbXkPf6mtZfP72rNAFDFiZ8JZFHhATvPmapYK7qUDWvdH"),
    "3294938047": ("MONSTR #1981", "QmVv9ii7s8bGnDyJwSysmGp8yJdfgz5bY5j8RU6bTFjhsE"),
    "3294938086": ("MONSTR #1982", "QmRxjMn5Uog4QDyXEdi2G3fQPhRiSVnvQijTAdoGHpNVtd"),
    "3294938215": ("MONSTR #1983", "QmdBfZAKrX6QJjyFGCwSmbpnFAn2wR6kXqY2oST5bpsi6E"),
    "3294938404": ("MONSTR #1985", "QmSZR4bo6oLo3f4cP38ZQpJMLQBXQYn6QtqcEBy5oLnmwh"),
    "3294938462": ("MONSTR #1986", "QmRgrGXSok2wWv9MwG9Zv1Trx9JGHK6hbPQ94gv6jq3Z3y"),
    "3294938531": ("MONSTR #1987", "QmQiBu6T7HuVc63vQCkTjqXEFooeP489FFewr9tqx4xaHY"),
    "3294938630": ("MONSTR #1988", "QmXPAJRGuk3hW3u21C5tDPthYfwjtW8kjDntFimnnj1cJV"),
    "3294938758": ("MONSTR #1990", "QmY3doMGc7r7cYxkRT5a6JHGJjXDaBKs7tj2qqMoLEiENW"),
    "3294938911": ("MONSTR #1991", "QmXgrAfkefNJeHj9xqH6iu5Fa6RHXwtxuVv4E5LDc34xy6"),
    "3294938999": ("MONSTR #1992", "QmUyUdhEXY3B4XZvmsPiK1ZepGrnrzmXhkY58SjQMSJ23w"),
    "3294939137": ("MONSTR #1993", "QmenK6nYDAAxZnkRhkweMZ9U68CCHyww4VEE7SG3Xr44dP"),
    "3294939286": ("MONSTR #1994", "QmRYT9gWXcBgYo2dd7J2MjUh5RYEquqaX2V93tNq3Pbhyd"),
    "3294939414": ("MONSTR #1995", "QmQbGWR9UuQZPev2mCco4GsZRPgUWjZj9EQiEkCk3CmKnA"),
    "3294939591": ("MONSTR #1997", "QmRgDNQUyCeQrEytmevVQgo7CMrctkMMLxBnXyG5mPSzYo"),
    "3294939991": ("MONSTR #2000", "QmZTH5yScmRJe4wSsZQj1GGbTT4vj61UWsx9uENCgdzhWF"),
    "3294305295": ("MONSTR #0001", "QmVNZPtrLWUxW9JcWrQMre6okkKG5Dp6QU937txdffazA5"),
    "3294305660": ("MONSTR #0003", "QmcK7HW7pMBVouUzqvsW9corNfhS5BchT6UvnjFp1CCPn8"),
    "3294305857": ("MONSTR #0004", "QmSMb6cUwkpiZYWo4dPh8sWc3bKf7bqZQaFRgYe1QHFGVi"),
    "3294306391": ("MONSTR #0007", "QmepMCDqF4R1DDn6ytfARXh2pxgiWPzyQgxNVS5w9xfpPZ"),
    "3294306459": ("MONSTR #0008", "QmUc8RJhtThkPSxTo2KRC4Kk5Sf32CqVUuf7ivwhgktw5Z"),
    "3294307153": ("MONSTR #0011", "QmXyKbivnY8oqWSLUKxW1cetzojafTmoJKUbTLUsrSYMq3"),
    "3294307389": ("MONSTR #0012", "QmP3jyaHzeBJiZXgP675hUtbau7n7c7NKhAiNZtLGNd3m3"),
    "3294307526": ("MONSTR #0013", "QmaDeGpuZW3jNpLE5LoSL8SViF4AMeop2bGrC76y844BYK"),
    "3294307733": ("MONSTR #0014", "QmTybLhKo3sMYGmZg8xhQknKcjiFWxTSeS7NkjgaH7cu6u"),
    "3294308046": ("MONSTR #0017", "QmYHxH8geU574fgLipoieM5fYNj6TW98CFDD7zVD2321kL"),
    "3294308127": ("MONSTR #0018", "QmRU7ELcaiw1QtUkepQsBp3d3R3KGp8fcEpjiTidDSAvQh"),
    "3294308740": ("MONSTR #0022", "QmUbV8RGB6VenMEeeXwiuLtYEEfGBg414TZYF8GXgq76ro"),
    "3294308852": ("MONSTR #0023", "QmfV82pYbvphkZ2L4Tp4vo51UJSsjW7go3DQmPU12GyfTi"),
    "3294308947": ("MONSTR #0024", "QmVw4LUrxbPF2tqRZK9JL3vFgneiPWPXiNQG2ZwnBoPteY"),
    "3294309325": ("MONSTR #0026", "QmTQRNea1VHXi4utWPDqUU7xgTtX4DYYz9Vdr8UJvRAyjF"),
    "3294309594": ("MONSTR #0028", "QmQN3apAcNppvsHxgBdSgfKrKzRcX42iDLRzuyoH4ocoMu"),
    "3294310176": ("MONSTR #0030", "QmZsXVhMAUEofPuQyB3cErtRhSqqaW17EPuYK4BwCNxJt7"),
    "3294310249": ("MONSTR #0031", "QmT5beddmzhKP9HbSHexyZzNDApQTvh5vBsEhiHMt3xxee"),
    "3294310441": ("MONSTR #0033", "QmdmJeEXcNpiAotDVpkBFdWoft5sHAqdosGERUKyWUNgMR"),
    "3294310572": ("MONSTR #0034", "QmYyPqXJ6r9Xm2D3m83REVhc6Yn4tSJjqxDTLz42Xnsjbp"),
    "3294311047": ("MONSTR #0036", "QmdC9kfZo77gBPP5ger13pim5exVteTBrfdMc7GGeS9SHV"),
    "3294311275": ("MONSTR #0038", "QmQz8T779b8U2GCNWgVQLsHMYF178qv25SfJ2mGqGPotkX"),
    "3294311140": ("MONSTR #0037", "Qmegmd8e8Gz2ntYJbyuLaxha5mq7kYNrU9nuapuofEQh87"),
    "3294311957": ("MONSTR #0045", "QmZSobHUvwYs7z1Fom1RUXkd6w3KhjrLg6HJPSG6dopaqQ"),
    "3294312249": ("MONSTR #0049", "QmXHbi8vg9DH6Mmn7m6wj7DnRdLhR8DANG3PSVbjaRagYK"),
    "3294312313": ("MONSTR #0050", "QmecXzCCaGFBCwwwafqCGs1hB1yJbfLh5FJnesxvrvwEam"),
    "3294312395": ("MONSTR #0051", "QmaQ53vwsqAS1Li1Rot3oAx3vAAhqZXveuH8pivPEtiHt4"),
    "3294312487": ("MONSTR #0052", "QmSjVCDsgsrJR8eLESPirGLKqVor17AVuv8JqdYmYxpYC4"),
    "3294312636": ("MONSTR #0054", "QmQKuFWZuyseWeDY5nWjqMgfmGFVEPM9Ao3h6wUxDyFM9q"),
    "3294312718": ("MONSTR #0055", "QmWH9a6K2F11f3ZKGgSxaGU64boCdjduVRaRdQyUQCGtan"),
    "3294312953": ("MONSTR #0057", "QmTQjnaoCMAJArzYA3THp5kbsDvQV5oJewASy7FGjdCCXD"),
    "3294313203": ("MONSTR #0059", "QmTvCeTue1q1HfKGs88wK95cSHY6Gm1QYkGVURYjdLVQZC"),
    "3294313506": ("MONSTR #0060", "QmZcix9CuWQQpNh4p1HdQqGWkiaNoULEhCF3CZe2vE5gUZ"),
    "3294313634": ("MONSTR #0062", "Qmeq6e3weSVrcu4QiGMe9E1epChtm81WJ7E2siQJMD8sc4"),
    "3294313739": ("MONSTR #0063", "QmZBqLQM1J7WHbWJ6utdYwaKH5ahsGDy3my8Wq72hn3BD6"),
    "3294313845": ("MONSTR #0064", "QmejBhCqhx5nPFdarcHa8wfyTBEtEmR9RNzWGYQuEJ69fK"),
    "3294314676": ("MONSTR #0072", "QmVv95h49stzH4ALww5g6cGh55i6orpctqT4mmtjRdoJzF"),
    "3294314845": ("MONSTR #0073", "QmZrrw4dJuLxe5jPiEehNZxkVbpFendJPpow2i7MTw4WkL"),
    "3294314933": ("MONSTR #0074", "QmSZeawSZJ5ZQnHopmRn5YcstbJ57xbHEEepCMHbiRemE3"),
    "3294315043": ("MONSTR #0075", "QmXLWRjRynHwsx9SJEbCHUYaaUvyTKjEcvnv6JS9PDpPaP"),
    "3294315524": ("MONSTR #0080", "QmcJVSMvSWmn1FV9nTwqA9rDEDPSrMvs3j8LYQdzUjThNd"),
    "3294315616": ("MONSTR #0081", "QmbUVsktFzoiJ9QH5NwJHujWSgsDyJ427QycKhsEAoxdeN"),
    "3294316129": ("MONSTR #0085", "Qmbg6G2FrJYpe7rDwKupiEaSvyT2EMzhprSk81QnuVZPBW"),
    "3294316293": ("MONSTR #0086", "Qmd2Po99ygyX2NoX31sLnCvCKrDWTSYHZrfRY8kvaNoBQv"),
    "3294316387": ("MONSTR #0087", "QmRpFcsogGJ3MS4tiMi6AeH6UvScSUm9ru6xQLRE29G1jD"),
    "3294316471": ("MONSTR #0088", "QmPbdZXu8CPzp3ugbX45XjKRPFo7fJ7uAUoUpUqSCy7qZR"),
    "3294316539": ("MONSTR #0089", "Qmf1Ze1cKkH5r4o7bXrgSpRLo9Cjx3Pwnjue4XwkWhjPrm"),
    "3294316647": ("MONSTR #0090", "QmenLguZbefuxeXDFv3rNuDADu89VbgmouPVDhsy5iKsqC"),
    "3294316862": ("MONSTR #0092", "QmTZVyD3czZwqttqUom4wnzJkfap9oELKmmVWmVspc39Cv"),
    "3294317017": ("MONSTR #0093", "QmPKbz3kvVqwT6ogY7VUM4XjaG1YeX6HPNuFgKynxsdBw4"),
    "3294317227": ("MONSTR #0095", "QmTMLHn9GPFvgRw6Y6QUStiZHsPiVJfhq3eEcrbSJV8Rw2"),
    "3294317453": ("MONSTR #0097", "QmU9Uou1TAwqgdE6VzcCW5M3eVcyGPx3peYetsa2nse7DR"),
    "3294317784": ("MONSTR #0099", "QmWJ8HcdcD6q8trMa4MxXoPFGztVNtMRzaMo78U5Z8vGz1"),
    "3294317988": ("MONSTR #0100", "QmbpNcS5aFnKyTpSB5yD7nDTANPFojYJUmsKXNj3gwuLGB"),
    "3294318071": ("MONSTR #0101", "QmU3o4ErpLsR2VUvDeoxdv4jzJEWgKMEMHrcE57cf2Jdfn"),
    "3294318213": ("MONSTR #0102", "QmaGg1NG5YHXy81K1ffLXxHgSkjA5TqEozc3AQqtn4Ejku"),
    "3294318491": ("MONSTR #0103", "QmdoPRdYj4pBhJbNPVeZyqHy1pW9YbZtdpir23FQq2E3h1"),
    "3294318879": ("MONSTR #0105", "QmTF3D9Apke5Zc8mZTMz1W9ougkun7pK4hsXiNdktDsBNi"),
    "3294319194": ("MONSTR #0107", "QmVmeMYNjmMzooUyZQdm9jyAtPo2cYidmmtrAD6EuvPxDV"),
    "3294319431": ("MONSTR #0108", "QmbN4JBHidZfKPovFdqJDNghxYWJvag2E8E4VCsSFq9RmT"),
    "3294320248": ("MONSTR #0113", "QmZmvTufCTHGFGu6JkiSUS2rk4tj85QTrNw8Rj5grw4kt3"),
    "3294320397": ("MONSTR #0115", "QmVrpJkCQcsaU93sFRi4rmjahr6auCvc9DygtZmj1ifmGh"),
    "3294320626": ("MONSTR #0118", "QmcUPtLtSuYz2bP3Cb1jp1h17KCnnxDECmUp7uNjVsWomU"),
    "3294320744": ("MONSTR #0119", "QmNnJn2vxU22qQPXVAG3EjqL3C5R7g3HvY9yPSaGGdUv9E"),
    "3294320806": ("MONSTR #0120", "QmZyV3LnKc4RJW3YzV5n2KHdjdm2AXPBffyrUSeNmfzgcc"),
    "3294320963": ("MONSTR #0121", "QmfBBsbXjCz6myARRdJrtm7tdf7SyMyofJiaCf9wdABAHa"),
    "3294321210": ("MONSTR #0124", "Qmb5zPJmDF8Ru4pmWUmLfntTF9J9fUMszAXoo4FNgcv9Lu"),
    "3294321297": ("MONSTR #0125", "QmatTNFwF1hcucsoMpBy41HPVLzjxoFSpqehhK1Z8M2QTj"),
    "3294321386": ("MONSTR #0126", "QmRUvb58vrhV6YzRsKQaVKmUkRywX4YNgW9THYustx2Rsw"),
    "3294321655": ("MONSTR #0129", "QmXfUCZcWMXsayp2yJCin1tSxpQTRxpZVFtXXekk5sC8h7"),
    "3294321931": ("MONSTR #0131", "Qmck3FL2LCVnVEyH2WTp7xZ31MmqN715Ace9oLCKfxwjcX"),
    "3294322182": ("MONSTR #0134", "QmXfcwUzeK9gBjg1eWLxaMAbY3JhCdb1nXwriPRVd1UqFN"),
    "3294322232": ("MONSTR #0135", "Qme7g2nkF7S4gb23jHD7WTUxmL7EKSJDv4tzr5WoXrmTim"),
    "3294322439": ("MONSTR #0137", "QmWPh8NMbud1A4QErA11vi1exS7bCHnpNfF5Fgsa5eYviR"),
    "3294322497": ("MONSTR #0138", "QmV3X7otavXupfqWnnuuh5WG7TK8P9qYaAPCvPNRcprEfx"),
    "3294322640": ("MONSTR #0140", "QmbNQYdzxYoQdNT2HWZ2nU5PVwrQwRDqHfhxPDEaxL5peP"),
    "3294322807": ("MONSTR #0141", "Qmd8sUPTPDBjJ97Ys5muhrrGEWjjUXYx3yszoLWckYt6U9"),
    "3294322905": ("MONSTR #0142", "QmWphrMvf7WA63QiAxLUDT1qTtqZ6ESJkTCkouJTi7yHrC"),
    "3294323078": ("MONSTR #0144", "QmQy5a6yS8faZrzueHPJpwho7EQ584prp7SHbjoi4bCbPZ"),
    "3294323398": ("MONSTR #0145", "QmXZizgX1PqTs7fhSWcZ1Yq6u32AywqsbQdCFQDz1dxovs"),
    "3294323513": ("MONSTR #0146", "QmeENyWAzCbE1yzu9R9Vb1oMuUMMN3nj7Db6hgGsA7PaK5"),
    "3294323668": ("MONSTR #0147", "QmWp8bZNeCJ9VU3cz3JdM3WbpPxgpEL9YCAq8nSiF92dGx"),
    "3294323785": ("MONSTR #0148", "Qmbft3BapaFPs37xpJBXMAeMN4ouiaCbKVu65DYR5HsbiE"),
    "3294324061": ("MONSTR #0149", "QmYdSQn7oJxNsdV5n9HdZtGhVA6SFkDxCrcKEcQCaTnoog"),
    "3294324276": ("MONSTR #0151", "QmXcdLEd5bxFrjsLdYtiToEmcFuzUhj8Xgx4GnVkzQ4Tod"),
    "3294324337": ("MONSTR #0152", "QmNa1PPn26hG9GY4mzctwrjFDUJHZUA9pPsr7fYjSDLbT6"),
    "3294324411": ("MONSTR #0153", "QmTDXpYJs8cYAT5aQbqQE2wz62JXp5ckCcN2KBS3NmHENT"),
    "3294324717": ("MONSTR #0155", "QmUrx5MaoSrZ6d4LzFohwnfMMeTY29uxSfEx5K6qg9MxPF"),
    "3294325134": ("MONSTR #0157", "QmXPJtU8mbR5MtwcVAFbW12qBHiq7JuGLdswzzmouPZWNe"),
    "3294325539": ("MONSTR #0158", "QmWWodWDYhDsDfQ5huamUV1CKuxxTpgA6iphnYq7gE8nqn"),
    "3294325724": ("MONSTR #0160", "QmPR4niF8Tfg7uGGiDxTiXBEzdmveQ3Jrs792iWJFortep"),
    "3294325840": ("MONSTR #0161", "QmQFrs6oWM25KmXR8xsoZ4u7swbXTAgATv4eiwpQVc71i9"),
    "3294326028": ("MONSTR #0162", "QmSciRvce2te5JeThZJnxnquAgDZNKgGDaLdx4GkhtHCeZ"),
    "3294326175": ("MONSTR #0164", "QmZPfj33ChpdtyHbYu69QngFUnh4xLUu6hTBCjChGEwj3q"),
    "3294326297": ("MONSTR #0165", "Qma5yrygfDCkiKL32AqLbkFS1oYB8ULw1shYxNjt2AFjZ4"),
    "3294326402": ("MONSTR #0166", "QmNsCgeYFEDq1gkSBizeXXbcKSArv9Udx5nU6twe1e9mym"),
    "3294326526": ("MONSTR #0167", "QmeumbbgXiRkZcWHUHKDqFSvr7d2U251vBb1nWdJB3CFWZ"),
    "3294326658": ("MONSTR #0168", "QmPEfUQs5mwMAFJmeaxCKCGyambJA2Cfk43YAMYHBiGipA"),
    "3294326926": ("MONSTR #0169", "QmYxY33UunnLQotNNQCCXyCw5fSdw4qgBt1cHhLBxoy5xj"),
    "3294327179": ("MONSTR #0171", "QmU7Kp7RLM33mcDPKXFX15VjcZQXJYxRMymc4ZvXTN4yWo"),
    "3294327229": ("MONSTR #0172", "QmdMkhu1bTS6ZY2y4t2Lzjrgc9KDG5hkRsTtZu5T2EZNEZ"),
    "3294327376": ("MONSTR #0173", "QmUxuyFWWX6nmWWC1zLJoL4TB6oURz7MU6tdGsTSqSVS7S"),
    "3294327430": ("MONSTR #0174", "QmXLqKXXc4A7EmNm7LqPmtWKEpp8mMyK6Bq85oWcNk2M2e"),
    "3294327539": ("MONSTR #0175", "QmYyy7VMNbNiaCSem6uDmXHgY8rQ7Xg3F9KWCsXqpc9gUT"),
    "3294327688": ("MONSTR #0176", "QmUJJAEiyZ6VVypwHUnVyTU6nEXSNwQqm8XXfNDgXXJBNE"),
    "3294327897": ("MONSTR #0177", "QmUwYkYibcj2evRYANfug23UDCfsLu1UD1sRPMku3jBZm1"),
    "3294327953": ("MONSTR #0178", "QmSNns1NBzBDv7t9tePVMYYh99nwRttuxLFpWhUB4g5SQq"),
    "3294328142": ("MONSTR #0180", "QmNTzb73yaBT9MFdgK5c5AjGmW5j18vUriNEvAdFzgiaZi"),
    "3294328194": ("MONSTR #0181", "QmYjipUrAgYE8SY6yaYxAuFNd6RuKocT7FNN9EqzBsx6TY"),
    "3294328349": ("MONSTR #0182", "QmbxRjqbb9eVHPZVuF4RQTUCLtYVE54ShSD7ynKcBjVa9X"),
    "3294328482": ("MONSTR #0183", "QmYbdFQYW3FTzbhg2AvZpk9Z7n7zHkWGt41R2VdR3YnR3n"),
    "3294328637": ("MONSTR #0185", "QmbJieg9eoMPyrXTvT4ybbohmrTNmm9AsMj5fD9v1ghcD9"),
    "3294329084": ("MONSTR #0187", "QmSrTL4brJnN8ecSpCsQJ8D1cuSWupKvNWJcSrhVuggyeP"),
    "3294329192": ("MONSTR #0188", "QmPXEegSnQjcNXmDxyDo1jkkjSN371hpA5nscmcXyvYh2z"),
    "3294329301": ("MONSTR #0189", "QmY5aw77RAdiCXUaK4gvxqusL6YDotMMcQLzWjrb2Q2m3b"),
    "3294329558": ("MONSTR #0192", "QmbJcEnmBxDtGFt1eAxm5zMYPrKjFwnE6ZKXMmtDZeEegp"),
    "3294329963": ("MONSTR #0194", "QmSWLjHqKVwyvzTdSNYChqniBWMV9nTnc9xJTTPVbax7mL"),
    "3294330058": ("MONSTR #0195", "Qmasjjh4moYqj7tWwnGM3gNJih1ui7ZExD2LpZRYHyGAEx"),
    "3294330239": ("MONSTR #0196", "QmS4RguEGRzUUFgs4mhTunxWRKjxSaxgfRyHjQ3MzoDTrd"),
    "3294330761": ("MONSTR #0200", "Qmd8q5wffqxXkYrD7URjZ4T5DxmfvkvSHs81dWmKMgYtuo"),
    "3294330926": ("MONSTR #0202", "QmVcKSmuSj2eaLmYWrEbmTg5QWM4sWoi3uQbZ5Y8QUeZNq"),
    "3294331030": ("MONSTR #0203", "QmXp9AwweBRT95KMEp6DDhfhWHgnT58g2EfA39Sjd3Ts1v"),
    "3294331218": ("MONSTR #0205", "QmQjThEeAkz2yk9fDrdD61aaiGE3xELtPaoPbMvAUsXoEb"),
    "3294331561": ("MONSTR #0207", "QmaA7Ns7w6GDYTmHtw6x7XdWCjDtdA9UHeGV6NDH8SjeP8"),
    "3294331782": ("MONSTR #0209", "QmbXnNzUYuu5bReURqJCAGUyCE1XtPZbYpRoaHnbzDi53b"),
    "3294332001": ("MONSTR #0211", "QmXtd5g3nMByxfXxknCXuTcbSgd98P4g5F9MpfSRNtQR1B"),
    "3294332106": ("MONSTR #0212", "QmSJ2NF3my3h2ai4U4cvkfu2gUugmyWP92ne62N9sPzKUj"),
    "3294332472": ("MONSTR #0215", "QmRZ4cPNqSuXPsHwTYM4xyHoXw9JjCy9CrjjYHf1WqYs12"),
    "3294332601": ("MONSTR #0216", "QmTAaLydP6uy72Gqq75Gj5uHWS3SVpr3vFtcHDGjCsUSoS"),
    "3294332884": ("MONSTR #0218", "QmXhJtADKTBQcdbBXARBb8trG16WNpVBCKNSr7z7YTUxfp"),
    "3294332955": ("MONSTR #0219", "QmVuoZjmqUnR3iyih7JvxFZSVtSmmM181aUCPYmiaV7qeY"),
    "3294333074": ("MONSTR #0220", "QmVUu5PQZKnrV2LfMZ3ro16Dru4amoAGhYephx4kyQUNKh"),
    "3294333481": ("MONSTR #0222", "QmWhQmGb8FMBbb68VvYVWpfQT8PnzJULKQi2pTrfQWQJbV"),
    "3294333510": ("MONSTR #0223", "QmXihYeBYBmVFBbrBSWhhteeSaE7kKqHurHyuPAUZQUrCH"),
    "3294333630": ("MONSTR #0224", "QmSExhdjrFrTqHAhAqVtVmuHsRBkh8RfJNoz1drHP6NLxd"),
    "3294333700": ("MONSTR #0225", "QmVtRq1VZCft6B4hTM6wMHsaJUfhwZNatHro91WrCn8ah1"),
    "3294333878": ("MONSTR #0226", "QmPZuu7ex25MrSqeR4r8hLk3iy2f7k3FjyGAjxRBs51Z4J"),
    "3294333939": ("MONSTR #0227", "Qmb7CvbRzpD6dNvCMCj8rdsTi31QrU8gHg55MJqh75Wcg6"),
    "3294334015": ("MONSTR #0228", "QmTNMa7TEtPP3KJ5dUaPQ2BSRWdk7bB2inbokd6WzegK5U"),
    "3294334192": ("MONSTR #0230", "QmUXArjYBxHoHSRa9rxSXTzrt77T9hFBR4zjvroB1iABhm"),
    "3294334244": ("MONSTR #0231", "QmSWh3ZZ1qZZ1SjfND1rtFP1XKygrdhoMrMZQdrSWZPzCV"),
    "3294334446": ("MONSTR #0232", "QmSKBMzHyVFSYd3XubScht2TjVWeqfTUySYfDb7mXMPJHr"),
    "3294334909": ("MONSTR #0233", "QmWtztv7DfgAkjpAF6k2eyg6fFrUVbY2MbKZfHKssBwQbj"),
    "3294336501": ("MONSTR #0235", "QmXFQUbStP9ESBMDBZnAg7W49Txh6yvwYzLQyNSKkggJ6Y"),
    "3294336622": ("MONSTR #0236", "QmP9fCHrHYtpwSsqNptTaXicxA9uLDHQyL6Fox2DsyT6SY"),
    "3294337110": ("MONSTR #0240", "QmSodTJsHrmngnz2VzYCGkeuZ85xJ1eFRPPnmZQimuQrmr"),
    "3294337363": ("MONSTR #0242", "QmZsqFnjqVeMZX4dPPWtmRfBNeHQFbF7UJ1yMrAWErbWKj"),
    "3294337411": ("MONSTR #0243", "QmQcatX6zwSzETDNbRJmXEJCHB5BL9crPhdtVFd6WoC99W"),
    "3294337659": ("MONSTR #0245", "QmTGofv65NE3oinP1h5CsRcbTSqDoQVoEzxpNDoTfm6emv"),
    "3294337754": ("MONSTR #0247", "QmYfuLEB48JuLLTMb2hicePVGWUEoZvmdx47TdkwQJKqBg"),
    "3294337807": ("MONSTR #0248", "QmXqeaBCif7CwcjLWAkWeDbMmo8c3ppSXa6Mp7ozBhLmzv"),
    "3294338287": ("MONSTR #0251", "QmZbRfdPzt1GEg3oxCXcJmTHAJngzKdZVFvcFr6FZkGPFD"),
    "3294338745": ("MONSTR #0252", "QmP5hwipBDRLHyf1Z9GqfqrHmkCJuzcPyKqrnEnVixzauY"),
    "3294342058": ("MONSTR #0254", "QmUvVw2sc6c7j2rijXzaWNHmh5F5z8RMYWRxZ7dBqxQvdY"),
    "3294342383": ("MONSTR #0255", "QmXNXDe5EWpFU94PcAGgzkNfCuJGgXuGZXXFRttKP2v22o"),
    "3294342862": ("MONSTR #0258", "QmX6H99151LVDgJxVg8E7e7TKzMMyxLnHByxAi4EvKUboc"),
    "3294343065": ("MONSTR #0260", "Qma6L1VmtHbot6ukkusKVtGbjeunj2BXzZcxviijgB2uJm"),
    "3294343471": ("MONSTR #0262", "QmSngZkizEakfi3MTCEEyH68NTyiGwvLULxsGYvLNfCGBV"),
    "3294343527": ("MONSTR #0263", "QmajVFCWFW8dsqVXsyuM884gLuyH34WUXjPgvLb4CCLVpf"),
    "3294343934": ("MONSTR #0265", "QmTDeK2iRERm6duMqbRPGLfQd2un8TdsoRcUg38w66xB2T"),
    "3294344064": ("MONSTR #0266", "QmdUocjMmKCRj9cYY3172GNo6oCuRmJKjXcskMBXJsa4SN"),
    "3294344208": ("MONSTR #0268", "QmSowhsp4TPvSmzzteBzYHLGpiEyB877CZSiD4WL4ZWiEE"),
    "3294344377": ("MONSTR #0269", "QmRx7t8c8Pn7pNVkf7mYaC63dp8dFDQz8RPyFfoZuUdgUo"),
    "3294344706": ("MONSTR #0272", "QmW7r39exFsFss4Kucs6dXiEvhfbtQJrGNhhEm8PFb1VG2"),
    "3294344980": ("MONSTR #0274", "QmTeWre3GQVyAZ46qnX6Gt7DqCMPbWPcrvLEiboPWbCwpX"),
    "3294345061": ("MONSTR #0275", "QmY2pyV66VrYN2JRcwZej6LCfoJ2x8HAbtXLLGDTVZKoXt"),
    "3294345247": ("MONSTR #0276", "QmaWmWjfNibxDeA87h4C25wK5LfZAxCcda1Mq69CrpnAhV"),
    "3294345352": ("MONSTR #0277", "QmXtjA1RQNfosAbU7xBCg3jcVhfP96n3MsA4fEc7kPx4T7"),
    "3294345443": ("MONSTR #0278", "QmdgF6hmEKCXnHcjCfd43k1PrgFYh31NN3n4EC5Te4b4Pf"),
    "3294345648": ("MONSTR #0279", "QmaxJL946cX1yLNPgwJe6nx4R1pSEonwF3rYzmjZjHVF1j"),
    "3294345829": ("MONSTR #0282", "QmTd6hJZx1nDP3BagFZMWZ689C9S6mN7EKLFKtABcoBNfv"),
    "3294346007": ("MONSTR #0284", "QmR7YKW2qzR5uoqQmvRCi5VWeHKe3fynewsbWRKGtaVPW2"),
    "3294346072": ("MONSTR #0285", "QmdEizNyfZxiGFwMmc92icnx5rQGMKuvCHnTsHi3k7VMhx"),
    "3294346291": ("MONSTR #0286", "QmTdhDsfHKmmm1pYoZo2M9C1vdRAFbugQasxGJWF2jjoB2"),
    "3294346498": ("MONSTR #0288", "QmTRQWmufRdV38wtZc9eJ9edKh93VwCm4yzSpvmerebQhk"),
    "3294346832": ("MONSTR #0290", "Qme4kZkCCeWatiE8GYhzXUoQcBS8huNnGF4oojLYLs1Vu7"),
    "3294346954": ("MONSTR #0291", "QmYZRqTWxfMnXBN87rhRkV93bA66DUVu3i5ams2wd8MAAm"),
    "3294347169": ("MONSTR #0292", "QmSSJTXSXUUyk6WzRaD4hSByz3mNEqS8DAo3QfQS8HcwoP"),
    "3294347474": ("MONSTR #0294", "QmSKTbbKTcujU5E3dvS5E28Ux4CxxLghtRbMr4sBtAKERN"),
    "3294347521": ("MONSTR #0295", "QmUwNZ3frTdB3iwS9QzPFzVoRm563KgeaUtoRritwbNGgG"),
    "3294347617": ("MONSTR #0296", "QmS91zgfEHwDw7zmTng4CiT2px6nCtipCDo6C3rQN9aZjf"),
    "3294347827": ("MONSTR #0297", "QmVXaviySdibwBeXDAiEQBtzFjc5UQqBwFjTeMyqxVikuZ"),
    "3294347921": ("MONSTR #0298", "QmeE3usea2oqsE7NoF3M8yR4bWKpQbRMNLpmpUqdRsC1zR"),
    "3294347958": ("MONSTR #0299", "QmeQBYebtZWjxXg3mGLuprLtX7s76Fdqquod5FdHXuPiX8"),
    "3294348026": ("MONSTR #0300", "QmWNhHr8p8dMr2VKtaUtKQKExDEqycmdQdSGopFPfcgDeZ"),
    "3294348125": ("MONSTR #0301", "QmUZnFWTsynnG3sSFPkzxdX4ckiwdREsJz9AJ7p2hgCsz8"),
    "3294348235": ("MONSTR #0302", "QmTxB6edzrkiAnB8MuTNo2rgoPtyvj2ZtqAFHkyndi4PAL"),
    "3294348337": ("MONSTR #0303", "QmQ28S6QYZNmhZH8vEEX49FG7yKESTtvDSaWwdeR3TSvEF"),
    "3294348410": ("MONSTR #0304", "QmZtzp2x1erVtBYdQDWuvyUEsJ5YDY9EhKYtNZryWQ1tB8"),
    "3294348687": ("MONSTR #0306", "QmfVXquEPURGq5aXrdCBFsZyUbxbLN5G59z61ZHA4SvX9H"),
    "3294348805": ("MONSTR #0307", "QmUyibSHZWUFCeJgVDfKsuQjkX3PCchxYEkyiU38goMuuu"),
    "3294348951": ("MONSTR #0309", "QmRcoxzn6FxFetqq84ts6hDWLxQkZT5f7MYYm9Rmnp7PPN"),
    "3294349032": ("MONSTR #0310", "QmboKgUDxjjG3QMKY2dZe7aTPvn4dLH4qegeN9N4bqSedE"),
    "3294349211": ("MONSTR #0312", "QmWW8rJANGUjq9W49j2Edpva3JLhwtZenxWhLa1ZgacZe6"),
    "3294349310": ("MONSTR #0313", "QmUY38TdW1aQTrmxiAFYc8xwQnkifNvQJKh3YEQT5wrR1k"),
    "3294349411": ("MONSTR #0315", "QmUHM8tpCUWCG5ER5ccXq7xvX4H75p8WJydyLct1tdgykN"),
    "3294349516": ("MONSTR #0316", "QmZyAXVMZofBK1moR86s1kkaYBj7c9FjMwEWHBfiJaWUPC"),
    "3294349730": ("MONSTR #0318", "QmR4o3B96yxQ1K1LnPPBy13Z6dqkJ6ob2U6fGfNnGJkoo5"),
    "3294349841": ("MONSTR #0319", "QmU1J7dZnYkBKWFc2kEdKUCyQuFRvmFNzRLgBd4FHk1cfx"),
    "3294349940": ("MONSTR #0320", "QmP8DguJkZto2vBJYYAMc5oWSNPnnxJ9WvPRcgSAjQDy1h"),
    "3294350107": ("MONSTR #0322", "QmQ6nEsuM6qfobvnyqZataEcyf8A88UBnBRUqxggR4Chjf"),
    "3294350217": ("MONSTR #0323", "QmdbU1Kz5tQU2u4shfuZZVTy5uDXbqZ5JbjaFRZtN3r3u7"),
    "3294350386": ("MONSTR #0324", "QmT5WQWNHUkQbcvssJkqETWAZDGNj9bXPTFyHgzUkGUhUQ"),
    "3294350612": ("MONSTR #0326", "QmZP2ZqyFokpmJJrusAa1zq9JcLy1ech2Ci8vTDKudwBEQ"),
    "3294350712": ("MONSTR #0327", "QmUMsaoTZymdmfthHXHeGpXhvm9NYaZZaAf1YztSRvUC5m"),
    "3294350887": ("MONSTR #0329", "QmTk8s18BwUPHa9gKcVpc8uHQh1aYHiSXvHL1yfLibJjtg"),
    "3294350949": ("MONSTR #0330", "QmPN7WsScYDFmF3K9Lb586Vmpbmb8pQLRqMeE2JPBp22kf"),
    "3294351047": ("MONSTR #0332", "QmdJMCja719nbpgQocaehWkEDbiZaZRQPJtJ16GF243T4K"),
    "3294351153": ("MONSTR #0333", "QmbuKezECBozvNXSPygetfK7AXac4kfCSo6nsDU2SXVHxy"),
    "3294351536": ("MONSTR #0337", "QmUAsQ5mdrbNp368MmYagWeuzBjF2yi5c2rRFwjMH2ngAz"),
    "3294351752": ("MONSTR #0338", "QmYXPdrLQuWY5UWDKq4GQCLCrLiRWH8RdAAZbrjv9iTWSk"),
    "3294351875": ("MONSTR #0339", "QmYXUC8ST995vsfGfd95Jj5Ex8u847WXQkz67xQ2RJqkpX"),
    "3294351931": ("MONSTR #0340", "QmYJfUqZkTQD2s9rqarz2vPhaWYD7kFdQab9rk9G12BuaT"),
    "3294351989": ("MONSTR #0341", "QmUxMpe9spq1J5fZ2rdMwVeQq2LhN8SbAiAC6E1xyerjF9"),
    "3294352152": ("MONSTR #0342", "QmcXANkCYXkcMCcTeKRFF6aY7uASpWqDsor1yZka1mgZU9"),
    "3294352299": ("MONSTR #0343", "QmTPfHYZVWpkk5ufTuCeNJSE6RSRh91t8RuSCRs5bEEg3y"),
    "3294352368": ("MONSTR #0344", "QmWUUaE1s5xWV6RMBL5u6R8mL4QSU1tR9ycWmmXKksBrYX"),
    "3294352433": ("MONSTR #0345", "QmfQgXevtSiHAryVG6nZtHhxthswErRddJ3vuaUY7kAS7s"),
    "3294352896": ("MONSTR #0347", "QmeayMUFHeugmshFDzTiV2E8bST1r3JuP6M9thKYFc4qsB"),
    "3294353098": ("MONSTR #0349", "QmXUixaFkVfEZjaL5pXReK7JjWeUiCq1Ven22XzChqbhFy"),
    "3294353148": ("MONSTR #0350", "QmRjnuPMdCu3aZVSVQT7ab9jSinUWaerFahKvipThp3grE"),
    "3294353410": ("MONSTR #0352", "QmNn4EQUgFhc7K7bP6DjGnQk9AuFhSq6W8B9c8bxPsrDt8"),
    "3294353500": ("MONSTR #0353", "QmYbikvhfH6JHQHe1urMNBhpL6ifXcqPT4a6WU5oDCCtEr"),
    "3294353658": ("MONSTR #0355", "QmUJFvxfjJqn5ReDMBDA2TczYLMiZ7MYhcetwb5xz61RmK"),
    "3294353793": ("MONSTR #0357", "QmSFNAnv4DHgayCadNK2D5cveYmvRXE8GU4T2P1CSHCCbH"),
    "3294354441": ("MONSTR #0365", "QmYgrHomhd2BGeDgK3qUYpjXu8hCht16fE6JVV5Nsiv6vw"),
    "3294354050": ("MONSTR #0360", "QmQ9wgkUPzM41GYvNtwG44RVSdWuj47NXQBHgzhfq3jLRy"),
    "3294354126": ("MONSTR #0361", "QmUpcdHnrCJf8Qzmq7jiMq9ywSdSwmu5gbQnp6VLGzDQ5k"),
    "3294354388": ("MONSTR #0364", "QmamBtAhqPcCVbHcmdeHsahZbzfAhFChhqNQ5T8GSZJz3C"),
    "3294355135": ("MONSTR #0367", "QmUNRhXydAg2dAyt4DeBhQPHvxG4dnUQBQsRyRNfT2ne6k"),
    "3294355197": ("MONSTR #0368", "QmcYJn3mdK25aaEhG81kzd9mBGKb2Y93YsTCqHXRhFzyhV"),
    "3294355468": ("MONSTR #0371", "QmdDYPmWG3hQsJwT5M4Nwvp7NE3irZ8GgzAh6MZdpmZAU8"),
    "3294355622": ("MONSTR #0373", "QmRBGnra5g8VtAiTiYpmKcsadhZ7kiAjvug5VFwsSKpXAS"),
    "3294355783": ("MONSTR #0375", "QmVdwAgztE2tBnFVnsvAYMmdAVkuZbFBm5XhCasFSVvM97"),
    "3294355861": ("MONSTR #0376", "QmP3kiSZPgWVfwac8kjWmsiiyM6Dw7vvcpDizPDBKSF8uW"),
    "3294356408": ("MONSTR #0378", "QmTvxRcApQG4gJ8XK7bdy13TxWYwfpB4CPB3tnh1Dz2h5J"),
    "3294356719": ("MONSTR #0380", "QmQTuGq7jmbKrBbFA8s52NK9dcHACyR3QTaU8cbm38rLCx"),
    "3294357230": ("MONSTR #0383", "QmNMrVmJVTJ5bzam8PgCXER6wgWjLEXb9EiMwMXw3TJyZG"),
    "3294357440": ("MONSTR #0385", "QmYQxrHWJRAEEvsDrguo8b9YJLo7A47Udbkggi6N3VpxNd"),
    "3294357505": ("MONSTR #0386", "QmX9jdv1PanB1tBXMJXzvTvmRfLrFYjCUM4cPDYWw6uQpQ"),
    "3294357566": ("MONSTR #0387", "QmW2wtyJ1cD46hhAg6jP7dE5KzGQu6nGGBrXLkuTNf8WLu"),
    "3294358131": ("MONSTR #0392", "Qmb7d7ZLbpwJguQTYzwXkZtuZxAYrFUr92dt4wUVQsRiHH"),
    "3294358007": ("MONSTR #0390", "QmYHQ3qzoZcccnjN8QpKZ7R7eGVUfJFTzAJkduaPDGsWMk"),
    "3294358162": ("MONSTR #0393", "Qmbkf4qwJoBgtLR7KDmo6MssDeS1MfjiJkGHLW2s36zVky"),
    "3294358402": ("MONSTR #0395", "QmeG36pUoWFo9F7d7KcdfVSzirJBN8NYrHKmkTfuTEyLfu"),
    "3294358555": ("MONSTR #0396", "QmR7pXC4kcMJCMbLFGN9XFFt72QZRFZfvioiDDdqzB87zw"),
    "3294358744": ("MONSTR #0397", "QmR8JDvsxQ2tY4djR7XfSTxb5crcCbMdNPHrrBSNeBUimu"),
    "3294359092": ("MONSTR #0401", "QmYqWgrc652jA2SAaCPuyfX31j1FytcJbUYSGbmcWGwUDk"),
    "3294359232": ("MONSTR #0402", "QmVbFuNXeoTD3gdktwB8qpRvSUCz8nu57TwDfxG1NTzK4q"),
    "3294359373": ("MONSTR #0403", "QmP476E74hJRfC5XBqMCwrXdUrx67WRGkYQhjmBaEvkuvS"),
    "3294359591": ("MONSTR #0405", "QmVDyQ3XKKAMa3ei4xqztwoyZSoFu9DzKc3CFow3qazdtu"),
    "3294359823": ("MONSTR #0407", "QmP1iXm1eQ8n7gD57o8wfrEbKKC3zXGJZveb1HCDEXnyzV"),
    "3294359898": ("MONSTR #0408", "QmWdJR3AJPcSPAq7gexaTEqVBGNzSoZ2xwZ9gCkyuBVWis"),
    "3294359927": ("MONSTR #0409", "QmYsvR6VcPEcbtW6jPyw1Cvfts281ek9jtJu4ZHCTp5JX3"),
    "3294360071": ("MONSTR #0411", "QmSY3BfriAwTVqTneWUYjUy77pmrHwVzh1MgeSJb1D5Zaw"),
    "3294360140": ("MONSTR #0412", "QmdeRsYwRFauUVVSB5daXvWirKihWiHb7Q4M56Lip3MqjL"),
    "3294360379": ("MONSTR #0414", "QmYdPLB4J2hXeRCLAH3NNMfX81YUVu2YP8a2haqnJJcZem"),
    "3294360418": ("MONSTR #0415", "QmbqWSRFMVQ9VcuDhq3JCxficH4pa437GJdreUqHneTZ5x"),
    "3294360486": ("MONSTR #0416", "QmWcU6uHQYB2PJPPSxjLxWeUoqHtyeiS8RQQ3gehBXXrcQ"),
    "3294360626": ("MONSTR #0417", "QmUHJAKP6uc4oTQ2sSiyTCk8cqKrcuuHC86x7esnZzoFYJ"),
    "3294360944": ("MONSTR #0420", "QmVAV818CSG6yVpaBmnUECRJHwmqWDzmL1scJFEDVndRKs"),
    "3294361053": ("MONSTR #0421", "QmcMUTX7vNQ4RjFV7SMqjjWLfZdiUbrg6y1ygPQ6EhCJNH"),
    "3294361109": ("MONSTR #0422", "QmdtubA7maSa93x4RY8jM6AjnqHdkNgUMXkumipyGYGijA"),
    "3294361331": ("MONSTR #0424", "QmYhWmvt9vVWM2zkX4Ua4im7W6gwU1WfZjxGEKy9RSY56F"),
    "3294361610": ("MONSTR #0425", "QmWpZuYBYJh2g3SMB2VJsoFzprUiVKiQLBwbQpwYqgu4ne"),
    "3294361741": ("MONSTR #0427", "QmTmVaRL3hhNDEiZ4eZgPjjaBoU8NQBuhM279yDx4kieKk"),
    "3294361975": ("MONSTR #0430", "QmUNBNCXN1GeXkzXADXrETSvv1WXoTWipnqDh7FWzxuoBq"),
    "3294362095": ("MONSTR #0432", "Qmdapgf6FiUdVbP4ovmG4frkxS1P7Jex7U651WXRYUptaZ"),
    "3294362252": ("MONSTR #0434", "Qmdq8wHvB4CYrBemZFp3eJpcxfCYfaMay2ZSAyvxwLJZME"),
    "3294362365": ("MONSTR #0436", "QmP5djxiEdWPUB6KNdt4HEmNtAuRJSs6jZ2LvDSpPH7LaK"),
    "3294362443": ("MONSTR #0437", "QmPsbsBzcDdSpdtrSfDew46c4UQPSomMUSaaRMTXgV11dL"),
    "3294362815": ("MONSTR #0440", "Qmf2SJ3SUdw7ruQ6eTN5zPM8xiUvstbZmFmU8nPDhZYNKp"),
    "3294362939": ("MONSTR #0441", "QmVAAAqPQfeNcnHJ67EFcDFHWn6b8BJSiSwfySuTLaQy8H"),
    "3294363181": ("MONSTR #0443", "QmSnXpnU49REurm7sLtvUDN1z6o3o7uurm3ZDVzeWRvMWr"),
    "3294363254": ("MONSTR #0444", "QmVWFeAqvQz2jntJYo66i3Gq1t91RHAuHWsVHt4R29xM8S"),
    "3294363635": ("MONSTR #0446", "Qmf9EkXjMExmosBRtvXtUG57KZLefReKWiByVgv6NP58AS"),
    "3294363738": ("MONSTR #0447", "QmZkw7XAfM2DVKt2MC5mp3D3Vc4kmpR8tu2uaneJpebzQF"),
    "3294363871": ("MONSTR #0449", "QmY4UM1VmBUjLAca5BNmZFRkAHYcxiZHFNQPcEL3pkYDrc"),
    "3294364042": ("MONSTR #0451", "QmNYTANkp9b69A4xhUvMJu1vnoTJQGJWgkamFxKvzqEZwG"),
    "3294364124": ("MONSTR #0452", "QmUwTKx4JVUqdFozw9yTJAJ2YCUMeXTJTmSJee5JAAQDwV"),
    "3294364245": ("MONSTR #0454", "QmP7tztNkSWM9MkWQGjejcZPxEFBoukmwGKVFgjzY49xWg"),
    "3294364675": ("MONSTR #0458", "QmTTMTBbP7sj6EaDTDZyWZrByBuFcB7H9ivv52uwYHaDNP"),
    "3294365239": ("MONSTR #0462", "QmYu4k5ZwMiMJM4jQ3nqGwYyJBVrx8LhUYzDJdWqhXvg3t"),
    "3294365660": ("MONSTR #0465", "QmRA4RoH47F1n49TxdXBqN1wV4s87Yi8hgzHkxE6N8sBiN"),
    "3294365849": ("MONSTR #0467", "QmNwYpSYAPXiSvdGAqDSpncWwXusmSqaWK1KeApNsnSufe"),
    "3294365943": ("MONSTR #0468", "QmVHvULkKqDcb3Z6L2R9YKRbRhSPhhUdHXJeLfJo7n9UgZ"),
    "3294366116": ("MONSTR #0470", "QmYKwTXZRckhtpKXp6Rv9TFZA3vtYfcRxohiuxcS44km5W"),
    "3294366195": ("MONSTR #0472", "QmfLYCW3Gz9EJwv5mJAQ73Ma8ZCp9wLufSLZmCk8yBxH8N"),
    "3294366296": ("MONSTR #0473", "QmdSi84fTVrxVxddWcLXs1u4AFp7Xsh5ggaeV6mHmDHRUR"),
    "3294366419": ("MONSTR #0474", "Qmcu6L9thqrhJK9pPrZzi6zaSECSxxba31iZEGKSH5TBFX"),
    "3294366529": ("MONSTR #0475", "QmTVRyywHv9uAENiCSzq7yZCfNqgTaps1MxdLjGV6V4R2g"),
    "3294366611": ("MONSTR #0476", "QmSgy59FtRddxuwWkiZYMc7asVCtvastKHSguExnMZQALr"),
    "3294366669": ("MONSTR #0477", "QmURSUueonMEWB5PAbBcsrehAxXBySNzagwSWugNx2fGao"),
    "3294366817": ("MONSTR #0478", "QmfQpG7ikz2BScz3gRo3zZqQiqfDg2cyp3dMLQtDAASprK"),
    "3294367024": ("MONSTR #0480", "QmfWhEt5ShjQRMFtcdKwGAnpEu1UzsfWGVWeacScBdivzz"),
    "3294367262": ("MONSTR #0482", "QmdUUuxwxA4BnhfYGjES9ztja43HFaJrnTkyLjRXP8FMt1"),
    "3294368035": ("MONSTR #0488", "QmVk1MkH9skEFah9foqiNnYhiyEar41ZkrDqXsc2ajrnso"),
    "3294368681": ("MONSTR #0491", "QmZWQd9AHvkPSnUtswg1s92hWTEC8NEUvLH5c4NcCvcHhw"),
    "3294369065": ("MONSTR #0495", "QmPGQb3ntcyoDAGtAus4bQ4N59gwN69L8kJD8dJKxKmkE3"),
    "3294369418": ("MONSTR #0498", "Qmf3gLq89NH1GaUzrNUshB7KHLNSuUEhumvn5YQv3zLu6y"),
    "3294369479": ("MONSTR #0499", "QmVyEEMrZFCDFrP8EiNUKPNtLRE4aEzCgE4APKRP1x3X83"),
    "3294369657": ("MONSTR #0501", "QmYDmtLd9Nn8mRZfh8AGXyq2dutnzFbof2e8XC2QivRitA"),
    "3294369984": ("MONSTR #0502", "QmSXG6q31GJALsVhdQbQKMRCq156xRqGxRWUmTMSZ7NJ9w"),
    "3294370201": ("MONSTR #0505", "QmZQoAhucSEVrLusUcxmHDM5FyyBqhv9i1rBJ4Zb9Vk4iu"),
    "3294370276": ("MONSTR #0506", "QmTUK43jVVMqoUJqv3xWXZuwCpLfjopqyunJDgk3JbpGLM"),
    "3294370316": ("MONSTR #0507", "QmbWb4SufcyYUxkMWS17EWNqWhkFK5TxT9faM9GcYsBdqE"),
    "3294370684": ("MONSTR #0511", "QmacQ1L4Z1ZtNCHS5YAXhRj1zpYMB8cM9mTrGKZV6AghPq"),
    "3294370939": ("MONSTR #0514", "QmUb33n2CYtPmoUe6Tpxak2pWrCJJfViUCtgEb6VqPkr3u"),
    "3294371141": ("MONSTR #0516", "QmNbF7e39MkVi2PYB8ukEWqVGgptL9cCNHAJymwe2oEM33"),
    "3294371248": ("MONSTR #0517", "QmYVqwv84ggs9jp4hsaZHTAyfa5DVoL4ywXdKBSaj5a7Vt"),
    "3294371616": ("MONSTR #0521", "QmRgsVG3RZTLkS8ncPZimEcrhod6JPG2yasMj4VW4Yi4oN"),
    "3294371709": ("MONSTR #0523", "QmYexdUGe7GDXPHkJC5a2gZEQRdPECuSo171QTBExhxGA2"),
    "3294371861": ("MONSTR #0524", "QmcuaLej4jvLjmovqKEoLcA32bxBxyfEVA9eDATb1jeBuP"),
    "3294372114": ("MONSTR #0526", "QmYnjfVG7DvRQx468UUyLQcuAQMtfAN1qSMkkx9Xq4LZMf"),
    "3294372205": ("MONSTR #0527", "QmRzAaJKCrPVMY4qVyZgXPLPCYq7JpdgG99KpSub3hzVWZ"),
    "3294372281": ("MONSTR #0528", "QmRY1scaaV5jdrATDm9HLZuTbb9wuxkwuvxWARWEUpN5hi"),
    "3294372568": ("MONSTR #0530", "QmYjYE64w35Y5871ybnTRcX5zdHeTfd85rzKjvLaAAhDv5"),
    "3294372811": ("MONSTR #0533", "QmXQUk98b1dW5EEZSjnkt971MtSyCqJaeghXkVA9bfSKSf"),
    "3294372915": ("MONSTR #0534", "QmQpNJamkown4VXRRenQtwtXAuv6aDka5wsHCCphcMYvzs"),
    "3294373061": ("MONSTR #0535", "QmY2oMdHavSej4NUHWzuGTptpEbonP5mUC373rDyeMYnLQ"),
    "3294373463": ("MONSTR #0540", "QmV4EBNWZ7Q24ArvBsiTYYtTjmyX8fnXwcAZzqZKE2SGpG"),
    "3294373524": ("MONSTR #0541", "QmNQ9W6HqS5EuxDy8bXG4JnuXcZSHzkZHjbbyE3fFsaMcK"),
    "3294373733": ("MONSTR #0543", "QmSdCvqH7Hig6k5RKSngiFuCwrsH4RnANtt1xv2q7VMHjp"),
    "3294373818": ("MONSTR #0544", "QmcEYacvqkeYmhhjZJXJS6HXST7k3nz6G5yPudyBjj17L7"),
    "3294374033": ("MONSTR #0545", "QmVtjw9dTjkMwXhznDjhHuevGKoT2xAmydyXCpAkZLePmr"),
    "3294374146": ("MONSTR #0547", "QmQXhujWtWHE9UrCZBuAacqeNWkVy24oKSKQHCMAG1iqFv"),
    "3294374196": ("MONSTR #0548", "QmfJ6UnN2LSmt3MKyDrGL4npRNKBVCPWEJr75WQuCeUjZ9"),
    "3294374312": ("MONSTR #0550", "QmcZrHFGwyid5MD9eUFKGhXuY4afwszEzNksqER5mwhcck"),
    "3294374434": ("MONSTR #0552", "QmWopz8ySYe43dXkV967Z22qgtyS8ftgXxNymkfpjtqvwb"),
    "3294374573": ("MONSTR #0554", "QmerUXmQTjFeBXPfwhoPd9dnWSQerrzd1D9Q3DmTpE8nsv"),
    "3294374891": ("MONSTR #0556", "QmPfFkE754zsptgudWrTGKSGzYRKPPXR9TdzrcpBGvzPRj"),
    "3294375049": ("MONSTR #0558", "QmbcZx3GJcLbhTKq3B9BcY4HSptxWyiaikxg7vRLzQqKVo"),
    "3294375664": ("MONSTR #0562", "QmfSGAKuFknCpdAaXC4irAWAYag9J5oPGMoVFrD7cUhwJe"),
    "3294375974": ("MONSTR #0564", "QmYiF5KCbJdQsZ6V7qZdxS26cDuGFrzJuPNHjMfrdioHZb"),
    "3294376207": ("MONSTR #0567", "QmTKe8grqkedM5iNadP1m231Th5Hk4mMiimgkM7zSHSHTF"),
    "3294376293": ("MONSTR #0568", "Qmf25kZGNeJAj1BLdbBmvC2f13M2XkebqXyYwSmt3753no"),
    "3294376166": ("MONSTR #0566", "QmP3rTbZqtTnsHd4iSPF6UhPgtmBoyygvRwkAAjxyVMPED"),
    "3294376498": ("MONSTR #0570", "QmTM4odqvcWN281rZM7ZA9jLgLf6EHw96EyBcxzeh7EiSH"),
    "3294376546": ("MONSTR #0571", "QmZmaibgQPBxrpEYQVWmGFRQiAdKau5MWWha43RF8Cxo9p"),
    "3294377015": ("MONSTR #0574", "QmR96p4KVaDYW8Pf9vmVPcN5LvKqiEwX9YLbYyXwBDhw6Y"),
    "3294377075": ("MONSTR #0575", "QmWBzKQHHpwtQqEqXQwhhnn8XsE8DFfpW3CHwn1hRAiYiv"),
    "3294377393": ("MONSTR #0578", "Qmf1KY2XPkt14qEnCkEa2DQSk7KzZsuYWZYBxXUpcmFJQ7"),
    "3294377518": ("MONSTR #0581", "QmNbwRPYd7GMCDuEeh7ua2FaYtupRXqBXmXwHjdpwpJYhs"),
    "3294377657": ("MONSTR #0583", "QmdHsEa7rBe9pK9XPfCehgxs8kQyVNQEJDiNPN7MAfngVD"),
    "3294377785": ("MONSTR #0585", "QmSP159BYVQGe6Uax7P2jpV1VYxFBzJ59RnqbSw3SyPGKw"),
    "3294378657": ("MONSTR #0595", "QmUMkRHmw3ngcVt9qgfpGSC7wPV5TmD1p5urcKC61wTCTG"),
    "3294377965": ("MONSTR #0587", "QmR6mcJhMALAGqKwNP2zWuYhTp4wokF6EzPFX7SAQZfUSK"),
    "3294378076": ("MONSTR #0588", "QmeK28tvYN7JbETudDeEWLh4GAMwFgPPzu5KzUNZia9oGS"),
    "3294378197": ("MONSTR #0590", "QmYFnJpdoKbEfh4JPY8kAWdY27ZB7Lpvva64wJU2GG79ZN"),
    "3294378309": ("MONSTR #0591", "QmajH2iKfAmYn9zgaeDJrcXg1hrntXfAsZgje7sm5xuMMi"),
    "3294378397": ("MONSTR #0592", "QmSdFEysNd9wAwDhaM9XSZ1eZDpkNTqivhiQB87QscQHz3"),
    "3294378547": ("MONSTR #0594", "QmQsVG2wg6yjQwW2tKhC9PE3qb76s1nv7eKtstGGV4wUE8"),
    "3294379111": ("MONSTR #0598", "QmUEmxGcUZw485koUCTKXybep3tuEV3erWRRTaE9zfArVL"),
    "3294379410": ("MONSTR #0600", "QmeP8C9EChTddHHBjUtM97DhoKEVHQk9NnT2E3H2T69nr9"),
    "3294379443": ("MONSTR #0601", "QmYxRC1nXrMg8q7vrCABfTFgUFYDwYmihoRXfmzVycUMjJ"),
    "3294379511": ("MONSTR #0602", "QmRRCTFCQZitmbpyUsybEkdxDo1RxScqBJt1ML5Fh3YQRw"),
    "3294379685": ("MONSTR #0605", "QmU6MCAkyuJzeDHuMFdWQn6ZEKuHnSnqsKa5HQT6hz5GjU"),
    "3294379743": ("MONSTR #0606", "QmbQc3Fb5rpxggmaqNf4jShRodea35LRJddEDrikFKVBr7"),
    "3294380006": ("MONSTR #0608", "QmUSv8XypENH2t4rqjujJoTFcgsQef7UdGVDfPBJ6WrVNF"),
    "3294380257": ("MONSTR #0611", "QmVb26PUyaUpwhcKTphubLbEaDRCqufqyFS4EV4c1Eva28"),
    "3294380299": ("MONSTR #0612", "QmbarXNS16TjpZqngxFiSHwnopjbUsTRywUT5aXoKtBzcX"),
    "3294380479": ("MONSTR #0615", "QmZtMVGQHW2AUhXD2YoQ8iLo6Q94QtexN18PEnTF5RGwfx"),
    "3294380544": ("MONSTR #0616", "QmWVhmFYmBJmJEtQkuDKcQ4pnpuZjcWM9ratfdJUpCfC5v"),
    "3294380623": ("MONSTR #0617", "QmTQAPFc8SawJWeGTX9VW5hbCeH43ZiNJrP67k4pe2BUPX"),
    "3294380936": ("MONSTR #0620", "QmQ4srXiBjp4mVyVqhJqLUnKzXAbsPVTbqrdb1xbNXmQHM"),
    "3294381341": ("MONSTR #0623", "QmaaSufKy6WKQjjvnoAhj6gDsGNv1n64Yr2PyMQRH8mUiM"),
    "3294381714": ("MONSTR #0625", "QmQKxUa9SLqzgPdajAYiF9QdgXnk3vLzvxkKRYs353o67A"),
    "3294381831": ("MONSTR #0626", "QmcrYWYzKQnBPrv45NLnxWcdYomMe55mFNcwxUpnYwj75T"),
    "3294381962": ("MONSTR #0627", "QmbUDZPPL7DDvNidd4UzpBrgkNYaMfVbyPEBfjmMAaBBEu"),
    "3294382168": ("MONSTR #0629", "Qmd3BpqyfRp2dZSRvAzkz8UCpBopwpJsyC1ChYnaFaxAuY"),
    "3294382587": ("MONSTR #0633", "QmZ9eW77F7WuH2sYKfi17HfP99dwzpmTAhKZ9Q7PKsVHxa"),
    "3294382734": ("MONSTR #0634", "QmWqCDs2JQ1vVR4Lppxuqu69KamSXT2bJ32RyNAFSymB34"),
    "3294382935": ("MONSTR #0636", "QmUY1ZtvVqZfSECiSn6hRtNLAGyPUTamPYkvYNXfATA93S"),
    "3294383159": ("MONSTR #0638", "QmRXgeudiPDFwD4F4JATuvVYUeVcDo4rmVMeCCdoCJexEZ"),
    "3294383255": ("MONSTR #0639", "QmX33upwxSLbsA98B7xAn8DNSQ3umHHcJmFLQCkZNcHoCX"),
    "3294383328": ("MONSTR #0640", "QmTyQHGDwmDaDZXbaLNMf9Sy5KYcm82fdquEe4TxezFUUe"),
    "3294383539": ("MONSTR #0643", "QmevJUJuUTrYEWhZdUEjm1rqukupMRcHEebXhtyyPVdikt"),
    "3294383680": ("MONSTR #0644", "QmTgiGH14qW3Mw8mGwEUwuSP3t99LsvkNGZwmEHgYCbWKE"),
    "3294383809": ("MONSTR #0646", "QmQtE4EDYv8JHLvTwkwyEJup3q8wZ5k3KhuR62FS2UP3Ym"),
    "3294384068": ("MONSTR #0648", "Qmbg3H6Ly4jvQ45m1ctLSyfRkcHCx3WPMD6TnfoYkV89qr"),
    "3294384190": ("MONSTR #0650", "QmTpyjd85heHUtXZd1VqqPduFXmcMC9amC9hUTMpZTXN6L"),
    "3294384354": ("MONSTR #0652", "QmRBz6s1g4HGrLFU7GAWF3ax3yTcf8Xe3oqBs546A4i5u8"),
    "3294384670": ("MONSTR #0655", "QmZdzqQvefLF832HFHSmEPQTbG9Ea2vYrfUTCGTrW3GqAA"),
    "3294384728": ("MONSTR #0656", "QmSWyUvsdT5bLzmucmBdshrRANovFvh6r97Fib6m6J5nuM"),
    "3294384881": ("MONSTR #0657", "QmTdACu6yKP5XN42D8cMbUdLPcm3iv3g46RF4q4rPxFRvi"),
    "3294385018": ("MONSTR #0659", "QmfUbgGA9JYbCaVXasyb3PkZyYszaBvPAYptjhgEcFrtnY"),
    "3294385282": ("MONSTR #0662", "QmbVZAxGA3zpbJ4Jw8WkrQVdtT1QL1QZmUP3nwdVrw74P1"),
    "3294385566": ("MONSTR #0664", "QmZU4mVUeVwAJGQpzXvH54z1wQKWDWuxihtV5zWiqNr5Tt"),
    "3294385967": ("MONSTR #0668", "QmQf4TpoZ4994Tavsc3qNBAj721PDEDCr7pWeh1SUjUk9e"),
    "3294386312": ("MONSTR #0670", "QmeGMCWDAjhX99eh5a7CuFBp5EYg9hVhRpkrxJt4TEmF3R"),
    "3294386469": ("MONSTR #0672", "Qme6F29jYW8rFneeBuJrFdiHkfJwMa1PRFEt9ZtSC8px2M"),
    "3294386577": ("MONSTR #0673", "QmSVoJdStPdujzJREap7X97dxDDf9Yf3JgPQrFWMws4WMX"),
    "3294386669": ("MONSTR #0674", "QmPyb8Jm91T3J4dWv3Rch5VE34U3KgnKy4YMW2cJgHb5PB"),
    "3294386881": ("MONSTR #0677", "QmauSmgvMVGS78J2Bbp6TxxUkhPxAptUet9nA1VabkRGqG"),
    "3294387060": ("MONSTR #0679", "QmdJbePjxFsA4rCQJgViEX5qELmtdXGCuo8tVsuapobix9"),
    "3294387254": ("MONSTR #0681", "QmfABzfvRYCiLT7xjGVcgo8oSZEPrxrB5f9CiZNnFZ23mX"),
    "3294387333": ("MONSTR #0682", "QmaoV1DvicubzyT23TXmPELNrLAbMpKMnzaFRjnL4UX4Ch"),
    "3294387485": ("MONSTR #0683", "QmfNSEtP1KeLVS1BctmKME47KAtvrFUpUAZHkYfVtgc7Zs"),
    "3294387652": ("MONSTR #0685", "QmYpXok47mrZJ4c9Sqf6VVmJDsSTGvGuSdWcyAkv9tMXFh"),
    "3294387976": ("MONSTR #0687", "QmdcXUMA39DtTSK69pv6ogtByP6TYQjuoJSBPoF2gs2Ayq"),
    "3294388181": ("MONSTR #0690", "QmYn2EmrR93ciSZCkcGWMn24eCnjkcAAjr4js8Udvet38q"),
    "3294388211": ("MONSTR #0691", "QmR5i1DSwHebWosn18VZDmUS3b29HBs5wJJJheeNyKXiq7"),
    "3294388436": ("MONSTR #0693", "QmeDNxyUjtVCYFHJvN24UPTi7cs21MfuMv82RJpbsk384q"),
    "3294388614": ("MONSTR #0695", "QmQfVfWcFnkmgGYXS3iwLFxyzbzUuxWaBPAmxsjDMqdSLL"),
    "3294388804": ("MONSTR #0697", "QmXPd16TRpk4hpA1d4HdoNm1Aj8nTAtR67CZL7PUQ5RrdC"),
    "3294388900": ("MONSTR #0698", "QmTm3SQQjynGmWthQsKegkHL8AkcnfKw3cBRaB89HpeDHM"),
    "3294389021": ("MONSTR #0699", "QmbYhzMJGdsF8Cyc3aNsFURE3dv2ZjPaUobWWa6rKSC4K8"),
    "3294389115": ("MONSTR #0700", "QmZFqeeosRyXYGNTGnhTAbtidqh7PrhFjgW9dqf4vN67pw"),
    "3294389319": ("MONSTR #0702", "QmeqTUc8tp6iwHsPNTppyVoJ1Z7z9EKZPTdVxCFYL72uw6"),
    "3294393692": ("MONSTR #0707", "QmPJEeJ62RXe6L8j7S4Pnhg8k8us4LddroyFiBcUW8sZtQ"),
    "3294393957": ("MONSTR #0709", "QmZcvtJnQKhFqFdsPqtUZst9rR5sDmsX9c25GVetFewuJS"),
    "3294394221": ("MONSTR #0710", "QmNmrVw6WuereMS6KMwsdBtpLG39dg3xrLWMBi7uPLbVF1"),
    "3294394959": ("MONSTR #0713", "QmYZmpZtgGj3cRnyQvA6ALNr5q76eH5N9JsuZqfANkAJtW"),
    "3294395201": ("MONSTR #0714", "QmYq9VNcn3WxPfTPw7v9KLbp14vPcokdMTmMKyRyWeqc8q"),
    "3294395457": ("MONSTR #0716", "QmWisPdDxVL8CLq1PaxroBfMCVBXnfWdA9qZhHppsGbB6Q"),
    "3294395555": ("MONSTR #0717", "QmU2dAZKNqbbTXzKEh1u4Avx8M5P6rDaDzQqwutcpJ2iK1"),
    "3294395840": ("MONSTR #0720", "QmeQR45Fh5XKJX4z2YikcEnvv9VuEfZVKZ6HKp3pWb977b"),
    "3294396022": ("MONSTR #0721", "QmUXrqbFpbYEN88KwtMuvbZDNTc6DnRE8whYbsKcijpvvQ"),
    "3294396209": ("MONSTR #0722", "QmPnKdCbRdNhUQ41EibqpzdzNytfAjuT9CU6ZW6Eirp2UL"),
    "3294396324": ("MONSTR #0723", "QmV1EstTy8XiCEii2sEDCAsKUzW9UyJt7d24qqmibZMkdS"),
    "3294396616": ("MONSTR #0725", "QmaivvqvrjDff8HqXQwxo6SHbDzuXY9rHbVNZCKi3XLhCM"),
    "3294397145": ("MONSTR #0729", "QmbJ62sCbZh54SC9p3RR8vZ1oJp47mszcGrVjUYXPCJCvy"),
    "3294397667": ("MONSTR #0730", "QmUFqzkMFDrdbcAWWarXp8kAMsV2U4QuWdDLbLRmSdf4kq"),
    "3294398349": ("MONSTR #0732", "Qmch4qSRoCGFVBz8YN26WmN7iZLUAsn4tr97hWJ3Y8hGCU"),
    "3294398632": ("MONSTR #0733", "QmTt18uUX8z1CA1Lw79NDnarZBPpxLUahJyFuqqYKhgNeM"),
    "3294399938": ("MONSTR #0735", "QmYE5JGi8aAQwcQnCjdXwv7zDeVRfSHa6HPCyKMGHAr8jD"),
    "3294400321": ("MONSTR #0736", "QmawFQbFrdrgKtCauZbHTjDUaequTVsqKYKUtLNNk8jh2S"),
    "3294401452": ("MONSTR #0739", "QmcwDhRSDqU6CCJrQYWpNJNZznZ6yFXcaootZ2eokpS2rh"),
    "3294402437": ("MONSTR #0742", "QmZeDouhkp6f8tG5hvVroiHTHVCBbaNNKKEFdZ7BU8sYu7"),
    "3294402803": ("MONSTR #0743", "QmYR79GhCDbAj3n2LPfUcxgzEogWLX8bdCXL7NyGhbdvtC"),
    "3294403333": ("MONSTR #0745", "Qmbqh8qWRRgpzinz3w4aAPVhzV7WoAzi1dd3j6F15GXc8R"),
    "3294404001": ("MONSTR #0748", "QmTEpCxYuHkh9egWBnqmCGmEzREZYPPmMGPmhTQ8uGVx4p"),
    "3294405359": ("MONSTR #0753", "Qmasycd85aZJqWrRDz4ZYrV7h3F3mSwuNxaDFCQVKHhT5U"),
    "3294405762": ("MONSTR #0754", "QmYXnzSBdZubTM7WTDvmz8rgbmkXVQW53tQjThTL65yEc1"),
    "3294406600": ("MONSTR #0756", "QmbwpNAy1vGTpvu2xCBPcguRkCqhCX4Jz5D5aDxgixEqNR"),
    "3294407087": ("MONSTR #0758", "QmfH7fHbpQvmR7oMHt7pqcSVYtZFUQ4qs2ewkw5rMsfjPF"),
    "3294407799": ("MONSTR #0760", "QmfVUmg2gpmM6YJTgG3DNHQPxyH8dgbt7c6HazB4gBNHHW"),
    "3294407970": ("MONSTR #0761", "QmZgVGmrXJq6pjqMv6vjHm79pXs8d3RUJVSEXPWoBfGEaw"),
    "3294408164": ("MONSTR #0762", "QmQxa9AHhmDPkbjJmeZZqyHiv6HxofCoSjtUyPFuRK2QSM"),
    "3294408937": ("MONSTR #0764", "QmZGdyrNWPgu7KKPScCx7pzk6kV41XtviPqaa4aKMZdZPb"),
    "3294409446": ("MONSTR #0765", "Qmcs2mjS14Ag5dSJ9EpDuvye18vxiZ3pTXEdQz6RzHPVf2"),
    "3294410513": ("MONSTR #0767", "QmXKMX6cbPHCjrkN8zxng9WeMkAoB8nK9Tb6Big3u9DHsm"),
    "3294411383": ("MONSTR #0770", "QmPU7LF8bRse1erXMxrcaER96Lr2XyppAdLnCBwupV1yDi"),
    "3294412093": ("MONSTR #0773", "QmZaSwLVU1PZS4sdrocRmgk1Eyx5F9U7bKV755w9tk7MRn"),
    "3294413177": ("MONSTR #0776", "QmaybyU2n1s2EbbtBX94Ah11LUch5wmdVdSMW8fcJ2XsiL"),
    "3294413339": ("MONSTR #0777", "QmYgyyV13X65YrxaqBVFxGmMKvW5EwNv3V8ugTLAa4MWmj"),
    "3294414289": ("MONSTR #0780", "QmcqCV8pkvdGDujH642nn3G9LYgnmemMsPnH9Tm6kCxZ7A"),
    "3294414794": ("MONSTR #0783", "Qmb8oHu8gXvue5Q4jTuBurd5sf2mUNF5fTgGbwNV2fexq2"),
    "3294415511": ("MONSTR #0785", "QmVrHjRtwyATH1RwiihGpigJbWSi5wYMK43uzpNHge2rcW"),
    "3294415789": ("MONSTR #0786", "QmRyvqtRKzzg3oj7HnRmP5wZcEFfNWY8m9TEuSnzxsPNbf"),
    "3294416741": ("MONSTR #0789", "Qmeh4XJd8AtPR3U81HMZxZag9eod9G4agj8uo217Pxt4iG"),
    "3294417434": ("MONSTR #0790", "QmPutUEu6boYkX21vpbNpib6Fn9SiJZjmoGhDhFQTWwMLw"),
    "3294418048": ("MONSTR #0795", "QmX3ybGmzrmGyQzoRSeTh6MpsPnCCnMjrxG3qHczVAoKa6"),
    "3294418489": ("MONSTR #0796", "QmbKHxro4zy9V7xkqs9NEE89uG6dmVJBz7uv3Wdi7ynmYu"),
    "3294418719": ("MONSTR #0797", "QmTk2aRQqP7nnwYePiNzThtXUmJhWEsY6oswiY17duDoLQ"),
    "3294419123": ("MONSTR #0799", "QmVQYv18KnFwYAjM9Q1rN1NWjtfUHahkUrzGNAHf2troLj"),
    "3294419287": ("MONSTR #0800", "QmVEXyAhcaX2mZLqyD8T5N8Av7voypHnXBkSHAuYbe9eFv"),
    "3294420055": ("MONSTR #0803", "QmPhvzFuidLq95j53Todq2iSmxdB6AsKzvXqfshDTguLcB"),
    "3294420695": ("MONSTR #0804", "Qmcia7KeXVVZbKc6gME8JA1xH1c8N16TiP5rd6LE3Zf855"),
    "3294420913": ("MONSTR #0805", "QmVi5Wf7nG74fZxPxnSM3MgUiyJqH2xm1LVmsN2bjmnQr5"),
    "3294421459": ("MONSTR #0806", "QmRdwyoJLeXFomMyxqnfHyrpL3SE9irKaaparxQPBA2cCQ"),
    "3294421713": ("MONSTR #0807", "QmYLQEqYmQQQg9yrfkxhBijApmUnazGzbc68p33LQ5cKbE"),
    "3294422207": ("MONSTR #0808", "QmX79VTy8VnhGVreSKA5dSiLB7wvsdoUxFFkFHwgVsTzBJ"),
    "3294422483": ("MONSTR #0809", "QmcRJQHjM3x3fHCn4C7RwRrn5ZERmXZBdnMBFahPHuPQig"),
    "3294422857": ("MONSTR #0811", "QmcLN2VhvMDmYiqxZisJNYTfZ8GpLCaveKkLJxoV1iJGJJ"),
    "3294424175": ("MONSTR #0815", "QmVnYyGusLvcHbSCqiRc896NFVL1RXm3zmeM7YoUY6vQrR"),
    "3294425067": ("MONSTR #0818", "QmeYCbN7TydqVspjEyffXU5iy61jXg6mJDFF2UPpTkzpbd"),
    "3294425305": ("MONSTR #0819", "Qmdp88ZapTyom8YcNsAozfxf4T3MajMkfF4wcuL2e9cCKP"),
    "3294425980": ("MONSTR #0822", "QmQFVZbifYwxxqyxunV9tnMGFAJk5boYFwrRzE1F2SjFfY"),
    "3294728106": ("MONSTR #0825", "QmcJwGeQmeBxMJCvkYSByGXaPeWttVXYPn8Re113YWgSc7"),
    "3294728339": ("MONSTR #0826", "Qma5KTPNa3kq4n2vFGaAAj4Z4KNLmiTkqx34nFMgEU8HZF"),
    "3294728491": ("MONSTR #0827", "QmPHoWyiAE2EtPWYxPpijCrsJPs1xkKBWk3hY8VDe8TovY"),
    "3294728554": ("MONSTR #0828", "QmdauRHXgu3nZzvcz2qZdSUvGmpHEGU4Xp1HVXrzEyprB2"),
    "3294728608": ("MONSTR #0829", "QmUJdatdMaJgB9A127BXQ6v1XQdRSjUJTYSir1zjW4M4Cv"),
    "3294728733": ("MONSTR #0830", "QmWYiXUu8qYbLdicmMSeUZsz5RUBs2zfnwSCyQkKRMKkkG"),
    "3294729065": ("MONSTR #0832", "QmXd4LaoV4HuyhjZotVUBebwXNhAqqHRTg258bP9gfZFvo"),
    "3294729109": ("MONSTR #0833", "QmWYFQe2R8gukKzPPKU2bguwUYen5nokPHxY4wZEh9rg1m"),
    "3294729233": ("MONSTR #0834", "QmaiJUFo7etJnanXAMTegP6LW5byt7xt7kYpR6gg2F671u"),
    "3294729313": ("MONSTR #0835", "QmeDg83Nqteq1znxdotTCtdbwDfPFL5F5AMn3te8fc42vv"),
    "3294729520": ("MONSTR #0836", "QmeV7JkYLgFg2tLQvAzrzX11NUL9C6dSecdHpnPi88weHV"),
    "3294729592": ("MONSTR #0837", "QmT8K7rrk6QjvpCyLyrWrg2pvRzctVhoQZyh9W4X29Jj65"),
    "3294729655": ("MONSTR #0838", "QmXmyZLr8vZr1U15thUPJGf6z9EXXAoH6dUZxLtrPoDKNV"),
    "3294730155": ("MONSTR #0840", "QmRtdmKpYYxojjSULLFhj5FDUxjdNByHseXzhNWLuCCdcn"),
    "3294730293": ("MONSTR #0841", "QmbAEW8PBjmcCgX9F24pg4FtALpYcYb17Kcm9F7w2crQFz"),
    "3294730522": ("MONSTR #0842", "QmYn6zP792mN6ff5AsigqghorQ7fquRZb2sVWKN5at6StQ"),
    "3294730635": ("MONSTR #0843", "QmRxRTUwC6dydhCVwXbjKM8aLYdnFy6YYW4vLkut4Qttdh"),
    "3294730744": ("MONSTR #0844", "QmP6JKFj9wkr5CM6iH9HVodErdUVa42N1Tmdp7yNqo6D47"),
    "3294730833": ("MONSTR #0845", "QmPkjffiqZL9Q2QStjvBRvFHzVDhfYWZUcPDL5x55k6jmB"),
    "3294730918": ("MONSTR #0846", "QmWKKW8Ge2CfWi9HZFX3UCsCjoZM9EKHSQrfkwaCwheESY"),
    "3294731259": ("MONSTR #0847", "QmeqhyunnxcBDUeJmHkeV2Re9Da5k7T15yy8ha9gAimuau"),
    "3294731425": ("MONSTR #0848", "QmZrLvaNYaABjJt3HwHt1W8DtMerR5WZmJUSDP76YBS2q3"),
    "3294731719": ("MONSTR #0850", "QmdTGHFHpHFLdwxQv8fqUYjE3dprYj6tfaKzz3khE11mEz"),
    "3294731826": ("MONSTR #0851", "QmcczyYDKupnqckrsXFWS8Bf7BFHuVPr2rPtGkGUznh9tk"),
    "3294732128": ("MONSTR #0852", "QmP5pHjJQMTgf7XiTvu6q3GLB7e3cgBRLwyPaQsgLiGjV8"),
    "3294732384": ("MONSTR #0853", "QmYH6ShVnuF7D6Vvwcjb18swcP48rovAGnuTE7WiVV3rnq"),
    "3294732996": ("MONSTR #0854", "Qmd64MmPCr26LXK3hVryWj9V99KsLsp8MjU6mDEBoiQs6b"),
    "3294733282": ("MONSTR #0855", "Qmc5bSdjSm3migsBA2YQPppgdCD8XVHNaRbzaGZK8SsLga"),
    "3294733351": ("MONSTR #0856", "QmczJ5qK4aJd3h4pSjG3NpzcZ78WMCUQTdjLYy9BXLatkD"),
    "3294733521": ("MONSTR #0857", "QmVzmvmAkDbfrdhs9XVtZXvwdHQNK3JXT9Ta8JSriC5Nop"),
    "3294733663": ("MONSTR #0858", "QmexwZamjgtXpG1SMDdvE6FntqpYd5AEdo7ioDVcWcjmKA"),
    "3294733777": ("MONSTR #0859", "QmPnAJidKCY6EHbLKvTm3f8qZDLtgzBr8ppKZxZjXv8VZ1"),
    "3294733913": ("MONSTR #0860", "QmU17WWjVijYWdoFFFeMxvCHaxYYFG5vsannUSk4nZjnjG"),
    "3294734362": ("MONSTR #0863", "QmVEK64111z3ckPNdfTd8BiNDKuAsw85VwjydJQng1PNVd"),
    "3294734065": ("MONSTR #0861", "QmSBeUiPNGpa7VieLf2tKTtnpgzUN7VzouH9csk91NK6mp"),
    "3294734315": ("MONSTR #0862", "QmWVBzvBcABuXZZLp1JVaqLeMuFVXSF6DsbiGKwwc5FPv9"),
    "3294734426": ("MONSTR #0864", "QmdNWchjdVqTBLfyvdLYKJVybRJZndZaPvKoJWkDwPYwnW"),
    "3294734512": ("MONSTR #0865", "QmWNVKJWpwK1PimsFZxfDNEo4tCD5hdkzkqaCJ3ffgqkBQ"),
    "3294734723": ("MONSTR #0866", "QmcZB1FKjbn46pMPUJd3tFvYqpm6yDTqfxrUseDYc9QPio"),
    "3294734970": ("MONSTR #0868", "QmTXXkeLRFGxc5EJbbGSAYHAsewu8tmXrPNKNbN1wgo2sC"),
    "3294735076": ("MONSTR #0869", "QmSsJCxwqiZA2U4X5KQLeV2bkp4Z1wrMxXSaawf6rmkAT5"),
    "3294735575": ("MONSTR #0873", "QmaL1QsvzrPNuh5zUuT5sDtYk72GFGnWaWfa9CcGzdmCtY"),
    "3294735799": ("MONSTR #0875", "QmUgxoUDBDqmfG3Ys2oKdyHVvtspHAqzMwdT68ewcuXKBN"),
    "3294735674": ("MONSTR #0874", "Qmcx6cwSu64A954ozRZcb7sCuJro9W4GftXh5rZSyF4P8D"),
    "3294735953": ("MONSTR #0876", "Qmbb9aBJisRNY6FDNWnUkrwuAD4S7piMfRxf4xhzoNLdPZ"),
    "3294736065": ("MONSTR #0877", "QmR1DHKZk4atYXPcqwNBgQqa38TReuQFQkXGjBUBCJWjzh"),
    "3294736317": ("MONSTR #0879", "QmW4Lvp4KgrC9GuafepbvNBqtCcRFE5WMMuhK17z7DiGn6"),
    "3294736360": ("MONSTR #0880", "QmchHDQvJCHmjKHaQ5KiuLuC1VQD5JN7eeRdDSBQ5b1zfd"),
    "3294736574": ("MONSTR #0881", "QmT6BiDTNwbhkt1mYhWXfpEBMkspiUYuUzjS81hAos7SEi"),
    "3294736663": ("MONSTR #0882", "QmUu3Co4AowsGRSztnMdSviP29gi6j2Hmfqo9tdb2Kse5S"),
    "3294736902": ("MONSTR #0883", "QmdhQM4bu91nKiQJannpGYJTazuMKRBC6WxALFTNDzTDmy"),
    "3294737081": ("MONSTR #0884", "QmTy5FH3G7Nk1hgP5P2DsDHQ1ozSDzTFe9yCQw5zinLgDB"),
    "3294737127": ("MONSTR #0885", "QmRXkwACnzqPhJd1wr8kNvKpjHfY4Rvfya6mv2ddHGt9TQ"),
    "3294737199": ("MONSTR #0886", "QmTrYMXMHNP68q3g3wZSGNBTo4uMZGc9Ko5JXS7PmHDPXa"),
    "3294737281": ("MONSTR #0887", "QmPTeQcBAASgybcK81GQZyVhjQCR1Xq6SupohRtApVVZKB"),
    "3294737354": ("MONSTR #0888", "QmfY62vSVZdVXTDddWBVfJG1Ck2TKBvNSatkyeskDf5vVe"),
    "3294737520": ("MONSTR #0889", "QmW8qCmUVTUr2Bta5RJyTtCgBuNvkzoirXcCoSfS9S2GQQ"),
    "3294737670": ("MONSTR #0890", "QmXp3kizu2DgPohrMXWjMPCEj25hHzHs1ZkwVPLw3gW757"),
    "3294737727": ("MONSTR #0891", "QmQ2gcgW4pgGZ7fCmoX1KDu7wabeTC5t6YsZ9UdKoGu2xm"),
    "3294737854": ("MONSTR #0892", "QmYwgjo3myLWe3ypdD4dgaNYeBpRDmnAoo5so64gxWNq33"),
    "3294738274": ("MONSTR #0893", "QmRWXUvZ5KHWvHTXqxRFaAzG92hpGbXprEEquA3dYhjrEa"),
    "3294738763": ("MONSTR #0898", "QmR5Lex2bQabWNPWa4XZaw6RG7oJU6ncALcXe1AVoLWjDB"),
    "3294755717": ("MONSTR #0899", "QmX757Rsiaq5fhxMnnU15a4GqNayFF9XarqN9BXZvgf2kW"),
    "3294756011": ("MONSTR #0900", "QmYJLuCJSghLZjT9ZSzxV6AsT3AywuREhxN33xkhU7urMp"),
    "3294756216": ("MONSTR #0902", "Qme9yedfVwfcFz395urq4WGgsLtp3MzBY2JpJK7eHGoHxw"),
    "3294756280": ("MONSTR #0903", "QmUcGBHHMDLgwFMSRBQfyUCLdh3QQApkiTvhoukTBPsKJp"),
    "3294756339": ("MONSTR #0904", "QmfFsyJpMHFDuhceKEosAWNJ2xtgmgvhqY7Qunyp4WWnpM"),
    "3294756451": ("MONSTR #0905", "QmP1oUpZVLoVQEvYQKWChfESDcFfidik5kvgWQGJk3zFAR"),
    "3294756532": ("MONSTR #0906", "QmcJ9T7ERk3GzNNQiSaqKdNMD9chGaAVrL3JLBfvBEo4nH"),
    "3294756644": ("MONSTR #0907", "QmVLBoPpxhjLm7Fum5Gj4MkUwdmbRP6F9wxNhmPkTJkqtH"),
    "3294756769": ("MONSTR #0908", "QmQfYXbQXt6nCPvo1g5rFmjTBqfmMtLRJ7whFPf5aDJib6"),
    "3294756867": ("MONSTR #0909", "Qma3DeiPBu3tuCL3v1a6A3CPHmvdwgCv5mJFkwehvksR4z"),
    "3294757040": ("MONSTR #0910", "QmZ8nNFeEuJPuNpMqkcnvGvh56T54HDyZpYvZmq9E7FSF6"),
    "3294757167": ("MONSTR #0911", "QmQYZ1tw3FR8FApXjv6HcyiczLbCqCAsoy9FMvecNvkJQk"),
    "3294757253": ("MONSTR #0912", "Qmf45tDKSyDqx8TNmuJccwynom29sntzoSeEw1anj6yt4q"),
    "3294757388": ("MONSTR #0913", "QmZ7snfyuC2mWQQ1eZ4S1zfmU52wwqPp5ocDoTxX9X4FBE"),
    "3294757517": ("MONSTR #0914", "QmaotPbCyY1DvCqEYNV9bi8Z6kyvyVdgmhmem2o3VKZeRC"),
    "3294757593": ("MONSTR #0915", "QmbtWdChU1hMfyyhMCzPff17rqMafspDzvLLjQcZxpPfmc"),
    "3294757675": ("MONSTR #0916", "QmVm4yTcarekqpXmAciSTyfatUE6JV9nCKWckvcDhGWDMT"),
    "3294757886": ("MONSTR #0917", "QmXEvguLgjLzrSnc4ba3h1Rr1D9Z8D7tjjk1QQaKREfEMJ"),
    "3294757958": ("MONSTR #0918", "QmQmvPaR1GJc5cMXdCCPvACRHtQYiTdoSfdyQEMP6Z4keg"),
    "3294758209": ("MONSTR #0920", "Qmb5FqXzruD6Hf1dWmv8LjVoAcP1iPK3orwGpFX1jDPWJU"),
    "3294758335": ("MONSTR #0921", "QmQjxZq9AStiRApWVHfMbrhH2a9CM3rKd5bH4fuvEr31Yh"),
    "3294758407": ("MONSTR #0922", "QmTU1NJQVQFvGYS3h5FxHBhLa8BbFBnsbwfbGHtNCqVUC2"),
    "3294758511": ("MONSTR #0923", "QmQyHoCwN5Tcoyd3f8RjzSaDu2x1dZ3SgruNZY4kGLYy8e"),
    "3294758585": ("MONSTR #0924", "QmZFeSZhw2k1s665qGnFCp3UtaoafJQQTvLxh2GiFrVVvH"),
    "3294758699": ("MONSTR #0925", "QmU8dQaUisE3apmT6azUBFiSatP2isxbTWT2bm7kmnAkYY"),
    "3294758837": ("MONSTR #0926", "QmQAiPN6yfwXYvxgM1JHEDGYhAusmxuq2qAKigHJbk85Fk"),
    "3294758940": ("MONSTR #0927", "QmbVSRdzeLGc83qmjckVtbfhtXNM7dt7TA7CmUNA6RMF4u"),
    "3294759070": ("MONSTR #0928", "QmRp1HQCyWFREvji4sRwDp1FAuPAt4AvcVtanKmvRKuPP3"),
    "3294759191": ("MONSTR #0929", "QmazXkeowLHNTAR6oKM8kto9i18oY3uCjsierresUWBGv9"),
    "3294759321": ("MONSTR #0930", "QmcfERkjMc3f5P7tapGDnkMWjxJShkcNXcpwA45zCNqRfg"),
    "3294759383": ("MONSTR #0931", "QmPmCRxMTUztx6h85iHFBViBSxpCiTDBHJCMmGXwcC77sX"),
    "3294759466": ("MONSTR #0932", "QmbDxXDn4KVKLP5W54NCEtq56Woiho2nmrYUUBHipfeFRu"),
    "3294759651": ("MONSTR #0933", "QmNNXachFJeTYeBvtixD6GiLmxZSW4w6XoP41BZgAR2WFn"),
    "3294760030": ("MONSTR #0934", "QmbRBHXGjkVVWZJ6Qm4ykzdTzrPbqkDNvp5aXjnWrEKnXS"),
    "3294760238": ("MONSTR #0935", "QmXNKFzEbC1t7xMRg3cRCmZEfb3Tq4oR9ZrC8eYuZv12p5"),
    "3294760377": ("MONSTR #0936", "QmXvv9Td7kavBhkHZoLFyCtkhFFz54JLk6n7qk5k6mvXi6"),
    "3294760531": ("MONSTR #0938", "QmPDRkNcPRbszQXzykG7Vkndzd8Lw9buTymGZ5CUFCthmW"),
    "3294760921": ("MONSTR #0939", "QmZZ9w2jYzzBNL35THyEFpMJSGPADdwtUT51nyU4KMfKbm"),
    "3294761099": ("MONSTR #0940", "QmRs6t8DVEuY2kiUVtcMUZ4wv9kfDPeeejs2iPfbZTJuCh"),
    "3294761492": ("MONSTR #0941", "QmWHBia9aPGzb9x2vqFqxWuCDrRKSCXUvw6yz2rqh8JNV5"),
    "3294761587": ("MONSTR #0942", "QmVDLQr279BmGKAaSn7N8mgiW5XFoYDqoSJFg1aGFkGEB9"),
    "3294761725": ("MONSTR #0943", "QmbpS6Xu4MnoFsFE9s5BHsr3gYYHQEBo3fgqH64mzVspCB"),
    "3294761812": ("MONSTR #0944", "QmTDezsGkpjNhrPxiLzVhoPRsMG9qMM4wheknN5DyxyJXX"),
    "3294761937": ("MONSTR #0945", "QmaSqwRQ92wXWKbJ6csuNNLicgPraRDC15aHAW5tc3qWvg"),
    "3294762021": ("MONSTR #0946", "QmTmotRKAyT1zpdycEc54GWz9uvLVZBmsWBKe7L12Ho68M"),
    "3294762109": ("MONSTR #0947", "QmRanfer1DAgKFiH5dngCNKMse5dBwpU6Xd2cUZuiPpX42"),
    "3294762198": ("MONSTR #0948", "QmbyEo6xWKkpEwjRpg7i3jsHyDwRm3ypgHqgrdZg7Hqboa"),
    "3294762593": ("MONSTR #0949", "QmTwdmXPWkaZXbQ1gf3DiqRjkox8sogBd3QhqHHQy6n3FS"),
    "3294762735": ("MONSTR #0950", "QmYudd27veRyYNq6bxhLhE8ViTDSsPLJkF9zzB5J9C8oQB"),
    "3294763023": ("MONSTR #0951", "QmSxU24LfTpxD9WYxhvYgNbY4xpYjjGvZAiXQ3EUBiGwDN"),
    "3294763156": ("MONSTR #0952", "QmREgEmgpJbYVdjr91PBpfhNmiYxQWT7ekTfbgARyXtmfv"),
    "3294763350": ("MONSTR #0953", "QmPsNvpNjLT9qKZQXcwcHUWbAoYNgNtykPAqY5AcS8wpGU"),
    "3294763467": ("MONSTR #0954", "QmQEP13LRUiyJHwaE5Nmc121mPxjWB3LNBpPFKukDZQSi8"),
    "3294763602": ("MONSTR #0955", "QmNYKGS7VPH9DwantaRgWTkKZFDGc32ffKNThDxs8XKWmS"),
    "3294763740": ("MONSTR #0956", "QmVZ5PmcMQAj1bdm6ae9H5gx1EUYnZaKmwqdLwCUsE5Ax7"),
    "3294763923": ("MONSTR #0957", "QmVr7UoH8d9RKy5TDHfm27K6ZWFqzWM2ziNwX5moDtuxCu"),
    "3294764298": ("MONSTR #0958", "QmY1g7GFXydLMfxDBZZz4vHcVrw33Z2Jc2LxqSKz6gCzAe"),
    "3294764542": ("MONSTR #0959", "QmRuVLDuAQmfrk123t7U45XzXZxopi2bHQbtAH4o4Ctfz1"),
    "3294764651": ("MONSTR #0960", "QmebErJfXkiX9gCeGg8jhxAfToANPx4kxEA9fMk5zzpu4T"),
    "3294764800": ("MONSTR #0961", "QmU69kXfRWHv7xCo1TEDKYFppQ87mDsy5tgVjKcodjQ81B"),
    "3294765075": ("MONSTR #0962", "QmNvrKVT5DvWHJabpVGcrCLA5n7C91Q5t1hTVgLVxskyMu"),
    "3294765283": ("MONSTR #0963", "QmXbAT35673cBy2hsnVqcAMuoaJ2Rbgd3PGYU27Kv4iSBo"),
    "3294765587": ("MONSTR #0964", "QmeLn1NuK8Xno6SFWapfzs8qkbQzbXvSf6o7enZAJ5w7iq"),
    "3294765765": ("MONSTR #0965", "QmPDAGCxeBS7b23uwvdTZ6cxqDBBpDxuKMdniW1omx6MXN"),
    "3294765914": ("MONSTR #0966", "QmaV2QDE7TNhNehy8HW6ygdib6ZovRzSFUUf6CKBS2SGBx"),
    "3294766454": ("MONSTR #0967", "QmV6XXgxTnYfkncD8z8nTmNjLiB7T6udqXiT5761sSD5Vn"),
    "3294766584": ("MONSTR #0968", "QmNqYLvobRVoFE6eoHRnBrTqVv2kFMwJbLBbN5X1PrHwW2"),
    "3294766718": ("MONSTR #0969", "QmVtaKtiaAwYERFXquhLnjqQMuJswqHJe9YSLC9qA2G3QD"),
    "3294766783": ("MONSTR #0970", "QmacTyFPSGeMXAzYqE3sJnZDtLCeuYvUPc3sF2SZEg99Ey"),
    "3294767188": ("MONSTR #0971", "QmZjVCeKfbsvJ7jRDYRd8whWsfpJLQ4x1ZbrhG9wKwcNPm"),
    "3294767314": ("MONSTR #0972", "QmafqDWM5FEFVvXYpus7g65TCMzWzNyjR9bQzTueJ7fhmW"),
    "3294767423": ("MONSTR #0973", "QmW7f3HrP5zJxJwJLAUVQ3X6CdnZjyg8CK92bDor5gCFNL"),
    "3294767541": ("MONSTR #0974", "QmbMcKEjif2VoYqoUj1pehGm7d4RxvZAYdEiP77b1xBUQ8"),
    "3294767746": ("MONSTR #0976", "QmdPTn4FVxEsQvtBqGXLmVjMxtL8RJYvckHEZGBEd6CWd7"),
    "3294767782": ("MONSTR #0977", "QmV3LUXw3D4ysNX4pL5AMVWMbzHRtfbHjK4pEPvbDB7C7Q"),
    "3294767916": ("MONSTR #0978", "QmSrqwVz81T2BxZVW1mQ1u8H1RzZ5rM5JgWD77P3FfEnm8"),
    "3294768056": ("MONSTR #0979", "QmeEa9q6Ukou2UFhVDsm51DTyziZS9Mu97Ystn74DL6kYj"),
    "3294768117": ("MONSTR #0980", "QmZd9vsbGKh8EJxDWKk9sPmfAUepvWrVNbaciHygnUsH8Z"),
    "3294768183": ("MONSTR #0981", "QmSS2iC3Jf4eruCUBSXT6UGrsGxSmhx81EH6ur89GDF1VD"),
    "3294768362": ("MONSTR #0982", "QmUDApb6fafSvyFpUjxM1bjfthV5NaaQJery5D9vbnHYUo"),
    "3294768495": ("MONSTR #0983", "QmSFX4Knz6wUx9SimTk7JjJyWoXzeb6p1MzuxNMPh8JEn1"),
    "3294768671": ("MONSTR #0984", "QmabbjhL1kkfEGsANr3fxuEoZQ7UQn6nZeb42ZhwrqJ9Jj"),
    "3294768775": ("MONSTR #0985", "QmYb4SkccsPmtPChyEUhZgKvZzz5zBNfzp8V3FFu74Xezy"),
    "3294768956": ("MONSTR #0986", "QmS7Jwd5FRB2HM5QeyJozTF84sYxbkHG7MogKoxAVLCwYm"),
    "3294769184": ("MONSTR #0987", "QmPGM9kbqopYa6qtXtFbKhgZCWTLu8GvjojE39cZKqUU1n"),
    "3294769247": ("MONSTR #0988", "QmXE2hes4advTMEBjHz1PCCmRSdYKxqPiD2srmdRWrBt9M"),
    "3294769310": ("MONSTR #0989", "QmZoqhtWRoN1qt3ExepaNDRXRHHrNS58BCFkeJd4V8u8gh"),
    "3294769561": ("MONSTR #0990", "QmRUfDwoL1E5rwCUnotJjKRtfeVbgxwAhspD8PRhmAzvNu"),
    "3294769631": ("MONSTR #0991", "QmX1VhguqZFB2XmmAK8s46FZSUs4AgjoS4ufCqtcaRFPfn"),
    "3294769660": ("MONSTR #0992", "QmcVJC8nve5QvdPGdWy9uP7tCNvuFAf3HfrJFJqJMzCvAw"),
    "3294769995": ("MONSTR #0995", "Qma9hzxJ7MxrE1538d2sj8ohr3JC14uFdLsQGqhJrnRb6k"),
    "3294770049": ("MONSTR #0996", "QmYTA3skUGMun1GYr6BUX5zzo7XbunQtUbSZP8JAzqnsXo"),
    "3294770133": ("MONSTR #0997", "Qmao91UnnDY3Jbxbamzgj85Mm9Bt3kEdE9NV1nk6NCJSsG"),
    "3294770314": ("MONSTR #0998", "QmW3Rncx4jxwj99y2MKTf2Un97VgbePRj8H4ZgTf1Sv6FV"),
    "3294770657": ("MONSTR #1000", "QmVbwNPV31qixAWdwjoknCkZMwwDrkqNFfWPrc9DxRk7dE"),
    "3294305455": ("MONSTR #0002", "QmdSCpDJYthgsZFjfqQRxXeWehksjrP84CScyoxLZJrJMW"),
    "3294305989": ("MONSTR #0005", "QmZPYh4PG1nNaAyHRU5HnzngApg175ux82d2LaBTjKYw8D"),
    "3294306210": ("MONSTR #0006", "QmWTksJvc2HZesgBkHL3Ud2sqwtBd35JvXyd2ZHGosfJVT"),
    "3294306840": ("MONSTR #0009", "QmP7m3rk8KhgNiLA9ujYNBeddj84PuXyEdooZPUambWNLD"),
    "3294306918": ("MONSTR #0010", "QmekNXPRZwiukHBBBiDcBHJCAUajHa7VTn79JFFNdvw441"),
    "3294307799": ("MONSTR #0015", "QmX8TtQkL6BWEf23Bx1j6T1bH9D87FWbdWytYG2Wdts6LK"),
    "3294307952": ("MONSTR #0016", "QmSV4rRZp2xCesDifGKs63JnmS1jN9A89B2YkpPtBSrbVd"),
    "3294308276": ("MONSTR #0019", "QmfRVYJh9QRvnYryFEvY8EJnD7wA7JHfGeEZF4dpXdCA5g"),
    "3294308492": ("MONSTR #0020", "Qmbq569DYkaaabUeUiqvnf3L7kEmvnD5ewX8oV5RMdcc24"),
    "3294308612": ("MONSTR #0021", "QmX8wUXRxcfxSUz4CsAKFHZ9ZB5g3hkGND2TjxF3vALaPW"),
    "3294309263": ("MONSTR #0025", "QmRFfCexjCe5TsChNeJ5t1dSn8gBJjaEyzKudzzUXis9m6"),
    "3294309400": ("MONSTR #0027", "QmbvZ6e6PERoq5BwXPM35PYvSxedMEjiKkFzwWphd5x6vH"),
    "3294309980": ("MONSTR #0029", "QmSejYT79SWYsCthLfknMHnf4tNhYGJGpZDjjxGGEn52WX"),
    "3294310915": ("MONSTR #0035", "QmZaKUwmYgZqPk93H5iV7ZWVPAn8TjzJ9aK2YQBQmx1kDm"),
    "3294311412": ("MONSTR #0039", "QmfQRPUoV2ZSnfVa5pi2mYQWyg5NGm2YwZm4CEM9FPMy9o"),
    "3294311477": ("MONSTR #0040", "QmTYDnnJoYv82BDhJ2S15N4nrXmDCq3R3ubCibSqM5rn1n"),
    "3294311565": ("MONSTR #0041", "QmWMmwAoThD51BuC3yUrmNLtoZo9pSTgkLU34oLzNXVrnM"),
    "3294311642": ("MONSTR #0042", "QmZuzpxJ1m8hh8KtutwPhvmc4j7x5gWQZSqSTCkqZkHPY6"),
    "3294311741": ("MONSTR #0043", "Qmc9armFdMBgQbtdaQnTBDVZ4HeeMWsKWHnqN3gCzo99en"),
    "3294312019": ("MONSTR #0046", "QmbjcWBKjqnHQp7bm5r2egjD3AiYbVzY8t7NQb9L7NaAdz"),
    "3294312204": ("MONSTR #0048", "QmWCw1UocckaGnEuipj3LU5JtVD6T7sv3uEeWbeUftuh3U"),
    "3294312583": ("MONSTR #0053", "Qme2G1UnjFcvj8FTzqfBv8qWKxwqfdkde6iWtpjFZkFbg5"),
    "3294312893": ("MONSTR #0056", "QmY5GmpwtMkd5368jhhep6JULQ9WN23zNzr3EgoUNXubWj"),
    "3294313049": ("MONSTR #0058", "QmasSv2hwpFSvPdAx9CgWiUhrYrQECYUArWzxvWYuT1cLW"),
    "3294313551": ("MONSTR #0061", "QmXaZd7wmYacerHP1rcrhp4xptcycmsUotWPmZJm7tqhZc"),
    "3294313930": ("MONSTR #0065", "QmSfbEdmXMGBwahn2iCkWh7kpKEQy6qSbTspjq17LLPnNn"),
    "3294314098": ("MONSTR #0067", "Qme7ouSibDum3UdB7TsqQbE8jUr3SGNtnM523VtMNhzmWo"),
    "3294314394": ("MONSTR #0069", "QmemoxUtGJL7cUNRncMNvgiTHy6tuEBYkYZpBTf7Qe2uvF"),
    "3294314620": ("MONSTR #0071", "QmSRp1HdLQ2XrZziaeFVZBghDmaUK8j5zUoggiojaymkGC"),
    "3294315230": ("MONSTR #0078", "QmejnGnXYyzArCxDdmyycexMHctQzAfmzicvcJAMUaQzzK"),
    "3294315385": ("MONSTR #0079", "QmWqSRdSjwaqjgMuwN7zRMiVJCZUac7BFz1T7yU8Y9E19s"),
    "3294315716": ("MONSTR #0082", "QmPaQKfemYkXRCgM8zY4Je22wkLKeDyF8GZcHEFccXUTEh"),
    "3294316029": ("MONSTR #0084", "QmSz3A3YAhuG5UAf2uhkuUqcX1FWCedMEwkTtVpsAnfAaz"),
    "3294316786": ("MONSTR #0091", "QmeUz3h73u3yyigUE5QhMcYzX3YbMTFxL7NEWEvsCt3wgx"),
    "3294317087": ("MONSTR #0094", "Qmf8sWesUBQ7eqUiPHoiyMP55GACsmFAaEvMXbqjwYPhaD"),
    "3294317349": ("MONSTR #0096", "QmaTvJ6X7CJF9m7ZJfp464Jqb2kpt7cfprTdwzoc5echsU"),
    "3294317527": ("MONSTR #0098", "QmTcapzC6XrqwhaNM7y3vustqmF6QE12ika9miGJyv9ddp"),
    "3294318583": ("MONSTR #0104", "QmWdZN1VhQq1kMEprKEUR5BhR7XUVqCUZ6xPm5bbFgS8oN"),
    "3294319552": ("MONSTR #0109", "QmQKJH4MsMVRcH1VMqNY73NmQKgQRhGGQce5myXbxNvi75"),
    "3294319653": ("MONSTR #0110", "QmU1m37jVYY1vTaMgazK6uCbGLZqBApkyBoURw3CBDNy18"),
    "3294319979": ("MONSTR #0111", "QmVbVRTBDMcGfZVycyRRcnDvejFNPP4WxmTEfU2n9KtiBG"),
    "3294320344": ("MONSTR #0114", "QmaoM8PZ4j6pgki6KkTcNQQavFtHB4msELPmDio6zEahe5"),
    "3294320508": ("MONSTR #0116", "QmfPATXcAixWxT9YeU2hYpg91RjNThrCdRMPiagruMhMsG"),
    "3294321114": ("MONSTR #0123", "QmNuwfGwZiNDoKw8xuigvQf83QVBufTqWwVwL1VVxh78p7"),
    "3294321464": ("MONSTR #0127", "QmdeUqEEwxmypuUQ2ugTTrCfzJyf6EeNmLY3uHX3A8kAMx"),
    "3294321842": ("MONSTR #0130", "QmRPBcx7NwuSNHbZ1e7VaCoESYqN9eRvtrZPUj5aLdeG32"),
    "3294322098": ("MONSTR #0133", "QmaA2j1Q1q9Zyj7nue6yRn3UYmQjjCkgst3pEZrP9aNbkd"),
    "3294322551": ("MONSTR #0139", "QmehmcQ34Jg3cKc5sEUhuZnQHFpuodLTSraRmV1H3D7KYR"),
    "3294324214": ("MONSTR #0150", "QmXiCYjcdm1MX3CWtLvzMPVCgVhBed8xSKpG7sfzu1BDqm"),
    "3294325035": ("MONSTR #0156", "QmZEHH5Ffrv54iRk65qZsoU4ZbmogMafzwT4GH9KYw76He"),
    "3294325637": ("MONSTR #0159", "QmUvV4VLScD5qvFQsKNRBnXJjsJXpRLathECiALdTpuBdZ"),
    "3294326105": ("MONSTR #0163", "QmZMHqPqmx7hqBaUGyjw1X9jST57bdTtcLf35yj3rbybL9"),
    "3294326994": ("MONSTR #0170", "QmNnT52VZ5e1hVXrfBUTgVLA1MDBhPb2e8XDYLoGYECUbq"),
    "3294328012": ("MONSTR #0179", "QmaCre1LyMZaV2bkJfgsSBrtYsowh2UnNVmP848kt5vnK9"),
    "3294328541": ("MONSTR #0184", "QmWm56NHDkQd373kmoxuZZNeU7WV1cE7TawrzUCzPJfHAM"),
    "3294328937": ("MONSTR #0186", "QmTyuR7uRv8W5AD7kqAPADzzaBNnPjQE1GRSF5jW5KjJHQ"),
    "3294329468": ("MONSTR #0191", "QmQnhVxXYSXsMun1WpqFUydrwB9bq3oJPFZPAcihmms9Y7"),
    "3294330445": ("MONSTR #0197", "QmfSrPb5789a3brMETbBksi4fDHjpPGBs5PeeAm7hFe3PW"),
    "3294330581": ("MONSTR #0199", "QmYUV9t6jfpojkRgHjfq1ZieMLEV1HY2pchGGTc877CNu7"),
    "3294330847": ("MONSTR #0201", "QmWdtyJpk6HWHLGBCowXyzUJ1ySeSGzu2N3xwPEyTvhzov"),
    "3294331370": ("MONSTR #0206", "QmVcJRaVZ682re46U9ELzD21f4HMNDf9DX5a8LFsTRd2MW"),
    "3294331700": ("MONSTR #0208", "QmXb95L9g1LBD2csAyRuRgeqWALBYu2EwGtz9zuoQTbkcT"),
    "3294331905": ("MONSTR #0210", "QmTukTC1A6GfgXQtuSL4wepjB6HHSLjPDb5fX363KMcvuk"),
    "3294332352": ("MONSTR #0214", "QmUTJGxAbax1sAGnGv7jzLXunPUSDVDNH2j86eLMbG5QXC"),
    "3294332765": ("MONSTR #0217", "QmP12WnRJ3A9w8FZCU9PL5Dfmwg3BYCRmkfuERrfthSm1N"),
    "3294333184": ("MONSTR #0221", "QmXQ51guuzEcBNB9zSuJsStpBk3AgvSrFiuJueSkWC1Jc9"),
    "3294334143": ("MONSTR #0229", "QmUVjFVRgk8xQMCKZQydssfNSy4jkWRv66KqvmiJt2nnND"),
    "3294334953": ("MONSTR #0234", "QmcoDkFhLDRsU1WKQBhB7Y9b921UtK6rryFANSve2LRWGf"),
    "3294336797": ("MONSTR #0237", "QmVn1EbASMPtU3E8nJbrQNJZWLR5AYeBKBY2PoUzoDDNBB"),
    "3294337018": ("MONSTR #0239", "QmSB1N3XjzMwyWVfdNtKuoCgReh75ATNhzwJgvWAnRHjDU"),
    "3294337465": ("MONSTR #0244", "QmY5KCU9BixNMAY8nfCSdSbEKTAKQjbFMBECMVtQvKUzVL"),
    "3294337715": ("MONSTR #0246", "QmQnmgBJCP6YxN5e1xvSHPXsYUt81BVqVhZd1XGgx9UM4K"),
    "3294337974": ("MONSTR #0250", "QmTsL9doq5Md7tV6SCYHRWz9JUWBfsUWVa2s6z4FmCeKRQ"),
    "3294339249": ("MONSTR #0253", "QmbyjzwHFznnAQwETprBnXokNG2stPRp9wwZRpW1RdZPj9"),
    "3294342649": ("MONSTR #0256", "QmcNU5wWrjby43cD8rYPqmc2YpZwATQgsS1XWBbFfbxKz4"),
    "3294342784": ("MONSTR #0257", "QmUXX5pTSpZHfnKuBuWsLWioK3ZaxFc384zbg8iA6diGaW"),
    "3294343383": ("MONSTR #0261", "QmWRgJ61mZd1EqZMUiPcXwm8F2gZJ9gNzQxEQnxgrFx9m5"),
    "3294343863": ("MONSTR #0264", "QmcCEpS63aDgj221rebqKWnGyAeaGhkk7rpm6QWen2DxtE"),
    "3294344166": ("MONSTR #0267", "QmXLehnh2j9imXdt9ThQiwYeVghY3y1XvyA7snaGdTduwP"),
    "3294344431": ("MONSTR #0270", "QmSQF9rXaqNtaaSAxW64bSpPy7vXdcNmc5PHsRSqLMAt8E"),
    "3294344594": ("MONSTR #0271", "Qmbzfha4ntWUx8h6A5a9T2BnFcWeXfRDUca4Ks1MFfaQdu"),
    "3294344765": ("MONSTR #0273", "Qmb4WqabQegBMW8Lh71n2pa183DekS15XbuNZ7gch36ccV"),
    "3294345691": ("MONSTR #0280", "QmP48Pqjp48Td5yZF5NoBQ6oKZNakssqqYQnK1vYa73FCp"),
    "3294345737": ("MONSTR #0281", "QmYDaiZmDN47UgdxLxafofEDLoUaYvVMJLkgwiVcpyZqS1"),
    "3294346345": ("MONSTR #0287", "QmcfT9HSp37kmS2UNUUK3bUHDAsSXBi231poJmRHQcCpWQ"),
    "3294346744": ("MONSTR #0289", "QmSTquvkcsWFiSyJvE8TFePKncp8LQUEWqo5qFZujzs8xc"),
    "3294347371": ("MONSTR #0293", "QmcK7fQSs9vyZ6aH63huQvrKtXRnmNa9X5eodKXwEW9u6H"),
    "3294348538": ("MONSTR #0305", "QmTNGAVfPanQr9bojbJCBa1vDobn2tiSpergv74AFkz19h"),
    "3294348863": ("MONSTR #0308", "QmSz196Yq1MRdURnkDRQ2KEqraSSudwkvy8u9BraiMEaPq"),
    "3294349185": ("MONSTR #0311", "QmeZ3u3LBoJVZWqYtKvdGkD5BfGZfxExpPaHsUPGE4owDv"),
    "3294349338": ("MONSTR #0314", "Qmd4eEkh92ADEQt3RANvf445VGKAD8F4CjDwi9njPvsLgn"),
    "3294349671": ("MONSTR #0317", "QmPJoXWQub4EmtBHyYeBR5pHvJxj2eDF3JS7nLwMTZJNwv"),
    "3294350052": ("MONSTR #0321", "QmWNrXASLCWApqrKeWWni6d9TtujQiycbpjq5pmQGGdtwu"),
    "3294350511": ("MONSTR #0325", "QmSBMmCzABQiyxyPPBuW9u5HmkHgVTJ7tSdDKceQAzaYWE"),
    "3294350986": ("MONSTR #0331", "QmcZUA759dJafTXMGSndaLeJyPBcFspDBAbYb5pDq2Lfwh"),
    "3294351263": ("MONSTR #0334", "QmVpZZ6DdN4QziEHMPtzdMJn1biH7mQgVC9bry14PVy6ha"),
    "3294351474": ("MONSTR #0336", "QmRornmZki2w9NumBkCtJvezraKA8uQ9Fo4hM3NkMPapzm"),
    "3294352740": ("MONSTR #0346", "QmVH4am2YH2dFQtpGj3deTTUf7W4ctmjS4Z58BgDpyC9Zz"),
    "3294353328": ("MONSTR #0351", "QmfZdSt7wjqwSEtGv6hUkgb7XpeNHDxrkHdnEoEGZkNJy3"),
    "3294353570": ("MONSTR #0354", "QmNbZFELL2QNMgUdosfDArb1jo9CGB9gTC74sTrAxXZV7M"),
    "3294353727": ("MONSTR #0356", "QmezGed7Dg97WFi7F5hgmx9mj2yHDABP6hLMk31vjdzZZy"),
    "3294353852": ("MONSTR #0358", "QmaL7pJERWHSbvf3d3h3BGohfb3MRPwmjnjkvWPz8cAWuY"),
    "3294353947": ("MONSTR #0359", "QmT2Y3UVn1vKjSymZkFEfRXsg615LeUXxkx9LU1WSAkkTV"),
    "3294354293": ("MONSTR #0363", "QmTKtCZQMwt4NDZxXPKBe5sHnAUXEbh5ebGirUn5crkM9a"),
    "3294354504": ("MONSTR #0366", "QmWduHGZSva5XcLR9JuxxpeHYxnP2MMhdExMYPfbwnwEzW"),
    "3294355354": ("MONSTR #0369", "QmSwvKiQi8Bc2v3FcEEKUd2xrfFTLikeBb9oAwUPD5SMGb"),
    "3294355431": ("MONSTR #0370", "QmXKetHJ6sVtp6dT52a7GL9gGT36pJxjBPGvrK9X1p62Zp"),
    "3294355696": ("MONSTR #0374", "QmVh6JeFCW1EJBihPVbpWosXRLWS7a3HU2ujDAVgoqryWG"),
    "3294355984": ("MONSTR #0377", "QmR8PaBJHAKC6djZbFoc1xJPJAEZzDXJ6ebrekuocV8zzP"),
    "3294356578": ("MONSTR #0379", "QmQZQmVLZpYCynY6JfzH6G7zQVfcyuaAAetzDeuxm8mzG1"),
    "3294356950": ("MONSTR #0381", "QmdL6xbvqmP8F36uV2u37oLazhodcWUq2rxP45MxyZwmN8"),
    "3294357109": ("MONSTR #0382", "QmSqYNc9KA6uAJ99SxxmmTTMDRnbYBJudpFsZXvYSHAbCr"),
    "3294357291": ("MONSTR #0384", "QmYZfH4q2mNyhLF8UvyVGdLCcCqsNb435fmbGyUJu9zGg3"),
    "3294357751": ("MONSTR #0388", "QmPzN7qHAfEtFe6L9BV1PAU1MT62iAG3nLzfg2VRvT8tB6"),
    "3294358063": ("MONSTR #0391", "QmNrKQMe9czzwnDBXsuPzQ3cvzLdhxJQvCBDmcXCkS3xyR"),
    "3294358262": ("MONSTR #0394", "QmXtFvVYcvaCjj7dLARStxHZ7TxTYWCBMFzDkYJW2439q1"),
    "3294358795": ("MONSTR #0398", "QmdSD2UYr6vV1x31KLarVkwrNZiLUUh5nJYdxRtSGmLzQF"),
    "3294358841": ("MONSTR #0399", "QmWyqqvemciG1mfM1VtWHXqH2vpWkzpWFStxSExz1ifi1v"),
    "3294358925": ("MONSTR #0400", "QmfXyMDB4TrbmK5wixppmzPpdffx62CSJKt3CM6bMuvaiW"),
    "3294359501": ("MONSTR #0404", "QmetpQqfi9MNuvC78iyKSkBuh3WzaGYiEqcYuuCGRVpQSL"),
    "3294359646": ("MONSTR #0406", "QmXKwtQ2JvY5wQEaZeyG8BgKuDut1kk6CdBydaSD6me5Z2"),
    "3294360021": ("MONSTR #0410", "QmWwRRed1sJYQ2Z3bAawEVb7uhvDZ9RsmEsTiVeekiu94U"),
    "3294360771": ("MONSTR #0419", "QmZTMQ5ASs5cwz1hFnHBoP9PGBWb6JcaFNkWaEBUGRWg9Y"),
    "3294361209": ("MONSTR #0423", "QmarvQrqJQ8nkfSgwdJEeRV7N4rvW2Hz5BS8dnmTUZiHvX"),
    "3294361682": ("MONSTR #0426", "QmXAxtS4vqs5AUPSWZvWGw4QZGiRHWb4U3ghmQxLtEE44i"),
    "3294362045": ("MONSTR #0431", "QmNQ9XhgHYyG7borK225chMjV6gpBwj1wprg1rtL1kxR2S"),
    "3294362135": ("MONSTR #0433", "QmckAfvkj8FKZWhkVqA6tFKr7aKJXK6tD6Guro3uW1BM36"),
    "3294362303": ("MONSTR #0435", "QmZ3aXQBTWx7MX9DLTQRzQ9G5XbnJGKj97BJk6EynYcLYP"),
    "3294362591": ("MONSTR #0438", "QmPNNoom9nFJsZ9ibiFbszjFDJ2kD5nPd34ynwfWMfnphq"),
    "3294362746": ("MONSTR #0439", "QmbNtnNAVaAyhAhgcT1bzegwzoGo7yofVNf8Cf9oR2fVvx"),
    "3294362968": ("MONSTR #0442", "QmP32CY5WLxHeGyYHSiA4oYbxLnF3sKbneHBvn7tK9YXA2"),
    "3294363528": ("MONSTR #0445", "QmSFUJjEmq8P2LvvFZN55kvmSSaGkQ3YpYRVgJFv8tToMJ"),
    "3294364010": ("MONSTR #0450", "QmbzxUqty6Pxnybxb7HpmQZvCNPNewPEPjgLs15ucH1qZz"),
    "3294364143": ("MONSTR #0453", "QmUeB9EPgkm4B2bQFzc2Dd8id2D48SsYzXQBRXjFMed6kt"),
    "3294364284": ("MONSTR #0455", "QmbJa6r5b4h1tMizSNEcjrb1NsnersSMDnxfqoA2nqrZgS"),
    "3294364383": ("MONSTR #0456", "QmdrAVYLayxwYGSHBA4XSU2Y95aJKffYreqMtq8DsCK7FG"),
    "3294364542": ("MONSTR #0457", "QmPavBnjXpLDow1KHg3brbWUUDXmf6HADFqD1i47G2Swtq"),
    "3294364732": ("MONSTR #0459", "QmSQ9oac7uYEoFsFpvCqvH2cjWuEPtjFxWt5vhmWeJHrwg"),
    "3294364902": ("MONSTR #0461", "QmQ92tgs1cvhLjufkXtWFx5Yk9b8bKNCDQmpTXkCrRXr6k"),
    "3294364821": ("MONSTR #0460", "QmUtXqvvnwAaAjb82amznvVpzK8nysof5hr8wmsk2aA6zM"),
    "3294365354": ("MONSTR #0464", "QmbtwatJ9RT4j6xoxM5FkQPNPPrcVWB3ozfSZqXZH6ER5T"),
    "3294365798": ("MONSTR #0466", "QmTzfU5hpUsMWtKkyzQ69g1MWanA5yerpEAPQLjsRXCGjs"),
    "3294365993": ("MONSTR #0469", "Qmb5PJkscR7LQm92Du4737wQqG9wL2Gcei5sQtbWeE6A2P"),
    "3294366181": ("MONSTR #0471", "QmWeKHypFAmQJZL78hzByGS9ncYZ3REPQAc2iY6ccETNt2"),
    "3294366941": ("MONSTR #0479", "QmdjLoPFw3tFshCPoUvWTj2P5aQsJ7jbYQfTFEhb1qcwSK"),
    "3294367122": ("MONSTR #0481", "QmTzvctfCGz8bCyoGCQDDkFYGUcrNV98xkcVLD26kt8WjH"),
    "3294367483": ("MONSTR #0483", "QmWkFyG15N5JT6RooUMc2snAehw3McPgTaq3VX8F8FwGrR"),
    "3294367631": ("MONSTR #0484", "Qmby1Ew6pzeT2FsZxYDwnqd7QqT75K24HYYJ5oV6uAKTN3"),
    "3294367910": ("MONSTR #0486", "QmYDZWh9EcsCZTrqNavnp6Wi56jpZTCH7zifZ3CcbEEc5W"),
    "3294368271": ("MONSTR #0489", "QmWnJDzjiR9UhYN8aQqavRrjaY2jorynpUW3LAgrTNKQXG"),
    "3294368489": ("MONSTR #0490", "QmPGb469HezPQT7esEopFynm9ZySDLvDBoL36xdBknmXNb"),
    "3294368879": ("MONSTR #0493", "QmNuqX4JAmrqdrLuqqpXPx8D7gpUjAbxKddpFeHf8aystX"),
    "3294369352": ("MONSTR #0497", "QmTBwSxNnGF3FkQiJP8qEtEA77hfcw787bA2fqSoGYiPhD"),
    "3294369258": ("MONSTR #0496", "Qmbx4F5yPRcPvjf8p89nXPDQZb9SaGahPdVHPD3covbeJR"),
    "3294369582": ("MONSTR #0500", "QmVQ4pF6RVVaaH9zU8yUvzFR3TnRYsjWVGfPaP8BoUkqCJ"),
    "3294370041": ("MONSTR #0503", "Qmbb8wjCA1FdrueiYBWb8xNdxFxuvd7S4DZUufcPBimGN9"),
    "3294370110": ("MONSTR #0504", "QmTPXz92YcYiFHBaihRbpURj1bKVMMNUMhCHoyuUAisiyY"),
    "3294370505": ("MONSTR #0508", "QmPT2Usf8QYA2SnRMZh13o1MZxDT6LxNb5kq4Wq47ZVHhz"),
    "3294370581": ("MONSTR #0510", "QmPcmW2S8NKRfWvnu18MQ3bAoULQD1tSXtbDcjackyfn6j"),
    "3294370792": ("MONSTR #0513", "QmPiw6MuACWpA1fNQWvfQgTbnVC6LpqZxeE3rKfoTRZSne"),
    "3294370981": ("MONSTR #0515", "QmQLZkCu2XBzx8ufdMkXXu4aWaouNU6cnLFF6ZVQzBgNLK"),
    "3294371310": ("MONSTR #0518", "QmWSGsRUkEoWoJ7ihhyyd2CsQoh4T8JzPCdbpbQCyk65XC"),
    "3294371557": ("MONSTR #0520", "QmWF71Bqq1SdcqY2QcDArHdCnfy8MEQgidvwF1UKx35ocn"),
    "3294371660": ("MONSTR #0522", "QmeHfFfH1FmYKke25i7Yd9hTeYU7RRjQvY8UJbPGtPH6fc"),
    "3294371982": ("MONSTR #0525", "QmQQu23LbepKRtuKejqDp4WJScMewjwNo6QDDPCD3Ztbq3"),
    "3294372385": ("MONSTR #0529", "QmQZVvsBwMZwTgK6kMPdNfrjxjZVEzTUufSPuWBoDAzSwd"),
    "3294372674": ("MONSTR #0531", "QmZ28PcB7joDkztB4wBsJ8RevHnTwMG5PVeT4S97oPAZwo"),
    "3294373166": ("MONSTR #0536", "QmSBjs8bWwFZrmv8atMVriDx9jjKHSWh4Xkb3r6NsJmRq4"),
    "3294373399": ("MONSTR #0539", "QmaB4gTfFs6FmWdius71N5BDgNqT4Xp9mDMx1yKA3ELvv3"),
    "3294373694": ("MONSTR #0542", "QmSgrEepuu1nvSKNefbeuLY5EvLReMEh8zZueJEWJC5Whx"),
    "3294374088": ("MONSTR #0546", "QmWznMo6sxFA3mJNCaWT7ETnW7JvTfLctZhFM117kCb3xQ"),
    "3294374244": ("MONSTR #0549", "QmPVv4s4RLvEFf7BuPo73RyiGBK6beupaL1yFMFQM7zqUZ"),
    "3294374385": ("MONSTR #0551", "QmSHkuesH2vH2NLKmtKx9Ya9VfdVeXtVET65sFYKt2jE5T"),
    "3294374517": ("MONSTR #0553", "QmV8WyBRKcCGXUy1ZTFp8PTWXvo9wnfeSJep4zUArSv7sw"),
    "3294374720": ("MONSTR #0555", "QmfMwkzsmVtdSc9HGim6czAEZBj3YsdnuSPy49PhosApZa"),
    "3294375111": ("MONSTR #0559", "QmUuiyZy52auF9ScrzPmpT2TXTxiE1AY2Fyq3aFY9fhFjE"),
    "3294375586": ("MONSTR #0561", "QmfPyYVeQFRBWhBa3FTz2M4Y1giDiRXtxyj1f7LZsjvMQv"),
    "3294375729": ("MONSTR #0563", "QmTogppehzNPJP1zD4srYvStRZkESpD4AwbR7qAmXDGSPa"),
    "3294376121": ("MONSTR #0565", "Qmc6MXmzX6HyU37AjjxeRedxqN2d2WKm4jgqHYSFCx5NCN"),
    "3294376385": ("MONSTR #0569", "QmR2qnD33bb79vUHmzELXXNbzwtfsfUKvnJ4XkGo4HASYQ"),
    "3294376807": ("MONSTR #0572", "QmdS7sk65zmDQfDf8uqLTe6QVBqUeLFp61je5uLDfgwyhe"),
    "3294376879": ("MONSTR #0573", "QmciF6vUvnAJwmi4FJZ9faVvJUfpMYSpmj23syqUbikRho"),
    "3294377166": ("MONSTR #0576", "QmXbA5uucQWeL35S18XGDUHDKnSo6barTe82RMfXqJK2Kf"),
    "3294377420": ("MONSTR #0579", "QmapAmP7Bp9MoedC7urfkZyR28pB4yp3u1UkSpKBrpUsx7"),
    "3294377477": ("MONSTR #0580", "QmRqPrmrhAHaiWXhkL9Szps5sCYXETzKmGBsC62g1GuXL7"),
    "3294377593": ("MONSTR #0582", "Qme7ksXcPiditFFnZ1ghpRgkafj5PNGJKCDUtXqkPXdLYq"),
    "3294377732": ("MONSTR #0584", "QmZjXDSLkGcJgXNMXpw4A5SUFJDkiMRK1hckS5QmiJ6Y6q"),
    "3294377849": ("MONSTR #0586", "QmQbjMfkV2U2HeVWxyyv1PJMznmsStZw2ALLoAHpK9oKti"),
    "3294378140": ("MONSTR #0589", "QmUehD7P884g3yJ51DywXiLqqnUHY85Pgear6hZF9BBZs5"),
    "3294378456": ("MONSTR #0593", "Qmf1TQ3u5EghhaMyS5hRjcAy9UwH2ad7uL4D3aBGbXVpoD"),
    "3294378804": ("MONSTR #0596", "QmUQ28PGqYcw7JrYf3Y2EhwqRtygMnVyLGW9H6JYKbagzh"),
    "3294378932": ("MONSTR #0597", "QmaeMf8hbx5YoJRnSC3hSRsyr7junjozN7T225EU3hskvQ"),
    "3294379310": ("MONSTR #0599", "QmUwpDx7Ei5Razh63WYuB3Nd2sUnjfjrZKSGoeNcpH2Bwq"),
    "3294379588": ("MONSTR #0603", "QmShHs1DR5dHgbs9o7K7XPUX8Sqhfs5iHxhLhpnB5FBmG1"),
    "3294379640": ("MONSTR #0604", "QmamLVgHV9TZeamvCgZ3i14Xgz9uijCEKQNcmUzcvc4PWr"),
    "3294380100": ("MONSTR #0609", "QmVFTBMDbkz1bWLMAE5xAxKHXRV9PouMY2eJUtL8d3WN4G"),
    "3294380138": ("MONSTR #0610", "QmdBZrd5HtFzAK2QckFEeZa22aE3VQcyVxUo24zCEp1d7i"),
    "3294380349": ("MONSTR #0613", "Qmbj5fbQZcqyFnFvG2vLYi7wBeREgVMhG8isGUuJbwQxEW"),
    "3294380397": ("MONSTR #0614", "QmamNSdpdt5eNaLv8dVDiNKCCZptp5kwNX6W9GKQKzWA6D"),
    "3294380747": ("MONSTR #0618", "QmX1Tn19qVvpisBzjyabQz8hnu3fjPDuMFKKnjqFYvhCgH"),
    "3294380872": ("MONSTR #0619", "QmQsjQRGZXv6UMoshDdfzkrbTW8C3MRNkNRTn2aRmfKvUR"),
    "3294381041": ("MONSTR #0621", "QmPp4NYF7JF3d5QnST8yoPVX2LdW2zhomyrQ3Sf6xW5utF"),
    "3294381266": ("MONSTR #0622", "QmccTRTeBajeLFp3Pu6u6gvwMYchN8oavGnSerwo7ZqkAP"),
    "3294381451": ("MONSTR #0624", "QmPbVMAnAkWhYkoF82htcjsxQZPog2U5k7Y65HFmshFbKg"),
    "3294382092": ("MONSTR #0628", "Qmb8yGXKUsS7H9r16NDhAeNUenT3UNVraz6dzmbZV2BQDo"),
    "3294382320": ("MONSTR #0630", "QmSZjPKBws1jfpSqUUmGA7v4Lwy2YhccGmpheAgxrWT5E6"),
    "3294382414": ("MONSTR #0631", "QmeuPgWXNXXZ4j3a6Euk4J7jf7DEGF6Jqcmo24iUD5V5QZ"),
    "3294382841": ("MONSTR #0635", "QmTaNd2x4czKECaxzdKDjfnZSNmN4aZcDGka2VUwK6KLRG"),
    "3294383115": ("MONSTR #0637", "QmaomghqCBkXgHVeepSpMPQdDd6EcNyHrRVE4mpXTY51vX"),
    "3294383456": ("MONSTR #0642", "QmPtBVPdiYvQmuwZELG59CSPNEcYHnpEPojDnRM8WTPdKd"),
    "3294383750": ("MONSTR #0645", "QmWf7fFmmPRSqgJeHLCcfUdv8GhPywAtUTrNgLMpBGDMZC"),
    "3294384159": ("MONSTR #0649", "QmSKs8gTQ5ykBFPS4ucPmoaF3xiVvsSEU1dxXyNpDpUwNT"),
    "3294384270": ("MONSTR #0651", "QmX7Y5fdTiXUqCf3zYpBXhwUNXSAoc9FM7c1j29duk4erJ"),
    "3294384392": ("MONSTR #0653", "QmdNL9zD75NxX2XSfQSd6wTawjExjFtQqKfHauv37eJzSK"),
    "3294384528": ("MONSTR #0654", "QmZhNSbQaWvrsAoz7Si5bPwrqHUTy3SwwHWqj5SjkRmcno"),
    "3294384916": ("MONSTR #0658", "QmWWZmToENr6DNNq7tGmaKhjXZorxbY3dfPWqkMN87MyEf"),
    "3294385181": ("MONSTR #0661", "QmS5zZiustLpw6DxmoJBfdDdWXzFFwztooPNy7kedjqtHM"),
    "3294385627": ("MONSTR #0665", "QmRbc73eEbh3mzBjDfVfZ5jikXsc8hvYcvc8KxUV5zUW3C"),
    "3294385818": ("MONSTR #0667", "QmQSZGetTjL7DcGb7k315G2Ui3SKhwNEGXQwzEeuYnMTeL"),
    "3294386074": ("MONSTR #0669", "Qmf7hLRCiZFz7yG2VEYn683RFVNyJ5pJJxQJarJ48bAyYT"),
    "3294386362": ("MONSTR #0671", "QmW2oSY4DkxZo1daxk9tCSFsaNXC5NFWQf62n6UpEDWRe2"),
    "3294386802": ("MONSTR #0676", "QmZAPvWRiUAdRydCs451Lv7sD2AcQaXYMXGecCq8mY7s1R"),
    "3294386972": ("MONSTR #0678", "QmZeCZTfkdYaFCuqwET4g2jHZya4c8makAJZ7qUzqs26oP"),
    "3294387611": ("MONSTR #0684", "QmYCJTS4bEFgRKCH15L8o9oP2FgjnRyGWTaZLr3rcykkjZ"),
    "3294387785": ("MONSTR #0686", "QmeRYG3bM5kzsj99jyWzYtn7E4tAuyWdBFjQ2QWKDmeTVY"),
    "3294388062": ("MONSTR #0688", "QmdZsTNV75ShYGx3i3tVQ5WegXtY1YwageEdXBi3pPzHDW"),
    "3294388130": ("MONSTR #0689", "QmRkTYupoJUwjMTsfE2WicjajiNknbKHG9Jz1rmHnHNGBE"),
    "3294388382": ("MONSTR #0692", "QmYNHxaanUxpGE3BCiqcjA9yxFy6BaiPGBXK9TvKK5R2p8"),
    "3294388513": ("MONSTR #0694", "QmYNkZfaJDeYkGYqCT3Uaso9w6nxxLR5VjHmpEHkdYwXyR"),
    "3294388743": ("MONSTR #0696", "QmRdjFbErXhcH3cWffV7d2ZCVUXeXHxATUGB2nXXfe2X5S"),
    "3294389199": ("MONSTR #0701", "QmYULziDp4kbSujAUvqVzJi9DWXkAQk88Xij2ChauFJG6o"),
    "3294389737": ("MONSTR #0704", "QmT9nP8abDZjMn4AWgnqKHvXFL9kPRCbUGzYYHkzEnZAau"),
    "3294389912": ("MONSTR #0705", "QmbP2ofDX4Xuvd2CecUQzxz2JeypVms5eFHyYpj2tpTwzT"),
    "3294390818": ("MONSTR #0706", "QmbTk9Z7mVj3zuzZmHhVF1HmTYXuW4m6Tk1VtjZgL1fRn9"),
    "3294393816": ("MONSTR #0708", "QmVqYJY8wZwHfT66KSBNrm2YKoz8YJ3Mvv4mpB5sxrrehm"),
    "3294394384": ("MONSTR #0711", "QmW676s7pWBSvwesxB322esdS1ovP6E7AfY5QKpJDvPQDK"),
    "3294394877": ("MONSTR #0712", "Qme52YV9xMCKMYs5gjcNbjun5pYAo5YVyBe9FjNGtqsz8v"),
    "3294395310": ("MONSTR #0715", "QmdzXznfiE7MQTLSBQzAfewYFbL6eLtrCPW1KKUUjnS6wL"),
    "3294395634": ("MONSTR #0718", "QmSYsDwjXtA31E8Z9hUQin9fNECg6Fs1dA6pTcJpyzqbTQ"),
    "3294395703": ("MONSTR #0719", "QmaTwE2c7xmVceRBHJSrfrPLc9tQGUkV5NVu55r7AYGpt3"),
    "3294396472": ("MONSTR #0724", "QmQr2UbL6ee7pX8t4PHKDqAEyik9sy1FycLQ2gePp7gFJN"),
    "3294396689": ("MONSTR #0726", "QmehTqNTMEdHwtnV7Sr29GnzyofH43LgQEPSskw22TSTdy"),
    "3294396953": ("MONSTR #0728", "QmbCxCwg8pXbPXHDSxg3HaL77EPzEKuSWG1gfZQUKGyV1V"),
    "3294398030": ("MONSTR #0731", "Qmb8Rn8PH6tnw1qS8pChsQZ8xP8BTAvoYbyxqvcPZzxhuX"),
    "3294399741": ("MONSTR #0734", "QmX5coLoGoGEhzi3VbvQzq5c5HKWoL3ba3yrz5VhcjHFnC"),
    "3294401859": ("MONSTR #0740", "QmPAa6J919FUdoULjN7CgiaWY32Ld5BXNV9cdkY7iSD6HV"),
    "3294402056": ("MONSTR #0741", "QmWNS1X99xqzRHtLq69jk5wYbemz5yHxeSsGsz6gMi1u9C"),
    "3294403063": ("MONSTR #0744", "QmWD57pVgwnd4ux7Zyf4sEdbe2E2rPRzEHzBf6XiVz4BUV"),
    "3294403608": ("MONSTR #0746", "QmcNvAhA8whEZ1kkfxakxFSm7MwdMbvMf5JPGn7vnRZW1X"),
    "3294404277": ("MONSTR #0749", "QmVQ1D74CNxmYeYCLy9fn9YZ64te7QU9vGW9DR98RS7GEi"),
    "3294404757": ("MONSTR #0750", "QmZtYBt4VUk9zxmUC2rZn6TbrtZpGE59Km2AiaRZE7J9tJ"),
    "3294405125": ("MONSTR #0752", "QmQtEgwz1FP5dWVmEtfgf8XMruS4gt6tKY3mRTyFj5PnVE"),
    "3294406338": ("MONSTR #0755", "QmYktSK3pNNchSwTBnSTEWEX5hAYQjgjRUcivARzyYaJbx"),
    "3294406869": ("MONSTR #0757", "QmRE9P269gkaqWZVrBuU1QvW4wmDEqR1sYBw7gPuHViouA"),
    "3294407684": ("MONSTR #0759", "QmPgbyLvE2vLv6Q2AUCXc7GCgpCqFgj3jbCTt9cF8sWkxK"),
    "3294408556": ("MONSTR #0763", "QmSutqWcSUpwh4F1Dc6iynhxoVpKVqB9kMCuNcDD4J8nnD"),
    "3294409561": ("MONSTR #0766", "QmToaKQXdXDzMz18YsRGGYuiAj8Xbs1evszGFks9Y7dcjc"),
    "3294410763": ("MONSTR #0768", "QmeuUtjJWcgxahPFVsbjK4AAAyR31toJZVTEFwqSV2K3hH"),
    "3294411526": ("MONSTR #0771", "QmSXReSqN6iQJE33gd37MLRLmsE48RZWt6hGD38sdH9L7s"),
    "3294411867": ("MONSTR #0772", "QmbvrLT1DV5ZgVjrZj3mzdTRYeQNrK5kymXr6EY84Y6AYg"),
    "3294412752": ("MONSTR #0775", "QmYoXu9L9PEUKYmfEj11Ep6HT1AiMqW95773XEFhv3FNhv"),
    "3294413614": ("MONSTR #0778", "QmVPtb8r6dBnATgGpkSsDMXjc5PMvgiuMKd8Tx8AAgVyfg"),
    "3294414556": ("MONSTR #0781", "QmaKKzPTTAzQLUdRnrBBJEpNTVJAsf415YZzKnjc26BQ8F"),
    "3294414654": ("MONSTR #0782", "QmSiViQ8abbLTs51up8pqsfhbSMXNfVgAHAo2G1oZdE8Zb"),
    "3294415096": ("MONSTR #0784", "QmfQmA3XpYBNwWnqZAPZLppk2LUfdE58WxfjePmyN9mM3p"),
    "3294416487": ("MONSTR #0788", "QmUqYAGok4KWtMoMcYouji8PYvbFy7JhVRzc95U6nKuH7A"),
    "3294417573": ("MONSTR #0791", "QmZkzKPGJoBV4J4C5MTdCJhRJbLhtN5HR89cq57WJCHLQy"),
    "3294417662": ("MONSTR #0792", "QmegAMNcBw7qiWBW2N9aufLcKAb3ZEJ6waySzRCEhr5ynJ"),
    "3294417833": ("MONSTR #0794", "QmYFLhtyEXAK1XBpg4wyH9a6jemJMSTe439MFwyjMsgiFL"),
    "3294418945": ("MONSTR #0798", "QmRK8NDZydygVuvrWjatzx4SxpaVK3bd3eP7adiY219Tsc"),
    "3294419630": ("MONSTR #0801", "QmXzWQ8ekERbisTEpHN3RbdNeLNcUddYgR1QTuR4mTsFPB"),
    "3294419813": ("MONSTR #0802", "QmXqdM12vx4LRE3kYD35LS9yJjczjKV1eUu8CarHjqzsyM"),
    "3294422589": ("MONSTR #0810", "QmfU1fs1gdEoABgQveidsMguDMJY3CuNuS1kPQUENHtqwT"),
    "3294423422": ("MONSTR #0813", "QmQLvrmLhhE4MhULxZQvufeAqBexut5U3hT8BBK4rYerjP"),
    "3294424442": ("MONSTR #0816", "QmRRNNtqh15jyHtSrFXRR1MGxsLN1TTqhHsy6N5GUXBPSt"),
    "3294424643": ("MONSTR #0817", "QmWruSbDBysKH8ZsCQUcvVUHk47rfD8C6kyxJFHhggzLNt"),
    "3294425450": ("MONSTR #0820", "QmUGm6fhW3WmNzMeY4aR3ugFUx7uMesfPAvH9zcvKHeAz3"),
    "3294425778": ("MONSTR #0821", "QmTX1PifPGiED2XGmbtsn5YXYAZG9h9YNP2Ec9BBzrbcv4"),
    "3294311884": ("MONSTR #0044", "QmQBkwAwCDABkxBijGDocXe13fFCWnYLYQRbohVzyn9FbX"),
    "3294312149": ("MONSTR #0047", "QmPJ6aKwR3MgAjcebd1ew9GdAU3PGjHgwYCo2vwEfp5fGv"),
    "3294314195": ("MONSTR #0068", "QmfAcbNqPAdG6BN5Je8WHihzW9yhAwf4cczuRMAsufoqrY"),
    "3294314505": ("MONSTR #0070", "QmbMQEZ6C9oqvLBDfkZEdTVZCqnLH9V6eLr6Cceb3i5UPG"),
    "3294315205": ("MONSTR #0077", "QmdY6CoH2dABi7KyqERWW2Lm4YRX2QhGmjePZXmazDE4EL"),
    "3294315932": ("MONSTR #0083", "QmNbDWEtKXkAw8L3PtxoTMAzA3Ej7gWPN1KbRFt7SJs8Pr"),
    "3294320553": ("MONSTR #0117", "QmPdiyyi5vfV883zTjCpgaxuHo9JG9rjish91VWGRJPZ45"),
    "3294321033": ("MONSTR #0122", "QmUxWWgduZCvPYk7TNx8PMkyoKBkqZEsE4Tw9PUCt2AGdm"),
    "3294323026": ("MONSTR #0143", "QmViehcwCbmYBbfCobPMa3FxEUcgS7XK3A9srq1cqeV1yi"),
    "3294329388": ("MONSTR #0190", "QmPeaBxgxpPxbhFTdoPKFem5HripRN7WmwbCAmCrVyjEv2"),
    "3294331102": ("MONSTR #0204", "QmVbfVFcpyLnYNcfJqV2Bt6vgwpsUYZoV8UMNdvcKev9uc"),
    "3294332191": ("MONSTR #0213", "QmbnkHfM89gViPWha27qwsEqFp8p8fkX8NEihQFk2yeKiG"),
    "3294337891": ("MONSTR #0249", "QmY45RAse5kffGeGbetLY8y1ZJLHdZBP81KpBQWzs6VV7Y"),
    "3294351383": ("MONSTR #0335", "QmXbaye9inL4qNrX6UAF7Xs9nTorEz9WJMVSp4uPn4m1jq"),
    "3294353000": ("MONSTR #0348", "QmYvQA838gYGKvCKxHK1BxkeoZREg4whjnrLRkZUvbKv93"),
    "3294365284": ("MONSTR #0463", "QmRC89Kc92M1PDPcvQueV8WLbaffgswHihn7xpyjXERGZY"),
    "3294367849": ("MONSTR #0485", "Qmd1VbpHZmFwMT9ZW5C7EfgEG9iU7pScuUS2LtavjW7vGU"),
    "3294368826": ("MONSTR #0492", "QmdqN49d7DQ6V8nvuejRuSAE2zgN6YcvxqRYWGJrAnYWfH"),
    "3294368941": ("MONSTR #0494", "QmcG12RZEZGgvPx9xr6X26dZRBDBkY7p2PqhAN8NehvXBL"),
    "3294370550": ("MONSTR #0509", "QmYAuL4cyuS8PrPQCvk4YNFXY8qpUFzxQcKoPPCasV2Gfq"),
    "3294370760": ("MONSTR #0512", "QmPvWStEHJfq9ugTC5JnRK5qiyJuTaku6WzXBQQUQ9hiuw"),
    "3294373221": ("MONSTR #0537", "QmXMFFBuxJ45crxQwoNaoQEiAHT8ibopzzHxTgjSi5rzwB"),
    "3294373309": ("MONSTR #0538", "QmeXYqMLZHS5giWcyGxqaTL5D8x9LirHjXru87FGzDe4ho"),
    "3294375288": ("MONSTR #0560", "QmZRm7ZhBPBZ9hGfHfvr4ee4GRPwWRmqUUctDMbabznFhE"),
    "3294377260": ("MONSTR #0577", "QmaGDrUPGQUkfszVXYKWjVHGkLcj1nvzhqSctY3c1Yi78N"),
    "3294379875": ("MONSTR #0607", "Qme1kYFgLXRVb6CEg8YzU5o9vqa9AtNACt3CjFbUda5AwK"),
    "3294382448": ("MONSTR #0632", "QmcdC2TVRPXmX78VaAbAHJxetyK9DzTaqtDGLxs3ADAvSP"),
    "3294383385": ("MONSTR #0641", "QmTfRsJ5U8bqhrHXYiwbhacKEiG3NPuYmxeAZWUaqi8LQM"),
    "3294384037": ("MONSTR #0647", "QmTX2sZ1HvZ8fc9po78iZTVdCedtj3vGoQfyeT8y9o5aQY"),
    "3294385110": ("MONSTR #0660", "QmPsjVWMgUCLSafmCK2dZNLkg6B5kqKjnU9V6mRnS6XYyc"),
    "3294385728": ("MONSTR #0666", "QmUp4TqWRA6SzScf8jG1ys19Vc8iczWzsiaptBcDieUAUd"),
    "3294387102": ("MONSTR #0680", "QmXVL6FQX5mqAQeSmVts53HGuyiCgRMybn56bfBV1RcqFE"),
    "3294396836": ("MONSTR #0727", "QmWsZ6eFsFNi7NzMS4qREfHP7KesjCQyRrC5h9Pgvr1fgH"),
    "3294403837": ("MONSTR #0747", "QmWkP1ybMDbMBBo8iYpNPRyGXZbTKUQDAxxUjZf4R8kfE5"),
    "3294404980": ("MONSTR #0751", "QmaHSSSgD4ChAWGJyBrk4Gc6qUK7W4EgpJhU7D5X9C63ym"),
    "3294412481": ("MONSTR #0774", "QmVf2JZFgK634e4Hyohp5qPuPQeqwCpPXwrfPHamUDuGRU"),
    "3294423937": ("MONSTR #0814", "QmchX4ERAyrBiAxZtLVGRBhKB3xTzG8dVZZHbkVneCgofd"),
    "3294719520": ("MONSTR #0823", "QmcRD9XKEho25Evup8W9YiN538QzLVq7t2GnyT8KgTefMV"),
    "3294729754": ("MONSTR #0839", "QmdJ3ocGHovvT1PcUQSAWFYjJtLFtBNpjei4eCcfxLRVbH"),
    "3294731613": ("MONSTR #0849", "QmQqeK8KDjvKaa3cghTydMP26BFscdD4ebo3M2zrqVQNJU"),
    "3294735212": ("MONSTR #0870", "QmU3FEYYjkWe3RKEWmh8B4MyU5GLrK37BXKfpx8b5ANVBA"),
    "3294735327": ("MONSTR #0871", "QmZavJqtgeb8jtZttkgCSWTTUEZQbwVWrzorNHoduqzdR5"),
    "3294735480": ("MONSTR #0872", "QmQisDmCYjqccGqUb6zYFgNhSwVBz8hUZWZnLvtoweVZxt"),
    "3294738465": ("MONSTR #0895", "QmVjqf5qKKbuVYovwequ67A3LvoF7DU2oU6VTnmqAB5KXz"),
    "3294738534": ("MONSTR #0896", "QmZyeqkurd8PQJpi4qoRHRLB13n3bmxd5JMfDMoUWocRwn"),
    "3294738652": ("MONSTR #0897", "QmQyacMK7yVbJhjVx4hX5e8qQjQ2adT5kquWcn7Z4wCGad"),
    "3294758123": ("MONSTR #0919", "QmViigpsTB7wsY6u5NSnhaBKbxa37VAb3t2HjQ41nE8Zfz"),
    "3294760446": ("MONSTR #0937", "Qmccxr32UJ3gRtNGzmZU87JarUnRBeRgjcPpoahuzmRAL4"),
    "3294769900": ("MONSTR #0994", "QmNdV67LPdxrGjKFMEzt6smQtrc623rnSerZiPqZCZ7rjn"),
    "3294770551": ("MONSTR #0999", "QmUpDurYssbg4KAR8txoP7ZPvqCCDs7VjuaLvgP7jAsTNv"),
    "3294775230": ("MONSTR #1039", "QmUt7gH9d1WxWVSh6SaCVLt77HkmxcwpCyzZ92xXqCLzBi"),
    "3294775328": ("MONSTR #1040", "QmW5Z7RpHep2sNYRAqorMKwvf79h1hAaDpfcpGgu9mCrgo"),
    "3294778471": ("MONSTR #1069", "QmSxm6qkKHtfBLc4ZabBF5heoPBXcwuTSKLonDbayRR4xR"),
    "3294778596": ("MONSTR #1072", "QmdtyTZwnqqGbSwmg4HVyAyhpAQh6r45w9PkyPZKyRAizD"),
    "3294778663": ("MONSTR #1073", "QmQeW5rrwiP48qBBJZbk9UZCwjEZN7jg2yZ2Byb7BxAHUg"),
    "3294780228": ("MONSTR #1091", "QmYuTunPFZECgbNFiGWoMWsUo5VGYkgVq8SMa2xERtJMyh"),
    "3294781040": ("MONSTR #1098", "QmPfRennBUG38RLWQsAMJfweqxsDvfZbHobeo7rR7NNX3S"),
    "3294785858": ("MONSTR #1104", "QmUd8MgiwKySQomCVcVqCq2Gu36rfRY4ctT2o5g9HRAcgt"),
    "3294788258": ("MONSTR #1117", "QmaLaao9ivwWLZyiiogytXAjg8byWjogn5tXL8BxhpsVfh"),
    "3294797327": ("MONSTR #1137", "QmdPe2ZGA5khYpjfD8yefQ8yQJtvP7h9QbPFtDjzdw5MHn"),
    "3294803001": ("MONSTR #1154", "QmNomvYix7fvruNZzwcFxSGJWjrLLvvA8jSfhUMwn1Ax1r"),
    "3294814990": ("MONSTR #1217", "QmdASvB4SiMkwsETZWbtvUPvn8UmRjktVELor7gYEaDjoD"),
    "3294821621": ("MONSTR #1242", "QmbN6biZx3DKeXjeeN22Zs2yhat6pRDejAWSoqr1tFTgmR"),
    "3294826282": ("MONSTR #1265", "QmSnK8cJi6nMj3d9R51MGAT2F2Lp1TrpxmZuU7Z1QgA7hz"),
    "3294826649": ("MONSTR #1266", "Qmao98HthKVKRoQkqXu7DUxu18GDwtpc9d9faRT1ddyQZp"),
    "3294834673": ("MONSTR #1315", "QmXRoSTqWYFUHDjZdk48wYXx3VTsdyuowg2RrStfHrTFHV"),
    "3294834774": ("MONSTR #1316", "QmPzdkKdPwgZLmPTJD3oJN177xTuB1DPD3YVXRjuvpYHQq"),
    "3294835541": ("MONSTR #1319", "QmYJm3XXtfUqJGgnp7noy73ZcAqeXN2aBoM8DFJNtgRbpg"),
    "3294836545": ("MONSTR #1324", "QmVA1TFMuq8cKZwDSSfVGqkcz7Zp5HoHuaUMX6ZynC6WbB"),
    "3294837117": ("MONSTR #1327", "QmaF3L5snpGzjFKmJaw8VHKZyMMHm7P568A4XnTAqJseJt"),
    "3294840246": ("MONSTR #1341", "Qmeg5htg1dn9mJAczmqkU8ZuYoYNnWrKpz9ZF3s7TG6asL"),
    "3294840587": ("MONSTR #1344", "QmR4U3eXWe1y9GgyUV2KnhNpsm6PfrvVxQtVZzRsU2LmAJ"),
    "3294841272": ("MONSTR #1350", "QmX8LcmaDJt3DwYwmdqKDZRHsEGonXAj2A44MxUM4u5gFD"),
    "3294841428": ("MONSTR #1351", "QmT9SdLnAVod4GxuktHcXEnGTct2iU9jobXspn366gAwtP"),
    "3294844069": ("MONSTR #1373", "QmRMNrPT2v2jx3JXjBb3feTESDse86yyRksaRRs23PEoqJ"),
    "3294847429": ("MONSTR #1396", "QmSFqfUfEz8geQD1WgTQfEQGoBmc895Kw8jdFmQEMFo2fF"),
    "3294849079": ("MONSTR #1414", "QmeZFEgcpJNjJyspryqhYeYy7j4murQGMFGxxqMjikRVF7"),
    "3294849166": ("MONSTR #1415", "Qmcfa4DuAV66Zkv3MjwXbiPxZEi5xwHCSjdHDCcsYWPP95"),
    "3294850443": ("MONSTR #1427", "QmXDNDVrB6jDjJpSb1NpfM9AkVr161D2PtTEcGCCqyVDfr"),
    "3294850560": ("MONSTR #1429", "QmWw8DR5TkRjVp4MBbM6jGUu9LynHfDTpZH6EobBftrjR7"),
    "3294851748": ("MONSTR #1441", "QmXDg3gapqCjvaFVTThnFe15KzdVF5TvfUkSZy9Dmgxfor"),
    "3294851843": ("MONSTR #1442", "QmWhqyjA1in2Gc74fPpgQC6XNbrHtzrn2KnkrfYK7HdtPB"),
    "3294855108": ("MONSTR #1465", "QmVp5UTp4XFCB7WLrJcUjjSrQ16xWDqWfhyDHxbL3BzLr6"),
    "3294855264": ("MONSTR #1466", "QmTNAsUUUYEov7mARBnDCKMsRJXREacKDLCbfCBASe1cQK"),
    "3294858432": ("MONSTR #1490", "QmcHccghpAkbuBdP5AmJPTUczUvWCc2HG3fzYty8QfWnpi"),
    "3294858478": ("MONSTR #1491", "QmZwAJjMjunaxKU6Ma8KQCYwxG6HPkxnbW83zFtQCXQ1vw"),
    "3294858726": ("MONSTR #1493", "QmSYxZAafdwQYjojHhd4mhnNKZattEVGRMBUYUBTYGPBAB"),
    "3294858857": ("MONSTR #1494", "QmbTjziJcunEsD5EJ6XwYJsQ3rZfYsC8hdRcLj5547Z1eb"),
    "3294862422": ("MONSTR #1520", "QmYqFDYHLHGgnTvsc5LyGZyXDYaJCVrbjxcsRAFdj6xUER"),
    "3294864913": ("MONSTR #1541", "QmbmJYpwhWuKZkEPVqEgVqoH3d1LwUwU36ZVCxtjgPxtUR"),
    "3294870933": ("MONSTR #1557", "QmYqrHw8Ab9sP5c8xgxm8X5MJoK7vQZvomJ13jVAfKr5Ld"),
    "3294871635": ("MONSTR #1562", "Qmf5BqQFgibuV5V5eBrbGZAEXhbSG3XfQTVHo8ZZaHqQgm"),
    "3294872550": ("MONSTR #1564", "QmPAQhKS1pXZCGqGWvLHpjtcT2APWF8sP9WyfuYnYC1zaP"),
    "3294876062": ("MONSTR #1591", "QmbVAZvzf5ZQCks34dj5cg67d8oAVqc2Q1BiPmURSZQkzw"),
    "3294881953": ("MONSTR #1631", "QmQ37aV26Hy61hbLm5Unz4KUa9ysDkbTtSRxJsnuYALZRs"),
    "3294884186": ("MONSTR #1645", "QmXSejKF24dzxtpF4Hhe2sVGeiHfzHfreic7GrtH2mVZNG"),
    "3294886970": ("MONSTR #1660", "QmVCjwiF8ZFmEe7VCxBgPNkvXCHXegQeGVLMyGApKuzdsA"),
    "3294887072": ("MONSTR #1661", "QmVZ3mBfEGrdpdnqmdsMWADa4s8QKxv8SfsVsN3THqDDv5"),
    "3294887552": ("MONSTR #1663", "QmYX2zQgyDJyMC9fX4z9ZsLrieD4uikXxd9zQPrvXNJD7w"),
    "3294896184": ("MONSTR #1721", "Qma5S2shrUEsChwTjqQbp73Pvq3YAnEJnAE2XXg3nZtw7j"),
    "3294896408": ("MONSTR #1723", "QmZsHaqdxWPVSGdCdwBxdHh2ZxWKK9xX2LtxYfRPWUyXbY"),
    "3294897988": ("MONSTR #1738", "QmU2NabknAmw9tTt3Bpc7Z2wd6hC8Q8jFmVZkJ2wG65KPV"),
    "3294900627": ("MONSTR #1762", "Qma3ccaKZCHBnPFPwu7hbvVTe8AEWRegW35pZVMTGEh8Q8"),
    "3294906253": ("MONSTR #1788", "QmZfU5XA9BsnVJxVjvsGuC8wxffoRpHXkxVgU8Lmmb8BGv"),
    "3294906426": ("MONSTR #1789", "QmZnXzU27uXSAiuikKcXUSUWhrRdBUYCdxx1xXEp6RNTfi"),
    "3294909681": ("MONSTR #1812", "QmNonUtu3WeBYXKu9WJZZAKLs8NnMyp8KeMTQFzaujNazf"),
    "3294914485": ("MONSTR #1813", "Qmbsns7WjHDgE7FUgP3XYzfg2rKYpCwiKmEmdvAQLxLh41"),
    "3294918145": ("MONSTR #1834", "QmUwhvNBDJWbngdg59z1p4FqK95AgBeJBKWpiGveVWokuv"),
    "3294918590": ("MONSTR #1837", "QmZpd9GrwbXutaYBgj7N3P6kKGDuCsfDSPurvTZ7QxL89U"),
    "3294918668": ("MONSTR #1838", "Qmbj5ayDEE5Zw6Hj5u8zsUJLky8dKPgZW71YJZPjn1Lx77"),
    "3294921718": ("MONSTR #1862", "QmVjsYYrT1Q2BnqkcwEH1Gdmgnqa7Hb8fHR8jXkcMkTgB4"),
    "3294921759": ("MONSTR #1863", "QmSXLru5eNov1zRaAUsr1k9NqPh8vcASPZ8fdybMVQLdez"),
    "3294923644": ("MONSTR #1879", "QmUnsaXmd8z45iciZL3GHUkFfU3b1o9T3hiyvJhEGRWUFP"),
    "3294924881": ("MONSTR #1887", "QmdRAwwdhZTdhPg5XCPfxWfzsmMwfyavLLwEZP9uzC5pVt"),
    "3294926032": ("MONSTR #1896", "Qmcw95pzY6KWgfrpKKuLB22LH42ngqgyXkVLf8ZbLDxwRN"),
    "3294926116": ("MONSTR #1897", "Qmd9AbcDMV3UGRA8V4Cnz42C1ru9SjuB4y22iTaTkS8tLK"),
    "3294926685": ("MONSTR #1902", "QmSNQKPXngAF83FpxU6LmAjpyDhMsyYEbsB9DTNN3fugyr"),
    "3294926897": ("MONSTR #1904", "QmYmP5oD4GGvwitdB75jiPYFUZnWYY6kQFkAUjVqf1Q877"),
    "3294931756": ("MONSTR #1936", "QmYQGBSXChowuRygWq9MJ7xKPvA2x3HvAqK9ZxXCHmoD64"),
    "3294934695": ("MONSTR #1960", "QmQvhv3i6dVVAiq2v9hDsgXwxYFgbwj9zW42ss8EAdFUdH"),
    "3294937067": ("MONSTR #1974", "QmThBMTyjX3ytGyUW6F5pJUHFwiCv5jrPh4scZm3m91m7u"),
    "3294939506": ("MONSTR #1996", "QmX7iYXUb9ZupAbEfrsVx8AtWd39d7arryTqz4AqdFD99w"),
    "3294939752": ("MONSTR #1999", "Qmf1boL9nDTYkhNV5inptcbArLYaqwa7VP2SmBnDcvccNe"),
    "3294940101": ("MONSTR #2001", "QmfXR4qsdrGXHwAoQdunnQ5pjtP8mTDUGeGAX5EccZJkqs"),
    "3294322002": ("MONSTR #0132", "QmZ2szW7GjVMQoqCNuxHH2RWpJAL64RGsuQSEYmQsP1vuf"),
    "3294898484": ("MONSTR #1742", "QmemQ38HdTYHiUbYZpFAi8GCcDD8ZUBjQVM7yZFDWzVDjp"),
    "3294411253": ("MONSTR #0769", "QmTvS8NXsNh3JseFSUQwEuGRxNFL1LYdVE7NXRh8CxLZN1"),
    "3294756099": ("MONSTR #0901", "QmXRB19D2j8ubNuLtpgDTbvaj3e7jwJT4AqYpFkbFAEPTQ"),
    "3294927665": ("MONSTR #1908", "QmWRodheNXHDeswFNmXmupjUvPxmytHodSQ1mgHeWKXSDp"),
    "3294863527": ("MONSTR #1531", "QmUcfQ239h44D9HX1VB8ueLMzAkSCDVdBXF2kGTnLT8cDL"),
    "3294313983": ("MONSTR #0066", "QmUN6fok99yezc7GjkTQLzdKVxMgEArecq98vFRTJML53N"),
    "3294413814": ("MONSTR #0779", "QmT6qr1zfT5QGeaBzZa4YH6ALDonkHXD4AHgz13qgwMdNh"),
    "3294917910": ("MONSTR #1833", "QmYqaTkKhKdnRZWzXyj2Cdvh3UpUgPG5DPf2rsJYJLrESp"),
    "3294336979": ("MONSTR #0238", "Qmc2GVR86xVWft4b43CUt72C4J49ne4vVgDneaM3BgE7Gu"),
    "3294350834": ("MONSTR #0328", "QmZcTTMD1VRZh4g62bVCTLpwpJBcHWYVJR2yWHgos5NKRq"),
    "3294875407": ("MONSTR #1587", "QmdKaKWLMvWLx7hUtaUub11dUnsDf6pSSyoTtYw4D6H5PJ"),
    "3294417759": ("MONSTR #0793", "QmavR3Q8ECM6NVhye7a6JwyjQ5fBQ33pjVYPRoCCcGKAXD"),
    "3294771395": ("MONSTR #1007", "QmYhbHq4JUtduW5LGJwxoGNFew67Dxhhcir4uZVGyP7L24"),
    "3294818591": ("MONSTR #1232", "QmWeoshR7yhupdPvsS3ZfrmdYNsdjPqPy8Rdk3cjiF1TNF"),
    "3294738364": ("MONSTR #0894", "QmXb7uH2cUMm6DNtfPDxWXfJF9os28B36U2ZXQKYs9Xrnj"),
    "3294319049": ("MONSTR #0106", "QmaFqXgTsHzE2876jeHKrcHqGqTUmLbpEZ6U5NvNDsqNAb"),
    "3294728986": ("MONSTR #0831", "QmQaNPMdRiv1fFYYNC72vefSaJo3B3XggDYYrvJETSn5eC"),
    "3294322385": ("MONSTR #0136", "QmZW9cCS3iVUmh8a84CZgd4Efd1HbUsjqoxSQhXC99cAbN"),
    "3294355540": ("MONSTR #0372", "QmW4bM8A73Wy3VJVfWk1k7bwohGogDqwUM7h1ysWQK1qtq"),
    "3294315097": ("MONSTR #0076", "Qmb3FraXsVMgXx4AgvRWVPiGG5L2FHz5gvDfn7rg43ic2u"),
    "3294357911": ("MONSTR #0389", "QmTWgcBChtvby2zd3tsHr1NWyJVgcLYJYcJ1orNVDJubrJ"),
    "3294815615": ("MONSTR #1220", "QmPxxxmbdFu3gKqtyGX2LCcioW5FPingKzY8x9UnsYKVDc"),
    "3294921505": ("MONSTR #1860", "QmdVtEhMng1g8piSyTA4vhEQ5ZnPXDPpm2HwXPKMgrQ4hW"),
    "3294321628": ("MONSTR #0128", "QmW15oBdG3LtUL39B9mJJPzwkQphK5gjZYMmSht8agaRST"),
    "3294851565": ("MONSTR #1440", "QmRb3w61hXceUrWxCaKtsRfAuLwHuLjUtKVEo9et5F69Y3"),
    "3294367964": ("MONSTR #0487", "QmbuuJwP54ZVkjx2ayD4hVMp1FX8fmfi1XZAdwwhAcHqy4"),
    "3294773647": ("MONSTR #1026", "QmZTToSA2XELdxzpSWBarhe6VaCNJ6rSpnJ7Tuk5vDb4cu"),
    "3294734785": ("MONSTR #0867", "QmbtkaPf2HcKDkdQ3L9J5cxE8rsH7kMnYEmauF2zY8w7eW"),
    "3294846816": ("MONSTR #1392", "QmPr47EPUqZt6SM4Sv7YGGgNk2ds5rFSENuRsk8zrT6Qj8"),
    "3294877422": ("MONSTR #1601", "QmZ1Exmyc4TiGBCyZ8dmKH8t81wM7Xrjgh2DQZKQ5KPJR3"),
    "3294342968": ("MONSTR #0259", "QmUdqP8VTcCeoo8qxALiVmHvYtGC3KuvjUpRgmmizJEhUt"),
    "3294354195": ("MONSTR #0362", "QmbsBaEXNwMyXkQxU2ceRwsjUXBF3Rxngn6k4UKDk3nnau"),
    "3294889625": ("MONSTR #1675", "QmYVBGj3Ck9UAQrFnjncCEvBb8EvFrU8cv7pjL6ASrWjAc"),
    "3294926397": ("MONSTR #1899", "QmZpXpkTt9UTZWso6TDBFByqJASzQ728idtEAA9Tx7XxmX"),
    "3294790531": ("MONSTR #1121", "QmVe7jNb8Wn5ZFR9acpuDYAHcJ4wKgG8diPhsF5tGfXGQg"),
    "3294374982": ("MONSTR #0557", "QmXGTgnvCehkYdoSQ5aVk5vJxLbyzdnbVaeVHxm8gj8kdf"),
    "3294897417": ("MONSTR #1733", "Qmcdqjb2nzDkoAiD6RYcaMmDvR2x4knDiEhNrQV2Xu9WvV"),
    "3294361845": ("MONSTR #0429", "QmNfotGHZDBzu2nLDYUYHLgkABftnBZn4EZqRk8eqWJ2MJ"),
    "3294895015": ("MONSTR #1712", "QmewdSqjeLarqNqnTbKgyBx1bK3op3qMCjKr3enpMPii1R"),
    "3294320151": ("MONSTR #0112", "QmPemzGG7PmMZAPA2YEdz4d8XeoAs3BmtVCtM1fvg8Mx8t"),
    "3294840405": ("MONSTR #1342", "QmXCyJHq7hVfobhKkWhx5faJYFvxWuUwJ6Emv9vjCtGNTo"),
    "3294938670": ("MONSTR #1989", "Qmc2GZUj2g6t2k1wLWmnxyBWpry2Gpxw9W5e45ycEuddxJ"),
    "3294769748": ("MONSTR #0993", "QmR1rVfcY9uGWoPhLmiiPXryHonKdb99eLzQPgwSdwwW85"),
    "3294360669": ("MONSTR #0418", "QmSVjj6BdJUF5aqUtzmmieeEQWZPGugvQ8PSM56RwRcrZy"),
    "3294936604": ("MONSTR #1970", "QmTukGQAqqFE8L9UKXth5VG7Vi53j5CxiwxnQQ7wMEkEEn"),
    "3294906768": ("MONSTR #1791", "QmSCvgdhnjAwYaNvVXjoZtMVrmPMBBoTsBQaDVAAxjKnBZ"),
    "3294400712": ("MONSTR #0737", "QmQ1kBqsYUywhR6dmBrgMknUoSaAu6x8hepA4qMKmWqzpL"),
    "3294361822": ("MONSTR #0428", "QmXoWWNUorBkVC4JXyRsvYNvyAyQPbVKScHWFChHedpqAi"),
    "3294372733": ("MONSTR #0532", "Qmb2f8VLSLgfoHmp5vrmLy8NaKYF3L11sNs3GuzCKfPq3F"),
    "3294363826": ("MONSTR #0448", "QmUVRDSX3DLcpJUMzbnQvUm2uGXHFkGNAE8dN1EYsJ6qLP"),
    "3294935494": ("MONSTR #1965", "QmaxuR7DsNAAde53b1mbFFTvx5RHbeSjkD6pbnbhYuKVG1"),
    "3294324555": ("MONSTR #0154", "QmXvDrL1bwp927Np3uoYsY1HqVmYNMEEB7wsvNqjHZK8hQ"),
    "3294423215": ("MONSTR #0812", "QmSR3zvUK3zKPoNMjkTuFzt4UV8nHc7hUwa56pttPaTMMu"),
    "3294852191": ("MONSTR #1445", "Qmcc6fZfVv9JRLzXSLqSemZ6y7feLLBXtr2mS7cvLw571d"),
    "3294385465": ("MONSTR #0663", "QmcAusheBLMSN5FYW5Vi3PvQGUXBUUhpHvKwcrwXnc3hqJ"),
    "3294820914": ("MONSTR #1239", "QmdSwseqPowovPWGHJwMzp8SXANxVz6udaLVPcjfWgGtXm"),
    "3294389532": ("MONSTR #0703", "QmSYj5txmLYpeS5Y3ueeQgir7VrR3JA6XsxjNvwDZCemfo"),
    "3294345888": ("MONSTR #0283", "QmQihz67zAypYdQqv8ZqqQLzzjo7gZgS5ZV3TRwtHgmqvs"),
    "3294401203": ("MONSTR #0738", "QmYwjSVSdf5mBUoZTsgAx2HgariAmamw1CL53KaoMZGooQ"),
    "3294736217": ("MONSTR #0878", "QmcX65kxYJrQ75mAeBuMTqc7ENzn4x4u1EqsMW6qsfhjKq"),
    "3294931890": ("MONSTR #1937", "QmZNjKhBefQJcp19SNQM8PvvzUi4ozNajxagQSUL4WMYCP"),
    "3294938357": ("MONSTR #1984", "QmZonX2njYS73QS8FQX8Tdv3Do4FW7GkwiwXgz7UFMrFvW"),
    "3294888904": ("MONSTR #1671", "QmadPvVv39sUCRTk2N3srZEJ7sBJSC94kCsBS3PLwJPeth"),
    "3294886134": ("MONSTR #1656", "QmWghSbDom8YXHqpytmCFXRRkxAEJBFZakcTSgyHbrUoRc"),
    "3294876440": ("MONSTR #1594", "QmZJ3XqGVApddwGMAC4KiVmTFc5cPADnoib6uQMGfXr6jY"),
    "3294872666": ("MONSTR #1565", "QmRMGc5DYMaXEtApWrqrjWTMeK1cUuf6Ag8Ek7tys3kjGG"),
    "3294416038": ("MONSTR #0787", "QmPMpW97egUn4mJBcpdv6KypL7UYvCHUK8yGjwe3Y5cNa9"),
    "3294371404": ("MONSTR #0519", "QmVFQpSDKTHiJhuD1HYhu8cj4kyP4jv2YyCammuV2ShUXK"),
    "3294728007": ("MONSTR #0824", "QmSnLbAcPyznXLL6io97qmdarGdNg4Cn3ix8ojKNwmm2eM"),
    "3294330486": ("MONSTR #0198", "QmdQbuUtaq2Q53zcwQNHcUCT6fKpYNhTrdoxBApbuB3ekY"),
    "3294878722": ("MONSTR #1610", "QmZ8bHXy5KNvBF72Aasyarx7szfdhkqXW6mcJoZxL7Kxwo"),
    "3294844204": ("MONSTR #1374", "QmccG4aDooEYAdytKtXYMc8ydKCoLJqG9jTMd5aCXpqgJ5"),
    "3294360236": ("MONSTR #0413", "QmTz5hd2g9emdx4Ls5aTuJtzEpwKY74g8C3LUXuNQfmFQ8"),
    "3294835097": ("MONSTR #1317", "QmdVcXEpY5rgFY4EiuipUAjWWAR7tYyMAgBJgy9idKcCRB"),
    "3294329862": ("MONSTR #0193", "QmZJf2cqLMASC9jus6StQaLwXaHcGsnFJCxPcqoHQb84kf"),
    "3294889232": ("MONSTR #1673", "QmNzxbsBwnYcCMBTEkfYAzpbc8uM5CEGCHnXF8qrJnX9sr"),
    "3294767687": ("MONSTR #0975", "Qmdg5TGFKT9xQkqi33SM7BH6jvB7ihJw2ZsQFKEMNM5Fh2"),
    "3294337239": ("MONSTR #0241", "QmNv62N1fjFNkgiSp13N9LWuqgyuznmsjuoEG2gFKASCTG"),
    "3294939701": ("MONSTR #1998", "QmdbqE9Bc2YJca5Nbo5JPA9LaEwn4XcickCtX7zpY7BH6P"),
}

IPFS_GATEWAY = "https://ipfs.algonode.xyz/ipfs/"


def decode_arc19_reserve(reserve_address: str) -> str | None:
    """
    ARC19 encodes the IPFS CID in the asset reserve address.
    Algorand address = base32(32-byte-CID-multihash + 4-byte-checksum)
    We decode the address, strip the checksum, then base58-encode
    the multihash to get the standard Qm... CIDv1 string.
    """
    try:
        import base64

        # Algorand addresses are base32 uppercase, 58 chars
        # Pad to multiple of 8 for standard base32 decode
        padded = reserve_address.upper() + "=" * (-len(reserve_address) % 8)
        decoded = base64.b32decode(padded)  # 36 bytes: 32 data + 4 checksum
        multihash_bytes = decoded[:32]      # raw multihash (sha2-256 digest)

        # Prepend multihash header: 0x12 = sha2-256, 0x20 = 32 bytes length
        full_multihash = bytes([0x12, 0x20]) + multihash_bytes

        # Base58 encode to get standard Qm... CID
        BASE58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        num = int.from_bytes(full_multihash, "big")
        result = []
        while num > 0:
            num, rem = divmod(num, 58)
            result.append(BASE58_ALPHABET[rem:rem+1])
        # Add leading "1"s for leading zero bytes
        for byte in full_multihash:
            if byte == 0:
                result.append(b"1")
            else:
                break
        cid = b"".join(reversed(result)).decode()
        return cid
    except Exception as e:
        print(f"[ARC19] CID decode failed: {e}")
        return None


async def fetch_live_image_url(asa_id: str) -> str | None:
    """
    ARC19 two-step image fetch:
      1. Decode reserve address → metadata CID
      2. Fetch metadata JSON from IPFS → extract image CID
      3. Return direct image URL

    Falls back to stored hash in MONSTR_ASSETS if any step fails.
    """
    import urllib.request
    import json

    indexer_url = os.getenv("INDEXER_URL", "https://mainnet-idx.algonode.cloud")

    try:
        # Step 1: Get asset params from indexer
        url = f"{indexer_url}/v2/assets/{asa_id}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "X-Indexer-API-Token": os.getenv("INDEXER_TOKEN", ""),
        })
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())

        params = data.get("asset", {}).get("params", {})
        asset_url = params.get("url", "")
        reserve = params.get("reserve")

        # Step 2: Decode reserve → metadata CID
        metadata_cid = None
        if "template-ipfs" in asset_url and reserve:
            metadata_cid = decode_arc19_reserve(reserve)
        elif asset_url.startswith("ipfs://"):
            metadata_cid = asset_url.replace("ipfs://", "")

        if not metadata_cid:
            print(f"[ARC19] Could not resolve metadata CID for ASA {asa_id}")
            return None

        print(f"[ARC19] Metadata CID for ASA {asa_id}: {metadata_cid}")

        # Step 3: Fetch metadata JSON from IPFS — try multiple gateways
        METADATA_GATEWAYS = [
            "https://ipfs.algonode.xyz/ipfs/",
            "https://ipfs.io/ipfs/",
            "https://gateway.pinata.cloud/ipfs/",
            "https://cloudflare-ipfs.com/ipfs/",
            "https://dweb.link/ipfs/",
        ]

        metadata = None
        for gw in METADATA_GATEWAYS:
            try:
                metadata_url = f"{gw}{metadata_cid}"
                req2 = urllib.request.Request(metadata_url, headers={
                    "User-Agent": "Mozilla/5.0 (compatible; MONSTRSBot/1.0)",
                    "Accept": "application/json",
                })
                with urllib.request.urlopen(req2, timeout=10) as r:
                    metadata = json.loads(r.read())
                print(f"[ARC19] Metadata fetched via {gw}")
                break
            except Exception as e:
                print(f"[ARC19] Gateway {gw} failed: {e}")
                continue

        if not metadata:
            print(f"[ARC19] All metadata gateways failed for ASA {asa_id}")
            return None

        image_field = metadata.get("image", "")
        if not image_field:
            print(f"[ARC19] No image field in metadata for ASA {asa_id}")
            return None

        # image field is ipfs://QmXXX — fetch image bytes directly
        image_cid = image_field.replace("ipfs://", "")

        IMAGE_GATEWAYS = [
            "https://ipfs.algonode.xyz/ipfs/",
            "https://ipfs.io/ipfs/",
            "https://gateway.pinata.cloud/ipfs/",
            "https://cloudflare-ipfs.com/ipfs/",
            "https://dweb.link/ipfs/",
        ]

        for gw in IMAGE_GATEWAYS:
            try:
                image_url = f"{gw}{image_cid}"
                req3 = urllib.request.Request(image_url, headers={
                    "User-Agent": "Mozilla/5.0 (compatible; MONSTRSBot/1.0)",
                })
                # Stream in chunks — stop at 20MB, we'll resize it down
                chunks = []
                total = 0
                with urllib.request.urlopen(req3, timeout=20) as r:
                    while True:
                        chunk = r.read(1_048_576)  # 1MB chunks
                        if not chunk:
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                        if total >= 20_000_000:
                            print(f"[ARC19] Stopping download at 20MB from {gw}")
                            break
                image_bytes = b"".join(chunks)
                print(f"[ARC19] Image fetched ({total // 1024}KB) via {gw}")
                return image_bytes
            except Exception as e:
                print(f"[ARC19] Image gateway {gw} failed: {e}")
                continue

        print(f"[ARC19] All image gateways failed for ASA {asa_id}")
        return None

    except Exception as e:
        print(f"[ARC19] Live fetch failed for ASA {asa_id}: {e}")
        return None


def get_fallback_image_url(asa_id: str) -> str:
    """Return stored IPFS URL from MONSTR_ASSETS dict as fallback."""
    _, ipfs_hash = MONSTR_ASSETS.get(asa_id, ("Unknown", ""))
    return f"{IPFS_GATEWAY}{ipfs_hash}" if ipfs_hash else ""


async def pick_random_monstr(boss: bool = False) -> dict:
    asa_id = random.choice(list(MONSTR_ASSETS.keys()))
    name, _ = MONSTR_ASSETS[asa_id]
    base_hp = random.randint(5000, 10000)

    try:
        image_bytes = await asyncio.wait_for(fetch_live_image_url(asa_id), timeout=20)
    except asyncio.TimeoutError:
        print(f"[ARC19] Image fetch timed out for ASA {asa_id}, spawning without image")
        image_bytes = None
    except Exception as e:
        print(f"[ARC19] Image fetch error for ASA {asa_id}: {e}")
        image_bytes = None

    return {
        "asa_id":       asa_id,
        "name":         name,
        "image_bytes":  image_bytes,
        "image_url":    get_fallback_image_url(asa_id),
        "max_hp":       base_hp * 3 if boss else base_hp,
        "is_boss":      boss,
    }

async def pick_wave(boss: bool = False) -> list:
    """Pick MONSTR(s) for an encounter. Boss = 1, standard = 3 waves."""
    count = 1 if boss else WAVE_COUNT
    # Fetch all wave MONSTRs concurrently
    monstrs = await asyncio.gather(*[pick_random_monstr(boss=boss) for _ in range(count)])
    return list(monstrs)


# ─────────────────────────────────────────────
# PAYOUT CONFIG
# ─────────────────────────────────────────────

DAMAGE_POOL        = 3000
KILL_SHOT_BONUS    = 750
FIRST_STRIKE_BONUS = 500
TEAM_BONUS_POOL    = 750
TOTAL_GOO          = DAMAGE_POOL + KILL_SHOT_BONUS + FIRST_STRIKE_BONUS + TEAM_BONUS_POOL  # 5000
BOSS_MULTIPLIER    = 3
ENCOUNTER_DURATION = 600   # 10 minutes total for all waves
CRIT_CHANCE        = 0.05  # 5%
WAVE_COUNT         = 3     # MONSTRs per encounter (boss = 1)

# Attack options — (label, emoji, min_dmg, max_dmg, counter_chance)
ATTACK_OPTIONS = [
    ("Zap",        "⚡", 30,  80,  0.03),  # 3% counter chance
    ("Blaze",      "🔥", 60,  120, 0.05),  # 5% counter chance
    ("Obliterate", "💀", 20,  200, 0.10),  # 10% counter chance — riskier
]

# Counter-attack config
COUNTER_BASE_CHANCE  = 0.05   # base chance per wave
COUNTER_WAVE_SCALING = 0.02   # +2% per wave (wave 2 = 7%, wave 3 = 9% for Blaze)
COUNTER_COOLDOWN     = 30     # seconds when stunned
BASE_COOLDOWN        = 15     # normal cooldown


# ─────────────────────────────────────────────
# ENCOUNTER STATE
# ─────────────────────────────────────────────

class EncounterState:
    def __init__(self, monstrs: list, is_boss: bool = False):
        # Wave support — list of MONSTR dicts
        self.monstrs            = monstrs
        self.wave_index         = 0
        self.is_boss            = is_boss
        self.started_at         = datetime.now(timezone.utc)

        # All-encounter tracking (persists across waves)
        self.damage_dealt:  dict[int, int] = {}
        self.attack_counts: dict[int, int] = {}
        self.crit_counts:   dict[int, int] = {}
        self.tag_pairs:     dict[int, int] = {}
        self.attackers_ordered: list[int] = []
        self.total_attacks: int = 0
        self.crit_count:    int = 0

        # Per-wave tracking (reset on each new wave)
        self.first_striker:       int | None = None
        self.kill_shotter:        int | None = None
        self.wave_kills:          list[int | None] = []  # kill shotter per wave
        self.wave_first_strikers: list[int | None] = []  # first striker per wave

        # Init first wave
        self._init_wave()

    def _init_wave(self):
        m = self.monstrs[self.wave_index]
        self.hp     = m["max_hp"]
        self.max_hp = m["max_hp"]
        self.alive  = True
        # Reset per-wave trackers
        self.kill_shotter  = None
        self.first_striker = None  # resets each wave so every wave has a first strike bonus

    @property
    def monstr(self):
        idx = min(self.wave_index, len(self.monstrs) - 1)
        return self.monstrs[idx]

    @property
    def wave_num(self):
        return self.wave_index + 1

    @property
    def total_waves(self):
        return len(self.monstrs)

    def next_wave(self) -> bool:
        """Advance to next wave. Returns True if there is one, False if encounter over."""
        # Save this wave's bonuses before resetting
        self.wave_kills.append(self.kill_shotter)
        self.wave_first_strikers.append(self.first_striker)
        self.wave_index += 1
        if self.wave_index >= len(self.monstrs):
            return False
        self._init_wave()
        return True

    def register_attack(self, user_id: int, tagged_id: int | None, attack_type: int = 1) -> dict:
        """attack_type: 0=Zap, 1=Blaze, 2=Obliterate"""
        if not self.alive:
            return {"error": "encounter_over"}

        is_crit = random.random() < CRIT_CHANCE
        _, _, min_dmg, max_dmg, counter_chance = ATTACK_OPTIONS[attack_type]
        base_damage = random.randint(min_dmg, max_dmg)
        atk_mult = get_atk_multiplier(str(user_id))
        damage = int(base_damage * atk_mult * (2 if is_crit else 1))
        if self.is_boss:
            damage = int(damage * BOSS_MULTIPLIER * 0.6)

        # Counter-attack check — scales with wave number
        wave_counter_chance = counter_chance + (self.wave_index * COUNTER_WAVE_SCALING)
        is_counter = random.random() < wave_counter_chance and self.hp > 0

        events = []
        if self.first_striker is None:
            self.first_striker = user_id
            events.append("first_strike")

        if user_id not in self.damage_dealt:
            self.attackers_ordered.append(user_id)
            self.damage_dealt[user_id] = 0

        if tagged_id and tagged_id != user_id and user_id not in self.tag_pairs:
            self.tag_pairs[user_id] = tagged_id

        self.damage_dealt[user_id] += damage
        self.attack_counts[user_id] = self.attack_counts.get(user_id, 0) + 1
        self.total_attacks += 1
        self.hp = max(0, self.hp - damage)

        if is_crit:
            self.crit_count += 1
            self.crit_counts[user_id] = self.crit_counts.get(user_id, 0) + 1
            events.append("crit")

        if self.hp <= 0 and self.alive:
            self.alive = False
            if user_id != self.first_striker:
                self.kill_shotter = user_id
            else:
                others = [uid for uid in self.attackers_ordered if uid != user_id]
                self.kill_shotter = others[0] if others else None
            events.append("kill_shot")

        return {
            "damage":       damage,
            "hp_remaining": self.hp,
            "max_hp":       self.max_hp,
            "events":       events,
            "is_crit":      is_crit,
            "is_counter":   is_counter,
        }

    def calculate_payouts(self) -> dict[int, int]:
        mult = BOSS_MULTIPLIER if self.is_boss else 1
        damage_pool = DAMAGE_POOL * mult
        kill_bonus  = KILL_SHOT_BONUS * mult
        first_bonus = FIRST_STRIKE_BONUS * mult
        team_pool   = TEAM_BONUS_POOL * mult

        payouts: dict[int, int] = {}
        total_damage = sum(self.damage_dealt.values())

        valid_team_pairs = {
            uid: tagged for uid, tagged in self.tag_pairs.items()
            if tagged in self.damage_dealt
        }
        if not valid_team_pairs:
            damage_pool += team_pool

        if total_damage > 0:
            for uid, dmg in self.damage_dealt.items():
                share = int((dmg / total_damage) * damage_pool)
                payouts[uid] = payouts.get(uid, 0) + share

        # Award first strike bonus per wave (wave_kills tracks completed waves,
        # current wave first_striker is still in self.first_striker)
        # Collect all wave first strikers — stored in wave_first_strikers list
        all_first_strikers = list(self.wave_first_strikers) + ([self.first_striker] if self.first_striker else [])
        for fs in set(all_first_strikers):
            if fs and fs in self.damage_dealt:
                payouts[fs] = payouts.get(fs, 0) + first_bonus

        # Award kill shot bonus per wave
        all_kill_shotters = list(self.wave_kills) + ([self.kill_shotter] if self.kill_shotter else [])
        for ks in set(all_kill_shotters):
            if ks and ks in self.damage_dealt:
                payouts[ks] = payouts.get(ks, 0) + kill_bonus

        if valid_team_pairs:
            team_members: set[int] = set()
            for uid, tagged in valid_team_pairs.items():
                team_members.add(uid)
                team_members.add(tagged)
            per_member = team_pool // max(len(team_members), 1)
            for uid in team_members:
                payouts[uid] = payouts.get(uid, 0) + per_member

        return payouts


# ─────────────────────────────────────────────
# SUPABASE HELPERS
# ─────────────────────────────────────────────

def log_encounter_to_db(state: EncounterState, payouts: dict[int, int]) -> str | None:
    try:
        db = get_supabase()
        mult = BOSS_MULTIPLIER if state.is_boss else 1
        enc = db.table("encounters").insert({
            "monstr_asa":             state.monstr["asa_id"],
            "monstr_name":            state.monstr["name"],
            "max_hp":                 state.max_hp,
            "is_boss":                state.is_boss,
            "started_at":             state.started_at.isoformat(),
            "ended_at":               datetime.now(timezone.utc).isoformat(),
            "status":                 "completed" if not state.alive else "expired",
            "total_attackers":        len(state.damage_dealt),
            "total_goo_distributed":  sum(payouts.values()),
        }).execute()
        encounter_id = enc.data[0]["id"] if enc.data else None

        for uid, dmg in state.damage_dealt.items():
            db.table("encounter_attacks").insert({
                "encounter_id":     encounter_id,
                "user_id":          str(uid),
                "damage_dealt":     dmg,
                "tagged_user_id":   str(state.tag_pairs[uid]) if uid in state.tag_pairs else None,
                "goo_earned":       payouts.get(uid, 0),
                "got_first_strike": uid == state.first_striker,
                "got_kill_shot":    uid == state.kill_shotter,
            }).execute()

        return encounter_id
    except Exception as e:
        print(f"[ERROR] log_encounter_to_db: {e}")
        return None

def update_weekly_stats(state: EncounterState):
    try:
        db = get_supabase()
        week_start = _week_start()
        for uid, dmg in state.damage_dealt.items():
            existing = db.table("weekly_stats").select("*").eq("user_id", str(uid)).eq("week_start", week_start).execute()
            kill = 1 if uid == state.kill_shotter else 0
            if existing.data:
                row = existing.data[0]
                db.table("weekly_stats").update({
                    "total_damage":      row["total_damage"] + dmg,
                    "kill_shots":        row["kill_shots"] + kill,
                    "encounters_joined": row["encounters_joined"] + 1,
                }).eq("user_id", str(uid)).eq("week_start", week_start).execute()
            else:
                db.table("weekly_stats").insert({
                    "user_id":           str(uid),
                    "week_start":        week_start,
                    "total_damage":      dmg,
                    "kill_shots":        kill,
                    "encounters_joined": 1,
                }).execute()
    except Exception as e:
        print(f"[ERROR] update_weekly_stats: {e}")

def _week_start() -> str:
    now = datetime.now(timezone.utc)
    monday = now - timedelta(days=now.weekday())
    return monday.strftime("%Y-%m-%d")

async def payout_all(state: EncounterState, payouts: dict[int, int], channel: discord.TextChannel):
    """
    Instant — credits GOO balances in Supabase via batch upsert.
    Background sender loop handles actual on-chain transfers.
    """
    db = get_supabase()
    try:
        # Fetch all existing balances in one query
        user_ids = [str(uid) for uid, amt in payouts.items() if amt > 0]
        existing = db.table("goo_balances").select("user_id,balance").in_("user_id", user_ids).execute()
        existing_map = {r["user_id"]: r["balance"] for r in existing.data}

        # Build upsert rows
        rows = []
        for uid, amount in payouts.items():
            if amount <= 0:
                continue
            prev = existing_map.get(str(uid), 0)
            rows.append({
                "user_id":      str(uid),
                "balance":      prev + amount,
                "needs_payout": True,
                "updated_at":   datetime.now(timezone.utc).isoformat(),
            })

        if rows:
            db.table("goo_balances").upsert(rows, on_conflict="user_id").execute()
            print(f"[PAYOUT] Credited {len(rows)} balances: { {r['user_id'][:8]: r['balance'] for r in rows} }")
    except Exception as e:
        print(f"[PAYOUT] Batch credit failed: {e}")


def _add_pending(db, user_id: str, amount: int):
    existing = db.table("pending_goo").select("amount").eq("user_id", user_id).execute()
    if existing.data:
        new_amt = existing.data[0]["amount"] + amount
        db.table("pending_goo").update({"amount": new_amt}).eq("user_id", user_id).execute()
    else:
        db.table("pending_goo").insert({"user_id": user_id, "amount": amount}).execute()


# ─────────────────────────────────────────────
# BOSS SCHEDULING
# ─────────────────────────────────────────────

def should_schedule_boss(db) -> bool:
    """True if no boss in last 30 days and random 1-in-60 rolls hit."""
    try:
        thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        recent = db.table("encounters").select("id").eq("is_boss", True).gte("started_at", thirty_days_ago).execute()
        if recent.data:
            return False  # Boss already happened this month
        return random.randint(1, 60) == 1
    except Exception as e:
        print(f"[BOSS] schedule check failed: {e}")
        return False




# ─────────────────────────────────────────────
# MILESTONE CONFIG
# ─────────────────────────────────────────────

STREAK_MILESTONES = {
    3:  500,
    7:  1500,
    14: 4000,
    30: 10000,
    60: 25000,
}

KILL_MILESTONES = {
    1:  250,
    5:  1000,
    10: 2500,
    25: 6000,
    50: 15000,
}

ENCOUNTER_BONUSES = {
    "most_damage":  250,
    "most_attacks": 250,
    "crit":         100,  # per crit
}


# ─────────────────────────────────────────────
# MILESTONE + STATS DB HELPERS
# ─────────────────────────────────────────────

def get_or_create_player_stats(db, user_id: str) -> dict:
    row = db.table("player_stats").select("*").eq("user_id", user_id).execute()
    if row.data:
        return row.data[0]
    db.table("player_stats").insert({
        "user_id":            user_id,
        "total_encounters":   0,
        "total_damage":       0,
        "total_kill_shots":   0,
        "total_goo_earned":   0,
        "total_crits":        0,
        "total_attacks":      0,
        "current_streak":     0,
        "longest_streak":     0,
        "last_encounter_date": None,
    }).execute()
    return get_or_create_player_stats(db, user_id)


def update_player_stats(db, user_id: str, dmg: int, attacks: int, crits: int,
                        got_kill: bool, goo: int) -> dict:
    """Update all-time stats and streak. Returns updated row."""
    stats = get_or_create_player_stats(db, user_id)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    last = stats.get("last_encounter_date")

    # Streak logic
    if last == today:
        new_streak = stats["current_streak"]  # already attended today
    elif last == (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d"):
        new_streak = stats["current_streak"] + 1  # consecutive day
    else:
        new_streak = 1  # streak broken or first time

    longest = max(stats["longest_streak"], new_streak)

    db.table("player_stats").update({
        "total_encounters":    stats["total_encounters"] + 1,
        "total_damage":        stats["total_damage"] + dmg,
        "total_kill_shots":    stats["total_kill_shots"] + (1 if got_kill else 0),
        "total_goo_earned":    stats["total_goo_earned"] + goo,
        "total_crits":         stats["total_crits"] + crits,
        "total_attacks":       stats["total_attacks"] + attacks,
        "current_streak":      new_streak,
        "longest_streak":      longest,
        "last_encounter_date": today,
    }).eq("user_id", user_id).execute()

    return {**stats, "current_streak": new_streak, "total_kill_shots": stats["total_kill_shots"] + (1 if got_kill else 0)}


def check_and_award_milestones(db, user_id: str, updated_stats: dict) -> list[tuple[str, int]]:
    """
    Check if player just crossed any milestone thresholds.
    Returns list of (description, goo_amount) for each newly unlocked milestone.
    """
    awarded = []

    # Fetch already-awarded milestones
    existing = db.table("awarded_milestones").select("milestone_key").eq("user_id", user_id).execute()
    done = {r["milestone_key"] for r in existing.data}

    streak = updated_stats["current_streak"]
    kills  = updated_stats["total_kill_shots"]

    for days, goo in STREAK_MILESTONES.items():
        key = f"streak_{days}"
        if key not in done and streak >= days:
            db.table("awarded_milestones").insert({"user_id": user_id, "milestone_key": key}).execute()
            db.table("pending_goo").upsert({"user_id": user_id, "amount": goo}, on_conflict="user_id").execute()
            awarded.append((f"🔥 **{days}-Day Streak!**", goo))

    for k, goo in KILL_MILESTONES.items():
        key = f"kills_{k}"
        if key not in done and kills >= k:
            db.table("awarded_milestones").insert({"user_id": user_id, "milestone_key": key}).execute()
            db.table("pending_goo").upsert({"user_id": user_id, "amount": goo}, on_conflict="user_id").execute()
            awarded.append((f"💀 **{k} Kill Shot{'s' if k > 1 else ''}!**", goo))

    return awarded


async def announce_milestones(bot, channel, user_id: int, milestones: list[tuple[str, int]]):
    """DM the player and announce publicly for each milestone."""
    for desc, goo in milestones:
        # Public announcement
        try:
            await channel.send(f"🏆 <@{user_id}> just unlocked {desc} — **{goo:,} $GOO** bonus incoming!")
        except Exception as e:
            print(f"[MILESTONE] Public announce failed: {e}")

        # DM the player
        try:
            user = await bot.fetch_user(user_id)
            await user.send(
                f"🎉 **Milestone Unlocked!**\n\n"
                f"{desc}\n"
                f"**+{goo:,} $GOO** has been added to your balance and will be sent on-chain automatically."
            )
        except Exception as e:
            print(f"[MILESTONE] DM failed for {user_id}: {e}")

# ─────────────────────────────────────────────
# BUTTONS + MODAL
# ─────────────────────────────────────────────

class TeammateSelectView(discord.ui.View):
    """Ephemeral view with a Discord user select menu — shows autocomplete suggestions."""
    def __init__(self, cog):
        super().__init__(timeout=60)
        self.cog = cog
        self.add_item(TeammateSelect(cog))


class TeammateSelect(discord.ui.UserSelect):
    def __init__(self, cog):
        super().__init__(
            placeholder="Search for a teammate...",
            min_values=1,
            max_values=1,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]

        if selected.id == interaction.user.id:
            await interaction.response.send_message("You can't tag yourself!", ephemeral=True)
            return

        if not self.cog.active_encounter:
            await interaction.response.send_message("The encounter already ended!", ephemeral=True)
            return

        await self.cog._process_attack(
            interaction,
            tagged_id=selected.id,
            tagged_name=selected.display_name,
            attack_type=1  # Blaze for team attacks
        )


class AttackButton(discord.ui.Button):
    def __init__(self, attack_type: int):
        label, emoji, min_d, max_d, _ = ATTACK_OPTIONS[attack_type]
        super().__init__(
            label=f"{emoji} {label}",
            style=discord.ButtonStyle.danger if attack_type == 2 else (
                discord.ButtonStyle.primary if attack_type == 0 else discord.ButtonStyle.success
            ),
            custom_id=f"attack_{attack_type}",
        )
        self.attack_type = attack_type

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.cogs.get("EncountersCog")
        if not cog or not cog.active_encounter:
            await interaction.response.send_message(
                "⚠️ No active encounter right now!",
                ephemeral=True
            )
            return
        await cog._process_attack(interaction, tagged_id=None, tagged_name=None, attack_type=self.attack_type)


class TagTeammateButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="🤝 Tag a Friend",
            style=discord.ButtonStyle.primary,
            custom_id="attack_team",
        )

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.cogs.get("EncountersCog")
        if not cog or not cog.active_encounter:
            await interaction.response.send_message(
                "⚠️ No active encounter right now!",
                ephemeral=True
            )
            return
        await interaction.response.send_message(
            "👇 Pick your teammate from the list below:",
            view=TeammateSelectView(cog),
            ephemeral=True
        )


# ─────────────────────────────────────────────
# DISCORD COG
# ─────────────────────────────────────────────

class EncountersCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel_id = int(os.environ["DISCORD_ENCOUNTERS_CHANNEL_ID"])
        self.encounter_role_id = int(os.environ.get("DISCORD_ENCOUNTER_ROLE_ID", "0"))
        self.active_encounter: EncounterState | None = None
        self.encounter_message: discord.Message | None = None
        self._next_encounters: dict = {}
        self._pending_boss: bool = False
        self._attack_cooldowns: dict[int, tuple] = {}  # user_id → (last_attack_time, stun_until)
        self._last_embed_update: float = 0.0  # timestamp of last embed edit
        self.encounter_scheduler.start()
        self.goo_sender.start()

    def cog_unload(self):
        self.encounter_scheduler.cancel()
        self.goo_sender.cancel()

    @tasks.loop(seconds=30)
    async def goo_sender(self):
        """Background loop — sends pending GOO balances on-chain every 30 seconds."""
        try:
            db = get_supabase()
            rows = db.table("goo_balances").select("*").eq("needs_payout", True).gt("balance", 0).execute()
            if not rows.data:
                return

            for row in rows.data:
                user_id = row["user_id"]
                amount = row["balance"]
                try:
                    wallet_row = db.table("linked_wallets").select("wallet_address").eq("user_id", user_id).execute()
                    if not wallet_row.data:
                        continue
                    wallet = wallet_row.data[0]["wallet_address"]

                    # Check opt-in with timeout
                    try:
                        opted = await asyncio.wait_for(asyncio.to_thread(has_opted_in, wallet), timeout=5)
                    except asyncio.TimeoutError:
                        continue
                    if not opted:
                        continue

                    # Send on-chain
                    tx_id = await asyncio.wait_for(
                        asyncio.to_thread(send_goo, wallet, amount),
                        timeout=15
                    )
                    # Mark as sent
                    db.table("goo_balances").update({"balance": 0, "needs_payout": False}).eq("user_id", user_id).execute()
                    print(f"[SENDER] {amount} GOO → {user_id} ({wallet[:8]}...) TxID: {tx_id}")

                except asyncio.TimeoutError:
                    print(f"[SENDER] Timeout for {user_id}, will retry")
                except Exception as e:
                    print(f"[SENDER] Failed for {user_id}: {e}")

        except Exception as e:
            print(f"[SENDER] Loop error: {e}")

    @goo_sender.before_loop
    async def before_sender(self):
        await self.bot.wait_until_ready()

    # ── Scheduler ──────────────────────────────

    @tasks.loop(minutes=1)
    async def encounter_scheduler(self):
        now = datetime.now(timezone.utc)
        if not self._next_encounters:
            self._schedule_next_encounters(now)

        for slot_key in ["am", "pm"]:
            slot = self._next_encounters.get(slot_key)
            if not slot:
                continue

            # 5-minute warning
            warning_time = slot["time"] - timedelta(minutes=5)
            if not slot.get("warned") and now >= warning_time:
                slot["warned"] = True
                asyncio.create_task(self._send_warning(slot.get("is_boss", False)))

            # Spawn
            if not slot.get("fired") and now >= slot["time"]:
                slot["fired"] = True
                asyncio.create_task(self.run_encounter(boss=slot.get("is_boss", False)))
                tomorrow = now + timedelta(days=1)
                self._schedule_slot(slot_key, tomorrow)

    @encounter_scheduler.before_loop
    async def before_scheduler(self):
        await self.bot.wait_until_ready()

    def _schedule_next_encounters(self, now: datetime):
        self._schedule_slot("am", now)
        self._schedule_slot("pm", now)

    def _schedule_slot(self, slot: str, base: datetime):
        db = get_supabase()
        is_boss = should_schedule_boss(db)

        hour   = random.randint(10, 13) if slot == "am" else random.randint(18, 21)
        minute = random.randint(0, 59)
        scheduled = base.replace(hour=hour, minute=minute, second=0, microsecond=0)

        now = datetime.now(timezone.utc)
        if scheduled <= now:
            scheduled += timedelta(days=1)

        self._next_encounters[slot] = {
            "time":    scheduled,
            "is_boss": is_boss,
            "warned":  False,
            "fired":   False,
        }

        label = "👹 BOSS" if is_boss else "👾 standard"
        print(f"[ENCOUNTERS] Next {slot.upper()} ({label}) scheduled: {scheduled.strftime('%Y-%m-%d %H:%M UTC')}")

    async def _send_warning(self, is_boss: bool = False):
        channel = self.bot.get_channel(self.channel_id)
        if not channel:
            return
        role_ping = f"<@&{self.encounter_role_id}> " if self.encounter_role_id else ""
        if is_boss:
            await channel.send(
                f"{role_ping}🚨 **BOSS ENCOUNTER IN 5 MINUTES!**\n"
                "A powerful MONSTR is approaching. This is a rare event — 3x GOO on the line. Get ready! 👹"
            )
        else:
            await channel.send(
                f"{role_ping}⚠️ **A MONSTR is approaching — 5 minutes!** Get ready to attack! 👾"
            )

    # ── Encounter Runner ───────────────────────

    async def run_encounter(self, boss: bool = False):
        if self.active_encounter:
            print("[ENCOUNTERS] Already active, skipping.")
            return

        channel = self.bot.get_channel(self.channel_id)
        if not channel:
            print(f"[ERROR] Channel {self.channel_id} not found.")
            return

        monstrs = await pick_wave(boss=boss)
        self.active_encounter = EncounterState(monstrs, is_boss=boss)

        role_ping = f"<@&{self.encounter_role_id}> " if self.encounter_role_id else ""
        prefix = f"{role_ping}👹 **BOSS MONSTR HAS APPEARED!** 15,000 $GOO up for grabs!" if boss else f"{role_ping}⚠️ **Wave 1/3 begins!** 3 MONSTRs — 5,000 $GOO up for grabs!"

        # Post first wave embed
        await self._post_wave_embed(channel, prefix)

        # Run timed encounter — handle wave transitions
        end_time = asyncio.get_event_loop().time() + ENCOUNTER_DURATION
        while asyncio.get_event_loop().time() < end_time:
            await asyncio.sleep(1)
            state = self.active_encounter
            if state and not state.alive:
                has_next = state.next_wave()
                if has_next:
                    await self._post_wave_embed(
                        channel,
                        f"💥 **Wave {state.wave_num}/{state.total_waves}!** {state.monstr['name']} appears!"
                    )
                else:
                    break  # All waves cleared

        await self._close_encounter(channel)

    async def _close_encounter(self, channel: discord.TextChannel):
        state = self.active_encounter
        if not state:
            print("[CLOSE] No active encounter, skipping.")
            return

        print(f"[CLOSE] Closing encounter for {state.monstr['name']} — {len(state.damage_dealt)} attackers")
        payouts = state.calculate_payouts()
        print(f"[CLOSE] Payouts calculated: {payouts}")

        # ── In-encounter bonuses ──
        if state.damage_dealt:
            top_damage_uid = max(state.damage_dealt, key=state.damage_dealt.get)
            top_attacks_uid = max(state.attack_counts, key=state.attack_counts.get) if state.attack_counts else None
            for uid in state.damage_dealt:
                bonus = 0
                if uid == top_damage_uid:
                    bonus += ENCOUNTER_BONUSES["most_damage"]
                if uid == top_attacks_uid:
                    bonus += ENCOUNTER_BONUSES["most_attacks"]
                crit_bonus = state.crit_counts.get(uid, 0) * ENCOUNTER_BONUSES["crit"]
                bonus += crit_bonus
                if bonus > 0:
                    payouts[uid] = payouts.get(uid, 0) + bonus
                    print(f"[BONUS] {uid} gets +{bonus} GOO (damage:{uid==top_damage_uid} attacks:{uid==top_attacks_uid} crits:{state.crit_counts.get(uid,0)})")

        # ── Clear active encounter immediately so next battle can start ──
        self.active_encounter = None
        self.encounter_message = None
        self._attack_cooldowns.clear()

        # ── Credit GOO balances instantly (no chain involvement) ──
        await payout_all(state, payouts, channel)
        print("[CLOSE] GOO credited to balances — background sender handles on-chain")

        # ── Show escaped state on main embed if time ran out ──
        all_waves_done = state.wave_index >= state.total_waves
        if not all_waves_done and self.encounter_message:
            try:
                escaped_embed = discord.Embed(
                    title="💨 They got away...",
                    description=(
                        "The MONSTRs slipped back into the GOO.\n"
                        "Better luck next time — encounters run twice a day!"
                    ),
                    color=0x555555,
                )
                if state.monstr.get("image_bytes"):
                    escaped_embed.set_thumbnail(url="attachment://monstr.jpg")
                await self.encounter_message.edit(embed=escaped_embed, view=None)
            except Exception as e:
                print(f"[WARN] Could not edit escaped message: {e}")

        # ── Post results immediately ──
        print("[CLOSE] Sending results embed...")
        results_embed = self._build_results_embed(state, payouts)
        await channel.send(embed=results_embed)
        print("[CLOSE] Results embed sent")

        # ── Background tasks: DB writes, stats, milestones (non-blocking) ──
        # ── Background tasks: DB writes, stats, milestones (non-blocking) ──
        async def _background_tasks():
            try:
                print("[CLOSE] Writing to DB...")
                await asyncio.to_thread(log_encounter_to_db, state, payouts)
                print("[CLOSE] DB write OK")
                await asyncio.to_thread(update_weekly_stats, state)
                print("[CLOSE] Weekly stats OK")
            except Exception as e:
                print(f"[ERROR] DB write: {e}")

            milestone_tasks = []
            try:
                db = get_supabase()
                for uid, dmg in state.damage_dealt.items():
                    got_kill = uid == state.kill_shotter
                    attacks = state.attack_counts.get(uid, 0)
                    crits = state.crit_counts.get(uid, 0)
                    goo = payouts.get(uid, 0)
                    updated = update_player_stats(db, str(uid), dmg, attacks, crits, got_kill, goo)
                    milestones = check_and_award_milestones(db, str(uid), updated)
                    if milestones:
                        milestone_tasks.append((uid, milestones))
            except Exception as e:
                print(f"[ERROR] Stats/milestone update: {e}")

            for uid, milestones in milestone_tasks:
                await announce_milestones(self.bot, channel, uid, milestones)

            # ── Award BP to teams ──
            try:
                db_bp = get_supabase()
                # Fetch all linked wallets for attackers in one pass
                attacker_ids = [str(uid) for uid in state.damage_dealt]
                wallet_rows  = db_bp.table("linked_wallets").select("user_id,wallet_address").in_("user_id", attacker_ids).execute()
                wallet_map   = {r["user_id"]: r["wallet_address"] for r in wallet_rows.data}

                for uid, dmg in state.damage_dealt.items():
                    got_kill = uid == state.kill_shotter
                    crits    = state.crit_counts.get(uid, 0)

                    # Live holdings count for BP multiplier
                    wallet = wallet_map.get(str(uid))
                    holdings = 0
                    if wallet:
                        try:
                            holdings = await asyncio.wait_for(
                                asyncio.to_thread(fetch_monstr_holdings, wallet),
                                timeout=8
                            )
                        except Exception:
                            holdings = 0

                    bp_earned, old_tier, new_tier = award_bp(
                        str(uid),
                        participated=True,
                        got_kill_shot=got_kill,
                        crits=crits,
                        is_boss=state.is_boss,
                        holdings=holdings,
                    )
                    if old_tier != new_tier:
                        label = next(t[1] for t in TIERS if t[0] == new_tier)
                        await channel.send(
                            f"⚡ <@{uid}> **TIER UP!** Your team just reached **{label}**! "
                            f"Attack power and stun resistance increased."
                        )
            except Exception as e:
                print(f"[ERROR] BP award: {e}")

            print("[CLOSE] Encounter fully closed")

        asyncio.create_task(_background_tasks())

    # ── Embeds ─────────────────────────────────

    def _build_encounter_embed(self, state: EncounterState) -> discord.Embed:
        hp_bar = self._hp_bar(state.hp, state.max_hp)
        goo_total = TOTAL_GOO * (BOSS_MULTIPLIER if state.is_boss else 1)
        color = 0xff0000 if state.is_boss else 0x00ff99

        if state.is_boss:
            title = f"👹 BOSS: {state.monstr['name']}!"
            wave_line = ""
        else:
            title = f"👾 {state.monstr['name']} — Wave {state.wave_num}/{state.total_waves}"
            wave_line = f"⚔️ Wave **{state.wave_num}** of **{state.total_waves}**\n"

        embed = discord.Embed(
            title=title,
            description=(
                f"{hp_bar}\n\n"
                f"{wave_line}"
                f"🏆 **{goo_total:,} $GOO** shared across all waves\n\n"
                f"Choose your attack:"
            ),
            color=color,
        )
        if state.monstr.get("image_bytes"):
            embed.set_image(url="attachment://monstr.jpg")
        else:
            embed.set_image(url=state.monstr.get("image_url", ""))
        embed.set_footer(text=f"ASA #{state.monstr['asa_id']}{'  — BOSS' if state.is_boss else ''}")
        return embed

    def _build_defeated_embed(self, state: EncounterState) -> discord.Embed:
        """Shown immediately when HP hits zero — replaces the live battle embed."""
        color = 0xff0000 if state.is_boss else 0xff4444
        title = f"☠️ {state.monstr['name']} has been defeated!"

        embed = discord.Embed(
            title=title,
            description=(
                "\u2588" * 20 + " 💀 **DEFEATED**\n\n"
                "Calculating payouts and sending $GOO...\n"
                "Results incoming!"
            ),
            color=color,
        )
        if state.monstr.get("image_bytes"):
            embed.set_thumbnail(url="attachment://monstr.jpg")
        else:
            embed.set_thumbnail(url=state.monstr.get("image_url", ""))
        embed.set_footer(text=f"ASA #{state.monstr['asa_id']}")
        return embed

    def _build_results_embed(self, state: EncounterState, payouts: dict[int, int]) -> discord.Embed:
        all_waves_done = state.wave_index >= state.total_waves
        defeated = all_waves_done or not state.alive
        color = 0xff0000 if state.is_boss and defeated else (0xff4444 if defeated else 0x888888)
        ESCAPE_MSGS = [
            "🏃 The MONSTRs said 'not today' and bounced.",
            "😤 They survived. They'll be back. And they're mad.",
            "🌀 The MONSTRs dissolved into the GOO. Cowards.",
            "💨 Gone. Just a faint whiff of $GOO left behind.",
            "🙈 They ran so fast they left their companion behind.",
        ]
        import random as _r
        if state.is_boss:
            title = f"💀 BOSS {state.monstr['name']} DEFEATED!" if defeated else f"⏰ Boss escaped... embarrassing."
        elif all_waves_done:
            title = f"💀 All {state.total_waves} MONSTRs defeated!"
        elif defeated:
            title = f"💀 Wave {state.wave_num} cleared!"
        else:
            title = _r.choice(ESCAPE_MSGS)

        sorted_payouts = sorted(payouts.items(), key=lambda x: x[1], reverse=True)
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, (uid, goo) in enumerate(sorted_payouts[:5]):
            medal = medals[i] if i < 3 else "▪️"
            dmg = state.damage_dealt.get(uid, 0)
            lines.append(f"{medal} <@{uid}> — **{goo:,} $GOO** ({dmg} dmg)")

        # Show all wave first strikers and kill shotters
        all_fs = list(state.wave_first_strikers) + ([state.first_striker] if state.first_striker else [])
        all_ks = list(state.wave_kills) + ([state.kill_shotter] if state.kill_shotter else [])
        seen_fs = set()
        seen_ks = set()
        fs_lines = []
        ks_lines = []
        for fs in all_fs:
            if fs and fs not in seen_fs:
                fs_lines.append(f"<@{fs}>")
                seen_fs.add(fs)
        for ks in all_ks:
            if ks and ks not in seen_ks:
                ks_lines.append(f"<@{ks}>")
                seen_ks.add(ks)
        if fs_lines:
            lines.append(f"\n⚡ First Strike: {', '.join(fs_lines)}")
        if ks_lines:
            lines.append(f"💥 Kill Shot: {', '.join(ks_lines)}")
        if state.crit_count:
            lines.append(f"🎯 Critical hits: **{state.crit_count}**")

        embed = discord.Embed(
            title=title,
            description="\n".join(lines) if lines else "Nobody attacked...",
            color=color,
        )
        if state.monstr.get("image_bytes"):
            embed.set_thumbnail(url="attachment://monstr.png")
        else:
            embed.set_thumbnail(url=state.monstr.get("image_url", ""))
        embed.set_footer(text=f"Total: {sum(payouts.values()):,} $GOO | Sending to wallets shortly...")
        return embed

    @staticmethod
    def _hp_bar(hp: int, max_hp: int, length: int = 20) -> str:
        filled = int((hp / max_hp) * length)
        bar = "█" * filled + "░" * (length - filled)
        pct = int((hp / max_hp) * 100)
        return f"`{bar}` {pct}% HP"

    # ── WAVE EMBED POSTER ─────────────────────────

    async def _post_wave_embed(self, channel: discord.TextChannel, prefix: str):
        """Post or update the encounter embed for a new wave."""
        # Disable buttons on the previous wave's message before posting the new one
        if self.encounter_message:
            try:
                disabled_view = discord.ui.View()
                for item in self._build_attack_view().children:
                    item.disabled = True
                    disabled_view.add_item(item)
                await self.encounter_message.edit(view=disabled_view)
            except Exception as e:
                print(f"[WAVE] Could not disable old wave buttons: {e}")

        state = self.active_encounter
        embed = self._build_encounter_embed(state)
        view = self._build_attack_view()

        image_bytes = state.monstr.get("image_bytes")
        file = None
        if image_bytes:
            try:
                import io
                from PIL import Image
                img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
                img.thumbnail((400, 400), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=70, optimize=True)
                buf.seek(0)
                size_kb = buf.getbuffer().nbytes // 1024
                print(f"[IMAGE] Resized to {size_kb}KB")
                file = discord.File(buf, filename="monstr.jpg")
                embed.set_image(url="attachment://monstr.jpg")
            except Exception as e:
                print(f"[IMAGE] Resize failed: {e}")

        if file:
            self.encounter_message = await channel.send(prefix, embed=embed, view=view, file=file)
        else:
            self.encounter_message = await channel.send(prefix, embed=embed, view=view)
        self._last_embed_update = 0.0

    # ── ATTACK PROCESSOR ───────────────────────

    async def _process_attack(self, interaction: discord.Interaction, tagged_id: int | None, tagged_name: str | None, attack_type: int = 1):
        state = self.active_encounter
        if not state:
            await interaction.response.send_message("The encounter has already ended.", ephemeral=True)
            return

        user_id = interaction.user.id

        # Cooldown check — base 15s, extended to 45s if stunned by counter-attack
        import time
        now = time.time()
        cooldown_data = self._attack_cooldowns.get(user_id, (0, 0))
        last, stun_until = cooldown_data if isinstance(cooldown_data, tuple) else (cooldown_data, 0)
        if now < stun_until:
            remaining = stun_until - now
            await interaction.response.send_message(
                f"😵 **You've been counter-attacked!** Stunned for **{remaining:.0f}s** more...",
                ephemeral=True
            )
            return
        remaining = BASE_COOLDOWN - (now - last)
        if remaining > 0:
            await interaction.response.send_message(
                f"⏳ You can attack again in **{remaining:.0f}s**!",
                ephemeral=True
            )
            return
        self._attack_cooldowns[user_id] = (now, 0)

        # Guard against attacks from a previous wave's message buttons
        if (
            self.encounter_message
            and hasattr(interaction, "message")
            and interaction.message
            and interaction.message.id != self.encounter_message.id
        ):
            await interaction.response.send_message(
                "⚔️ That wave is already over — attack the current MONSTR!", ephemeral=True
            )
            return

        result = state.register_attack(user_id, tagged_id, attack_type=attack_type)

        if "error" in result:
            await interaction.response.send_message("The encounter has already ended.", ephemeral=True)
            return

        events = result["events"]
        lines = []

        # Ephemeral damage feedback
        attack_name = ATTACK_OPTIONS[attack_type][0]
        attack_emoji = ATTACK_OPTIONS[attack_type][1]
        if result["is_crit"]:
            lines.append(f"⚡ **CRITICAL HIT!** {attack_emoji} {attack_name} dealt **{result['damage']} damage!**")
        else:
            lines.append(f"{attack_emoji} **{attack_name}** dealt **{result['damage']} damage!**")

        lines.append(self._hp_bar(result["hp_remaining"], result["max_hp"]))

        # XP / stats display
        total_dmg = state.damage_dealt.get(user_id, 0)
        total_damage_all = sum(state.damage_dealt.values())
        my_share_pct = int((total_dmg / total_damage_all) * 100) if total_damage_all > 0 else 0
        goo_total = TOTAL_GOO * (BOSS_MULTIPLIER if state.is_boss else 1)
        est_goo = int((total_dmg / total_damage_all) * (goo_total * 0.6)) if total_damage_all > 0 else 0
        rank = sorted(state.damage_dealt.values(), reverse=True).index(total_dmg) + 1
        lines.append(
            f"\n📊 **Your stats:** {total_dmg} dmg ({my_share_pct}% of total) · "
            f"#{rank} on board · ~{est_goo:,} $GOO so far"
        )
        lines.append(f"⚔️ Wave {state.wave_num}/{state.total_waves}")

        if "first_strike" in events:
            lines.append("🥊 **First Strike!** +500 GOO bonus.")
        if "kill_shot" in events:
            lines.append("💀 **Kill Shot landed!** Bonus incoming.")
        if tagged_name:
            lines.append(f"🤝 Teamed up with **{tagged_name}**!")

        # Counter-attack — apply stun if triggered (stun resist can block it)
        if result.get("is_counter") and state.alive:
            import time as _ctime
            if roll_stun_resist(user_id):
                lines.append(f"\n🛡️ **Counter-attack blocked!** Your team's experience shrugged it off!")
            else:
                stun_end = _ctime.time() + COUNTER_COOLDOWN
                self._attack_cooldowns[user_id] = (_ctime.time(), stun_end)
                lines.append(f"\n💢 **COUNTER-ATTACK!** {state.monstr['name']} strikes back — you're stunned for {COUNTER_COOLDOWN}s!")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

        # Public announcements for hype moments
        channel = interaction.channel
        if "first_strike" in events:
            await channel.send(f"🥊 **First Strike!** <@{user_id}> drew first blood on Wave {state.wave_num}!")
        if result["is_crit"] and "first_strike" not in events:
            await channel.send(f"⚡ **CRITICAL HIT!** <@{user_id}> landed a massive blow!")
        # Counter-attack is ephemeral only — no public announcement

        # Update main embed on every attack, respecting Discord's 1/sec rate limit
        import time as _time
        if self.encounter_message:
            try:
                if not state.alive:
                    # Always update immediately on defeat
                    defeated_embed = self._build_defeated_embed(state)
                    await self.encounter_message.edit(embed=defeated_embed, view=None)
                    self._last_embed_update = _time.time()
                elif _time.time() - self._last_embed_update >= 1.1:
                    # Update if at least 1.1 seconds since last edit
                    updated_embed = self._build_encounter_embed(state)
                    view = self._build_attack_view()
                    await self.encounter_message.edit(embed=updated_embed, view=view)
                    self._last_embed_update = _time.time()
            except discord.HTTPException as e:
                if e.status == 429:
                    print(f"[WARN] Rate limited on embed update, skipping")
                else:
                    print(f"[WARN] embed update: {e}")
            except Exception as e:
                print(f"[WARN] embed update: {e}")

    # ── BUTTONS ────────────────────────────────

    def _build_attack_view(self) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        for i in range(len(ATTACK_OPTIONS)):
            view.add_item(AttackButton(attack_type=i))
        view.add_item(TagTeammateButton())
        return view

    # ── /leaderboard ───────────────────────────

    @discord.app_commands.command(name="leaderboard", description="MONSTR Encounters leaderboard")
    @discord.app_commands.describe(board="Weekly or all-time leaderboard")
    @discord.app_commands.choices(board=[
        discord.app_commands.Choice(name="Weekly", value="weekly"),
        discord.app_commands.Choice(name="All-Time", value="alltime"),
    ])
    async def leaderboard(self, interaction: discord.Interaction, board: str = "weekly"):
        await interaction.response.defer()
        try:
            db = get_supabase()
            medals = ["🥇","🥈","🥉","4️⃣","5️⃣"]

            def fmt(rows, key, label):
                ranked = sorted(rows, key=lambda r: r.get(key, 0), reverse=True)[:5]
                if not ranked:
                    return "No data yet"
                return "\n".join(
                    f"{medals[i]} <@{r['user_id']}> — **{r.get(key,0):,} {label}**"
                    for i, r in enumerate(ranked)
                )

            if board == "weekly":
                week = _week_start()
                rows = db.table("weekly_stats").select("*").eq("week_start", week).execute().data
                if not rows:
                    await interaction.followup.send("No encounters recorded this week yet!", ephemeral=True)
                    return
                embed = discord.Embed(title=f"📊 Weekly Leaderboard — w/c {week}", color=0x9b59b6)
                embed.add_field(name="⚔️ Top Damage", value=fmt(rows, "total_damage", "dmg"), inline=False)
                embed.add_field(name="💀 Kill Shots", value=fmt(rows, "kill_shots", "kills"), inline=False)
                embed.add_field(name="🎮 Participation", value=fmt(rows, "encounters_joined", "encounters"), inline=False)
                embed.set_footer(text="Resets every Monday · 14 encounters per week")
            else:
                rows = db.table("player_stats").select("*").execute().data
                if not rows:
                    await interaction.followup.send("No all-time stats yet!", ephemeral=True)
                    return
                embed = discord.Embed(title="🏆 All-Time Leaderboard", color=0xf1c40f)
                embed.add_field(name="⚔️ Total Damage", value=fmt(rows, "total_damage", "dmg"), inline=False)
                embed.add_field(name="💀 Kill Shots", value=fmt(rows, "total_kill_shots", "kills"), inline=False)
                embed.add_field(name="🧪 GOO Earned", value=fmt(rows, "total_goo_earned", "GOO"), inline=False)
                embed.add_field(name="🔥 Longest Streak", value=fmt(rows, "longest_streak", "days"), inline=False)
                embed.set_footer(text="All-time records across all encounters")

            await interaction.followup.send(embed=embed)
        except Exception as e:
            print(f"[ERROR] leaderboard: {e}")
            await interaction.followup.send("Couldn\'t load the leaderboard right now.", ephemeral=True)

    # ── TEST COMMANDS (remove before going live) ──

    @discord.app_commands.command(name="testencounter", description="[TEST] Fire a standard encounter now")
    async def test_encounter(self, interaction: discord.Interaction):
        await interaction.response.send_message("🧪 Spawning test encounter...", ephemeral=True)
        asyncio.create_task(self.run_encounter(boss=False))

    @discord.app_commands.command(name="testboss", description="[TEST] Fire a boss encounter now")
    async def test_boss(self, interaction: discord.Interaction):
        await interaction.response.send_message("🧪 Spawning test boss encounter...", ephemeral=True)
        asyncio.create_task(self.run_encounter(boss=True))


# ─────────────────────────────────────────────
# WALLET COG
# ─────────────────────────────────────────────

class WalletCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @discord.app_commands.command(name="link", description="Link your Algorand wallet to receive $GOO rewards")
    @discord.app_commands.describe(wallet="Your Algorand wallet address")
    async def link_wallet(self, interaction: discord.Interaction, wallet: str):
        if len(wallet) != 58 or not wallet.isalnum():
            await interaction.response.send_message(
                "⚠️ That doesn't look like a valid Algorand address. Please double-check and try again.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            db = get_supabase()
            asset_id = os.environ["GOO_ASSET_ID"]

            # Gate: must hold at least one MONSTR to link
            holdings = await asyncio.wait_for(
                asyncio.to_thread(fetch_monstr_holdings, wallet),
                timeout=15
            )
            if holdings == 0:
                await interaction.followup.send(
                    "❌ **No MONSTRs detected in that wallet.**\n\n"
                    "You need to hold at least one MONSTR NFT to participate in $GOO Encounters.\n"
                    "Pick one up and try again!",
                    ephemeral=True
                )
                return

            db.table("linked_wallets").upsert({
                "user_id":        str(interaction.user.id),
                "wallet_address": wallet,
            }, on_conflict="user_id").execute()

            opted_in = has_opted_in(wallet)

            # Check + pay out any pending GOO
            pending_row = db.table("pending_goo").select("amount").eq("user_id", str(interaction.user.id)).execute()
            pending = pending_row.data[0]["amount"] if pending_row.data else 0

            if pending > 0 and opted_in:
                try:
                    tx_id = await asyncio.to_thread(send_goo, wallet, pending)
                    db.table("pending_goo").delete().eq("user_id", str(interaction.user.id)).execute()
                    pending_msg = f"\n\n💸 **{pending:,} pending $GOO sent!** TxID: `{tx_id[:16]}...`"
                except Exception as e:
                    pending_msg = f"\n\n⚠️ Had {pending:,} pending GOO but send failed. Will retry next encounter."
                    print(f"[ERROR] pending payout on link: {e}")
            elif pending > 0:
                pending_msg = f"\n\n💰 You have **{pending:,} $GOO** waiting — opt in to $GOO to receive it."
            else:
                pending_msg = ""

            # Assign encounter role on first link
            role_line = ""
            try:
                role_id = int(os.environ.get("DISCORD_ENCOUNTER_ROLE_ID", "0"))
                if role_id and interaction.guild:
                    role = interaction.guild.get_role(role_id)
                    member = interaction.guild.get_member(interaction.user.id)
                    if role and member and role not in member.roles:
                        await member.add_roles(role, reason="Linked wallet to $GOO Warden")
                        role_line = f"\n🎮 You've been given the <@&{role_id}> role — you'll be pinged for every encounter!"
            except Exception as e:
                print(f"[ROLE] Failed to assign role: {e}")

            if opted_in:
                msg = (
                    f"✅ **Wallet linked!**\n\n"
                    f"👛 `{wallet[:8]}...{wallet[-4:]}`\n"
                    f"You're all set — $GOO rewards will be sent automatically after each encounter."
                    + role_line
                    + pending_msg
                )
            else:
                msg = (
                    f"⚠️ **Wallet linked, but you need to opt in to $GOO first.**\n\n"
                    f"Open **Pera Wallet** → search ASA ID `{asset_id}` → **Add Asset**\n\n"
                    f"Your rewards will be held until you opt in."
                    + role_line
                    + pending_msg
                )

            await interaction.followup.send(msg, ephemeral=True)

        except Exception as e:
            print(f"[ERROR] link_wallet: {e}")
            await interaction.followup.send("Something went wrong. Try again.", ephemeral=True)

    @discord.app_commands.command(name="stats", description="View your all-time MONSTR Encounters stats")
    async def stats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            db = get_supabase()
            user_id = str(interaction.user.id)
            s = get_or_create_player_stats(db, user_id)

            # Next streak milestone
            streak = s["current_streak"]
            next_streak = next((d for d in sorted(STREAK_MILESTONES) if d > streak), None)
            streak_line = (
                f"{streak} days (next milestone: {next_streak} days → {STREAK_MILESTONES[next_streak]:,} GOO)"
                if next_streak else f"{streak} days 🏆 Max milestone reached!"
            )

            # Next kill milestone
            kills = s["total_kill_shots"]
            next_kill = next((k for k in sorted(KILL_MILESTONES) if k > kills), None)
            kill_line = (
                f"{kills} (next milestone: {next_kill} kills → {KILL_MILESTONES[next_kill]:,} GOO)"
                if next_kill else f"{kills} 🏆 Max milestone reached!"
            )

            embed = discord.Embed(title=f"📊 {interaction.user.display_name}'s Stats", color=0x9b59b6)
            embed.add_field(name="🎮 Encounters", value=str(s["total_encounters"]), inline=True)
            embed.add_field(name="⚔️ Total Attacks", value=str(s["total_attacks"]), inline=True)
            embed.add_field(name="💥 Total Damage", value=f"{s['total_damage']:,}", inline=True)
            embed.add_field(name="💀 Kill Shots", value=kill_line, inline=False)
            embed.add_field(name="⚡ Critical Hits", value=str(s["total_crits"]), inline=True)
            embed.add_field(name="🧪 GOO Earned", value=f"{s['total_goo_earned']:,}", inline=True)
            embed.add_field(name="🔥 Streak", value=streak_line, inline=False)
            embed.add_field(name="📈 Longest Streak", value=f"{s['longest_streak']} days", inline=True)

            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            print(f"[ERROR] stats: {e}")
            await interaction.followup.send("Couldn't fetch your stats right now.", ephemeral=True)

    @discord.app_commands.command(name="balance", description="Check your $GOO status")
    async def balance(self, interaction: discord.Interaction):
        try:
            db = get_supabase()
            user_id = str(interaction.user.id)

            wallet_row  = db.table("linked_wallets").select("wallet_address").eq("user_id", user_id).execute()
            pending_row = db.table("pending_goo").select("amount").eq("user_id", user_id).execute()

            wallet_str = (
                f"`{wallet_row.data[0]['wallet_address'][:8]}...{wallet_row.data[0]['wallet_address'][-4:]}`"
                if wallet_row.data else "Not linked — use `/link`"
            )
            pending = pending_row.data[0]["amount"] if pending_row.data else 0

            lines = [f"👛 **Wallet:** {wallet_str}"]
            if pending > 0:
                lines.append(f"⏳ **Pending $GOO:** {pending:,} (opt in to $GOO ASA to receive)")
            else:
                lines.append("✅ No pending GOO — all rewards sent on-chain automatically.")

            await interaction.response.send_message("\n".join(lines), ephemeral=True)
        except Exception as e:
            print(f"[ERROR] balance: {e}")
            await interaction.response.send_message("Couldn't fetch your status right now.", ephemeral=True)


# ─────────────────────────────────────────────
# TEAMS COG
# ─────────────────────────────────────────────

class TeamsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── /team_setup ──────────────────────────

    @discord.app_commands.command(
        name="team_setup",
        description="Create or update your MONSTR team name and avatar"
    )
    @discord.app_commands.describe(
        team_name="Your team name (e.g. 'The Rot Squad')",
        avatar_asa="ASA ID of your favourite MONSTR (e.g. 3294386711)",
    )
    async def team_setup(
        self,
        interaction: discord.Interaction,
        team_name: str,
        avatar_asa: str,
    ):
        await interaction.response.defer(ephemeral=True)
        try:
            db = get_supabase()
            user_id = str(interaction.user.id)

            if len(team_name.strip()) < 2 or len(team_name.strip()) > 32:
                await interaction.followup.send(
                    "⚠️ Team name must be between 2 and 32 characters.", ephemeral=True
                )
                return

            avatar_asa = avatar_asa.strip()
            if not avatar_asa.isdigit():
                await interaction.followup.send(
                    "⚠️ ASA ID must be a number (e.g. `3294386711`).", ephemeral=True
                )
                return

            await interaction.followup.send(
                "🔍 Fetching your MONSTR image — one moment...", ephemeral=True
            )

            image_url = await asyncio.to_thread(fetch_avatar_url, avatar_asa)

            get_or_create_team(db, user_id)
            db.table("monstr_teams").update({
                "team_name":        team_name.strip(),
                "avatar_asa_id":    avatar_asa,
                "avatar_image_url": image_url or "",
                "updated_at":       datetime.now(timezone.utc).isoformat(),
            }).eq("user_id", user_id).execute()

            team = get_team(db, user_id)
            _, tier_label, _, _ = resolve_tier(team.get("total_bp", 0))

            embed = discord.Embed(
                title="✅ Team Updated!",
                description=(
                    f"**{team_name.strip()}**\n"
                    f"Tier: {tier_label}\n"
                    f"Avatar MONSTR: ASA `{avatar_asa}`"
                ),
                color=0x00ff99,
            )
            if image_url:
                embed.set_thumbnail(url=image_url)
            else:
                embed.set_footer(text="⚠️ Couldn't load image — avatar will appear when available.")

            await interaction.edit_original_response(content=None, embed=embed)

        except Exception as e:
            print(f"[ERROR] team_setup: {e}")
            await interaction.followup.send("Something went wrong. Try again.", ephemeral=True)

    # ── /rank ─────────────────────────────────

    @discord.app_commands.command(
        name="rank",
        description="View your MONSTR team's Battle Points rank"
    )
    async def rank(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            db = get_supabase()
            user_id = str(interaction.user.id)

            team = get_team(db, user_id)

            # Live holdings count (needed for both registered and unregistered)
            wallet_row = db.table("linked_wallets").select("wallet_address").eq("user_id", user_id).execute()
            holdings = 0
            if wallet_row.data:
                wallet = wallet_row.data[0]["wallet_address"]
                holdings = await asyncio.to_thread(fetch_monstr_holdings, wallet)

            from monstr_teams import get_holdings_multiplier, HOLDINGS_MULTIPLIERS
            holdings_mult = get_holdings_multiplier(holdings)
            next_holdings_tier = next(
                (f"{mult}x BP at {threshold}+ MONSTRs" for threshold, mult in reversed(HOLDINGS_MULTIPLIERS) if holdings < threshold),
                None
            )

            # Unregistered — show banked BP nudge
            if not team or not team.get("team_name"):
                bp = team.get("total_bp", 0) if team else 0
                _, tier_label, _, _ = resolve_tier(bp)
                embed = discord.Embed(
                    title="🧟 You don't have a team name yet!",
                    description=(
                        f"You have **{bp:,} BP** banked from your encounters so far.\n\n"
                        f"Use `/team_setup` to claim your name, pick your avatar MONSTR, "
                        f"and appear on the `/bp_leaderboard`."
                    ),
                    color=0xf39c12,
                )
                embed.add_field(name="⚡ Banked BP",       value=f"**{bp:,}**  •  {tier_label}",                     inline=True)
                embed.add_field(name="📦 MONSTR Holdings", value=f"**{holdings}**  •  **{holdings_mult}x** BP mult", inline=True)
                if next_holdings_tier:
                    embed.set_footer(text=f"Hold more MONSTRs to earn BP faster — {next_holdings_tier}")
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            bp = team.get("total_bp", 0)
            tier_key, tier_label, atk_mult, stun_resist = resolve_tier(bp)
            next_info = next_tier_info(tier_key)

            # Rank position
            all_teams = db.table("monstr_teams").select("user_id,total_bp").order("total_bp", desc=True).execute()
            rank_pos = next(
                (i + 1 for i, t in enumerate(all_teams.data) if t["user_id"] == user_id),
                len(all_teams.data)
            )

            team_name  = team.get("team_name") or "Unnamed Team"
            streak     = team.get("streak_days", 0)
            wins       = team.get("encounters_won", 0)
            played     = team.get("encounters_played", 0)
            avatar_url = team.get("avatar_image_url", "")

            if next_info:
                next_label, next_bp = next_info
                progress_line = f"{next_bp - bp:,} BP to {next_label}"
            else:
                progress_line = "Max tier reached 🏆"

            footer_parts = [progress_line]
            if next_holdings_tier:
                footer_parts.append(next_holdings_tier)

            embed = discord.Embed(title=f"🧟 {team_name}", color=0x9b59b6)
            embed.add_field(name="🏆 Rank",            value=f"**#{rank_pos}**",                          inline=True)
            embed.add_field(name="⚡ Battle Points",   value=f"**{bp:,} BP**  •  {tier_label}",           inline=True)
            embed.add_field(name="\u200b",              value="\u200b",                                    inline=True)
            embed.add_field(name="🔥 Daily streak",    value=f"**{streak} day{'s' if streak != 1 else ''}**", inline=True)
            embed.add_field(name="⚔️ Total wins",      value=f"**{wins}**",                               inline=True)
            embed.add_field(name="🎮 Total played",    value=f"**{played}**",                             inline=True)
            embed.add_field(name="📦 MONSTR Holdings", value=f"**{holdings}**  •  **{holdings_mult}x** BP", inline=True)
            embed.add_field(name="⚔️ Atk Bonus",       value=f"**+{int((atk_mult - 1) * 100)}%**",       inline=True)
            embed.add_field(name="🛡️ Stun Resist",     value=f"**{stun_resist}%**",                      inline=True)
            embed.set_footer(text="  •  ".join(footer_parts))

            if avatar_url:
                embed.set_thumbnail(url=avatar_url)

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            print(f"[ERROR] rank: {e}")
            await interaction.followup.send("Couldn't fetch your rank right now.", ephemeral=True)

    # ── /info ─────────────────────────────────

    @discord.app_commands.command(
        name="info",
        description="List all available $GOO Warden bot commands"
    )
    async def info(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        embed = discord.Embed(
            title="🧟 $GOO Warden — Command List",
            description="Everything you can do with the MONSTRS Encounters bot.",
            color=0x9b59b6,
        )

        embed.add_field(name="​", value="**⚔️ Encounters**", inline=False)
        embed.add_field(
            name="`/leaderboard`",
            value="View weekly or all-time encounter stats — damage, kill shots, and participation.",
            inline=False
        )
        embed.add_field(
            name="`/stats`",
            value="View your personal all-time encounter stats.",
            inline=False
        )

        embed.add_field(name="​", value="**🧟 Teams & Battle Points**", inline=False)
        embed.add_field(
            name="`/team_setup`",
            value="Create or update your team — set your name and choose an avatar MONSTR by ASA ID.",
            inline=False
        )
        embed.add_field(
            name="`/rank`",
            value="View your team card — BP, tier, streak, wins, holdings count, attack bonus, and stun resist.",
            inline=False
        )
        embed.add_field(
            name="`/bp_leaderboard`",
            value="Top 10 teams on the server ranked by Battle Points.",
            inline=False
        )

        embed.add_field(name="​", value="**💧 $GOO & Wallet**", inline=False)
        embed.add_field(
            name="`/link`",
            value="Link your Algorand wallet to receive $GOO rewards from encounters.",
            inline=False
        )
        embed.add_field(
            name="`/balance`",
            value="Check your current $GOO balance and any pending rewards.",
            inline=False
        )

        embed.add_field(name="​", value="**📊 BP Tier System**", inline=False)
        embed.add_field(
            name="Tiers",
            value=(
                "🥚 Raw — 0 BP\n"
                "🔰 Scrapper — 150 BP  •  +5% atk  •  10% stun resist\n"
                "⚔️ Fighter — 400 BP  •  +10% atk  •  20% stun resist\n"
                "🔥 Veteran — 900 BP  •  +18% atk  •  32% stun resist\n"
                "💀 Warlord — 2,000 BP  •  +28% atk  •  45% stun resist"
            ),
            inline=False
        )
        embed.add_field(
            name="Holdings Multiplier",
            value=(
                "1–5 MONSTRs: 1.0x BP\n"
                "6–14: 1.25x  •  15–24: 1.5x  •  25–49: 1.75x  •  50+: 2.0x"
            ),
            inline=False
        )

        embed.set_footer(text="BP never resets • Hold more MONSTRs to earn faster")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /bp_leaderboard ───────────────────────

    @discord.app_commands.command(
        name="bp_leaderboard",
        description="Top 10 MONSTR teams by Battle Points"
    )
    async def bp_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            db = get_supabase()

            rows = (
                db.table("monstr_teams")
                .select("user_id,team_name,total_bp,tier")
                .order("total_bp", desc=True)
                .limit(10)
                .execute()
            )

            if not rows.data:
                await interaction.followup.send(
                    "No teams registered yet! Use `/team_setup` to be first.", ephemeral=True
                )
                return

            medals = ["🥇", "🥈", "🥉"]
            lines  = []
            for i, row in enumerate(rows.data):
                medal      = medals[i] if i < 3 else f"**#{i + 1}**"
                bp         = row.get("total_bp", 0)
                _, tier_label, _, _ = resolve_tier(bp)
                name       = row.get("team_name") or "Unnamed Team"
                lines.append(
                    f"{medal}  {name}  —  **{bp:,} BP**  •  {tier_label}  (<@{row['user_id']}>)"
                )

            embed = discord.Embed(
                title="💀 MONSTR Battle Leaderboard",
                description="\n".join(lines),
                color=0xf1c40f,
            )
            embed.set_footer(text="BP earned through Encounters • /team_setup to join")
            await interaction.followup.send(embed=embed)

        except Exception as e:
            print(f"[ERROR] bp_leaderboard: {e}")
            await interaction.followup.send("Couldn't load the leaderboard right now.", ephemeral=True)


# ─────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────

async def setup(bot: commands.Bot):
    await bot.add_cog(EncountersCog(bot))
    await bot.add_cog(WalletCog(bot))
    await bot.add_cog(TeamsCog(bot))
