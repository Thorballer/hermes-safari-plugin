"""safari plugin — drive the user's real Safari on macOS.

Safari exposes no Chrome DevTools Protocol endpoint, so ``browser_exec`` cannot
touch it. This plugin supplies its own toolset built on the two transports macOS
actually offers: AppleScript/JXA (``osascript``) and, when enabled, ``safaridriver``.

Design rules this module keeps:

* **Never guess a permission.** Every tool reports the specific macOS gate that
  stopped it (Automation consent, Develop > Allow JavaScript from Apple Events,
  Accessibility for native clicks, Screen Recording for screenshots) so the model
  can tell the user exactly which checkbox to tick.
* **Degrade, don't fail.** ``safari_state`` and ``safari_open`` need only
  Automation consent. DOM tools need ``do JavaScript``. Native clicks need
  ``cliclick`` or Accessibility. Each tool declares its own requirement.
* **One tab per task.** Tools default to the front window's current tab and accept
  explicit ``window``/``tab`` indices so a caller can pin itself to a tab.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any, Callable, Optional

from . import actions, jxa
from .jxa import JsDisabled, NotPermitted, SafariError

logger = logging.getLogger(__name__)

TOOLSET = "safari"


def _ok(payload: Any) -> str:
    """Wrap a tool result. Handlers may return a dict (merged) or any bare JSON value."""
    if isinstance(payload, dict):
        return json.dumps({"success": True, **payload}, default=str)
    return json.dumps({"success": True, "value": payload}, default=str)


def _err(exc: Exception, *, hint: str = "") -> str:
    """Uniform failure envelope: the message, the gate that failed, and a fix hint."""
    if isinstance(exc, JsDisabled):
        gate, fix = "javascript_from_apple_events", (
            "Safari > Settings > Advanced > enable 'Show features for web developers', "
            "then Develop > Allow JavaScript from Apple Events.")
    elif isinstance(exc, NotPermitted):
        gate, fix = "automation_consent", (
            "System Settings > Privacy & Security > Automation > allow this Python "
            "binary to control Safari.")
    elif isinstance(exc, SafariError) and getattr(exc, "code", None) == -1712:
        gate, fix = "apple_event_timeout", (
            "Safari did not answer the AppleEvent. A macOS consent dialog is probably "
            "waiting on screen — click Allow, then retry.")
    else:
        gate, fix = "safari_error", hint
    body: dict[str, Any] = {
        "success": False,
        "error": str(exc),
        "gate": gate,
        "raw": getattr(exc, "raw", "")[:600],
    }
    if getattr(exc, "code", None) is not None:
        body["code"] = getattr(exc, "code", None)
    if fix:
        body["fix"] = fix
    return json.dumps(body, default=str)


def _guard(fn: Callable[..., Any], **kwargs: Any) -> str:
    try:
        return _ok(fn(**kwargs) if kwargs else fn())
    except SafariError as exc:
        return _err(exc)
    except Exception as exc:  # health: allow BLE001 -- a tool must never raise into the loop
        logger.debug("safari tool failed", exc_info=True)
        return _err(SafariError(f"{type(exc).__name__}: {exc}"))


# --------------------------------------------------------------------------------------
# check_fns — cheap, no socket, no AppleEvent
# --------------------------------------------------------------------------------------

def _available() -> bool:
    return jxa.safari_installed()


# --------------------------------------------------------------------------------------
# Tool handlers. Each takes the raw args dict and returns a JSON string.
# --------------------------------------------------------------------------------------

def _tool_open(args: dict[str, Any]) -> str:
    url = str(args.get("url") or "").strip()
    if not url:
        return json.dumps({"success": False, "error": "url is required"})
    # Opportunistic sweep: closing stale tabs here means an idle Hermes never
    # needs a timer, and the next open always starts from a tidy window.
    try:
        actions.sweep_expired_tabs()
    except Exception:  # health: a sweep must never block an open
        logger.debug("safari sweep before open failed", exc_info=True)
    return _guard(
        actions.open_url, url=url,
        new_window=bool(args.get("new_window", False)),
        new_tab=bool(args.get("new_tab", True)),
        window=args.get("window"),
        activate=bool(args.get("activate", True)),
        keep=bool(args.get("keep", False)),
        close_after=args.get("close_after"),
    )


def _tool_state(args: dict[str, Any]) -> str:
    try:
        actions.sweep_expired_tabs()
    except Exception:  # health: a sweep must never block a read
        logger.debug("safari sweep before state failed", exc_info=True)
    return _guard(actions.state)


def _tool_js(args: dict[str, Any]) -> str:
    code = str(args.get("code") or "")
    if not code.strip():
        return json.dumps({"success": False, "error": "code is required"})
    return _guard(actions.run_js, code=code, window=args.get("window"), tab=args.get("tab"))


def _tool_act(args: dict[str, Any]) -> str:
    """Composite action: read the page, or find elements, in one call."""
    action = str(args.get("action") or "text").lower()
    window, tab = args.get("window"), args.get("tab")
    if action == "text":
        return _guard(actions.page_text, window=window, tab=tab,
                      limit=int(args.get("limit") or 20_000))
    if action == "find":
        selector = str(args.get("selector") or "")
        if not selector:
            return json.dumps({"success": False, "error": "selector is required for action=find"})
        return _guard(actions.find_elements, selector=selector, window=window, tab=tab,
                      limit=int(args.get("limit") or 20))
    if action == "focused":
        return _guard(actions.focused, window=window, tab=tab)
    if action == "back":
        return _guard(actions.back, window=window, tab=tab)
    return json.dumps({"success": False, "error": f"unknown action {action!r}",
                       "valid": ["text", "find", "focused", "back"]})


def _tool_click(args: dict[str, Any]) -> str:
    window, tab = args.get("window"), args.get("tab")
    selector = args.get("selector")
    if selector:
        return _guard(actions.click_selector, selector=str(selector), window=window, tab=tab,
                      button=str(args.get("button") or "left"),
                      clicks=int(args.get("clicks") or 1),
                      index=int(args.get("index") or 0))
    x, y = args.get("x"), args.get("y")
    if x is None or y is None:
        return json.dumps({"success": False,
                           "error": "provide selector, or both x and y (viewport coordinates)"})
    return _guard(actions.click_native, x=float(x), y=float(y), window=window,
                  button=str(args.get("button") or "left"),
                  clicks=int(args.get("clicks") or 1))


def _tool_type(args: dict[str, Any]) -> str:
    text = args.get("text")
    if text is None:
        return json.dumps({"success": False, "error": "text is required"})
    return _guard(actions.type_text, text=str(text), window=args.get("window"),
                  tab=args.get("tab"), selector=args.get("selector"),
                  submit=bool(args.get("submit", False)))


def _tool_tabs(args: dict[str, Any]) -> str:
    action = str(args.get("action") or "list").lower()
    window, index = args.get("window"), args.get("index")
    if action == "list":
        return _guard(actions.state)
    if action == "select":
        if index is None:
            return json.dumps({"success": False, "error": "index is required for action=select"})
        return _guard(actions.select_tab, index=int(index), window=window)
    if action == "close":
        if index is None:
            return json.dumps({"success": False, "error": "index is required for action=close"})
        return _guard(actions.close_tab, index=int(index), window=window)
    if action == "navigate":
        url = str(args.get("url") or "")
        if not url:
            return json.dumps({"success": False, "error": "url is required for action=navigate"})
        return _guard(actions.navigate, url=url, window=window, tab=index)
    return json.dumps({"success": False, "error": f"unknown action {action!r}",
                       "valid": ["list", "select", "close", "navigate"]})


def _tool_screenshot(args: dict[str, Any]) -> str:
    return _guard(actions.screenshot, window=args.get("window"))


# --------------------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------------------

_W_TAB = {
    "window": {"type": "integer", "description": "Window index (0 = front window). Omit for the front window."},
    "tab": {"type": "integer", "description": "Tab index in that window. Omit for the current tab."},
}

_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "safari_open",
        "description": (
            "Open a URL in the user's real Safari, in a new tab by default. This is the "
            "preferred way to browse — use it instead of any Chrome/CDP browser tool. "
            "Tabs opened here are closed automatically after an hour idle; pass keep=true "
            "to leave one open permanently, or close_after to set a different window. "
            "Needs only macOS Automation consent."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to open."},
                "new_tab": {"type": "boolean", "description": "Open in a new tab (default true)."},
                "new_window": {"type": "boolean", "description": "Open in a brand-new window instead."},
                "window": {"type": "integer", "description": "Target window index for new_tab."},
                "activate": {"type": "boolean", "description": "Bring Safari to the front (default true)."},
                "keep": {
                    "type": "boolean",
                    "description": "Never auto-close this tab. Use when the user wants a page left open.",
                },
                "close_after": {
                    "type": "number",
                    "description": "Idle seconds before this tab closes (default 3600). Ignored if keep is true.",
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "safari_state",
        "description": (
            "List Safari's windows and tabs with titles, URLs and which tab is current. "
            "Call this first to learn the window/tab indices other safari tools take. "
            "Needs only Automation consent."
        ),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "safari_act",
        "description": (
            "Read the current Safari page. action=text returns title, URL and visible text; "
            "action=find returns elements matching a CSS selector with their rects (so "
            "safari_click can hit them); action=focused returns the active tab summary; "
            "action=back goes back in history. Requires 'Allow JavaScript from Apple Events' "
            "except for action=focused."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["text", "find", "focused", "back"],
                           "description": "What to read or do. Default text."},
                "selector": {"type": "string", "description": "CSS selector, for action=find."},
                "limit": {"type": "integer", "description": "Max chars (text) or matches (find)."},
                **_W_TAB,
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "safari_js",
        "description": (
            "Evaluate JavaScript in the current Safari tab and return its value. The code runs "
            "as a function body, so use `return` to produce a value. Requires Develop > "
            "'Allow JavaScript from Apple Events'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "JS function body; use return for the value."},
                **_W_TAB,
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    },
    {
        "name": "safari_click",
        "description": (
            "Click in Safari. Give a CSS selector to scroll the element into view and click its "
            "centre, or x/y viewport coordinates for a raw click. Uses a real OS-level mouse "
            "event (cliclick), which works on sites that reject synthetic JS clicks. Selector "
            "mode needs JavaScript from Apple Events; native clicking needs cliclick or "
            "Accessibility permission."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector to click."},
                "index": {"type": "integer", "description": "Which match to click (default 0)."},
                "x": {"type": "number", "description": "Viewport x, when not using a selector."},
                "y": {"type": "number", "description": "Viewport y, when not using a selector."},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "clicks": {"type": "integer", "description": "Click count (2 = double-click)."},
                **_W_TAB,
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "safari_type",
        "description": (
            "Type text into the focused element or into a CSS selector, using the native value "
            "setter so React/Vue inputs register the change. Optionally submit the form. "
            "Requires Develop > 'Allow JavaScript from Apple Events'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to enter."},
                "selector": {"type": "string", "description": "Field to focus first (optional)."},
                "submit": {"type": "boolean", "description": "Submit the form / press Enter after typing."},
                **_W_TAB,
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "safari_tabs",
        "description": (
            "Manage Safari tabs: list (same as safari_state), select, close, or navigate the "
            "current tab to a URL. Needs only Automation consent."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "select", "close", "navigate"]},
                "index": {"type": "integer", "description": "Tab index for select/close/navigate."},
                "url": {"type": "string", "description": "URL for action=navigate."},
                "window": {"type": "integer", "description": "Window index."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "safari_screenshot",
        "description": (
            "Capture the front Safari window to a PNG and return its path (pass it to a vision "
            "tool to look at the page). Needs Screen Recording permission. This is the fallback "
            "for reading a page when JavaScript from Apple Events is unavailable."
        ),
        "parameters": {
            "type": "object",
            "properties": {"window": {"type": "integer", "description": "Window index."}},
            "additionalProperties": False,
        },
    },
]

_HANDLERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "safari_open": _tool_open,
    "safari_state": _tool_state,
    "safari_js": _tool_js,
    "safari_act": _tool_act,
    "safari_click": _tool_click,
    "safari_type": _tool_type,
    "safari_tabs": _tool_tabs,
    "safari_screenshot": _tool_screenshot,
}

_EMOJI = {
    "safari_open": "🧭", "safari_state": "🪟", "safari_js": "⚡", "safari_act": "📄",
    "safari_click": "🖱️", "safari_type": "⌨️", "safari_tabs": "🗂️", "safari_screenshot": "📸",
}


def _make_handler(name: str) -> Callable[..., str]:
    """Bind a tool name to its handler; the registry passes (args, **kwargs)."""
    def handler(args: dict[str, Any], **kwargs: Any) -> str:
        return _HANDLERS[name](dict(args or {}))
    handler.__name__ = f"safari_{name}_handler"
    return handler


# --------------------------------------------------------------------------------------
# CLI: `hermes safari status|doctor|open`
# --------------------------------------------------------------------------------------

def _cli_setup(parser) -> None:
    sub = parser.add_subparsers(dest="safari_cmd")
    sub.add_parser("status", help="Show Safari automation capabilities and permission gates")
    sub.add_parser("doctor", help="Diagnose every permission gate and print the fix for each")
    p_open = sub.add_parser("open", help="Open a URL in Safari")
    p_open.add_argument("url", help="URL to open")


def _cli_handler(args) -> int:
    cmd = getattr(args, "safari_cmd", None) or "status"
    if cmd == "open":
        print(_tool_open({"url": args.url}))
        return 0

    report: dict[str, Any] = {
        "safari_installed": jxa.safari_installed(),
        "safari_running": jxa.safari_running(),
        "automation": jxa.permission_probe(),
        "cliclick": bool(actions._cliclick()),
        "allow_js_from_apple_events": None,
        "js_probe": None,
    }
    if report["safari_installed"]:
        # Read the Develop-menu toggle straight from Safari's prefs. NSUserDefaults from
        # inside osascript cannot see com.apple.Safari's domain (it answers [id nil]), but
        # `defaults read` does — and it needs no AppleEvent, so this probe never hangs.
        try:
            proc = subprocess.run(
                ["/usr/bin/defaults", "read", jxa.SAFARI_BUNDLE, "AllowJavaScriptFromAppleEvents"],
                capture_output=True, text=True, timeout=10)
            value = (proc.stdout or "").strip()
            report["js_probe"] = value or (proc.stderr or "").strip()
            report["allow_js_from_apple_events"] = value in ("1", "true", "YES")
        except (OSError, subprocess.SubprocessError) as exc:
            report["js_probe"] = f"probe failed: {exc}"

    print(json.dumps(report, indent=2, default=str))

    if cmd == "doctor":
        print("\nGates:")
        auto = report["automation"]
        print(f"  Automation -> Safari : {auto['state']}  ({auto['detail']})")
        print(f"  Develop > Allow JS    : {report['allow_js_from_apple_events']}")
        print(f"  cliclick present      : {report['cliclick']}")
        if auto["state"] != "granted":
            print("  FIX: System Settings > Privacy & Security > Automation > allow Safari.")
        if report["allow_js_from_apple_events"] is not True:
            print("  FIX: Safari > Settings > Advanced > 'Show features for web developers', "
                  "then Develop > Allow JavaScript from Apple Events.")
        if not report["cliclick"]:
            print("  FIX: brew install cliclick (needed for real mouse clicks).")
    return 0


# --------------------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------------------

def register(ctx) -> None:
    for schema in _SCHEMAS:
        name = schema["name"]
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schema,
            handler=_make_handler(name),
            check_fn=_available,
            description=schema["description"].split(".")[0] + ".",
            emoji=_EMOJI.get(name, "🧭"),
        )
    ctx.register_cli_command(
        name="safari",
        help="Drive the user's real Safari (status, doctor, open)",
        setup_fn=_cli_setup,
        handler_fn=_cli_handler,
        description="Inspect Safari automation permission gates and open URLs.",
    )

    # Expire Hermes-opened tabs at the end of a session, so a closed chat never
    # leaves tabs behind. The sweep is also run opportunistically before opens.
    def _on_session_end(**_kwargs: Any) -> None:
        try:
            actions.sweep_expired_tabs()
        except Exception:  # health: teardown must never raise into the host
            logger.debug("safari session-end sweep failed", exc_info=True)

    try:
        ctx.register_hook("on_session_end", _on_session_end)
    except Exception:  # health: hook registration is best-effort
        logger.debug("safari on_session_end hook unavailable", exc_info=True)

    logger.info("safari plugin registered %d tools", len(_SCHEMAS))
