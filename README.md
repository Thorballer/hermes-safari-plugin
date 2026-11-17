# hermes-safari

Drive the user's **real Safari** on macOS from Hermes.

Safari has no Chrome DevTools Protocol, so this plugin speaks AppleScript (JXA)
directly to Safari. That means it drives the browser you are actually logged
into — your session cookies are the login, and there is no separate automation
profile to authenticate.

## Why not a headless / CDP browser

A CDP browser is a *different* browser with its own empty profile. Anything
behind a login lands on a sign-in wall. This plugin drives the real thing, so
sites you are already signed into just work.

## Install

From the Hermes plugin catalog:

```bash
hermes plugins install safari
```

That clones the plugin, pins it to the catalogued commit, and installs it
**enabled**. Verify:

```bash
hermes plugins list | grep safari     # -> safari  enabled
hermes safari doctor                  # -> permission gates, with the fix for each
```

**No dependencies.** The plugin is pure Python stdlib and drives the `osascript`
and `defaults` binaries that ship with macOS — nothing to `pip install`, no
`requirements.txt`, no Homebrew package needed to get working.

The one optional extra is [`cliclick`](https://github.com/BlueM/cliclick), and
only for one tool:

```bash
brew install cliclick          # optional — real OS mouse events
```

Skip it and you still have 7 of 8 tools plus selector-mode `safari_click`. It
only adds `safari_click` with raw `x`/`y` viewport coordinates, and makes
selector clicks land as genuine OS events that sites rejecting synthetic JS
clicks will accept. Everything else — open, state, tabs, JS, page text, find,
type, screenshot — works without it.

<details>
<summary>Other install routes</summary>

Directly from the repo (bypasses the catalog pin):

```bash
hermes plugins install https://github.com/Thorballer/hermes-safari-plugin
```

Or drop the directory in by hand and enable it:

```bash
git clone https://github.com/Thorballer/hermes-safari-plugin ~/.hermes/plugins/safari
hermes plugins enable safari
```

</details>

## Setup: getting every capability turned on

The plugin works in layers. The cheapest gate gets you `safari_open`,
`safari_state` and `safari_tabs`; each further gate unlocks more tools. Do them
in this order — every step is a one-time click, and **no step requires a
package install** except the optional Step 3.

### Step 0 — confirm what you have

```bash
hermes safari status
```

Prints JSON: `safari_installed`, `safari_running`, `automation` (state + detail),
`cliclick`, `allow_js_from_apple_events`. Anything `false` is a gate you still
need to open.

### Step 1 — Automation consent (required for everything)

The first AppleEvent Hermes sends triggers a macOS dialog:

> "Python" wants access to control "Safari".

Click **Allow**. If it was dismissed once, macOS will not ask again — fix it by
hand: **System Settings → Privacy & Security → Automation →** your Python/Terminal
→ toggle **Safari** on.

Unlocks: `safari_open`, `safari_state`, `safari_tabs`.

Symptoms when missing: `-1712` AppleEvent timeouts, or `-1743` denials.

### Step 2 — JavaScript from Apple Events (DOM reads and writes)

In Safari: **Settings → Advanced →** check **"Show features for web developers"**.
Then in the menu bar: **Develop → Allow JavaScript from Apple Events**.

Unlocks: `safari_js`, `safari_act` (text / find / focused / back),
`safari_type`, and selector mode of `safari_click`.

This is the gate that matters most — it is what makes the plugin a real browser
driver instead of a tab opener. Note the setting is per-Safari-install and
survives restarts, but a Safari update or a prefs reset can clear it; re-check
with `hermes safari doctor` if JS calls start failing.

### Step 3 — cliclick (optional: real OS mouse events)

Only needed for coordinate clicks and for sites that reject synthetic JS clicks.
Skip it entirely if selector-mode clicking is enough.

```bash
brew install cliclick
```

Unlocks: coordinate mode of `safari_click` (`x`/`y`), and makes selector clicks
land as genuine OS events that sites rejecting synthetic JS clicks will accept.

Needs **Accessibility** permission for the binary that runs it — macOS prompts on
first click, or pre-grant at **System Settings → Privacy & Security →
Accessibility**.

### Step 4 — Screen Recording (screenshots only)

**System Settings → Privacy & Security → Screen Recording →** enable your
Python/Terminal. macOS requires a restart of that process after granting.

Unlocks: `safari_screenshot`.

This is the fallback read path when Step 2 is unavailable: screenshot the window
and hand the PNG to a vision tool.

### Step 5 — verify the whole stack

```bash
hermes safari doctor
```

```
Gates:
  Automation -> Safari : granted  (...)
  Develop > Allow JS    : True
  cliclick present      : True
```

Three greens and every tool in the table below is live. Anything red prints the
exact fix on the line below it.

### Optional — turn the toolset on for a platform

If you talk to Hermes over Telegram/Discord/webhook rather than the CLI, add
`safari` to that platform's toolset list in `config.yaml`:

```yaml
platform_toolsets:
  telegram:
    - safari
```

## Tools

| Tool | What it does | Gate |
|---|---|---|
| `safari_open` | Open a URL in a new tab (or window), optionally kept permanently | Automation |
| `safari_state` | List windows and tabs with titles, URLs, current tab | Automation |
| `safari_tabs` | List / select / close / navigate tabs | Automation |
| `safari_js` | Evaluate JavaScript in a tab and return the value | JS from Apple Events |
| `safari_act` | Read page text, find elements by CSS selector, focused element, go back | JS from Apple Events |
| `safari_type` | Type into a field via the native value setter (React/Vue-safe), optional submit | JS from Apple Events |
| `safari_click` | Click by CSS selector, or by viewport coordinates as a real OS mouse event | JS (selector) / cliclick (coords) |
| `safari_screenshot` | Capture the front window to a PNG | Screen Recording |

## Demo: a full round trip

Once Steps 1–2 are green, this is the whole loop — open, wait for render, read,
find, fill, submit. Each call is a tool invocation; the JSON is what comes back.

**1. Open a page.**

```json
{"tool": "safari_open", "args": {"url": "https://news.ycombinator.com"}}
```
```json
{"success": true, "window": 0, "tab": 3, "url": "https://news.ycombinator.com"}
```

**2. Read it.** `safari_act` with `action: "text"` returns the rendered body text,
already trimmed to 20k chars.

```json
{"tool": "safari_act", "args": {"action": "text", "limit": 4000}}
```

**3. Ask the page a question instead of dumping it.** Anything `safari_js` can
evaluate, it returns — this is how you avoid pulling a whole DOM into context:

```json
{"tool": "safari_js", "args": {"code": "return [...document.querySelectorAll('.titleline > a')].slice(0,5).map(a => a.textContent)"}}
```
```json
{"success": true, "value": ["Show HN: ...", "The ...", "..."]}
```

**4. Find elements before clicking them.** `action: "find"` returns matches with
their text, so you never click blind:

```json
{"tool": "safari_act", "args": {"action": "find", "selector": "a.morelink", "limit": 3}}
```

**5. Click.** Selector mode scrolls the element into view and clicks its centre:

```json
{"tool": "safari_click", "args": {"selector": "a.morelink"}}
```

**6. Fill a form and submit.** `safari_type` uses the native value setter, so
React/Vue inputs actually register the change:

```json
{"tool": "safari_type", "args": {"selector": "input[name=q]", "text": "hermes agent", "submit": true}}
```

**7. Confirm where you ended up.**

```json
{"tool": "safari_state"}
```
```json
{"success": true, "windows": [{"index": 0, "current": 3, "tabs": [{"index": 3, "title": "hermes agent | Search", "url": "https://..."}]}]}
```

**8. SPA that hasn't rendered yet?** Poll instead of sleeping once — a single read
after a fixed delay gives wrong answers:

```json
{"tool": "safari_js", "args": {"code": "return document.body.innerText.length > 500 ? document.body.innerText.slice(0,2000) : null"}}
```

Call it again until it returns a value rather than `null`.

**9. Clean up.** Tabs opened by `safari_open` are closed after an hour idle, or
immediately at session end — pass `keep: true` for anything you want to survive.
To close now:

```json
{"tool": "safari_tabs", "args": {"action": "close", "index": 3}}
```

## Notes

- `safari_click` in coordinate mode uses a real OS-level mouse event
  (`cliclick`), so it works on sites that reject synthetic JS clicks.
- Tabs opened by `safari_open` are closed automatically after an hour idle
  unless you pass `keep=true`. The sweep also runs on session end and
  opportunistically before each open, so a closed chat never leaves tabs behind.
- If a page is a client-rendered SPA, the first read is often an empty shell.
  Poll `safari_js` returning `document.body.innerText` until content appears;
  a single read after a fixed sleep gives wrong answers.
- Every tool returns a uniform envelope: `{"success": true, ...}` or
  `{"success": false, "error": "...", "gate": "...", "fix": "..."}`. Read `gate`
  and `fix` — they name the exact checkbox that stopped you instead of making
  you guess.
- `safari_state`, `safari_open` and `safari_tabs` need only Automation consent,
  so a half-configured install still does something useful.

## License

MIT
