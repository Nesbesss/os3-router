"""Automatic updates from GitHub releases. Every 30 min the watchdog owner checks the latest
release; a newer one is downloaded, its own offline test suite must pass, then its files are
copied over the install (previous version kept in app.prev) and the service reloads without
downtime. Only for installs made by the installer (~/.codex-os3/app); off with auto_update=false."""
import io, json, os, plistlib, shutil, subprocess, sys, tarfile, tempfile, time, urllib.request

from . import __version__, config, store

REPO = "Nesbesss/os3-router"
EVERY_S = 1800
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ver(v):
    try:
        return tuple(int(x) for x in str(v).lstrip("v").split("-")[0].split(".")[:3])
    except ValueError:
        return (0,)


def _get(url, timeout=60):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "os3-router/" + __version__}),
                                  timeout=timeout).read()


def latest():
    return json.loads(_get(f"https://api.github.com/repos/{REPO}/releases/latest", 20))["tag_name"]


def managed():
    """True for installer-made installs; dev checkouts and test copies never update themselves."""
    return os.path.normcase(os.path.realpath(APP)) == os.path.normcase(os.path.realpath(os.path.join(config.HOME, "app")))


def install(tag, app=APP, run_tests=True):
    tmp = tempfile.mkdtemp(prefix="os3-router-update-")
    try:
        with tarfile.open(fileobj=io.BytesIO(_get(f"https://codeload.github.com/{REPO}/tar.gz/refs/tags/{tag}"))) as t:
            safe = [m for m in t.getmembers() if not (m.name.startswith(("/", "\\")) or ".." in m.name.split("/")
                                                      or m.issym() or m.islnk())]
            t.extractall(tmp, safe)
        src = os.path.join(tmp, os.listdir(tmp)[0])
        with open(os.path.join(src, "codex_os3", "__init__.py")) as f:
            if f'"{tag.lstrip("v")}"' not in f.read():
                raise RuntimeError(f"release {tag} does not contain version {tag.lstrip('v')}")
        if run_tests:  # the new version must pass its own offline tests on this machine
            env = {k: v for k, v in os.environ.items() if not k.startswith("CODEX_OS3_")}
            r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=src, env=env,
                               capture_output=True, text=True, timeout=900)
            if r.returncode:
                raise RuntimeError("new version failed its tests: " + (r.stderr or r.stdout)[-400:])
        prev = app + ".prev"
        shutil.rmtree(prev, ignore_errors=True)
        shutil.copytree(app, prev)
        shutil.copytree(src, app, dirs_exist_ok=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    open(os.path.join(config.HOME, "reload.request"), "w").close()  # zero-downtime switch


def update_apps(tag):
    """The companion apps: the macOS menu bar app is a separate download; the Windows tray runs
    tray.ps1 from the updated files and only needs a restart. Failures never fail the update."""
    if sys.platform == "darwin":
        apps = os.path.expanduser("~/Applications")
        old = [os.path.join(apps, n) for n in ("OS3 Router.app", "Codex OS3.app") if os.path.isdir(os.path.join(apps, n))]
        if not old:
            return  # installed with --no-app
        try:  # unchanged app: keep it (a new unsigned build has to be approved in Privacy & Security again)
            with open(os.path.join(APP, "app", "macos", "VERSION")) as f:
                want = f.read().strip()
            with open(os.path.join(old[0], "Contents", "Info.plist"), "rb") as f:
                have = plistlib.load(f).get("CFBundleShortVersionString")
            if have == want and old[0].endswith("OS3 Router.app"):
                return
        except (OSError, ValueError):
            pass
        tmp = tempfile.mkdtemp(prefix="os3-router-app-")
        try:
            z = os.path.join(tmp, "app.zip")
            with open(z, "wb") as f:
                f.write(_get(f"https://github.com/{REPO}/releases/download/{tag}/OS3Router-macos.zip", 120))
            subprocess.run(["ditto", "-x", "-k", z, tmp], check=True, timeout=120)
            new = os.path.join(tmp, "OS3 Router.app")
            if not os.path.isdir(new):
                raise RuntimeError("release zip has no OS3 Router.app")
            subprocess.run(["pkill", "-f", "Contents/MacOS/CodexOS3"], timeout=10)
            for a in old:
                shutil.rmtree(a, ignore_errors=True)
            dest = os.path.join(apps, "OS3 Router.app")
            shutil.move(new, dest)
            # mark it downloaded, so macOS offers "Open Anyway" instead of silently refusing to start it
            subprocess.run(["xattr", "-w", "com.apple.quarantine", f"0083;{int(time.time()):x};os3-router;", dest], timeout=10)
            subprocess.run(["open", dest], timeout=30)
            from .notify import desktop
            desktop("The OS3 Router app was updated. If macOS blocks it: System Settings → Privacy & Security → Open Anyway.",
                    key="app-updated")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    elif sys.platform == "win32":  # Start menu entry (installs updated from before 0.4.1 lack it), new tray
        subprocess.run(["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File",
                        os.path.join(APP, "app", "windows", "shortcut.ps1")], capture_output=True, timeout=120,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for a in ("/End", "/Run"):
            subprocess.run(["schtasks", a, "/TN", "codex-os3 tray"], capture_output=True, timeout=30)
    elif sys.platform.startswith("linux"):  # app-menu entry for the OS3 Router app window
        d = os.path.expanduser("~/.local/share/applications")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "os3-router.desktop"), "w") as f:
            f.write(DESKTOP.format(app=APP))


DESKTOP = """[Desktop Entry]
Type=Application
Name=OS3 Router
Comment=Status, limits and models of your OS3 router
Exec=sh "{app}/app/linux/os3-router-app"
Icon={app}/codex_os3/ui/guide/app-icon.png
Categories=Utility;Network;
StartupWMClass=os3-router
"""


def maybe(cfg):
    """Called from the watchdog loop (one owner at a time)."""
    if not cfg.get("auto_update", True) or not managed():
        return
    if store.kv_get("codex_outdated"):  # Codex refused a model as too old: update it right away
        from . import selffix
        store.kv_set("codex_outdated", None)
        ok, msg = selffix.update_codex(cfg)
        store.event("codex_update", msg, source="updater", level="info" if ok else "warn")
    if store.kv_get("apps_version") != __version__:  # first run of this version: bring the apps along
        store.kv_set("apps_version", __version__)  # (done by the new version, whatever did the update)
        try:
            update_apps("v" + __version__)
            store.event("update", f"menu bar / tray app updated to {__version__}", source="updater")
        except Exception as e:
            store.event("update_failed", f"menu bar / tray app: {type(e).__name__}: {e}"[:300], source="updater", level="warn")
    if time.time() - (store.kv_get("update_checked") or 0) < EVERY_S:
        return
    store.kv_set("update_checked", time.time())
    try:
        tag = latest()
        store.kv_set("update_latest", tag)
        from . import selffix, ui_api
        v = ui_api.codex_info(cfg, fresh=True)["version"]
        if v and ver(v) < ver(ui_api.MIN_CODEX):
            ok, msg = selffix.update_codex(cfg)
            store.event("codex_update", msg, source="updater", level="info" if ok else "warn")
        if ver(tag) <= ver(__version__):
            return
        store.event("update", f"installing {tag} (running {__version__})", source="updater")
        install(tag)
        store.event("update", f"{tag} installed, switching over", source="updater")
    except Exception as e:  # offline, GitHub down, tests failed: stay on this version
        store.event("update_failed", f"{type(e).__name__}: {e}"[:300], source="updater", level="warn")
