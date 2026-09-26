"""Static checks for the Windows scripts (CI parses them on Windows; these guard the fixes for real-world reports)."""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class InstallLoginCheck(unittest.TestCase):
    def test_login_status_stderr_is_not_fatal(self):
        # `codex login status` prints "Logged in using ChatGPT" on stderr and exits 0. Under
        # $ErrorActionPreference = "Stop" Windows PowerShell 5.1 aborts on that (NativeCommandError).
        src = read("install.ps1")
        status = src.index("login status")
        relax = src.rindex('$ErrorActionPreference = "Continue"', 0, status)
        restore = src.index("$ErrorActionPreference = $prevEap", status)
        self.assertLess(relax, status)
        self.assertLess(status, restore)
        self.assertLess(restore - status, 200, "the relaxed preference must only wrap the status call")
        self.assertIn("$loginStatus = $LASTEXITCODE", src[status:restore])
        # the exit code is still what decides whether to log in
        self.assertIn("if ($loginStatus -ne 0)", src[restore:])


if __name__ == "__main__":
    unittest.main()
