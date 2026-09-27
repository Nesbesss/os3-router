"""Long-running service process (started by launchd/systemd).

Runs one HTTP worker at a time (the worker also runs the watchdog, so reloads update it). On a reload request (`codex-os3 reload`,
the UI, an upgrade: a reload.request file, or SIGHUP) it starts a new worker first, waits
until it answers /health, then asks the old worker to stop accepting and drain its
in-flight requests. Both bind the port with SO_REUSEPORT, so OS3's tunnel never sees a
refused connection. Windows has no SO_REUSEPORT: there the old worker stops listening
first and the new one starts right after (a gap of about a second)."""
import json, os, signal, socket, subprocess, sys, threading, time, urllib.request

from . import config, engine, sleep_control, store

PIDFILE = os.path.join(config.HOME, "supervisor.pid")
RELOAD_FILE = os.path.join(config.HOME, "reload.request")
REUSEPORT = hasattr(socket, "SO_REUSEPORT")


def drain(p):
    """Ask a worker to stop accepting and finish its requests (file-based: works on Windows)."""
    open(os.path.join(config.HOME, f"drain-{p.pid}"), "w").close()


def _spawn():
    env = dict(os.environ, CODEX_OS3_SUPERVISOR=str(os.getpid()))
    return subprocess.Popen([sys.executable, "-m", "codex_os3", "worker"], env=env,
                            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _healthy(cfg, pid, timeout=40):
    """True once the worker with this pid answers /health (during a swap both workers
    listen on the port, so a plain 200 could come from the old one)."""
    host = "127.0.0.1" if cfg["bind"] in ("0.0.0.0", "::") else cfg["bind"]
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://{host}:{cfg['port']}/health", timeout=2) as r:
                if json.loads(r.read()).get("pid") == pid:
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def run():
    config.ensure_key()
    os.makedirs(config.HOME, exist_ok=True)
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    stop, reload_req = threading.Event(), threading.Event()
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, lambda *_: reload_req.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    worker = _spawn()
    engine.log(f"supervisor {os.getpid()} started worker {worker.pid}")
    store.event("service_start", f"supervisor {os.getpid()}", source="supervisor")
    draining = []
    sleep_guard = sleep_control.IdleSleepInhibitor()
    sleep_error = None
    while not stop.is_set():
        stop.wait(1)
        try:
            sleep_guard.sync(bool(config.load()["no_sleep"]))
            sleep_error = None
        except OSError as exc:
            if str(exc) != sleep_error:
                store.event("sleep_prevention_failed", str(exc), source="supervisor", level="error")
                sleep_error = str(exc)
        draining = [p for p in draining if p.poll() is None]
        if os.path.exists(RELOAD_FILE):
            os.unlink(RELOAD_FILE)
            reload_req.set()
        if reload_req.is_set():
            reload_req.clear()
            if not REUSEPORT:
                drain(worker)
                time.sleep(1.5)  # let it close its listening socket
            new = _spawn()
            if _healthy(config.load(), new.pid):
                if REUSEPORT:
                    drain(worker)  # old one stops accepting, finishes its requests
                draining.append(worker)
                worker = new
                engine.log(f"reloaded: new worker {new.pid}, draining old")
                store.event("reload", f"worker swapped to {new.pid}", source="supervisor")
            else:
                new.kill()
                store.event("reload_failed", "new worker never became healthy; kept the old one",
                            source="supervisor", level="error")
        elif worker.poll() is not None:  # crashed: restart it
            store.event("worker_crash", f"worker exited with {worker.returncode}; restarting",
                        source="supervisor", level="error")
            time.sleep(2)
            worker = _spawn()
    for p in [worker] + draining:
        if p.poll() is None:
            drain(p)
    for p in [worker] + draining:
        try:
            p.wait(timeout=900)
        except subprocess.TimeoutExpired:
            p.kill()
    try:
        sleep_guard.close()
    except OSError as exc:
        store.event("sleep_prevention_failed", str(exc), source="supervisor", level="error")
    try:
        os.unlink(PIDFILE)
    except OSError:
        pass
