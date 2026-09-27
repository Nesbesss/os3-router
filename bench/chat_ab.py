#!/usr/bin/env python3
"""A/B for the main chat: an OS3-shaped conversation (big system prompt + long channel history,
a volatile <supplementary-context> at the end of every request) run for 5 turns through the real
engine of a given checkout. Prints one JSON line per turn: seconds, first streamed text, tokens,
and whether the answer was right (early facts and the *latest* snapshot are both asked for).

  CODEX_OS3_HOME=<empty dir> python3 bench/chat_ab.py <checkout> <label> [engine] [stream]
"""
import json, os, random, sys, time

code, label = sys.argv[1], sys.argv[2]
sys.path.insert(0, code)
if len(sys.argv) > 3:
    os.environ["CODEX_OS3_ENGINE"] = sys.argv[3]
if len(sys.argv) > 4:
    os.environ["CODEX_OS3_STREAM_CHAT"] = sys.argv[4]
from codex_os3 import config, engine, store  # noqa: E402

KB = int(os.environ.get("CHAT_AB_KCHARS", "400")) * 1000  # ~100K tokens, real OS3 chat is ~215K
random.seed(3)
W = "task worker channel status update reply event tool result school planner week toets huiswerk".split()
filler = lambda n: " ".join(random.choice(W) + str(random.randint(0, 99)) for _ in range(n))
system = ("You are the Channel Agent for the `rabbit-hole-ui` channel in OS3, the user's point of contact. "
          "Answer the user briefly.\n" + "\n".join(f"## Rule {i}\n{filler(60)}" for i in range(KB // 1400)))
hist = [{"role": "user", "content": "Remember this: my locker code is 4417."},
        {"role": "assistant", "content": "Noted: locker code 4417."}]
for i in range(KB // 1400):
    hist.append({"role": "user", "content": f'<channel-activity channel="rabbit-hole-ui">{filler(40)}</channel-activity>'})
    if i == KB // 2800:
        hist.append({"role": "user", "content": "By the way, my favourite subject is aardrijkskunde."})
    hist.append({"role": "assistant", "content": filler(15)})
TOOLS = [{"type": "function", "function": {"name": n, "description": d, "parameters": {"type": "object", "properties": {}}}}
         for n, d in (("create_task", "Start a worker for long work."), ("notify_before_act", "Tell the user what you do."),
                      ("wait", "Wait for workers."), ("cancel_task", "Cancel a worker."))]
snap = lambda t, k: {"role": "user", "content": f"<supplementary-context>\n<current-time>2026-09-27 {t} Sun</current-time>\n"
                                                f"<state-snapshot>open tasks: {k}</state-snapshot>\n</supplementary-context>"}
TURNS = [("What's my locker code? Answer with just the number.", "17:02", 3, ["4417"]),
         ("What time is it now, per the latest context? Answer HH:MM only.", "17:09", 5, ["17:09"]),
         ("How many open tasks are there right now? Just the number.", "17:15", 7, ["7"]),
         ("What's my favourite subject? One word.", "17:21", 2, ["aardrijkskunde"]),
         ("Current time and number of open tasks? Format: HH:MM N", "17:30", 4, ["17:30", "4"])]

cfg = config.load()
msgs = [{"role": "system", "content": system}] + hist
for n, (q, t, k, expect) in enumerate(TURNS, 1):
    msgs = msgs + [{"role": "user", "content": q}, snap(t, k)]
    turn = engine.Turn(cfg, {"model": "gpt-6-luna", "messages": msgs, "tools": TOOLS, "stream": True}, lambda: True)
    first = []
    turn.stream_sink = lambda text: first or first.append(time.time())
    start = time.time()
    msg, _ = turn.run()
    secs = time.time() - start
    r = store.q("SELECT mode,in_tok,cached_tok,out_tok FROM requests ORDER BY id DESC LIMIT 1")[0]
    ans = msg.get("content") or json.dumps(msg.get("tool_calls"))
    print(json.dumps({"label": label, "turn": n, "s": round(secs, 1), "first_s": round(first[0] - start, 1) if first else None,
                      "mode": r["mode"], "in": r["in_tok"], "cached": r["cached_tok"], "out": r["out_tok"],
                      "ok": all(e in ans.lower() for e in expect), "answer": ans[:80]}), flush=True)
    msgs = msgs[:-1] + [{"role": "assistant", "content": ans}]  # OS3 drops the old snapshot next time
