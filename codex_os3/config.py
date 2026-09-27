"""Settings live in ~/.codex-os3/config.json; env vars override (CODEX_OS3_<KEY>)."""
import json, os, secrets, threading

HOME = os.path.expanduser(os.environ.get("CODEX_OS3_HOME", "~/.codex-os3"))
PATH = os.path.join(HOME, "config.json")

DEFAULTS = {
    "port": 11435,            # 11434 is Ollama's
    "bind": "127.0.0.1",      # rabbit relays via the local rabbit-agent; set 0.0.0.0 for remote use
    "api_key": "",
    "model": "gpt-6-luna",
    "models": ["gpt-6-luna", "gpt-6-sol", "gpt-6-astra"],  # extra ids for /v1/models; the rest comes from Codex
    "effort": "medium",
    "role_routing": True,     # pick the model per OS3 role instead of the one OS3 sends
    "roles": {
        "chat": {"model": "gpt-6-luna", "effort": "medium"},
        "worker": {"model": "gpt-6-sol", "effort": "medium"},
        "background": {"model": "gpt-6-luna", "effort": "low"},
    },
    "codex_bin": "",          # absolute path to codex (services often lack the user's PATH)
    "claude_bin": "",         # same for Claude Code (only needed when a role uses a Claude model)
    "max_codex": 3,           # concurrent codex processes
    "max_images": 2,          # newest screenshots attached per turn
    "hang_idle_s": 90,        # no codex output/rollout growth for this long = hung upstream
    "hang_max_s": 600,
    "captures": False,        # store full request/response bodies (contain screenshots + secrets)
    "retention_days": 7,
    "jev_key": "",            # optional TypeSafe key: smarter watchdog
    "webhook": "",            # optional ntfy/Telegram-style URL for watchdog alerts
    "watchdog": True,
    "restart_agent": True,    # watchdog may restart a stuck rabbit-agent
    "auto_update": True,      # install new releases from GitHub (tested first, zero downtime)
    "codex_images": True,     # workers create images with Codex's own image generation (no paid image provider)
    "engine": "exec",         # "appserver" (prototype): one long-running codex instead of one per request
    "stream_chat": False,     # prototype, appserver only: stream the main chat's answer to OS3 as it's written
    "compact_tokens": 0,      # prototype: codex summarizes a task's history above this many tokens (0 = its default)
    "fallback": {},           # role -> {model, effort} used while the role's subscription is at its limit
    "share_reports": None,    # anonymous problem reports to the developer: None = not asked yet
}

_lock = threading.Lock()


def load():
    with _lock:
        cfg = dict(DEFAULTS)
        try:
            with open(PATH) as f:
                cfg.update(json.load(f))
        except (OSError, ValueError):
            pass
        for k, v in DEFAULTS.items():
            env = os.environ.get("CODEX_OS3_" + k.upper())
            if env is not None:
                cfg[k] = type(v)(json.loads(env)) if isinstance(v, (bool, list)) else type(v)(env)
        return cfg


def save(updates):
    """Merge updates into the file (only known keys) and return the new config."""
    with _lock:
        cur = {}
        try:
            with open(PATH) as f:
                cur = json.load(f)
        except (OSError, ValueError):
            pass
        cur.update({k: v for k, v in updates.items() if k in DEFAULTS})
        os.makedirs(HOME, exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cur, f, indent=2)
        os.chmod(tmp, 0o600)  # holds the API key
        os.replace(tmp, PATH)
    return load()


def ensure_key():
    cfg = load()
    if not cfg["api_key"]:
        cfg = save({"api_key": new_key()})
    return cfg


def new_key():
    return "cx-" + secrets.token_hex(16)
