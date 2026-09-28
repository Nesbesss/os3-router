"""Automatic updates from GitHub releases. Every 30 min the watchdog owner checks the latest
release; a newer one is downloaded, its own offline test suite must pass, then its files are
copied over the install (previous version kept in app.prev) and the service reloads without
downtime. Only for installs made by the installer (~/.codex-os3/app); off with auto_update=false."""
import io, json, os, plistlib, shutil, subprocess, sys, tarfile, tempfile, time, urllib.error, urllib.parse, urllib.request

from . import __version__, config, store

REPO = "Nesbesss/os3-router"
EVERY_S = 1800
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ver(v):
    try:
        return tuple(int(x) for x in str(v).lstrip("v").split("-")[0].split(".")[:3])
    except ValueError:
        return (0,)


def _request(url, timeout=60, method="GET"):
    request = urllib.request.Request(url, headers={"User-Agent": "os3-router/" + __version__}, method=method)
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
        try:
            return (response.read() if method != "HEAD" else b""), response.geturl()
        finally:
            response.close()
    except urllib.error.URLError as e:
        # Python without usable root certificates (python.org macOS builds; Windows only has the roots
        # already installed). curl uses the OS's own trust (Windows fetches missing roots itself).
        curl = shutil.which("curl.exe" if os.name == "nt" else "curl")
        if "CERTIFICATE_VERIFY_FAILED" not in str(e) or not curl:
            raise
        with tempfile.TemporaryDirectory(prefix="os3-router-http-") as tmp:
            output = os.path.join(tmp, "response")
            command = [curl, "-sSL", "--max-time", str(timeout), "-A", "os3-router/" + __version__,
                       "--output", output, "--write-out", "%{http_code}\n%{url_effective}"]
            if method == "HEAD":
                command.append("--head")
            r = subprocess.run(command + [url], capture_output=True, timeout=timeout + 10)
            if r.returncode:
                raise RuntimeError(f"curl: {r.stderr.decode(errors='replace').strip()[:200]}") from e
            status_text, final_url = r.stdout.decode(errors="replace").split("\n", 1)
            status = int(status_text)
            if status >= 400:
                raise urllib.error.HTTPError(url, status, f"HTTP {status}", {}, None)
            if method == "HEAD":
                return b"", final_url.strip()
            with open(output, "rb") as response_file:
                return response_file.read(), final_url.strip()


def _get(url, timeout=60):
    return _request(url, timeout)[0]


def latest():
    try:
        return json.loads(_get(f"https://api.github.com/repos/{REPO}/releases/latest", 20))["tag_name"]
    except urllib.error.HTTPError as e:
        if e.code != 403:
            raise
        # Unauthenticated GitHub API checks can exhaust the shared IP's hourly quota.
        # The public latest-release redirect is not subject to that API quota.
        url = f"https://github.com/{REPO}/releases/latest"
        _, final_url = _request(url, timeout=20, method="HEAD")
        final = urllib.parse.urlparse(final_url)
        prefix = f"/{REPO}/releases/tag/"
        if final.scheme != "https" or final.netloc != "github.com" or not final.path.startswith(prefix):
            raise ValueError(f"GitHub latest release did not redirect to a tag: {final.geturl()}")
        tag = urllib.parse.unquote(final.path[len(prefix):])
        if not tag or "/" in tag or ver(tag) == (0,):
            raise ValueError(f"GitHub latest release returned an invalid tag: {tag}")
        return tag


def managed():
    """True for installer-made installs; dev checkouts and test copies never update themselves."""
    return os.path.normcase(os.path.realpath(APP)) == os.path.normcase(os.path.realpath(os.path.join(config.HOME, "app")))


def manual_state():
    """A requested update is installed only when this running version confirms it."""
    state = store.kv_get("manual_update") or {}
    if state.get("tag") and ver(__version__) >= ver(state["tag"]):
        return {"state": "installed", "tag": state["tag"]}
    return state


def update_status(cfg):
    tag = store.kv_get("update_latest")
    return {"managed": managed(), "current": __version__, "latest": tag,
            "available": bool(tag and ver(tag) > ver(__version__)),
            "automatic": bool(cfg.get("auto_update", True)), "manual": manual_state()}


def check_now(cfg):
    """Check GitHub now without changing the automatic check interval."""
    if not managed():
        raise ValueError("Updates in Settings require an installer-made installation. Update this checkout with git.")
    tag = latest()
    store.kv_set("update_latest", tag)
    return update_status(cfg)


def request_update(cfg):
    """Queue the last discovered newer release for the watchdog, even if auto updates are off."""
    if not managed():
        raise ValueError("Updates in Settings require an installer-made installation.")
    tag = store.kv_get("update_latest")
    if not tag or ver(tag) <= ver(__version__):
        raise ValueError("No newer release is known. Choose Find new updates first.")
    state = manual_state().get("state")
    if state in ("queued", "installing", "switching"):
        raise ValueError("An update is already in progress.")
    store.kv_set("manual_update", {"state": "queued", "tag": tag})
    store.event("update", f"manual update to {tag} requested", source="ui")
    return update_status(cfg)


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


def _update_codex(cfg):
    """selffix.update_codex, but after a failure not again for 6 hours: a broken npm was retried (and logged)
    at every check, every 30 minutes, for days."""
    from . import selffix
    if time.time() - (store.kv_get("codex_update_failed") or 0) < 6 * 3600:
        return
    ok, msg = selffix.update_codex(cfg)
    store.kv_set("codex_update_failed", 0 if ok else time.time())
    store.event("codex_update", msg, source="updater", level="info" if ok else "warn")


def maybe(cfg):
    """Called from the watchdog loop (one owner at a time)."""
    if not managed():
        return
    manual = store.kv_get("manual_update") or {}
    if manual.get("state") == "queued":
        tag = manual["tag"]
        if ver(tag) <= ver(__version__):
            store.kv_set("manual_update", {"state": "installed", "tag": tag})
            return
        store.kv_set("manual_update", {"state": "installing", "tag": tag})
        store.kv_set("update_checked", time.time())  # avoid a second automatic install while switching
        try:
            store.event("update", f"manually installing {tag}", source="updater")
            install(tag)
            store.kv_set("manual_update", {"state": "switching", "tag": tag})
            store.event("update", f"{tag} installed on disk, switching over", source="updater")
        except Exception as e:
            store.kv_set("manual_update", {"state": "failed", "tag": tag,
                                           "error": f"{type(e).__name__}: {e}"[:300]})
            store.event("update_failed", f"manual {tag}: {type(e).__name__}: {e}"[:300],
                        source="updater", level="warn")
        return
    if not cfg.get("auto_update", True):
        return
    if store.kv_get("codex_outdated"):  # Codex refused a model as too old: update it right away
        store.kv_set("codex_outdated", None)
        _update_codex(cfg)
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
        from . import ui_api
        v = ui_api.codex_info(cfg, fresh=True)["version"]
        if v and ver(v) < ver(ui_api.MIN_CODEX):
            _update_codex(cfg)
        if ver(tag) <= ver(__version__):
            return
        store.event("update", f"installing {tag} (running {__version__})", source="updater")
        install(tag)
        store.event("update", f"{tag} installed, switching over", source="updater")
    except Exception as e:  # offline, GitHub down, tests failed: stay on this version
        # a dropped connection (Wi-Fi, sleep) is not a problem to show; a refused download or a failed test is
        blip = isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError)) and not isinstance(e, urllib.error.HTTPError)
        store.event("update_failed", f"{type(e).__name__}: {e}"[:300], source="updater", level="info" if blip else "warn")
