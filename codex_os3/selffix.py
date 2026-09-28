"""Self fix: repairs the router can do on its own, and the help desk.

update_codex() is deterministic (npm / Homebrew) and also runs automatically when Codex is
too old. diagnose() is the help desk: the user describes the problem, the router adds redacted
diagnostics, a small model (gpt-6-luna; Claude Code as fallback when Codex itself is broken)
explains the cause and may propose actions, but only from ACTIONS below, and each one only
runs when the user approves it. It never runs arbitrary commands."""
import json, os, platform, shutil, subprocess, sys, threading, time

from . import __version__, codex_runner, config, onboarding, os3, platform_util, roles, store, ui_api
from .export import redact

_lock = threading.Lock()


# -- deterministic fixes --------------------------------------------------------

def update_codex(cfg):
    """-> (ok, message). Updates the Codex CLI the way it was installed."""
    b = platform_util.native_bin(cfg.get("codex_bin") or shutil.which("codex") or "")
    real = os.path.realpath(b) if b else ""
    env = dict(os.environ)
    if b:
        env["PATH"] = os.path.dirname(b) + os.pathsep + env.get("PATH", "")  # npm next to node (nvm)
    if "/Cellar/" in real or "/Caskroom/" in real:
        cmd = [shutil.which("brew") or "/opt/homebrew/bin/brew", "upgrade", "codex"]
    else:
        npm = None
        for d in (os.path.dirname(b) if b else "", ""):
            cand = os.path.join(d, "npm.cmd" if platform_util.WINDOWS else "npm") if d else shutil.which("npm")
            if cand and os.path.isfile(cand):
                npm = cand
                break
        if not npm:
            return False, "npm not found: install Node.js, then run: npm i -g @openai/codex@latest"
        cmd = [npm, "install", "-g", "@openai/codex@latest"]
    before = ui_api.codex_info(cfg, fresh=True)["version"]
    try:
        # from the router's own folder: an installer swapping the app folder can delete the one this process
        # started in, and npm then dies with "uv_cwd ENOENT" (seen on a real Mac, every 30 minutes for days)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900, env=env, cwd=config.HOME)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{' '.join(cmd[1:])} failed: {e}"
    codex_runner._known.clear()  # its feature flags may have changed
    after = ui_api.codex_info(cfg, fresh=True)["version"]
    if r.returncode:
        return False, f"{' '.join(cmd[1:])} failed: {(r.stderr or r.stdout)[-300:]}"
    store.kv_set("codex_outdated", None)
    return True, f"Codex updated {before} → {after}"


def _set_role_model(cfg, role="", model="", effort=""):
    r = dict(cfg.get("roles") or {})
    r[role] = {"model": model, "effort": effort}
    clean = ui_api.clean_roles(r)
    if role not in clean:
        return False, f"unknown role or model: {role} / {model}"
    config.save({"roles": clean, "role_routing": True})
    return True, f"{role} now uses {clean[role]['model']} ({clean[role]['effort']})"


ACTIONS = {
    "update_codex": ("Update the Codex CLI", lambda cfg: update_codex(cfg)),
    "restart_agent": ("Restart the rabbit-agent (OS3 node)", lambda cfg: os3.restart_agent()),
    "reload_router": ("Restart the router (OS3 keeps working)",
                      lambda cfg: (ui_api.request_reload(), (True, "router restart requested"))[1]),
    "update_router": ("Install the newest os3-router now",
                      lambda cfg: (store.kv_set("update_checked", 0), (True, "update check requested"))[1]),
    "set_role_model": ("Change the model of a role", _set_role_model),
}


def run_action(cfg, name, args=None):
    if name not in ACTIONS:
        return False, "unknown action"
    try:
        ok, msg = ACTIONS[name][1](cfg, **(args or {}))
    except TypeError:
        return False, "bad arguments"
    store.event("selffix_action", f"{name}: {msg}", source="selffix", level="info" if ok else "warn")
    return ok, msg


# -- test my setup -----------------------------------------------------------------

def _fn(name, desc, props=None, req=()):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props or {}, "required": list(req)}}}


def _plain(err):
    e = str(err).lower()
    if ("readonly database" in e or "sqlite error (code 8)" in e) and ("state_db" in e or "codex_rollout" in e):
        return ("Codex cannot write its state database under ~/.codex. Check that this folder and its database "
                "are writable by your Mac account, then retry Find the problem.")
    if "newer version" in e:
        return "Codex is too old for this model. Click Fix under “Check this machine” (or Get help)."
    if "not logged in" in e or "login" in e or "401" in e or "unauthorized" in e:
        return "Not logged in. Log in to Codex (`codex login`) or Claude Code (`claude`, then /login) in a terminal."
    if "not supported" in e or "does not exist" in e or "model" in e and "not" in e:
        return "Your plan doesn't offer this model. Pick another one in Settings → Models."
    return str(err)[:300]


def selftest(cfg):
    """OS3's own connection test (ping, then ping again) and a worker tool call, through the
    real pipeline, marked source=selftest so they don't count as OS3 traffic."""
    from . import engine
    ping = [_fn("ping", "Connection test. Call it when asked.")]
    shell = [_fn("shell", "Run a shell command on a device.", {"command": {"type": "string"}}, ("command",))]
    tests = [
        ("OS3's connection test", "chat", [{"role": "user", "content": "Call the ping function now."}], ping, "ping"),
        ("OS3's connection test, 2nd call", "chat", [
            {"role": "user", "content": "Call the ping function now."},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function",
                                                                   "function": {"name": "ping", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "Done."},
            {"role": "user", "content": "Now call the ping function"}], ping, "ping"),
        ("A worker using a tool", "worker", [
            {"role": "system", "content": "You are a worker agent. Use the tools to do the task."},
            {"role": "user", "content": "Print the text hello on this device with the shell tool."}], shell, "shell"),
    ]
    out = []
    for name, role, msgs, tools, want in tests:
        t = time.time()
        turn = None
        try:
            turn = engine.Turn(cfg, {"model": cfg["model"], "messages": msgs, "tools": tools}, lambda: True, source="selftest")
            msg, _ = turn.run()
            got = [c["function"]["name"] for c in msg.get("tool_calls") or []]
            ok = want in got
            detail = (f"called {want}" if ok else (msg.get("content") or "no tool call")[:300])
        except Exception as e:
            ok, detail = False, _plain(e)
        out.append({"test": name, "ok": ok, "detail": detail, "model": turn.model if turn else "",
                    "secs": round(time.time() - t)})
        if not ok:
            break  # the next tests would fail the same way
    good = all(r["ok"] for r in out)
    store.event("selftest", ("passed: " if good else "FAILED: ") + "; ".join(f"{r['test']} ({r['model']}): {r['detail']}" for r in out)[:900],
                source="selffix", level="info" if good else "warn")
    return {"ok": good, "results": out}


# -- help desk -------------------------------------------------------------------

GUIDE = """os3-router lets rabbit OS3 use the user's Codex (ChatGPT) or Claude Code subscription.
How it works: OS3 cloud -> rabbit-agent (the OS3 node on this machine) -> os3-router on http://localhost:<port>/v1
-> codex exec / claude -p. The router must run on the SAME machine that is picked as "device" in OS3's connection.
Correct OS3 setup: Settings -> api keys -> provider "Local model" -> device = this machine -> endpoint
http://localhost:<port>/v1 (the ollama preset fills 11434, which is WRONG) -> model id e.g. gpt-6-luna (only a label
when "pick a model per role" is on) -> api key = the router's key -> save connection. An existing local connection
can't be edited: "delete connection", then add it again.
Common problems: wrong port (11434 instead of 11435); wrong device (router not on that device); old connection / old
api key (router sees wrong-key calls); Codex CLI too old ("requires a newer version"); Codex or Claude Code not logged
in; usage limit reached (wait for reset or switch a role to the other subscription); rabbit-agent disconnected or its
LLM tunnel stuck (restart it); on macOS the router was installed over SSH (must be run in Terminal on the Mac itself);
Windows: WinError 193 was fixed in 0.2.5 (update)."""

SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["summary", "cause", "steps", "actions"],
          "properties": {
              "summary": {"type": "string"},
              "cause": {"type": "string"},
              "steps": {"type": "array", "items": {"type": "string"}},
              "actions": {"type": "array", "items": {
                  "type": "object", "additionalProperties": False, "required": ["id", "args_json", "why"],
                  "properties": {"id": {"type": "string", "enum": list(ACTIONS)},
                                 "args_json": {"type": "string"}, "why": {"type": "string"}}}}}}


def context(cfg):
    """What the router knows, without message contents or secrets."""
    reqs = store.q("SELECT ts, role, model, status, result, error FROM requests ORDER BY id DESC LIMIT 15")
    ev = store.q("SELECT ts, source, kind, level, msg FROM events WHERE level IN ('warn','error') ORDER BY ts DESC LIMIT 30")
    now = time.time()
    return redact(json.dumps({
        "router_version": __version__, "os": f"{platform.system()} {platform.release()}", "python": sys.version.split()[0],
        "setup_steps": onboarding.status(cfg)["steps"],
        "checks": ui_api.doctor(cfg),
        "settings": {k: cfg.get(k) for k in ("port", "bind", "model", "effort", "role_routing", "roles", "auto_update")},
        "models_available": [m["slug"] for m in roles.available_models()],
        "wrong_key_calls": store.kv_get("auth_fail"),
        "usage_limit": store.kv_get("usage_limit"),
        "limits": store.latest_limits(),
        "recent_requests": [dict(r, ago_s=round(now - r.pop("ts"))) for r in reqs],
        "recent_problems": [dict(e, ago_s=round(now - e.pop("ts"))) for e in ev],
        "rabbit_agent_log": [f"{e.get('component')} {e.get('level')} {e.get('message', '')[:160]}"
                             for e in os3.log_tail(since_ts=now - 3600)[-25:]],
    }, default=str))


def diagnose(cfg, problem, step=""):
    if not _lock.acquire(blocking=False):
        return {"error": "a diagnosis is already running"}
    try:
        prompt = (f"You are the help desk of os3-router. Reply in the language the user wrote in.\n\n{GUIDE}\n\n"
                  f"The user is at setup step: {step or 'unknown'}\nThe user says: {problem[:2000]}\n\n"
                  f"Router diagnostics (JSON):\n{context(cfg)}\n\n"
                  "Find the most likely cause from the diagnostics. summary: one or two plain sentences. cause: the "
                  "evidence. steps: short, concrete instructions for the user (name the exact OS3 screen and field). "
                  "actions: only if one of the allowed actions would fix it; args_json is \"{}\" except for "
                  "set_role_model: {\"role\": \"chat|worker|background\", \"model\": \"<slug>\", \"effort\": \"low|medium|high\"}. "
                  "Never suggest actions that aren't needed.")
        t, used = time.time(), None
        for model, runner in (("gpt-6-luna-medium", codex_runner), ("claude-haiku-4-5-low", None)):
            if runner is None:
                if not roles.claude_installed(cfg):
                    break
                from . import claude_runner as runner
            try:
                text, usage, _, _ = runner.run(cfg, prompt, model, SCHEMA)
                out = json.loads(text[text.find("{"):text.rfind("}") + 1])
                used = model
                break
            except Exception as e:  # e.g. Codex itself is the broken part: try Claude Code
                err = f"{type(e).__name__}: {e}"[:300]
        if not used:
            return {"error": f"couldn't ask a model ({_plain(err)}). The setup steps above show what the router detected."}
        out["actions"] = [dict(a, label=ACTIONS[a["id"]][0]) for a in out.get("actions", []) if a.get("id") in ACTIONS]
        out["model"] = used
        store.event("selffix", f"{problem[:100]} -> {out.get('summary', '')[:150]} ({used}, {time.time() - t:.0f}s)",
                    source="selffix")
        return out
    finally:
        _lock.release()
