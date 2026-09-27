<p align="center"><img src="assets/icon-256.png" width="128" alt="os3-router"></p>

# os3-router

Use your **Codex / ChatGPT subscription** and/or your **Claude Code** login as the LLM for **rabbit OS3**:
chat, tool calling, workers, browser and computer use, and **image generation and editing**, all on the
subscription you already pay for. Mix them per role, e.g. `gpt-6-luna` for the main chat and `claude-sonnet-5`
for the workers. It includes the OS3 Router app, token and limit tracking, a watchdog that repairs a stuck
rabbit-agent, and automatic updates.

**Highlights**
- **Your subscription, not an API bill:** Codex (ChatGPT Plus/Pro) and Claude Code logins, per role
- **Images included:** rabbit makes and edits pictures with Codex's own image generation, no paid image provider
- **Browser and computer use that doesn't give up:** workers look at the screen when a page can't be read
  (Excel Online, Google Docs, canvas apps) and check again before saying something isn't there
- **Keeps working:** watchdog for the rabbit-agent tunnel, fallback model at the usage limit, automatic updates
  that test themselves before switching over

```
OS3 cloud ──▶ rabbit-agent (your machine) ──▶ os3-router (localhost:11435) ──▶ codex exec / claude -p ──▶ your subscription
```

> ⚠️ **Read first.** This drives the official Codex CLI and/or the official Claude Code CLI, unmodified, with
> **your own** login. Check whether OpenAI's and Anthropic's terms allow using your subscription this way;
> that decision is yours (see [Claude Code](#claude-code) for what we found). Heavy agent use also burns
> through your 5-hour and weekly limits quickly (the dashboard shows both).

> The project was called **codex-os3** before 0.2.0; old links redirect, and existing installs keep working.

## Install

On the machine that runs your OS3 node (rabbit-agent), in a terminal **on that machine** (not over SSH):

**macOS / Linux**
```sh
curl -fsSL https://raw.githubusercontent.com/Nesbesss/os3-router/main/install.sh | bash
```

**Windows (beta: no real OS3 test yet)**
```powershell
irm https://raw.githubusercontent.com/Nesbesss/os3-router/main/install.ps1 | iex
```

The installer:
1. checks Python 3.9+, installs the Codex CLI if needed, and runs `codex login`; if Claude Code is installed, it is detected too
2. checks that the rabbit-agent (OS3 node) is on this machine
3. installs the router as a service (launchd / systemd / Task Scheduler) that starts at login and restarts on crashes
4. installs the **OS3 Router app** (macOS) or tray icon (Windows)
5. opens the setup page and **waits until OS3 connects**

Then open the dashboard (`http://localhost:11435`): the **setup wizard** walks you through OS3 step by step
with screenshots, checks each step live, and has a **Test my setup** button. In short, in OS3 go to
**Settings → API keys**, provider **local** (Local model), and enter:

| field | value |
|---|---|
| device | **this machine** (the one you installed on) |
| endpoint | `http://localhost:11435/v1` |
| model id | `gpt-6-luna` (see [Models](#models)) |
| api key | shown by the installer and on the setup page |
| context window (advanced) | `200000` |

**Pick models per role.** OS3's local mode only lets you set one model, but the router knows who is
asking, so you can choose a model for each role in the dashboard (Settings → Models), like OS3's picker
for cloud providers:

| role | used for | default |
|---|---|---|
| **Small** | the main chat you talk to | `gpt-6-luna` · medium |
| **Standard** | workers that carry out tasks (shell, files, computer use) | `gpt-6-sol` · medium |
| **Background** | memory, fact extraction and reply review (frequent, light) | `gpt-6-luna` · low |

The Codex list comes from your Codex account, so new models show up by themselves; Claude models appear
when Claude Code is installed. The dashboard shows token use per role.

**Several nodes?** Install the router on **one** machine, ideally the one that is always on, and
pick it as the LLM device. Tasks still run on every node. No Tailscale, ngrok, or open ports are needed,
because rabbit relays model calls through the rabbit-agent of the device you picked.

**Other OS3 machines** (nodes that don't run the router) can get a small keep-alive that reconnects their
rabbit-agent after sleep or a lost connection:
```sh
curl -fsSL https://raw.githubusercontent.com/Nesbesss/os3-router/main/install.sh | bash -s -- --node-only
```

**Fallback at the usage limit.** In Settings → Models, give a role a fallback model (e.g. Standard:
`gpt-6-sol`, fallback `claude-sonnet-5`). When a subscription hits its limit, the same request is retried on
the fallback, OS3 keeps working, and the router tries the main model again every 15 minutes.

**Get help (Self fix).** Describe the problem in the dashboard; the router adds its own diagnostics (checks,
recent errors, the rabbit-agent log; never your messages or keys), asks `gpt-6-luna` for the cause and
offers fixes from a fixed, safe list (update Codex, restart the agent, …) that only run when you click them.

## What it does for OS3

Codex is an agent CLI, not a chat API, so the router does a lot of translation:

- **Tool calling** via a strict output schema, with **several calls per turn** (OS3's computer-use
  skill requires "act → wait → screenshot → look" in one turn)
- **Screenshots** go to the model as real images; the newest 2 are attached
- **One Codex session per task** (`codex exec resume`): each turn sends only new events instead of re-reading
  the whole conversation, and the model keeps its own reasoning
- **Call repair and validation** before OS3 sees a call: broken JSON escapes, device names or garbled device ids
  instead of ids, dlam actions sent as script names, missing `feed_image` after a screenshot, and every
  argument checked against OS3's tool schemas and `act.py`'s own parser. Invalid calls go back to the model once
  to be fixed.
- **Self-checks:** a false "tool not available" gets one retry, "done" after computer use gets one
  verify pass, and a screenshot loop gets a nudge. These extra steps can never make a request fail.
- **Browser workers that look:** when a web page's text comes back empty (Excel/Word Online, Google Docs, apps
  that draw their content), workers switch to screenshots and click through like a person; a "couldn't find it"
  after browsing gets one more careful look first
- **Hang handling:** no Codex activity for 90 s means the call is killed and retried once
- **Usage limit** shows up in OS3 as a clear message with the reset time, not "something went wrong"
- **Zero-downtime updates:** upgrades swap the worker process while running requests finish
- **Automatic updates:** every 30 min it checks for a new release, runs that release's tests on your machine,
  and only then switches over (keeps the previous version; off in Settings; see [Updates](#updates))

## Images: generate and edit with your subscription

OS3's newest version asks for a paid image provider when a task needs a picture (the *"Generate images"*
card). With the router you don't need one: when rabbit is asked to draw, generate or edit an image, the
worker uses **Codex's built-in image generation** and hands the file to OS3, which shows it to you.

- **Generate:** "make an image of a red apple"
- **Edit (image to image):** send rabbit a photo and ask for a change ("make the apple blue")
- It counts toward your normal Codex limit (about 1% of a Plus 5-hour window per image in our tests) and takes
  roughly 30–60 seconds
- If the model makes the image but forgets to hand it over, the router hands it over itself
- Switch it off with `codex_images: false`; with a Claude model as worker, OS3's own image tool is used

OS3 also checks whether the model can **see** images before a worker may use screenshots (it asks for the
colours of four bands in a test picture, the *"Understand images"* card if it fails). The router answers that
check with your worker model, so browser and computer use work without an extra provider.

## Updates

The router checks for a new release every 30 minutes, runs that release's own tests on your machine, and only
then switches over, without dropping running requests. The previous version is kept.

**Stuck on an old version** (updates failing with `CERTIFICATE_VERIFY_FAILED`)? Some Python installs have no
root certificates; since 0.4.7 the router works around that, but a router older than 0.4.7 needs **one** manual
update, after which it updates itself again:
- **macOS:** double-click *Install Certificates.command* in Applications → Python 3.x, or rerun the install command
- **Windows:** rerun `irm https://raw.githubusercontent.com/Nesbesss/os3-router/main/install.ps1 | iex` in PowerShell

Rerunning the install command always updates to the latest release and keeps your settings and API key.

## The OS3 Router app (macOS)

A small app with your status, limits, models (with an effort slider), the setup wizard, help and problem
reports. It lives in the menu bar and opens a window from Launchpad/Finder. The installer adds it; you can also
download **OS3Router.dmg** from the [latest release](https://github.com/Nesbesss/os3-router/releases/latest) and drag
it to Applications. It's the router's own page (`http://localhost:11435/app`) in a native window, so it updates
together with the router.

**First open:** the app isn't signed with a paid Apple developer account, so macOS says *"OS3 Router" Not Opened*.
Click **Done**, open **System Settings → Privacy & Security**, scroll down and click **Open Anyway** (then your
password). You only do this once; router updates don't replace the app.

## Dashboard, menu bar app, watchdog

`http://localhost:11435/` (local only; from elsewhere it needs the API key):

- **Dashboard:** 5-hour and weekly limits with reset countdowns, tokens per hour, recent requests
- **Setup:** OS3 values with copy buttons, key rotation, checks, and a "connected" indicator
- **Watchdog:** events and actions, plus a manual rabbit-agent restart
- **Tasks & export:** one row per OS3 task and a **log export** (zip with a report, the timeline and
  router fixes, with secrets redacted), meant for bug reports
- **Settings:** model, effort, watchdog, captures, alert webhook, optional Jev key

**Watchdog.** The rabbit-agent's LLM tunnel can die silently: it still says "connected" and still runs
commands, but OS3's model requests never arrive, and every task fails with *"Local LLM device can't be
reached"*. The watchdog detects this from hard evidence (our reply asked OS3 to run tools, the agent ran them
or aborted the task, and no follow-up request came) and restarts the agent **through its own scheduler**,
so on macOS it keeps its Accessibility and Screen Recording permissions. It acts at most once per stalled reply
and ignores normal waits (`ask_user`, `wait`, workers). It also warns about usage limits and unstable Codex
connections, and can post alerts to a webhook (e.g. `https://ntfy.sh/<topic>`).

**Jev (optional).** With a [TypeSafe](https://typesafe.ai) key, the watchdog also asks Jev for a second
opinion on the ambiguous case ("is this silence normal?"). Jev receives only timings, tool names and agent log
lines, never your messages or screenshots. Rules stay in charge: Jev can only escalate when it is ≥ 90% sure
*and* the hard signals agree.

## Models

The dashboard's model selector lists exactly what **your** Codex account offers (read from Codex's own
model list), so it may differ per account and changes when OpenAI adds models. Typical options:

| model | Codex's description | good for |
|---|---|---|
| `gpt-6-luna` | Fast and affordable model for easier tasks | **Small** (main chat) and **Background**; clean tool calls |
| `gpt-6-sol` | Workhorse model for coding and everyday work | **Standard** (workers): multi-step tasks, computer use |
| `gpt-6-astra` | Frontier intelligence for the most demanding work | hard worker tasks; uses the most of your limit |
| `gpt-5.6-luna` / `gpt-5.6-sol` / `gpt-5.5` | older generations | fallback |
| `claude-sonnet-5` | Claude Code · balanced | **Standard** (workers) |
| `claude-opus-5-5` / `claude-fable-5-1` | Claude Code · most capable | hard worker tasks; uses the most of your limit |
| `claude-haiku-4-5` | Claude Code · fastest | **Background** |

Each model offers its own **effort** levels (from `low` up to `max` or `ultra`); higher is slower and uses
more of your 5-hour and weekly limits. Suggested starting point: Small = `gpt-6-luna` medium,
Standard = `gpt-6-sol` medium, Background = `gpt-6-luna` low.

What we measured with computer use (small sample, your mileage may vary): `gpt-6-luna` occasionally
**misreads digits in screenshots** (226295 → 26295), while `gpt-5.6-luna` read them correctly. If a
worker task depends on exact numbers from the screen, double-check them or use a stronger worker model.

In OS3 itself, the model id you enter (e.g. `gpt-6-luna`) only matters when per-role models are switched
off in the dashboard; then that one model is used for everything. Append an effort to it if you like,
e.g. `gpt-6-sol-high`.

The logo is an original mark (a terminal prompt whose cursor branches into two routes); os3-router is not
affiliated with or endorsed by OpenAI, Anthropic or rabbit.

## Claude Code

Pick a Claude model for any role in Settings → Models. The router then runs the official `claude` CLI
(`claude -p`), unmodified, the way it runs `codex exec`: Claude Code's own tools, MCP servers, hooks and
settings are switched off for these calls, OS3's tools are passed as a schema, screenshots go in as images,
and each task keeps one Claude Code session. The dashboard shows your Claude 5-hour and weekly limits next to
Codex's.

Setup: install Claude Code on the same machine and sign in **in Claude Code itself** (`claude`, then
`/login`). The router never asks for, reads or stores your login; it only starts the `claude` binary.

On the terms: Anthropic's [Claude Code legal page](https://code.claude.com/docs/en/legal-and-compliance)
allows an end user to sign in to the unmodified Claude Code binary with their own subscription, including
where another product runs Claude Code, and Anthropic's support assistant said this setup is allowed
(an AI chatbot, not a binding ruling). It also says
subscription limits assume ordinary, individual use and that Anthropic may enforce its restrictions without
notice. OS3 workers doing a lot of computer use are heavy use: keep an eye on the limits and decide for yourself.
The router does not load your `~/.claude/settings.json`; it uses the login you made in Claude Code. Only an
`ANTHROPIC_API_KEY` in the service's environment would switch it to per-token API billing.

## Troubleshooting

| you see | cause and fix |
|---|---|
| OS3: *"The device is offline or the local endpoint is unreachable"* when saving | the router must run on the **device you selected** in OS3, and the endpoint must be `http://localhost:11435/v1`. Check `os3-router doctor` on that machine. |
| OS3: *"This model did not make a tool call"* when saving | make sure the **API key** field holds the router's key and `os3-router doctor` is all ✓ (an outdated Codex CLI is the usual cause), then save again; the check is a live model call, so an occasional retry is normal |
| OS3: *"Local LLM device can't be reached"* during tasks | the rabbit-agent's tunnel died; the watchdog restarts the agent automatically within ~2 min, or use *Restart rabbit-agent* in the dashboard / menu bar |
| *"Codex / Claude usage limit reached — resets at …"* | your plan's 5-hour or weekly limit; the app shows both per subscription. Use lighter models/effort per role, set a fallback model, or move a role to the other subscription |
| OS3 shows an *"Understand images"* card asking for a provider | update to **0.4.8+**: the router then passes OS3's image check itself |
| OS3 shows a *"Generate images"* card asking for a provider | update to **0.4.9+**: images are made with your Codex subscription |
| updates fail with `CERTIFICATE_VERIFY_FAILED` | Python without root certificates; update once by hand (see [Updates](#updates)), 0.4.7+ handles it |
| a worker fails at the end with *"unhashable type: 'dict'"* | a bug in 0.4.10, fixed in **0.4.11** (updates itself) |
| installer says *run this in Terminal on the Mac itself* | macOS services started over SSH lose their permissions; run it locally |
| dashboard shows *Codex CLI version* ✗ | `npm i -g @openai/codex@latest` (older CLIs reject the current models) |
| something else | open an issue with `os3-router doctor` output and a log export (dashboard → Tasks & export) |

`os3-router doctor`: `cd ~/.codex-os3/app && python3 -m codex_os3 doctor` (the internal names kept the old name)

## Privacy and security

- **Problem reports are opt-in.** The wizard asks once. If you say yes and a reporting endpoint is configured,
  errors and what Self fix or the router fixed can be sent to the developer's Discord channel: version, OS,
  a random install id and the event, with keys and tokens removed. Never message contents, names or hostnames.
  Switch it off in Settings anytime.


- The router listens on `127.0.0.1` only by default, and `/v1` always needs the API key
- It stores **metadata** (timings, token counts, tool names), not message contents. "Captures"
  (full requests, including screenshots and anything you typed) are **off** by default.
- History is kept for 7 days (configurable)
- Exports redact keys, tokens and password-like strings
- It never starts services from SSH on macOS (processes started that way lose their permissions)

## Commands

```sh
cd ~/.codex-os3/app && python3 -m codex_os3 <command>
  status | doctor | setup-info | key [--rotate] | reload | update | export <task>
```
Uninstall: `bash install.sh --uninstall [--purge]` · Windows: `install.ps1 -Uninstall [-Purge]`

## Maintainers: reports → GitHub issues

`tools/reports_to_issues.py` reads the reports channel with a Discord bot and turns problems into GitHub issues
(label `report`, duplicates become "+1" comments). Run it on your own machine, e.g. from an OS3 scheduled task;
the bot token stays there (`~/.os3-reports/discord_token`), never in this repo. New client code sends
opt-in reports to `https://os3-router-report-intake.vercel.app/api/report`. The separate
[`report-intake`](report-intake/README.md) Vercel Function validates and rate-limits submissions, then forwards
them to the same Discord reports channel. Its webhook is a sensitive Vercel Production environment variable,
never bundled with the client. `CODEX_OS3_REPORT_ENDPOINT` can override the public endpoint for testing;
setting it to an empty string disables reporting. This client change is staged on a feature branch and has
not been released to existing installs. Previously published webhook copies remain valid until separately revoked.

## Development

```sh
python3 -m unittest discover -s tests                     # offline tests (no Codex calls)
CODEX_OS3_HOME=/tmp/x CODEX_OS3_PORT=11499 python3 -m codex_os3 serve &
CODEX_OS3_HOME=/tmp/x CODEX_OS3_PORT=11499 python3 tests/live_smoke.py --reload   # uses real Codex quota
app/macos/build.sh                                        # menu bar app (Xcode 15+)
```
`bench/` runs real computer-use tasks on a Mac through the router (see `bench/README.md`). It is
expensive in Codex quota; never run it in CI.

## Platform status

| | install / service / upgrade / uninstall | with a real rabbit-agent + OS3 |
|---|---|---|
| **macOS** | ✓ launchd, tested on real Macs | ✓ in daily use (Codex); Claude Code tested live |
| **Linux** | ✓ systemd user service and cron fallback (CI + Docker) | not yet |
| **Windows** | ✓ Task Scheduler (CI on Windows Server) | not yet (beta) |

CI runs the full test suite on macOS, Linux and Windows with Python 3.9 and 3.12, against a fake Codex
CLI (no quota), plus end-to-end installer runs on Linux and Windows.

What changed in each version: [CHANGELOG.md](CHANGELOG.md) (the app also shows *What's new* after an update).

MIT license.
