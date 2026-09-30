"""Prototype backend (engine="appserver"): one long-running `codex app-server` instead of a
`codex exec` per request. Saves the process start, the connection setup and the shutdown
(measured: a follow-up turn 1.9 s instead of 4-6 s), keeps task threads loaded, and streams the
answer's text while it is written. Same run() contract as codex_runner.run, so the engine's
corrections, repairs and sessions work unchanged. Off by default until measured on real traffic."""
import json, os, queue, subprocess, tempfile, threading, time

from . import accounts, platform_util, sessions, store
from .codex_runner import (DISABLED, PLAN_RE, RESET_RE, WORKDIR, ClientGone, CodexHung, SignedOut, UsageLimit, idle_limit,
                           known_features, split_model, take)

_EOF = {"method": "__eof__"}


class Server:
    def __init__(self, codex, account=None, images=False):
        known = known_features(codex)
        disabled = [f for f in DISABLED if (f in known or not known) and not (images and f == "image_generation")]
        # like exec's --ignore-user-config: no personal MCP servers or plugins in the decision backend
        cmd = [codex, "app-server", *[a for f in disabled for a in ("--disable", f)], "-c", "mcp_servers={}"]
        self.codex, self.account = codex, account
        self.other = queue.Queue()  # notifications not about a thread (e.g. account/login/completed)
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8", errors="replace", bufsize=1, env=accounts.env(account),
                                  **platform_util.popen_group_kwargs())
        self.lock, self.next = threading.Lock(), 0
        self.pending, self.subs, self.loaded = {}, {}, set()
        self.limits, self.limits_ts = None, 0
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=lambda: [None for _ in self.p.stderr], daemon=True).start()  # drain
        try:
            self.request("initialize", {"clientInfo": {"name": "os3-router", "version": "appserver"}})
            self._send({"jsonrpc": "2.0", "method": "initialized"})
        except Exception:
            self.stop()  # a codex that won't start must not be left running behind the error
            raise

    def alive(self):
        return self.p.poll() is None

    def _send(self, obj):
        with self.lock:
            self.p.stdin.write(json.dumps(obj) + "\n")
            self.p.stdin.flush()

    def request(self, method, params, timeout=90):
        q = queue.Queue()
        with self.lock:
            self.next += 1
            i = self.next
            self.pending[i] = q
        self._send({"jsonrpc": "2.0", "id": i, "method": method, "params": params})
        try:
            m = q.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError(f"app-server: no answer to {method} in {timeout}s")
        finally:
            self.pending.pop(i, None)
        if m is _EOF:
            raise RuntimeError("app-server exited")
        if "error" in m:
            raise RuntimeError(f"app-server {method}: {m['error'].get('message')}")
        return m.get("result") or {}

    def _read(self):
        for line in self.p.stdout:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if "id" in m and ("result" in m or "error" in m):
                q = self.pending.get(m["id"])
                if q:
                    q.put(m)
            elif "id" in m and "method" in m:  # approvals / client tool calls: the router runs no tools itself
                self._send({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "not supported"}})
            elif m.get("method") == "account/rateLimits/updated":
                self.limits, self.limits_ts = (m.get("params") or {}).get("rateLimits"), time.time()
            elif "method" in m:
                q = self.subs.get((m.get("params") or {}).get("threadId"))
                if q:
                    q.put(m)
                elif m["method"].startswith("account/"):
                    self.other.put(m)
        for q in list(self.pending.values()) + list(self.subs.values()):
            q.put(_EOF)

    def rate_limits(self):
        if self.limits is None or time.time() - self.limits_ts > 300:
            try:
                self.limits = self.request("account/rateLimits/read", {}, 20).get("rateLimits")
                self.limits_ts = time.time()
                accounts.note_plan(self.account, (self.limits or {}).get("planType"))
            except RuntimeError as e:
                if accounts.AUTH_RE.search(str(e)):
                    accounts.mark_signed_out(self.account, str(e))
        return self.limits

    def stop(self):
        platform_util.kill_tree(self.p)


_servers, _lock, _starting = {}, threading.Lock(), {}


def server(cfg, account=None, image_gen=False):
    """One running Codex per account: share the auth manager across all task types.

    Separate image servers used the same rotating refresh token with independent caches.
    Image generation is enabled in the process and restricted per thread below.
    """
    codex, acct = cfg.get("codex_bin") or "codex", account or accounts.MAIN
    key = acct
    with _lock:
        s = _servers.get(key)
        if s is not None and s.alive() and s.codex == codex:
            return s
        lk = _starting.setdefault(key, threading.Lock())
    with lk:  # one start per account; other accounts' requests don't wait behind a slow one
        with _lock:
            s = _servers.get(key)
            if s is not None and s.alive() and s.codex == codex:
                return s
        s = Server(codex, None if acct == accounts.MAIN else acct, images=True)
        with _lock:
            _servers[key] = s
        return s


def stop_all():
    with _lock:
        for s in _servers.values():
            s.stop()
        _servers.clear()


def _limits(rl):
    """app-server rate limits -> codex_runner's shape (primary = 5 h, secondary = weekly)."""
    if not rl:
        return None
    w = lambda x: x and {"used_percent": x.get("usedPercent"), "resets_at": x.get("resetsAt"),
                         "window_minutes": x.get("windowDurationMins")}
    return {"primary": w(rl.get("primary")), "secondary": w(rl.get("secondary"))}


def run(cfg, prompt, model, schema=None, alive=lambda: True, images=(), resume=None, keep=False, role=None, on_text=None,
        account=None, image_gen=False):
    """-> (text, usage, thread, rate_limits). Raises ClientGone, CodexHung, UsageLimit, RuntimeError."""
    os.makedirs(WORKDIR, exist_ok=True)
    slug, effort = split_model(model, cfg["effort"])
    tmp = []
    for ext, data in images:
        f = tempfile.NamedTemporaryFile(suffix="." + ext, delete=False)
        f.write(data)
        f.close()
        tmp.append(f.name)
    release = take(cfg, role, alive)
    try:
        srv = server(cfg, account, image_gen)
        base = {"model": slug, "cwd": WORKDIR, "sandbox": "read-only", "approvalPolicy": "never",
                "config": {"features.image_generation": bool(image_gen)}}
        if cfg.get("compact_tokens"):
            base["config"]["model_auto_compact_token_limit"] = int(cfg["compact_tokens"])
        if resume:
            if resume not in srv.loaded:  # e.g. after a router restart: load it from disk
                srv.request("thread/resume", dict(base, threadId=resume))
                srv.loaded.add(resume)
            tid = resume
        else:
            tid = srv.request("thread/start", dict(base, ephemeral=not keep))["thread"]["id"]
            srv.loaded.add(tid)
        q = srv.subs[tid] = queue.Queue()
        try:
            inp = [{"type": "text", "text": prompt}] + [{"type": "localImage", "path": f} for f in tmp]
            turn = srv.request("turn/start", {"threadId": tid, "input": inp, "outputSchema": schema,
                                              "effort": effort, "model": slug})
            msgs, deltas, usage, errs, status = [], [], {}, [], None
            before = None  # the thread's token total before this turn: usage = the whole turn, not its last model call
            start = last = time.time()
            limit = idle_limit(cfg, effort, role)
            rollout, next_check = None, 0
            while status is None:
                try:
                    m = q.get(timeout=1)
                except queue.Empty:
                    m = None
                now = time.time()
                if m is _EOF:
                    raise RuntimeError("app-server exited during the turn")
                if m is not None:
                    meth, p = m.get("method", ""), m.get("params") or {}
                    # only progress counts as activity: status notices (MCP startup, thread status) kept a
                    # stalled turn "alive" for 10 minutes once, until OS3 gave up on the worker
                    if meth.startswith(("item/", "turn/")) or meth == "thread/tokenUsage/updated":
                        last = now
                    if meth == "item/agentMessage/delta":
                        deltas.append(p.get("delta", ""))
                        if on_text:
                            on_text(p.get("delta", ""))
                    elif meth == "item/completed" and (p.get("item") or {}).get("type") == "agentMessage":
                        msgs.append(p["item"].get("text", ""))  # all of them, like exec: the first decision wins
                    elif meth == "thread/tokenUsage/updated":
                        tu = p.get("tokenUsage") or {}
                        tot, lst = tu.get("total") or {}, tu.get("last") or {}
                        keys = ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens")
                        if before is None:
                            before = {k: tot.get(k, 0) - lst.get(k, 0) for k in keys}
                        d = {k: tot.get(k, 0) - before[k] for k in keys} if tot else lst
                        usage = {"input_tokens": d.get("inputTokens", 0), "cached_input_tokens": d.get("cachedInputTokens", 0),
                                 "output_tokens": d.get("outputTokens", 0), "reasoning_output_tokens": d.get("reasoningOutputTokens", 0)}
                    elif meth == "error" and not p.get("willRetry"):
                        errs.append((p.get("error") or {}).get("message", "error"))
                    elif meth == "turn/completed":
                        t = p.get("turn") or {}
                        status = t.get("status")
                        if t.get("error"):
                            errs.append(t["error"].get("message", "turn failed"))
                if status is not None or now < next_check:  # check once a second, also while notices stream in
                    continue
                next_check = now + 1
                if rollout is None:  # reasoning is silent: a growing session file also counts as activity
                    rollout = (sessions.rollout_files(tid) or [False])[0]
                if rollout:
                    try:
                        last = max(last, os.path.getmtime(rollout))
                    except OSError:
                        pass
                gone = not alive()
                if gone or now - last > limit or now - start > cfg["hang_max_s"]:
                    try:
                        srv.request("turn/interrupt", {"threadId": tid, "turnId": (turn.get("turn") or {}).get("id")}, 10)
                    except RuntimeError:
                        pass
                    if gone:
                        raise ClientGone()
                    raise CodexHung(f"codex idle {now - last:.0f}s (total {now - start:.0f}s)")
        finally:
            srv.subs.pop(tid, None)
            if not keep and not resume:  # one-off call: let the server forget it
                srv.loaded.discard(tid)
                try:
                    srv.request("thread/unsubscribe", {"threadId": tid}, 10)
                except RuntimeError:
                    pass
        text = "\n".join(msgs) if msgs else "".join(deltas)
        if not text:
            msg = " | ".join(errs) or f"no output from codex (turn {status})"
            if "newer version" in msg.lower():
                store.kv_set("codex_outdated", time.time())
            if PLAN_RE.search(msg):
                raise UsageLimit(msg, plan=True)
            if accounts.AUTH_RE.search(msg):
                accounts.mark_signed_out(account, msg)
                raise SignedOut(msg)
            if "usage limit" in msg.lower():
                r = RESET_RE.search(msg)
                raise UsageLimit(msg, r.group(1).strip() if r else "")
            raise RuntimeError(msg)
        return text, usage, tid, _limits(srv.rate_limits())
    finally:
        release()
        for f in tmp:
            try:
                os.unlink(f)
            except OSError:
                pass
