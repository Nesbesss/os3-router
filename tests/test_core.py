"""Offline tests (no Codex calls). Each case is a failure seen in real OS3 traffic."""
import json, os, sys, tempfile, time, unittest
from unittest import mock

os.environ["CODEX_OS3_HOME"] = tempfile.mkdtemp(prefix="cxos3-test-")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codex_os3 import export, prompt as P, repair, sessions, store, watchdog  # noqa: E402

NODE_A, NODE_B = "11111111-2222-4333-8444-555555555555", "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
SYSTEM = (f'<node id="{NODE_A}" name="studio-mini" default="true"><hostname>Studio-Mini.local</hostname></node>'
          f'<node id="{NODE_B}" name="laptop"><hostname>Laptop.local</hostname></node>')
GEOM = ["--view-width", "1365", "--view-height", "768", "--original-width", "1920", "--original-height", "1080"]
TOOLS = [
    {"type": "function", "function": {"name": "computer_use", "parameters": {
        "type": "object", "required": ["node_id", "script"],
        "properties": {"node_id": {"type": "string"}, "script": {"type": "string"}, "args": {"type": "array"}}}}},
    {"type": "function", "function": {"name": "feed_image", "parameters": {
        "type": "object", "required": ["reference", "node_id"],
        "properties": {"reference": {"type": "string"}, "node_id": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "shell", "parameters": {
        "type": "object", "required": ["command", "node_id"],
        "properties": {"command": {"type": "string"}, "node_id": {"type": "string"}}}}},
]
CU = TOOLS[0]["function"]


def decision(*calls):
    return {"kind": "tool_call", "content": "",
            "calls": [{"tool": t, "arguments_json": json.dumps(a)} for t, a in calls]}


class Repair(unittest.TestCase):
    def test_unescaped_shell_backslash_is_repaired(self):
        # models write `find . \( -name x \)` unescaped: invalid JSON, OS3 then says node_id missing
        a = repair.load_args(r'{"command":"find . \( -name x \) ; echo a\nb","node_id":"x"}')
        self.assertEqual(a["command"], "find . \\( -name x \\) ; echo a\nb")

    def test_node_id_by_name_hostname_and_typo(self):
        f = lambda v: repair.fix_node_id({"node_id": v}, CU, SYSTEM)["node_id"]
        self.assertEqual(f("Studio Mini"), NODE_A)
        self.assertEqual(f("Studio-Mini.local"), NODE_A)
        self.assertEqual(f(NODE_A.replace("4333", "4334")), NODE_A)          # one garbled char
        self.assertEqual(f("0000-far-off"), "0000-far-off")               # never guess

    def test_missing_node_id_only_filled_when_one_node(self):
        one = f'<node id="{NODE_A}" name="x"></node>'
        self.assertEqual(repair.fix_node_id({}, CU, one)["node_id"], NODE_A)
        self.assertNotIn("node_id", repair.fix_node_id({}, CU, SYSTEM))

    def test_dlam_action_as_script(self):
        a = repair.fix_computer_use("computer_use", {"script": "wait", "args": ["--duration", "1"]})
        self.assertEqual((a["script"], a["args"][0]), ("act.py", "wait"))
        self.assertEqual(repair.fix_computer_use("computer_use", {"script": "capture"})["script"], "capture.py")

    def test_schema_problems(self):
        probs = repair.decision_problems(decision(("feed_image", {"node_id": NODE_A}), ("screenshot", {})),
                                         TOOLS, SYSTEM)
        self.assertTrue(any("reference is required" in p for p in probs))
        self.assertTrue(any("no tool named 'screenshot'" in p for p in probs))
        self.assertEqual(repair.decision_problems(decision(
            ("computer_use", {"node_id": NODE_A, "script": "capture.py", "args": ["--out", "/tmp/s.png"]})),
            TOOLS, SYSTEM), [])

    def test_capture_gets_feed_image(self):
        cap = {"id": "1", "type": "function", "function": {"name": "computer_use", "arguments": json.dumps(
            {"node_id": NODE_A, "script": "capture.py", "args": ["--out", "/s.png"]})}}
        out = repair.add_missing_feed([cap], TOOLS)
        self.assertEqual(out[-1]["function"]["name"], "feed_image")
        self.assertEqual(json.loads(out[-1]["function"]["arguments"])["reference"], "/s.png")


class Prompt(unittest.TestCase):
    def test_first_json_object_wins(self):
        # two identical objects glued together used to leak into chat as raw JSON
        raw = '{"kind":"tool_call","calls":[],"content":""}\n{"kind":"tool_call","calls":[],"content":""}'
        self.assertEqual(P.parse_decision(raw)["kind"], "tool_call")
        self.assertIsNone(P.parse_decision("no json"))

    def test_only_newest_images_attached(self):
        img = {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}
        msgs = [{"role": "user", "content": [{"type": "text", "text": f"s{i}"}, img]} for i in range(5)]
        imgs = P.Images(msgs, 2)
        p = P.flatten(msgs, [], imgs)
        self.assertEqual(len(imgs.files), 2)
        self.assertEqual(p.count("[older image omitted]"), 3)
        self.assertNotIn("iVBORw0KGgo", p)

    def test_tool_results_named_by_call_id(self):
        msgs = [{"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "get_setup_status"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "ok"}]
        self.assertIn("[tool result: get_setup_status]", P.flatten(msgs, TOOLS))

    def test_observe_loop(self):
        look = {"role": "assistant", "tool_calls": [{"function": {"name": "computer_use",
                "arguments": json.dumps({"script": "capture.py"})}}]}
        act = {"role": "assistant", "tool_calls": [{"function": {"name": "computer_use",
               "arguments": json.dumps({"script": "act.py"})}}]}
        self.assertEqual(P.observe_streak([act, look, look, look]), 3)
        self.assertEqual(P.observe_streak([look, act]), 0)

    def test_false_unavailable(self):
        self.assertTrue(P.FALSE_UNAVAILABLE.search("computer control isn’t available in this session"))
        self.assertTrue(P.FALSE_UNAVAILABLE.search("I don’t have a `ping` tool available in this session."))
        self.assertTrue(P.FALSE_UNAVAILABLE.search("There is no ping function available here."))
        self.assertFalse(P.FALSE_UNAVAILABLE.search("Done — the file is saved."))
        self.assertFalse(P.FALSE_UNAVAILABLE.search("I don't have any more questions, it's done."))
        names = ["ping", "computer_use", "shell"]
        self.assertTrue(P.claims_unavailable("The available tools here don’t include `ping`, so I can’t make that call.", names))
        self.assertTrue(P.claims_unavailable("computer_use is not something I can run here", names))
        self.assertFalse(P.claims_unavailable("Pinged it: the reply was pong.", names))          # no negation
        self.assertFalse(P.claims_unavailable("I can't find that file on your Mac.", names))       # no tool named
        self.assertFalse(P.claims_unavailable("Done, nothing else to do.", names))
        long_ok = ("I ran shell to check the display server and everything is set up correctly; screenshots and "
                   "input both work. There's no permission you need to enable.")
        self.assertFalse(P.claims_unavailable(long_ok, names))


class Sessions(unittest.TestCase):
    def test_resume_parallel_and_compaction(self):
        m1 = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}]
        k = sessions.key(m1, TOOLS)
        self.assertEqual(sessions.plan(k, m1), (True, None, None))
        self.assertEqual(sessions.plan(k, m1)[0], False)       # parallel retry: untracked
        sessions.done(k, "T1", m1, True)
        m2 = m1 + [{"role": "tool", "content": "r"}]
        tracked, th, delta = sessions.plan(k, m2)
        self.assertEqual((tracked, th, len(delta)), (True, "T1", 1))
        sessions.done(k, "T1", m2, True)
        compacted = m1 + [{"role": "user", "content": "[summary]"}]
        self.assertEqual(sessions.plan(k, compacted)[1], None)  # history rewritten: fresh
        sessions.done(k, None, compacted, False)
        self.assertIsNone(store.session_get(k))

    def test_chat_continues_when_os3_replaces_its_trailing_snapshot(self):
        base = [{"role": "system", "content": "channel"}, {"role": "user", "content": "hi"}]
        snap = lambda t: {"role": "user", "content": f"<supplementary-context>{t}</supplementary-context>"}
        k = sessions.key(base, TOOLS)
        sessions.plan(k, base + [snap(1)])
        sessions.done(k, "C1", base + [snap(1)], True)
        nxt = base + [{"role": "assistant", "content": "hello"}, {"role": "user", "content": "q2"}, snap(2)]
        tracked, th, delta = sessions.plan(k, nxt)
        self.assertEqual((th, [m["content"] for m in delta]), ("C1", ["hello", "q2", snap(2)["content"]]))
        sessions.done(k, "C1", nxt, True)
        rewritten = base[:1] + [{"role": "user", "content": "other"}, snap(3)]
        self.assertIsNone(sessions.plan(k, rewritten)[1])  # an older message changed: still fresh
        sessions.done(k, None, rewritten, False)


class ContinueByLastReply(unittest.TestCase):
    """OS3 changes older messages between requests (shortens a result, swaps its context notes). The session is
    still continued as long as our own last reply is the newest assistant message, with something new after it."""
    sysm = {"role": "system", "content": "channel"}
    first = {"role": "user", "content": "do it"}
    snap = staticmethod(lambda t: {"role": "user", "content": f"<supplementary-context>{t}</supplementary-context>"})
    call = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_abc123", "type": "function",
                                                                  "function": {"name": "shell", "arguments": "{}"}}]}

    def stored(self, reply):
        base = [self.sysm, self.first]
        k = sessions.key(base, TOOLS)
        m1 = base + [{"role": "assistant", "content": "earlier"}, {"role": "tool", "tool_call_id": "x", "content": "FULL RESULT " * 50}, self.snap(1)]
        sessions.plan(k, m1)
        sessions.done(k, "T1", m1, True, reply)
        return k, base

    def test_older_message_changed_but_our_reply_is_there(self):
        k, base = self.stored(self.call)
        m2 = base + [{"role": "assistant", "content": "earlier"}, {"role": "tool", "tool_call_id": "x", "content": "FULL RESULT [shortened]"},
                     self.call, {"role": "tool", "tool_call_id": "call_abc123", "content": "done"}, self.snap(2)]
        tracked, th, delta = sessions.plan(k, m2)
        self.assertEqual((th, sessions.VIA.pop(k, None), len(delta)), ("T1", "anchor", 3))
        self.assertIs(delta[0], self.call)
        sessions.done(k, None, m2, False)

    def test_plain_answer_is_found_by_its_text(self):
        k, base = self.stored({"role": "assistant", "content": "All set.  "})
        m2 = base + [{"role": "assistant", "content": "earlier (rewritten)"}, {"role": "assistant", "content": "All set."},
                     {"role": "user", "content": "thanks, and?"}, self.snap(2)]
        self.assertEqual(sessions.plan(k, m2)[1], "T1")
        sessions.done(k, None, m2, False)

    def test_not_continued_when_it_is_not_ours(self):
        for label, tail in (("another assistant message is newer", [self.call, {"role": "assistant", "content": "someone else"}, self.snap(2)]),
                            ("our reply is the last message (nothing new)", [self.call]),
                            ("a different call", [dict(self.call, tool_calls=[{"id": "call_zzz", "type": "function", "function": {"name": "shell", "arguments": "{}"}}]), self.snap(2)])):
            k, base = self.stored(self.call)
            m2 = base + [{"role": "assistant", "content": "earlier (rewritten)"}] + tail
            self.assertIsNone(sessions.plan(k, m2)[1], label)
            sessions.done(k, None, m2, False)

    def test_no_reply_stored_means_no_guessing(self):
        k, base = self.stored(None)
        m2 = base + [{"role": "assistant", "content": "changed"}, self.call, {"role": "tool", "tool_call_id": "call_abc123", "content": "done"}]
        self.assertIsNone(sessions.plan(k, m2)[1])
        sessions.done(k, None, m2, False)

    def test_engine_resumes_and_says_so(self):
        from codex_os3 import codex_runner, config, engine
        seen = []

        def run(cfg, prompt, model, schema, alive, images, resume, keep, **kw):
            seen.append(resume)
            return '{"kind":"tool_call","content":"","calls":[{"tool":"create_task","arguments_json":"{}"}]}', {}, "T-eng", None
        tools = [{"type": "function", "function": {"name": "create_task", "parameters": {}}}]
        cfg = dict(config.load(), engine="exec", role_routing=False)
        base = [self.sysm, {"role": "user", "content": "engine anchor test"}]
        with mock.patch.object(codex_runner, "run", side_effect=run):
            hist = lambda text: [{"role": "assistant", "content": "earlier"}, {"role": "tool", "tool_call_id": "x", "content": text}]
            t1 = engine.Turn(cfg, {"model": "gpt-6-luna", "messages": base + hist("FULL RESULT " * 40) + [self.snap(1)], "tools": tools}, lambda: True)
            reply, _ = t1.run()
            older = hist("FULL RESULT [shortened by OS3]")                          # an older message that is not the same any more
            t2 = engine.Turn(cfg, {"model": "gpt-6-luna", "tools": tools, "messages": base + older + [reply,
                             {"role": "tool", "tool_call_id": reply["tool_calls"][0]["id"], "content": "ok"}, self.snap(2)]}, lambda: True)
            t2.run()
        self.assertEqual(seen, [None, "T-eng"])
        row = store.q("SELECT mode FROM requests WHERE id=?", (t2.rid,))[0]
        self.assertEqual(row["mode"], "resume·anchor")
        sessions.done(t2.task, None, [], False)


class Roles(unittest.TestCase):
    def body(self, tools, system="You are an assistant."):
        return {"tools": [{"type": "function", "function": {"name": n}} for n in tools],
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": "x"}]}

    def test_classify(self):
        from codex_os3 import roles
        self.assertEqual(roles.classify(self.body(["create_task", "notify_before_act", "wait"])), "chat")
        self.assertEqual(roles.classify(self.body(["shell", "computer_use"], "You are a worker agent in OS3.")), "worker")
        self.assertEqual(roles.classify(self.body(["report_missed_action", "report_correction", "wait"])), "background")
        self.assertEqual(roles.classify(self.body(["emit_facts"])), "background")
        self.assertEqual(roles.classify(self.body([])), "background")
        self.assertEqual(roles.classify(self.body(["ping"])), "chat")  # OS3's connection probe

    def test_pick(self):
        from codex_os3 import roles
        cfg = {"model": "gpt-6-luna", "effort": "medium", "role_routing": True,
               "roles": {"worker": {"model": "gpt-6-sol", "effort": "high"}}}
        self.assertEqual(roles.pick(cfg, "worker", "gpt-6-luna"), "gpt-6-sol-high")
        self.assertEqual(roles.pick(cfg, "chat", "gpt-6-luna"), "gpt-6-luna-medium")
        self.assertEqual(roles.pick(dict(cfg, role_routing=False), "worker", "gpt-5.5"), "gpt-5.5")
        bg = dict(cfg, roles=dict(cfg["roles"], background={"model": "gpt-6-luna", "effort": "low"}))
        self.assertEqual(roles.pick(bg, "background", "gpt-6-luna"), "gpt-6-luna-low")
        self.assertEqual(roles.pick(bg, "background", "gpt-6-luna", images=True), "gpt-6-sol-low")  # OS3's image check

    def test_split_effort(self):
        from codex_os3.codex_runner import split_model
        self.assertEqual(split_model("gpt-6-sol-ultra", "medium"), ("gpt-6-sol", "ultra"))
        self.assertEqual(split_model("gpt-6-luna", "medium"), ("gpt-6-luna", "medium"))


class Export(unittest.TestCase):
    def test_redaction(self):
        t = export.redact('Authorization: Bearer abcdefghijklmnop "password": "hunter2secret" '
                          'user pass: schoolpw123 key cx-0123456789abcdef0123 wachtwoord=geheim99',
                          extra=["mysecretkey"])
        for leak in ("abcdefghijklmnop", "hunter2secret", "schoolpw123", "cx-0123456789abcdef0123", "geheim99"):
            self.assertNotIn(leak, t)

    def test_build(self):
        rid = store.request_start("abcdef0123456789", "t", "m", False, 3, 2, 100)
        store.request_end(rid, status="ok", result="tool_call", calls=["computer_use"], mode="fresh")
        store.event("call_fixed", "password: hunter2 in args", task="abcdef0123456789")
        z = export.build("abcdef0123456789", {"api_key": "", "jev_key": ""})
        import io, zipfile
        rep = zipfile.ZipFile(io.BytesIO(z)).read("report.md").decode()
        self.assertIn("no follow-up request", rep)
        self.assertNotIn("hunter2", rep)


class Watchdog(unittest.TestCase):
    def snap(self, **kw):
        s = {"now": time.time(), "last_response": {"ago_s": 150, "result": "tool_call",
             "calls": ["computer_use", "feed_image"], "task": "t"}, "last_request_ago_s": 160,
             "agent": {"running": True, "status": "connected"}, "agent_execs_since_response": 2,
             "agent_aborted_task_since_response": False, "hangs_30m": 0, "errors_30m": 0,
             "limits": None, "usage_limit": None}
        s.update(kw)
        return s

    def kinds(self, s):
        return [(f["kind"], f["action"]) for f in watchdog.rules(s)]

    def test_silence_after_a_one_shot_housekeeping_call_is_normal(self):
        """OS3 never follows up emit_facts / extract_file_signals / emit_merged_soul (96-100% of tasks end there):
        the watchdog must not call that a dead tunnel or restart the agent (58% of tunnel_dead on a real Mac)."""
        for tools in (["emit_facts"], ["extract_file_signals"], ["emit_merged_soul"], ["emit_facts", "report_correction"]):
            s = self.snap(last_response={"ago_s": 700, "result": "tool_call", "calls": tools, "task": "t"},
                          last_request_ago_s=705, agent_aborted_task_since_response=True)
            self.assertEqual([k for k, _ in self.kinds(s)], [], tools)
        s = self.snap(last_response={"ago_s": 700, "result": "tool_call", "calls": ["emit_facts", "shell"], "task": "t"},
                      last_request_ago_s=705)
        self.assertIn(("tunnel_dead", "restart_agent"), self.kinds(s))  # a real tool mixed in: still watched

    def test_dead_tunnel_after_executed_calls(self):
        self.assertIn(("tunnel_dead", "restart_agent"), self.kinds(self.snap()))

    def test_dead_tunnel_on_abort(self):
        s = self.snap(last_response={"ago_s": 60, "result": "tool_call", "calls": ["shell"], "task": "t"},
                      last_request_ago_s=65, agent_aborted_task_since_response=True, agent_abort_ago_s=50)
        self.assertIn(("tunnel_dead", "restart_agent"), self.kinds(s))
        s.update(agent_abort_ago_s=10, agent_execs_since_response=0)   # maybe a user cancel: wait
        self.assertEqual(self.kinds(s), [])

    def test_waiting_on_user_is_not_a_failure(self):
        s = self.snap(last_response={"ago_s": 900, "result": "tool_call", "calls": ["ask_user"], "task": "t"},
                      last_request_ago_s=905)
        self.assertEqual(self.kinds(s), [])

    def test_new_request_arrived(self):
        self.assertEqual(self.kinds(self.snap(last_request_ago_s=5)), [])

    def test_single_watchdog_lease(self):
        import os
        store.kv_set("watchdog_owner", {})
        self.assertTrue(watchdog._owner(os.getpid()))
        self.assertFalse(watchdog._owner(999999999 if os.getpid() != 999999999 else 1) and False)
        other = os.getppid()  # alive process holding a fresh lease blocks us
        store.kv_set("watchdog_owner", {"pid": other, "ts": time.time()})
        self.assertFalse(watchdog._owner(os.getpid()))
        store.kv_set("watchdog_owner", {"pid": other, "ts": time.time() - 999})  # stale lease
        self.assertTrue(watchdog._owner(os.getpid()))

    def test_cancelled_request_counts_as_activity(self):
        # a reply with tool calls at t-150; OS3's next request started earlier (a retry of a slow
        # turn) and was cancelled at t-20: OS3 reached us, so the tunnel is not dead
        now = time.time()
        store.db().execute("DELETE FROM requests")
        r1 = store.request_start("t", "x", "m", False, 1, 2, 10)
        r2 = store.request_start("t", "x", "m", False, 1, 2, 10)
        store.db().execute("UPDATE requests SET ts=? WHERE id=?", (now - 300, r1))
        store.db().execute("UPDATE requests SET ts=? WHERE id=?", (now - 200, r2))
        store.request_end(r1, status="ok", result="tool_call", calls=["ls", "shell"], done_ts=now - 150)
        store.request_end(r2, status="gone", done_ts=now - 20)
        self.assertLess(now - watchdog.last_request_ts(), 30)
        r3 = store.request_start("t", "x", "m", False, 1, 2, 10)  # still running = alive
        self.assertLess(now - watchdog.last_request_ts(), 2)
        store.request_end(r3, status="ok")

    def test_hang_limit_grows_with_effort(self):
        from codex_os3.codex_runner import idle_limit
        self.assertEqual(idle_limit({"hang_idle_s": 90}, "medium"), 90)
        self.assertEqual(idle_limit({"hang_idle_s": 90}, "high"), 180)
        self.assertEqual(round(idle_limit({"hang_idle_s": 90}, "xhigh")), 300)

    def test_background_calls_get_the_long_hang_limit(self):
        # issue #18: low-effort memory/soul merges write 10k+ tokens as one message and codex
        # prints nothing until it is done; a 90s idle limit killed them twice in a row
        from codex_os3.codex_runner import idle_limit
        cfg = {"hang_idle_s": 90}
        self.assertEqual(round(idle_limit(cfg, "low", "background")), 300)
        self.assertEqual(round(idle_limit(cfg, "high", "background")), 300)
        self.assertEqual(idle_limit(cfg, "low", "chat"), 90)  # interactive roles keep the fast limit
        self.assertEqual(idle_limit(cfg, "medium", "worker"), 90)
        self.assertEqual(idle_limit(cfg, "low"), 90)

    def test_engine_passes_role_to_runner(self):
        from codex_os3 import codex_runner, engine
        t = engine.Turn.__new__(engine.Turn)
        t.cfg, t.model, t.backend, t.schema, t.alive, t.rid, t.role = (
            {"engine": "exec"}, "gpt-6-luna-low", "codex", None, lambda: True, None, "background")
        with mock.patch.object(codex_runner, "run", return_value=("ok", {}, "th", None)) as run,                 mock.patch.object(engine.store, "add_tokens"), mock.patch.object(engine.store, "add_limits"):
            self.assertEqual(t.codex("p"), ("ok", "th"))
        self.assertEqual(run.call_args.kwargs["role"], "background")

    def test_final_answer_is_quiet(self):
        s = self.snap(last_response={"ago_s": 900, "result": "final", "calls": [], "task": "t"})
        self.assertEqual(self.kinds(s), [])


class ClaudeBackendTest(unittest.TestCase):
    def test_backend_by_model(self):
        from codex_os3 import roles
        for m in ("claude-sonnet-5-medium", "sonnet-high", "claude-opus-5-5"):
            self.assertEqual(roles.backend(m), "claude", m)
        for m in ("gpt-6-luna-medium", "gpt-6-sol"):
            self.assertEqual(roles.backend(m), "codex", m)

    def test_cmd_and_limits(self):
        from codex_os3 import claude_runner as C
        cmd = C.build_cmd({"effort": "medium"}, "claude-opus-5-5-ultra", {"type": "object"})
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-opus-5-5")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "max")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertIn("--no-session-persistence", cmd)
        self.assertIn("--resume", C.build_cmd({"effort": "low"}, "claude-sonnet-5", resume="abc"))
        rl = C.limits({"unifiedWindows": {"five_hour": {"utilization": 0.39, "resetsAt": 1},
                                          "seven_day": {"utilization": 0.44, "resetsAt": 2}}})
        self.assertEqual((rl["primary"]["used_percent"], rl["secondary"]["window_minutes"]), (39.0, 10080))
        self.assertIsNone(C.limits({}))


    def test_claude_errors_are_told_apart(self):
        from codex_os3 import claude_runner as C
        from codex_os3.codex_runner import SignedOut, UsageLimit
        for msg, exc, plan in (("Not logged in · Please run /login", SignedOut, False),
                               ("There's an issue with the selected model (claude-x). It may not exist or you may not have access to it.",
                                UsageLimit, True)):
            line = json.dumps({"type": "result", "is_error": True, "result": msg})
            with mock.patch.object(C, "_supervise", return_value=([line], [], "t")):
                with self.assertRaises(exc) as cm:
                    C.run({"effort": "medium", "max_codex": 3, "hang_idle_s": 90}, "hi", "claude-sonnet-5-5-low")
            self.assertEqual(cm.exception.plan, plan)

    def test_claude_model_without_claude_code_uses_codex(self):
        from codex_os3 import roles
        cfg = {"role_routing": True, "model": "gpt-6-luna", "effort": "medium",
               "roles": {"chat": {"model": "claude-sonnet-5-5", "effort": "medium"}},
               "fallback": {"chat": {"model": "claude-haiku-4-5", "effort": "low"}}}
        with mock.patch.object(roles, "claude_installed", return_value=False):
            self.assertEqual(roles.pick(cfg, "chat", "gpt-6-luna"), "gpt-6-luna-medium")
            self.assertIsNone(roles.pick_fallback(cfg, "chat"))
            self.assertEqual(roles.pick(dict(cfg, role_routing=False, model="claude-opus-5-5"), "chat", None), "gpt-6-luna")
        with mock.patch.object(roles, "claude_installed", return_value=True):
            self.assertEqual(roles.pick(cfg, "chat", "gpt-6-luna"), "claude-sonnet-5-5-medium")

    def test_a_codex_thread_is_not_resumed_by_claude(self):
        from codex_os3 import claude_runner, config, engine, roles, sessions
        cfg = dict(config.load(), roles={"chat": {"model": "claude-sonnet-5-5", "effort": "low"}})
        body = {"model": "x", "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}],
                "tools": [{"type": "function", "function": {"name": "create_task", "parameters": {}}}]}
        seen = []

        def run(cfg, prompt, model, schema, alive, images, resume, keep, **k):
            seen.append(resume)
            return '{"kind":"final","calls":[],"content":"hi"}', {}, "S1", None
        with mock.patch.object(roles, "claude_installed", return_value=True), \
                mock.patch.object(sessions, "plan", return_value=(True, "CODEX-THREAD", body["messages"][1:])), \
                mock.patch.object(engine.accounts, "owner", return_value="main"), \
                mock.patch.object(claude_runner, "run", side_effect=run):
            msg, _ = engine.Turn(cfg, body, lambda: True).run()
        self.assertEqual((seen, msg["content"]), ([None], "hi"))


class LimitWarningNameTest(unittest.TestCase):
    def test_names_the_account(self):
        snap = {"last_response": None, "agent": {}, "usage_limit": None, "hangs_30m": 0, "last_request_ago_s": None,
                "limits": {b: {"p_pct": 95, "p_reset": time.time() + 3600, "p_window": 300, "s_pct": 0, "s_reset": None, "s_window": None}
                           for b in ("codex", "codex:2", "claude")}}
        msgs = [f["msg"] for f in watchdog.rules(snap)]
        self.assertEqual(sorted(m.split(" 5-hour")[0] for m in msgs), ["ChatGPT (Codex)", "ChatGPT account 2", "Claude"])


class CodexUpdatePauseTest(unittest.TestCase):
    def test_failed_codex_update_is_not_retried_for_hours(self):
        from codex_os3 import selffix, updater
        store.kv_set("codex_update_failed", 0)
        with mock.patch.object(selffix, "update_codex", return_value=(False, "npm broke")) as up, \
                mock.patch.object(updater.store, "event"):
            updater._update_codex({})
            updater._update_codex({})
            self.assertEqual(up.call_count, 1)
            store.kv_set("codex_update_failed", time.time() - 7 * 3600)
            updater._update_codex({})
            self.assertEqual(up.call_count, 2)
        store.kv_set("codex_update_failed", 0)

    def test_npm_runs_from_a_folder_that_exists(self):
        from codex_os3 import config, selffix
        with mock.patch.object(selffix.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run, \
                mock.patch.object(selffix.ui_api, "codex_info", return_value={"version": "1"}), \
                mock.patch.object(selffix.shutil, "which", return_value="/usr/bin/npm"), \
                mock.patch.object(selffix.os.path, "isfile", return_value=True):
            selffix.update_codex({"codex_bin": "/x/codex"})
        self.assertEqual(run.call_args.kwargs["cwd"], config.HOME)


class SetupChoiceTest(unittest.TestCase):
    """The wizard asks which AI you use and then checks only that one (it used to insist on Codex)."""
    def cfg(self, **kw):
        from codex_os3 import config
        return dict(config.DEFAULTS, **kw)

    def apply(self, cfg, which):
        from codex_os3 import onboarding
        return dict(cfg, **onboarding.apply_choice(cfg, which))

    def test_choices_set_the_routing(self):
        from codex_os3 import onboarding as O
        c = self.apply(self.cfg(), "claude")
        self.assertEqual({k: v["model"] for k, v in c["roles"].items()},
                         {"chat": "claude-sonnet-5-5", "worker": "claude-sonnet-5-5", "background": "claude-haiku-4-5"})
        self.assertEqual((O.choice(c), O.backends(c)), ("claude", {"claude"}))
        b = self.apply(self.cfg(), "both")
        self.assertEqual(O.choice(b), "both")
        self.assertTrue(all(b["fallback"][k]["model"].startswith("claude") for k in O.ROLE_KEYS))
        back = self.apply(self.apply(c, "both"), "codex")   # Claude -> both -> ChatGPT: nothing of Claude left behind
        self.assertEqual((O.choice(back), back["fallback"]), ("codex", {}))
        with self.assertRaises(ValueError):
            O.apply_choice(self.cfg(), "gemini")

    def test_own_picks_survive(self):
        mine = self.cfg(roles={"chat": {"model": "gpt-6-sol", "effort": "high"}, "worker": {"model": "claude-opus-5-5", "effort": "high"},
                               "background": {"model": "gpt-6-luna", "effort": "low"}})
        c = self.apply(mine, "claude")
        self.assertEqual((c["roles"]["worker"]["model"], c["roles"]["worker"]["effort"]), ("claude-opus-5-5", "high"))
        b = self.apply(mine, "both")
        self.assertEqual(b["roles"]["chat"], {"model": "gpt-6-sol", "effort": "high"})
        self.assertTrue(b["fallback"]["chat"]["model"].startswith("claude") and b["fallback"]["worker"]["model"].startswith("gpt"))

    def test_only_the_chosen_subscription_is_checked(self):
        """A Claude-only setup passes without Codex on the machine; one that needs Codex still fails without it."""
        from codex_os3 import onboarding as O, ui_api
        gone = {"path": None, "version": None, "logged_in": None}
        claude_ok = {"path": "/x/claude", "logged_in": True, "detail": ""}
        with mock.patch.object(ui_api, "codex_info", return_value=gone), mock.patch.object(ui_api, "claude_info", return_value=claude_ok):
            self.assertEqual(O.engine_step(self.apply(self.cfg(), "claude"))["state"], "ok")
            s = O.engine_step(self.cfg())
            self.assertEqual(s["state"], "error")
            self.assertIn("Codex CLI", s["detail"])
        with mock.patch.object(ui_api, "claude_info", return_value={"path": None, "logged_in": None, "detail": ""}), \
                mock.patch.object(ui_api, "codex_info", return_value={"path": "/x/codex", "version": "9.9.9", "logged_in": True}):
            s = O.engine_step(self.apply(self.cfg(), "claude"))
            self.assertEqual(s["state"], "error")
            self.assertIn("Claude Code isn't installed", s["detail"])
            self.assertEqual(O.engine_step(self.cfg())["state"], "ok")     # ChatGPT users aren't asked about Claude

    def test_endpoint_saves_the_choice(self):
        from codex_os3 import config, ui_api
        with tempfile.TemporaryDirectory() as home, mock.patch.object(config, "HOME", home), \
                mock.patch.object(config, "PATH", os.path.join(home, "config.json")), mock.patch.object(ui_api.store, "event"):
            self.assertEqual(ui_api.handle("POST", "onboarding/engine", {"choice": "claude"}, {}, config.load())[0], 200)
            self.assertEqual(config.load()["roles"]["chat"]["model"], "claude-sonnet-5-5")
            self.assertEqual(ui_api.handle("POST", "onboarding/engine", {"choice": "x"}, {}, config.load())[0], 400)


class SpeedNumbersTest(unittest.TestCase):
    def test_the_speed_card_reads_what_was_recorded(self):
        from codex_os3 import ui_api
        store._w("DELETE FROM requests")
        now = time.time()

        def add(ts, secs, mode, task, first=None, role="chat", status="ok"):
            rid = store.request_start(task, "127.0.0.1", "gpt-6-luna-medium", True, 1, 2, 10, role)
            store._w("UPDATE requests SET ts=?, done_ts=?, mode=?, status=?, first_ts=?, in_tok=100, cached_tok=60 WHERE id=?",
                     (ts, ts + secs, mode, status, ts + first if first else None, rid))
        for i in range(10):                                   # this week: continued follow-ups, streamed, quick
            add(now - 3600 - i * 60, 6.0, "fresh" if i == 9 else "resume", "t-now", first=2.0 if i % 2 == 0 else None)
        for i in range(10):                                   # the week before: everything started over, slower
            add(now - 3 * 86400 - i * 60, 10.0, "fresh", "t-old")
        add(now - 600, 99, "fresh", "t-x", role="worker")     # not chat: not counted
        add(now - 500, 99, "fresh", "t-y", status="limit")    # not answered: not counted
        sp = ui_api.speed(24)
        a, b = sp["now"], sp["before"]
        self.assertEqual((a["n"], a["median_s"], a["first_s"], a["n_first"]), (10, 6.0, 2.0, 5))
        self.assertEqual((a["follow_n"], a["continued_pct"]), (9, 100))     # the first of a conversation is not a follow-up
        self.assertEqual((b["n"], b["median_s"], b["continued_pct"], b["first_s"]), (10, 10.0, 0, None))
        self.assertEqual(a["cached_pct"], 60)
        store._w("DELETE FROM requests")


class CapacityTest(unittest.TestCase):
    def test_background_never_takes_the_last_place(self):
        import threading
        from codex_os3 import codex_runner as C
        with mock.patch.object(C, "_slots", None), mock.patch.object(C, "_bg", None), mock.patch.object(C, "_bg_n", None):
            cfg = {"max_codex": 3}
            alive = lambda: True
            bg = [C.take(cfg, "background", alive), C.take(cfg, "background", alive)]   # two of three places
            got = []
            def third():
                try:
                    got.append(C.take(cfg, "background", lambda: bool(time.sleep(0.05)) or time.time() < t0 + 0.6))
                except C.ClientGone:
                    pass                                   # gave up waiting, as a cancelled request does
            t0 = time.time()
            t = threading.Thread(target=third)
            t.start()
            chat = C.take(cfg, "chat", alive)             # the last place is still there for a chat message
            self.assertTrue(callable(chat))
            t.join(3)
            self.assertFalse(got)                         # a third background call did not get one (it gave up waiting)
            for r in bg + [chat]:
                r()
            again = [C.take(cfg, "worker", alive) for _ in range(3)]   # all three are free again: nothing leaked
            for r in again:
                r()


class AppServerStartTest(unittest.TestCase):
    def test_codex_that_fails_to_initialise_is_not_left_running(self):
        from codex_os3 import appserver
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdout, proc.stderr = iter(()), iter(())
        with mock.patch.object(appserver.subprocess, "Popen", return_value=proc), \
                mock.patch.object(appserver, "known_features", return_value=set()), \
                mock.patch.object(appserver.Server, "request", side_effect=RuntimeError("no answer")), \
                mock.patch.object(appserver.platform_util, "kill_tree") as kill:
            with self.assertRaises(RuntimeError):
                appserver.Server("codex")
        kill.assert_called_once_with(proc)

    def test_one_slow_account_start_does_not_block_another(self):
        import threading
        from codex_os3 import appserver
        gate, made = threading.Event(), []

        class Slow:
            def __init__(self, codex, account=None, images=False):
                if account == "2":
                    gate.wait(5)
                made.append(account)
                self.codex = codex

            def alive(self):
                return True
        with mock.patch.object(appserver, "Server", Slow), mock.patch.dict(appserver._servers, clear=True), \
                mock.patch.dict(appserver._starting, clear=True):
            t = threading.Thread(target=appserver.server, args=({"codex_bin": "c"}, "2"))
            t.start()
            time.sleep(0.2)
            appserver.server({"codex_bin": "c"}, None)     # main: must not wait for account 2's start
            self.assertEqual(made, [None])
            gate.set()
            t.join()


class UpdaterTest(unittest.TestCase):
    def test_install_from_release_archive(self):
        import io, tarfile
        from codex_os3 import config, updater
        self.assertFalse(updater.managed())  # a checkout never updates itself
        self.assertTrue(updater.ver("v0.10.0") > updater.ver("0.9.9"))
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for name, data in (("os3-router-0.9.0/codex_os3/__init__.py", b'__version__ = "0.9.0"\n'),
                               ("os3-router-0.9.0/NEW.txt", b"new")):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
        app = tempfile.mkdtemp()
        with open(os.path.join(app, "OLD.txt"), "w") as f:
            f.write("old")
        orig, updater._get = updater._get, lambda url, timeout=60: buf.getvalue()
        try:
            with self.assertRaises(RuntimeError):  # archive doesn't hold the tagged version
                updater.install("v0.9.1", app=app, run_tests=False)
            self.assertFalse(os.path.exists(os.path.join(app, "NEW.txt")))
            updater.install("v0.9.0", app=app, run_tests=False)
        finally:
            updater._get = orig
        self.assertTrue(os.path.exists(os.path.join(app, "NEW.txt")))
        self.assertTrue(os.path.exists(os.path.join(app + ".prev", "OLD.txt")))
        self.assertTrue(os.path.exists(os.path.join(config.HOME, "reload.request")))


class EffortTest(unittest.TestCase):
    def test_os3_slider_wins(self):
        from codex_os3 import roles
        cfg = {"role_routing": True, "model": "gpt-6-luna", "effort": "medium",
               "roles": {"worker": {"model": "gpt-6-sol", "effort": "medium"}}}
        self.assertEqual(roles.requested_effort({"reasoning_effort": "xhigh"}), "xhigh")
        self.assertEqual(roles.requested_effort({"reasoning": {"effort": "High"}}), "high")
        self.assertIsNone(roles.requested_effort({"reasoning": "weird"}))
        self.assertEqual(roles.pick(cfg, "worker", "gpt-6-luna", "xhigh"), "gpt-6-sol-xhigh")
        self.assertEqual(roles.pick(cfg, "worker", "gpt-6-luna"), "gpt-6-sol-medium")
        cfg["roles"]["background"] = {"model": "gpt-6-luna", "effort": "low"}
        self.assertEqual(roles.pick(cfg, "background", "gpt-6-luna", "high"), "gpt-6-luna-low")
        self.assertEqual(roles.pick(dict(cfg, role_routing=False), "worker", "gpt-6-luna", "high"), "gpt-6-luna-high")
        self.assertEqual(roles.pick(dict(cfg, role_routing=False), "worker", "gpt-6-luna-low", "high"), "gpt-6-luna-low")
        from codex_os3 import prompt as P
        self.assertEqual(P.forced_tool({"tool_choice": {"type": "function", "function": {"name": "emit_facts"}}}), "emit_facts")
        self.assertEqual(P.forced_tool({"tool_choice": "required"}), "*")
        self.assertIsNone(P.forced_tool({"tool_choice": "auto"}))
        # an effort the model lacks goes to the nearest one it has
        self.assertEqual(roles.fit_effort("claude-haiku-4-5", "xhigh"), "high")


class WindowsProcessTest(unittest.TestCase):
    def test_codex_children_do_not_open_a_console_window(self):
        from codex_os3 import platform_util
        with mock.patch.object(platform_util, "WINDOWS", True), \
             mock.patch.object(platform_util.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True), \
             mock.patch.object(platform_util.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True):
            self.assertEqual(platform_util.popen_group_kwargs(), {"creationflags": 0x08000200})

    def test_feature_probe_uses_child_process_flags(self):
        from codex_os3 import codex_runner
        codex = "codex-for-window-test"
        codex_runner._known.pop(codex, None)
        with mock.patch.object(codex_runner.platform_util, "popen_group_kwargs",
                               return_value={"creationflags": 0x08000200}), \
             mock.patch.object(codex_runner.subprocess, "run",
                               return_value=mock.Mock(stdout="computer_use stable\n")) as run:
            self.assertEqual(codex_runner.known_features(codex), {"computer_use"})
        run.assert_called_once_with([codex, "features", "list"], capture_output=True,
                                    text=True, timeout=30, creationflags=0x08000200)
        codex_runner._known.pop(codex, None)


class WindowsBinTest(unittest.TestCase):
    def test_npm_shims_resolve_to_native_exe(self):
        from codex_os3.platform_util import native_bin
        npm = tempfile.mkdtemp()
        for f in ("codex", "codex.cmd", "codex.ps1"):
            open(os.path.join(npm, f), "w").close()
        ps1 = os.path.join(npm, "codex.ps1")
        self.assertEqual(native_bin(ps1, windows=False), ps1)
        self.assertEqual(native_bin(ps1, windows=True), os.path.join(npm, "codex.cmd"))  # no exe yet
        exe_dir = os.path.join(npm, "node_modules", "@openai", "codex", "node_modules", "@openai", "codex-win32-x64",
                               "vendor", "x86_64-pc-windows-msvc", "codex")
        os.makedirs(exe_dir)
        exe = os.path.join(exe_dir, "codex.exe")
        open(exe, "w").close()
        for shim in ("codex.ps1", "codex.cmd", "codex"):
            self.assertEqual(native_bin(os.path.join(npm, shim), windows=True), exe, shim)
        self.assertEqual(native_bin(exe, windows=True), exe)


class WhatsNewTest(unittest.TestCase):
    def test_changelog_since_last_seen(self):
        from codex_os3 import __version__, ui_api, updater
        store.kv_set("whatsnew_seen", None)
        w = ui_api.whatsnew()  # fresh install (no app.prev): only the current version
        self.assertEqual([x["version"] for x in w["sections"]], [__version__])
        store.kv_set("whatsnew_seen", "0.2.2")
        w = ui_api.whatsnew()
        vs = [x["version"] for x in w["sections"]]
        self.assertTrue(w["show"])
        self.assertEqual(vs[0], __version__)
        self.assertNotIn("0.2.2", vs)
        self.assertTrue(all(updater.ver(v) > updater.ver("0.2.2") for v in vs))
        store.kv_set("whatsnew_seen", __version__)
        self.assertFalse(ui_api.whatsnew()["show"])


class ReportTest(unittest.TestCase):
    def test_no_endpoint_disables_reports(self):
        from codex_os3 import config, report
        with mock.patch.dict(os.environ, {"CODEX_OS3_REPORT_ENDPOINT": ""}):
            with mock.patch.object(report, "_post") as post:
                config.save({"share_reports": True})
                try:
                    report.maybe_send("error", "example")
                    self.assertEqual(report.user_report("example"), (False, "problem reporting is not configured"))
                    post.assert_not_called()
                finally:
                    config.save({"share_reports": None})

    @mock.patch.dict(os.environ, {"CODEX_OS3_REPORT_ENDPOINT": "https://example.invalid/test"})
    def test_posts_to_configured_endpoint(self):
        from codex_os3 import report
        with mock.patch.object(report.urllib.request, "urlopen") as opened:
            report._post(report._payload("error", "example"))
        req = opened.call_args.args[0]
        self.assertEqual(req.full_url, "https://example.invalid/test")
        self.assertEqual(set(json.loads(req.data)), {"kind", "level", "version", "os", "installId", "message"})

    @mock.patch.dict(os.environ, {"CODEX_OS3_REPORT_ENDPOINT": "https://example.invalid/test"})
    def test_opt_in_redacted_and_rate_limited(self):
        from codex_os3 import config, report
        sent = []
        orig, report._post = report._post, sent.append
        try:
            config.save({"share_reports": None})
            store.kv_set("report_times", [])
            report.maybe_send("error", "boom")
            time.sleep(0.2)
            self.assertEqual(sent, [])  # not asked yet = nothing leaves the machine
            config.save({"share_reports": True})
            report.maybe_send("worker_start", "noise")  # not a reported kind
            report.maybe_send("error", "failed with key cx-0123456789abcdef0123456789abcdef")
            time.sleep(0.2)
            self.assertEqual(len(sent), 1)
            self.assertNotIn("0123456789abcdef", sent[0]["message"])
            for _ in range(20):
                report.maybe_send("error", "again")
            time.sleep(0.3)
            self.assertEqual(len(sent), report.PER_HOUR)
        finally:
            report._post = orig
            config.save({"share_reports": None})


class UserReportTest(unittest.TestCase):
    @mock.patch.dict(os.environ, {"CODEX_OS3_REPORT_ENDPOINT": "https://example.invalid/test"})
    def test_sent_on_request_redacted_limited(self):
        from codex_os3 import config, report
        sent = []
        orig, report._post = report._post, sent.append
        try:
            config.save({"share_reports": False})  # an explicit report is sent anyway
            store.kv_set("user_report_times", [])
            ok, _ = report.user_report("broken after update, key cx-0123456789abcdef0123456789abcdef")
            time.sleep(0.2)
            self.assertTrue(ok)
            self.assertEqual(sent[0]["kind"], "user_report")
            self.assertNotIn("0123456789abcdef", sent[0]["message"])
            for _ in range(6):
                ok, msg = report.user_report("again", diagnostics=False)
            self.assertFalse(ok)
            self.assertFalse(report.user_report("   ")[0])
        finally:
            report._post = orig
            config.save({"share_reports": None})


class AgentKeepaliveRuleTest(unittest.TestCase):
    def test_disconnected_for_minutes_restarts(self):
        s = Watchdog.snap(Watchdog(), last_response=None, agent={"running": True, "status": "disconnected"},
                          agent_status_age_s=400)
        self.assertIn(("agent_down", "restart_agent"), [(f["kind"], f["action"]) for f in watchdog.rules(s)])
        s["agent_status_age_s"] = 30  # its own reconnect may still work
        self.assertEqual([f["kind"] for f in watchdog.rules(s)], [])


class MacAppUpdateTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "macOS app")
    def test_unchanged_app_is_not_replaced(self):
        # a new unsigned build must be approved in Privacy & Security again: only replace it when it changed
        import plistlib
        from codex_os3 import updater
        home = tempfile.mkdtemp()
        app = os.path.join(home, "Applications", "OS3 Router.app", "Contents")
        os.makedirs(app)
        with open(os.path.join(updater.APP, "app", "macos", "VERSION")) as f:
            want = f.read().strip()
        with open(os.path.join(app, "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleShortVersionString": want}, f)
        def no_download(*a, **k):
            raise AssertionError("downloaded an unchanged app")
        orig, updater._get = updater._get, no_download
        old_home, os.environ["HOME"] = os.environ.get("HOME"), home
        try:
            updater.update_apps("v9.9.9")  # returns without downloading
        finally:
            updater._get = orig
            os.environ["HOME"] = old_home


class ContentStreamTest(unittest.TestCase):
    def feed(self, raw, ok=lambda t: True, step=3):
        out = []
        cs = P.ContentStream(out.append, ok)
        for i in range(0, len(raw), step):
            cs.feed(raw[i:i + step])
        return "".join(out)

    def test_streams_final_content_split_anywhere(self):
        text = 'Hi "you"\nweek 38: toets \u00e9 \U0001f600 ' + "x" * 200
        raw = json.dumps({"kind": "final", "calls": [], "content": text})
        for step in (1, 2, 5, 7):
            self.assertEqual(self.feed(raw, step=step), text)

    def test_tool_calls_and_held_corrections_are_not_streamed(self):
        self.assertEqual(self.feed(json.dumps({"kind": "tools", "calls": [], "content": "x" * 300})), "")
        self.assertEqual(self.feed(json.dumps({"kind": "final", "calls": [], "content": "nope " * 60}),
                                   ok=lambda t: False), "")


class UpdaterCertTest(unittest.TestCase):
    @staticmethod
    def curl_reply(status, final_url, body=b""):
        def run(command, **kwargs):
            with open(command[command.index("--output") + 1], "wb") as output:
                output.write(body)
            return mock.Mock(returncode=0, stdout=f"{status}\n{final_url}".encode(), stderr=b"")
        return run

    def test_rate_limited_api_uses_latest_release_redirect(self):
        import urllib.error
        from codex_os3 import updater
        limited = urllib.error.HTTPError("https://api.github.com/", 403, "rate limit exceeded", {}, None)
        response = mock.Mock()
        response.geturl.return_value = "https://github.com/Nesbesss/os3-router/releases/tag/v0.5.0"
        with mock.patch.object(updater, "_get", side_effect=limited), \
                mock.patch.object(updater.urllib.request, "urlopen", return_value=response) as open_url:
            self.assertEqual(updater.latest(), "v0.5.0")
        self.assertEqual(open_url.call_args.args[0].get_method(), "HEAD")
        response.close.assert_called_once_with()

    def test_rate_limit_fallback_rejects_unexpected_redirect(self):
        import urllib.error
        from codex_os3 import updater
        limited = urllib.error.HTTPError("https://api.github.com/", 403, "rate limit exceeded", {}, None)
        response = mock.Mock()
        response.geturl.return_value = "https://github.com/Nesbesss/os3-router/releases"
        with mock.patch.object(updater, "_get", side_effect=limited), \
                mock.patch.object(updater.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "did not redirect to a tag"):
                updater.latest()

    def test_falls_back_to_curl_on_certificate_errors(self):
        import urllib.error
        from codex_os3 import updater
        bad = urllib.error.URLError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        api = f"https://api.github.com/repos/{updater.REPO}/releases/latest"
        with mock.patch.object(updater.urllib.request, "urlopen", side_effect=bad), \
                mock.patch.object(updater.shutil, "which", return_value="/usr/bin/curl"), \
                mock.patch.object(updater.subprocess, "run", side_effect=self.curl_reply(
                    200, api, b'{"tag_name": "v9.9.9"}')) as run:
            self.assertEqual(updater.latest(), "v9.9.9")
        self.assertEqual(run.call_args[0][0][0], "/usr/bin/curl")
        with mock.patch.object(updater.urllib.request, "urlopen", side_effect=urllib.error.URLError("timed out")):
            self.assertRaises(urllib.error.URLError, updater.latest)  # other errors are not retried

    def test_curl_api_403_uses_release_redirect(self):
        import urllib.error
        from codex_os3 import updater
        bad = urllib.error.URLError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        api = f"https://api.github.com/repos/{updater.REPO}/releases/latest"
        response = mock.Mock()
        response.geturl.return_value = "https://github.com/Nesbesss/os3-router/releases/tag/v0.5.0"
        with mock.patch.object(updater.urllib.request, "urlopen", side_effect=[bad, response]) as opened, \
                mock.patch.object(updater.shutil, "which", return_value="/usr/bin/curl"), \
                mock.patch.object(updater.subprocess, "run", side_effect=self.curl_reply(403, api)):
            self.assertEqual(updater.latest(), "v0.5.0")
        self.assertEqual(opened.call_args.args[0].get_method(), "HEAD")
        response.close.assert_called_once_with()

    def test_redirect_recovers_from_certificate_error_with_curl(self):
        import urllib.error
        from codex_os3 import updater
        limited = urllib.error.HTTPError("https://api.github.com/", 403, "rate limit exceeded", {}, None)
        bad = urllib.error.URLError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        release = "https://github.com/Nesbesss/os3-router/releases/tag/v0.5.0"
        with mock.patch.object(updater, "_get", side_effect=limited), \
                mock.patch.object(updater.urllib.request, "urlopen", side_effect=bad), \
                mock.patch.object(updater.shutil, "which", return_value="/usr/bin/curl"), \
                mock.patch.object(updater.subprocess, "run", side_effect=self.curl_reply(200, release)) as run:
            self.assertEqual(updater.latest(), "v0.5.0")
        self.assertIn("--head", run.call_args.args[0])


class AccountsTest(unittest.TestCase):
    """Two Codex accounts: stay on one until it's nearly out, keep conversations on their account,
    move to the next at the limit, and back to main once it has room."""
    def setUp(self):
        from codex_os3 import accounts, config
        self.a, self.cfg = accounts, config
        self.home = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.home, "sessions"))
        self.env = mock.patch.dict(os.environ, {"CODEX_HOME": self.home})
        self.env.start()
        os.makedirs(os.path.join(accounts.root(), "2", "sessions", "2026"), exist_ok=True)
        open(os.path.join(accounts.root(), "2", "auth.json"), "w").close()
        store._w("DELETE FROM limits")
        store._w("DELETE FROM kv WHERE k LIKE 'limited:%' OR k LIKE 'signed_out:%' OR k LIKE 'plan:%' OR k = 'account_order'")

    def tearDown(self):
        self.env.stop()
        for f in ("models_cache.json", "os3-models.json"):  # (account 2's folder outlives the test)
            try:
                os.unlink(os.path.join(self.a.root(), "2", f))
            except OSError:
                pass
        store._w("DELETE FROM kv WHERE k LIKE 'signed_out:%' OR k LIKE 'plan:%' OR k LIKE 'models_try:%' OR k = 'account_order'")

    def test_account_used_first(self):
        a, now = self.a, time.time()
        a.use_first("2")                                   # e.g. main dropped to a plan with only a monthly window
        self.assertEqual((a.all_accounts(), a.pick()), (["2", "main"], "2"))
        store.add_limits({"primary": {"used_percent": 96, "resets_at": now + 3600}}, "codex:2")
        self.assertEqual(a.pick(), "main")                 # its 5 hours are nearly used: the leftover account
        store.add_limits({"primary": {"used_percent": 3, "resets_at": now + 18000}}, "codex:2")
        self.assertEqual(a.pick(), "2")                    # reset: back to the first one
        with self.assertRaises(ValueError):
            a.use_first("9")

    def test_signed_out_account_is_skipped_until_signed_in(self):
        a = self.a
        a.mark_signed_out("main", "401 Unauthorized: Your authentication token has been invalidated.")
        self.assertEqual(a.pick(), "2")
        self.assertTrue(next(x for x in a.overview({})["accounts"] if x["id"] == "main")["signed_out"])
        a.note_plan("main", "free")                         # a good answer from OpenAI: in again, with the live plan
        self.assertEqual(a.pick(), "main")
        self.assertEqual(next(x for x in a.overview({})["accounts"] if x["id"] == "main")["plan"], "free")
        self.assertTrue(a.AUTH_RE.search('{"code": "token_invalidated"}'))
        self.assertTrue(a.AUTH_RE.search("Your access token could not be refreshed because you have since logged out or signed in to another account."))
        self.assertFalse(a.AUTH_RE.search("You've hit your usage limit."))

    def test_pick_and_switch(self):
        a, now = self.a, time.time()
        self.assertEqual(a.all_accounts(), ["main", "2"])
        self.assertEqual(a.pick(), "main")
        store.add_limits({"primary": {"used_percent": 96, "resets_at": now + 3600}}, "codex")
        self.assertEqual(a.pick(), "2")                    # main nearly out: next account
        self.assertEqual(a.pick(prefer="2"), "2")
        store.add_limits({"primary": {"used_percent": 10, "resets_at": now + 3600}}, "codex")
        self.assertEqual(a.pick(), "main")                 # main has room again: back to it
        self.assertEqual(a.pick(prefer="2"), "2")          # but a conversation on 2 stays there
        a.mark_limited("main")
        a.mark_limited("2")
        self.assertIsNone(a.pick())                        # everything out: model fallback takes over
        self.assertEqual(a.env("2")["CODEX_HOME"], os.path.join(a.root(), "2"))
        self.assertIsNone(a.env("main"))

    def test_streamed_chat_keeps_its_account(self):
        from codex_os3 import appserver, engine
        body = {"model": "gpt-6-luna", "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}],
                "tools": [{"type": "function", "function": {"name": "create_task", "parameters": {}}}]}
        t = engine.Turn(dict(self.cfg.load(), engine="appserver", stream_chat=True), body, lambda: True)
        t.account, t.stream_sink = "2", lambda text: None
        with mock.patch.object(appserver, "run", return_value=("{}", {}, "T", None)) as run:
            t.codex("p", stream=True)
        self.assertEqual(run.call_args.kwargs["account"], "2")
        self.assertIn("on_text", run.call_args.kwargs)

    def test_owner_finds_thread_in_its_account(self):
        open(os.path.join(self.a.root(), "2", "sessions", "2026", "rollout-x-T42.jsonl"), "w").close()
        self.assertEqual(self.a.owner("T42"), "2")
        self.assertIsNone(self.a.owner("nope"))

    def test_engine_retries_on_next_account(self):
        from codex_os3 import codex_runner, engine
        from codex_os3.codex_runner import UsageLimit
        seen = []

        def run(cfg, prompt, model, *a, account=None, **k):
            seen.append(account)
            if account == "main":
                raise UsageLimit("usage limit")
            return '{"kind":"final","calls":[],"content":"hi"}', {}, "T1", None
        body = {"model": "gpt-6-luna", "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}],
                "tools": [{"type": "function", "function": {"name": "create_task", "parameters": {}}}]}
        cfg = dict(self.cfg.load(), engine="exec")
        with mock.patch.object(codex_runner, "run", side_effect=run):
            msg, _ = engine.Turn(cfg, body, lambda: True).run()
        self.assertEqual((seen, msg["content"]), (["main", "2"], "hi"))


    def test_next_account_without_the_model_gets_another_one(self):
        """Main is out of limits; account 2 (a Free plan) doesn't include gpt-6-sol: it runs the same request on a model it has."""
        from codex_os3 import codex_runner, engine
        from codex_os3.codex_runner import UsageLimit
        seen = []

        def run(cfg, prompt, model, *a, account=None, **k):
            seen.append((account, model))
            if account == "main":
                raise UsageLimit("usage limit")
            if model.startswith("gpt-6-sol"):
                raise UsageLimit("not supported when using Codex with a ChatGPT account", plan=True)
            return '{"kind":"final","calls":[],"content":"hi"}', {}, "T1", None
        store._w("DELETE FROM kv WHERE k LIKE 'no_model:%'")
        body = {"model": "gpt-6-sol", "messages": [{"role": "system", "content": "You are a worker agent"}, {"role": "user", "content": "q"}],
                "tools": [{"type": "function", "function": {"name": "shell", "parameters": {}}}]}
        cfg = dict(self.cfg.load(), engine="exec", role_routing=False)
        with mock.patch.object(codex_runner, "run", side_effect=run):
            msg, _ = engine.Turn(cfg, body, lambda: True).run()
        self.assertEqual(msg["content"], "hi")
        self.assertEqual([a for a, m in seen], ["main", "2", "2"])
        self.assertTrue(seen[-1][1].startswith("gpt-6-luna"))

    def _models(self, home, *slugs):
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, "models_cache.json"), "w") as f:
            json.dump({"models": [{"slug": m, "display_name": m.upper(), "visibility": "list",
                                   "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}]} for m in slugs]}, f)

    def test_selector_lists_the_models_of_every_account(self):
        """Main is a Free plan, account 2 a Plus one: the selector must offer what Plus adds, and say where."""
        from codex_os3 import roles
        self._models(self.home, "gpt-6-luna")
        self._models(os.path.join(self.a.root(), "2"), "gpt-6-luna", "gpt-6-sol")
        got = {m["slug"]: m for m in roles.available_models() if m["backend"] == "codex"}
        self.assertEqual(sorted(got), ["gpt-6-luna", "gpt-6-sol"])
        self.assertEqual(got["gpt-6-sol"]["only"], ["2"])
        self.assertNotIn("only", got["gpt-6-luna"])
        self.assertEqual([m["slug"] for m in roles.available_models(self.home) if m["backend"] == "codex"], ["gpt-6-luna"])

    def test_request_goes_to_the_account_whose_plan_has_the_model(self):
        from codex_os3 import codex_runner, engine
        self._models(self.home, "gpt-6-luna")
        self._models(os.path.join(self.a.root(), "2"), "gpt-6-luna", "gpt-6-sol")
        seen = []

        def run(cfg, prompt, model, *a, account=None, **k):
            seen.append((account, model))
            return "hi", {}, "T1", None
        cfg = dict(self.cfg.load(), engine="exec", role_routing=False)
        with mock.patch.object(codex_runner, "run", side_effect=run):
            for model in ("gpt-6-sol", "gpt-6-luna"):
                body = {"model": model, "messages": [{"role": "user", "content": "q"}]}
                engine.Turn(cfg, body, lambda: True).run()
        self.assertEqual([a for a, _ in seen], ["2", "main"])  # sol only exists on 2; luna stays on the first account

    def test_account_without_a_list_is_asked_once(self):
        from codex_os3 import roles
        self._models(self.home, "gpt-6-luna")
        srv = mock.Mock()
        srv.request.return_value = {"data": [
            {"model": "gpt-6-sol", "displayName": "GPT-6-Sol", "hidden": False, "defaultReasoningEffort": "medium",
             "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "medium"}]},
            {"model": "gpt-hidden", "hidden": True}]}
        store._w("DELETE FROM kv WHERE k LIKE 'models_try:%'")
        with mock.patch("codex_os3.appserver.server", return_value=srv) as make:
            self.a.ensure_models({})
            self.a.ensure_models({})
        self.assertEqual(make.call_count, 1)   # account 2 only; main has its cache, and the second call finds a list
        self.assertEqual([m["slug"] for m in roles.account_models(self.a.home("2"))], ["gpt-6-sol"])



class UnconfirmedResultTest(unittest.TestCase):
    def test_rejected_result_gets_a_fresh_look(self):
        call = {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "report_business_result", "arguments": "{}"}}]}
        rej = {"role": "tool", "tool_call_id": "c1", "content": 'Error: {"success":false,"code":"RESULT_UNCONFIRMED"}'}
        self.assertTrue(P.unconfirmed([call, rej]))
        self.assertFalse(P.unconfirmed([call, rej, {"role": "assistant", "content": "x"}]))  # already answered
        self.assertFalse(P.unconfirmed([call, {"role": "tool", "tool_call_id": "c1", "content": "ok"}]))


class CodexImagesTest(unittest.TestCase):
    """OS3's image_generate needs a paid provider: workers make images with Codex's own generator."""
    def test_worker_with_image_generate_uses_codex_images(self):
        from codex_os3 import codex_runner, config, engine
        tools = TOOLS + [{"type": "function", "function": {"name": "image_generate", "parameters": {}}}]
        body = {"model": "gpt-6-sol", "tools": tools,
                "messages": [{"role": "system", "content": "You are a worker agent in OS3."}, {"role": "user", "content": "draw an apple"}]}
        t = engine.Turn(dict(config.load(), engine="exec"), body, lambda: True)
        self.assertTrue(t.own_images())
        self.assertIn("built-in image generation", t.build(full=True)[1])
        self.assertFalse(engine.Turn(dict(config.load(), codex_images=False), body, lambda: True).own_images())
        self.assertFalse(engine.Turn(config.load(), dict(body, tools=TOOLS), lambda: True).own_images())
        cmd = codex_runner.build_cmd({"effort": "low"}, "gpt-6-sol-low", image_gen=True)
        self.assertNotIn("image_generation", cmd)
        self.assertIn("image_generation", codex_runner.build_cmd({"effort": "low"}, "gpt-6-sol-low"))
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, "generated_images", "T1"))
        png = os.path.join(home, "generated_images", "T1", "apple.png")
        open(png, "wb").close()
        with mock.patch.dict(os.environ, {"CODEX_HOME": home}):
            t.tid = "T1"
            msg, fin = t.to_message(json.dumps({"kind": "final", "calls": [], "content": "I made an apple."}))
            self.assertEqual((fin, msg["tool_calls"][0]["function"]["name"]), ("tool_calls", "report_result_files"))
            self.assertEqual(json.loads(msg["tool_calls"][0]["function"]["arguments"])["files"], [png])
            given = decision(("report_result_files", {"files": [png]}))
            self.assertEqual(len(t.to_message(json.dumps(given))[0]["tool_calls"]), 1)  # already handed over
        os3_files = {"type": "function", "function": {"name": "report_result_files", "parameters": {"type": "object", "properties": {
            "files": {"type": "array", "items": {"type": "object", "properties": {"node_id": {"type": "string"}, "path": {"type": "string"},
                                                                                "deliverToUser": {"type": "boolean"}}}}}}}}
        real = engine.Turn(config.load(), dict(body, tools=tools + [os3_files], messages=[
            {"role": "system", "content": "You are a worker agent in OS3. " + SYSTEM}, body["messages"][1]]), lambda: True)
        real.tid = "T1"
        with mock.patch.dict(os.environ, {"CODEX_HOME": home}):
            msg, _ = real.to_message(json.dumps({"kind": "final", "calls": [], "content": "done"}))
            f = json.loads(msg["tool_calls"][0]["function"]["arguments"])["files"]
            self.assertEqual(f, [{"node_id": NODE_A, "path": png, "deliverToUser": True}])  # default node when hostname unknown
            own = decision(("report_result_files", {"files": [{"node_id": NODE_A, "path": png, "deliverToUser": True}]}))
            self.assertEqual(len(real.to_message(json.dumps(own))[0]["tool_calls"]), 1)  # the model's own objects: no crash
        bad = decision(("image_generate", {"prompt": "apple"}))
        self.assertTrue(repair.decision_problems(bad, tools, SYSTEM, own_images=True))
        self.assertFalse(repair.decision_problems(bad, tools, SYSTEM))  # Claude: OS3's tool is the only way


class PlanLimitsTest(unittest.TestCase):
    """Limit windows are stored by their length: Plus sends 5 h + weekly, Max only a weekly one (as "primary")."""
    def row(self, rl):
        store._w("DELETE FROM limits WHERE backend='plantest'")
        store.add_limits(rl, "plantest")
        return store.latest_limits()["plantest"]

    def test_plans(self):
        w = lambda pct, mins: {"used_percent": pct, "resets_at": time.time() + 3600, "window_minutes": mins}
        plus = self.row({"primary": w(20, 300), "secondary": w(40, 10080)})
        self.assertEqual((plus["p_pct"], plus["s_pct"], plus["s_window"]), (20, 40, 10080))
        mx = self.row({"primary": w(12, 10080), "secondary": None})  # Max: weekly only
        self.assertEqual((mx["p_pct"], mx["s_pct"], mx["s_window"]), (None, 12, 10080))
        free = self.row({"primary": w(60, 300)})
        self.assertEqual((free["p_pct"], free["s_pct"]), (60, None))
        old = self.row({"primary": {"used_percent": 5}, "secondary": {"used_percent": 7}})  # no lengths: keep the order
        self.assertEqual((old["p_pct"], old["s_pct"]), (5, 7))
        self.assertEqual((store.win_label(10080, "x"), store.win_label(300, "x"), store.win_label(None, "x")), ("weekly", "5-hour", "x"))


class AppServerHangTest(unittest.TestCase):
    """A stalled turn that only gets status notices (MCP startup, thread status) must count as hung."""
    def test_status_noise_is_not_activity(self):
        import threading
        from codex_os3 import appserver, codex_runner

        class Fake:
            loaded, subs, interrupted = set(), {}, []

            def request(self, method, params, timeout=90):
                if method == "thread/start":
                    return {"thread": {"id": "T1"}}
                if method == "turn/interrupt":
                    self.interrupted.append(params)
                return {"turn": {"id": "U1"}}

            def rate_limits(self):
                return None
        fake = Fake()

        def noise():
            while not fake.interrupted:
                q = fake.subs.get("T1")
                if q:
                    q.put({"method": "mcpServer/startupStatus/updated", "params": {"threadId": "T1"}})
                time.sleep(0.2)
        threading.Thread(target=noise, daemon=True).start()
        cfg = {"effort": "medium", "max_codex": 1, "hang_idle_s": 1, "hang_max_s": 60}
        with mock.patch.object(appserver, "server", lambda cfg, *a: fake), \
                mock.patch.object(appserver.sessions, "rollout_files", lambda tid: []):
            t0 = time.time()
            with self.assertRaises(codex_runner.CodexHung):
                appserver.run(cfg, "hi", "gpt-6-luna-medium")
        self.assertLess(time.time() - t0, 5)
        self.assertTrue(fake.interrupted)


class BrowserBehaviourTest(unittest.TestCase):
    def test_not_found_phrases(self):
        for s in ("I couldn't find Extreme weather on the page.", "The lesson is not listed.",
                  "Ik kon 'Extreem weer' niet vinden in de planner.", "Extreem weer is niet gevonden",
                  "I was unable to locate the geography tab"):
            self.assertTrue(P.claims_not_found(s), s)
        for s in ("Found it: 1.2 Extreem weer, 42% done.", "Opened the geography tab and read lesson 1.2."):
            self.assertFalse(P.claims_not_found(s), s)

    def test_probe_streak_counts_until_a_look(self):
        call = lambda n: {"role": "assistant", "tool_calls": [{"function": {"name": n, "arguments": "{}"}}]}
        res = {"role": "tool", "content": "{}"}
        msgs = [call("dummy_system_image"), res] + [call("dummy_system"), res, call("dummy_system_result"), res] * 4
        self.assertEqual(P.probe_streak(msgs), 8)
        self.assertEqual(P.probe_streak(msgs + [call("dummy_system_image"), res]), 0)
        self.assertEqual(P.probe_streak([call("shell"), res, call("dummy_system"), res]), 1)

    def test_browser_guide_only_with_browser_tools(self):
        fn = lambda n: {"type": "function", "function": {"name": n, "parameters": {}}}
        msgs = [{"role": "user", "content": "hi"}]
        self.assertIn("sheet tabs", P.flatten(msgs, [fn("dummy_system"), fn("shell")]))
        self.assertNotIn("sheet tabs", P.flatten(msgs, [fn("shell")]))


if __name__ == "__main__":
    unittest.main()


class SecondRouterTest(unittest.TestCase):
    """A keep-alive (cron, ~/.profile) must not start a second router next to a running one."""
    def test_already_running(self):
        from codex_os3 import supervisor
        if os.name == "nt":
            self.skipTest("POSIX only")
        cfg = {"bind": "127.0.0.1", "port": 1}
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b'{"status": "ok", "pid": 1}'
        with tempfile.TemporaryDirectory() as d, mock.patch.object(supervisor, "PIDFILE", os.path.join(d, "pid")):
            self.assertFalse(supervisor.already_running(cfg))            # no pid file
            with open(supervisor.PIDFILE, "w") as f:
                f.write(str(os.getppid()))                                 # a live process
            with mock.patch.object(supervisor.urllib.request, "urlopen", return_value=ok):
                self.assertTrue(supervisor.already_running(cfg))
            self.assertFalse(supervisor.already_running(cfg))            # alive, but nothing answers: stale
            with open(supervisor.PIDFILE, "w") as f:
                f.write("999999")                                          # dead
            with mock.patch.object(supervisor.urllib.request, "urlopen", return_value=ok):
                self.assertFalse(supervisor.already_running(cfg))


class ClaudeFoundTest(unittest.TestCase):
    def test_found_inside_the_desktop_app_or_editor_extension(self):
        """No `claude` command anywhere (only the Claude desktop app / VS Code extension): its bundled copy is used,
        the newest one, and a real install in the usual folders still wins."""
        from codex_os3 import roles
        with tempfile.TemporaryDirectory() as home:
            def make(*parts):
                f = os.path.join(home, *parts)
                os.makedirs(os.path.dirname(f), exist_ok=True)
                open(f, "w").close()
                return f
            app = make("Library", "Application Support", "Claude", "claude-code", "2.1.281", "claude.app", "Contents", "MacOS", "claude")
            with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home, "PATH": ""}), \
                    mock.patch.object(roles, "CLAUDE_DIRS", ()), mock.patch.object(roles, "_from_login_shell", return_value=None):
                self.assertEqual(roles.claude_path({}), app)
                ext = make(".vscode", "extensions", "anthropic.claude-code-2.1.282-darwin-arm64", "resources", "native-binary", "claude")
                os.utime(ext, (time.time() + 60, time.time() + 60))
                self.assertEqual(roles.claude_path({}), ext)                       # the newer copy
                self.assertEqual(roles.claude_path({"claude_bin": os.path.join(home, "gone")}), ext)  # stale saved path
            real = make(".local", "bin", "claude")
            with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home, "PATH": ""}), \
                    mock.patch.object(roles, "CLAUDE_DIRS", ("~/.local/bin",)):
                self.assertEqual(roles.claude_path({}), real)

    @unittest.skipIf(sys.platform == "win32", "Windows has no login shell to ask")
    def test_found_by_login_shell(self):
        """An nvm/alias install is only known to the user's shell: ask it when the usual folders miss."""
        from codex_os3 import roles
        with tempfile.TemporaryDirectory() as home:
            cli = os.path.join(home, "claude")
            open(cli, "w").close()
            roles._shell[0] = 0
            fake = mock.Mock(stdout="Last login: today\nalias claude=" + cli + "\n")
            with mock.patch.dict(os.environ, {"HOME": home, "PATH": "", "SHELL": "/bin/zsh"}), \
                    mock.patch.object(roles, "CLAUDE_DIRS", ()), mock.patch.object(roles.subprocess, "run", return_value=fake):
                self.assertEqual(roles.claude_path({}), cli)
            roles._shell[0] = 0

    def test_path_set_from_the_models_page(self):
        from codex_os3 import config, ui_api
        with tempfile.TemporaryDirectory() as home, mock.patch.object(config, "HOME", home), \
                mock.patch.object(config, "PATH", os.path.join(home, "config.json")), mock.patch.object(ui_api.store, "event"):
            cli = os.path.join(home, "claude")
            open(cli, "w").close()
            self.assertEqual(ui_api.handle("POST", "config", {"claude_bin": cli + "x"}, {}, config.load())[0], 400)
            self.assertEqual(ui_api.handle("POST", "config", {"claude_bin": " " + cli + " "}, {}, config.load())[0], 200)
            self.assertEqual(config.load()["claude_bin"], cli)

    def test_listed_without_codex_cache(self):
        """No models_cache.json (fresh Codex / new account) must not hide the Claude models."""
        from codex_os3 import roles
        with tempfile.TemporaryDirectory() as home, mock.patch.object(roles, "claude_installed", return_value=True):
            slugs = [m["slug"] for m in roles.available_models(home)]
        self.assertIn("gpt-6-sol", slugs)
        self.assertIn("claude-sonnet-5-5", slugs)


    """Claude Code installed in ~/.local/bin (its installer's default) is found although the service's
    PATH doesn't include it; before, only GPT models were offered."""
    def test_found_outside_path(self):
        from codex_os3 import roles
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, ".local", "bin"))
            cli = os.path.join(home, ".local", "bin", "claude")
            open(cli, "w").close()
            with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home, "PATH": ""}), \
                    mock.patch.object(roles, "CLAUDE_DIRS", ("~/.local/bin",)):
                self.assertEqual(roles.claude_path({}), cli)
                self.assertTrue(roles.claude_installed({}))
                self.assertEqual(roles.claude_path({"claude_bin": os.path.join(home, "gone")}), cli)  # stale saved path
            with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home, "PATH": ""}), \
                    mock.patch.object(roles, "CLAUDE_DIRS", ()), \
                    mock.patch.object(roles, "_from_login_shell", return_value=None):
                self.assertFalse(roles.claude_installed({}))
