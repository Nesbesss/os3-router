import os, tempfile, unittest
from types import SimpleNamespace
from unittest import mock
from codex_os3 import appserver, codex_runner, platform_util, server, ui_api, updater, store

class CodexPathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
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
        binary = self.executable(os.path.join(self.root, 'ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'))
        cfg = {'codex_bin': self.root + '/Codex.app/missing'}
        with mock.patch.object(platform_util.sys, 'platform', 'darwin'):
            self.assertEqual(platform_util.codex_path(cfg), binary)
        self.assertIn('Codex.app', cfg['codex_bin'])

    def test_missing_saved_binary_uses_path(self):
        binary = self.executable(os.path.join(self.root, 'codex'))
        with mock.patch.object(platform_util.shutil, 'which', return_value=binary):
            self.assertEqual(platform_util.codex_path({'codex_bin': '/missing/codex'}), binary)

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

    def test_health_separates_liveness_and_missing_codex(self):
        handler = SimpleNamespace(path='/health', send=mock.Mock())
        with mock.patch.object(server.config, 'load', return_value={'codex_bin': '/missing/codex'}):
            with mock.patch.object(platform_util.sys, 'platform', 'linux'):
                server.Handler.do_GET(handler)
        status, body = handler.send.call_args.args
        self.assertEqual((status, body['status']), (200, 'ok'))
        self.assertFalse(body['codex_available'])
        self.assertIn('missing', body['codex_error'])

    def test_whats_new_after_release(self):
        with mock.patch.object(ui_api, '__version__', '0.6.1'), mock.patch.object(store, 'kv_get', return_value='0.6.0'):
            with mock.patch.object(updater, 'APP', os.path.dirname(os.path.dirname(__file__))):
                note = ui_api.whatsnew()
        self.assertTrue(note['show'])
        self.assertIn('ChatGPT.app', note['sections'][0]['body'])
