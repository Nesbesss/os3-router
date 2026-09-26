"""Runs the real service (supervisor + worker) against the fake Codex CLI. No network, no quota.
Works on macOS, Linux and Windows."""
import json, os, shutil, socket, subprocess, sys, tempfile, threading, time, unittest, urllib.error, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE = os.path.join(ROOT, "tests", "fake_codex", "codex.cmd" if sys.platform == "win32" else "codex")
WEATHER = [{"type": "function", "function": {"name": "get_weather", "description": "weather",
            "parameters": {"type": "object", "properties": {"location": {"type": "string"}}, "required": ["location"]}}}]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Service(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="cxos3-it-")
        cls.home = os.path.join(cls.tmp, "home")
        cls.port = free_port()
        os.makedirs(cls.home)
        with open(os.path.join(cls.home, "config.json"), "w") as f:
            json.dump({"port": cls.port, "api_key": "cx-test", "codex_bin": FAKE, "hang_idle_s": 3,
                       "watchdog": False}, f)
        cls.env = dict(os.environ, CODEX_OS3_HOME=cls.home, CODEX_HOME=os.path.join(cls.tmp, "codex"),
                       PYTHONUNBUFFERED="1")
        cls.log = open(os.path.join(cls.tmp, "service.log"), "w")
        cls.proc = subprocess.Popen([sys.executable, "-m", "codex_os3", "serve"], cwd=ROOT, env=cls.env,
                                    stdout=cls.log, stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                cls.get("/health")
                return
            except Exception:
                time.sleep(0.2)
        raise RuntimeError("service did not start: " + open(cls.log.name).read())

    @classmethod
    def tearDownClass(cls):
        if sys.platform == "win32":
            cls.proc.kill()  # the worker notices its supervisor is gone and exits
        else:
            cls.proc.terminate()
        cls.proc.wait(timeout=60)
        for _ in range(50):  # the worker must go too, freeing the port
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.5).close()
                time.sleep(0.2)
            except OSError:
                break
        else:
            raise AssertionError("worker still listening after the supervisor stopped")
        cls.log.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- helpers --------------------------------------------------------------
    @classmethod
    def url(cls, path):
        return f"http://127.0.0.1:{cls.port}{path}"

    @classmethod
    def get(cls, path):
        return json.load(urllib.request.urlopen(cls.url(path), timeout=10))

    def post(self, body, key="cx-test", raw=False, timeout=60):
        req = urllib.request.Request(self.url("/v1/chat/completions"), json.dumps(body).encode(),
                                     {"Content-Type": "application/json", "Authorization": "Bearer " + key})
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.read().decode() if raw else json.load(r)

    def api_post(self, path, header=True, body=None):
        h = {"Content-Type": "application/json"}
        if header:
            h["X-Codex-OS3"] = "1"
        return json.load(urllib.request.urlopen(urllib.request.Request(
            self.url("/api/" + path), json.dumps(body or {}).encode(), h), timeout=30))

    # -- tests ----------------------------------------------------------------
    def test_auth(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.post({"messages": [{"role": "user", "content": "hi"}]}, key="wrong")
        self.assertEqual(e.exception.code, 401)
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.api_post("key/rotate", header=False)  # CSRF guard
        self.assertEqual(e.exception.code, 403)

    def test_remote_login_cookie(self):
        req = urllib.request.Request(self.url("/login?key=cx-test"))
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.build_opener(NoRedirect).open(req, timeout=10)
        self.assertEqual(e.exception.code, 302)
        self.assertIn("cxos3=cx-test", e.exception.headers["Set-Cookie"])
        self.assertIn("HttpOnly", e.exception.headers["Set-Cookie"])
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(self.url("/login?key=nope"), timeout=10)
        self.assertEqual(e.exception.code, 401)

    def test_chat(self):
        d = self.post({"messages": [{"role": "user", "content": "hi"}]})
        self.assertIn("hello from fake codex", d["choices"][0]["message"]["content"])
        self.assertEqual(d["usage"]["prompt_tokens"], 1000)

    def test_tool_call_resume_and_limits(self):
        m1 = [{"role": "system", "content": "sys"}, {"role": "user", "content": "weather in Oslo?"}]
        d1 = self.post({"tools": WEATHER, "messages": m1})["choices"][0]
        self.assertEqual(d1["finish_reason"], "tool_calls")
        tc = d1["message"]["tool_calls"][0]
        self.assertEqual(json.loads(tc["function"]["arguments"]), {"location": "Oslo"})
        m2 = m1 + [d1["message"], {"role": "tool", "tool_call_id": tc["id"], "content": "snow FAKE_FINAL"}]
        d2 = self.post({"tools": WEATHER, "messages": m2})["choices"][0]["message"]
        self.assertIn("resumed", d2["content"])
        st = self.get("/api/status")
        self.assertEqual((st["limits"]["p_pct"], st["limits"]["s_pct"]), (12.0, 34.0))

    def test_stream(self):
        s = self.post({"stream": True, "tools": WEATHER, "messages": [{"role": "user", "content": "x"}]}, raw=True)
        self.assertIn('"tool_calls"', s)
        self.assertTrue(s.rstrip().endswith("data: [DONE]"))

    def test_usage_limit_is_a_readable_reply(self):
        d = self.post({"messages": [{"role": "user", "content": "FAKE_LIMIT"}]})
        c = d["choices"][0]["message"]["content"]
        self.assertIn("usage limit", c)
        self.assertIn("9:11 PM", c)

    def test_fallback_at_usage_limit(self):
        self.api_post("config", body={"fallback": {"worker": {"model": "gpt-6-luna", "effort": "low"}}})
        try:
            worker = {"tools": WEATHER, "messages": [{"role": "system", "content": "You are a worker agent."},
                                                     {"role": "user", "content": "FAKE_LIMIT_SOL weather?"}]}
            d = self.post(worker)["choices"][0]["message"]  # gpt-6-sol hits the limit -> same request on luna
            self.assertEqual(d["tool_calls"][0]["function"]["name"], "get_weather")
            r = self.get("/api/requests?limit=1")[0]
            self.assertEqual((r["mode"], r["model"].startswith("gpt-6-luna")), ("fallback", True))
            worker["messages"][1]["content"] = "FAKE_LIMIT_SOL again?"
            self.post(worker)  # while limited: straight to the fallback
            self.assertIn("fallback", self.get("/api/requests?limit=1")[0]["mode"])
            self.assertIn("codex", self.get("/api/status")["fallback_active"])
        finally:
            self.api_post("config", body={"fallback": {}})

    def test_hang_is_killed(self):
        t = time.time()
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.post({"messages": [{"role": "user", "content": "FAKE_HANG"}]}, timeout=60)
        self.assertEqual(e.exception.code, 502)
        self.assertLess(time.time() - t, 30)

    def test_invalid_args_do_not_break_the_reply(self):
        d = self.post({"tools": WEATHER, "messages": [{"role": "user", "content": "FAKE_BADJSON"}]})
        self.assertEqual(d["choices"][0]["finish_reason"], "tool_calls")

    def test_app_window_page(self):
        page = urllib.request.urlopen(self.url("/app"), timeout=10).read().decode()
        self.assertIn('<link rel="manifest" href="/app.webmanifest">', page)
        m = json.load(urllib.request.urlopen(self.url("/app.webmanifest"), timeout=10))
        self.assertEqual((m["name"], m["start_url"]), ("OS3 Router", "/app"))
        icon = urllib.request.urlopen(self.url(m["icons"][0]["src"]), timeout=10)
        self.assertEqual((icon.headers["Content-Type"], icon.read()[:4]), ("image/png", b"\x89PNG"))
        with self.assertRaises(urllib.error.HTTPError):  # only guide images, nothing else from ui/
            urllib.request.urlopen(self.url("/guide/../app.html"), timeout=10)

    def test_ui_and_export(self):
        self.post({"tools": WEATHER, "messages": [{"role": "user", "content": "export me"}]})
        page = urllib.request.urlopen(self.url("/"), timeout=10).read().decode()
        self.assertIn("os3-router", page)
        self.assertTrue(all(c["check"] for c in self.get("/api/doctor")))
        task = self.get("/api/tasks")[0]["task"]
        z = urllib.request.urlopen(self.url(f"/api/export?task={task}"), timeout=10).read()
        self.assertEqual(z[:2], b"PK")

    def test_reload_keeps_requests(self):
        before = self.get("/health")["pid"]
        res = {}
        th = threading.Thread(target=lambda: res.update(d=self.post({"messages": [{"role": "user", "content": "hi"}]})))
        th.start()
        self.api_post("reload")
        pids, t = set(), time.time()
        while time.time() - t < 45 and (not pids or pids == {before}):
            try:
                pids.add(self.get("/health")["pid"])
            except OSError:
                pass  # Windows has no SO_REUSEPORT: a short gap is expected there
            time.sleep(0.2)
        th.join(30)
        self.assertIn("d", res)
        self.assertTrue(pids - {before}, "new worker never answered; service log:\n" + open(self.log.name).read()[-3000:])


if __name__ == "__main__":
    unittest.main()
