"""Several Codex (ChatGPT) accounts. The default login (~/.codex, or $CODEX_HOME) is "main"; extra
accounts each have their own Codex home under ~/.codex-os3/accounts/<id>, signed in from the app with
Codex's device-code login. Requests stay on one account until it is nearly out (SWITCH_AT %) or hits
its limit, then move to the next; the first account in order with room is always preferred, so traffic
goes back to it once it resets. The order is main first unless the user put another account first. A conversation stays on the account that holds its thread."""
import glob, json, os, re, time

from . import config, store

SWITCH_AT = 95  # % of the 5-hour or weekly window: move to the next account before hitting the wall
MAIN = "main"
# OpenAI signed this login out (e.g. the plan changed or the password was reset): it needs a new sign-in
AUTH_RE = re.compile(r"token_invalidated|token has been invalidated|sign(?:ing)? in again|refresh_token_reused|"
                     r"could not be refreshed|not logged in|401 unauthorized", re.I)
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
    """Every account, in the order the router tries them (main first unless another was put first)."""
    accts = ([] if store.kv_get("main_removed") else [MAIN]) + extra()
    order = store.kv_get("account_order") or []
    return sorted(accts, key=lambda a: (order.index(a) if a in order else len(order), accts.index(a)))


def use_first(aid):
    """Try this account first; the others follow in their current order."""
    if aid not in all_accounts():
        raise ValueError("unknown account")
    store.kv_set("account_order", [aid] + [a for a in all_accounts() if a != aid])
    store.event("account_order", f"Codex account {aid} is now used first")


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


def signed_out(acct):
    """Signed out by OpenAI in the last 10 minutes (then it is tried again: the flag is only a guess
    that the login is still dead)."""
    return (store.kv_get("signed_out:" + backend_key(acct)) or 0) > time.time() - 600


def mark_signed_out(acct, why=""):
    if not signed_out(acct):
        msg = f"Codex account {acct or MAIN} is signed out: sign in again in the router app (Accounts)"
        store.event("signed_out", f"{msg}. {why[:160]}", "error")
        from . import notify
        notify.desktop(msg, key="signed_out:" + (acct or MAIN))
    store.kv_set("signed_out:" + backend_key(acct), time.time())


def note_plan(acct, plan):
    """The plan as OpenAI reports it with the usage limits: always current (the one in the saved login
    only changes when Codex renews it, days later)."""
    k = "plan:" + backend_key(acct)
    if plan and plan != "unknown" and store.kv_get(k) != plan:
        store.kv_set(k, plan)
    store.kv_set("signed_out:" + backend_key(acct), 0)


def usable(acct):
    return not limited(acct) and not nearly_out(acct) and not signed_out(acct)


def offers(acct, slug):
    """Does this account's plan include the model? None = its model list isn't known yet."""
    from . import roles
    ms = roles.account_models(home(acct))
    return None if ms is None else any(m["slug"] == slug for m in ms)


def pick(prefer=None, model=None):
    """The account for a request. `prefer` = the account that holds this conversation's thread: kept
    while it can still be used (switching loses the cache once). None = every account is out.
    `model`: an account whose plan has it goes first (Free has fewer models than Plus); if none that can
    be used has it, the usual order applies and the plan-swap picks a model the account does have."""
    accts = all_accounts()

    def fits(a):
        return model is None or offers(a, model) is not False
    if prefer in accts and usable(prefer) and fits(prefer):
        return prefer
    for a in accts:
        if usable(a) and fits(a):
            return a
    for a in accts:
        if usable(a):
            return a
    for a in accts:  # all nearly out: take one that isn't hard-limited yet
        if not limited(a) and not signed_out(a):
            return a
    return None


def fetch_models(srv, acct):
    """Ask this account's Codex for its models and keep the answer next to its login (for an account whose
    Codex hasn't run yet, so has no cache of its own)."""
    ms = [{"slug": m["model"], "name": m.get("displayName") or m["model"], "description": m.get("description") or "",
           "efforts": [e.get("reasoningEffort") for e in m.get("supportedReasoningEfforts") or [] if e.get("reasoningEffort")] or ["medium"],
           "default_effort": m.get("defaultReasoningEffort") or "medium", "backend": "codex"}
          for m in srv.request("model/list", {}, 60).get("data") or [] if m.get("model") and not m.get("hidden")]
    if ms:
        os.makedirs(home(acct), exist_ok=True)
        with open(os.path.join(home(acct), "os3-models.json"), "w") as f:
            json.dump({"models": ms, "ts": time.time()}, f)
    return ms


def ensure_models(cfg):
    """Every account needs a model list, or the selector can't show what its plan adds. Accounts without one
    are asked once (again after 10 minutes if that failed)."""
    from . import appserver, roles
    for a in all_accounts():
        if roles.account_models(home(a)) is not None:
            continue
        k = "models_try:" + a
        if (store.kv_get(k) or 0) > time.time() - 600:
            continue
        store.kv_set(k, time.time())
        try:
            fetch_models(appserver.server(cfg, a), a)
        except Exception as e:
            store.event("models_list", f"account {a}: could not ask Codex for its models: {e}"[:200], level="warn", source="accounts")


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


def start_login(cfg, accept_terms=False, again=None):
    """-> {id, login, url, code}. again = an existing account (main too) that needs a new sign-in;
    otherwise a new account, which raises ValueError until the terms are accepted."""
    if again is not None and again not in all_accounts():
        raise ValueError("unknown account")
    if again is None:
        if accept_terms:
            store.kv_set("accounts_terms", time.time())
        if not terms_accepted():
            raise ValueError("accept the terms first")
    from . import appserver
    import threading
    aid = again or new_id()
    os.makedirs(home(aid), exist_ok=True)
    srv = appserver.Server(cfg.get("codex_bin") or "codex", aid)
    try:
        r = srv.request("account/login/start", {"type": "chatgptDeviceCode"}, 60)
    except Exception:
        srv.stop()
        if again is None:
            _drop(aid)
        raise
    st = {"id": aid, "login": r["loginId"], "url": r["verificationUrl"], "code": r["userCode"],
          "status": "waiting", "started": time.time(), "again": again is not None}
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
        if st["id"] == MAIN:
            store.kv_set("account_info:main", dict(who, ts=time.time()))
        else:
            with open(os.path.join(home(st["id"]), "os3-account.json"), "w") as f:
                json.dump(dict(info(st["id"]), **who, added=info(st["id"]).get("added") or time.time()), f)
        note_plan(st["id"], who["plan"])
        try:
            fetch_models(srv, st["id"])  # the plan decides which models this account can offer
        except Exception:
            pass  # (asked again from the model list)
        st.update(status="done", email=who["email"], plan=who["plan"])
        if st["again"]:
            _restart(st["id"])  # its running Codex still holds the old, dead login
            store.event("signed_in", f"Codex account {st['id']} signed in again ({who['plan']})")
        else:
            store.event("account_added", f"Codex account {st['id']} added ({who['plan']})")
    except Exception as e:  # timeout (queue.Empty), refused, duplicate
        st.update(status="error", error=str(e) or "sign-in timed out")
        if not st["again"]:
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


def _restart(aid):
    """Stop this account's running Codex servers; the next request starts fresh ones."""
    from . import appserver
    with appserver._lock:
        gone = [appserver._servers.pop(k) for k in (aid, aid + "+images") if k in appserver._servers]
    for s in gone:
        s.stop()


def remove(aid):
    if aid not in all_accounts():
        raise ValueError("unknown account")
    _restart(aid)
    if aid == MAIN:
        store.kv_set("main_removed", True)  # detach only: never delete the user's shared Codex login
    else:
        _drop(aid)
    store.kv_set("account_order", [a for a in (store.kv_get("account_order") or []) if a != aid])
    key = backend_key(aid)
    for prefix in ("signed_out:", "limited:", "plan:"):
        store.kv_set(prefix + key, None)
    store.kv_set("models_try:" + aid, None)
    store._w("DELETE FROM limits WHERE backend=?", (key,))
    store.event("account_removed", f"Codex account {aid} removed")


def restore_main():
    store.kv_set("main_removed", False)
    store.kv_set("account_info:main", None)


def overview(cfg):
    """For the app: every account with its plan, limits and whether it is in use right now."""
    lims, active = store.latest_limits(), pick()
    out = []
    for a in all_accounts():
        i = main_info(cfg) if a == MAIN else info(a)
        lim = lims.get(backend_key(a))
        out.append({"id": a, "email": i.get("email"), "plan": store.kv_get("plan:" + backend_key(a)) or i.get("plan"),
                    "active": a == active, "limited": limited(a), "nearly_out": nearly_out(a), "signed_out": signed_out(a),
                    "limits": dict(lim) if lim else None})
    return {"accounts": out, "terms": TERMS, "terms_accepted": terms_accepted(), "switch_at": SWITCH_AT,
            "main_removed": bool(store.kv_get("main_removed"))}
