"""
Daily full-refresh counter.

Tracks how many times the e-ink panel has done a FULL refresh today
(partial refreshes, like ereader page-turns, aren't counted here - this
is specifically for watching e-ink cycle-count wear, which is a full-
refresh concern). Resets automatically at midnight local time - not via a
cron job, just by comparing dates on each read/write, so it's robust to
the Pi being off at midnight.

Stored in the project's config dir (persistent, not /tmp - tmpfs on this
device gets wiped on reboot).
"""

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

STATS_FILE = Path(__file__).parent.parent / "config" / "refresh_stats.json"


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _load():
    if STATS_FILE.exists():
        try:
            with open(STATS_FILE, "r") as f:
                data = json.load(f)
            if data.get("date") == _today():
                return data
        except Exception as e:
            logger.warning(f"Refresh stats read failed, resetting: {e}")
    return {"date": _today(), "count": 0}


def _save(data):
    try:
        STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATS_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.warning(f"Refresh stats write failed: {e}")


def increment_daily_refresh_count():
    """Call this exactly once per FULL physical panel refresh."""
    data = _load()  # already resets to 0 if the date rolled over
    data["count"] += 1
    _save(data)
    logger.info(f"Daily full-refresh count: {data['count']}")
    return data["count"]


def get_daily_refresh_count():
    """Read-only - also self-resets at midnight without incrementing."""
    data = _load()
    return {"date": data["date"], "count": data["count"]}
