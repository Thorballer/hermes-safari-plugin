"""Tab lifecycle for the safari plugin: Hermes-owned tabs that expire when idle.

Every tab Hermes opens is recorded here, keyed by Safari's window/tab identity.
A tab is closed after ``idle_seconds`` (default one hour) with no tool touching
it. Tabs the user opened are never recorded, so they are never closed.

Design rules:

* **Only Hermes's own tabs are eligible.** The registry holds what this plugin
  opened. A tab the user made by hand is invisible to it and safe.
* **Activity is any touch.** ``touch()`` runs on every successful tool call that
  names a tab, so a long session stays alive; a forgotten one expires.
* **Expiry is lazy plus swept.** ``sweep()`` runs opportunistically before new
  work and from the ``on_session_end`` hook, so no timer thread is needed and an
  idle Hermes costs nothing.
* **The model can opt out.** ``keep=True`` marks a tab permanent (the model said
  to leave it), and ``close_after`` overrides the idle window per tab.
* **State survives a restart.** The registry is a JSON file under the plugin's
  data dir, so a gateway restart does not orphan tabs into permanence.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_IDLE_SECONDS = 3600.0


def _data_dir() -> Path:
    """Plugin data directory, following Hermes conventions."""
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    path = Path(home) / "plugin-data" / "safari"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _registry_path() -> Path:
    return _data_dir() / "tabs.json"


def _load() -> dict[str, Any]:
    try:
        raw = _registry_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("tabs"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"tabs": {}, "idle_seconds": DEFAULT_IDLE_SECONDS}


def _save(data: dict[str, Any]) -> None:
    path = _registry_path()
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.debug("safari tab registry save failed: %s", exc)


def _key(window: int, tab_url: str, tab_title: str) -> str:
    """Stable identity for a tab.

    Safari exposes no persistent tab id over AppleScript, and tab indices shift
    when anything is closed, so identity is the window plus the URL/title pair
    the tab was opened with. Two Hermes tabs on the same URL in one window
    collapse into one record — acceptable, since both expire together anyway.
    """
    return f"{window}|{tab_url}|{tab_title}"


def idle_seconds() -> float:
    data = _load()
    try:
        value = float(data.get("idle_seconds") or DEFAULT_IDLE_SECONDS)
    except (TypeError, ValueError):
        value = DEFAULT_IDLE_SECONDS
    return value if value > 0 else DEFAULT_IDLE_SECONDS


def set_idle_seconds(seconds: float) -> None:
    data = _load()
    data["idle_seconds"] = float(seconds)
    _save(data)


def record(window: int, url: str, title: str = "", *, keep: bool = False,
           close_after: Optional[float] = None) -> None:
    """Register a tab Hermes just opened."""
    data = _load()
    now = time.time()
    data["tabs"][_key(window, url, title)] = {
        "window": int(window),
        "url": url,
        "title": title,
        "opened_at": now,
        "last_used": now,
        "keep": bool(keep),
        "close_after": float(close_after) if close_after else None,
    }
    _save(data)


def touch(url: str, title: str = "", *, window: Optional[int] = None) -> None:
    """Mark a Hermes tab as used, so it does not expire mid-task."""
    data = _load()
    now = time.time()
    changed = False
    for rec in data["tabs"].values():
        if rec.get("url") != url:
            continue
        if window is not None and rec.get("window") != window:
            continue
        if title and rec.get("title") and rec["title"] != title:
            continue
        rec["last_used"] = now
        rec["title"] = title or rec.get("title") or ""
        changed = True
    if changed:
        _save(data)


def forget(url: str, title: str = "", *, window: Optional[int] = None) -> None:
    """Drop a tab from the registry (it was closed, by us or the user)."""
    data = _load()
    doomed = [
        k for k, rec in data["tabs"].items()
        if rec.get("url") == url
        and (window is None or rec.get("window") == window)
        and (not title or rec.get("title") == title)
    ]
    for key in doomed:
        data["tabs"].pop(key, None)
    if doomed:
        _save(data)


def expired() -> list[dict[str, Any]]:
    """Tabs past their idle window, oldest first."""
    data = _load()
    default_idle = idle_seconds()
    now = time.time()
    out: list[dict[str, Any]] = []
    for rec in data["tabs"].values():
        if rec.get("keep"):
            continue
        window = rec.get("close_after") or default_idle
        last = float(rec.get("last_used") or rec.get("opened_at") or 0)
        if now - last >= window:
            out.append(rec)
    out.sort(key=lambda r: float(r.get("last_used") or 0))
    return out


def prune(closed: list[dict[str, Any]]) -> None:
    """Remove records for tabs that were successfully closed."""
    if not closed:
        return
    data = _load()
    for rec in closed:
        data["tabs"].pop(_key(rec.get("window", 0), rec.get("url", ""), rec.get("title", "")), None)
    _save(data)


def describe() -> dict[str, Any]:
    """Registry summary for ``safari_state`` and the CLI doctor."""
    data = _load()
    default_idle = idle_seconds()
    now = time.time()
    tabs = []
    for rec in data["tabs"].values():
        last = float(rec.get("last_used") or rec.get("opened_at") or 0)
        window = rec.get("close_after") or default_idle
        tabs.append({
            "url": rec.get("url"),
            "title": rec.get("title"),
            "window": rec.get("window"),
            "idle_seconds": round(now - last, 1),
            "expires_in": None if rec.get("keep") else round(max(0.0, window - (now - last)), 1),
            "keep": bool(rec.get("keep")),
        })
    tabs.sort(key=lambda t: t["idle_seconds"], reverse=True)
    return {"idle_seconds": default_idle, "managed_tabs": tabs, "count": len(tabs)}
