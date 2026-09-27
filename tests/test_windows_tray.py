"""Static checks for the Windows tray startup (CI parses the scripts on Windows; these guard the fix for a
reported logon failure: the hidden tray task exited within 3 s with 0xc0000142 and left no trace)."""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class TrayTask(unittest.TestCase):
    def section(self):
        src = read("install.ps1")
        return src[src.index("# --- tray app"):src.index("# --- app window")]

    def test_tray_starts_after_the_desktop_is_ready(self):
        sec = self.section()
        self.assertIn('$trigger.Delay = "PT20S"', sec)
        self.assertIn("-Trigger $trigger", sec)

    def test_tray_runs_interactive_and_restarts(self):
        sec = self.section()
        self.assertIn("New-ScheduledTaskPrincipal", sec)
        self.assertIn("-LogonType Interactive", sec)
        self.assertIn("-Principal $principal", sec)
        self.assertIn("-RestartCount 3", sec)

    def test_failing_to_start_the_tray_is_a_warning_not_an_abort(self):
        sec = self.section()
        self.assertIn("catch { Warn", sec)


class TrayScript(unittest.TestCase):
    def test_startup_errors_are_logged(self):
        src = read("app", "windows", "tray.ps1")
        self.assertIn("tray.log", src)
        trap = src.index("trap {")
        # the trap has to be in place before anything that can fail at startup
        self.assertLess(trap, src.index("Add-Type"))
        self.assertLess(trap, src.index("New-Object System.Windows.Forms.NotifyIcon"))

    def test_idle_sleep_toggle_sends_boolean_and_tracks_status(self):
        src = read("app", "windows", "tray.ps1")
        self.assertIn('$sleepItem = $menu.Items.Add("Prevent idle sleep")', src)
        self.assertIn('Post "config" (@{ no_sleep = $desired } | ConvertTo-Json -Compress)', src)
        self.assertIn('$sleepItem.Checked = [bool]$s.no_sleep', src)


if __name__ == "__main__":
    unittest.main()
