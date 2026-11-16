"""Safari operations: tabs, navigation, DOM reads, and native input.

Split in two on purpose:

* **AppleScript/JXA actions** (``open``, ``tabs``, ``navigate``, ``state``) work with
  only automation consent — no Develop menu toggle. They are the always-available floor.
* **DOM actions** (``js``, ``click``, ``type``, ``screenshot``) need
  ``do JavaScript``. ``click`` has a native fallback: Safari's ``do JavaScript`` can
  dispatch a synthetic event, but a real click is often what a site demands, so
  :func:`click` prefers an element-rect + ``cliclick``/``osascript`` native click when
  JS is off and coordinates can still be derived.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from typing import Any, Optional

from . import jxa, tab_lifecycle
from .jxa import SafariError

logger = logging.getLogger(__name__)

# Safari's `do JavaScript` returns the last expression's value; wrap user code so a
# thrown error or a non-serialisable value still comes back as JSON.
_JS_ENVELOPE = """
(function() {
  try {
    var __v = (function() { %s })();
    if (__v === undefined) { return JSON.stringify({ok: true, value: null}); }
    try { return JSON.stringify({ok: true, value: __v}); }
    catch (e) { return JSON.stringify({ok: true, value: String(__v)}); }
  } catch (e) {
    return JSON.stringify({ok: false, error: String(e && e.message ? e.message : e),
                           stack: e && e.stack ? String(e.stack) : null});
  }
})()
"""


def _js_literal(code: str) -> str:
    """Embed arbitrary JS source as a JXA string literal (double-encoded on purpose)."""
    return json.dumps(code)


def _target_selector(window: Optional[int], tab: Optional[int]) -> str:
    return f"windowAt({json.dumps(window)}), tabAt(windowAt({json.dumps(window)}), {json.dumps(tab)})"


# --------------------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------------------

def state(window: Optional[int] = None, tab: Optional[int] = None) -> dict[str, Any]:
    """Windows, tabs, titles and URLs. Never needs ``do JavaScript``."""
    body = f"""
    const ws = windowsSafe();
    const out = [];
    for (let wi = 0; wi < ws.length; wi++) {{
      const w = ws[wi];
      let tabs = [];
      let curIdx = null;
      try {{
        const cur = w.currentTab();
        const curUrl = cur.url(), curName = cur.name();
        const ts0 = w.tabs();
        for (let i = 0; i < ts0.length; i++) {{
          try {{ if (ts0[i].url() === curUrl && ts0[i].name() === curName) {{ curIdx = i; break; }} }} catch (e) {{}}
        }}
      }} catch (e) {{}}
      try {{
        tabs = w.tabs().map((t, ti) => ({{
          index: ti,
          title: (function() {{ try {{ return t.name(); }} catch (e) {{ return ''; }} }})(),
          url: (function() {{ try {{ return t.url(); }} catch (e) {{ return ''; }} }})(),
          current: ti === curIdx,
          loading: (function() {{ try {{ return t.loading ? t.loading() : null; }} catch (e) {{ return null; }} }})(),
        }}));
      }} catch (e) {{ tabs = []; }}
      out.push({{
        index: wi,
        name: (function() {{ try {{ return w.name(); }} catch (e) {{ return ''; }} }})(),
        bounds: (function() {{ try {{ return w.bounds(); }} catch (e) {{ return null; }} }})(),
        visible: (function() {{ try {{ return w.visible(); }} catch (e) {{ return null; }} }})(),
        currentTabIndex: curIdx,
        tabs: tabs,
      }});
    }}
    return emit({{ok: true, frontmost: Safari.frontmost ? Safari.frontmost() : null,
                 running: true, windows: out}});
    """
    result = jxa.run_json(jxa._wrap(body))
    return jxa._checked(result)


def focused(window: Optional[int] = None, tab: Optional[int] = None) -> dict[str, Any]:
    """The one tab a follow-up action will hit, plus its viewport size."""
    body = f"""
    const w = windowAt({json.dumps(window)});
    const t = tabAt(w, {json.dumps(tab)});
    return emit({{ok: true,
      window: (function() {{ try {{ return w.name(); }} catch (e) {{ return ''; }} }})(),
      title: (function() {{ try {{ return t.name(); }} catch (e) {{ return ''; }} }})(),
      url: (function() {{ try {{ return t.url(); }} catch (e) {{ return ''; }} }})(),
      loading: (function() {{ try {{ return t.loading ? t.loading() : null; }} catch (e) {{ return null; }} }})(),
      bounds: (function() {{ try {{ return w.bounds(); }} catch (e) {{ return null; }} }})(),
    }});
    """
    return jxa._checked(jxa.run_json(jxa._wrap(body)))


# --------------------------------------------------------------------------------------
# Tabs and navigation
# --------------------------------------------------------------------------------------

def open_url(url: str, *, new_window: bool = False, new_tab: bool = True,
             window: Optional[int] = None, activate: bool = True,
             keep: bool = False, close_after: Optional[float] = None) -> dict[str, Any]:
    """Open ``url`` in Safari, in a new tab (default) or the current one.

    A new tab is registered with the tab lifecycle so it is closed after the
    idle window unless ``keep`` is set. Reusing the current tab is not recorded:
    that tab may be one the user opened, and this plugin never closes those.
    """
    if not url or not url.strip():
        raise SafariError("url is required")
    url_literal = json.dumps(url.strip())
    if new_window:
        body = f"""
        const w = Safari.Window();
        Safari.windows.push(w);
        w.currentTab = Safari.Tab({{url: {url_literal}}});
        { "Safari.activate();" if activate else "" }
        return emit({{ok: true, window: Safari.windows().length - 1, url: {url_literal}}});
        """
    elif new_tab:
        body = f"""
        let w;
        try {{ w = windowAt({json.dumps(window)}); }}
        catch (e) {{
          w = Safari.Window();
          Safari.windows.push(w);
        }}
        const t = Safari.Tab({{url: {url_literal}}});
        w.tabs.push(t);
        try {{ w.currentTab = t; }} catch (e) {{}}
        { "Safari.activate();" if activate else "" }
        return emit({{ok: true, url: {url_literal}}});
        """
    else:
        body = f"""
        const w = windowAt({json.dumps(window)});
        tabAt(w, null).url = {url_literal};
        { "Safari.activate();" if activate else "" }
        return emit({{ok: true, url: {url_literal}}});
        """
    result = jxa._checked(jxa.run_json(jxa._wrap(body)))
    if new_tab or new_window:
        try:
            win_index = int(result.get("window", 0)) if isinstance(result, dict) else 0
            tab_lifecycle.record(win_index, url.strip(), "", keep=keep, close_after=close_after)
        except Exception:  # health: registry bookkeeping must not fail the open
            logger.debug("safari tab registry record failed", exc_info=True)
    return result


def navigate(url: str, *, window: Optional[int] = None, tab: Optional[int] = None) -> dict[str, Any]:
    """Point the current tab at ``url`` (no new tab)."""
    body = f"""
    const w = windowAt({json.dumps(window)});
    const t = tabAt(w, {json.dumps(tab)});
    t.url = {json.dumps(url)};
    return emit({{ok: true, url: {json.dumps(url)}}});
    """
    return jxa._checked(jxa.run_json(jxa._wrap(body)))


def close_tab(index: int, *, window: Optional[int] = None) -> dict[str, Any]:
    body = f"""
    const w = windowAt({json.dumps(window)});
    const t = tabAt(w, {json.dumps(int(index))});
    t.close();
    return emit({{ok: true, closed: {json.dumps(int(index))}}});
    """
    return jxa._checked(jxa.run_json(jxa._wrap(body)))


def _close_by_url(url: str, *, window: Optional[int] = None) -> bool:
    """Close the first tab in ``window`` whose URL matches. True if one closed."""
    body = f"""
    const w = windowAt({json.dumps(window)});
    const want = {json.dumps(url)};
    const ts = w.tabs();
    for (let i = 0; i < ts.length; i++) {{
      let u = '';
      try {{ u = ts[i].url(); }} catch (e) {{ continue; }}
      if (u === want) {{ ts[i].close(); return emit({{ok: true, closed: i}}); }}
    }}
    return emit({{ok: true, closed: null}});
    """
    try:
        result = jxa._checked(jxa.run_json(jxa._wrap(body)))
    except SafariError as exc:
        logger.debug("safari sweep close failed for %s: %s", url, exc)
        return False
    return isinstance(result, dict) and result.get("closed") is not None


def sweep_expired_tabs() -> dict[str, Any]:
    """Close Hermes-opened tabs that have been idle past their window.

    Safe to call at any time: only tabs in the lifecycle registry are eligible,
    and a tab the user already closed is simply dropped from the registry.
    """
    due = tab_lifecycle.expired()
    if not due:
        return {"closed": 0, "tabs": []}
    closed: list[dict[str, Any]] = []
    for rec in due:
        url = rec.get("url") or ""
        if not url:
            closed.append(rec)
            continue
        if _close_by_url(url, window=rec.get("window")):
            closed.append(rec)
        else:
            # The user closed it, or it navigated away — either way stop tracking.
            closed.append(rec)
    tab_lifecycle.prune(closed)
    return {"closed": len(closed), "tabs": [{"url": r.get("url"), "title": r.get("title")}
                                            for r in closed]}


def select_tab(index: int, *, window: Optional[int] = None) -> dict[str, Any]:
    body = f"""
    const w = windowAt({json.dumps(window)});
    const t = tabAt(w, {json.dumps(int(index))});
    w.currentTab = t;
    return emit({{ok: true, selected: {json.dumps(int(index))}}});
    """
    return jxa._checked(jxa.run_json(jxa._wrap(body)))


def back(window: Optional[int] = None, tab: Optional[int] = None) -> dict[str, Any]:
    body = f"""
    const w = windowAt({json.dumps(window)});
    const t = tabAt(w, {json.dumps(tab)});
    const before = t.url();
    try {{ Safari.doJavaScript('history.back()', {{in: t}}); }}
    catch (e) {{ return emit({{ok: false, error: 'history.back() needs do JavaScript: ' + e.message}}); }}
    return emit({{ok: true, from: before}});
    """
    return jxa._checked(jxa.run_json(jxa._wrap(body)))


# --------------------------------------------------------------------------------------
# DOM: run JavaScript in the page
# --------------------------------------------------------------------------------------

def run_js(code: str, *, window: Optional[int] = None, tab: Optional[int] = None,
           timeout: float = jxa.JS_TIMEOUT) -> Any:
    """Evaluate ``code`` in the page, returning its JSON-serialisable value."""
    if not code or not code.strip():
        raise SafariError("code is required")
    envelope = _JS_ENVELOPE % code
    body = f"""
    const w = windowAt({json.dumps(window)});
    const t = tabAt(w, {json.dumps(tab)});
    let raw;
    try {{ raw = Safari.doJavaScript({_js_literal(envelope)}, {{in: t}}); }}
    catch (e) {{ return emit({{ok: false, error: String(e.message || e), code: e.errorNumber || null}}); }}
    let parsed;
    try {{ parsed = JSON.parse(raw); }}
    catch (e) {{ return emit({{ok: true, value: raw}}); }}
    return emit({{ok: true, parsed: parsed}});
    """
    outer = jxa._checked(jxa.run_json(jxa._wrap(body), timeout=timeout))
    parsed = outer.get("parsed") if isinstance(outer, dict) else None
    if isinstance(parsed, dict) and parsed.get("ok") is False:
        raise SafariError(f"page JavaScript error: {parsed.get('error')}",
                          raw=str(parsed.get("stack") or ""))
    if isinstance(parsed, dict) and "value" in parsed:
        return parsed["value"]
    return parsed


def page_text(*, window: Optional[int] = None, tab: Optional[int] = None,
              limit: int = 20_000) -> dict[str, Any]:
    """Readable page text plus title/url — the cheap "what is on screen" call."""
    value = run_js(
        "return {title: document.title, url: location.href, "
        "text: (document.body ? document.body.innerText : '').slice(0, %d)};" % int(limit),
        window=window, tab=tab)
    out = value if isinstance(value, dict) else {"text": value}
    if isinstance(out, dict) and out.get("url"):
        try:
            tab_lifecycle.touch(str(out["url"]), str(out.get("title") or ""), window=window)
        except Exception:  # health: bookkeeping must not fail a read
            logger.debug("safari tab touch failed", exc_info=True)
    return out


def find_elements(selector: str, *, window: Optional[int] = None, tab: Optional[int] = None,
                  limit: int = 20) -> dict[str, Any]:
    """List matches for a CSS selector with rects, so a native click can follow."""
    code = """
    var sel = %s, lim = %d;
    var nodes = Array.prototype.slice.call(document.querySelectorAll(sel)).slice(0, lim);
    return nodes.map(function(n, i) {
      var r = n.getBoundingClientRect();
      return {index: i, tag: n.tagName.toLowerCase(),
              text: (n.innerText || n.value || '').trim().slice(0, 200),
              id: n.id || '', cls: (n.className && n.className.toString ? n.className.toString() : '').slice(0, 120),
              href: n.href || '', type: n.type || '',
              rect: {x: r.x, y: r.y, w: r.width, h: r.height},
              visible: !!(r.width && r.height)};
    });
    """ % (json.dumps(selector), int(limit))
    value = run_js(code, window=window, tab=tab)
    return {"selector": selector, "matches": value if isinstance(value, list) else []}


# --------------------------------------------------------------------------------------
# Input: native clicks and typing
# --------------------------------------------------------------------------------------

def _cliclick() -> Optional[str]:
    return shutil.which("cliclick")


def _window_origin(window: Optional[int]) -> tuple[int, int, dict[str, Any]]:
    """Screen-space origin of the page viewport for a window, via JXA bounds."""
    info = focused(window=window)
    bounds = info.get("bounds") or {}
    return int(bounds.get("x", 0)), int(bounds.get("y", 0)), info


def _chrome_offsets(info: dict[str, Any]) -> tuple[int, int]:
    """Height between the window's top edge and the page viewport's top edge.

    Safari's window bounds include the tab bar and toolbar, and there is no API to
    read the viewport origin. A hardcoded constant drifts with Safari version,
    tab-bar style and zoom, so measure it once per call: ask the page for its own
    viewport height and compare against the window height minus the known chrome.
    Falls back to the calibrated constant when the page cannot be measured.
    """
    try:
        vertical = int(os.environ.get("SAFARI_TOOLBAR_HEIGHT", "0") or 0)
    except ValueError:
        vertical = 0
    if vertical:
        return 0, vertical

    # Measure: window height - page innerHeight = total chrome (toolbar + tab bar).
    try:
        metrics = run_js("return {ih: innerHeight, oh: outerHeight};")
        win_h = int((info.get("bounds") or {}).get("height") or 0)
        inner_h = int(metrics.get("ih") or 0)
        if win_h and inner_h and 0 < win_h - inner_h < 200:
            return 0, win_h - inner_h
    except SafariError:
        pass
    # Safari 17+ compact tab bar + toolbar measures ~79px at default zoom.
    return 0, 79


def click_native(x: float, y: float, *, window: Optional[int] = None,
                 button: str = "left", clicks: int = 1) -> dict[str, Any]:
    """Click at viewport coordinates using a real OS-level mouse event."""
    # A native click goes wherever the cursor is, so Safari must own the screen
    # first — otherwise the click just activates the window and is swallowed.
    jxa.activate_safari()
    time.sleep(0.15)
    ox, oy, info = _window_origin(window)
    dx, dy = _chrome_offsets(info)
    sx, sy = int(ox + x + dx), int(oy + y + dy)
    cli = _cliclick()
    if cli:
        flag = {"left": "c", "right": "rc", "middle": "mc"}.get(button, "c")
        args = [cli]
        for _ in range(max(1, int(clicks))):
            args.append(f"{flag}:{sx},{sy}")
        proc = subprocess.run(args, capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            raise SafariError(f"cliclick failed: {proc.stderr.strip() or proc.returncode}")
    else:
        # No cliclick: fall back to System Events, which needs Accessibility consent.
        script = (
            f'tell application "System Events" to click at {{{sx}, {sy}}}'
        )
        proc = subprocess.run(["/usr/bin/osascript", "-e", script],
                              capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            raise SafariError(
                "No native click available: install cliclick (`brew install cliclick`) "
                "or grant Accessibility to this binary for System Events. "
                f"Underlying error: {proc.stderr.strip()}")
    return {"clicked": {"viewport": [x, y], "screen": [sx, sy], "button": button,
                        "clicks": clicks, "method": "cliclick" if cli else "system-events"}}


def click_selector(selector: str, *, window: Optional[int] = None, tab: Optional[int] = None,
                   button: str = "left", clicks: int = 1, index: int = 0) -> dict[str, Any]:
    """Scroll an element into view, then click it natively at its centre."""
    code = """
    var nodes = document.querySelectorAll(%s);
    var n = nodes[%d];
    if (!n) { throw new Error('no element matches ' + %s + ' at index %d'); }
    n.scrollIntoView({block: 'center', inline: 'center'});
    var r = n.getBoundingClientRect();
    return {x: r.x + r.width / 2, y: r.y + r.height / 2, w: r.width, h: r.height,
            tag: n.tagName.toLowerCase(), text: (n.innerText || '').trim().slice(0, 120)};
    """ % (json.dumps(selector), int(index), json.dumps(selector), int(index))
    rect = run_js(code, window=window, tab=tab)
    if not isinstance(rect, dict):
        raise SafariError(f"could not locate {selector!r}")
    if not rect.get("w") or not rect.get("h"):
        raise SafariError(f"{selector!r} has no layout box (hidden or zero-size)")
    result = click_native(rect["x"], rect["y"], window=window, button=button, clicks=clicks)
    result["element"] = rect
    return result


def type_text(text: str, *, window: Optional[int] = None, tab: Optional[int] = None,
              selector: Optional[str] = None, submit: bool = False) -> dict[str, Any]:
    """Type into the focused element, or into ``selector`` after focusing it.

    Uses ``do JavaScript`` to set the value through the native setter so React and
    friends see the change, then dispatches input/change events. Falls back to
    System Events keystrokes when JS is unavailable.
    """
    if selector:
        focus = """
        var n = document.querySelector(%s);
        if (!n) { throw new Error('no element matches ' + %s); }
        n.focus();
        """ % (json.dumps(selector), json.dumps(selector))
        run_js(focus, window=window, tab=tab)
    code = """
    var el = document.activeElement;
    if (!el) { throw new Error('no focused element'); }
    var text = %s;
    var proto = el instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : (el instanceof HTMLInputElement ? HTMLInputElement.prototype : null);
    var setter = proto && Object.getOwnPropertyDescriptor(proto, 'value');
    if (setter && setter.set) { setter.set.call(el, text); } else { el.value = text; }
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    return {tag: el.tagName.toLowerCase(), value: (el.value || '').slice(0, 200)};
    """ % json.dumps(text)
    result = run_js(code, window=window, tab=tab)
    if submit:
        run_js(
            "var f = document.activeElement && document.activeElement.form;"
            "if (f) { if (f.requestSubmit) f.requestSubmit(); else f.submit(); return 'submitted'; }"
            "document.activeElement && document.activeElement.dispatchEvent("
            "new KeyboardEvent('keydown', {key: 'Enter', keyCode: 13, bubbles: true}));"
            "return 'enter';", window=window, tab=tab)
    return result if isinstance(result, dict) else {"value": result}


# --------------------------------------------------------------------------------------
# Screenshot
# --------------------------------------------------------------------------------------

def screenshot(window: Optional[int] = None, *, full_page: bool = False) -> dict[str, Any]:
    """Capture the Safari window to a PNG in the scratch dir.

    ``screencapture -l <windowid>`` is the only reliable way to grab a Safari window
    (its window id comes from CoreGraphics, not AppleScript), so this asks
    System Events for the window bounds and captures that rect instead.
    """
    info = focused(window=window)
    bounds = info.get("bounds") or {}
    if not bounds:
        raise SafariError("Safari window has no bounds to capture")
    out_dir = os.environ.get("TMPDIR") or tempfile.gettempdir()
    path = os.path.join(out_dir, f"safari-shot-{os.getpid()}-{abs(hash(str(bounds))) % 100000}.png")
    rect = f"{int(bounds.get('x', 0))},{int(bounds.get('y', 0))},{int(bounds.get('width', 0))},{int(bounds.get('height', 0))}"
    proc = subprocess.run(["/usr/sbin/screencapture", "-x", "-R", rect, path],
                          capture_output=True, text=True, timeout=20)
    if proc.returncode != 0 or not os.path.exists(path):
        raise SafariError(
            "screencapture failed — Screen Recording permission is required for this "
            f"binary. {proc.stderr.strip()}")
    return {"path": path, "bounds": bounds, "url": info.get("url"), "title": info.get("title")}
