"""Long-running service process (started by launchd/systemd).

Runs one HTTP worker at a time (the worker also runs the watchdog, so reloads update it). On a reload request (`codex-os3 reload`,
the UI, an upgrade: a reload.request file, or SIGHUP) it starts a new worker first, waits
until it answers /health, then asks the old worker to stop accepting and drain its
in-flight requests. Both bind the port with SO_REUSEPORT, so OS3's tunnel never sees a
refused connection. Windows has no SO_REUSEPORT: there the old worker stops listening
first and the new one starts right after (a gap of about a second)."""
import json, os, signal, socket, subprocess, sys, threading, time, urllib.request

from . import config, engine, platform_util, sleep_control, store, update_progress, updater

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
            with urllib.request.urlopen(f"http://{host}:{cfg['port']}/health?ready=1", timeout=2) as r:
                health = json.loads(r.read())
                if health.get("status") == "ok" and health.get("pid") == pid:
                    progress = update_progress.get()
                    if progress.get("state") == "switching":
                        ver = updater.ver
                        if ver(health.get("version", "")) < ver(progress["tag"]):
                            time.sleep(0.3)
                            continue
                    update_progress.confirm_running(health.get("version", ""))
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def _recover_failed_update():
    """Restore checked files before starting a fallback Windows worker."""
    if update_progress.get().get("state") != "switching":
        return
    message = "The updated router did not pass its startup health check."
    try:
        updater.restore_previous()
        message += " The previous files were restored; restarting the previous version."
    except Exception as e:
        message += f" Could not restore the previous files: {e}"[:200]
    update_progress.fail(message)
    store.event("update_failed", message, source="supervisor", level="error")


def already_running(cfg):
    """Another supervisor is alive and its router answers (e.g. a cron keep-alive started a second
    copy). POSIX only: signal 0 means something else on Windows, where Task Scheduler runs one copy."""
    try:
        with open(PIDFILE) as f:
            pid = int(f.read().strip())
        if os.name == "nt" or pid == os.getpid():
            return False
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    host = "127.0.0.1" if cfg["bind"] in ("0.0.0.0", "::") else cfg["bind"]
    try:
        with urllib.request.urlopen(f"http://{host}:{cfg['port']}/health", timeout=3) as r:
            return json.loads(r.read()).get("status") == "ok"
    except Exception:
        return False


def run():
    cfg = config.ensure_key()
    if already_running(cfg):
        print("os3-router is already running: not starting a second copy", flush=True)
        return
    os.makedirs(config.HOME, exist_ok=True)
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    stop, reload_req = threading.Event(), threading.Event()
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, lambda *_: reload_req.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    worker = _spawn()
    if update_progress.get().get("state") == "switching":
        if not _healthy(cfg, worker.pid):
            worker.kill()
            worker.wait(timeout=10)
            _recover_failed_update()
            worker = _spawn()
    started, misses, last_check, crashes = time.time(), 0, 0.0, 0
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
            elif REUSEPORT:
                new.kill()
                new.wait(timeout=10)
                _recover_failed_update()
                store.event("reload_failed", "new worker never became healthy; kept the old one",
                            source="supervisor", level="error")
            else:  # Windows: the old one already stopped listening, so keeping it would leave nobody on the port
                new.kill()
                new.wait(timeout=10)
                _recover_failed_update()
                draining.append(worker)
                worker = _spawn()
                store.event("reload_failed", f"new worker never became healthy; started another ({worker.pid})",
                            source="supervisor", level="error")
            started, misses = time.time(), 0
        elif worker.poll() is not None:  # crashed: restart it
            # one that keeps dying at once (its port taken, a broken install) is retried ever more slowly,
            # not every 2 s for ever: that filled the events table and the log
            crashes = crashes + 1 if time.time() - started < 30 else 1
            wait = min(2 * 2 ** (crashes - 1), 60)
            if crashes <= 5 or crashes % 10 == 0:
                store.event("worker_crash", f"worker exited with {worker.returncode} ({crashes} in a row); restarting in {wait}s",
                            source="supervisor", level="error")
            stop.wait(wait)
            worker = _spawn()
            started, misses = time.time(), 0
        elif time.time() - started > 60 and time.time() - last_check > 15:
            # running but not answering (e.g. stuck after a failed swap): the app shows "refused to connect"
            last_check = time.time()
            misses = 0 if _healthy(config.load(), worker.pid, timeout=5) else misses + 1
            if misses >= 3:
                store.event("worker_unresponsive", f"worker {worker.pid} stopped answering; restarting it",
                            source="supervisor", level="error")
                # Windows: with its codex children (taskkill /T); elsewhere the worker isn't a group leader
                platform_util.kill_tree(worker) if os.name == "nt" else worker.kill()
                if not REUSEPORT:
                    time.sleep(1.5)
                worker = _spawn()
                started, misses = time.time(), 0
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
