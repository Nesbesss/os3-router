"""Runs the official Claude Code CLI (`claude -p`, unmodified, signed in by the user with their
own account) as a decision backend, the same way codex_runner runs `codex exec`. Same
signature and return value as codex_runner.run."""
import base64, json, os, shutil, time

from . import platform_util
from .codex_runner import WORKDIR, ClientGone, UsageLimit, _supervise, idle_limit, slots, split_model

SYSTEM = ("You are the decision engine of an app. The app runs the tools listed in the prompt and "
          "sends you their results. Answer only through the structured output.")
MEDIA = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}
EFFORTS = {"minimal": "low", "ultra": "max"}  # codex effort names without a Claude equivalent


def binary(cfg):
    return platform_util.native_bin(cfg.get("claude_bin") or shutil.which("claude") or "claude")


def environ(bin_path):
    """npm/nvm installs are node scripts: services lack the user's PATH, so put the directory
    of claude (where nvm also keeps node) first."""
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(os.path.realpath(bin_path)) + os.pathsep + os.path.dirname(bin_path) \
        + os.pathsep + env.get("PATH", "")
    return env


def build_cmd(cfg, model, schema=None, resume=None, keep=False):
    model, effort = split_model(model, cfg["effort"])
    # Claude Code's own tools, MCP servers, skills and user/project settings (incl. hooks) are
    # off: the app executes tools, Claude only decides.
    cmd = [binary(cfg), "-p", "--model", model, "--effort", EFFORTS.get(effort, effort),
           "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
           "--include-partial-messages",  # streamed tokens = activity for hang detection
           "--tools", "", "--setting-sources", "project", "--strict-mcp-config",
           "--disable-slash-commands", "--system-prompt", SYSTEM]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    if resume:
        cmd += ["--resume", resume]
    elif not keep:
        cmd.append("--no-session-persistence")
    return cmd


def message(prompt, images=()):
    content = [{"type": "image", "source": {"type": "base64", "media_type": MEDIA.get(ext, "image/png"),
                                            "data": base64.b64encode(data).decode()}} for ext, data in images]
    content.append({"type": "text", "text": prompt})
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"


def limits(info):
    """rate_limit_event -> codex's rate_limits shape (primary = 5 h, secondary = weekly)."""
    w = info.get("unifiedWindows") or {}

    def win(k, minutes):
        x = w.get(k) or {}
        if x.get("utilization") is None:
            return None
        return {"used_percent": round(x["utilization"] * 100, 1), "resets_at": x.get("resetsAt"),
                "window_minutes": minutes}
    rl = {"primary": win("five_hour", 300), "secondary": win("seven_day", 10080)}
    return rl if rl["primary"] or rl["secondary"] else None


def run(cfg, prompt, model, schema=None, alive=lambda: True, images=(), resume=None, keep=False, role=None):
    """-> (text, usage, session_id, rate_limits). Raises ClientGone, CodexHung, UsageLimit,
    RuntimeError."""
    os.makedirs(WORKDIR, exist_ok=True)
    cmd = build_cmd(cfg, model, schema, resume, keep)
    sem = slots(cfg["max_codex"])
    while not sem.acquire(timeout=2):
        if not alive():
            raise ClientGone()
    try:
        idle = idle_limit(cfg, split_model(model, cfg["effort"])[1], role)
        out, err, thread = _supervise(dict(cfg, hang_idle_s=idle), cmd, message(prompt, images), alive, resume,
                                      cwd=WORKDIR, final='"type":"result"', env=environ(cmd[0]))
    finally:
        sem.release()

    result, rl, info = None, None, {}
    for line in out:
        if '"stream_event"' in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "result":
            result = e
        elif e.get("type") == "rate_limit_event":
            info = e.get("rate_limit_info") or {}
            rl = limits(info) or rl

    u = (result or {}).get("usage") or {}
    cached = u.get("cache_read_input_tokens", 0)
    usage = {"input_tokens": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + cached,
             "cached_input_tokens": cached, "output_tokens": u.get("output_tokens", 0),
             "reasoning_output_tokens": (u.get("output_tokens_details") or {}).get("thinking_tokens", 0)}
    if result and not result.get("is_error"):
        so = result.get("structured_output")
        return (json.dumps(so) if so is not None else result.get("result") or ""), usage, thread, rl
    msg = ((result or {}).get("result") or " | ".join(map(str, (result or {}).get("errors") or []))
           or ("\n".join(err) or "no output from claude")[-600:])
    if "usage credits" in msg.lower() or "not available" in msg.lower() and "model" in msg.lower():
        raise UsageLimit(msg, plan=True)  # e.g. Fable on Pro: "requires usage credits"
    if info.get("status") == "rejected" or "limit" in msg.lower() and ("usage" in msg.lower() or "reset" in msg.lower()):
        reset = info.get("resetsAt")
        raise UsageLimit(msg, time.strftime("%H:%M", time.localtime(reset)) if reset else "")
    raise RuntimeError(msg)
