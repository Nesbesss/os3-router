"""Several Codex (ChatGPT) accounts. The default login (~/.codex, or $CODEX_HOME) is "main"; extra
accounts each have their own Codex home under ~/.codex-os3/accounts/<id>, signed in from the app with
Codex's device-code login. Requests stay on one account until it is nearly out (SWITCH_AT %) or hits
its limit, then move to the next; the first account in order with room is always preferred, so traffic
goes back to main once it resets. A conversation stays on the account that holds its thread."""
import glob, json, os, time

from . import config, store

SWITCH_AT = 95  # % of the 5-hour or weekly window: move to the next account before hitting the wall
MAIN = "main"
TERMS = ("Using more than one ChatGPT account with os3-router may go against OpenAI's terms of use. "
         "OpenAI could limit, suspend or close the accounts involved. You add accounts at your own risk: "
         "the os3-router developer is not responsible for any limits, suspensions, bans or lost access.")


def root():
    return os.path.join(config.HOME, "accounts")


def default_home():
    return os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex")


def home(acct):
    return default_home() if acct in (None, MAIN) else os.path.join(root(), acct)


def env(acct):
    """Environment for a codex process of this account (None = inherit: the main account)."""
    return None if acct in (None, MAIN) else dict(os.environ, CODEX_HOME=home(acct))


def extra():
    """Signed-in extra accounts, in the order they were added."""
    out = []
    for d in glob.glob(os.path.join(root(), "*")):
        if os.path.isfile(os.path.join(d, "auth.json")):
            out.append(os.path.basename(d))
    return sorted(out, key=lambda a: (len(a), a))


def all_accounts():
    return [MAIN] + extra()


def backend_key(acct):
    """Key for limits/limited bookkeeping: "codex" for main (as before multi-account), "codex:<id>" else."""
    return "codex" if acct in (None, MAIN) else "codex:" + acct


def info(acct):
    try:
        with open(os.path.join(home(acct), "os3-account.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def limited(acct):
    return (store.kv_get("limited:" + backend_key(acct)) or 0) > time.time()


def nearly_out(acct):
    lim = store.latest_limits().get(backend_key(acct))
    if not lim:
        return False
    now = time.time()
    return any((lim[p + "_pct"] or 0) >= SWITCH_AT and (lim[p + "_reset"] or 0) > now for p in ("p", "s"))


def usable(acct):
    return not limited(acct) and not nearly_out(acct)


def pick(prefer=None):
    """The account for a request. `prefer` = the account that holds this conversation's thread: kept
    while it can still be used (switching loses the cache once). None = every account is out."""
    accts = all_accounts()
    if prefer in accts and usable(prefer):
        return prefer
    for a in accts:
        if usable(a):
            return a
    for a in accts:  # all nearly out: take one that isn't hard-limited yet
        if not limited(a):
            return a
    return None


def owner(thread):
    """The account whose Codex home holds this thread (thread ids are unique across accounts)."""
    if not thread:
        return None
    for a in all_accounts():
        if glob.glob(os.path.join(home(a), "sessions", "**", f"rollout-*{thread}.jsonl"), recursive=True):
            return a
    return None


def mark_limited(acct, resets_at=None):
    until = resets_at if resets_at and resets_at > time.time() else time.time() + 900
    store.kv_set("limited:" + backend_key(acct), min(until, time.time() + 7 * 86400))


def new_id():
    n = 2
    while os.path.exists(os.path.join(root(), str(n))):
        n += 1
    return str(n)


# -- adding an account: Codex's device-code login, driven from the app ------------------------------

_logins, _llock = {}, __import__("threading").Lock()
LOGIN_TIMEOUT = 900


def terms_accepted():
    return bool(store.kv_get("accounts_terms"))


def _read(srv):
    a = (srv.request("account/read", {}, 30).get("account") or {})
    return {"email": a.get("email"), "plan": a.get("planType"), "type": a.get("type")}


def main_info(cfg):
    """Email and plan of the main login (cached for an hour)."""
    c = store.kv_get("account_info:main")
    if c and c.get("ts", 0) > time.time() - 3600:
        return c
    from . import appserver
    try:
        c = dict(_read(appserver.server(cfg)), ts=time.time())
        store.kv_set("account_info:main", c)
    except Exception:
        c = c or {}
    return c


def start_login(cfg, accept_terms=False):
    """-> {id, login, url, code}. Raises ValueError until the terms are accepted."""
    if accept_terms:
        store.kv_set("accounts_terms", time.time())
    if not terms_accepted():
        raise ValueError("accept the terms first")
    from . import appserver
    import threading
    aid = new_id()
    os.makedirs(home(aid), exist_ok=True)
    srv = appserver.Server(cfg.get("codex_bin") or "codex", aid)
    try:
        r = srv.request("account/login/start", {"type": "chatgptDeviceCode"}, 60)
    except Exception:
        srv.stop()
        _drop(aid)
        raise
    st = {"id": aid, "login": r["loginId"], "url": r["verificationUrl"], "code": r["userCode"],
          "status": "waiting", "started": time.time()}
    with _llock:
        _logins[st["login"]] = st
    threading.Thread(target=_finish, args=(cfg, srv, st), daemon=True).start()
    return {k: st[k] for k in ("id", "login", "url", "code")}


def _finish(cfg, srv, st):
    try:
        while True:
            m = srv.other.get(timeout=max(1, LOGIN_TIMEOUT - (time.time() - st["started"])))
            if m.get("method") == "account/login/completed":
                break
        p = m.get("params") or {}
        if not p.get("success"):
            raise RuntimeError(p.get("error") or "sign-in failed")
        who = _read(srv)
        taken = [a for a in all_accounts() if a != st["id"] and (info(a) if a != MAIN else main_info(cfg)).get("email")
                 == who["email"]]
        if who["email"] and taken:
            raise RuntimeError(f"{who['email']} is already added (account {taken[0]})")
        with open(os.path.join(home(st["id"]), "os3-account.json"), "w") as f:
            json.dump(dict(who, added=time.time()), f)
        st.update(status="done", email=who["email"], plan=who["plan"])
        store.event("account_added", f"Codex account {st['id']} added ({who['plan']})")
    except Exception as e:  # timeout (queue.Empty), refused, duplicate
        st.update(status="error", error=str(e) or "sign-in timed out")
        _drop(st["id"])
    finally:
        srv.stop()


def login_status(login):
    with _llock:
        st = _logins.get(login)
    return {k: v for k, v in (st or {"status": "unknown"}).items() if k != "started"}


def _drop(aid):
    import shutil
    if aid not in (None, MAIN, "") and os.sep not in aid:
        shutil.rmtree(os.path.join(root(), aid), ignore_errors=True)


def remove(aid):
    if aid == MAIN or aid not in extra():
        raise ValueError("unknown account")
    from . import appserver
    with appserver._lock:
        gone = [appserver._servers.pop(k) for k in (aid, aid + "+images") if k in appserver._servers]
    for s in gone:
        s.stop()
    _drop(aid)
    store.event("account_removed", f"Codex account {aid} removed")


def overview(cfg):
    """For the app: every account with its plan, limits and whether it is in use right now."""
    lims, active = store.latest_limits(), pick()
    out = []
    for a in all_accounts():
        i = main_info(cfg) if a == MAIN else info(a)
        lim = lims.get(backend_key(a))
        out.append({"id": a, "email": i.get("email"), "plan": i.get("plan"), "active": a == active,
                    "limited": limited(a), "nearly_out": nearly_out(a),
                    "limits": dict(lim) if lim else None})
    return {"accounts": out, "terms": TERMS, "terms_accepted": terms_accepted(), "switch_at": SWITCH_AT}
