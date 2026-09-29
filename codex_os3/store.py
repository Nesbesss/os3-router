"""SQLite state shared by worker and supervisor processes (WAL mode).
Stores metadata only (sizes, timings, tokens, tool names); message bodies go to
captures/ and only when captures are enabled."""
import json, os, sqlite3, threading, time

from . import config

DB = os.path.join(config.HOME, "state.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY, ts REAL, done_ts REAL, task TEXT, source TEXT, model TEXT,
  stream INT, tools INT, msgs INT, bytes INT, imgs INT, mode TEXT, status TEXT, error TEXT,
  result TEXT, calls TEXT, in_tok INT, cached_tok INT, out_tok INT, reason_tok INT, role TEXT, first_ts REAL);
CREATE INDEX IF NOT EXISTS req_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS req_task ON requests(task);
CREATE TABLE IF NOT EXISTS limits (
  ts REAL, p_pct REAL, p_reset REAL, p_window INT, s_pct REAL, s_reset REAL, s_window INT, backend TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts REAL, task TEXT, source TEXT, kind TEXT, level TEXT, msg TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS ev_ts ON events(ts);
CREATE TABLE IF NOT EXISTS sessions (key TEXT PRIMARY KEY, thread TEXT, hashes TEXT, used REAL, anchor TEXT);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""

_local = threading.local()
_lock = threading.Lock()


def db():
    c = getattr(_local, "c", None)
    if c is None:
        os.makedirs(config.HOME, exist_ok=True)
        c = sqlite3.connect(DB, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=30000")
        for attempt in range(20):  # supervisor, watchdog and workers may all open it at once
            try:
                c.execute("PRAGMA journal_mode=WAL")
                c.executescript(SCHEMA)
                cols = {r[1] for r in c.execute("PRAGMA table_info(requests)")}
                if "role" not in cols:  # databases from 0.1.0
                    c.execute("ALTER TABLE requests ADD COLUMN role TEXT")
                if "backend" not in {r[1] for r in c.execute("PRAGMA table_info(limits)")}:  # before 0.2.0
                    c.execute("ALTER TABLE limits ADD COLUMN backend TEXT DEFAULT 'codex'")
                if "first_ts" not in cols:  # before 0.6.0: when the first words of a streamed answer went out
                    c.execute("ALTER TABLE requests ADD COLUMN first_ts REAL")
                if "anchor" not in {r[1] for r in c.execute("PRAGMA table_info(sessions)")}:  # before 0.6.0
                    c.execute("ALTER TABLE sessions ADD COLUMN anchor TEXT")
                break
            except sqlite3.OperationalError:
                time.sleep(0.2 * (attempt + 1))
        _local.c = c
    return c


def _w(sql, args=()):
    with _lock:
        return db().execute(sql, args)


def q(sql, args=()):
    return [dict(r) for r in db().execute(sql, args).fetchall()]


# -- requests -----------------------------------------------------------------

def request_start(task, source, model, stream, tools, msgs, nbytes, role=None):
    return _w("INSERT INTO requests(ts,task,source,model,stream,tools,msgs,bytes,status,role) "
              "VALUES(?,?,?,?,?,?,?,?,'running',?)",
              (time.time(), task, source, model, int(stream), tools, msgs, nbytes, role)).lastrowid


def request_end(rid, **f):
    f.setdefault("done_ts", time.time())
    if isinstance(f.get("calls"), (list, tuple)):
        f["calls"] = json.dumps(f["calls"])
    keys = [k for k in f if k in ("done_ts", "imgs", "mode", "status", "error", "result", "calls", "model",
                                  "in_tok", "cached_tok", "out_tok", "reason_tok", "first_ts")]
    _w(f"UPDATE requests SET {','.join(k + '=?' for k in keys)} WHERE id=?",
       [f[k] for k in keys] + [rid])


def add_tokens(rid, usage):
    """Accumulate codex usage (several codex runs can serve one request)."""
    if not usage:
        return
    _w("UPDATE requests SET in_tok=COALESCE(in_tok,0)+?, cached_tok=COALESCE(cached_tok,0)+?, "
       "out_tok=COALESCE(out_tok,0)+?, reason_tok=COALESCE(reason_tok,0)+? WHERE id=?",
       (usage.get("input_tokens", 0), usage.get("cached_input_tokens", 0),
        usage.get("output_tokens", 0), usage.get("reasoning_output_tokens", 0), rid))


def add_limits(rl, backend="codex"):
    """Store a subscription's limit windows by their real length: the short one (e.g. 5 hours) in p_*, the long one
    (a week) in s_*. Plans differ: Plus has both, Max only a weekly window (sent as its "primary"), Free may have
    others, so the order the backend sends them in means nothing."""
    if not rl:
        return
    wins = []
    for w, default in ((rl.get("primary"), 300), (rl.get("secondary"), 10080)):
        if w and w.get("used_percent") is not None:
            wins.append(dict(w, window_minutes=w.get("window_minutes") or default))
    wins.sort(key=lambda w: w["window_minutes"])
    short = [w for w in wins if w["window_minutes"] < 3 * 1440]
    long_ = [w for w in wins if w["window_minutes"] >= 3 * 1440]
    p = short[0] if short else {}
    s = long_[-1] if long_ else (short[-1] if len(short) > 1 else {})
    _w("INSERT INTO limits VALUES(?,?,?,?,?,?,?,?)",
       (time.time(), p.get("used_percent"), p.get("resets_at"), p.get("window_minutes"),
        s.get("used_percent"), s.get("resets_at"), s.get("window_minutes"), backend))


def win_label(minutes, default):
    """A limit window's name from its length: "5-hour", "daily", "weekly", "monthly"."""
    if not minutes:
        return default
    if minutes < 1440:
        return f"{round(minutes / 60)}-hour"
    return "daily" if minutes < 3 * 1440 else "weekly" if minutes < 20 * 1440 else "monthly"


def latest_limits():
    """{backend: newest limits row}"""
    return {r["backend"] or "codex": r for r in
            q("SELECT l.* FROM limits l JOIN (SELECT COALESCE(backend,'codex') b, MAX(ts) t FROM limits "
              "GROUP BY b) m ON COALESCE(l.backend,'codex')=m.b AND l.ts=m.t")}


def event(kind, msg, task=None, source="router", level="info", data=None):
    _w("INSERT INTO events(ts,task,source,kind,level,msg,data) VALUES(?,?,?,?,?,?,?)",
       (time.time(), task, source, kind, level, msg, json.dumps(data) if data is not None else None))
    from . import report  # opt-in problem reports (does nothing unless the user said yes)
    report.maybe_send(kind, f"[{source}] {msg}", level)


# -- sessions (survive worker reloads) ----------------------------------------

def session_get(key):
    r = q("SELECT * FROM sessions WHERE key=?", (key,))
    return r[0] if r else None


def session_put(key, thread, hashes, anchor=None):
    _w("INSERT OR REPLACE INTO sessions(key, thread, hashes, used, anchor) VALUES(?,?,?,?,?)",
       (key, thread, json.dumps(hashes), time.time(), json.dumps(anchor) if anchor else None))


def session_drop(key):
    _w("DELETE FROM sessions WHERE key=?", (key,))


def sessions_stale(ttl):
    return q("SELECT * FROM sessions WHERE used < ?", (time.time() - ttl,))


# -- kv + housekeeping --------------------------------------------------------

def kv_get(k, default=None):
    r = q("SELECT v FROM kv WHERE k=?", (k,))
    return json.loads(r[0]["v"]) if r else default


def kv_set(k, v):
    _w("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, json.dumps(v)))


def prune(days):
    cut = time.time() - days * 86400
    for t in ("requests", "limits", "events"):
        _w(f"DELETE FROM {t} WHERE ts < ?", (cut,))
