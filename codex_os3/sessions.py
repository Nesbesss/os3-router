"""One persistent codex session per OS3 task, so the model keeps its own reasoning across
turns instead of re-reading a flattened transcript from scratch. A task is keyed by its
first two messages (system + task) and tool set; a turn resumes only if the request's
history starts with exactly the messages already sent. OS3 sometimes compacts history,
which breaks that and falls back to a fresh session. State is kept in SQLite so a worker
reload doesn't forget running tasks.
OS3 ends a chat request with a volatile user message (current time + state snapshot) that the
next request replaces; a trailing user message may therefore be dropped, and the thread
continues with the new one. Without that, every chat turn started fresh and nothing was cached."""
import glob, hashlib, json, os, threading

from . import store

TTL = 3 * 3600


def h(obj):
    return hashlib.sha1(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def key(messages, tools):
    names = sorted(t.get("function", t).get("name", "") for t in tools)
    return h([messages[:2], names])[:16]


_busy = set()
_lock = threading.Lock()


def plan(k, messages):
    """(tracked, thread, new_messages). thread is None for a fresh start. A tracked task is
    marked busy and the caller must call done(); an untracked one (parallel retry of a busy
    task) runs standalone and must not touch the session."""
    hashes = [h(m) for m in messages]
    with _lock:
        if k in _busy:
            return False, None, None
        _busy.add(k)
    s = store.session_get(k)
    if s and s["thread"]:
        known = json.loads(s["hashes"])
        if len(hashes) > len(known) and hashes[:len(known)] == [x.removeprefix("u:") for x in known]:
            return True, s["thread"], messages[len(known):]
        if known and known[-1].startswith("u:") and len(hashes) >= len(known) and hashes[:len(known) - 1] == known[:-1]:
            return True, s["thread"], messages[len(known) - 1:]  # the old trailing user message was replaced
    return True, None, None


def done(k, thread, messages, ok):
    with _lock:
        _busy.discard(k)
    if ok and thread:
        hashes = [h(m) for m in messages]
        if messages and messages[-1].get("role") == "user":
            hashes[-1] = "u:" + hashes[-1]
        store.session_put(k, thread, hashes)
    elif not ok:
        store.session_drop(k)


def rollout_files(thread):
    """A session's transcript files: codex rollouts or Claude Code session logs."""
    from . import accounts  # every Codex account keeps its own sessions
    claude = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))
    return ([f for a in accounts.all_accounts() for f in
             glob.glob(os.path.join(accounts.home(a), "sessions", "**", f"rollout-*{thread}.jsonl"), recursive=True)]
            + glob.glob(os.path.join(claude, "projects", "*", f"{thread}.jsonl")))


def sweep():
    """Drop idle sessions and delete their rollout files (they hold every screenshot)."""
    for s in store.sessions_stale(TTL):
        with _lock:
            if s["key"] in _busy:
                continue
        store.session_drop(s["key"])
        for f in rollout_files(s["thread"] or "-"):
            try:
                os.unlink(f)
            except OSError:
                pass
