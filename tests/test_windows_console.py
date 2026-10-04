"""Windows: the router runs under pythonw.exe, so a console child started without CREATE_NO_WINDOW opens its
own window. The open app/dashboard polls /api/onboarding every 10 s and codex_info is cached for 20 s, so
`codex --version` + `codex login status` flashed two empty windows about every 30 s and stole keystrokes."""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("CODEX_OS3_HOME", tempfile.mkdtemp(prefix="cxos3-test-"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codex_os3 import platform_util, roles, ui_api  # noqa: E402

NO_WINDOW = 0x08000000


class Done:
    def __init__(self, stdout):
        self.stdout, self.stderr, self.returncode = stdout, "", 0


class StatusChecksOpenNoWindow(unittest.TestCase):
    def run_on_windows(self, fn, cfg, stdout):
        calls = []

        def fake_run(cmd, **kw):
            calls.append((cmd, kw))
            return Done(stdout)
        with mock.patch.object(platform_util, "WINDOWS", True), \
             mock.patch.object(platform_util.subprocess, "CREATE_NO_WINDOW", NO_WINDOW, create=True), \
             mock.patch.object(ui_api.subprocess, "run", fake_run):
            fn(cfg, fresh=True)
        return calls

    def test_codex_checks_have_no_console_window(self):
        with mock.patch.object(platform_util, "codex_path", return_value="codex.exe"):
            calls = self.run_on_windows(ui_api.codex_info, {"codex_bin": "codex.exe"}, "codex-cli 0.160.0")
        self.assertEqual([c[0][1:] for c in calls], [["--version"], ["login", "status"]])
        for cmd, kw in calls:
            self.assertTrue(kw.get("creationflags", 0) & NO_WINDOW, cmd)

    def test_claude_check_has_no_console_window(self):
        # Discovery can start a login shell on macOS when Claude is absent.
        # Target the auth subprocess independently of installed CLIs.
        with mock.patch.object(roles, "claude_path", return_value=r"C:\npm\claude.exe"):
            calls = self.run_on_windows(ui_api.claude_info, {"claude_bin": r"C:\npm\claude.exe"}, '{"loggedIn": true}')
        self.assertEqual([c[0] for c in calls], [[r"C:\npm\claude.exe", "auth", "status"]])
        self.assertTrue(calls[0][1].get("creationflags", 0) & NO_WINDOW)

    def test_no_window_kwargs_is_empty_elsewhere(self):
        with mock.patch.object(platform_util, "WINDOWS", False):
            self.assertEqual(platform_util.no_window_kwargs(), {})


if __name__ == "__main__":
    unittest.main()
