"""Runs `codex exec` (fresh or resumed) and returns the agent's reply, token usage and
the account's rate limits."""
import json, os, re, subprocess, tempfile, threading, time

from . import accounts, config, platform_util, sessions, store

WORKDIR = os.path.join(config.HOME, "work")

# codex is used as a decision backend; the app executes tools. Its own agent features would
# make it act locally ("verify" in its read-only sandbox) instead of emitting tool calls.
DISABLED = ("computer_use", "browser_use", "browser_use_external", "in_app_browser",
            "multi_agent", "image_generation", "goals", "memories", "plugins", "apps", "hooks",
            "shell_tool", "unified_exec", "sleep_tool", "tool_suggest")

_slots = None
_slots_lock = threading.Lock()


class ClientGone(Exception):
    """The HTTP client hung up (e.g. OS3 timed out and retried)."""


class CodexHung(RuntimeError):
    """codex showed no activity for hang_idle_s (upstream stream stalled)."""


class ModelCapacity(RuntimeError):
    """The selected upstream model is temporarily at capacity."""


class UsageLimit(RuntimeError):
    """The subscription's usage limit is reached."""

    def __init__(self, msg, resets="", plan=False):
        super().__init__(msg)
        self.resets = resets
        self.plan = plan  # the model is not part of the user's plan at all


class SignedOut(UsageLimit):
    """OpenAI signed this Codex login out (plan change, password reset): it needs a new sign-in."""


def slots(n):
    global _slots
    with _slots_lock:
        if _slots is None:
            _slots = threading.BoundedSemaphore(n)
    return _slots


_bg, _bg_n = None, None


def take(cfg, role, alive):
    """A place to run a model call -> the function that gives it back. Background calls (OS3's memory and review
    work, often long) may hold all but one place, so a chat message or a worker step never waits behind them:
    on a real router chat took 16 s instead of 10 s while three other calls were already running."""
    global _bg, _bg_n
    n = cfg["max_codex"]
    shared = slots(n)  # (takes the same lock: not inside it)
    with _slots_lock:
        if role == "background" and (_bg is None or _bg_n != n):
            _bg, _bg_n = threading.BoundedSemaphore(max(1, n - 1)), n
        sems = ([_bg] if role == "background" else []) + [shared]
    got = []
    try:
        for s in sems:
            while not s.acquire(timeout=2):
                if not alive():
                    raise ClientGone()
            got.append(s)
    except BaseException:
        for s in reversed(got):
            s.release()
        raise
    return lambda: [s.release() for s in reversed(got)]


def split_model(model, default_effort):
    for e in ("-xhigh", "-ultra", "-max", "-high", "-medium", "-low", "-minimal"):
        if model.endswith(e):
            return model[:-len(e)], e[1:]
    return model, default_effort


_known = {}


def known_features(codex):
    """Feature flags this codex build knows (`codex features list`); passing an unknown one to
    --disable is a hard error, and the set changes between versions."""
    if codex not in _known:
        try:
            out = subprocess.run([codex, "features", "list"], capture_output=True, text=True, timeout=30, cwd=config.HOME,
                                 **platform_util.popen_group_kwargs()).stdout
            _known[codex] = {line.split()[0] for line in out.splitlines() if line.strip()}
        except (OSError, subprocess.SubprocessError):
            _known[codex] = set()
    return _known[codex]


def build_cmd(cfg, model, schema_file=None, image_files=(), resume=None, image_gen=False):
    model, effort = split_model(model, cfg["effort"])
    codex = platform_util.codex_path(cfg)
    if not codex:
        raise RuntimeError(platform_util.CODEX_MISSING)
    known = known_features(codex)
    disabled = [f for f in DISABLED if (f in known or not known) and not (image_gen and f == "image_generation")]
    cmd = [codex, "exec", *(["resume"] if resume else []), "--json",
           "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
           *[a for f in disabled for a in ("--disable", f)],
           *([] if resume else ["-s", "read-only", "-C", WORKDIR]), "-m", model,
           "-c", f"model_reasoning_effort={json.dumps(effort)}"]
    if cfg.get("compact_tokens"):  # summarize long task histories sooner: smaller, faster steps
        cmd[-2:-2] = ["-c", f"model_auto_compact_token_limit={int(cfg['compact_tokens'])}"]
    if schema_file:
        cmd += ["--output-schema", schema_file]
    if image_files:
        cmd += ["-i", *image_files]
    if resume:
        cmd.append(resume)
    # the prompt goes over stdin ("-"): prompts reach 500 KB (near macOS's argv limit) and
    # Windows' codex.cmd shim would mangle special characters in an argument
    return cmd + ["--", "-"]


def idle_limit(cfg, effort, role=None):
    """Seconds without output before a run counts as hung. Codex prints nothing while it
    thinks, and higher efforts think longer: killing a run that is still thinking only
    restarts the work.

    Background calls (OS3's memory/soul merges, fact extraction) answer with one long
    message of 10k-27k tokens, and codex emits no event until that message is complete, so
    even at low effort they are silent for well over 90s while working normally. They get
    the longest limit; hang_max_s still caps the whole run."""
    mult = {"high": 2, "xhigh": 10 / 3, "max": 10 / 3, "ultra": 10 / 3}.get(effort, 1)
    if role == "background":
        mult = max(mult, 10 / 3)
    return cfg["hang_idle_s"] * mult


RESET_RE = re.compile(r"try again (?:at|in) ([^.\"\\]+)", re.I)
# a model this ChatGPT plan doesn't include (e.g. Free): "The 'gpt-6-sol' model is not supported when using Codex with a ChatGPT account."
PLAN_RE = re.compile(r"not supported when using codex with a chatgpt account|not (?:included|available) (?:in|on|with) your (?:plan|subscription)", re.I)


def last_token_count(thread):
    """(rate_limits, last turn's usage) from the session rollout's newest token_count event.
    rate_limits = 5h primary / weekly secondary. The usage matters for resumed sessions: exec's
    own turn.completed usage is the thread's running total, which overstated every resumed step."""
    for f in sessions.rollout_files(thread):
        try:
            with open(f, "rb") as fh:
                fh.seek(max(0, os.path.getsize(f) - 400_000))
                tail = fh.read().decode(errors="ignore").splitlines()
        except OSError:
            continue
        for line in reversed(tail):
            if '"rate_limits"' in line:
                try:
                    p = json.loads(line).get("payload") or {}
                except ValueError:
                    continue
                return p.get("rate_limits"), (p.get("info") or {}).get("last_token_usage")
    return None, None


def run(cfg, prompt, model, schema=None, alive=lambda: True, images=(), resume=None, keep=False, role=None, account=None,
        image_gen=False):
    """-> (text, usage, thread, rate_limits). Raises ClientGone, CodexHung, UsageLimit,
    RuntimeError."""
    os.makedirs(WORKDIR, exist_ok=True)
    tmp = []
    try:
        schema_file = None
        if schema:
            f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
            json.dump(schema, f)
            f.close()
            tmp.append(f.name)
            schema_file = f.name
        img_files = []
        for ext, data in images:
            f = tempfile.NamedTemporaryFile(suffix="." + ext, delete=False)
            f.write(data)
            f.close()
            tmp.append(f.name)
            img_files.append(f.name)
        cmd = build_cmd(cfg, model, schema_file, img_files, resume, image_gen)
        if resume:
            wait_exiting(resume)

        release = take(cfg, role, alive)
        try:
            idle = idle_limit(cfg, split_model(model, cfg["effort"])[1], role)
            out, err_lines, thread = _supervise(dict(cfg, hang_idle_s=idle), cmd, prompt, alive, resume, env=accounts.env(account),
                                                final=codex_done, detach=True)
        finally:
            release()
    finally:
        for f in tmp:
            try:
                os.unlink(f)
            except OSError:
                pass

    text, errs, usage = [], [], {}
    for line in out:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        t = e.get("type")
        if t == "thread.started":
            thread = e.get("thread_id") or thread
        elif t == "item.completed" and (e.get("item") or {}).get("type") == "agent_message":
            text.append(e["item"].get("text", ""))
        elif t == "turn.completed":
            usage = e.get("usage") or {}
        elif t in ("error", "turn.failed"):  # keep all: the first one usually has the details
            errs.append(e.get("message") or json.dumps(e.get("error", "")))

    limits, last = last_token_count(thread) if thread else (None, None)
    if resume and last:
        usage = {k: last.get(k, 0) for k in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")}
    if thread and not keep and not resume:  # one-off call: don't leave history behind
        for f in sessions.rollout_files(thread):
            try:
                os.unlink(f)
            except OSError:
                pass
    if not text:
        msg = " | ".join(errs) or ("\n".join(err_lines) or "no output from codex")[-600:]
        if "newer version" in msg.lower():  # e.g. "requires a newer version of Codex": the watchdog updates it
            store.kv_set("codex_outdated", time.time())
        if PLAN_RE.search(msg):
            raise UsageLimit(msg, plan=True)
        if accounts.AUTH_RE.search(msg):
            accounts.mark_signed_out(account, msg)
            raise SignedOut(msg)
        if "usage limit" in msg.lower():
            m = RESET_RE.search(msg)
            raise UsageLimit(msg, m.group(1).strip() if m else "")
        raise RuntimeError(msg)
    return "\n".join(text), usage, thread, limits


def _supervise(cfg, cmd, prompt, alive, thread, cwd=None, final=None, env=None, detach=False):
    """Run a backend CLI, killing it when the client leaves or it stops showing activity
    (new stdout event or its session file growing). Returns as soon as `final(line)` says the
    answer is complete: with detach=True the CLI finishes its own shutdown in the background
    (codex spends 1-2 s exiting after its answer), otherwise it is stopped (claude with
    stream-json input keeps running)."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE, cwd=cwd, env=env,
                         text=True, encoding="utf-8", errors="replace", **platform_util.popen_group_kwargs())
    out, err = [], []
    done, answered = threading.Event(), threading.Event()

    def feed():
        try:
            p.stdin.write(prompt)
            p.stdin.close()
        except (OSError, ValueError):
            pass
    state = {"last": time.time(), "thread": thread}

    def read_out():
        for line in p.stdout:  # keeps draining after the answer, so a detached CLI can't block on a full pipe
            line = line.strip()
            if line.startswith("{") and not answered.is_set():
                out.append(line)
                state["last"] = time.time()
                if '"thread.started"' in line or not state["thread"] and '"session_id"' in line:
                    try:
                        e = json.loads(line)
                        state["thread"] = e.get("thread_id") or e.get("session_id") or state["thread"]
                    except ValueError:
                        pass
                if final and final(line):
                    answered.set()
                    done.set()
        done.set()  # EOF: the CLI exited

    def read_err():
        for line in p.stderr:
            err.append(line.rstrip())
            del err[:-50]

    readers = [threading.Thread(target=read_out, daemon=True), threading.Thread(target=read_err, daemon=True),
               threading.Thread(target=feed, daemon=True)]
    for r in readers:
        r.start()
    start, rollout = time.time(), None
    try:
        while not done.wait(1):  # wakes immediately when the answer (or EOF) arrives
            if not alive():
                raise ClientGone()
            if rollout is None and state["thread"]:
                rollout = (sessions.rollout_files(state["thread"]) or [None])[0]
            if rollout:
                try:
                    state["last"] = max(state["last"], os.path.getmtime(rollout))
                except OSError:
                    pass
            now = time.time()
            if now - state["last"] > cfg["hang_idle_s"] or now - start > cfg["hang_max_s"]:
                raise CodexHung(f"codex idle {now - state['last']:.0f}s (total {now - start:.0f}s)")
    except BaseException:
        platform_util.kill_tree(p)  # wrapper + native codex child
        p.wait()
        raise
    if answered.is_set() and p.poll() is None:
        if detach:
            threading.Thread(target=p.wait, daemon=True).start()  # reap it when it's done shutting down
            if state["thread"]:
                _exiting[state["thread"]] = p
            return list(out), list(err), state["thread"]
        platform_util.kill_tree(p)
        p.wait()
    p.wait()
    for r in readers:
        r.join(timeout=5)
    return out, err, state["thread"]


_exiting = {}  # thread id -> codex process still shutting down after its answer


def wait_exiting(thread, timeout=10):
    """A resume must not read the session while the previous codex is still writing it."""
    p = _exiting.pop(thread, None)
    if p is not None:
        try:
            p.wait(timeout)
        except subprocess.TimeoutExpired:
            pass


def codex_done(line):
    """codex --json: the turn's last event."""
    if "turn.completed" not in line and "turn.failed" not in line:
        return False
    try:
        return json.loads(line).get("type") in ("turn.completed", "turn.failed")
    except ValueError:
        return False
