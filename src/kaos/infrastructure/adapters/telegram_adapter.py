#!/usr/bin/env python3
"""
TelegramAdapter – Lightweight Bot for KAOS Monitor & Control
==============================================================
Sends alerts and receives commands (e.g. /kill, /status) via plain
Telegram Bot API (no python-telegram-bot dependency).
Uses asyncio + aiohttp for fully async communication.
"""
import asyncio
import html
import logging
import time
from typing import Callable, Coroutine, Any, Dict, Optional
from pathlib import Path

import aiohttp

from kaos.application.ports import NotificationPort

logger = logging.getLogger(__name__)

Handler = Callable[[str, str], Coroutine[Any, Any, None]]  # (chat_id, text) -> None


class TelegramAdapter(NotificationPort):
    """Trình kết nối Telegram Bot API dạng polling (getUpdates)."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        polling_interval: float = 3.0,
        max_consecutive_errors: int = 5,
    ):
        self._token = token
        self._chat_id = chat_id
        self._polling_interval = polling_interval
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._session: Optional[aiohttp.ClientSession] = None
        self._handlers: Dict[str, Handler] = {}
        self._offset: int = 0
        self._running = False
        self._max_errors = max_consecutive_errors
        self._error_count = 0

    def register_command(self, command: str, handler: Handler) -> None:
        """Đăng ký command (ví dụ: 'kill', 'status') với handler."""
        normalized = command.lstrip("/")
        self._handlers[normalized] = handler
        logger.info(f"   📞 Registered Telegram command: /{normalized}")

    # ── Gửi tin nhắn ──────────────────────────────────────────────

    async def send_message(self, text: str) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        url = f"{self._base_url}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        async with self._session.post(url, json=payload) as resp:
            if resp.status != 200:
                logger.error(f"Telegram sendMessage failed: {resp.status} – {await resp.text()}")

    async def send_alert(self, title: str, details: str, level: str = "WARNING") -> None:
        escaped_title = html.escape(title)
        escaped_details = html.escape(details)
        text = (
            f"🚨 <b>{level}</b>: {escaped_title}\n\n"
            f"<pre>{escaped_details[:500]}</pre>"
        )
        await self.send_message(text)

    # ── Lắng nghe command (polling) ───────────────────────────────

    async def start_polling(self) -> None:
        """Bắt đầu long-polling vô hạn – chạy như background coroutine."""
        if self._running:
            return
        self._running = True
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        logger.info(f"   📲 Telegram polling started (interval={self._polling_interval}s)")
        while self._running:
            await self._poll_once()
            await asyncio.sleep(self._polling_interval)

    def stop_polling(self) -> None:
        self._running = False

    async def _poll_once(self) -> None:
        try:
            url = f"{self._base_url}/getUpdates"
            params = {
                "offset": self._offset,
                "timeout": self._polling_interval,
            }
            async with self._session.get(url, params=params) as resp:
                if resp.status != 200:
                    self._error_count += 1
                    if self._error_count >= self._max_errors:
                        logger.warning(
                            f"   ⚠️ Telegram getUpdates failed {self._error_count}x, stop polling"
                        )
                        self._running = False
                    return
                data = await resp.json()
                self._error_count = 0

            updates = data.get("result", [])
            for upd in updates:
                self._offset = upd.get("update_id", 0) + 1
                message = upd.get("message", {})
                text = message.get("text", "")
                chat_id = str(message.get("chat", {}).get("id", ""))

                if not text or not chat_id:
                    continue

                # Xử lý command /abc
                if text.startswith("/"):
                    parts = text[1:].split(" ", 1)
                    cmd = parts[0].lower()
                    args = parts[1] if len(parts) > 1 else ""
                    handler = self._handlers.get(cmd)
                    if handler:
                        await handler(chat_id, args)
                    else:
                        await self.send_message(f"Unknown command: /{cmd}")
                else:
                    logger.info(f"   💬 Telegram message (ignored): {text}")
        except asyncio.CancelledError:
            self._running = False
        except Exception as exc:
            logger.warning(f"   ⚠️ Telegram polling error: {exc}")
            self._error_count += 1

    # ── Lifecycle helpers ─────────────────────────────────────────

    async def __aenter__(self):
        if self._session is None:
            self._session = aiohttp.ClientSession()
        if not self._running:
            asyncio.ensure_future(self.start_polling())
        return self

    async def __aexit__(self, *args):
        self.stop_polling()
        if self._session:
            await self._session.close()
            self._session = None
