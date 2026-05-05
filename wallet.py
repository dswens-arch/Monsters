"""
wallet.py
---------
Algorand wallet layer for the MONSTRS Encounters bot.

Handles:
  - Bot hot wallet setup (mnemonic-based)
  - Sending $GOO (ASA) to player wallets on withdrawal
  - Polling indexer for incoming $GOO deposits
  - Opt-in check for $GOO asset

Requires env vars:
  BOT_MNEMONIC    — 25-word mnemonic for the bot's hot wallet
  GOO_ASSET_ID    — Algorand ASA ID for $GOO token
  ALGOD_TOKEN     — algod API token (use "" for AlgoNode)
  ALGOD_URL       — algod node URL
  INDEXER_TOKEN   — indexer API token (use "" for AlgoNode)
  INDEXER_URL     — indexer node URL

Recommended free nodes:
  ALGOD_URL    = https://mainnet-api.algonode.cloud
  INDEXER_URL  = https://mainnet-idx.algonode.cloud
"""

import os
import time
from algosdk import mnemonic, transaction
from algosdk.v2client import algod, indexer


# ─────────────────────────────────────────────
# CLIENT SETUP
# ─────────────────────────────────────────────

def get_algod() -> algod.AlgodClient:
    return algod.AlgodClient(
        algod_token=os.getenv("ALGOD_TOKEN", ""),
        algod_address=os.getenv("ALGOD_URL", "https://mainnet-api.algonode.cloud"),
    )

def get_indexer() -> indexer.IndexerClient:
    return indexer.IndexerClient(
        indexer_token=os.getenv("INDEXER_TOKEN", ""),
        indexer_address=os.getenv("INDEXER_URL", "https://mainnet-idx.algonode.cloud"),
    )

def get_bot_account() -> tuple[str, str]:
    """Returns (private_key, address) for the bot hot wallet."""
    mn = os.getenv("BOT_MNEMONIC")
    if not mn:
        raise EnvironmentError("BOT_MNEMONIC env var not set.")
    private_key = mnemonic.to_private_key(mn)
    address = mnemonic.to_public_key(mn)
    return private_key, address

def get_goo_asset_id() -> int:
    asset_id = os.getenv("GOO_ASSET_ID")
    if not asset_id:
        raise EnvironmentError("GOO_ASSET_ID env var not set.")
    return int(asset_id)


# ─────────────────────────────────────────────
# SEND $GOO
# Called on /withdraw — sends from bot wallet to player wallet
# ─────────────────────────────────────────────

def send_goo(to_address: str, amount: int, note: str = "MONSTRS GOO withdrawal") -> str:
    """
    Send $GOO ASA from bot wallet to a player address.
    Returns the transaction ID.
    Raises on failure.
    """
    private_key, bot_address = get_bot_account()
    asset_id = get_goo_asset_id()
    client = get_algod()

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

    # Wait for confirmation
    result = transaction.wait_for_confirmation(client, tx_id, wait_rounds=4)
    print(f"[WALLET] Sent {amount} GOO to {to_address[:10]}... TxID: {tx_id}")
    return tx_id


# ─────────────────────────────────────────────
# CHECK OPT-IN
# Player must opt-in to $GOO ASA before they can receive it
# ─────────────────────────────────────────────

def has_opted_in(wallet_address: str) -> bool:
    """Check if a wallet has opted in to the $GOO ASA."""
    try:
        client = get_algod()
        asset_id = get_goo_asset_id()
        account_info = client.account_info(wallet_address)
        assets = account_info.get("assets", [])
        return any(a["asset-id"] == asset_id for a in assets)
    except Exception as e:
        print(f"[WALLET] opt-in check failed for {wallet_address}: {e}")
        return False


# ─────────────────────────────────────────────
# POLL FOR INCOMING $GOO DEPOSITS
# Optional — only needed if you want players to deposit GOO into the bot.
# For Encounters, GOO flows OUT (rewards) not IN.
# Included here for completeness / future use.
# ─────────────────────────────────────────────

def get_recent_goo_deposits(since_round: int = 0) -> list[dict]:
    """
    Poll indexer for incoming $GOO transfers to the bot wallet.
    Returns list of dicts: {sender, amount, round, tx_id}
    """
    try:
        _, bot_address = get_bot_account()
        asset_id = get_goo_asset_id()
        idx = get_indexer()

        response = idx.search_asset_transactions(
            asset_id=asset_id,
            address=bot_address,
            address_role="receiver",
            min_round=since_round,
            txn_type="axfer",
            limit=50,
        )

        deposits = []
        for txn in response.get("transactions", []):
            transfer = txn.get("asset-transfer-transaction", {})
            if transfer.get("receiver") == bot_address and transfer.get("amount", 0) > 0:
                deposits.append({
                    "sender": txn.get("sender"),
                    "amount": transfer.get("amount"),
                    "round": txn.get("confirmed-round"),
                    "tx_id": txn.get("id"),
                })
        return deposits

    except Exception as e:
        print(f"[WALLET] deposit poll failed: {e}")
        return []


# ─────────────────────────────────────────────
# BOT WALLET INFO
# ─────────────────────────────────────────────

def get_bot_goo_balance() -> int:
    """Returns the bot wallet's current $GOO balance."""
    try:
        _, bot_address = get_bot_account()
        asset_id = get_goo_asset_id()
        client = get_algod()
        info = client.account_info(bot_address)
        for asset in info.get("assets", []):
            if asset["asset-id"] == asset_id:
                return asset["amount"]
        return 0
    except Exception as e:
        print(f"[WALLET] balance check failed: {e}")
        return 0
