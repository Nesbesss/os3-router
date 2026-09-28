"""Which OS3 role a request comes from, and which model/effort serves that role.

OS3 in "local" mode sends the same model id for everything, but the requests differ:
  chat        the orchestrator you talk to: create_task, notify_before_act, steer_task, …
  worker      background workers doing the job: "You are a worker agent", shell, computer_use, files
  background  small housekeeping calls: memory/fact extraction, reply review, titles (few or no tools)
"""
import json, os, re, shutil, subprocess, sys, time

ROLES = ("chat", "worker", "background")
LABEL = {"chat": "Small (main chat)", "worker": "Standard (workers)", "background": "Background"}
CHAT_TOOLS = {"create_task", "notify_before_act", "steer_task", "cancel_task", "report_task_on", "render_ui"}
WORKER_TOOLS = {"computer_use", "computer_use_prepare", "shell", "file_read", "file_write", "file_edit",
                "dummy_system", "feed_image"}


def classify(body):
    tools = {t.get("function", t).get("name", "") for t in body.get("tools") or []}
    system = ""
    for m in body.get("messages") or []:
        if m.get("role") == "system":
            c = m.get("content")
            system = c if isinstance(c, str) else " ".join(p.get("text", "") for p in c or [] if isinstance(p, dict))
            break
    if "worker agent" in system.lower() or (tools & WORKER_TOOLS and not tools & CHAT_TOOLS):
        return "worker"
    if tools & CHAT_TOOLS:
        return "chat"
    if tools & BACKGROUND_MARKERS:
        return "background"
    return "chat" if tools else "background"  # unknown tools (e.g. OS3's connection test) = main chat


# tools only OS3's housekeeping calls get: memory/fact extraction and the reply reviewer
BACKGROUND_MARKERS = {"emit_facts", "emit_merged_soul", "extract_file_signals", "report_missed_action",
                      "report_correction"}


EFFORT_ORDER = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]


def requested_effort(body):
    """The effort OS3's reasoning sliders send (OpenAI's two request shapes), or None."""
    r = body.get("reasoning")
    e = body.get("reasoning_effort") or (r.get("effort") if isinstance(r, dict) else None)
    return e.lower() if isinstance(e, str) and e.lower() in EFFORT_ORDER else None


def fit_effort(model, effort):
    """The nearest effort this model supports (e.g. OS3's "minimal" on a Claude model -> "low")."""
    m = next((x for x in all_codex_models() + CLAUDE if x["slug"] == model), None)
    if not m or effort in m["efforts"]:
        return effort
    if effort not in EFFORT_ORDER:  # a hand-edited config value
        return m["default_effort"]
    i = EFFORT_ORDER.index(effort)
    return min(m["efforts"], key=lambda e: abs(EFFORT_ORDER.index(e) - i) if e in EFFORT_ORDER else 99)


def runnable(cfg, model):
    """A Claude model can't run without Claude Code (uninstalled since it was chosen, or settings copied from
    another machine): use the Codex default instead of failing every request of that role."""
    if backend(model) != "claude" or claude_installed(cfg):
        return model
    return cfg["model"] if backend(cfg["model"]) == "codex" else FALLBACK[0]["slug"]


def pick(cfg, role, requested, os3_effort=None, images=False):
    """-> model string for the runners ("<slug>-<effort>"). The model comes from the role (or, with
    routing off, from OS3); the effort from OS3's reasoning slider when it sends one, else from the
    dashboard."""
    if not cfg.get("role_routing", True):
        model = runnable(cfg, requested or cfg["model"])
        if os3_effort and codex_split(model)[1] is None:
            return f"{model}-{fit_effort(model, os3_effort)}"
        return model
    r = (cfg.get("roles") or {}).get(role) or {}
    model = r.get("model") or requested or cfg["model"]
    if images and role == "background":  # e.g. OS3's image check (name 4 colour bands): the small model
        model = ((cfg.get("roles") or {}).get("worker") or {}).get("model") or model  # misread it, then OS3 asked for another provider
    # OS3's sliders are Small and Standard; it sends Small's on background calls too, but those run
    # often and OS3 has no Background slider, so the dashboard decides there
    effort = (r.get("effort") or os3_effort if role == "background" else os3_effort or r.get("effort")) or cfg["effort"]
    model = runnable(cfg, model)
    return f"{model}-{fit_effort(model, effort)}"


def pick_fallback(cfg, role, os3_effort=None):
    """The model this role switches to while its subscription is at its usage limit, or None."""
    f = (cfg.get("fallback") or {}).get(role) or {}
    if not f.get("model") or runnable(cfg, f["model"]) != f["model"]:
        return None
    effort = (f.get("effort") or os3_effort if role == "background" else os3_effort or f.get("effort")) or cfg["effort"]
    return f"{f['model']}-{fit_effort(f['model'], effort)}"


def codex_split(model):
    from .codex_runner import split_model
    return split_model(model, None)


def backend(model):
    """"claude" for Claude models (run through Claude Code), else "codex"."""
    m = (model or "").lower()
    return "claude" if m.startswith("claude") or m.split("-")[0] in ("sonnet", "opus", "haiku", "fable") else "codex"


# -- the models on offer: this Codex account's list, plus Claude if Claude Code is installed --

FALLBACK = [
    {"slug": "gpt-6-luna", "name": "GPT-6-Luna", "description": "Fast and affordable model for easier tasks.",
     "efforts": ["low", "medium", "high", "xhigh", "max"], "default_effort": "medium", "backend": "codex"},
    {"slug": "gpt-6-sol", "name": "GPT-6-Sol", "description": "Workhorse model for coding and everyday work.",
     "efforts": ["low", "medium", "high", "xhigh", "max", "ultra"], "default_effort": "medium", "backend": "codex"},
]


def available_models(home=None):
    """The Codex models plus, when Claude Code is installed, the Claude ones. No home: every Codex account's
    models (an account on a paid plan has models the Free one doesn't); a home: just that account's."""
    return (codex_models(home) if home else all_codex_models()) + (CLAUDE if claude_installed() else [])


def account_models(home):
    """One account's model list, or None when it has none yet: Codex's own cache (refreshed by every codex run of
    that account, so new models show up by themselves), else the one the router asked Codex for."""
    for name, raw in (("models_cache.json", True), ("os3-models.json", False)):
        try:
            with open(os.path.join(home, name)) as f:
                ms = json.load(f).get("models") or []
        except (OSError, ValueError):
            continue
        if raw:
            ms = [{"slug": m["slug"], "name": m.get("display_name") or m["slug"], "description": m.get("description") or "",
                   "efforts": [e["effort"] for e in m.get("supported_reasoning_levels") or []] or ["medium"],
                   "default_effort": m.get("default_reasoning_level") or "medium", "backend": "codex"}
                  for m in ms if m.get("slug") and m.get("visibility", "list") == "list"]
        if ms:
            return ms
    return None


def codex_models(home=None):
    """One account's models (default: the main one), the built-in list when it has none yet."""
    return account_models(home or os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex"))) or FALLBACK


def all_codex_models():
    """Every Codex account's models together. A model that only some accounts have carries "only": those accounts
    (shown in the model selector, and used to send a request to an account that can serve it)."""
    from . import accounts
    seen, known = {}, 0
    for a in accounts.all_accounts():
        ms = account_models(accounts.home(a))
        if ms is None:
            continue
        known += 1
        for m in ms:
            e = seen.setdefault(m["slug"], dict(m, efforts=list(m["efforts"]), accounts=[]))
            e["accounts"].append(a)
            e["efforts"] += [x for x in m["efforts"] if x not in e["efforts"]]
    if not seen:
        return FALLBACK
    for e in seen.values():
        e["efforts"].sort(key=lambda x: EFFORT_ORDER.index(x) if x in EFFORT_ORDER else 99)
        if known > 1 and len(e["accounts"]) < known:  # only when every account's list is known
            e["only"] = e["accounts"]
        del e["accounts"]
    return list(seen.values())


# where Claude Code's installers put the CLI: the service's PATH usually lacks these (~/.local/bin above
# all, the native installer's default), so a Claude Code installed after the router went unnoticed
CLAUDE_DIRS = ("~/.local/bin", "~/.claude/local", "/opt/homebrew/bin", "/usr/local/bin", "~/.npm-global/bin",
               "~/.bun/bin", "~/AppData/Roaming/npm", "~/AppData/Local/Programs/claude")


_shell = [0, None]  # [when asked, answer]


def _from_login_shell():
    """Last resort: ask the user's own shell where claude is. nvm/fnm/volta/asdf installs and aliases exist
    only in its startup files, never in the service's PATH. Misses are cached 5 min (it starts a shell)."""
    if sys.platform == "win32":
        return None
    if _shell[1] and os.path.isfile(_shell[1]):
        return _shell[1]
    if time.time() - _shell[0] < 300:  # a miss is re-checked rarely: it starts a shell
        return None
    found = None
    try:
        out = subprocess.run([os.environ.get("SHELL") or "/bin/zsh", "-ilc", "command -v claude"], capture_output=True,
                             text=True, timeout=8, stdin=subprocess.DEVNULL).stdout
        found = next((p for p in re.findall(r"/[^\s'\"=]+", out) if os.path.isfile(p)), None)
    except (OSError, subprocess.SubprocessError):
        pass
    _shell[:] = [time.time(), found]
    return found


def claude_path(cfg=None):
    """Claude Code's CLI: the saved path, else PATH, else its usual install folders. None = not installed."""
    if cfg is None:
        from . import config
        cfg = config.load()
    p = cfg.get("claude_bin")
    if p and os.path.exists(p):
        return p
    p = shutil.which("claude")
    if p:
        return p
    for d in CLAUDE_DIRS:
        for name in ("claude", "claude.exe", "claude.cmd"):
            f = os.path.normpath(os.path.join(os.path.expanduser(d), name))
            if os.path.isfile(f):
                return f
    p = _from_login_shell()
    if p:
        return p
    return cfg.get("claude_bin") or None  # set but missing right now (e.g. a drive not mounted): keep it


def claude_installed(cfg=None):
    return bool(claude_path(cfg))


_E = ["low", "medium", "high", "xhigh", "max"]
CLAUDE = [
    {"slug": "claude-sonnet-5-5", "name": "Claude Sonnet 5.5", "description": "Claude Code · fast and capable; the best balance.",
     "efforts": _E, "default_effort": "medium", "backend": "claude"},
    {"slug": "claude-sonnet-5", "name": "Claude Sonnet 5", "description": "Claude Code · the previous Sonnet.",
     "efforts": _E, "default_effort": "medium", "backend": "claude"},
    {"slug": "claude-opus-5-5", "name": "Claude Opus 5.5", "description": "Claude Code · most capable; uses the most of your limits.",
     "efforts": _E, "default_effort": "medium", "backend": "claude"},
    {"slug": "claude-fable-5-1", "name": "Claude Fable 5.1", "description": "Claude Code · not included in Pro (needs usage credits).",
     "efforts": _E, "default_effort": "medium", "backend": "claude"},
    {"slug": "claude-haiku-4-5", "name": "Claude Haiku 4.5", "description": "Claude Code · fastest and lightest.",
     "efforts": ["low", "medium", "high"], "default_effort": "low", "backend": "claude"},
]
