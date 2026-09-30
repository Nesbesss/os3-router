"""Process helpers that behave the same on macOS, Linux and Windows.
(Careful: on Windows os.kill(pid, 0) terminates the process instead of probing it.)"""
import glob, os, shutil, signal, subprocess, sys

WINDOWS = sys.platform == "win32"


def native_bin(path, windows=WINDOWS):
    """Windows: npm installs codex/claude as .ps1 / .cmd shims plus an extensionless sh script,
    and only .exe/.cmd can be started directly ("[WinError 193] not a valid Win32 application"
    for the others). Prefer the native .exe the npm package ships, else the .cmd shim."""
    if not windows or not path or path.lower().endswith(".exe"):
        return path
    base = os.path.splitext(path)[0] if path.lower().endswith((".ps1", ".cmd", ".bat")) else path
    name, d = os.path.basename(base), os.path.dirname(base)
    hits = sorted(glob.glob(os.path.join(d, "node_modules", "@*", "**", name + ".exe"), recursive=True))
    if hits:
        return hits[0]
    for ext in (".exe", ".cmd"):
        if os.path.isfile(base + ext):
            return base + ext
    return path


CODEX_DIRS = [os.path.expanduser(d) for d in
              ('~/.local/bin', '~/.npm-global/bin', '~/.volta/bin', '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin')]
CODEX_APP_DIRS = ['/Applications', os.path.expanduser('~/Applications')]
CODEX_MISSING = 'Codex CLI is missing or is not executable. Install Codex or ChatGPT, or run the router installer again.'


def codex_path(cfg=None):
    # Prefer a working explicit choice; rediscover installs when its saved path goes stale.
    # Launch services have a small PATH; desktop CLIs may move between app bundles.
    def executable(path):
        if not path:
            return None
        path = native_bin(os.path.expanduser(path))
        if not os.path.dirname(path):
            path = native_bin(shutil.which(path))
        return path if path and os.path.isfile(path) and (WINDOWS or os.access(path, os.X_OK)) else None

    saved = executable((cfg or {}).get('codex_bin'))
    if saved:
        return saved
    candidates = [shutil.which('codex')] + [os.path.join(d, 'codex') for d in CODEX_DIRS]
    if sys.platform == 'darwin':
        candidates += [os.path.join(d, app + '.app', 'Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex')
                       for d in CODEX_APP_DIRS for app in ('Codex', 'ChatGPT')]
    return next((b for path in candidates if (b := executable(path))), None)


def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if WINDOWS:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def no_window_kwargs():
    """Windows: run a console program without a console window. The router runs under
    pythonw.exe (no console), so each console child would otherwise open its own window
    that flashes up and steals keyboard focus."""
    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def popen_group_kwargs():
    """Start a child in its own process group so we can kill it with its children."""
    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def kill_tree(p):
    """Kill a Popen and everything it spawned (codex is a node wrapper around a native binary)."""
    try:
        if WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
        else:
            os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        pass


def terminate(pid):
    """Ask a process to stop (POSIX SIGTERM; on Windows a hard stop, use drain files instead)."""
    try:
        if WINDOWS:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        else:
            os.kill(int(pid), signal.SIGTERM)
    except (OSError, ValueError):
        pass


def https_certs():
    """python.org's macOS Python ships without root certificates until "Install Certificates" is run:
    every HTTPS call then fails (CERTIFICATE_VERIFY_FAILED), and the updater can never fetch its fix.
    If Python has none, use certifi or macOS's own root store, for every urlopen in this process."""
    import ssl, urllib.request
    paths = ssl.get_default_verify_paths()
    if (paths.cafile and os.path.exists(paths.cafile)) or (paths.capath and os.path.isdir(paths.capath)
                                                         and os.listdir(paths.capath)):
        return False
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except Exception:
        if sys.platform != "darwin":
            return False
        pem = subprocess.run(["security", "find-certificate", "-a", "-p",
                              "/System/Library/Keychains/SystemRootCertificates.keychain"],
                             capture_output=True, text=True, timeout=30).stdout
        if "BEGIN CERTIFICATE" not in pem:
            return False
        ctx.load_verify_locations(cadata=pem)
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx)))
    return True
