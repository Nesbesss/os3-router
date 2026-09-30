"""The setup wizard's state: which step is done, which is next, and in plain words what is
wrong. Everything is detected from what the router can see itself (CLIs, the rabbit-agent,
requests and wrong-key attempts from OS3)."""
import socket, time

from . import config, os3, roles, store, ui_api


def _ago(ts):
    s = time.time() - ts
    return f"{int(s)} s ago" if s < 90 else f"{int(s / 60)} min ago" if s < 5400 else f"{int(s / 3600)} h ago"


ROLE_KEYS = ("chat", "worker", "background")
# what "Claude" means for each role: Sonnet is the balanced one, Haiku the light one for the often-run background calls
CLAUDE_ROLES = {"chat": {"model": "claude-sonnet-5-5", "effort": "medium"},
                "worker": {"model": "claude-sonnet-5-5", "effort": "medium"},
                "background": {"model": "claude-haiku-4-5", "effort": "low"}}


def backends(cfg, fallbacks=True):
    """Which subscriptions the routing really uses: the model of every role (or the one model when routing is
    off) and, unless told not to, every fallback."""
    if cfg.get("role_routing", True):
        r = cfg.get("roles") or {}
        models = [(r.get(k) or {}).get("model") or cfg["model"] for k in ROLE_KEYS]
    else:
        models = [cfg["model"]]
    if fallbacks:
        models += [f.get("model") for f in (cfg.get("fallback") or {}).values() if (f or {}).get("model")]
    return {roles.backend(m) for m in models}


def choice(cfg):
    """"codex", "claude" or "both": what the routing uses now."""
    b = backends(cfg)
    return next(iter(b)) if len(b) == 1 else "both"


def apply_choice(cfg, which):
    """-> the config changes for the wizard's choice. Never throws away what someone set on purpose: a role that
    already uses the chosen subscription keeps its model. A fallback to the other subscription goes when you chose
    one (it would pull the other one in); with "both" every role gets one, unless it already has one that switches subscription."""
    if which not in ("codex", "claude", "both"):
        raise ValueError("choose codex, claude or both")
    cur, fb = cfg.get("roles") or {}, dict(cfg.get("fallback") or {})
    codex_default, out, changed = config.DEFAULTS["roles"], {}, set()
    for k in ROLE_KEYS:
        r = cur.get(k) or {"model": cfg["model"], "effort": cfg["effort"]}
        if which == "claude" and roles.backend(r["model"]) != "claude":
            r, _ = CLAUDE_ROLES[k], changed.add(k)
        elif which == "codex" and roles.backend(r["model"]) != "codex":
            r, _ = codex_default[k], changed.add(k)
        out[k] = dict(r)
    if which == "both":
        for k in ROLE_KEYS:
            m = (fb.get(k) or {}).get("model")
            if not m or roles.backend(m) == roles.backend(out[k]["model"]):  # (the same subscription can't take over from itself)
                fb[k] = dict(CLAUDE_ROLES[k] if roles.backend(out[k]["model"]) == "codex" else codex_default[k])
    else:  # one subscription: no fallback to the other, and none for a role that just moved (it was the other's fallback)
        fb = {a: f for a, f in fb.items() if a not in changed and roles.backend((f or {}).get("model")) == which}
    return {"role_routing": True, "roles": out, "fallback": fb}


def engines(cfg):
    """What the wizard's choice screen shows: is each one on this computer, and signed in."""
    c, k = ui_api.codex_info(cfg), ui_api.claude_info(cfg)
    return {"codex": {"installed": bool(c["path"]), "logged_in": bool(c["logged_in"])},
            "claude": {"installed": bool(k["path"]), "logged_in": bool(k["logged_in"])}, "choice": choice(cfg)}


def engine_step(cfg):
    need = backends(cfg)
    step = {"id": "engine", "title": "Your AI subscription is ready"}
    uses_claude = "claude" in need
    if uses_claude:
        k = ui_api.claude_info(cfg)
        if not k["path"]:
            return dict(step, state="error", detail="You chose Claude, but Claude Code isn't installed on this "
                        "machine. Install it (code.claude.com), then open it once and log in. (The copy inside the "
                        "Claude desktop app counts too.) Or go back and choose ChatGPT.")
        if not k["logged_in"]:
            return dict(step, state="error", detail="Claude Code isn't logged in. Open a terminal, run `claude`, then "
                        "type /login and follow the steps.")
    if "codex" not in need:
        return dict(step, state="ok", detail="Claude Code, logged in")
    c = ui_api.codex_info(cfg)
    if c.get("error"):
        return dict(step, state="error", detail=c["error"], action="update_codex")
    if not c["path"]:
        return dict(step, state="error", detail="The Codex CLI isn't installed. Run the installer again, or in a "
                    "terminal: npm i -g @openai/codex", action="update_codex")
    if ui_api._ver(c["version"]) < ui_api._ver(ui_api.MIN_CODEX) or store.kv_get("codex_outdated"):
        return dict(step, state="error", detail=f"Your Codex CLI ({c['version']}) is too old for the current models. "
                    "Click Fix to update it.", action="update_codex")
    if not c["logged_in"]:
        return dict(step, state="error", detail="Codex isn't logged in to your ChatGPT account. Open a terminal and "
                    "run: codex login")
    return dict(step, state="ok", detail=f"Codex {c['version']}, logged in" + (" · Claude Code logged in" if uses_claude else ""))


def node_step():
    step = {"id": "node", "title": "OS3 node on this machine"}
    if not os3.installed():
        return dict(step, state="error", detail="This machine isn't an OS3 device yet. In OS3, add this computer as a "
                    "device first (rabbit's node installer). The router must run on the device you pick in OS3.")
    a = os3.status() or {}
    if a.get("status") == "connected" and not a.get("running"):
        return dict(step, state="error", detail="The saved agent status says connected, but its recorded "
                    "process could not be verified. Check the OS3 node status on this machine.")
    if not (a.get("status") == "connected" and a.get("running")):
        return dict(step, state="error", detail=f"The rabbit-agent is {a.get('status') or 'not running'}. Click Fix "
                    "to restart it.", action="restart_agent")
    return dict(step, state="ok", detail=f"rabbit-agent connected (v{a.get('version')})")


def connection_step(cfg):
    step = {"id": "connection", "title": "Connect OS3 to the router"}
    last = store.q("SELECT MAX(ts) t FROM requests WHERE source != 'selftest'")[0]["t"]
    fail = store.kv_get("auth_fail")
    key_end = (cfg.get("api_key") or "")[-4:]
    if fail and (not last or fail["ts"] > last) and time.time() - fail["ts"] < 86400:
        got = fail.get("key_end")
        why = ("without an API key" if got == "none" else f"with an old API key (ending in …{got})" if got not in (None, "?")
               else "with a wrong API key")
        return dict(step, state="error", problem="wrong_key",
                    detail=f"OS3 reached the router {_ago(fail['ts'])}, but {why}; the router's key ends in …{key_end}. "
                           "In OS3, delete the connection and add it again with the key below.")
    if last:
        return dict(step, state="ok", detail=f"OS3 is using the router (last request {_ago(last)})")
    return dict(step, state="waiting", detail="Waiting for OS3… Follow the steps below, then press “save connection” in OS3.")


def models_step(cfg):
    names = {m["slug"]: m["name"] for m in roles.available_models()}
    if cfg.get("role_routing", True):
        r = cfg.get("roles") or {}
        used = " · ".join(f"{lbl}: {names.get((r.get(k) or {}).get('model'), (r.get(k) or {}).get('model') or cfg['model'])}"
                          for k, lbl in (("chat", "Small"), ("worker", "Standard"), ("background", "Background")))
    else:
        used = f"{names.get(cfg['model'], cfg['model'])} for everything"
    return {"id": "models", "title": "Models", "state": "ok", "detail": used}


def status(cfg):
    steps = [engine_step(cfg), node_step(), connection_step(cfg), models_step(cfg)]
    current = next((s["id"] for s in steps if s["state"] != "ok"), None)
    return {"steps": steps, "current": current, "done": current is None,
            "key": cfg.get("api_key"), "port": cfg["port"], "model": cfg["model"],
            "engines": engines(cfg), "host": socket.gethostname(), "share_reports": cfg.get("share_reports"), "problem": next((s.get("problem") for s in steps if s.get("problem")), None)}
