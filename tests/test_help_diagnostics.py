"""Regression checks for Help and setup when local status disagrees with Codex or the node."""
import unittest
from unittest import mock

from codex_os3 import onboarding, selffix


class HelpDiagnosticsTest(unittest.TestCase):
    def test_readonly_codex_state_database_has_useful_error(self):
        error = ("sqlite error (code 8), attempt to write a readonly database; "
                 "codex_rollout::state_db failed to initialize the state runtime")
        with mock.patch.object(selffix, "context", return_value="{}"), \
             mock.patch.object(selffix.codex_runner, "run", side_effect=RuntimeError(error)), \
             mock.patch.object(selffix.roles, "claude_installed", return_value=False):
            result = selffix.diagnose({}, "Find the problem fails")
        self.assertIn("cannot write its state database", result["error"])
        self.assertIn("~/.codex", result["error"])
        self.assertNotIn("sqlite error", result["error"])

    def test_saved_connected_status_without_live_process_is_not_ready(self):
        with mock.patch.object(onboarding.os3, "installed", return_value=True), \
             mock.patch.object(onboarding.os3, "status", return_value={"status": "connected", "running": False}):
            step = onboarding.node_step()
        self.assertEqual(step["state"], "error")
        self.assertIn("saved agent status says connected", step["detail"])
        self.assertIn("could not be verified", step["detail"])
        self.assertNotIn("action", step)

    def test_connected_process_remains_ready(self):
        with mock.patch.object(onboarding.os3, "installed", return_value=True), \
             mock.patch.object(onboarding.os3, "status", return_value={"status": "connected", "running": True, "version": "1"}):
            step = onboarding.node_step()
        self.assertEqual(step["state"], "ok")
