"""A launcher that preloads live config must never let fixtures run against it."""
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


class IsolationTest(unittest.TestCase):
    def test_preloaded_config_refuses_tests_and_keeps_login(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as scratch:
            home = pathlib.Path(scratch) / "live-router"
            auth = home / "accounts" / "2" / "auth.json"
            auth.parent.mkdir(parents=True)
            auth.write_text('{"sentinel":"saved login"}')
            env = dict(os.environ, CODEX_OS3_HOME=str(home))
            script = "from codex_os3 import config; import sys; sys.path.insert(0, 'tests'); import test_core"
            result = subprocess.run([sys.executable, "-c", script], cwd=root, env=env,
                                    capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("isolated CODEX_OS3_HOME", result.stderr)
            self.assertEqual(auth.read_text(), '{"sentinel":"saved login"}')
            self.assertFalse((home / "state.db").exists())
