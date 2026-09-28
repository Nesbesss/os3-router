# Changelog

## 0.5.13 (2026-09-28)
- **The router no longer restarts a healthy rabbit-agent after OS3's housekeeping calls.** After OS3 saves facts, memory or file notes it never calls the router back, and the router used to read that silence as a dead connection. On a real Mac this caused most of the "connection likely dead" and "no follow-up" warnings, and each false alarm restarted the agent, which sometimes came back disconnected. Silence after those calls is now normal; a real task that goes quiet is still watched.
- **Updating Codex no longer fails again and again.** If updating the Codex CLI fails (a broken npm, no permission), the router now waits 6 hours before trying again instead of every 30 minutes, and runs the update from its own folder, which fixes the "uv_cwd ENOENT" failure seen after an installer replaced the folder it was started in.
- **A dropped connection during an update check isn't shown as a problem.** Wi-Fi hiccups and sleeping laptops filled "Problems" with "update check failed"; those now only appear in the activity log. Failed downloads and failed tests still show as problems.

## 0.5.12 (2026-09-28)
- **A router that can't start no longer restarts every 2 seconds for ever.** If the router keeps crashing right after it starts (for example another program took its port), it now waits longer between tries (up to a minute) and writes one problem entry per try for the first few, instead of filling the log and the activity list.
- **A slow start for one ChatGPT account no longer holds up the others.** With several accounts, starting Codex for one used to block requests for all of them until it finished.
- **A Codex that fails to start is no longer left running in the background.**
- **The "limit at 90%" warning names the account** when you have several ChatGPT accounts ("ChatGPT account 2 5-hour limit at 95%"), not just "ChatGPT".

## 0.5.11 (2026-09-28)
- **Security: a website can no longer read your router's key or change its settings.** A web page could make your browser talk to the router as if it were on your own computer (DNS rebinding) and read the API key from the dashboard. The router now only answers the dashboard to requests addressed to `localhost`, `127.0.0.1` or `::1`. Nothing changes for the app or for OS3. If you use the dashboard from another computer, that still works with your key as before. **Please update.**
- **The installer puts the latest release on your computer, not the newest unfinished code.** New installs (macOS, Windows, Linux) now get the same tested version updates give you.
- **Installing again over a running router can't leave you without one.** The installer keeps the previous version until the new one answers, and puts it back if the new one doesn't start. On Windows it also no longer tries to delete the folder the running router is using.
- **The installer checks the port by trying it.** Other programs, and ports Windows keeps for itself, no longer stop the router from starting: it takes the next free port.
- **After installing, the installer asks the running router what it sees**, not its own window: is Codex found and signed in, is Claude Code available, and (when you're watching) a real test request. If something is wrong it says what, instead of "installed".
- **Windows: a failed install no longer closes the window with the message in it,** any error (not only the ones the installer expected) gets the friendly explanation, and if Windows won't let you create the scheduled task (some company PCs), the router starts from your Windows startup list instead.
- **macOS: the installer prefers Homebrew's or the system's Python** over whatever `python3` your shell has (conda, pyenv), which the background service depends on for good.
- **Several ChatGPT accounts: the model list now shows every account's models.** With a Free account and a Plus account, the list used to show only the first account's models, so what Plus adds (like the bigger models) never appeared. It now shows all of them, marks the ones only some accounts have (for example "GPT-6-Sol (account 2 only)"), and sends a request to an account whose plan includes the model, instead of giving the Free account a model it refuses and switching to a smaller one. An account that hasn't handled a request yet is asked for its models once.
- **Failed installs write what a person helping needs** (versions, where Python, Codex and Claude were found, the router's own checks, the end of its log) to `install.log`. Nothing is sent anywhere.

## 0.5.10 (2026-09-28)
- **Claude Code signed out is now said plainly.** Before, a Claude Code that was signed out (or whose login had expired) gave a raw error, or the wrong message about Codex. The router now tells you to run `claude`, then `/login`, and a fallback model takes over meanwhile if you set one.
- **A Claude model that can't run no longer breaks a role.** If a role (or its fallback) uses a Claude model but Claude Code isn't installed or can't be found, the router uses your default ChatGPT model instead of failing every request.
- **A model Claude Code doesn't know or you can't use** (Claude Code too old, or not in your plan) now shows the "not in your plan, pick another model" message instead of a raw error.
- **Switching between ChatGPT and Claude in the middle of a task no longer wastes a call.** A task that moved to the other one (a fallback, or you changed the model) used to try to continue the other's conversation, fail, and retry; it now starts fresh straight away.
- **A second ChatGPT account on a smaller plan.** When the first account hit its limit and the next account's plan lacked the model (Free has only the small ones), the request failed; it now runs on a model that account has.
- **Faster requests.** The Claude lookup added in 0.5.9 could start a shell during a request; it no longer does, and an odd effort value in the settings no longer breaks a request.

## 0.5.9 (2026-09-28)
- **Claude models no longer go missing when Claude Code is installed in an unusual place.** If the router can't find Claude Code in its usual folders, it now asks your own shell where `claude` is. That covers installs through nvm, fnm, volta or asdf, and a `claude` that is only an alias.
- **You can point the router at Claude Code yourself.** When no Claude models are listed, **Models** now shows a box: run `which claude` in Terminal, paste the path, and the Claude models appear right away.
- **A new Codex install, or a second ChatGPT account, no longer hides the Claude models.** Until Codex had run once, the model list skipped Claude.

## 0.5.8 (2026-09-28)
- **Claude Sonnet 5.5.** Anthropic's new Sonnet (released today) is in the model list for every role, through your Claude subscription with Claude Code: faster than Sonnet 5 and lighter on your limits. Pick it under **Models**.

## 0.5.7 (2026-09-28)
- **Claude models show up again on Macs.** The router only looked for Claude Code in the service's own PATH, which doesn't include `~/.local/bin`, where Claude Code's installer puts it, so many people only saw GPT models under Models. It now also finds Claude Code there (and in its other usual folders), even when it was installed after the router.
- **The app no longer gets stuck on "localhost refused to connect" (Windows).** When switching to a new version failed on Windows, the router could end up with nothing listening while it looked like it was running. It now starts a fresh copy instead, and it checks itself: a router that stops answering is restarted within about a minute.

## 0.5.6 (2026-09-28)
- **Help explains a read-only Codex state database.** If Codex cannot write its state, Find the problem now points to the folder permissions to check instead of repeating the raw SQLite error.
- **Setup separates a saved connected status from a running node.** When the saved status says connected but its process cannot be verified, Setup explains the mismatch without offering a restart.

## 0.5.5 (2026-09-28)
- **Fix for "The request was rejected (HTTP 403)" in OS3's new *models and connections* screen.** rabbit's firewall blocks saving a connection whose endpoint is `http://localhost…` or `http://127.0.0.1…`. The router now tells you to use `http://127.1:11435/v1`: the same address, written in a way the firewall lets through. Already set up and working? Nothing to change. Getting the 403? Replace the endpoint with the new one and save again.

## 0.5.4 (2026-09-28)
- **The installer works on Linux machines without systemd or cron** (containers, WSL, minimal installs): the router starts right away and again at every login, instead of stopping with "could not write your crontab".
- **Never two routers at once.** A keep-alive can no longer start a second copy next to a running router (on cron machines one could be added every 2 minutes).

## 0.5.3 (2026-09-28)
- **Pick which account is used first.** Accounts has a *Use first* button: put your account with a 5-hour window first and keep another as the leftover. The router moves to the next account when the first is almost out, and goes back once it resets.
- **Signed-out accounts are handled.** When OpenAI ends a login (for example after a plan change), the account shows *Signed out* with a *Sign in again* button (main too), requests move to your other accounts, and you get a notification instead of errors.
- **The plan shown is the real one.** It now comes from OpenAI's live usage data, so a changed plan shows right away instead of days later.
- **A much friendlier installer.** No Node.js or Python needed anymore (it gets them itself), sign-in works without a browser (a link and a code), numbered steps with progress, a free port is picked if 11435 is taken, and if something fails it tells you which step and what to do. Everything is saved to `~/.codex-os3/install.log`.

## 0.5.2 (2026-09-28)
- **Codex Max plan:** only the windows your plan has are shown. Max has a weekly limit and no 5-hour one, so you now see one weekly ring instead of an empty 5-hour ring. Rings, warnings and `codex-os3 status` are labelled 5-hour / daily / weekly / monthly.
- **Codex Free plan:** when a model isn't included in your plan, the router runs the request on one that is (e.g. gpt-6-sol → gpt-6-luna) instead of replying "not included". It remembers this per account for a day, so an upgraded plan gets its models back by itself.

## 0.5.1 (2026-09-28)
- **Update checks keep working when GitHub's API rate limit is reached.** The router uses GitHub's public latest-release link to find the newest version, so automatic and manual update checks can continue without waiting for the API quota to reset. The fallback also works when Python cannot verify website certificates and must use the system's curl tool.

## 0.5.0 (2026-09-28)
The biggest update yet.
- **Up to 3× faster:** one Codex now stays running instead of starting for every message. Follow-up replies take
  about 2 seconds, browser steps are quicker too. (macOS and Linux; Windows keeps the old engine for now.
  Setting: `engine`, `auto` by default.)
- **Your limits last much longer:** your main chat keeps its conversation between messages instead of starting
  over, so almost everything is reused from the cache (92% instead of about 50% on real use). Browser tasks
  also read less per step.
- **Two ChatGPT accounts (or more):** add another account under *Accounts* in the app (you sign in with a link
  and a code). When one is almost out, the router moves on to the next by itself, and back when the first resets.
  Using several accounts may go against OpenAI's terms: the app asks you to accept that risk first.
- **Hangs are caught sooner:** a stalled model call is retried after a few minutes instead of blocking a task.
- **When OS3 rejects a browser result** as unconfirmed, the worker takes a fresh look and reports again instead of
  giving up.
- **Token counts on the dashboard are correct** for tasks with several steps (they were shown as running totals).
- **Mac: keep running with the lid closed** (Settings). macOS asks for your password once to switch it on or off.
- A new *What's new* screen for this release 🎉

## 0.4.15 (2026-09-28)
- **Check and retry router updates from Settings.** Find new updates checks GitHub right away. When a newer release is available, Install update now asks the router to test and install it, even if automatic updates are turned off or the last attempt failed. Settings shows whether the update is waiting, installing, failed, or running on the new version. Installer-made Mac, Windows, and Linux installs support this; a source checkout still updates with git.

## 0.4.14 (2026-09-29)
- **Keep OS3 running while the computer is idle.** Turn on Prevent idle sleep in the app's Settings, the Mac menu bar, or the Windows tray. It is off by default. The screen may still turn off; choosing Sleep or closing a laptop lid still pauses OS3.

## 0.4.13 (2026-09-29)
- **Restart confirmation works reliably.** Refreshing the Activity page no longer makes one Restart click open multiple confirmation dialogs.

## 0.4.12 (2026-09-29)
- **More reliable updates on Windows.** A router timeout test now allows extra time for Windows to close processes, so a successful update is less likely to be rejected as a test failure.

## 0.4.11 (2026-09-27)
- **Fix for 0.4.10:** workers could fail at the very end with "unhashable type: 'dict'" when they reported their
  result files. OS3 describes each file as an object (node, path, deliver to user); the router now reads and writes
  that format, and handing over generated images can never fail a task anymore.

## 0.4.10 (2026-09-27)
- **Generated images always reach you.** If the model makes an image but forgets to hand it to OS3, the router now
  hands it over itself. Editing images works too: send rabbit a photo and ask for a change.

## 0.4.9 (2026-09-27)
- **Image generation with your ChatGPT subscription.** When you ask rabbit for an image, the worker now creates it
  with Codex's own image generation (it counts toward your normal Codex limit) and hands the file to OS3, instead
  of OS3 asking you to connect a paid image provider ("Generate images" card). Turn it off with `codex_images: false`.
  With Claude as the model, OS3's own image tool is still used.

## 0.4.8 (2026-09-27)
- **Computer use and browser tasks work again with OS3's newest version.** OS3 now checks whether the model can
  read images (it asks for four colour bands) before a worker may look at screenshots. The small background model
  often misread that check, and OS3 then asked you to connect a paid image provider. Background calls that
  contain an image now run on your worker model (Standard), which passes the check.

## 0.4.7 (2026-09-27)
- **Automatic updates work again on Macs and PCs where they failed with `CERTIFICATE_VERIFY_FAILED`.** Some Python
  installs have no root certificates (the python.org Python on macOS until "Install Certificates" is run; on Windows,
  Python only sees certificates Windows already downloaded). The router now uses macOS's own certificates when
  Python has none, and downloads updates with the system's `curl` if Python's download still fails on a certificate.
- **Stuck on an older version?** Update once by hand, after that it updates itself again. Mac: double-click
  "Install Certificates.command" in Applications → Python 3.x, or rerun the install command. Windows: rerun
  `irm https://raw.githubusercontent.com/Nesbesss/os3-router/main/install.ps1 | iex` in PowerShell.

## 0.4.6 (2026-09-27)
- **Workers give up less on web pages:** when a page's text can't be read (Excel, Word or Google Docs online,
  apps that draw their content), workers now look at the screen and click through like a person, for
  example the right sheet tab of a spreadsheet, instead of endlessly reading page code
- If a worker says it "couldn't find" something after browsing, it first takes one more careful look
  (other tabs and sections, the newest screenshot, the page's own search)

## 0.4.5 (2026-09-27)
- **Background tasks in OS3:** long memory updates (like saving what OS3 learned about you) no longer get stopped halfway as "hung". The router now gives these background calls up to 5 minutes of quiet time while the answer is being written, instead of 90 seconds. Chats and normal tasks still get the fast check.

## 0.4.4 (2026-09-27)
- **Windows:** no more empty windows popping up about every 30 seconds while the OS3 Router app or dashboard is open. The router's Codex and Claude Code sign-in checks now run in the background without opening a window, so they no longer interrupt your typing.

## 0.4.3 (2026-09-27)
- **Windows tray icon:** starts 20 seconds after sign-in and retries if startup fails. Startup errors are saved to a log, and setup continues if the icon cannot start immediately.

## 0.4.2 (2026-09-27)
- **Windows installer:** signing in with ChatGPT no longer makes the installer stop at the Codex login check.

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
