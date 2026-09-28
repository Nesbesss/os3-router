# Security

## What os3-router can reach
- **Your Codex/ChatGPT subscription.** Anyone who can call the router can spend your quota. The `/v1` API
  always requires the API key; by default the router only listens on `127.0.0.1`.
- **Your screen and files, indirectly.** OS3 decides what runs on your devices; the router only turns
  model output into tool calls. It repairs call arguments, but never invents new actions.
- **The rabbit-agent.** The watchdog may restart it (setting `restart_agent`), always through its own scheduler.

## Data on disk (`~/.codex-os3`)
- `config.json` (mode 0600): API key, optional Jev key and webhook URL
- `state.db`: request metadata (timings, sizes, token counts, tool names), limits, events; no message
  contents. Kept `retention_days` (default 7)
- `captures/` only when captures are enabled: full requests including screenshots and anything typed
- Codex session files in `~/.codex/sessions` for running tasks, deleted 3 h after a task goes idle

## Dashboard
- Allowed from the machine itself, and only when the request names this machine (`Host` is `localhost`, `127.x`
  or `::1`): a web page whose own name was re-pointed at 127.0.0.1 (DNS rebinding) also arrives from the machine
  itself, but with its own name, and is refused. Remotely only after `/login?key=<api key>` (HttpOnly,
  SameSite=Strict cookie) or with `Authorization: Bearer`. State-changing API calls also need an `X-Codex-OS3`
  header, which ordinary cross-site pages can't send.
- Exports are redacted (API keys, bearer tokens, password-like fields, images), but review them before sharing.

## Reporting
Please report vulnerabilities privately via GitHub security advisories on this repository.
