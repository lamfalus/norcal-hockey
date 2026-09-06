"""Post a message to a Telegram channel via the Bot API.

Standard library only, like the rest of the collector -- one HTTPS POST to
``sendMessage``. Failures never raise into the caller: a channel being briefly
unreachable must not fail a collection run, so every problem is logged and the
game is simply left un-announced (its ``notified_at`` stays NULL) to be retried
next cycle.

Nothing here reads or stores the token; it is passed in from the config, which
lives only in the gitignored file on the Pi.
"""

from __future__ import annotations

import html
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/sendMessage"


def link(text: str, url: str) -> str:
    """An HTML anchor with the visible text safely escaped."""
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(text)}</a>'


def send_message(token: str, chat_id: str, html_text: str, *,
                 timeout: float = 15.0) -> bool:
    """Send one HTML message. Returns True on success, False on any failure.

    Link previews are disabled: a Time to Score scoresheet has no useful preview
    and it would only clutter the channel.
    """
    if not token or not chat_id:
        return False
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": html_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(_API.format(token=token), data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if not payload.get("ok"):
            log.warning("telegram sendMessage rejected: %s",
                        payload.get("description", payload))
            return False
        return True
    except urllib.error.HTTPError as exc:
        # Telegram puts the reason in the body; surface it for debugging.
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        log.warning("telegram sendMessage HTTP %s: %s", exc.code, body)
        return False
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log.warning("telegram sendMessage failed: %s", exc)
        return False
