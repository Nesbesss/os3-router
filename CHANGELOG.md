# Changelog

## 0.4.1 (2026-09-26)
- **The OS3 Router app on Windows and Linux:** its own window (no address bar), with the same screens as on Mac.
  Windows: Start menu → OS3 Router, or double-click the tray icon. Linux: OS3 Router in your app menu

## 0.4.0 (2026-09-26)
- **The OS3 Router app:** a calm, simple app with your status, your limits as rings, the models per role with
  an effort slider and a fallback, the setup wizard, help and problem reports. On macOS it opens as its own
  window (from Launchpad or the menu bar icon); anywhere else at `http://localhost:11435/app`
- **Mac:** the app now also comes as a DMG. The first time, macOS asks you to approve it once:
  System Settings → Privacy & Security → Open Anyway. Updates of the router don't ask again
- Smoother look: pages fade in, rings fill up, light and dark mode follow your system

## 0.3.4 (2026-09-26)
- Windows: Codex requests now start without opening a flashing Terminal window.

## 0.3.3 (2026-09-26)
- If you opt in to problem reports, they now go through a protected public endpoint instead of a Discord webhook in the app. You can change the endpoint or turn reporting off.
- The previously exposed webhook still needs to be revoked separately.

## 0.3.2 (2026-09-26)
- Problem reports are now off until the developer configures a webhook, so the app no longer ships with an exposed webhook.

## 0.3.1 (2026-09-25)
- **Report a problem:** a button in the dashboard (and under Get help) to tell the developer what went wrong,
  with the router's diagnostics if you like. Anonymous: no OS3 messages, names or keys

## 0.3.0 (2026-09-25)
- **New setup wizard:** step by step, with screenshots of every OS3 screen, Copy buttons, live checks and a
  clear message when something is wrong (e.g. an old connection or the wrong port). Opens by itself until
  you're connected
- **Test my setup:** one click sends OS3's own connection test and a worker tool call through the router, so
  you know it works before you touch OS3
- **Get help (Self fix):** describe the problem; the router looks at its own diagnostics, explains the
  cause and offers fixes you approve with one click
- **Fallback at the usage limit:** pick a fallback model per role (e.g. Claude Sonnet when Codex is out);
  OS3 keeps working and switches back after the reset
- **Codex updates itself** when it's too old for the current models
- **Heads-up at 90%:** a notification when a subscription's 5-hour or weekly limit passes 90%
- **Keep-alive for your other OS3 machines:** `install.sh --node-only` keeps their rabbit-agent connected
  (e.g. after sleep). The router itself now also restarts an agent that stays disconnected
- **Redesigned dashboard:** one card per subscription, the models in use, readable activity; Watchdog and
  Tasks are now one Activity tab
- Optional, anonymous problem reports to the developer (you're asked once; no messages, names or keys)

## 0.2.6 (2026-09-25)
- **This screen:** after every update you see what's new, once, in the dashboard, the menu bar app (Mac)
  or the tray icon (Windows). Continue hides it everywhere
- **Everything updates itself:** the router checks for a new version every 30 minutes, tests it on your
  machine first and switches over without interrupting OS3; the menu bar app and tray icon now come along too

## 0.2.5 (2026-09-25)
- **Windows:** fixed the "[WinError 193] %1 is not a valid Win32 application" error; no more editing
  config.json by hand. Thanks to the tester who reported it

## 0.2.4 (2026-09-25)
- Checks for updates every 30 minutes (was every 6 hours)

## 0.2.3 (2026-09-25)
- **OS3's reasoning sliders now count:** the effort you set in OS3 (Small / Standard) is the one used.
  Background keeps the effort from the dashboard, because OS3 has no slider for it
- Fewer "tool not available" hiccups: when OS3 requires a specific tool (like saving a memory), the model
  is now told so
- The Setup page shows which models are actually used; the model id you entered in OS3 is only a label,
  keep it as is
- No more false "Codex CLI version ✗" on the Setup page

## 0.2.2 (2026-09-25)
- **Automatic updates:** checks GitHub every 6 h; a new release is installed only after its own test suite
  passes on your machine, the previous version is kept in `app.prev`, and the switch is zero-downtime.
  Off in Settings; `update` checks right away
- Linux: a reload no longer resets a connection that was waiting in the old worker's queue

## 0.2.1 (2026-09-25)
- Watchdog no longer restarts the rabbit-agent while OS3 is still reaching the router (a request running,
  cancelled or failed after our reply counts as a sign of life); seen in a tester's log
- Hang detection gives high/xhigh efforts more time to think (180 s / 300 s instead of 90 s) instead of
  killing a run that is still working and starting over

## 0.2.0 (2026-09-25)
- Renamed to **os3-router** (was codex-os3); internal names, paths and services are unchanged, so upgrades
  need nothing
- **Claude Code backend:** any role can use a Claude model (`claude-sonnet-5`, `claude-opus-5-5`, …) through
  the official `claude` CLI and your own Claude Code login; mix it with Codex per role
- Dashboard shows the 5-hour and weekly limits per subscription; `doctor` checks Claude Code when a role uses it
- Token card follows the chart's range (it was empty right after midnight)
- Tested live on a Mac with Claude Pro: OS3's connection test, main chat, workers, background calls and computer
  use with Sonnet 5 and Opus 5.5, and mixed Codex + Claude per role
- Clear message in OS3 when a model isn't part of your plan (e.g. Fable 5.1 on Claude Pro)

## 0.1.1 (2026-09-24)
- Per-role models: **Small** (main chat), **Standard** (workers), **Background** (memory/review), chosen in the
  dashboard from the models your Codex account offers; token use per role
- Only passes `--disable` flags the installed Codex knows; the installer updates a too-old Codex CLI
- Fixed OS3's connection test failing now and then (the model thought OS3's tools were unavailable)
- Watchdog runs inside the worker, so upgrades update it
- Windows installer fixes found by CI (Python detection, console encoding, tray app port)
- App icon

## 0.1.0 (2026-09-24)
First public version, grown from a live prototype used with rabbit OS3.

- OpenAI-compatible router over `codex exec`: chat, streaming, tool calling with several calls per turn
- Computer use: screenshots passed as images (newest 2), one persistent Codex session per OS3 task
- Tool-call repair and validation (JSON escapes, node ids, dlam scripts, OS3 schemas, `act.py` arguments)
- Self-checks: false "unavailable" answers, unverified "done" after computer use, screenshot loops
- Hang detection (90 s without activity), readable usage-limit replies with the reset time
- Supervisor with zero-downtime reloads; the worker exits if the supervisor is killed
- Watchdog: detects a silently dead rabbit-agent LLM tunnel and restarts the agent through its own
  scheduler; optional TypeSafe Jev second opinion; webhook alerts
- Local dashboard: limits, tokens, requests, setup with copy buttons, watchdog events, per-task log export
  (redacted), settings; remote access via `/login?key=`
- Installers: macOS (launchd), Linux (systemd user service or cron), Windows (Task Scheduler, beta)
- macOS menu bar app, Windows tray icon (beta)
