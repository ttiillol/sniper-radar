"""Phone notifications via ntfy.sh (free, no account).

Enabled only when NTFY_TOPIC is set (the iMac runner sets it), so tests and CI never send.
Open https://ntfy.sh/<topic> on the phone (or the ntfy app) to see the live feed.
The topic name is hard to guess but not secret: anyone who knows it can read the feed.
Nothing sensitive is ever sent (paper trades and public coin data only).
"""
import json
import os
import threading
import time

import requests

TOPIC = os.environ.get("NTFY_TOPIC", "")
SERVER = "https://ntfy.sh/"
DAILY_CAP = 180            # ntfy's free tier allows a few hundred messages per day
MIN_GAP = 6                # seconds between messages

_lock = threading.Lock()
_state = {"last": 0.0, "day": "", "count": 0}


def enabled():
    return bool(TOPIC)


def send(title, message, tags=None, click=None, priority=3):
    """Fire-and-forget. Never raises; returns True if accepted."""
    if not TOPIC:
        return False
    with _lock:
        day = time.strftime("%Y-%m-%d")
        if _state["day"] != day:
            _state.update(day=day, count=0)
        if _state["count"] >= DAILY_CAP and priority < 5:
            return False
        wait = _state["last"] + MIN_GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        _state["last"] = time.time()
        _state["count"] += 1
    body = {"topic": TOPIC, "title": title[:120], "message": message[:1500],
            "tags": [], "priority": priority}   # no emoji tags (plain design)
    if click:
        body["click"] = click
    try:
        r = requests.post(SERVER, data=json.dumps(body).encode(), timeout=10,
                          headers={"content-type": "application/json"})
        return r.status_code == 200
    except Exception:
        return False


def send_async(*a, **k):
    threading.Thread(target=send, args=a, kwargs=k, daemon=True).start()
