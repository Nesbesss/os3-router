"""Queued updates remain independent of optional watchdog recovery errors."""
import threading
import unittest
from unittest import mock

from codex_os3 import store, updater, watchdog


class UpdateWatchdogTest(unittest.TestCase):
    def run_once(self, owner, tick_error=None, report_error=None):
        stop = threading.Event()
        cfg = {'watchdog': True, 'retention_days': 7}
        with mock.patch.object(watchdog.config, 'load', return_value=cfg), \
                mock.patch.object(watchdog, '_owner', return_value=owner), \
                mock.patch.object(watchdog, 'tick', side_effect=tick_error), \
                mock.patch.object(store, 'event', side_effect=report_error), \
                mock.patch.object(store, 'prune'), \
                mock.patch.object(updater, 'maybe') as update, \
                mock.patch.object(stop, 'wait', side_effect=lambda seconds: stop.set()):
            watchdog.loop(stop)
        return update

    def test_recovery_exception_does_not_skip_update(self):
        self.run_once(True, OSError('optional recovery failed')).assert_called_once_with(
            {'watchdog': True, 'retention_days': 7})

    def test_error_reporting_exception_does_not_skip_update(self):
        self.run_once(True, OSError('optional recovery failed'), OSError('event failed')).assert_called_once()

    def test_non_owner_cannot_run_update(self):
        self.run_once(False).assert_not_called()
