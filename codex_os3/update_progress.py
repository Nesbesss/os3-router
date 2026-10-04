"""Durable update status shared by the worker, supervisor and Windows progress window."""
import json, os, subprocess, tempfile, time

from . import config, store

ACTIVE = ("installing", "switching")


def get():
    return store.kv_get("update_progress") or {}


def set_state(state, tag, phase, message, **extra):
    previous = get()
    now = time.time()
    value = dict(state=state, tag=tag, phase=phase, message=message, updated=now,
                 started=previous.get("started", now) if previous.get("tag") == tag and previous.get("state") in ("queued", *ACTIVE) and state != "queued" else now,
                 pid=os.getpid(), **extra)
    store.kv_set("update_progress", value)
    # Outside the app directory: the window keeps reading through file replacement and restart.
    # Atomic replace means PowerShell never sees half a JSON document. No keys/logins are included.
    path = os.path.join(config.HOME, "update-progress.json")
    tmp = None
    try:
        os.makedirs(config.HOME, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=config.HOME, delete=False) as f:
            tmp = f.name
            json.dump(value, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        store.event("update_window_failed", f"Could not write update progress: {e}"[:300], source="updater", level="warn")
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return value


def open_window(app):
    if os.name != "nt":
        return
    try:
        subprocess.Popen(["powershell.exe", "-NoProfile", "-STA", "-WindowStyle", "Hidden",
                          "-ExecutionPolicy", "Bypass", "-File",
                          os.path.join(app, "app", "windows", "update-window.ps1")],
                         env=dict(os.environ, CODEX_OS3_HOME=config.HOME),
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as e:
        store.event("update_window_failed", f"Could not open the update window: {e}"[:300], source="updater", level="warn")


def confirm_running(version):
    """Called only after the supervisor receives /health from the replacement worker."""
    from .updater import ver
    value = get()
    if value.get("state") == "switching" and ver(version) >= ver(value["tag"]):
        set_state("installed", value["tag"], "done", f"OS3 Router {version} is running.")
        manual = store.kv_get("manual_update") or {}
        if manual.get("tag") == value["tag"]:
            store.kv_set("manual_update", {"state": "installed", "tag": value["tag"]})


def fail(message, **extra):
    value = get()
    if value.get("state") not in ACTIVE:
        return
    set_state("failed", value["tag"], "failed", message, error=message, **extra)
    manual = store.kv_get("manual_update") or {}
    if manual.get("tag") == value["tag"]:
        store.kv_set("manual_update", {"state": "failed", "tag": value["tag"], "error": message})



def confirm_worker(cfg):
    """Confirm this worker over HTTP independently of watchdog work or lease ownership.

    Older supervisors stay loaded through reloads. A newly loaded worker must
    confirm activation even when its supervisor predates progress support.
    """
    if get().get("state") != "switching":
        return
    from . import supervisor
    supervisor._healthy(cfg, os.getpid())
