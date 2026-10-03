"""Real HTTP regression checks with an isolated router home and no model calls."""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest


class HTTPTransport(unittest.TestCase):
    def test_stream_opens_before_model_finishes_and_records_delivery(self):
        self.run_case("opening")

    def test_incomplete_upload_is_closed_with_408(self):
        self.run_case("upload")

    def test_lost_response_is_not_recorded_as_success(self):
        self.run_case("disconnect")

    def test_stream_write_failure_cancels_without_retry(self):
        self.run_case("midstream")

    def test_committed_text_finishes_without_another_model_call(self):
        self.run_case("committed")

    def run_case(self, case):
        # Fresh imports are essential: the parent's test launcher might already have
        # cached a config pointing at a different home.
        with tempfile.TemporaryDirectory(prefix="os3-http-test-") as home:
            env = dict(os.environ, HOME=home, USERPROFILE=home, CODEX_OS3_HOME=home,
                       CODEX_HOME=os.path.join(home, "codex"))
            try:
                result = subprocess.run([sys.executable, "-c", SCRIPT, case], env=env,
                                        capture_output=True, text=True, timeout=25)
            except subprocess.TimeoutExpired as error:
                self.fail(f"HTTP case {case} stalled: {error.stderr!r}")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


SCRIPT = textwrap.dedent('''
    import faulthandler, http.client, json, socket, struct, sys, threading, time
    faulthandler.dump_traceback_later(10)
    from unittest import mock
    from codex_os3 import config, server, store

    case = sys.argv[1]
    cfg = dict(config.load(), api_key="transport-test", engine="appserver",
               stream_chat=case in ("midstream", "committed"))
    entered, release = threading.Event(), threading.Event()
    real_turn = server.engine.Turn
    model_calls = []
    committed_content = "The initial read-only checks completed. " * 8 + "I don't have a shell tool available."
    def streamed_model(*args, **kwargs):
        model_calls.append(True)
        entered.set()
        raw = json.dumps({"kind": "final", "content": committed_content, "calls": []})
        feed = kwargs.get("on_text", lambda x: None)
        for i in range(0, len(raw), 37):
            feed(raw[i:i + 37])
        return raw, {}, None, None
    class DelayedTurn:
        streamed = ""
        requested = "fake"
        task = "transport-test"
        def __init__(self, *args, **kwargs):
            self.rid = store.request_start(self.task, "test", "fake", True, 0, 1, 10)
        def run(self):
            entered.set()
            assert release.wait(8)
            if case == "midstream":
                self.stream_sink("answer while model is running")
            store.request_end(self.rid, status="ok")
            return {"role": "assistant", "content": "real completed answer"}, "stop"

    with mock.patch.object(server.config, "load", return_value=cfg), \\
         mock.patch.object(real_turn, "plan_model", return_value=None), \\
         mock.patch.object(server.engine.accounts, "pick", return_value="main"), \\
         mock.patch.object(server.engine.accounts, "all_accounts", return_value=["main"]), \\
         mock.patch.object(server.engine.appserver, "run", side_effect=streamed_model), \\
         mock.patch.object(server.engine, "Turn", real_turn if case == "committed" else DelayedTurn), \\
         mock.patch.object(server, "HTTP_IO_TIMEOUT_S", 0.5), \\
         mock.patch.object(socket, "getfqdn", return_value="localhost"):
        # HTTPServer resolves its display name during bind. Transport tests must
        # not depend on the runner's external DNS configuration.
        srv = server.Server(("127.0.0.1", 0))
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        port = srv.server_address[1]
        try:
            if case == "upload":
                client = socket.create_connection(("127.0.0.1", port), timeout=3)
                client.sendall(b"POST /v1/chat/completions HTTP/1.1\\r\\nHost: localhost\\r\\n"
                    b"Authorization: Bearer transport-test\\r\\nContent-Length: 100\\r\\n\\r\\n{")
                response = http.client.HTTPResponse(client)
                response.begin()
                assert response.status == 408, response.status
                assert "stalled" in json.loads(response.read())["error"]["message"]
                assert client.recv(1) == b""
                client.close()
            else:
                client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                body = {"stream": True}
                if case == "committed":
                    body.update(model="gpt-6-luna", messages=[{"role": "user", "content": "Check access"}],
                        tools=[{"type": "function", "function": {"name": "shell", "parameters": {}}}])
                client.request("POST", "/v1/chat/completions", json.dumps(body),
                    {"Authorization": "Bearer transport-test", "Content-Type": "application/json"})
                response = client.getresponse()
                opening = response.readline().decode()
                delta = json.loads(opening.removeprefix("data: "))["choices"][0]["delta"]
                assert delta == {"role": "assistant", "content": ""}, delta
                assert entered.wait(2)
                assert not release.is_set()
                if case in ("disconnect", "midstream"):
                    sock = response.fp.raw._sock
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                        struct.pack("hh" if sys.platform == "win32" else "ii", 1, 0))
                    response.close()
                    client.close()
                release.set()
                if case in ("opening", "committed"):
                    remaining = response.read().decode()
                    if case == "committed":
                        chunks = [json.loads(line[6:]) for line in remaining.splitlines()
                            if line.startswith("data: ") and line != "data: [DONE]"]
                        assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks) == committed_content
                        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
                        assert len(model_calls) == 1, model_calls
                    else:
                        assert "real completed answer" in remaining
                    assert remaining.rstrip().endswith("data: [DONE]")
                deadline = time.monotonic() + 3
                kind = "response_sent" if case in ("opening", "committed") else "response_delivery_failed"
                while time.monotonic() < deadline:
                    events = store.q("SELECT kind FROM events WHERE kind=?", (kind,))
                    if events:
                        break
                    time.sleep(0.01)
                assert events, kind
                status = store.q("SELECT status FROM requests ORDER BY id DESC LIMIT 1")[0]["status"]
                assert status == ("ok" if case in ("opening", "committed") else "gone"), status
                response.close()
                client.close()
        finally:
            release.set()
            srv.shutdown()
            srv.server_close()
            thread.join(3)
            faulthandler.cancel_dump_traceback_later()
''')


if __name__ == "__main__":
    unittest.main()
