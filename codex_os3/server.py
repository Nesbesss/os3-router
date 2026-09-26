"""HTTP worker: the OpenAI-compatible API for OS3 plus the local web UI and its JSON API."""
import hmac, json, os, re, select, signal, socket, threading, time, urllib.parse, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, config, engine, store, ui_api
from .platform_util import pid_alive

UI_DIR = os.path.join(os.path.dirname(__file__), "ui")
LOCAL = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "os3-router/" + __version__

    def log_message(self, fmt, *a):  # request lines are noise; the engine logs what matters
        pass

    # -- helpers ------------------------------------------------------------

    def send(self, code, body, ctype="application/json", headers=()):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def local(self):
        return self.client_address[0] in LOCAL

    def api_key_ok(self, cfg):
        got = self.headers.get("Authorization", "")
        return bool(cfg["api_key"]) and hmac.compare_digest(got, f"Bearer {cfg['api_key']}")

    def ui_ok(self, cfg):
        """UI/API: always from this machine; remotely only with the key (header or cookie)."""
        if self.local():
            return True
        cookies = dict(c.strip().split("=", 1) for c in self.headers.get("Cookie", "").split(";") if "=" in c)
        return self.api_key_ok(cfg) or (bool(cfg["api_key"]) and
                                         hmac.compare_digest(cookies.get("cxos3", ""), cfg["api_key"]))

    def client_alive(self):
        try:
            r, _, _ = select.select([self.connection], [], [], 0)
            if r and not self.connection.recv(1, socket.MSG_PEEK):
                return False
        except OSError:
            return False
        return True

    def body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(n) or b"{}"), n

    # -- routes -------------------------------------------------------------

    def handle_one_request(self):
        """Count only requests being processed: idle keep-alive connections (an open dashboard
        tab) must not keep a draining worker alive."""
        self.raw_requestline = self.rfile.readline(65537)
        if not self.raw_requestline:
            self.close_connection = True
            return
        srv = self.server
        with srv.active_lock:
            srv.active += 1
        try:
            if not self.parse_request():
                return
            if srv.draining:
                self.close_connection = True  # finish this one, then let the client reconnect elsewhere
            method = getattr(self, "do_" + self.command, None)
            if method is None:
                return self.send_error(501, f"Unsupported method ({self.command!r})")
            method()
            self.wfile.flush()
        finally:
            with srv.active_lock:
                srv.active -= 1

    def do_OPTIONS(self):
        self.send(204, b"", "text/plain", [("Access-Control-Allow-Origin", "*"),
                                            ("Access-Control-Allow-Headers", "Authorization, Content-Type")])

    def do_GET(self):
        cfg = config.load()
        path = self.path.split("?")[0].rstrip("/")
        if path in ("/v1/models", "/models"):
            from . import roles
            ids = list(dict.fromkeys(cfg["models"] + [m["slug"] for m in roles.available_models()]))
            return self.send(200, {"object": "list", "data": [
                {"id": m, "object": "model", "created": 0, "owned_by": "os3-router"} for m in ids]})
        if path in ("/health", "/v1"):
            return self.send(200, {"status": "ok", "version": __version__, "pid": os.getpid()})
        if path == "/login":  # remote dashboard access: /login?key=<api key> sets a cookie
            key = dict(p.split("=", 1) for p in self.path.split("?", 1)[-1].split("&") if "=" in p).get("key", "")
            if not cfg["api_key"] or not hmac.compare_digest(urllib.parse.unquote(key), cfg["api_key"]):
                return self.send(401, b"wrong key", "text/plain")
            return self.send(302, b"", "text/plain", [
                ("Location", "/"),
                ("Set-Cookie", f"cxos3={cfg['api_key']}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000")])
        if path in ("", "/ui", "/index.html", "/app"):
            if not self.ui_ok(cfg):
                return self.send(401, b"unauthorized: open this page on the router's machine, "
                                 b"or log in once with /login?key=<api key>", "text/plain")
            with open(os.path.join(UI_DIR, "app.html" if path == "/app" else "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if path.startswith("/api/"):
            return self.api("GET", path, cfg)
        m = re.fullmatch(r"/guide/([0-9a-z-]+\.(jpg|png))", path)  # setup screenshots, app icon
        if m and os.path.isfile(os.path.join(UI_DIR, "guide", m.group(1))):
            with open(os.path.join(UI_DIR, "guide", m.group(1)), "rb") as f:
                return self.send(200, f.read(), "image/" + ("jpeg" if m.group(2) == "jpg" else "png"),
                                 [("Cache-Control", "max-age=86400")])
        if path == "/app.webmanifest":  # name and icon for the app window (Edge / Chrome app mode)
            return self.send(200, {"name": "OS3 Router", "short_name": "OS3 Router", "start_url": "/app",
                                   "display": "standalone", "background_color": "#1b1b1e", "theme_color": "#1b1b1e",
                                   "icons": [{"src": "/guide/app-icon.png", "sizes": "256x256", "type": "image/png"}]},
                             "application/manifest+json")
        self.send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        cfg = config.load()
        path = self.path.split("?")[0].rstrip("/")
        if path in ("/v1/chat/completions", "/chat/completions"):
            return self.chat(cfg)
        if path.startswith("/api/"):
            return self.api("POST", path, cfg)
        self.send(404, {"error": {"message": "not found"}})

    def api(self, method, path, cfg):
        if not self.ui_ok(cfg):
            return self.send(401, {"error": "unauthorized"})
        # a web page on another origin can't send custom headers without a CORS preflight
        # (which we never answer for /api), so this blocks drive-by POSTs from the browser
        if method == "POST" and self.headers.get("X-Codex-OS3") != "1":
            return self.send(403, {"error": "missing X-Codex-OS3 header"})
        q = {}
        try:
            data = self.body()[0] if method == "POST" else {}
            q = dict(p.split("=", 1) for p in self.path.split("?", 1)[1].split("&") if "=" in p) \
                if "?" in self.path else {}
            code, out, ctype = ui_api.handle(method, path[len("/api/"):], data, q, cfg)
        except Exception as e:  # never take the worker down from the UI
            code, out, ctype = 500, {"error": f"{type(e).__name__}: {e}"}, "application/json"
        extra = [("Content-Disposition", f'attachment; filename="{q.get("name", "export")}.zip"')] \
            if ctype == "application/zip" else []
        self.send(code, out, ctype, extra)

    def chat(self, cfg):
        if not self.api_key_ok(cfg):
            # usually an old OS3 connection (key rotated, reinstalled): the setup page explains it
            got = self.headers.get("Authorization", "").replace("Bearer ", "").strip()
            store.kv_set("auth_fail", {"ts": time.time(), "key_end": got[-4:] if len(got) >= 8 else ("none" if not got else "?")})
            return self.send(401, {"error": {"message": "wrong api key: copy the key from the os3-router setup page "
                                             "into your OS3 connection (delete the old connection first)"}})
        try:
            body, _ = self.body()
        except ValueError as e:
            return self.send(400, {"error": {"message": str(e)}})
        stream = bool(body.get("stream"))
        alive = self.client_alive
        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.flush()

            def stream_alive():
                try:  # SSE comment: ignored by clients, keeps idle timeouts from firing
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    return self.client_alive()
                except OSError:
                    return False
            alive = stream_alive

        turn = engine.Turn(cfg, body, alive, source=self.client_address[0])
        try:
            msg, finish = turn.run()
        except engine.ClientGone:
            self.close_connection = True
            return
        except engine.EngineError as e:
            err = {"error": {"message": str(e), "type": "codex_error"}}
            if stream:
                self.wfile.write(f"data: {json.dumps(err)}\n\ndata: [DONE]\n\n".encode())
                self.close_connection = True
                return
            return self.send(502, err)

        cid, created, model = f"chatcmpl-{uuid.uuid4().hex[:24]}", int(time.time()), turn.requested
        if stream:
            first = {"role": "assistant"}
            if msg.get("tool_calls"):
                first["tool_calls"] = [dict(tc, index=i) for i, tc in enumerate(msg["tool_calls"])]
            else:
                first["content"] = msg.get("content") or ""
            base = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model}
            for delta, fin in ((first, None), ({}, finish)):
                chunk = dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": fin}])
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True
            return
        r = store.q("SELECT in_tok, out_tok FROM requests WHERE id=?", (turn.rid,))
        i, o = (r[0]["in_tok"] or 0, r[0]["out_tok"] or 0) if r else (0, 0)
        self.send(200, {"id": cid, "object": "chat.completion", "created": created, "model": model,
                        "usage": {"prompt_tokens": i, "completion_tokens": o, "total_tokens": i + o},
                        "choices": [{"index": 0, "finish_reason": finish, "message": msg}]})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr):
        self.active = 0
        self.draining = False
        self.active_lock = threading.Lock()
        super().__init__(addr, Handler, bind_and_activate=False)
        if hasattr(socket, "SO_REUSEPORT"):  # lets a new worker bind while the old one drains
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        self.server_bind()
        self.server_activate()


def serve_worker():
    """Run one worker until SIGTERM, then stop accepting and drain in-flight requests."""
    cfg = config.ensure_key()
    srv = Server((cfg["bind"], cfg["port"]))
    stopping = threading.Event()

    def on_term(*_):
        wd_stop.set()  # the replacement worker takes the watchdog lease
        srv.draining = True
        stopping.set()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    drain_file = os.path.join(config.HOME, f"drain-{os.getpid()}")

    parent = os.environ.get("CODEX_OS3_SUPERVISOR")

    def watch_drain():  # the supervisor's portable stop request, or the supervisor vanishing
        while not stopping.is_set():
            if parent and not pid_alive(parent):  # killed hard (e.g. Task Scheduler "End")
                engine.log(f"supervisor {parent} is gone, draining")
                on_term()
                return
            if os.path.exists(drain_file):
                try:
                    os.unlink(drain_file)
                except OSError:
                    pass
                on_term()
                return
            time.sleep(0.5)
    threading.Thread(target=watch_drain, daemon=True).start()
    from . import watchdog
    wd_stop = threading.Event()
    threading.Thread(target=watchdog.loop, args=(wd_stop,), daemon=True, name="watchdog").start()
    engine.log(f"worker {os.getpid()} listening on {cfg['bind']}:{cfg['port']}")
    store.event("worker_start", f"worker {os.getpid()} on {cfg['bind']}:{cfg['port']}", source="worker")
    srv.serve_forever(poll_interval=0.5)
    # Linux spreads SO_REUSEPORT connections over both workers' sockets: whatever already waits
    # in this socket's queue would be reset by close(), so serve it first
    srv.socket.settimeout(0)
    quiet, end = 0, time.time() + 3
    while quiet < 4 and time.time() < end:
        try:
            conn, addr = srv.socket.accept()
        except OSError:
            quiet += 1
            time.sleep(0.05)
            continue
        quiet = 0
        conn.settimeout(None)
        srv.process_request(conn, addr)
    srv.socket.close()  # new connections now go to the new worker only
    deadline = time.time() + 900
    while srv.active and time.time() < deadline:
        time.sleep(0.5)
    engine.log(f"worker {os.getpid()} drained, exiting")
