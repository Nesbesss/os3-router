"""Settings update controls: a request is not an installed release."""
import os, tempfile, time, unittest
from unittest import mock

from codex_os3 import config, store, ui_api, updater, update_progress


class UpdateControlsTest(unittest.TestCase):
    def setUp(self):
        self.values = {}
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.patches = [
            mock.patch.object(config, "HOME", home.name),
            mock.patch.object(update_progress, "open_window"),
            mock.patch.object(updater, "managed", return_value=True),
            mock.patch.object(updater, "__version__", "0.4.14"),
            mock.patch.object(updater, "update_apps"),
            mock.patch.object(store, "kv_get", side_effect=lambda k, default=None: self.values.get(k, default)),
            mock.patch.object(updater.platform_util, 'pid_alive', return_value=False),

            mock.patch.object(store, "kv_set", side_effect=lambda k, v: self.values.__setitem__(k, v)),
            mock.patch.object(store, "event"),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.cfg = {"auto_update": False}

    def call(self, method, path):
        return ui_api.handle(method, path, {}, {}, self.cfg)[:2]

    def test_retry_requires_successful_restore_after_rollback_failure(self):
        self.values['update_latest'] = 'v0.4.15'
        update_progress.set_state('installing', 'v0.4.15', 'copy', 'Installing')
        update_progress.fail('rollback failed', recovery_required=True)
        with mock.patch.object(updater, 'restore_previous', side_effect=OSError('locked')):
            self.assertEqual(self.call('POST', 'updates/install')[0], 409)
        self.assertTrue(update_progress.get()['recovery_required'])
        with mock.patch.object(updater, 'restore_previous') as restore:
            self.assertEqual(self.call('POST', 'updates/install')[0], 202)
        restore.assert_called_once_with()
        self.assertEqual(update_progress.get()['state'], 'queued')
        self.assertNotIn('recovery_required', update_progress.get())


    def test_manual_update_with_auto_off_waits_for_running_version(self):
        with mock.patch.object(updater, "latest", return_value="v0.4.15"):
            code, found = self.call("POST", "updates")
        self.assertEqual(code, 200)
        self.assertTrue(found["available"])
        self.assertFalse(found["automatic"])
        code, queued = self.call("POST", "updates/install")
        self.assertEqual(code, 202)
        self.assertEqual(queued["manual"]["state"], "queued")
        def install_result(tag):
            update_progress.set_state("switching", tag, "restart", "Restarting")
        with mock.patch.object(updater, "install", side_effect=install_result) as install:
            updater.maybe(self.cfg)
            install.assert_called_once_with("v0.4.15")
        self.assertEqual(self.call("GET", "updates")[1]["manual"]["state"], "switching")
        update_progress.confirm_running("0.4.15")
        with mock.patch.object(updater, "__version__", "0.4.15"):
            self.assertEqual(self.call("GET", "updates")[1]["manual"]["state"], "installed")

    def test_failed_manual_install_can_be_retried(self):
        self.values["update_latest"] = "v0.4.15"
        self.call("POST", "updates/install")
        def fail_install(tag):
            update_progress.set_state("installing", tag, "tests", "Testing")
            update_progress.fail("tests failed")
            raise RuntimeError("tests failed")
        with mock.patch.object(updater, "install", side_effect=fail_install):
            updater.maybe(self.cfg)
        failed = self.call("GET", "updates")[1]["manual"]
        self.assertEqual(failed["state"], "failed")
        self.assertIn("tests failed", failed["error"])
        with mock.patch.object(updater, "install") as install:
            updater.maybe(self.cfg)  # auto updates are off: no automatic retry
            install.assert_not_called()
            self.assertEqual(self.call("POST", "updates/install")[1]["manual"]["state"], "queued")
            updater.maybe(self.cfg)
            install.assert_called_once_with("v0.4.15")

    def test_no_install_without_managed_install_and_newer_release(self):
        self.assertEqual(self.call("POST", "updates/install")[0], 409)
        self.values["update_latest"] = "v0.4.14"
        self.assertEqual(self.call("POST", "updates/install")[0], 409)
        with mock.patch.object(updater, "managed", return_value=False), mock.patch.object(updater, "latest") as latest:
            self.assertEqual(self.call("POST", "updates")[0], 409)
            self.assertEqual(self.call("POST", "updates/install")[0], 409)
            latest.assert_not_called()

    def test_failed_check_keeps_known_release_and_automatic_schedule(self):
        self.values["update_latest"] = "v0.4.15"
        with mock.patch.object(updater, "latest", side_effect=OSError("offline")):
            code, body = self.call("POST", "updates")
        self.assertEqual(code, 502)
        self.assertIn("Could not check", body["error"])
        self.assertEqual(self.values["update_latest"], "v0.4.15")
        self.assertNotIn("update_checked", self.values)
        self.cfg["auto_update"] = True
        self.values["apps_version"] = "0.4.14"
        self.values["update_checked"] = time.time() - updater.EVERY_S - 1
        with mock.patch.object(updater, "latest", return_value="v0.4.14") as latest, \
                mock.patch.object(ui_api, "codex_info", return_value={"version": None}):
            updater.maybe(self.cfg)
            latest.assert_called_once_with()

    def test_changelog_entry_reaches_whats_new_on_release(self):
        with mock.patch.object(ui_api, "__version__", "0.4.15"), \
                mock.patch.object(ui_api, "_prev_version", return_value="0.4.14"):
            note = ui_api.whatsnew()
        self.assertTrue(note["show"])
        self.assertEqual(note["sections"][0]["version"], "0.4.15")
        self.assertIn("Find new updates", note["sections"][0]["body"])

    def test_both_settings_surfaces_offer_check_and_install(self):
        for name in ("app.html", "index.html"):
            with self.subTest(name=name):
                path = os.path.join(os.path.dirname(ui_api.__file__), "ui", name)
                with open(path, encoding="utf-8") as f:
                    page = f.read()
                self.assertIn("Find new updates", page)
                self.assertIn('post("updates")', page)
                self.assertIn('post("updates/install")', page)
                self.assertIn('role="status"', page)


if __name__ == "__main__":
    unittest.main()
