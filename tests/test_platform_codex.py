"""Codex discovery and diagnostics use temporary executables, never installed CLIs."""
import os, subprocess, tempfile, unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault('CODEX_OS3_HOME', tempfile.mkdtemp(prefix='cxos3-codex-test-'))
from codex_os3 import appserver, codex_runner, config, onboarding, platform_util, server, store, ui_api, updater  # noqa: E402


class CodexPathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        ui_api._cache.clear()
        self.addCleanup(ui_api._cache.clear)
        for patch in (mock.patch.object(platform_util.shutil, 'which', return_value=None),
                      mock.patch.object(platform_util, 'CODEX_DIRS', [self.root]),
                      mock.patch.object(platform_util, 'CODEX_APP_DIRS', [self.root])):
            patch.start()
            self.addCleanup(patch.stop)

    def executable(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write('#!/bin/sh\nexit 0\n')
        os.chmod(path, 0o755)
        return path

    def test_valid_explicit_binary_wins(self):
        saved = self.executable(os.path.join(self.root, 'custom'))
        self.executable(os.path.join(self.root, 'codex'))
        self.assertEqual(platform_util.codex_path({'codex_bin': saved}), saved)

    def test_missing_saved_app_uses_chatgpt(self):
        suffix = 'Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'
        binary = self.executable(os.path.join(self.root, 'ChatGPT.app', suffix))
        cfg = {'codex_bin': os.path.join(self.root, 'Codex.app', suffix)}
        with mock.patch.object(platform_util.sys, 'platform', 'darwin'):
            self.assertEqual(platform_util.codex_path(cfg), binary)
        self.assertIn('Codex.app', cfg['codex_bin'])

    def test_valid_codex_app_is_preserved(self):
        suffix = 'Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'
        saved = self.executable(os.path.join(self.root, 'Codex.app', suffix))
        self.executable(os.path.join(self.root, 'ChatGPT.app', suffix))
        with mock.patch.object(platform_util.sys, 'platform', 'darwin'):
            self.assertEqual(platform_util.codex_path({'codex_bin': saved}), saved)

    def test_missing_saved_binary_uses_path(self):
        binary = self.executable(os.path.join(self.root, 'codex'))
        with mock.patch.object(platform_util.shutil, 'which', return_value=binary):
            self.assertEqual(platform_util.codex_path({'codex_bin': '/missing/codex'}), binary)

    def test_saved_command_is_resolved_on_path(self):
        binary = self.executable(os.path.join(self.root, 'custom-codex'))
        with mock.patch.object(platform_util.shutil, 'which', side_effect=lambda name: binary if name == 'custom-codex' else None):
            self.assertEqual(platform_util.codex_path({'codex_bin': 'custom-codex'}), binary)

    def test_non_executable_saved_path_is_recovered(self):
        saved = self.executable(os.path.join(self.root, 'custom'))
        os.chmod(saved, 0o644)
        binary = self.executable(os.path.join(self.root, 'codex'))
        with mock.patch.object(platform_util, 'WINDOWS', False), mock.patch.object(platform_util.os, 'access', side_effect=lambda path, mode: path == binary):
            self.assertEqual(platform_util.codex_path({'codex_bin': saved}), binary)

    def test_discovery_checks_each_request_without_changing_configuration(self):
        saved = self.executable(os.path.join(self.root, 'custom'))
        cfg = {'codex_bin': saved}
        self.assertEqual(platform_util.codex_path(cfg), saved)
        os.unlink(saved)
        binary = self.executable(os.path.join(self.root, 'codex'))
        self.assertEqual(platform_util.codex_path(cfg), binary)
        self.assertEqual(cfg['codex_bin'], saved)

    def test_missing_codex_is_reported(self):
        with mock.patch.object(platform_util.sys, 'platform', 'linux'), mock.patch.object(ui_api.subprocess, 'run') as run:
            info = ui_api.codex_info({'codex_bin': '/missing/codex'}, fresh=True)
        self.assertIsNone(info['path'])
        self.assertIn('missing', info['error'])
        run.assert_not_called()

    def test_both_engines_use_recovered_binary(self):
        binary = self.executable(os.path.join(self.root, 'codex'))
        cfg = {'codex_bin': '/missing/codex', 'effort': 'medium'}
        with mock.patch.object(codex_runner, 'known_features', return_value=set()):
            self.assertEqual(codex_runner.build_cmd(cfg, 'gpt-6-luna')[0], binary)
        with mock.patch.dict(appserver._servers, clear=True), mock.patch.dict(appserver._starting, clear=True):
            with mock.patch.object(appserver, 'Server') as make:
                appserver.server(cfg)
        self.assertEqual(make.call_args.args[0], binary)

    def test_both_engines_fail_clearly_when_codex_is_missing(self):
        cfg = {'codex_bin': '/missing/codex', 'effort': 'medium'}
        with mock.patch.object(platform_util, 'codex_path', return_value=None):
            for run in (lambda: codex_runner.build_cmd(cfg, 'gpt-6-luna'), lambda: appserver.server(cfg)):
                with self.assertRaisesRegex(RuntimeError, 'Codex CLI is missing'):
                    run()

    def test_failed_binary_is_reported(self):
        binary = self.executable(os.path.join(self.root, 'codex'))
        errors = [FileNotFoundError('binary moved'), PermissionError('cannot execute'), subprocess.TimeoutExpired(binary, 15)]
        for error in errors:
            with self.subTest(error=type(error).__name__), mock.patch.object(ui_api.subprocess, 'run', side_effect=error):
                info = ui_api.codex_info({'codex_bin': binary}, fresh=True)
            self.assertIn('could not start', info['error'])

    def test_failed_version_check_does_not_look_like_a_version(self):
        binary = self.executable(os.path.join(self.root, 'codex'))
        failed = SimpleNamespace(returncode=1, stdout='', stderr='loader failure')
        with mock.patch.object(ui_api.subprocess, 'run', return_value=failed) as run:
            info = ui_api.codex_info({'codex_bin': binary}, fresh=True)
        self.assertIsNone(info['version'])
        self.assertIn('loader failure', info['error'])
        self.assertEqual(run.call_count, 1)

    def test_signed_out_is_separate_from_a_failed_binary(self):
        binary = self.executable(os.path.join(self.root, 'codex'))
        results = [SimpleNamespace(returncode=0, stdout='codex-cli 0.160.0', stderr=''),
                   SimpleNamespace(returncode=1, stdout='', stderr='Not logged in')]
        with mock.patch.object(ui_api.subprocess, 'run', side_effect=results):
            info = ui_api.codex_info({'codex_bin': binary}, fresh=True)
        self.assertEqual(info['version'], '0.160.0')
        self.assertFalse(info['logged_in'])
        self.assertNotIn('error', info)

    def test_health_reports_missing_and_failed_binary_without_breaking_liveness(self):
        for path in ('/health', '/v1'):
            for info in ({'path': None, 'error': platform_util.CODEX_MISSING},
                         {'path': '/codex', 'error': 'Codex version check failed'}, {'path': '/codex'}):
                with self.subTest(path=path, info=info):
                    handler = SimpleNamespace(path=path, send=mock.Mock())
                    with mock.patch.object(server.config, 'load', return_value={}), mock.patch.object(ui_api, 'codex_info', return_value=info):
                        server.Handler.do_GET(handler)
                    status, body = handler.send.call_args.args
                    self.assertEqual((status, body['status']), (200, 'ok'))
                    self.assertEqual(body['codex_available'], 'error' not in info)
                    self.assertEqual(body['codex_error'], info.get('error'))

    def test_status_exposes_codex_failure_and_claude_only_requirement(self):
        info = {'path': None, 'error': platform_util.CODEX_MISSING}
        with mock.patch.object(ui_api, 'codex_info', return_value=info), mock.patch.object(store, 'latest_limits', return_value={}), \
                mock.patch.object(store, 'kv_get', return_value=None), mock.patch.object(store, 'q', return_value=[{'n': 0}]), \
                mock.patch.object(ui_api, 'whatsnew', return_value={'show': False}), mock.patch.object(ui_api.os3, 'status', return_value={}), \
                mock.patch.object(ui_api.accounts, 'pick', return_value='main'):
            for model, required in (('gpt-6-luna', True), ('claude-sonnet-5-5', False)):
                cfg = dict(config.DEFAULTS, role_routing=False, model=model, fallback={})
                code, body, _ = ui_api.handle('GET', 'status', {}, {}, cfg)
                self.assertEqual(code, 200)
                self.assertEqual(body['codex'], info)
                self.assertEqual(body['codex_required'], required)

    def test_setup_shows_launch_error(self):
        cfg = dict(config.DEFAULTS, role_routing=False, model='gpt-6-luna', fallback={})
        with mock.patch.object(ui_api, 'codex_info', return_value={'path': '/codex', 'error': 'Codex could not start: loader failure'}):
            step = onboarding.engine_step(cfg)
        self.assertEqual(step['state'], 'error')
        self.assertIn('loader failure', step['detail'])

    def test_whats_new_after_release(self):
        with mock.patch.object(ui_api, '__version__', '0.6.3'), mock.patch.object(store, 'kv_get', return_value='0.6.2'), \
                mock.patch.object(ui_api, '_prev_version', return_value=None), \
                mock.patch.object(updater, 'APP', os.path.dirname(os.path.dirname(__file__))):
            note = ui_api.whatsnew()
        self.assertTrue(note['show'])
        self.assertEqual(note['sections'][0]['version'], '0.6.3')
        self.assertIn('ChatGPT.app', note['sections'][0]['body'])
