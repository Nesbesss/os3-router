"""JSON API behind the web UI. handle() -> (status, body, content_type)."""
import json, os, re, subprocess, sys, time

from . import __version__, accounts, config, export, os3, roles, sleep_control, store
from . import platform_util
from .platform_util import pid_alive

J = "application/json"
EDITABLE = {"model", "effort", "bind", "port", "captures", "retention_days", "jev_key",
            "webhook", "watchdog", "restart_agent", "auto_update", "no_sleep", "fallback", "share_reports",
            "claude_bin", "engine", "stream_chat", "compact_tokens", "codex_images", "max_codex", "max_images", "role_routing", "roles"}


def clean_roles(value):
    """Only known roles with a model slug and an effort that model supports."""
    known = {m["slug"]: m for m in roles.available_models()}
    out = {}
    for role in roles.ROLES:
        r = (value or {}).get(role) or {}
        model = str(r.get("model", ""))
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", model):
            continue
        efforts = known.get(model, {}).get("efforts") or ["low", "medium", "high", "xhigh", "max", "ultra"]
        effort = r.get("effort") if r.get("effort") in efforts else known.get(model, {}).get("default_effort", "medium")
        out[role] = {"model": model, "effort": effort}
    return out


MIN_CODEX = "0.155.0"  # older CLIs reject the current models ("requires a newer version of Codex")


def _ver(v):
    try:
        return tuple(int(x) for x in str(v).split("-")[0].split(".")[:3])
    except ValueError:
        return (999,)  # dev/fake builds: don't block


_cache = {}


def cached(fn):
    """CLI checks spawn processes; the setup page polls, so reuse a result for 20 s."""
    def wrap(cfg=None, fresh=False):
        k = (fn.__name__, (cfg or {}).get("codex_bin"), (cfg or {}).get("claude_bin"))
        hit = _cache.get(k)
        if not fresh and hit and time.time() - hit[0] < 20:
            return hit[1]
        v = fn(cfg)
        _cache[k] = (time.time(), v)
        return v
    return wrap


@cached
def codex_info(cfg=None):
    b = platform_util.codex_path(cfg)  # same discovery as both runners
    info = {"path": b, "version": None, "logged_in": None}
    if not b:
        info["error"] = platform_util.CODEX_MISSING
        return info
    try:
        # polled by the open app/dashboard: without no_window_kwargs each check flashes a console window on Windows
        # cwd: a folder that always exists. Started from one that was deleted (an installer replaced the app folder under a
        # running worker) Codex says "Error loading configuration: No such file or directory" and looks signed out
        v = subprocess.run([b, "--version"], capture_output=True, text=True, timeout=15, cwd=config.HOME, **platform_util.no_window_kwargs())
        info["version"] = (v.stdout or v.stderr).strip().split()[-1] if (v.stdout or v.stderr) else None
        s = subprocess.run([b, "login", "status"], capture_output=True, text=True, timeout=15, cwd=config.HOME,
                           **platform_util.no_window_kwargs())
        out = (s.stdout + s.stderr).lower()
        info["logged_in"] = s.returncode == 0 and "not logged in" not in out
        info["login_detail"] = (s.stdout + s.stderr).strip()[:200]
    except (OSError, subprocess.SubprocessError) as e:
        info["error"] = f"Codex could not start: {e}"
    return info


@cached
def claude_info(cfg):
    b = platform_util.native_bin(roles.claude_path(cfg))
    info = {"path": b, "logged_in": None, "detail": ""}
    if not b:
        return info
    try:
        s = subprocess.run([b, "auth", "status"], capture_output=True, text=True, timeout=20, cwd=config.HOME,
                           **platform_util.no_window_kwargs())
        d = json.loads(s.stdout or "{}")
        info["logged_in"] = bool(d.get("loggedIn"))
        info["detail"] = d.get("authMethod") or ""
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return info


def uses_claude(cfg):
    models = [r.get("model") for r in (cfg.get("roles") or {}).values()] if cfg.get("role_routing", True) else []
    return any(roles.backend(m) == "claude" for m in models + [cfg["model"]])


def doctor(cfg):
    c = codex_info(cfg)
    a = os3.status()
    first = store.q("SELECT MIN(ts) t, MAX(ts) l, COUNT(*) n FROM requests")[0]
    extra = []
    if uses_claude(cfg):
        k = claude_info(cfg)
        extra = [{"check": "Claude Code installed", "ok": bool(k["path"]),
                  "detail": k["path"] or "see https://code.claude.com (a role uses a Claude model)"},
                 {"check": "Claude Code logged in (your own account)", "ok": bool(k["logged_in"]),
                  "detail": k["detail"] or "run: claude, then /login"}]
    return extra + [
        {"check": "Codex CLI installed", "ok": bool(c["path"]), "detail": c.get("error") or c["path"] or "npm i -g @openai/codex"},
        {"check": f"Codex CLI version ≥ {MIN_CODEX}", "ok": _ver(c["version"]) >= _ver(MIN_CODEX),
         "detail": (c["version"] or "?") + ("" if _ver(c["version"]) >= _ver(MIN_CODEX)
                                             else " — update: npm i -g @openai/codex@latest")},
        {"check": "Codex logged in (ChatGPT subscription)", "ok": bool(c["logged_in"]),
         "detail": c.get("login_detail") or "run: codex login"},
        {"check": "rabbit-agent installed on this machine", "ok": os3.installed(),
         "detail": "install the OS3 node on this machine first" if not os3.installed() else "~/.rabbit-agent"},
        {"check": "rabbit-agent connected", "ok": a.get("status") == "connected" and a.get("running"),
         "detail": f"{a.get('status')} pid {a.get('pid')} v{a.get('version')}" if a else "no status file"},
        {"check": "OS3 has called this router", "ok": bool(first["n"]),
         "detail": f"last request {time.strftime('%H:%M:%S', time.localtime(first['l']))}" if first["n"]
         else "not yet — save the connection in OS3 (Settings → API keys)"},
    ]


def _prev_version():
    """The version before the last automatic update (kept in app.prev), if any."""
    from . import updater
    try:
        with open(os.path.join(updater.APP + ".prev", "codex_os3", "__init__.py")) as f:
            return re.search(r'__version__ = "([^"]+)"', f.read()).group(1)
    except (OSError, AttributeError):
        return None


def whatsnew():
    """Changelog sections the user hasn't seen yet: after an update, the web UI, the menu bar
    app and the tray show them once (whichever is open first; Continue marks them seen)."""
    from . import updater
    seen = store.kv_get("whatsnew_seen") or _prev_version()
    sections = []
    try:
        with open(os.path.join(updater.APP, "CHANGELOG.md"), encoding="utf-8") as f:
            parts = re.split(r"^## ", f.read(), flags=re.M)[1:]
    except OSError:
        parts = []
    for p in parts:
        head, _, body = p.partition("\n")
        v = head.split()[0]
        if updater.ver(v) > updater.ver(__version__) or (seen and updater.ver(v) <= updater.ver(seen)):
            continue
        sections.append({"version": v, "title": head.strip(), "body": body.strip()})
        if not seen:  # fresh install: just the current version
            break
    return {"version": __version__, "show": seen != __version__ and bool(sections), "sections": sections[:6]}


def usage(hours):
    since = time.time() - hours * 3600
    bucket = 3600 if hours <= 48 else 86400
    rows = store.q(
        f"SELECT CAST(ts/{bucket} AS INT)*{bucket} AS t, COUNT(*) n, SUM(COALESCE(in_tok,0)) i, "
        "SUM(COALESCE(cached_tok,0)) c, SUM(COALESCE(out_tok,0)) o, SUM(tools>0) agent "
        "FROM requests WHERE ts>? GROUP BY t ORDER BY t", (since,))
    tot = store.q("SELECT COUNT(*) n, COALESCE(SUM(in_tok),0) i, COALESCE(SUM(cached_tok),0) c, "
                  "COALESCE(SUM(out_tok),0) o FROM requests WHERE ts>?", (since,))[0]
    by_role = store.q("SELECT COALESCE(role,'?') role, COUNT(*) n, SUM(COALESCE(in_tok,0)) i, "
                      "SUM(COALESCE(out_tok,0)) o FROM requests WHERE ts>? GROUP BY role ORDER BY i DESC", (since,))
    return {"bucket": bucket, "series": rows, "total": tot, "by_role": by_role}


def speed(hours=24):
    """How fast the chat is for this router: the last `hours` against the 7 days before. Chat is what a person waits
    for. Everything comes from what the router recorded itself (nothing here is an estimate)."""
    import statistics
    now = time.time()
    lo = now - hours * 3600

    def stats(a, b):
        rs = store.q(
            "SELECT r.ts, r.done_ts, r.first_ts, r.mode, r.in_tok, r.cached_tok, "
            "(SELECT COUNT(*) FROM requests p WHERE p.task = r.task AND p.ts < r.ts) AS earlier "
            "FROM requests r WHERE r.role='chat' AND r.status='ok' AND r.source != 'selftest' AND r.done_ts IS NOT NULL "
            "AND r.ts >= ? AND r.ts < ?", (a, b))
        med = lambda xs: round(statistics.median(xs), 1) if xs else None
        follow = [r for r in rs if r["earlier"]]
        cont = sum(1 for r in follow if (r["mode"] or "").startswith("resume"))
        tin = sum(r["in_tok"] or 0 for r in rs)
        return {"n": len(rs), "median_s": med([r["done_ts"] - r["ts"] for r in rs]),
                "first_s": med([r["first_ts"] - r["ts"] for r in rs if r["first_ts"]]), "n_first": sum(1 for r in rs if r["first_ts"]),
                "follow_n": len(follow), "continued_pct": round(100 * cont / len(follow)) if follow else None,
                "cached_pct": round(100 * sum(r["cached_tok"] or 0 for r in rs) / tin) if tin else None}
    return {"now": stats(lo, now + 1), "before": stats(lo - 7 * 86400, lo), "hours": hours}


def codex_latest(cfg):
    """{installed, latest, newer}: is there a newer Codex than the one the router runs? Newer models (GPT-6.1 Sol on launch
    day) can need a newer Codex than the one that is installed: the account is refused with a plan message that hides
    the real reason. Asked at most every 6 hours; a failed lookup says nothing."""
    from . import updater
    installed = codex_info(cfg)["version"]
    c = store.kv_get("codex_latest") or {}
    if time.time() - c.get("ts", 0) > 6 * 3600:
        try:
            c = {"ts": time.time(), "latest": json.loads(updater._get("https://registry.npmjs.org/@openai/codex/latest", 15))["version"]}
        except Exception:
            c = dict(c, ts=time.time() - 5 * 3600)  # (try again in an hour)
        store.kv_set("codex_latest", c)
    latest = c.get("latest")
    newer = bool(installed and latest and re.fullmatch(r"\d+\.\d+\.\d+", latest) and _ver(installed) < _ver(latest))
    return {"installed": installed, "latest": latest, "newer": newer}


def handle(method, path, data, q, cfg):
    if method == "GET" and path == "status":
        lims = store.latest_limits()
        wd = store.kv_get("watchdog_last") or {}
        running = store.q("SELECT COUNT(*) n FROM requests WHERE status='running' AND ts > ?", (time.time() - 900,))[0]["n"]
        return 200, {"version": __version__, "time": time.time(), "limits": lims.get("codex") or next(iter(lims.values()), None),
                     "limits_all": lims, "latest_release": store.kv_get("update_latest"),
                     "whats_new": whatsnew()["show"],
                     "fallback_active": [b for b in ("codex", "claude") if (accounts.pick() is None if b == "codex" else
                                                                           (store.kv_get("limited:" + b) or 0) > time.time())],
                     "alerts": store.kv_get("alerts") or [],
                     "usage_limit": store.kv_get("usage_limit"), "agent": os3.status(),
                     "watchdog": wd, "running": running, "model": cfg["model"],
                     "no_sleep": bool(cfg["no_sleep"]), "sleep_supported": sys.platform in sleep_control.SUPPORTED,
                     # 127.1 = 127.0.0.1: rabbit's firewall blocks saving a connection whose body contains
                     # http://localhost or http://127.0.0.1 (HTTP 403 in OS3's models and connections)
                     "endpoint": f"http://127.1:{cfg['port']}/v1"}, J
    if path == "updates":
        from . import updater
        if method == "GET":
            return 200, updater.update_status(cfg), J
        if method == "POST":
            try:
                return 200, updater.check_now(cfg), J
            except ValueError as e:
                return 409, {"error": str(e)}, J
            except Exception as e:
                return 502, {"error": f"Could not check for updates: {type(e).__name__}: {e}"[:300]}, J
    if method == "POST" and path == "updates/install":
        from . import updater
        try:
            return 202, updater.request_update(cfg), J
        except ValueError as e:
            return 409, {"error": str(e)}, J
    if method == "GET" and path == "usage":
        return 200, usage(float(q.get("hours", 24))), J
    if method == "GET" and path == "speed":
        return 200, speed(float(q.get("hours", 24))), J
    if method == "GET" and path == "requests":
        rows = store.q("SELECT id, ts, done_ts, task, source, model, role, stream, tools, msgs, bytes, imgs, mode, status, "
                       "error, result, calls, in_tok, cached_tok, out_tok FROM requests ORDER BY ts DESC LIMIT ?",
                       (int(q.get("limit", 100)),))
        return 200, rows, J
    if method == "GET" and path == "events":
        rows = store.q("SELECT * FROM events WHERE (? = '' OR source = ?) ORDER BY ts DESC LIMIT ?",
                       (q.get("source", ""), q.get("source", ""), int(q.get("limit", 200))))
        return 200, rows, J
    if method == "GET" and path == "tasks":
        return 200, export.tasks(int(q.get("limit", 50))), J
    if method == "GET" and path == "export":
        task = q.get("task", "")
        if not re.fullmatch(r"[0-9a-f]{8,40}", task):
            return 400, {"error": "bad task id"}, J
        return 200, export.build(task, cfg), "application/zip"
    if path.startswith("accounts"):  # several Codex accounts
        if method == "GET" and path == "accounts":
            return 200, accounts.overview(cfg), J
        if method == "GET" and path == "accounts/login":
            return 200, accounts.login_status(q.get("login", "")), J
        try:
            if method == "POST" and path == "accounts/add":
                return 200, accounts.start_login(cfg, bool(data.get("accept_terms"))), J
            if method == "POST" and path == "accounts/signin":  # an existing account signed out by OpenAI
                return 200, accounts.start_login(cfg, again=str(data.get("id", ""))), J
            if method == "POST" and path == "accounts/first":
                accounts.use_first(str(data.get("id", "")))
                return 200, {"ok": True}, J
            if method == "POST" and path == "accounts/remove":
                accounts.remove(str(data.get("id", "")))
                return 200, {"ok": True}, J
        except ValueError as e:
            return 400, {"error": str(e)}, J
    if method == "GET" and path == "onboarding":
        from . import onboarding
        return 200, onboarding.status(cfg), J
    if method == "POST" and path == "onboarding/engine":  # the wizard's "which AI do you use": ChatGPT, Claude or both
        from . import onboarding
        try:
            upd = onboarding.apply_choice(cfg, str(data.get("choice", "")))
        except ValueError as e:
            return 400, {"error": str(e)}, J
        upd["roles"], upd["fallback"] = clean_roles(upd["roles"]), clean_roles(upd["fallback"])
        config.save(upd)
        store.event("config", f"setup: using {data['choice']}", source="ui")
        return 200, {"ok": True}, J
    if method == "POST" and path == "selffix":
        from . import selffix
        return 200, selffix.diagnose(cfg, str(data.get("problem", "")), str(data.get("step", ""))), J
    if method == "POST" and path == "report":
        from . import report
        ok, msg = report.user_report(str(data.get("text", ""))[:3000], bool(data.get("diagnostics", True)))
        return 200, {"ok": ok, "message": msg}, J
    if method == "POST" and path == "selftest":
        from . import selffix
        return 200, selffix.selftest(cfg), J
    if method == "POST" and path == "selffix/action":
        from . import selffix
        name = str(data.get("id", ""))
        try:
            args = json.loads(data.get("args_json") or "{}") if name == "set_role_model" else {}
        except ValueError:
            args = {}
        ok, msg = selffix.run_action(cfg, name, args if isinstance(args, dict) else {})
        return 200, {"ok": ok, "message": msg}, J
    if method == "GET" and path == "whatsnew":
        return 200, whatsnew(), J
    if method == "POST" and path == "whatsnew/seen":
        store.kv_set("whatsnew_seen", __version__)
        return 200, {"ok": True}, J
    if method == "GET" and path == "codex/latest":
        return 200, codex_latest(cfg), J
    if method == "GET" and path == "models":
        accounts.ensure_models(cfg)  # an account that hasn't run yet has no list of its own
        return 200, roles.available_models(), J
    if method == "GET" and path == "doctor":
        return 200, doctor(cfg), J
    if method == "GET" and path == "config":
        c = dict(cfg)
        c["sleep_supported"] = sys.platform in sleep_control.SUPPORTED
        c["lid_supported"] = sys.platform == "darwin"
        if c["lid_supported"]:
            try:
                c["lid_awake"] = sleep_control.lid_awake()
            except (OSError, subprocess.SubprocessError):
                c["lid_awake"] = None
        c["jev_key"] = bool(c.get("jev_key"))  # never echo third-party secrets
        return 200, c, J  # api_key is shown: the UI is local-only or key-authenticated
    if method == "POST" and path == "lid_awake":  # Mac: keep running with the lid closed (asks for the password)
        if sys.platform != "darwin":
            return 400, {"error": "only on macOS"}, J
        try:
            on = sleep_control.set_lid_awake(bool(data.get("on")))
        except PermissionError as e:
            return 400, {"error": str(e)}, J
        store.event("lid_awake", "this Mac " + ("keeps running with the lid closed" if on else "sleeps again when the lid closes"))
        return 200, {"on": on}, J
    if method == "POST" and path == "config":
        upd = {k: v for k, v in data.items() if k in EDITABLE}
        if "no_sleep" in upd:
            if type(upd["no_sleep"]) is not bool:
                return 400, {"error": "no_sleep must be true or false"}, J
            if upd["no_sleep"] and sys.platform not in sleep_control.SUPPORTED:
                return 400, {"error": "idle sleep prevention is not supported on this platform"}, J
        if "claude_bin" in upd:  # the Models page lets people point at Claude Code when it isn't found by itself
            upd["claude_bin"] = os.path.expanduser(str(upd["claude_bin"]).strip())
            if upd["claude_bin"] and not os.path.isfile(upd["claude_bin"]):
                return 400, {"error": "no such file: " + upd["claude_bin"]}, J
        if "roles" in upd:
            upd["roles"] = dict(cfg.get("roles") or {}, **clean_roles(upd["roles"]))
        if "fallback" in upd:  # empty model = no fallback for that role
            upd["fallback"] = clean_roles({r: f for r, f in (upd["fallback"] or {}).items() if (f or {}).get("model")})
        if "share_reports" in upd:
            upd["share_reports"] = bool(upd["share_reports"])
        if "jev_key" in upd and upd["jev_key"] is True:
            upd.pop("jev_key")  # UI echoes the masked boolean back; keep the stored key
        new = config.save(upd)
        store.event("config", f"changed: {', '.join(sorted(upd))}", source="ui")
        restart = bool({"bind", "port"} & set(upd))
        return 200, {"ok": True, "restart_needed": restart, "model": new["model"]}, J
    if method == "POST" and path == "key/rotate":
        new = config.save({"api_key": config.new_key()})
        store.event("key_rotated", "API key rotated — update it in OS3", source="ui", level="warn")
        return 200, {"api_key": new["api_key"]}, J
    if method == "POST" and path == "agent/restart":
        ok, msg = os3.restart_agent()
        store.event("restart_agent", msg + " (from UI)", source="ui", level="info" if ok else "error")
        return 200, {"ok": ok, "message": msg}, J
    if method == "POST" and path == "reload":
        if not _supervisor_pid():
            return 409, {"error": "supervisor not running"}, J
        request_reload()
        return 200, {"ok": True}, J
    return 404, {"error": f"unknown endpoint {method} /api/{path}"}, J


def _supervisor_pid():
    try:
        with open(os.path.join(config.HOME, "supervisor.pid")) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) else None


def request_reload():
    """Graceful worker swap; a file instead of SIGHUP so it works on Windows too."""
    open(os.path.join(config.HOME, "reload.request"), "w").close()

