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

## Tools

| Tool | What it does |
|---|---|
| `safari_open` | Open a URL in a new tab (or window), optionally kept permanently |
| `safari_state` | List windows and tabs with titles, URLs, current tab |
| `safari_js` | Evaluate JavaScript in a tab and return the value |
| `safari_act` | Read page text, find elements by CSS selector, go back |
| `safari_click` | Click by CSS selector or viewport coordinates (real OS mouse event) |
| `safari_type` | Type into the focused element |
| `safari_tabs` | List / select / close / navigate tabs |
| `safari_screenshot` | Capture the front window to a PNG |

## Install

```bash
hermes plugins install https://github.com/Thorballer/hermes-safari-plugin
```

Or drop the directory into `~/.hermes/plugins/safari/` and enable it:

```bash
hermes plugins enable safari
```

## macOS permissions

Three gates, each with its own failure mode. Run `hermes safari` to have the
plugin report which ones are missing and the exact fix for each.

1. **Automation** — System Settings → Privacy & Security → Automation → allow
   your Python/Terminal to control **Safari**. Missing this produces
   `-1712` AppleEvent timeouts or `-1743` denials.
2. **JavaScript from Apple Events** — Safari → Develop → *Allow JavaScript from
   Apple Events*. Required for `safari_js` and `safari_act`. Enable the Develop
   menu first in Safari → Settings → Advanced.
3. **Screen Recording** — only for `safari_screenshot`.

## Notes

- `safari_click` uses a real OS-level mouse event (`cliclick`), so it works on
  sites that reject synthetic JS clicks.
- Tabs opened by `safari_open` are closed automatically after an hour idle
  unless you pass `keep=true`.
- If a page is a client-rendered SPA, the first read is often an empty shell.
  Poll `safari_js` returning `document.body.innerText` until content appears;
  a single read after a fixed sleep gives wrong answers.

## License

MIT
