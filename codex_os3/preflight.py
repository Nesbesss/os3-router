"""Install-time checks shared by install.sh and install.ps1, so the logic is written (and tested) once:
which port the router can really listen on, where Claude Code is, and whether the *running service*
(not the installer's own shell) can see Codex and Claude. Only the Codex and Claude checks are asked for:
the rabbit-agent ones can't pass before OS3 is connected."""
import json, os, socket, time, urllib.request

from . import config, platform_util, roles


def can_listen(host, port):
    """Really try to listen. Connecting to the port isn't enough: Windows reserves whole port ranges
    (Hyper-V, WSL) that refuse a bind while nobody answers on them."""
    s = socket.socket()
    try:
        if not platform_util.WINDOWS:  # (on Windows SO_REUSEADDR would let us share a port that is taken)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def is_ours(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            return json.load(r).get("status") == "ok"
    except (OSError, ValueError):
        return False


def choose_port(cfg, wanted=None, span=50):
    """-> the port to use. The router's own port stays while our router is on it (an upgrade); else the
    first one from `wanted` (or the configured one) upwards that can be listened on. None = none found."""
    start = wanted or cfg["port"]
    for p in range(start, start + span):
        if is_ours(p) or can_listen(cfg["bind"], p):
            return p
    return None


def claude_found(cfg):
    """Claude Code's path by every way the router itself looks (PATH, install folders, the user's shell)."""
    p = roles.claude_path(cfg)
    return p if p and os.path.isfile(p) else None


def preflight(argv):
    """`preflight [port]`: pick a usable port and remember where Claude Code is. Exit 1 if the asked-for port is taken."""
    cfg = config.load()
    wanted = int(argv[0]) if argv else None
    p = choose_port(cfg, wanted, span=1 if wanted else 50)
    if p is None:
        print(f"     ! port {wanted} can't be used (another program has it, or Windows reserved it): pick another with --port")
        return 1
    if p != cfg["port"]:
        config.save({"port": p})
        if not wanted:
            print(f"     ! port {cfg['port']} can't be used by the router: it uses {p}")
    c = claude_found(cfg)
    if c and c != cfg.get("claude_bin"):
        config.save({"claude_bin": c})
    if c:
        print(f"     Claude Code found: {c}")
    return 0


def _call(cfg, path, post=False, timeout=60):  # (doctor runs several CLI checks: slow on a busy machine)
    req = urllib.request.Request(f"http://127.0.0.1:{cfg['port']}/api/{path}", method="POST" if post else "GET",
                                 data=b"{}" if post else None, headers={"X-Codex-OS3": "1", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def verify_checks(cfg, selftest=False, call=_call):
    """-> [(ok, text)]: what the running service sees. It runs in another environment than the installer's
    shell (launchd's PATH, a Task Scheduler logon), which is where past installs broke."""
    out = []
    try:
        models = call(cfg, "models")
        checks = [c for c in call(cfg, "doctor") if c["check"].startswith(("Codex", "Claude"))]
    except (OSError, ValueError) as e:
        return [(False, f"can't ask the router service: {e}")]
    out += [(c["ok"], f"{c['check']}: {c['detail']}") for c in checks]
    mine = claude_found(cfg)
    seen = any(m.get("backend") == "claude" for m in models)
    if mine and not seen:
        out.append((False, f"Claude Code is at {mine} but the background service doesn't list Claude models: "
                           "open Models in the app and paste that path"))
    elif seen:
        out.append((True, "Claude models are available"))
    if selftest:
        try:
            r = call(cfg, "selftest", post=True, timeout=240)
            out += [(x["ok"], f"{x['test']} ({x['model']}, {x['secs']}s): {x['detail']}") for x in r["results"]]
        except (OSError, ValueError, KeyError) as e:
            out.append((False, f"the test request failed: {e}"))
    return out


def verify(argv, sleep=time.sleep):
    cfg = config.load()
    results = verify_checks(cfg)
    for _ in range(3):  # right after an update the old worker may still answer while it drains: look again before saying so
        if all(ok for ok, _ in results):
            break
        sleep(6)
        results = verify_checks(cfg)
    if "--selftest" in argv and all(ok for ok, _ in results):
        results = verify_checks(cfg, selftest=True)
    bad = 0
    for ok, text in results:
        print(f"     {'✓' if ok else '✗'} {text}")
        bad += not ok
    return 1 if bad else 0
