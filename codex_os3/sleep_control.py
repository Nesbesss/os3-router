"""Keep the host awake during idle time while the router service runs.

This does not override a manual Sleep request or a laptop lid closing. The
supervisor owns the assertion so it survives HTTP worker reloads.
"""
import ctypes
import os
import subprocess
import sys


SUPPORTED = ("darwin", "win32")
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class IdleSleepInhibitor:
    def __init__(self, platform=None):
        self.platform = platform or sys.platform
        self.process = None
        self.windows_active = False

    def sync(self, enabled):
        """Apply the saved setting; retry if a macOS assertion process exited."""
        if self.platform == "darwin":
            if enabled and (self.process is None or self.process.poll() is not None):
                self.process = subprocess.Popen(
                    ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif not enabled and self.process is not None:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait()
                self.process = None
        elif self.platform == "win32":
            if enabled != self.windows_active:
                flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enabled else 0)
                if not ctypes.windll.kernel32.SetThreadExecutionState(flags):
                    raise OSError("Windows rejected the idle sleep setting")
                self.windows_active = enabled
        elif enabled:
            raise OSError("Idle sleep prevention is not supported on this platform")

    def close(self):
        self.sync(False)
