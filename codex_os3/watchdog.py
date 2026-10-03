"""Watches the router and the local rabbit-agent and fixes what it safely can.

The failure it exists for: the rabbit-agent's LLM tunnel dies silently. The agent still
reports "connected" and still executes commands over its control channel, but OS3's
model requests never reach this router, so every task fails with "Local LLM device can't
be reached". Evidence: our last reply asked OS3 to run tools, the agent ran them (or
aborted the task with `act.py release_all`), yet no follow-up request ever arrived.
The fix is restarting the agent through its own scheduler (os3.restart_agent).

Rules decide; the optional Jev advisor (jev.py) only adds a second opinion for the
ambiguous "is this silence expected?" case and is recorded alongside."""
import json, os, time, urllib.request

from . import config, jev, os3, platform_util, roles, store
from .notify import desktop

TICK_S = 15
QUIET_S = 120                 # silence after a quick tool call before we suspect the tunnel
RESTART_COOLDOWN_S = 600
DISCONNECTED_S = 180          # agent "disconnected" this long -> restart it
DEDUPE_S = 600
# tool calls after which silence is normal: OS3 waits for the user, a worker, or a schedule
SLOW_TOOLS = {"wait", "ask_user", "create_task", "steer_task", "schedule_add", "schedule_update",
              "phone_call", "phone_call_status", "answer_worker_question"}


def last_response():
    r = store.q("SELECT * FROM requests WHERE status IN ('ok','limit') AND source != 'selftest' "
                "ORDER BY done_ts DESC LIMIT 1")
    return r[0] if r else None


def last_request_ts():
    """Last time OS3 reached the router: a request starting, ending (also cancelled or failed)
    or still running. Any of these proves the tunnel works."""
    r = store.q("SELECT MAX(ts) t, MAX(done_ts) d, SUM(status='running' AND ts > ?) n FROM requests "
                "WHERE source != 'selftest'",
                (time.time() - 900,))[0]
    return time.time() if r["n"] else max(r["t"] or 0, r["d"] or 0)


def snapshot():
    now = time.time()
    resp = last_response()
    calls = json.loads(resp["calls"]) if resp and resp.get("calls") else []
    since = resp["done_ts"] if resp else now - 900
    agent = os3.status()
    agent_events = os3.log_tail(since_ts=since - 5,
                                components=("exec", "guardian", "ws", "tunnel", "tunnel-ws", "main"))
    # v0.1.15 moved CommandExecutor logs to guardian. Ignore the previous process's
    # shutdown messages after a restart, which can otherwise restart the replacement.
    agent_events = [e for e in agent_events if not e.get("pid") or not agent.get("pid")
                    or e["pid"] == agent["pid"]]
    execs = [e for e in agent_events if e.get("component") in ("exec", "guardian")
             and "completed" in e.get("message", "")
             and e["ts"] > since]
    aborts = [e["ts"] for e in agent_events if "release_all" in e.get("message", "") and e["ts"] > since]
    failed = [e["ts"] for e in agent_events if e.get("component") == "tunnel-ws" and e["ts"] > since
              and (e.get("meta") or {}).get("errorCode") == "TUNNEL_SESSION_ERROR"]
    connected = [e["ts"] for e in agent_events if e.get("component") == "tunnel"
                 and "Tunnel pool connected" in e.get("message", "")]
    transport_failure = max(failed, default=0)
    if max(connected, default=0) >= transport_failure:
        transport_failure = 0
    request_ts = last_request_ts()
    return {
        "now": now,
        "last_response": resp and {"id": resp["id"], "ago_s": round(now - resp["done_ts"]),
                                   "result": resp["result"], "calls": calls, "task": resp["task"]},
        "last_request_ago_s": round(now - request_ts) if request_ts else None,
        "agent": agent,
        "agent_status_age_s": round(os3.status_age()),
        "agent_execs_since_response": len(execs),
        "agent_aborted_task_since_response": bool(aborts),
        "agent_abort_ago_s": round(now - aborts[0]) if aborts else None,
        "agent_transport_error_ago_s": round(now - transport_failure) if transport_failure else None,
        "agent_log_recent": [f"{e['component']} {e.get('level')} {e.get('message', '')[:120]}"
                             for e in agent_events[-12:]],
        "hangs_30m": store.q("SELECT COUNT(*) n FROM events WHERE kind IN ('retry','fix_failed','verify_failed') "
                             "AND ts > ?", (now - 1800,))[0]["n"],
        "errors_30m": store.q("SELECT COUNT(*) n FROM events WHERE level='error' AND ts > ?", (now - 1800,))[0]["n"],
        "limits": store.latest_limits(),
        "usage_limit": store.kv_get("usage_limit"),
    }


def rules(s):
    """-> list of findings {kind, level, msg, action}."""
    out = []
    resp, agent = s["last_response"], s["agent"]
    # OS3's housekeeping calls (facts, memory, file signals) are one-shot: it never follows them up, so the silence
    # after one is normal. On a real setup they were 58% of the "tunnel dead" and 81% of the "stalled" findings,
    # and each false one restarted a healthy rabbit-agent.
    one_shot = bool(resp) and bool(resp["calls"]) and set(resp["calls"]) <= roles.BACKGROUND_MARKERS
    waiting_for_os3 = (resp and resp["result"] == "tool_call" and not one_shot
                       and (s["last_request_ago_s"] or 0) >= resp["ago_s"] - 2)  # nothing came in since
    if waiting_for_os3 and agent.get("running"):
        quick = not (set(resp["calls"]) & SLOW_TOOLS)
        # a user cancelling also runs release_all, but then OS3 keeps talking to us (the chat
        # answers); a dead tunnel stays silent. Give it 45s before calling it dead.
        if s["agent_aborted_task_since_response"] and (s.get("agent_abort_ago_s") or 0) >= 45:
            out.append({"kind": "tunnel_dead", "level": "error", "action": "restart_agent",
                        "msg": "OS3 aborted the task after our tool call and never called back: "
                               "the rabbit-agent's LLM tunnel is likely dead"})
        elif quick and s["agent_execs_since_response"] and resp["ago_s"] > QUIET_S:
            out.append({"kind": "tunnel_dead", "level": "error", "action": "restart_agent",
                        "msg": f"the agent ran our tool calls ({', '.join(resp['calls'][:4])}) but no "
                               f"follow-up request for {resp['ago_s']}s: LLM tunnel likely dead"})
        elif quick and s.get("agent_transport_error_ago_s") is not None \
                and s["agent_transport_error_ago_s"] >= QUIET_S:
            out.append({"kind": "tunnel_dead", "level": "error", "action": "restart_agent",
                        "msg": "rabbit-agent reported a tunnel session failure after our tool reply; "
                               "no replacement tunnel or follow-up request arrived"})
        elif quick and resp["ago_s"] > QUIET_S * 2:
            out.append({"kind": "stalled", "level": "warn", "action": None,
                        "msg": f"no follow-up for {resp['ago_s']}s after {', '.join(resp['calls'][:4])}"})
    if agent and not agent.get("running") and os3.installed():
        out.append({"kind": "agent_down", "level": "error", "action": "restart_agent",
                    "msg": "rabbit-agent is not running"})
    elif agent.get("status") not in (None, "connected") and (s.get("agent_status_age_s") or 0) >= DISCONNECTED_S:
        # e.g. after sleep: its own reconnect gave up (seen on MacBooks)
        out.append({"kind": "agent_down", "level": "error", "action": "restart_agent",
                    "msg": f"rabbit-agent {agent.get('status')} for {s['agent_status_age_s'] // 60} min"})
    ul = s["usage_limit"]
    if ul and time.time() - ul["ts"] < 3600:
        out.append({"kind": "usage_limit", "level": "warn", "action": None,
                    "msg": f"{ul.get('backend', 'codex').title()} usage limit reached"
                           + (f", resets at {ul['resets']}" if ul.get("resets") else "")})
    for b, lim in (s["limits"] or {}).items():
        for pk, rk, wk, dflt in (("p_pct", "p_reset", "p_window", "5-hour"), ("s_pct", "s_reset", "s_window", "weekly")):
            pct, reset, win = lim.get(pk) or 0, lim.get(rk), store.win_label(lim.get(wk), dflt)
            if pct >= 90 and not (reset and reset < time.time()):  # a passed reset means it's fresh again
                name = ("Claude" if b == "claude" else f"ChatGPT account {b[6:]}" if b.startswith("codex:")
                        else "ChatGPT (Codex)")  # which account, when there are several
                out.append({"kind": "limit_high", "level": "warn", "action": None, "key": f"{b}:{win}:{reset}",
                            "msg": f"{name} {win} limit at {pct:.0f}%"})
    if s["hangs_30m"] >= 3:
        out.append({"kind": "codex_unstable", "level": "warn", "action": None,
                    "msg": f"{s['hangs_30m']} model hangs/failed retries in 30 min"})
    return out


def _recent(kind, within, source="watchdog"):
    r = store.q("SELECT MAX(ts) t FROM events WHERE source=? AND kind=?", (source, kind))
    return r and r[0]["t"] and time.time() - r[0]["t"] < within


def notify(cfg, finding):
    if not cfg.get("webhook"):
        return
    try:  # ntfy-style: POST plain text to the URL
        urllib.request.urlopen(urllib.request.Request(
            cfg["webhook"], data=f"os3-router: {finding['msg']}".encode(), method="POST"), timeout=10)
    except Exception:
        pass


def tick(cfg):
    s = snapshot()
    findings = rules(s)
    resp = s["last_response"] or {}
    pending = resp.get("result") == "tool_call" and resp.get("ago_s", 0) > 30
    advice = jev.advise(cfg, s) if cfg.get("jev_key") and (pending or findings) else None
    if advice:
        store.kv_set("watchdog_advice", dict(advice, ts=time.time()))
        for f in findings:  # Jev may escalate the ambiguous case; the hard signals still gate it
            if (f["kind"] == "stalled" and advice["diagnosis"] == "tunnel_dead"
                    and (advice.get("confidence") or 0) >= 0.9 and (advice.get("silence_expected") or 1) < 0.2):
                f.update(kind="tunnel_dead", level="error", action="restart_agent",
                         msg=f["msg"] + f" (Jev: tunnel_dead {advice['confidence']:.0%})")
    acted_for = store.kv_get("acted_for")
    for f in findings:
        if f["kind"] == "tunnel_dead" and resp.get("id") and acted_for == resp.get("id"):
            continue  # already handled this stalled reply; don't restart again for it
        if f["kind"] == "limit_high":
            fb = any((cfg.get("fallback") or {}).values())
            desktop(f["msg"] + (": the fallback model takes over at 100%" if fb else
                                       ": set a fallback model in os3-router's Settings"), key=f["key"])
        if f["action"] is None and _recent(f["kind"], 3 * 3600 if f["kind"] == "limit_high" else DEDUPE_S):
            continue
        acted = None
        if f["action"] == "restart_agent" and cfg.get("restart_agent"):
            if _recent("restart_agent", RESTART_COOLDOWN_S):
                acted = "skipped: restarted the agent less than 10 min ago"
            else:
                ok, msg = os3.restart_agent()
                store.event("restart_agent", msg, source="watchdog", level="info" if ok else "error")
                if ok and f["kind"] == "tunnel_dead":
                    store.kv_set("acted_for", resp.get("id"))
                acted = msg
        store.event(f["kind"], f["msg"] + (f" → {acted}" if acted else ""), task=(s["last_response"] or {}).get("task"),
                    source="watchdog", level=f["level"], data={"advice": advice})
        if f["level"] == "error" or acted:
            notify(cfg, f)
    store.kv_set("watchdog_last", {"ts": s["now"], "findings": findings,
                                   "agent": s["agent"], "advice": advice})
    return findings


def keepalive(stop=None):
    """For OS3 nodes without the router (`codex_os3 keepalive`, installed with --node-only):
    restart the rabbit-agent when it stopped or stays disconnected, e.g. after sleep."""
    while not (stop and stop.is_set()):
        try:
            a = os3.status()
            why = ("not running" if a and not a.get("running") else
                   f"{a.get('status')} for {int(os3.status_age())} s"
                   if a.get("status") not in (None, "connected") and os3.status_age() >= DISCONNECTED_S else None)
            if why and os3.installed() and not _recent("restart_agent", 300, source="keepalive"):
                ok, msg = os3.restart_agent()
                store.event("restart_agent", f"agent {why} -> {msg}", source="keepalive", level="info" if ok else "error")
            from . import __version__, updater
            updater.maybe(config.load())  # same automatic updates as the router
            with open(os.path.join(updater.APP, "codex_os3", "__init__.py")) as f:
                if f'"{__version__}"' not in f.read():
                    return  # updated on disk: exit so launchd/systemd starts the new version
        except Exception as e:  # never die
            store.event("keepalive_error", f"{type(e).__name__}: {e}", source="keepalive", level="error")
        (stop.wait if stop else time.sleep)(60)


def _owner(me):
    """Only one watchdog runs, even while two workers overlap during a reload: the owner
    renews a lease; a newer worker takes over once the old lease is stale or released."""
    import os
    lease = store.kv_get("watchdog_owner") or {}
    now = time.time()
    if lease.get("pid") in (None, me) or now - lease.get("ts", 0) > TICK_S * 3 \
            or lease.get("pid") != me and not platform_util.pid_alive(lease.get("pid")):
        store.kv_set("watchdog_owner", {"pid": me, "ts": now, "boot": os.environ.get("CODEX_OS3_SUPERVISOR")})
        return True
    return False


def loop(stop):
    import os
    me, last_prune = os.getpid(), 0
    while not stop.is_set():
        cfg = config.load()
        try:
            owner = _owner(me)
            if cfg.get("watchdog", True) and owner:
                tick(cfg)
            if owner:
                from . import updater
                updater.maybe(cfg)
            if time.time() - last_prune > 3600:
                store.prune(cfg["retention_days"])
                last_prune = time.time()
        except Exception as e:  # the watchdog must never die
            try:
                store.event("watchdog_error", f"{type(e).__name__}: {e}", source="watchdog", level="error")
            except Exception:
                pass
        stop.wait(TICK_S)
