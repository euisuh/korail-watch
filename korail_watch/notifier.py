from __future__ import annotations

import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

from .domain import BlockedError, TransientError
from .korail import _retry_after


_TOKEN = re.compile(r"^[1-9]\d{5,15}:[A-Za-z0-9_-]{30,64}$")
_CHAT_ID = re.compile(r"^-?[1-9]\d{0,19}$")


class TelegramNotifier:
    def __init__(self, timeout: float = 15.0):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not _TOKEN.fullmatch(self.token) or not _CHAT_ID.fullmatch(self.chat_id):
            raise ValueError("Telegram token or chat ID is missing or invalid")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = float(timeout)

    def send(self, text: str) -> None:
        if not isinstance(text, str) or not text:
            raise ValueError("notification text must not be empty")
        data = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text}).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            data=data,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read())
            if not result.get("ok"):
                raise BlockedError("Telegram rejected the notification")
        except urllib.error.HTTPError as exc:
            retry = _retry_after(exc.headers.get("Retry-After"))
            if exc.code == 429:
                if retry is None:
                    try:
                        retry = float(json.loads(exc.read()).get("parameters", {}).get("retry_after"))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
                raise TransientError(retry_after=retry) from None
            if exc.code >= 500:
                raise TransientError() from None
            raise BlockedError("Telegram rejected the notification") from None
        except (urllib.error.URLError, TimeoutError, socket.timeout):
            raise TransientError() from None
        except json.JSONDecodeError:
            raise TransientError() from None
        except BlockedError:
            raise
        except Exception:
            # urllib exceptions can retain the token-bearing request URL.
            raise TransientError() from None
