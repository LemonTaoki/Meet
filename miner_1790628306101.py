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
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


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
POOL_PORT = 3333

# Public receiving address only. Never put a seed phrase or private key here.
WALLET_ADDRESS = "835P6vhLc9WWDDxyZhGqCn6PNS7oYGrijFQ4i3haZqL1bkHPVyoScPuS5pauL5ep8G5tnc74i1g4r8mZzkhD6DWDGwi8UNF"
WORKER_NAME = "python-controller"
THREAD_COUNT = 1

# The package's light mode avoids allocating the approximately 2 GB RandomX
# dataset. It is slower than full mode but is practical on ordinary machines.
RANDOMX_FULL_MEM = False
RANDOMX_SECURE = True
RANDOMX_LARGE_PAGES = False
POOL_TLS = False
SOCKET_TIMEOUT = 1.0
NONCE_OFFSET = 39
AGENT = "PythonRandomX/1.0"

# Telegram is optional. Prefer environment variables so the token is not
# stored in this source file:
#   set TELEGRAM_BOT_TOKEN=123456:replace_me
#   set TELEGRAM_CHAT_ID=123456789
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

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


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp() -> str:
    return utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    days, total = divmod(total, 86400)
    hours, total = divmod(total, 3600)
    minutes, secs = divmod(total, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_hashrate(value: float) -> str:
    value = max(0.0, float(value or 0.0))
    units = ("H/s", "KH/s", "MH/s", "GH/s", "TH/s")
    index = 0
    while value >= 1000.0 and index < len(units) - 1:
        value /= 1000.0
        index += 1
    return f"{value:.2f} {units[index]}"


def redact(text: str) -> str:
    """Remove likely secret material from text before it reaches a log."""
    if not text:
        return text
    if TELEGRAM_BOT_TOKEN:
        text = text.replace(TELEGRAM_BOT_TOKEN, "<telegram-token-redacted>")
    return text


class Logger:
    def __init__(self, path: str = LOG_FILE) -> None:
        self._logger = logging.getLogger("python_miner")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        if not self._logger.handlers:
            formatter = logging.Formatter(
                "%(asctime)s | %(levelname)s | %(message)s",
                "%Y-%m-%d %H:%M:%S",
            )
            file_handler = logging.FileHandler(path, encoding="utf-8")
            file_handler.setFormatter(formatter)
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            self._logger.addHandler(file_handler)
            self._logger.addHandler(console_handler)

    def info(self, message: str) -> None:
        self._logger.info(redact(message))

    def warning(self, message: str) -> None:
        self._logger.warning(redact(message))

    def error(self, message: str) -> None:
        self._logger.error(redact(message))

    def exception(self, message: str) -> None:
        self._logger.exception(redact(message))


@dataclass
class MiningConfig:
    coin_name: str = COIN_NAME
    algorithm: str = ALGORITHM
    pool_host: str = POOL_HOST
    pool_port: int = POOL_PORT
    wallet_address: str = WALLET_ADDRESS
    worker_name: str = WORKER_NAME
    thread_count: int = THREAD_COUNT
    randomx_full_mem: bool = RANDOMX_FULL_MEM
    randomx_secure: bool = RANDOMX_SECURE
    randomx_large_pages: bool = RANDOMX_LARGE_PAGES
    pool_tls: bool = POOL_TLS
    socket_timeout: float = SOCKET_TIMEOUT
    nonce_offset: int = NONCE_OFFSET
    agent: str = AGENT
    telegram_token: str = TELEGRAM_BOT_TOKEN
    telegram_chat_id: str = TELEGRAM_CHAT_ID
    payout_api_url: str = PAYOUT_API_URL
    price_api_url: str = PRICE_API_URL

    @property
    def pool_display(self) -> str:
        return f"{self.pool_host}:{self.pool_port}"

    @property
    def telegram_enabled(self) -> bool:
        return bool(
            self.telegram_token
            and self.telegram_chat_id
            and self.telegram_token != "YOUR_BOT_TOKEN"
            and self.telegram_chat_id != "YOUR_CHAT_ID"
        )

    def validate(self, require_wallet: bool = True) -> List[str]:
        errors: List[str] = []
        if not self.coin_name.strip():
            errors.append("Coin name is empty.")
        if not self.algorithm.strip():
            errors.append("Algorithm is empty.")
        if not self.pool_host.strip() or any(
            char.isspace() for char in self.pool_host
        ):
            errors.append("Pool host is invalid.")
        if not isinstance(self.pool_port, int) or not 1 <= self.pool_port <= 65535:
            errors.append("Pool port must be an integer from 1 to 65535.")
        if not 1 <= int(self.thread_count) <= 1024:
            errors.append("THREAD_COUNT must be between 1 and 1024.")
        if not 0 <= int(self.nonce_offset) <= 1024:
            errors.append("NONCE_OFFSET must be a non-negative integer.")
        if self.socket_timeout <= 0:
            errors.append("SOCKET_TIMEOUT must be greater than zero.")
        if randomx is None:
            errors.append(
                "The randomx package is missing. Install it with: "
                "python -m pip install randomx"
            )
        if require_wallet:
            address = self.wallet_address.strip()
            if address.startswith("YOUR_"):
                errors.append("Replace WALLET_ADDRESS with a public receiving address.")
            elif len(address) not in (95, 106) or not re.fullmatch(
                r"[1-9A-HJ-NP-Za-km-z]+", address
            ):
                errors.append("WALLET_ADDRESS does not look like a Monero address.")
        if self.telegram_token and self.telegram_token == "YOUR_BOT_TOKEN":
            errors.append("Replace TELEGRAM_BOT_TOKEN or leave it empty to disable Telegram.")
        if self.telegram_token and not self.telegram_chat_id:
            errors.append("TELEGRAM_CHAT_ID is required when Telegram is enabled.")
        return errors

    def summary(self) -> str:
        telegram = "enabled" if self.telegram_enabled else "disabled"
        return (
            "\n========================================\n"
            "PYTHON MINER CONTROLLER\n"
            "========================================\n"
            f"Coin        : {self.coin_name}\n"
            f"Algorithm   : {self.algorithm}\n"
            f"Pool        : {self.pool_display}\n"
            f"Worker      : {self.worker_name}\n"
            f"Threads     : {self.thread_count}\n"
            f"RandomX     : {'full' if self.randomx_full_mem else 'light'} memory mode\n"
            f"Telegram    : {telegram}\n"
            "Mining      : NOT STARTED\n"
            "========================================\n"
        )


class HashrateTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at: Optional[float] = None
        self.total_hashes = 0
        self.current_hashrate = 0.0
        self.average_hashrate = 0.0
        self.last_update = 0.0

    def start(self) -> None:
        with self._lock:
            self.started_at = time.monotonic()
            self.total_hashes = 0
            self.current_hashrate = 0.0
            self.average_hashrate = 0.0
            self.last_update = time.monotonic()

    def stop(self) -> None:
        with self._lock:
            self.current_hashrate = 0.0

    def update(self, total_hashes: Optional[int] = None,
               current_hashrate: Optional[float] = None) -> None:
        with self._lock:
            if total_hashes is not None:
                self.total_hashes = max(0, int(total_hashes))
            if current_hashrate is not None:
                self.current_hashrate = max(0.0, float(current_hashrate))
            self.last_update = time.monotonic()
            if self.started_at:
                elapsed = max(0.001, time.monotonic() - self.started_at)
                self.average_hashrate = self.total_hashes / elapsed

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            runtime = (
                max(0.0, time.monotonic() - self.started_at)
                if self.started_at else 0.0
            )
            return {
                "total_hashes": self.total_hashes,
                "current_hashrate": self.current_hashrate,
                "average_hashrate": self.average_hashrate,
                "runtime": runtime,
            }


class ShareTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.accepted = 0
        self.rejected = 0
        self.stale = 0
        self.last_accepted_at: Optional[str] = None

    def set_counts(self, accepted: int, rejected: int) -> None:
        with self._lock:
            self.accepted = max(0, int(accepted))
            self.rejected = max(0, int(rejected))
            if self.accepted:
                self.last_accepted_at = timestamp()

    def record_line(self, line: str) -> None:
        lowered = line.lower()
        with self._lock:
            if "accepted" in lowered:
                self.accepted += 1
                self.last_accepted_at = timestamp()
            elif "rejected" in lowered:
                self.rejected += 1
            if "stale" in lowered:
                self.stale += 1

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "accepted": self.accepted,
                "rejected": self.rejected,
                "stale": self.stale,
                "last_accepted_at": self.last_accepted_at or "Never",
            }


class TelegramManager:
    def __init__(self, config: MiningConfig, logger: Logger) -> None:
        self.config = config
        self.logger = logger
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._offset = 0
        self._handler: Optional[Callable[[str], str]] = None

    def set_command_handler(self, handler: Callable[[str], str]) -> None:
        self._handler = handler

    def _api(self, method: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        if not self.config.telegram_enabled:
            return None
        url = f"https://api.telegram.org/bot{self.config.telegram_token}/{method}"
        data = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not result.get("ok"):
                self.logger.warning(f"Telegram API rejected {method}.")
                return None
            return result.get("result")
        except Exception as exc:
            self.logger.warning(f"Telegram {method} failed: {type(exc).__name__}.")
            return None

    def send(self, message: str) -> bool:
        if not self.config.telegram_enabled:
            return False
        result = self._api(
            "sendMessage",
            {"chat_id": self.config.telegram_chat_id, "text": message},
        )
        return result is not None

    def start(self) -> None:
        if not self.config.telegram_enabled:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, name="telegram-poller", daemon=True
        )
        self._thread.start()
        self.logger.info("Telegram command polling enabled.")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            updates = self._api(
                "getUpdates",
                {"timeout": 20, "offset": self._offset + 1, "allowed_updates": ["message"]},
            )
            if updates is None:
                self._stop_event.wait(10)
                continue
            for update in updates:
                self._offset = max(self._offset, int(update.get("update_id", 0)))
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                chat_id = str(chat.get("id", ""))
                if chat_id != str(self.config.telegram_chat_id):
                    continue
                text = str(message.get("text", "")).strip()
                if not text or not self._handler:
                    continue
                try:
                    reply = self._handler(text)
                    if reply:
                        self.send(reply)
                except Exception:
                    self.logger.exception("Telegram command handler failed.")


class MiningWorker:
    def __init__(
        self,
        config: MiningConfig,
        logger: Logger,
        hashrate: HashrateTracker,
        shares: ShareTracker,
        on_line: Callable[[str], None],
        on_exit: Callable[[int], None],
    ) -> None:
        self.config = config
        self.logger = logger
        self.hashrate = hashrate
        self.shares = shares
        self.on_line = on_line
        self.on_exit = on_exit
        self.sock: Optional[socket.socket] = None
        self._network_thread: Optional[threading.Thread] = None
        self._hash_threads: List[threading.Thread] = []
        self._stop_event = threading.Event()
        self._send_lock = threading.Lock()
        self._job_lock = threading.Lock()
        self._job_event = threading.Event()
        self._job: Optional[Dict[str, Any]] = None
        self._job_version = 0
        self._login_event = threading.Event()
        self._login_error = ""
        self._session_id = ""
        self._request_id = 1
        self._submit_request_ids: Set[int] = set()
        self._exit_notified = False
        self._hash_lock = threading.Lock()
        self._hash_total = 0
        self._hash_sample_total = 0
        self._hash_sample_started = time.monotonic()

    def _send(self, payload: Dict[str, Any]) -> None:
        data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        with self._send_lock:
            if not self.sock:
                raise ConnectionError("Stratum socket is not connected.")
            self.sock.sendall(data)

    def _next_request_id(self) -> int:
        with self._send_lock:
            self._request_id += 1
            return self._request_id

    def _connect(self) -> socket.socket:
        raw = socket.create_connection(
            (self.config.pool_host, self.config.pool_port),
            timeout=15,
        )
        if not self.config.pool_tls:
            raw.settimeout(self.config.socket_timeout)
            return raw
        try:
            import certifi
            context = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            context = ssl.create_default_context()
        wrapped = context.wrap_socket(
            raw,
            server_hostname=self.config.pool_host,
        )
        wrapped.settimeout(self.config.socket_timeout)
        return wrapped

    def _login(self) -> None:
        self._send(
            {
                "id": 1,
                "jsonrpc": "2.0",
                "method": "login",
                "params": {
                    "login": self.config.wallet_address,
                    "pass": self.config.worker_name,
                    "agent": self.config.agent,
                    "algo": ["rx/0"],
                },
            }
        )

    def start(self) -> bool:
        if randomx is None:
            self.logger.error(
                "The randomx package is missing. "
                "Install it with: python -m pip install randomx"
            )
            return False
        self._stop_event.clear()
        self._login_event.clear()
        self._login_error = ""
        self._exit_notified = False
        try:
            self.sock = self._connect()
            self._login()
        except ssl.SSLError as exc:
            self.logger.error(
                f"TLS connection to pool failed: {type(exc).__name__}: {exc}"
            )
            self.stop()
            return False
        except Exception as exc:
            self.logger.error(f"Could not connect to pool: {type(exc).__name__}.")
            return False

        self._network_thread = threading.Thread(
            target=self._network_loop,
            name="stratum-network",
            daemon=True,
        )
        self._network_thread.start()
        if not self._login_event.wait(timeout=20):
            self.logger.error("Pool did not complete the Stratum login handshake.")
            self.stop()
            return False
        if self._login_error:
            self.logger.error(f"Pool login rejected: {self._login_error}")
            self.stop()
            return False
        if not self._job_event.wait(timeout=10):
            self.logger.error("Pool login did not include a usable mining job.")
            self.stop()
            return False
        self._hash_threads = []
        for index in range(self.config.thread_count):
            thread = threading.Thread(
                target=self._hash_loop,
                args=(index,),
                name=f"randomx-worker-{index + 1}",
                daemon=True,
            )
            self._hash_threads.append(thread)
            thread.start()
        self.logger.info(
            f"RandomX Stratum session started on {self.config.pool_display}."
        )
        return True

    def _network_loop(self) -> None:
        if not self.sock:
            return
        buffer = bytearray()
        last_keepalive = time.monotonic()
        try:
            while not self._stop_event.is_set():
                try:
                    chunk = self.sock.recv(4096)
                except socket.timeout:
                    if time.monotonic() - last_keepalive >= 30:
                        self._send(
                            {
                                "id": self._next_request_id(),
                                "jsonrpc": "2.0",
                                "method": "keepalived",
                                "params": {},
                            }
                        )
                        last_keepalive = time.monotonic()
                    continue
                if not chunk:
                    raise ConnectionError("Pool closed the Stratum connection.")
                buffer.extend(chunk)
                while b"\n" in buffer:
                    raw_line, _, buffer = buffer.partition(b"\n")
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if line:
                        self._handle_message(line)
        except Exception:
            if not self._stop_event.is_set():
                self.logger.exception("Stratum network connection failed.")
        finally:
            self._login_event.set()
            if not self._stop_event.is_set():
                self._notify_exit(1)

    def _handle_message(self, line: str) -> None:
        self.on_line(line)
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            self.logger.warning("Pool sent a non-JSON Stratum line.")
            return
        response_id = message.get("id")
        if response_id is not None and response_id != 1:
            with self._send_lock:
                is_submit = response_id in self._submit_request_ids
                self._submit_request_ids.discard(response_id)
            if is_submit:
                if message.get("error"):
                    self.on_line(f"rejected share: {message['error']}")
                else:
                    self.on_line("accepted share")
        if message.get("error") and message.get("id") == 1:
            error = message["error"]
            self._login_error = str(
                error.get("message", error) if isinstance(error, dict) else error
            )
            self._login_event.set()
            return
        if message.get("id") == 1:
            result = message.get("result") or {}
            self._session_id = str(result.get("id", ""))
            if result.get("job"):
                self._install_job(result["job"])
            self._login_event.set()
            return
        if message.get("method") == "job":
            self._install_job(message.get("params") or {})
            return
        if message.get("method") in ("client_reconnect", "reconnect"):
            self.logger.warning("Pool requested a reconnect.")
            self._notify_exit(1)
            return

    def _install_job(self, raw_job: Dict[str, Any]) -> None:
        try:
            blob_text = str(raw_job["blob"])
            blob = bytes.fromhex(blob_text)
            offset = self.config.nonce_offset
            if len(blob) < offset + 4:
                raise ValueError("job blob is shorter than the nonce offset")
            target_value = raw_job["target"]
            target = int(str(target_value), 16)
            seed_text = str(raw_job.get("seed_hash", "")).strip()
            seed_hash = bytes.fromhex(seed_text) if seed_text else b""
            if len(seed_hash) == 0:
                raise ValueError("job does not contain seed_hash")
            algorithm = str(raw_job.get("algo", "rx/0"))
            if algorithm not in ("rx/0", "randomx"):
                raise ValueError(f"unsupported pool algorithm: {algorithm}")
            job = {
                "blob": blob,
                "target": target,
                "job_id": str(raw_job["job_id"]),
                "seed_hash": seed_hash,
                "algo": algorithm,
            }
        except (KeyError, TypeError, ValueError) as exc:
            self.logger.warning(f"Ignoring invalid pool job: {exc}")
            return
        with self._job_lock:
            self._job = job
            self._job_version += 1
        self._job_event.set()
        self.logger.info(f"New RandomX job received: {job['job_id']}")

    def _hash_loop(self, worker_index: int) -> None:
        nonce = worker_index & 0xFFFFFFFF
        while not self._stop_event.is_set():
            with self._job_lock:
                job = self._job
                version = self._job_version
            if not job:
                self._job_event.wait(1)
                continue
            try:
                vm = randomx.RandomX(
                    job["seed_hash"],
                    full_mem=self.config.randomx_full_mem,
                    secure=self.config.randomx_secure,
                    large_pages=self.config.randomx_large_pages,
                )
            except Exception as exc:
                self.logger.error(
                    f"RandomX VM initialization failed: {type(exc).__name__}."
                )
                self._stop_event.wait(5)
                continue
            while not self._stop_event.is_set():
                with self._job_lock:
                    if version != self._job_version:
                        break
                    current_job = self._job
                if not current_job:
                    break
                candidate = bytearray(current_job["blob"])
                struct.pack_into("<I", candidate, self.config.nonce_offset, nonce)
                digest = vm(bytes(candidate))
                self._record_hash()
                if self._meets_target(digest, current_job["target"]):
                    self._submit(current_job, candidate, digest)
                nonce = (
                    nonce + self.config.thread_count
                ) & 0xFFFFFFFF

    def _record_hash(self) -> None:
        now = time.monotonic()
        with self._hash_lock:
            self._hash_total += 1
            self._hash_sample_total += 1
            elapsed = now - self._hash_sample_started
            if elapsed < 1.0:
                return
            rate = self._hash_sample_total / elapsed
            total = self._hash_total
            self._hash_sample_total = 0
            self._hash_sample_started = now
        self.hashrate.update(total_hashes=total, current_hashrate=rate)

    @staticmethod
    def _meets_target(digest: bytes, target: int) -> bool:
        # CryptoNote/Monero interprets the first 64 bits as little-endian.
        return int.from_bytes(digest[:8], "little") <= target

    def _submit(
        self,
        job: Dict[str, Any],
        candidate: bytearray,
        digest: bytes,
    ) -> None:
        nonce = bytes(candidate[
            self.config.nonce_offset:self.config.nonce_offset + 4
        ]).hex()
        payload = {
            "id": self._session_id,
            "job_id": job["job_id"],
            "nonce": nonce,
            "result": digest.hex(),
        }
        if job.get("algo"):
            payload["algo"] = job["algo"]
        request_id = self._next_request_id()
        with self._send_lock:
            self._submit_request_ids.add(request_id)
        try:
            self._send(
                {
                    "id": request_id,
                    "jsonrpc": "2.0",
                    "method": "submit",
                    "params": payload,
                }
            )
        except Exception:
            with self._send_lock:
                self._submit_request_ids.discard(request_id)
            if not self._stop_event.is_set():
                self.logger.exception("Could not submit a valid share.")

    def stop(self) -> None:
        self._stop_event.set()
        self._job_event.set()
        sock = self.sock
        self.sock = None
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        current = threading.current_thread()
        if self._network_thread and (
            self._network_thread.is_alive()
            and self._network_thread is not current
        ):
            self._network_thread.join(timeout=3)
        for thread in self._hash_threads:
            if thread.is_alive() and thread is not current:
                thread.join(timeout=3)
        self._hash_threads = []

    def is_running(self) -> bool:
        return bool(
            not self._stop_event.is_set()
            and self._network_thread
            and self._network_thread.is_alive()
        )

    def _notify_exit(self, code: int) -> None:
        if self._exit_notified:
            return
        self._exit_notified = True
        self.on_exit(code)


class PoolManager:
    def __init__(
        self,
        config: MiningConfig,
        logger: Logger,
        hashrate: HashrateTracker,
        shares: ShareTracker,
        on_connection_error: Callable[[str], None],
    ) -> None:
        self.config = config
        self.logger = logger
        self.hashrate = hashrate
        self.shares = shares
        self.on_connection_error = on_connection_error
        self.worker: Optional[MiningWorker] = None
        self.connected = False
        self._stop_event = threading.Event()

    def start(self) -> bool:
        self._stop_event.clear()
        self.worker = MiningWorker(
            self.config,
            self.logger,
            self.hashrate,
            self.shares,
            self._handle_line,
            self._handle_exit,
        )
        if not self.worker.start():
            self.connected = False
            return False
        self.connected = True
        self.logger.info(f"Stratum pool connected to {self.config.pool_display}.")
        return True

    def _handle_line(self, line: str) -> None:
        lowered = line.lower()
        if "accepted" in lowered or "rejected" in lowered or "stale" in lowered:
            self.shares.record_line(line)
        if (
            "connection error" in lowered
            or "connection refused" in lowered
            or "closed the stratum" in lowered
        ):
            self.connected = False
            self.on_connection_error(line[:300])
        if any(
            marker in lowered
            for marker in (
                "accepted",
                "rejected",
                "stale",
                "connection error",
                "connection refused",
                "closed the stratum",
                "error",
            )
        ):
            self.logger.info(f"Stratum: {line}")

    def _handle_exit(self, code: int) -> None:
        self.connected = False
        if not self._stop_event.is_set():
            self.on_connection_error(f"Stratum connection exited with code {code}.")

    def stop(self) -> None:
        self._stop_event.set()
        if self.worker:
            self.worker.stop()
        self.worker = None
        self.connected = False

    def is_running(self) -> bool:
        return bool(self.worker and self.worker.is_running())


class PayoutMonitor:
    def __init__(
        self,
        config: MiningConfig,
        logger: Logger,
        telegram: TelegramManager,
    ) -> None:
        self.config = config
        self.logger = logger
        self.telegram = telegram
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.history: List[Dict[str, Any]] = []
        self.last_payout = "Unavailable"
        self._initialized = False
        self.load_state()

    def load_state(self) -> None:
        for path, fallback in ((PAYOUT_HISTORY_FILE, []),):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    value = json.load(handle)
                if isinstance(value, list):
                    self.history = value
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                self.history = fallback
        if self.history:
            self.last_payout = str(self.history[-1].get("timestamp", "Unavailable"))
            self._initialized = True

    def _save(self) -> None:
        temporary = f"{PAYOUT_HISTORY_FILE}.tmp"
        with self._lock:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(self.history[-100:], handle, indent=2)
            os.replace(temporary, PAYOUT_HISTORY_FILE)

    def start(self) -> None:
        if (
            not self.config.payout_api_url
            or self.config.wallet_address.startswith("YOUR_")
        ):
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="payout-monitor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self.poll_once()
            self._stop_event.wait(PAYOUT_POLL_INTERVAL)

    def poll_once(self) -> None:
        if (
            not self.config.payout_api_url
            or self.config.wallet_address.startswith("YOUR_")
        ):
            return
        try:
            wallet = urllib.parse.quote(self.config.wallet_address, safe="")
            url = self.config.payout_api_url.format(wallet=wallet)
            with urllib.request.urlopen(url, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
            payouts = payload if isinstance(payload, list) else payload.get("payouts", [])
            if not isinstance(payouts, list):
                raise ValueError("HashVault payments response must be a list")
            new_records: List[Dict[str, Any]] = []
            for payout in payouts:
                if not isinstance(payout, dict):
                    continue
                txid = str(payout.get("txnHash", payout.get("txid", ""))).strip()
                if not txid or any(item.get("txid") == txid for item in self.history):
                    continue
                raw_amount = float(payout.get("amount", 0.0))
                amount = raw_amount / 1_000_000_000_000
                if amount <= 0:
                    continue
                estimated = self._price_value(amount)
                raw_ts = payout.get("ts")
                if raw_ts:
                    payout_time = datetime.fromtimestamp(
                        float(raw_ts), timezone.utc
                    ).strftime("%Y-%m-%d %H:%M:%S UTC")
                else:
                    payout_time = timestamp()
                record = {
                    "txid": txid,
                    "coin": self.config.coin_name,
                    "amount": amount,
                    "estimated_usdt_value": estimated,
                    "pool": self.config.pool_display,
                    "timestamp": payout_time,
                    "confirmation_status": "confirmed",
                    "explorer": f"https://xmrchain.net/tx/{txid}",
                }
                new_records.append(record)
            if not self._initialized:
                with self._lock:
                    self.history.extend(reversed(new_records))
                    self.history.sort(key=lambda item: item.get("timestamp", ""))
                    if self.history:
                        self.last_payout = self.history[-1]["timestamp"]
                    self._save()
                self._initialized = True
                return
            for record in reversed(new_records):
                with self._lock:
                    self.history.append(record)
                    self.last_payout = record["timestamp"]
                    self._save()
                self.logger.info(f"Confirmed payout detected: {record['txid']}")
                self.telegram.send(self._notification(record))
        except Exception as exc:
            self.logger.warning(f"Payout API unavailable: {type(exc).__name__}.")

    def _price_value(self, amount: float) -> Optional[float]:
        if not self.config.price_api_url:
            return None
        try:
            with urllib.request.urlopen(self.config.price_api_url, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
            if "price_usdt" in data:
                price = float(data["price_usdt"])
            else:
                price = float(data["monero"]["usd"])
            return round(amount * price, 8)
        except Exception:
            self.logger.warning("Price API unavailable; USDT value not estimated.")
            return None

    def _notification(self, record: Dict[str, Any]) -> str:
        value = (
            f"${record['estimated_usdt_value']:.8f}"
            if record["estimated_usdt_value"] is not None
            else "unavailable"
        )
        return (
            "💰 PAYOUT RECEIVED\n\n"
            "Status: SUCCESS\n"
            f"Coin: {record['coin']}\n"
            f"Amount: {record['amount']:.8f} {record['coin']}\n"
            f"Estimated USDT value: {value}\n"
            f"Pool: {record['pool']}\n"
            f"Transaction ID: {record['txid']}\n"
            f"Timestamp: {record['timestamp']}\n"
            f"Explorer: {record['explorer']}"
        )

    def recent(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.history[-10:])

    def summary(self) -> Tuple[float, Optional[float]]:
        with self._lock:
            total_coin = sum(float(item.get("amount", 0.0)) for item in self.history)
            values = [
                float(item["estimated_usdt_value"])
                for item in self.history
                if item.get("estimated_usdt_value") is not None
            ]
            return total_coin, (sum(values) if values else None)


class MiningManager:
    def __init__(
        self,
        config: MiningConfig,
        logger: Logger,
        telegram: TelegramManager,
        payout_monitor: PayoutMonitor,
    ) -> None:
        self.config = config
        self.logger = logger
        self.telegram = telegram
        self.payout_monitor = payout_monitor
        self.hashrate = HashrateTracker()
        self.shares = ShareTracker()
        self.pool = PoolManager(
            config,
            logger,
            self.hashrate,
            self.shares,
            self._connection_error,
        )
        self._lock = threading.Lock()
        self._supervisor: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.mining = False
        self.started_at: Optional[float] = None
        self.last_connection_error = ""
        self._last_telegram_status = 0.0

    def start(self) -> str:
        with self._lock:
            if self.mining:
                return "Mining is already running."
            errors = self.config.validate(require_wallet=True)
            if errors:
                return "Cannot start:\n- " + "\n- ".join(errors)
            self._stop_event.clear()
            self.hashrate.start()
            self.started_at = time.monotonic()
            self.mining = True
            if not self.pool.start():
                self.mining = False
                self.hashrate.stop()
                return "RandomX Stratum mining could not be started. Check the log."
            self._supervisor = threading.Thread(
                target=self._supervise, name="miner-supervisor", daemon=True
            )
            self._supervisor.start()
        self.logger.info("Mining started by explicit user command.")
        self.telegram.send(self._started_message())
        return "Mining started."

    def stop(self) -> str:
        with self._lock:
            if not self.mining:
                return "Mining is not running."
            self._stop_event.set()
            self.pool.stop()
            self.mining = False
            self.hashrate.stop()
        self.logger.info("Mining stopped by user command.")
        self.telegram.send(self._stopped_message())
        return "Mining stopped."

    def restart(self) -> str:
        self.stop()
        time.sleep(1)
        return self.start()

    def _supervise(self) -> None:
        delay = 3
        while not self._stop_event.is_set():
            if self.pool.is_running():
                delay = 3
                self._maybe_telegram_status()
                self._stop_event.wait(2)
                continue
            if self._stop_event.is_set():
                break
            self.logger.warning(f"Reconnecting to pool in {delay} seconds.")
            self.telegram.send(
                f"🔄 POOL RECONNECTING\n\nPool: {self.config.pool_display}\n"
                f"Attempt delay: {delay}s"
            )
            if self._stop_event.wait(delay):
                break
            self.pool.stop()
            if self.pool.start():
                self.telegram.send(
                    f"🟢 POOL CONNECTED\n\nPool: {self.config.pool_display}\n"
                    f"Worker: {self.config.worker_name}"
                )
                delay = 3
            else:
                delay = min(RECONNECT_MAX_DELAY, delay * 2)

    def _maybe_telegram_status(self) -> None:
        if not self.config.telegram_enabled:
            return
        now = time.monotonic()
        if now - self._last_telegram_status >= TELEGRAM_STATUS_INTERVAL:
            self._last_telegram_status = now
            self.telegram.send(self._hashrate_message())

    def _connection_error(self, error: str) -> None:
        self.last_connection_error = error
        self.logger.warning(f"Pool connection error: {error}")
        self.telegram.send(
            f"⚠️ POOL CONNECTION ERROR\n\nPool: {self.config.pool_display}\n"
            f"Error: {error}"
        )

    def snapshot(self) -> Dict[str, Any]:
        data = {}
        data.update(self.hashrate.snapshot())
        data.update(self.shares.snapshot())
        data["mining"] = self.mining
        data["connected"] = self.pool.connected
        data["threads"] = self.config.thread_count
        data["worker"] = self.config.worker_name
        data["runtime"] = (
            max(0.0, time.monotonic() - self.started_at)
            if self.started_at and self.mining else 0.0
        )
        return data

    def _started_message(self) -> str:
        return (
            "⛏️ MINING STARTED\n\n"
            f"Coin: {self.config.coin_name}\n"
            f"Algorithm: {self.config.algorithm}\n"
            f"Pool: {self.config.pool_display}\n"
            f"Worker: {self.config.worker_name}\n"
            f"Threads: {self.config.thread_count}\n"
            f"Time: {timestamp()}"
        )

    def _stopped_message(self) -> str:
        state = self.snapshot()
        return (
            "🛑 MINING STOPPED\n\n"
            f"Runtime: {format_duration(state['runtime'])}\n"
            f"Total Hashes: {state['total_hashes']}\n"
            f"Accepted: {state['accepted']}\n"
            f"Rejected: {state['rejected']}"
        )

    def _hashrate_message(self) -> str:
        state = self.snapshot()
        return (
            "📊 HASHRATE UPDATE\n\n"
            f"Current: {format_hashrate(state['current_hashrate'])}\n"
            f"Average: {format_hashrate(state['average_hashrate'])}\n"
            f"Accepted: {state['accepted']}\n"
            f"Rejected: {state['rejected']}"
        )

    def telegram_command(self, command: str) -> str:
        command = command.lower().split()[0] if command.strip() else ""
        if command == "/start":
            return self.start()
        if command == "/stop":
            return self.stop()
        if command == "/restart":
            return self.restart()
        if command in ("/status", "/hashrate", "/workers"):
            return self.status_text()
        return (
            "Commands: /start /stop /status /hashrate /workers /restart"
        )

    def status_text(self) -> str:
        state = self.snapshot()
        return (
            f"Coin: {self.config.coin_name}\n"
            f"Algorithm: {self.config.algorithm}\n"
            f"Pool: {self.config.pool_display}\n"
            f"Worker: {state['worker']}\n"
            f"Connection: {'CONNECTED' if state['connected'] else 'DISCONNECTED'}\n"
            f"Mining: {'MINING' if state['mining'] else 'STOPPED'}\n"
            f"Threads: {state['threads']}\n"
            f"Current Hashrate: {format_hashrate(state['current_hashrate'])}\n"
            f"Average Hashrate: {format_hashrate(state['average_hashrate'])}\n"
            f"Total Hashes: {state['total_hashes']}\n"
            f"Accepted: {state['accepted']}\n"
            f"Rejected: {state['rejected']}\n"
            f"Stale: {state['stale']}\n"
            f"Runtime: {format_duration(state['runtime'])}\n"
            f"Last Accepted: {state['last_accepted_at']}\n"
            f"Last Payout: {self.payout_monitor.last_payout}\n"
        )


class TerminalUI:
    def __init__(
        self,
        config: MiningConfig,
        manager: MiningManager,
        telegram: TelegramManager,
        payout_monitor: PayoutMonitor,
        logger: Logger,
    ) -> None:
        self.config = config
        self.manager = manager
        self.telegram = telegram
        self.payout_monitor = payout_monitor
        self.logger = logger
        self._stop_event = threading.Event()
        self._screen_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._screen_thread = threading.Thread(
            target=self._screen_loop, name="terminal-status", daemon=True
        )
        self._screen_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._screen_thread and self._screen_thread.is_alive():
            self._screen_thread.join(timeout=2)

    def _screen_loop(self) -> None:
        while not self._stop_event.is_set():
            if self.manager.mining:
                self._render()
            self._stop_event.wait(STATUS_INTERVAL)

    def _render(self) -> None:
        state = self.manager.snapshot()
        try:
            os.system("cls" if os.name == "nt" else "clear")
        except Exception:
            pass
        print(
            "========================================\n"
            "PYTHON MINER\n"
            "========================================"
        )
        print(f"Coin        : {self.config.coin_name}")
        print(f"Algorithm   : {self.config.algorithm}")
        print(f"Pool        : {self.config.pool_display}")
        print(f"Worker      : {state['worker']}")
        print(f"Status      : {'MINING' if state['mining'] else 'STOPPED'}")
        print(f"Connection  : {'CONNECTED' if state['connected'] else 'DISCONNECTED'}")
        print(f"Hashrate    : {format_hashrate(state['current_hashrate'])}")
        print(f"Avg Hashrate: {format_hashrate(state['average_hashrate'])}")
        print(f"Total Hashes: {state['total_hashes']}")
        print(f"Accepted    : {state['accepted']}")
        print(f"Rejected    : {state['rejected']}")
        print(f"Stale       : {state['stale']}")
        print(f"Runtime     : {format_duration(state['runtime'])}")
        print(f"Last Share  : {state['last_accepted_at']}")
        print(f"Last Payout : {self.payout_monitor.last_payout}")
        print("\nType a command: status | stop | help | exit")

    def payouts(self) -> str:
        entries = self.payout_monitor.recent()
        if not entries:
            return "Payout information unavailable. No confirmed payouts recorded."
        lines = ["Recent confirmed payouts:"]
        for item in entries:
            value = (
                f"${item['estimated_usdt_value']:.8f}"
                if item.get("estimated_usdt_value") is not None
                else "unavailable"
            )
            lines.append(
                f"- {item['timestamp']} | {item['amount']:.8f} {item['coin']} | "
                f"USDT estimate: {value} | tx: {item['txid']}"
            )
        return "\n".join(lines)

    def help_text(self) -> str:
        return (
            "Commands:\n"
            "  start     Start Python RandomX mining\n"
            "  stop      Stop mining\n"
            "  status    Show complete status\n"
            "  hashrate  Show current and average hashrate\n"
            "  workers   Show worker status\n"
            "  restart   Restart the mining process\n"
            "  payouts   Show confirmed payout history\n"
            "  help      Show this help\n"
            "  exit      Stop everything and exit\n"
            "\nMining never starts automatically."
        )

    def run(self) -> None:
        print(self.config.summary())
        errors = self.config.validate(require_wallet=False)
        if errors:
            print("Configuration warnings:")
            for error in errors:
                print(f"- {error}")
        print(self.help_text())
        if not sys.stdin or not sys.stdin.isatty():
            self.logger.info("No interactive console; running headless.")
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
            finally:
                self.manager.stop()
                self.payout_monitor.stop()
                self.telegram.stop()
                self.stop()
                self.logger.info("Application exited.")
            return
        try:
            while True:
                command = input("\nminer> ").strip().lower()
                if command == "start":
                    print(self.manager.start())
                elif command == "stop":
                    print(self.manager.stop())
                elif command == "status":
                    print(self.manager.status_text())
                elif command in ("hashrate", "workers"):
                    print(self.manager.status_text())
                elif command == "restart":
                    print(self.manager.restart())
                elif command == "payouts":
                    print(self.payouts())
                elif command == "help":
                    print(self.help_text())
                elif command in ("exit", "quit"):
                    break
                elif command:
                    print("Unknown command. Type help.")
        except (KeyboardInterrupt, EOFError):
            print("\nShutdown requested.")
        finally:
            self.manager.stop()
            self.payout_monitor.stop()
            self.telegram.stop()
            self.stop()
            self.logger.info("Application exited.")


def main() -> None:
    config = MiningConfig()
    logger = Logger()
    telegram = TelegramManager(config, logger)
    payout_monitor = PayoutMonitor(config, logger, telegram)
    manager = MiningManager(config, logger, telegram, payout_monitor)
    telegram.set_command_handler(manager.telegram_command)
    telegram.start()
    payout_monitor.start()
    ui = TerminalUI(config, manager, telegram, payout_monitor, logger)
    ui.start()
    logger.info("Application ready; waiting for explicit start command.")
    ui.run()


if __name__ == "__main__":
    main()
