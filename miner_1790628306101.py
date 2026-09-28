"""
Single-file Monero RandomX miner for Windows CMD and Linux terminals.

This program does not contain a hidden miner and never starts mining on launch.
Mining starts only after the local command "start" or an authorized Telegram
command "/start".

The RandomX proof-of-work is provided by the native `randomx` Python package.
This file implements the CryptoNote/Monero Stratum protocol directly, so it
does not download, bundle, or start xmrig.exe.

Install the native binding first:
    python -m pip install randomx
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import ssl
import struct
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


try:
    import randomx
except ImportError:  # Keep the file importable so the error can be explained.
    randomx = None  # type: ignore[assignment]


# ============================================================================
# CONFIGURATION - edit this section
# ============================================================================

COIN_NAME = "Monero"
ALGORITHM = "RandomX"

# HashVault public Monero endpoint. Port 443 is their TLS Stratum endpoint.
POOL_HOST = "pool.hashvault.pro"
POOL_PORT = 443

# Public receiving address only. Never put a seed phrase or private key here.
# Replace this with your actual Monero public wallet address.
WALLET_ADDRESS = "PASTE_YOUR_PUBLIC_MONERO_WALLET_ADDRESS"
WORKER_NAME = "python-controller"
THREAD_COUNT = 1

# The package's light mode avoids allocating the approximately 2 GB RandomX
dataset. It is slower than full mode but is practical on ordinary machines.
RANDOMX_FULL_MEM = False
RANDOMX_SECURE = True
RANDOMX_LARGE_PAGES = False
POOL_TLS = True
SOCKET_TIMEOUT = 1.0
NONCE_OFFSET = 39
AGENT = "PythonRandomX/1.0"

# Telegram is optional. Prefer environment variables so the token is not
# stored in this source file:
#   set TELEGRAM_BOT_TOKEN=123456:replace_me
#   set TELEGRAM_CHAT_ID=123456789
# Since you asked to hardcode values directly in this file, replace the values below.
TELEGRAM_BOT_TOKEN = "PASTE_YOUR_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = "PASTE_YOUR_TELEGRAM_CHAT_ID"

STATUS_INTERVAL = 5
TELEGRAM_STATUS_INTERVAL = 60
RECONNECT_MAX_DELAY = 60

# HashVault's documented wallet payment endpoint. {wallet} is replaced at
# runtime with WALLET_ADDRESS. Amounts returned by this endpoint are atomic
# units (1 XMR = 1,000,000,000,000 atomic units).
PAYOUT_API_URL = (
    "https://api.hashvault.pro/v3/monero/wallet/{wallet}/payments"
)
# CoinGecko's public simple-price response is:
# {"monero":{"usd":123.45}}. This is an estimated market value, not payout.
PRICE_API_URL = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids=monero&vs_currencies=usd"
)
POOL_FEE_PERCENT = 0.9
DEFAULT_PAYOUT_THRESHOLD_XMR = 0.001
PAYOUT_POLL_INTERVAL = 300

LOG_FILE = "miner.log"
STATE_FILE = "miner_state.json"
PAYOUT_HISTORY_FILE = "payout_history.json"
