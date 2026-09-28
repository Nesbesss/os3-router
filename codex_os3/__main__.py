"""os3-router command line.

  serve              run the service (supervisor + worker + watchdog); used by launchd/systemd
  worker             run one HTTP worker (started by the supervisor)
  reload             swap in a fresh worker without dropping requests (after an upgrade)
  status             service, rabbit-agent and subscription limits at a glance
  doctor             setup checks
  key [--rotate]     print (or rotate) the API key for OS3
  setup-info         the values to paste into OS3
  export <task>      write a task's log export zip to the current directory
  wait-for-os3       block until OS3 sends its first request (used by the installer)
"""
import json, sys, time

from . import __version__, config


def _log_without_console():
    """Under pythonw (Windows service) there is no stdout: send output to service.log."""
    if sys.stdout is None or sys.stderr is None:
        import os
        os.makedirs(config.HOME, exist_ok=True)
        f = open(os.path.join(config.HOME, "service.log"), "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stdout or f
        sys.stderr = sys.stderr or f


def main(argv):
    for stream in (sys.stdout, sys.stderr):  # Windows consoles (cp1252) can't print ✓ → etc.
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    cmd = argv[0] if argv else "status"
    try:
        from .platform_util import https_certs
        https_certs()
    except Exception:  # never block startup over certificates
        pass
    if cmd in ("serve", "worker"):
        _log_without_console()
    if cmd == "serve":
        from . import supervisor
        return supervisor.run()
    if cmd == "worker":
        from . import server
        return server.serve_worker()
    if cmd == "reload":
        from . import ui_api
        if not ui_api._supervisor_pid():
            print("service not running")
            return 1
        ui_api.request_reload()
        print("reload requested")
        return 0
    if cmd in ("preflight", "verify"):  # used by the installers
        from . import preflight
        return getattr(preflight, cmd)(argv[1:])
    if cmd == "keepalive":  # OS3 node without the router: keep its rabbit-agent connected
        from . import watchdog
        watchdog.keepalive()
        return 0
    if cmd == "update":  # check for a new release now instead of within 6 h
        from . import store, updater
        if not updater.managed():
            print("not an installer-made install (e.g. a git checkout): update with git pull")
            return 1
        store.kv_set("update_checked", 0)
        print("update check requested; see the dashboard's Watchdog events")
        return 0
    if cmd == "key":
        cfg = config.save({"api_key": config.new_key()}) if "--rotate" in argv else config.ensure_key()
        print(cfg["api_key"])
        return 0
    if cmd == "setup-info":
        cfg = config.ensure_key()
        print(f"""
     In OS3: Settings → API keys → add a provider "local" with:
       device          this machine
       endpoint        http://127.1:{cfg['port']}/v1   (not localhost: rabbit's firewall blocks that)
       model id        {cfg['model']}
       api key         {cfg['api_key']}
       context window  200000   (advanced)

     Dashboard: http://localhost:{cfg['port']}/  (the app's setup page walks you through it)""")
        return 0
    if cmd == "doctor":
        from . import ui_api
        bad = 0
        for c in ui_api.doctor(config.load()):
            print(f"  {'✓' if c['ok'] else '✗'} {c['check']}: {c['detail']}")
            bad += not c["ok"]
        return 1 if bad else 0
    if cmd == "status":
        from . import os3, store
        from . import ui_api
        print(f"os3-router {__version__}  service: {'running' if ui_api._supervisor_pid() else 'stopped'}")
        a = os3.status()
        print(f"rabbit-agent: {a['status']} (pid {a.get('pid')})" if a else "rabbit-agent: not installed on this machine")
        for b, l in store.latest_limits().items():
            wins = [f"{store.win_label(l[w], d)}: {l[k]}% used" for k, w, d in (("p_pct", "p_window", "5-hour"), ("s_pct", "s_window", "weekly"))
                    if l[k] is not None]
            print(f"{b.title()} " + (" · ".join(wins) or "no limits reported yet"))
        return 0
    if cmd == "export" and len(argv) > 1:
        from . import export
        name = f"os3-router-{argv[1]}.zip"
        with open(name, "wb") as f:
            f.write(export.build(argv[1]))
        print(name)
        return 0
    if cmd == "wait-for-os3":
        from . import store
        since = time.time()
        timeout = float(argv[1]) if len(argv) > 1 else 1800
        while time.time() - since < timeout:
            r = store.q("SELECT ts, model FROM requests WHERE ts > ? ORDER BY ts LIMIT 1", (since,))
            if r:
                print(json.dumps(r[0]))
                return 0
            time.sleep(2)
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
