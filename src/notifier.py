"""Telegram notifier — single function, no SDK dependency."""

from __future__ import annotations

import logging
import os
import urllib.parse
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None):
        self.bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        if not self.bot_token or not self.chat_id:
            log.warning(
                "Telegram credentials missing — alerts will be logged only. "
                "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."
            )

    def send(self, text: str) -> bool:
        if not self.bot_token or not self.chat_id:
            log.info("[telegram-disabled] %s", text)
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        data = urllib.parse.urlencode(
            {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": "true",
            }
        ).encode()

        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    return True
                log.error("Telegram returned status %s", resp.status)
                return False
        except Exception as exc:  # noqa: BLE001
            log.error("Telegram send failed: %s", exc)
            return False
