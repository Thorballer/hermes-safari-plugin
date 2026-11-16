"""JXA bridge to Safari.

Safari exposes no CDP endpoint, so the transport here is ``osascript -l JavaScript``
(JXA) driving Safari's AppleScript dictionary, plus ``do JavaScript`` for DOM work.
Two hard facts shape the whole module:

1. **``do JavaScript`` needs Develop > "Allow JavaScript from Apple Events".**
   Without it Safari raises error ``-1723`` / "is not allowed to run JavaScript".
   Every JS path funnels through :func:`js_available` so the caller can degrade
   to AppleScript-only actions with an actionable message instead of a traceback.
2. **Automation consent is per (client binary, Safari) pair.** The TCC database
   holds ``kTCCServiceAppleEvents`` rows keyed on the exact executable path, so a
   Python upgrade silently resets consent. :func:`permission_probe` reports that
   state explicitly rather than letting the first real call hang on a dialog.

Every JXA snippet is run with ``osascript`` and must end in a JSON string; the
helpers here build that envelope so callers only deal with Python dicts.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

OSASCRIPT = "/usr/bin/osascript"
SAFARI_BUNDLE = "com.apple.Safari"

# osascript blocks while a TCC consent dialog waits on the user, so the default is
# deliberately short: a timeout is recoverable, a wedged agent turn is not.
DEFAULT_TIMEOUT = 25.0
JS_TIMEOUT = 30.0
MAX_OUTPUT_CHARS = 200_000


class SafariError(RuntimeError):
    """A Safari/AppleScript failure with the raw error text preserved."""

    def __init__(self, message: str, *, code: Optional[int] = None, raw: str = ""):
        super().__init__(message)
        self.code = code
        self.raw = raw


class JsDisabled(SafariError):
    """``do JavaScript`` is refused: Develop > Allow JavaScript from Apple Events is off."""


class NotPermitted(SafariError):
    """macOS automation consent for Safari has not been granted to this binary."""


def _osa_error_code(text: str) -> Optional[int]:
    """Pull the AppleScript error number out of osascript's stderr (``(-1723)``)."""
    for token in text.replace("(", " ").replace(")", " ").split():
        stripped = token.strip(",;")
        if stripped.startswith("-") and stripped[1:].isdigit():
            return int(stripped)
    return None


def _classify(text: str) -> SafariError:
    """Map an osascript failure onto the most specific error type we can justify."""
    lowered = text.lower()
    code = _osa_error_code(text)
    if code == -1723 or "not allowed to run javascript" in lowered:
        return JsDisabled(
            "Safari refused `do JavaScript`: enable Develop > Allow JavaScript from "
            "Apple Events (Safari Settings > Advanced > Show features for web "
            "developers first).",
            code=code, raw=text)
    if code in (-1743, -1744) or "not authorized" in lowered or "not permitted" in lowered:
        return NotPermitted(
            "macOS has not granted automation permission for Safari to this Python "
            "binary. Approve the prompt, or grant it in System Settings > Privacy & "
            "Security > Automation.",
            code=code, raw=text)
    if "application isn't running" in lowered or code == -600:
        return SafariError("Safari is not running.", code=code, raw=text)
    if code == -1719 or "invalid index" in lowered:
        return SafariError("That Safari window or tab index does not exist.", code=code, raw=text)
    return SafariError(text.strip() or "osascript failed", code=code, raw=text)


def run_jxa(script: str, *, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Run a JXA script and return raw stdout. Raises :class:`SafariError` on failure."""
    if not os.path.exists(OSASCRIPT):
        raise SafariError("osascript is missing — this plugin only runs on macOS.")
    try:
        proc = subprocess.run(
            [OSASCRIPT, "-l", "JavaScript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SafariError(
            f"osascript timed out after {timeout}s. A macOS permission dialog may be "
            f"waiting for you, or Safari is showing a modal sheet.",
            raw=str(exc)) from exc
    if proc.returncode != 0:
        raise _classify(proc.stderr or proc.stdout or "")
    return proc.stdout


def run_json(script: str, *, timeout: float = DEFAULT_TIMEOUT) -> Any:
    """Run a JXA script whose last expression is ``JSON.stringify(...)`` and parse it."""
    out = run_jxa(script, timeout=timeout).strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise SafariError(f"osascript returned non-JSON output: {out[:400]!r}") from exc


# --------------------------------------------------------------------------------------
# Preambles
# --------------------------------------------------------------------------------------

# Shared JXA prelude: application handle, error capture, and the JSON envelope. Kept in
# one place so every snippet fails the same way and the caller can classify errors.
_PRELUDE = """
const Safari = Application('Safari');
Safari.includeStandardAdditions = true;
function emit(o) { return JSON.stringify(o); }
function windowsSafe() {
  try { return Safari.windows(); } catch (e) { return []; }
}
function tabAt(w, i) {
  const tabs = w.tabs();
  if (i === undefined || i === null) {
    return w.currentTab ? w.currentTab() : tabs[0];
  }
  if (i < 0 || i >= tabs.length) { throw new Error('tab index ' + i + ' out of range (' + tabs.length + ' tabs)'); }
  return tabs[i];
}
function windowAt(i) {
  const ws = windowsSafe();
  if (!ws.length) { throw new Error('Safari has no windows'); }
  if (i === undefined || i === null) { return ws[0]; }
  if (i < 0 || i >= ws.length) { throw new Error('window index ' + i + ' out of range (' + ws.length + ' windows)'); }
  return ws[i];
}
"""


def _wrap(body: str) -> str:
    """Wrap a snippet body in a try/catch that reports a structured error."""
    return (
        "(() => {\n"
        + _PRELUDE
        + "try {\n"
        + body
        + "\n} catch (e) {\n"
        + "  return emit({ok: false, error: String(e && e.message ? e.message : e),"
        + " code: (e && e.errorNumber) || null, kind: 'jxa'});\n"
        + "}\n})()"
    )


def _checked(result: Any) -> Any:
    """Unwrap the ``{ok: ...}`` envelope, raising the right SafariError subclass."""
    if isinstance(result, dict) and result.get("ok") is False:
        raise _classify(f"{result.get('error', '')} ({result.get('code')})"
                        if result.get("code") else str(result.get("error", "")))
    return result


# --------------------------------------------------------------------------------------
# Environment / capability probes
# --------------------------------------------------------------------------------------

def safari_installed() -> bool:
    return os.path.isdir("/Applications/Safari.app") or os.path.exists(
        "/System/Applications/Safari.app")


def safari_running() -> bool:
    """True when Safari has a running process. Uses ``pgrep`` so no consent is needed."""
    try:
        proc = subprocess.run(["pgrep", "-x", "Safari"], capture_output=True, text=True, timeout=5)
        return proc.returncode == 0 and bool(proc.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def activate_safari(*, timeout: float = 10.0) -> None:
    """Launch or foreground Safari without touching any window."""
    run_jxa("Application('Safari').activate(); 'ok'", timeout=timeout)


def js_available() -> tuple[bool, str]:
    """``(available, reason)`` for ``do JavaScript``. Cheap: one probe per call site."""
    try:
        result = run_json(_wrap(
            "const w = windowAt();\n"
            "const t = tabAt(w);\n"
            "if (!t) { return emit({ok: true, js: false, reason: 'no tab'}); }\n"
            "try { Safari.doJavaScript('1+1', {in: t}); return emit({ok: true, js: true}); }\n"
            "catch (e) { return emit({ok: true, js: false, reason: String(e.message || e)}); }"
        ), timeout=DEFAULT_TIMEOUT)
    except SafariError as exc:
        return False, str(exc)
    if isinstance(result, dict) and result.get("js"):
        return True, ""
    reason = (result or {}).get("reason", "") if isinstance(result, dict) else ""
    return False, reason or "do JavaScript is unavailable"


def permission_probe() -> dict[str, Any]:
    """Report TCC automation state for this interpreter without triggering a dialog.

    Reads the user's TCC database read-only (it is world-readable for the owning
    user) and looks for an ``AppleEvents`` row whose client is this executable and
    whose target is Safari. ``unknown`` means the row could not be read, not that
    permission is missing.
    """
    exe = os.path.realpath(sys.executable)
    info: dict[str, Any] = {"executable": exe, "state": "unknown", "detail": ""}
    db = os.path.expanduser("~/Library/Application Support/com.apple.TCC/TCC.db")
    if not os.path.exists(db):
        info["detail"] = "TCC.db not readable (Full Disk Access not granted to this binary)"
        return info
    try:
        import sqlite3
        uri = f"file:{db}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as conn:
            rows = conn.execute(
                "SELECT auth_value FROM access WHERE service='kTCCServiceAppleEvents' "
                "AND client=? AND indirect_object_identifier=?",
                (exe, SAFARI_BUNDLE),
            ).fetchall()
    except Exception as exc:  # health: allow BLE001 -- a probe never breaks the caller
        info["detail"] = f"TCC read failed: {exc}"
        return info
    if not rows:
        info["state"] = "not_granted"
        info["detail"] = "no AppleEvents row for this binary -> Safari; the first call will prompt"
        return info
    values = {row[0] for row in rows}
    if 2 in values:
        info["state"] = "granted"
    elif 0 in values:
        info["state"] = "denied"
        info["detail"] = "explicitly denied; re-enable in System Settings > Privacy & Security > Automation"
    else:
        info["state"] = "unknown"
        info["detail"] = f"auth_value={sorted(values)}"
    return info


def safaridriver_path() -> str:
    return shutil.which("safaridriver") or "/usr/bin/safaridriver"


def safaridriver_enabled() -> bool:
    """True when ``safaridriver --enable`` has been run (WebDriver is usable).

    ``safaridriver --enable`` needs admin rights, so this is opt-in and never
    attempted automatically. The check is a status probe, not a launch.
    """
    try:
        proc = subprocess.run([safaridriver_path(), "--diagnose"],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    text = (proc.stdout + proc.stderr).lower()
    return proc.returncode == 0 and "not enabled" not in text
