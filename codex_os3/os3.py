"""rabbit OS3 specifics on this machine: the rabbit-agent's status, logs and restart."""
import calendar, json, os, platform, subprocess, time
from datetime import datetime

from .platform_util import WINDOWS, pid_alive, terminate

AGENT_DIR = os.path.expanduser("~/.rabbit-agent")
STATUS = os.path.join(AGENT_DIR, "runtime", "rabbit-agent.status.json")
LOG = os.path.join(AGENT_DIR, "logs", "agent.log")
MAC_JOB = "com.rabbit.rabbit-agent"


def installed():
    return os.path.isdir(AGENT_DIR)


def status():
    """{'status': 'connected', 'pid': .., 'version': .., 'nodeId': ..} or {} if unknown."""
    try:
        with open(STATUS) as f:
            s = json.load(f)
    except (OSError, ValueError):
        return {}
    pid = s.get("pid")
    s["running"] = pid_alive(pid)
    return s


def status_age():
    """Seconds in this status. Rabbit also rewrites the file during heartbeats."""
    try:
        changed = os.path.getmtime(STATUS)
    except OSError:
        return 0
    try:
        with open(STATUS) as f:
            updated = json.load(f).get("updatedAt")
        if isinstance(updated, str):
            stamp = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            if stamp.tzinfo is not None:
                changed = stamp.timestamp()
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        pass  # older or unreadable status files retain the mtime fallback
    return max(0, time.time() - changed)


def _tail(path, max_bytes):
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - max_bytes))
            return f.read().decode(errors="ignore").splitlines()
    except OSError:
        return []


def log_tail(since_ts=0, max_bytes=300_000, components=None):
    """Parsed agent log entries newer than since_ts (unix seconds). Reads the rotated
    agent.1.log too when the current file starts after since_ts."""
    lines = _tail(LOG, max_bytes)
    first = next((_ts(json.loads(x).get("timestamp", "")) for x in lines[1:2] if x.startswith("{")), 0)
    if since_ts and first > since_ts:
        lines = _tail(LOG.replace("agent.log", "agent.1.log"), max_bytes) + lines
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        ts = _ts(e.get("timestamp", ""))
        if ts < since_ts or (components and e.get("component") not in components):
            continue
        e["ts"] = ts
        out.append(e)
    return out


def _ts(iso):
    """agent.log timestamps are UTC ('2026-09-23T18:09:23.000Z')."""
    try:
        return calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0


def restart_agent():
    """Restart the rabbit-agent through its own scheduler so it keeps its identity and, on
    macOS, its Accessibility/Screen Recording grants (a process started by us or over SSH
    would lose them). Returns (ok, message)."""
    s = status()
    if not installed():
        return False, "rabbit-agent not installed"
    if pid_alive(s.get("pid")):
        terminate(s["pid"])
        for _ in range(30):
            if not pid_alive(s["pid"]):
                break
            time.sleep(1)
    if platform.system() == "Darwin":
        r = subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{MAC_JOB}"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return False, f"launchctl kickstart failed: {(r.stderr or r.stdout).strip()}"
    elif WINDOWS:  # best effort: rabbit's Windows scheduler task, else its installer
        r = subprocess.run(["schtasks", "/Run", "/TN", "rabbit-agent"], capture_output=True, text=True)
        ps1 = os.path.join(AGENT_DIR, "install.ps1")
        if r.returncode != 0 and os.path.exists(ps1):
            r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1],
                               capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            return False, "could not restart the rabbit-agent on Windows (untested path): " + (r.stderr or r.stdout)[-200:]
    else:  # Linux: rabbit's installer (run by its cron job) (re)starts the agent
        r = subprocess.run(["bash", os.path.join(AGENT_DIR, "install.sh")], capture_output=True,
                           text=True, timeout=300)
        if r.returncode != 0:
            return False, f"install.sh failed: {r.stderr.strip()[-300:]}"
    for _ in range(60):
        n = status()
        if n.get("running") and n.get("pid") != s.get("pid") and n.get("status") == "connected":
            return True, f"rabbit-agent restarted (pid {n['pid']})"
        time.sleep(1)
    return False, "rabbit-agent did not come back as connected within 60s"
