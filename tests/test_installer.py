"""The pieces the installers rely on: a usable port, the Claude lookup, the running service's view, and the
loopback-Host rule that keeps a web page from reaching the local dashboard (DNS rebinding)."""
import os, socket, sys, tempfile, unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from codex_os3 import preflight, server  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HostRuleTest(unittest.TestCase):
    def local(self, addr, host):
        return server.Handler.local(SimpleNamespace(client_address=(addr, 5), headers={"Host": host}))

    def test_only_loopback_names_from_this_machine(self):
        for h in ("localhost:11435", "127.0.0.1:11435", "127.1:11435", "[::1]:11435", "app.localhost", ""):
            self.assertTrue(self.local("127.0.0.1", h), h)
        for h in ("evil.example:11435", "127.0.0.1.evil.example", "0.0.0.0:11435", "192.168.1.5:11435"):
            self.assertFalse(self.local("127.0.0.1", h), h)      # rebinding: arrives from 127.0.0.1 with its own name
        self.assertFalse(self.local("192.168.1.9", "localhost"))  # another machine can't claim to be local


class PortTest(unittest.TestCase):
    def test_taken_port_is_skipped(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            taken = s.getsockname()[1]
            with mock.patch.object(preflight, "is_ours", return_value=False):
                got = preflight.choose_port({"port": taken, "bind": "127.0.0.1"})
        self.assertNotEqual(got, taken)
        self.assertTrue(got > taken)

    def test_our_own_router_keeps_its_port(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            taken = s.getsockname()[1]
            with mock.patch.object(preflight, "is_ours", return_value=True):
                self.assertEqual(preflight.choose_port({"port": taken, "bind": "127.0.0.1"}), taken)

    def test_asked_for_port_is_not_moved(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            taken = s.getsockname()[1]
            with mock.patch.object(preflight, "is_ours", return_value=False):
                self.assertIsNone(preflight.choose_port({"port": 1, "bind": "127.0.0.1"}, wanted=taken, span=1))


class VerifyTest(unittest.TestCase):
    CFG = {"port": 1}

    def run_checks(self, models, claude):
        def call(cfg, path, **k):
            return models if path == "models" else [{"check": "Codex CLI installed", "ok": True, "detail": "/x/codex"},
                                                    {"check": "rabbit-agent connected", "ok": False, "detail": "no"}]
        with tempfile.TemporaryDirectory() as d, mock.patch.object(preflight, "claude_found", return_value=claude and os.path.join(d, "claude")):
            return preflight.verify_checks(self.CFG, call=call)

    def test_service_that_cannot_see_claude_is_reported(self):
        out = self.run_checks([{"backend": "codex"}], claude=True)
        self.assertEqual([ok for ok, _ in out], [True, False])           # rabbit-agent check is not asked of a fresh install
        self.assertIn("doesn't list Claude", out[1][1])

    def test_all_good(self):
        self.assertTrue(all(ok for ok, _ in self.run_checks([{"backend": "codex"}, {"backend": "claude"}], claude=True)))
        self.assertTrue(all(ok for ok, _ in self.run_checks([{"backend": "codex"}], claude=False)))

    def test_service_down(self):
        def call(*a, **k):
            raise OSError("refused")
        out = preflight.verify_checks(self.CFG, call=call)
        self.assertFalse(out[0][0])


class VerifyWaitsForTheSwapTest(unittest.TestCase):
    def test_a_failure_right_after_an_update_is_looked_at_again(self):
        answers = [[(False, "Codex logged in: Error loading configuration")], [(False, "still")], [(True, "Codex logged in")]]
        with mock.patch.object(preflight, "verify_checks", side_effect=lambda *a, **k: answers.pop(0)) as vc:
            self.assertEqual(preflight.verify([], sleep=lambda s: None), 0)
        self.assertEqual(vc.call_count, 3)

    def test_a_real_problem_is_still_reported(self):
        with mock.patch.object(preflight, "verify_checks", return_value=[(False, "Codex is not signed in")]) as vc:
            self.assertEqual(preflight.verify([], sleep=lambda s: None), 1)
        self.assertEqual(vc.call_count, 4)                         # once, and three more looks


class StatusChecksRunFromAFolderThatExists(unittest.TestCase):
    def test_cwd_is_the_router_home(self):
        """Codex started from a deleted folder says 'Error loading configuration: No such file or directory' and
        looks signed out: happened after an installer replaced the app folder under the running router."""
        from codex_os3 import config, ui_api
        seen = []

        def run(cmd, **kw):
            seen.append(kw.get("cwd"))
            return SimpleNamespace(stdout="codex-cli 9.9.9", stderr="", returncode=0)
        with mock.patch.object(ui_api.subprocess, "run", side_effect=run), mock.patch.object(ui_api.shutil, "which", return_value="/x/codex"), \
                mock.patch.object(ui_api.roles, "claude_path", return_value="/x/claude"):
            ui_api.codex_info({"codex_bin": "/x/codex"}, fresh=True)
            ui_api.claude_info({"claude_bin": "/x/claude"}, fresh=True)
        self.assertEqual(set(seen), {config.HOME})
        self.assertEqual(len(seen), 3)


if __name__ == "__main__":
    unittest.main()
