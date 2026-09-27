"""Regression tests for the opt-in idle-sleep setting."""
import os
import tempfile
import unittest
from unittest import mock

from codex_os3 import config, sleep_control, ui_api


class IdleSleepControl(unittest.TestCase):
    @mock.patch.object(sleep_control.subprocess, "Popen")
    def test_mac_assertion_follows_setting_and_survives_worker_reload(self, popen):
        proc = popen.return_value
        proc.poll.return_value = None
        guard = sleep_control.IdleSleepInhibitor(platform="darwin")
        guard.sync(False)
        popen.assert_not_called()
        guard.sync(True)
        self.assertEqual(popen.call_args.args[0],
                         ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])
        guard.sync(True)
        self.assertEqual(popen.call_count, 1)
        guard.close()
        proc.terminate.assert_called_once()
        self.assertIsNone(guard.process)

    @mock.patch.object(sleep_control.subprocess, "Popen")
    def test_mac_restarts_dead_assertion(self, popen):
        popen.return_value.poll.side_effect = [1]
        guard = sleep_control.IdleSleepInhibitor(platform="darwin")
        guard.sync(True)
        guard.sync(True)
        self.assertEqual(popen.call_count, 2)

    def test_windows_sets_and_clears_idle_sleep_assertion(self):
        kernel = mock.Mock()
        kernel.SetThreadExecutionState.return_value = 1
        with mock.patch.object(sleep_control.ctypes, "windll", create=True) as windll:
            windll.kernel32 = kernel
            guard = sleep_control.IdleSleepInhibitor(platform="win32")
            guard.sync(True)
            guard.sync(True)
            guard.close()
        self.assertEqual([c.args[0] for c in kernel.SetThreadExecutionState.call_args_list],
                         [sleep_control.ES_CONTINUOUS | sleep_control.ES_SYSTEM_REQUIRED,
                          sleep_control.ES_CONTINUOUS])

    def test_unsupported_platform_cannot_enable(self):
        guard = sleep_control.IdleSleepInhibitor(platform="linux")
        with self.assertRaises(OSError):
            guard.sync(True)
        guard.close()


class SleepSettingAPI(unittest.TestCase):
    def test_bool_setting_is_saved_and_visible(self):
        with (tempfile.TemporaryDirectory() as home,
              mock.patch.object(config, "HOME", home),
              mock.patch.object(config, "PATH", os.path.join(home, "config.json")),
              mock.patch.object(ui_api.store, "event"),
              mock.patch.object(ui_api.sys, "platform", "darwin")):
            cfg = config.load()
            code, result, _ = ui_api.handle("POST", "config", {"no_sleep": True}, {}, cfg)
            self.assertEqual((code, result["ok"]), (200, True))
            self.assertTrue(config.load()["no_sleep"])
            code, result, _ = ui_api.handle("GET", "config", {}, {}, config.load())
            self.assertTrue(result["no_sleep"])
            self.assertTrue(result["sleep_supported"])
            code, _, _ = ui_api.handle("POST", "config", {"no_sleep": False}, {}, config.load())
            self.assertEqual(code, 200)
            self.assertFalse(config.load()["no_sleep"])

    def test_rejects_non_boolean_or_unsupported_enable(self):
        cfg = config.load()
        with mock.patch.object(ui_api.sys, "platform", "linux"):
            for value in (True, "true"):
                code, _, _ = ui_api.handle("POST", "config", {"no_sleep": value}, {}, cfg)
                self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
