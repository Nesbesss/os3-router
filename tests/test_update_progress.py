"""Offline update lifecycle tests: failures must not look like completed updates."""
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

from codex_os3 import config, store, supervisor, update_progress, updater


class UpdateProgressTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.values = {}
        self.real_update_apps = updater.update_apps
        for patch in (
            mock.patch.object(config, 'HOME', self.temp.name),
            mock.patch.object(updater, 'update_apps'),
            mock.patch.object(updater.platform_util, 'pid_alive', return_value=False),
            mock.patch.object(store, 'kv_get', side_effect=lambda k, default=None: self.values.get(k, default)),
            mock.patch.object(store, 'kv_set', side_effect=lambda k, v: self.values.__setitem__(k, v)),
            mock.patch.object(store, 'event'),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.app = str(Path(self.temp.name) / 'app')
        old = Path(self.app) / 'codex_os3'
        old.mkdir(parents=True)
        (old / '__init__.py').write_text('__version__ = "0.8.0"')
        (Path(self.app) / 'keep.txt').write_text('previous')

    def archive(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz') as archive:
            for name, data in (('release/codex_os3/__init__.py', b'__version__ = "0.9.0"'),
                               ('release/new.txt', b'new')):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return buf.getvalue()

    def test_atomic_snapshot_tracks_phases_without_private_config(self):
        update_progress.set_state('installing', 'v0.9.0', 'download', 'Downloading')
        start = update_progress.get()['started']
        update_progress.set_state('installing', 'v0.9.0', 'tests', 'Testing')
        value = json.loads((Path(self.temp.name) / 'update-progress.json').read_text())
        self.assertEqual(value, update_progress.get())
        self.assertEqual(value['started'], start)
        self.assertEqual(value['phase'], 'tests')
        self.assertNotIn('api_key', value)
        self.assertEqual(sorted(p.name for p in Path(self.temp.name).iterdir()), ['app', 'update-progress.json'])

    def test_install_waits_for_health_and_retains_previous_files(self):
        self.values['manual_update'] = {'state': 'installing', 'tag': 'v0.9.0'}
        with mock.patch.object(updater, '_get', return_value=self.archive()), \
                mock.patch.object(update_progress, 'open_window') as window:
            updater.install('v0.9.0', app=self.app, run_tests=False)
        window.assert_called_once_with(self.app)
        self.assertEqual(update_progress.get()['state'], 'switching')
        self.assertEqual(self.values['manual_update']['state'], 'switching')
        self.assertTrue((Path(self.temp.name) / 'reload.request').exists())
        self.assertEqual((Path(self.app + '.prev') / 'keep.txt').read_text(), 'previous')
        update_progress.confirm_running('0.8.0')
        self.assertEqual(update_progress.get()['state'], 'switching')
        update_progress.confirm_running('0.9.0')
        self.assertEqual(update_progress.get()['state'], 'installed')
        self.assertEqual(self.values['manual_update']['state'], 'installed')

    def test_download_failure_is_visible_and_does_not_reload(self):
        with mock.patch.object(updater, '_get', side_effect=OSError('offline')), \
                mock.patch.object(update_progress, 'open_window'):
            with self.assertRaises(OSError):
                updater.install('v0.9.0', app=self.app, run_tests=False)
        self.assertEqual(update_progress.get()['state'], 'failed')
        self.assertIn('offline', update_progress.get()['error'])
        self.assertFalse((Path(self.temp.name) / 'reload.request').exists())
        self.assertEqual((Path(self.app) / 'keep.txt').read_text(), 'previous')

    def test_partial_copy_failure_restores_files_and_removes_new_files(self):
        real_copy = updater.shutil.copytree

        def copy(src, dst, *args, **kwargs):
            if dst == self.app and src != self.app + '.prev':
                (Path(dst) / 'new.txt').write_text('partial')
                (Path(dst) / 'codex_os3' / '__init__.py').write_text('broken')
                raise OSError('file locked')
            return real_copy(src, dst, *args, **kwargs)

        with mock.patch.object(updater, '_get', return_value=self.archive()), \
                mock.patch.object(update_progress, 'open_window'), \
                mock.patch.object(updater.shutil, 'copytree', side_effect=copy):
            with self.assertRaises(OSError):
                updater.install('v0.9.0', app=self.app, run_tests=False)
        self.assertFalse((Path(self.app) / 'new.txt').exists())
        self.assertIn('0.8.0', (Path(self.app) / 'codex_os3' / '__init__.py').read_text())
        self.assertIn('previous files were restored', update_progress.get()['message'])
        self.assertFalse((Path(self.temp.name) / 'reload.request').exists())

    def test_test_failure_preserves_previous_install(self):
        output = 'FAIL: test_claude_check_has_no_console_window\n' + 'socket recv_into frame\n' * 40
        def run(*args, **kwargs):
            kwargs['stdout'].write(output)
            return mock.Mock(returncode=1)
        with mock.patch.object(updater, '_get', return_value=self.archive()), \
                mock.patch.object(update_progress, 'open_window'), \
                mock.patch.object(updater.subprocess, 'run', side_effect=run):
            with self.assertRaisesRegex(RuntimeError, 'test_claude_check_has_no_console_window'):
                updater.install('v0.9.0', app=self.app)
        log = (Path(self.temp.name) / 'update-tests.log').read_text()
        self.assertIn(output, log)
        self.assertIn('Test process exit code: 1', log)
        self.assertEqual(update_progress.get()['state'], 'failed')
        self.assertFalse(Path(self.app + '.prev').exists())
        self.assertIn('0.8.0', (Path(self.app) / 'codex_os3' / '__init__.py').read_text())

    def test_validation_timeout_keeps_partial_log_and_previous_install(self):
        def run(*args, **kwargs):
            kwargs['stdout'].write('test_hanging ... partial traceback\n')
            raise updater.subprocess.TimeoutExpired(args[0], 900)
        with mock.patch.object(updater, '_get', return_value=self.archive()), \
                mock.patch.object(update_progress, 'open_window'), \
                mock.patch.object(updater.subprocess, 'run', side_effect=run):
            with self.assertRaisesRegex(RuntimeError, 'tests timed out; full log:'):
                updater.install('v0.9.0', app=self.app)
        log = (Path(self.temp.name) / 'update-tests.log').read_text()
        self.assertIn('partial traceback', log)
        self.assertIn('timed out after 900 seconds', log)
        self.assertFalse((Path(self.temp.name) / 'reload.request').exists())
        self.assertIn('0.8.0', (Path(self.app) / 'codex_os3' / '__init__.py').read_text())

    def test_live_updater_is_not_interrupted_after_lease_expires(self):
        update_progress.set_state('installing', 'v0.9.0', 'copy', 'Installing')
        self.values['update_progress']['pid'] = os.getpid() + 1
        with mock.patch.object(updater, 'managed', return_value=True), \
                mock.patch.object(updater.platform_util, 'pid_alive', return_value=True), \
                mock.patch.object(updater, 'restore_previous') as restore, \
                mock.patch.object(updater, 'install') as install:
            updater.maybe({'auto_update': True})
        restore.assert_not_called()
        install.assert_not_called()
        self.assertEqual(update_progress.get()['state'], 'installing')

    def test_failed_interruption_rollback_cannot_start_another_install(self):
        update_progress.set_state('installing', 'v0.9.0', 'copy', 'Installing')
        self.values['update_progress']['pid'] = os.getpid() + 1
        self.values['manual_update'] = {'state': 'installing', 'tag': 'v0.9.0'}
        with mock.patch.object(updater, 'managed', return_value=True), \
                mock.patch.object(updater.platform_util, 'pid_alive', return_value=False), \
                mock.patch.object(updater, 'restore_previous', side_effect=OSError('locked')), \
                mock.patch.object(updater, 'latest') as latest, \
                mock.patch.object(updater, 'install') as install:
            updater.maybe({'auto_update': True})
            updater.maybe({'auto_update': True})  # Later ticks must preserve the backup too.
        latest.assert_not_called()
        install.assert_not_called()
        self.assertEqual(update_progress.get()['state'], 'failed')
        self.assertIn('Could not restore', update_progress.get()['error'])

    def test_real_validation_subprocess_keeps_log_and_isolated_homes(self):
        buf = io.BytesIO()
        script = b"import os, unittest\nclass Isolated(unittest.TestCase):\n def test_home(self):\n  assert os.environ.get('HOME') == os.environ.get('USERPROFILE')\n  home = os.environ.get('CODEX_OS3_HOME')\n  assert os.path.basename(home) == 'router'\n  assert os.path.basename(os.path.dirname(home)) == 'test-home'\n"
        with tarfile.open(fileobj=buf, mode='w:gz') as archive:
            for name, data in [('release/codex_os3/__init__.py', b'__version__ = "0.9.0"'),
                               ('release/tests/test_isolated.py', script)]:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        with mock.patch.object(updater, '_get', return_value=buf.getvalue()), \
                mock.patch.object(update_progress, 'open_window'):
            updater.install('v0.9.0', app=self.app)
        log = (Path(self.temp.name) / 'update-tests.log').read_text()
        self.assertIn('test_home', log)
        self.assertIn('Test process exit code: 0', log)
        self.assertEqual(update_progress.get()['state'], 'switching')
        self.assertTrue(Path(self.app + '.prev').exists())

    def test_unreleased_update_notes_reach_whats_new(self):
        from codex_os3 import ui_api
        with mock.patch.object(ui_api, '__version__', '0.6.8'), \
                mock.patch.object(ui_api, '_prev_version', return_value='0.6.7'):
            note = ui_api.whatsnew()
        self.assertTrue(note['show'])
        self.assertEqual(note['sections'][0]['version'], '0.6.8')
        self.assertIn('complete test log', note['sections'][0]['body'])

    def test_health_from_old_pid_cannot_confirm_completion(self):
        update_progress.set_state('switching', 'v0.9.0', 'restart', 'Restarting')
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({'status': 'ok', 'pid': 10, 'version': '0.9.0'}).encode()
        with mock.patch.object(supervisor.urllib.request, 'urlopen', return_value=response), \
                mock.patch.object(supervisor.time, 'time', side_effect=[0, 0, 50]), \
                mock.patch.object(supervisor.time, 'sleep'):
            self.assertFalse(supervisor._healthy({'bind': '127.0.0.1', 'port': 1}, 20))
        self.assertEqual(update_progress.get()['state'], 'switching')

    def test_replacement_health_confirms_version(self):
        update_progress.set_state('switching', 'v0.9.0', 'restart', 'Restarting')
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"status":"ok","pid":20,"version":"0.9.0"}'
        with mock.patch.object(supervisor.urllib.request, 'urlopen', return_value=response):
            self.assertTrue(supervisor._healthy({'bind': '127.0.0.1', 'port': 1}, 20))
        self.assertEqual(update_progress.get()['state'], 'installed')

    def test_failed_restart_rolls_back_and_stays_failed(self):
        update_progress.set_state('switching', 'v0.9.0', 'restart', 'Restarting')
        with mock.patch.object(updater, 'restore_previous') as restore:
            supervisor._recover_failed_update()
        restore.assert_called_once_with()
        self.assertEqual(update_progress.get()['state'], 'failed')
        update_progress.confirm_running('0.8.0')
        self.assertEqual(update_progress.get()['state'], 'failed')

    def test_failed_rollback_is_reported(self):
        update_progress.set_state('switching', 'v0.9.0', 'restart', 'Restarting')
        with mock.patch.object(updater, 'restore_previous', side_effect=OSError('locked')):
            supervisor._recover_failed_update()
        self.assertIn('Could not restore', update_progress.get()['error'])

    def test_automatic_install_is_not_replayed_during_switch(self):
        update_progress.set_state('switching', 'v0.9.0', 'restart', 'Restarting')
        with mock.patch.object(updater, 'managed', return_value=True), mock.patch.object(updater, 'latest') as latest:
            updater.maybe({'auto_update': True})
        latest.assert_not_called()

    def test_interrupted_install_can_be_retried(self):
        update_progress.set_state('installing', 'v0.9.0', 'download', 'Downloading')
        self.values['update_progress']['pid'] = os.getpid() + 1
        self.values['manual_update'] = {'state': 'installing', 'tag': 'v0.9.0'}
        with mock.patch.object(updater, 'managed', return_value=True), mock.patch.object(updater.sys, 'platform', 'win32'):
            updater.maybe({'auto_update': False})
        self.assertEqual(self.values['manual_update']['state'], 'failed')
        self.assertIn('interrupted', update_progress.get()['error'])

    def test_window_start_failure_does_not_abort_update(self):
        with mock.patch.object(update_progress.os, 'name', 'nt'), \
                mock.patch.object(update_progress.subprocess, 'Popen', side_effect=OSError('no desktop')):
            update_progress.open_window(self.app)
        store.event.assert_called_once()

    def test_no_progress_window_outside_windows(self):
        with mock.patch.object(update_progress.os, 'name', 'posix'), \
                mock.patch.object(update_progress.subprocess, 'Popen') as launch:
            update_progress.open_window(self.app)
        launch.assert_not_called()

    def test_health_with_wrong_version_cannot_confirm_completion(self):
        update_progress.set_state('switching', 'v0.9.0', 'restart', 'Restarting')
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"status":"ok","pid":20,"version":"0.8.0"}'
        with mock.patch.object(supervisor.urllib.request, 'urlopen', return_value=response), \
                mock.patch.object(supervisor.time, 'time', side_effect=[0, 0, 50]), \
                mock.patch.object(supervisor.time, 'sleep'):
            self.assertFalse(supervisor._healthy({'bind': '127.0.0.1', 'port': 1}, 20))
        self.assertEqual(update_progress.get()['state'], 'switching')

    def test_manual_update_refreshes_companion_with_auto_off(self):
        with mock.patch.object(updater, 'managed', return_value=True), mock.patch.object(updater.sys, 'platform', 'win32'):
            updater.maybe({'auto_update': False})
        updater.update_apps.assert_called_once()
        self.assertEqual(self.values['apps_version'], updater.__version__)

    def test_companion_failure_is_retried_after_cooldown(self):
        updater.update_apps.side_effect = OSError('cannot create shortcut')
        with mock.patch.object(updater, 'managed', return_value=True), mock.patch.object(updater.sys, 'platform', 'win32'):
            updater.maybe({'auto_update': False})
            updater.maybe({'auto_update': False})
            self.assertEqual(updater.update_apps.call_count, 1)
            self.assertNotIn('apps_version', self.values)
            self.values['apps_retry'] -= 301
            updater.update_apps.side_effect = None
            updater.maybe({'auto_update': False})
        self.assertEqual(updater.update_apps.call_count, 2)
        self.assertEqual(self.values['apps_version'], updater.__version__)

    def test_windows_companion_checks_shortcut_and_tray_errors_without_console(self):
        with mock.patch.object(updater.sys, 'platform', 'win32'), \
                mock.patch.object(updater.platform_util, 'no_window_kwargs', return_value={'creationflags': 123}), \
                mock.patch.object(updater.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run:
            self.real_update_apps('v0.9.0')
        self.assertEqual(len(run.call_args_list), 4)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs['creationflags'], 123)
        self.assertTrue(run.call_args_list[0].kwargs['check'])
        self.assertTrue(run.call_args_list[-1].kwargs['check'])

    def test_no_tray_install_does_not_attempt_tray_restart(self):
        with mock.patch.object(updater.sys, 'platform', 'win32'), \
                mock.patch.object(updater.subprocess, 'run', side_effect=[mock.Mock(returncode=0), mock.Mock(returncode=1)]) as run:
            self.real_update_apps('v0.9.0')
        self.assertEqual(run.call_count, 2)

    def test_new_release_notes_reach_whats_new(self):
        from codex_os3 import ui_api
        with mock.patch.object(ui_api, '__version__', '0.6.5'), \
                mock.patch.object(ui_api, '_prev_version', return_value='0.6.3'):
            note = ui_api.whatsnew()
        self.assertTrue(note['show'])
        self.assertEqual(note['sections'][0]['version'], '0.6.5')
        self.assertIn('taskbar window', note['sections'][0]['body'])


    def test_retry_same_tag_starts_a_new_attempt(self):
        with mock.patch.object(update_progress.time, 'time', side_effect=[100, 101, 200]):
            update_progress.set_state('installing', 'v0.9.0', 'download', 'Downloading')
            update_progress.fail('offline')
            update_progress.set_state('queued', 'v0.9.0', 'queued', 'Retry requested')
        self.assertEqual(update_progress.get()['started'], 200)
        self.assertNotIn('error', update_progress.get())

    def test_interrupted_copy_restores_previous_before_retry(self):
        update_progress.set_state('installing', 'v0.9.0', 'copy', 'Installing')
        self.values['update_progress']['pid'] = os.getpid() + 1
        with mock.patch.object(updater, 'managed', return_value=True), mock.patch.object(updater, 'restore_previous') as restore:
            updater.maybe({'auto_update': False})
        restore.assert_called_once_with()
        self.assertEqual(update_progress.get()['state'], 'failed')
        self.assertIn('previous files were restored', update_progress.get()['message'])

    def test_interrupted_copy_rollback_failure_stays_visible(self):
        update_progress.set_state('installing', 'v0.9.0', 'copy', 'Installing')
        self.values['update_progress']['pid'] = os.getpid() + 1
        with mock.patch.object(updater, 'managed', return_value=True), mock.patch.object(updater, 'restore_previous', side_effect=OSError('locked')):
            updater.maybe({'auto_update': False})
        self.assertIn('Could not restore', update_progress.get()['error'])

    def test_worker_confirmation_is_independent_of_watchdog(self):
        update_progress.set_state('switching', 'v0.9.0', 'restart', 'Restarting')
        with mock.patch.object(supervisor, '_healthy', return_value=True) as healthy:
            update_progress.confirm_worker({'bind': '127.0.0.1', 'port': 1})
        healthy.assert_called_once_with({'bind': '127.0.0.1', 'port': 1}, os.getpid())

    def test_failed_health_status_cannot_confirm_completion(self):
        update_progress.set_state('switching', 'v0.9.0', 'restart', 'Restarting')
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"status":"error","pid":20,"version":"0.9.0"}'
        with mock.patch.object(supervisor.urllib.request, 'urlopen', return_value=response), mock.patch.object(supervisor.time, 'time', side_effect=[0, 0, 50]), mock.patch.object(supervisor.time, 'sleep'):
            self.assertFalse(supervisor._healthy({'bind': '127.0.0.1', 'port': 1}, 20))
        self.assertEqual(update_progress.get()['state'], 'switching')

    def test_running_version_alone_cannot_override_recorded_failure(self):
        self.values['manual_update'] = {'state': 'switching', 'tag': 'v0.9.0'}
        update_progress.set_state('switching', 'v0.9.0', 'restart', 'Restarting')
        update_progress.fail('health check failed')
        with mock.patch.object(updater, '__version__', '0.9.0'):
            self.assertEqual(updater.manual_state()['state'], 'failed')

    def test_snapshot_write_error_does_not_abort_update(self):
        with mock.patch.object(update_progress.os, 'replace', side_effect=PermissionError('file open')):
            update_progress.set_state('installing', 'v0.9.0', 'download', 'Downloading')
        self.assertEqual(update_progress.get()['state'], 'installing')
        self.assertFalse((Path(self.temp.name) / 'update-progress.json').exists())
        self.assertEqual(sorted(p.name for p in Path(self.temp.name).iterdir()), ['app'])
        store.event.assert_called_once()


    def test_readiness_does_not_wait_for_optional_codex_diagnostics(self):
        from types import SimpleNamespace
        from codex_os3 import server
        send = mock.Mock()
        request = SimpleNamespace(path='/health?ready=1', send=send)
        with mock.patch.object(config, 'load', return_value={}), mock.patch.object(server.ui_api, 'codex_info', side_effect=AssertionError('CLI probe must not run')):
            server.Handler.do_GET(request)
        self.assertEqual(send.call_args.args[0], 200)
        self.assertEqual(send.call_args.args[1]['pid'], os.getpid())
        self.assertEqual(send.call_args.args[1]['version'], server.__version__)


    def test_already_running_target_is_checked_and_clears_queue(self):
        self.values['manual_update'] = {'state': 'queued', 'tag': 'v0.9.0'}
        update_progress.set_state('queued', 'v0.9.0', 'queued', 'Waiting')
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({'status': 'ok', 'pid': os.getpid(), 'version': '0.9.0'}).encode()
        with mock.patch.object(updater, 'managed', return_value=True), mock.patch.object(updater, '__version__', '0.9.0'), mock.patch.object(supervisor.urllib.request, 'urlopen', return_value=response):
            updater.maybe({'auto_update': False, 'bind': '127.0.0.1', 'port': 1})
        self.assertEqual(update_progress.get()['state'], 'installed')
        self.assertEqual(updater.manual_state()['state'], 'installed')
