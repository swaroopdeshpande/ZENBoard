"""Fire-and-forget phone notifications via the local ntfy server.

Config lives at /etc/zenboard/ntfy.json, not in this file and not in the repo:
the topic name is the only thing protecting the feed, and that feed carries
presence data - when someone is home. The repo is public.

Every call is wrapped so a notification failure can never break a refresh. A
frame that fails to paint because the alerting was down would be a worse bug
than the one the alert was reporting.
"""

import json
import logging
import os
import threading

import requests

logger = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("ZENBOARD_NTFY_CONFIG", "/etc/zenboard/ntfy.json")
TIMEOUT = 5

_cfg = None


def _config():
    global _cfg
    if _cfg is None:
        try:
            with open(CONFIG_PATH) as f:
                _cfg = json.load(f)
        except Exception as e:
            logger.debug("ntfy: no config (%s)", e)
            _cfg = {}
    return _cfg


def notify(message, title=None, tags=None, priority=None, blocking=False):
    """Send a notification. Returns immediately unless blocking=True.

    Sent on a background thread by default. The refresh loop already takes tens
    of seconds to paint the panel; it must not also wait on an HTTP round trip,
    and must not stall if the ntfy server is wedged.
    """
    cfg = _config()
    url, topic = cfg.get("url"), cfg.get("topic")
    if not url or not topic or not cfg.get("enabled", True):
        return

    def _send():
        try:
            headers = {}
            if title:
                headers["Title"] = title
            if tags:
                headers["Tags"] = tags if isinstance(tags, str) else ",".join(tags)
            if priority:
                headers["Priority"] = str(priority)
            requests.post(f"{url.rstrip('/')}/{topic}",
                          data=message.encode("utf-8"),
                          headers=headers, timeout=TIMEOUT)
        except Exception as e:
            logger.debug("ntfy: send failed (%s)", e)

    if blocking:
        _send()
    else:
        threading.Thread(target=_send, daemon=True).start()
