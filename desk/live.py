"""Live activity feed for the phone page ("what is the desk doing right now?").

Every step writes a tiny site/live.json; desk.html polls it every 2 seconds.
Display only: nothing here may ever stop the desk, so every error is swallowed.
"""
import json
import os
import threading
import time
from collections import deque

from . import config as C

MAX_FEED = 40
_lock = threading.Lock()
_feed = deque(maxlen=MAX_FEED)
_now = {"text": "起動中…", "icon": "⏳", "since": 0}
_counts = {"steps": 0}
_extra = {}


def step(icon, text, sym="", mint="", result=None, log=True):
    """Record one step. result=None means 'in progress'; otherwise e.g. 'BUY', 'REJECT', 'SKIP'.
    log=False: only update the 'right now' line (for frequent routine checks)."""
    try:
        t = time.time()
        with _lock:
            _counts["steps"] += 1
            _now.update(text=text, icon=icon, sym=sym, mint=mint, since=t)
            if log:
                _feed.appendleft({"t": round(t, 1), "i": icon, "x": text, "s": sym, "m": mint, "r": result})
        flush()
    except Exception:
        pass


def idle(text):
    """Waiting between rounds: shown as the current action, not added to the feed."""
    try:
        with _lock:
            _now.update(text=text, icon="💤", sym="", mint="", since=time.time())
        flush()
    except Exception:
        pass


def status(**kw):
    """Small numbers shown next to the live line (cash, open positions...)."""
    try:
        with _lock:
            _extra.update(kw)
    except Exception:
        pass


def flush():
    try:
        with _lock:
            data = {"ts": round(time.time(), 1), "now": dict(_now), "feed": list(_feed),
                    "steps": _counts["steps"], "status": dict(_extra)}
        path = C.LIVE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)          # atomic: the page never reads a half-written file
    except Exception:
        pass


def reset():
    with _lock:
        _feed.clear()
        _counts["steps"] = 0
        _extra.clear()
        _now.update(text="起動中…", icon="⏳", sym="", mint="", since=0)
