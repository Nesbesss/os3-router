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
VIA = {}  # task -> "anchor" when the last plan() continued a conversation by finding our own last reply


def _text(content):
    if isinstance(content, list):
        content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content.strip() if isinstance(content, str) else ""


def anchor_of(reply):
    """How to find our own last reply in OS3's next request: the ids of the tool calls we made (random, so unique
    everywhere), or, for a plain answer, a hash of its text."""
    if not isinstance(reply, dict):
        return None
    ids = [c.get("id") for c in reply.get("tool_calls") or [] if c.get("id")]
    if ids:
        return {"ids": ids}
    text = _text(reply.get("content"))
    return {"text": h(text)} if text else None


def find_anchor(messages, anchor):
    """Index of the newest assistant message if it is our last reply, else None. Everything before it is what the
    session already holds, whatever OS3 has since done to those older messages (shortened a result, dropped an
    image, changed its context notes): the only thing that has to be true is that our own answer is still the
    newest one, followed by something new."""
    if not anchor:
        return None
    for i in range(len(messages) - 1, 1, -1):  # 0 and 1 are the system prompt and the task: the key already covers them
        m = messages[i]
        if m.get("role") != "assistant":
            continue
        if "ids" in anchor:
            have = {c.get("id") for c in m.get("tool_calls") or []}
            ok = bool(have) and set(anchor["ids"]) <= have
        else:
            ok = h(_text(m.get("content"))) == anchor.get("text")
        return i if ok and i + 1 < len(messages) else None
    return None


def _note_miss(k, s, known, hashes, messages):
    """Why a follow-up could not continue its session: only sizes and roles, never content."""
    prior = [x.removeprefix("u:") for x in known]
    p = next((i for i, (a, b) in enumerate(zip(prior, hashes)) if a != b), min(len(prior), len(hashes)))
    store.event("session_miss", f"kept {len(prior)} messages, now {len(hashes)}; the same for the first {p}", task=k, source="sessions",
                data={"kept": len(prior), "now": len(hashes), "same_prefix": p, "had_anchor": bool(s.get("anchor")),
                      "role_at_change": messages[p].get("role") if p < len(messages) else None})


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
        a = find_anchor(messages, json.loads(s["anchor"]) if s.get("anchor") else None)
        if a is not None:  # OS3 changed something in the older messages, but our last reply is right where we left it
            VIA[k] = "anchor"
            return True, s["thread"], messages[a:]
        _note_miss(k, s, known, hashes, messages)
    return True, None, None


def done(k, thread, messages, ok, reply=None):
    with _lock:
        _busy.discard(k)
    if ok and thread:
        hashes = [h(m) for m in messages]
        if messages and messages[-1].get("role") == "user":
            hashes[-1] = "u:" + hashes[-1]
        store.session_put(k, thread, hashes, anchor_of(reply))
    elif not ok:
        store.session_drop(k)


def rollout_files(thread):
    """A session's transcript files: codex rollouts or Claude Code session logs."""
    from . import accounts  # every Codex account keeps its own sessions
    claude = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))
    return ([f for a in accounts.all_accounts() for f in
             glob.glob(os.path.join(accounts.home(a), "sessions", "**", f"rollout-*{thread}.jsonl"), recursive=True)]
            + glob.glob(os.path.join(claude, "projects", "*", f"{thread}.jsonl")))


def is_claude(thread):
    """True when this thread is a Claude Code session (not a Codex thread): a task that moved from one backend
    to the other (fallback, a changed model) must start fresh, not `resume` an id the other CLI doesn't know."""
    claude = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))
    return bool(glob.glob(os.path.join(claude, "projects", "*", f"{thread}.jsonl")))


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
