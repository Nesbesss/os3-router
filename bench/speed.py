#!/usr/bin/env python3
"""Compare how fast models answer through the router (real Codex calls on your own subscription: a few small ones).

    python3 bench/speed.py gpt-6-sol gpt-6.1-sol            # 3 runs each, medium effort
    python3 bench/speed.py gpt-6.1-sol --runs 5 --effort low

For every model it measures the two things a person waits for:
  chat    a ~150 word answer, streamed like OS3 asks for it: seconds until the first words, and until it is complete
  worker  "which tool next?": seconds until the tool call arrives (what a computer-use step costs, without the desktop)
A model the account can't use (not in the plan yet, or Codex too old for it) is reported as such, not as a slow one.
It starts its own router on a spare port with its own settings, and uses the Codex login of this computer
(CODEX_HOME to pick another account). It does not touch your running router. Numbers vary from run to run: use
--runs 5 or more before believing a difference of a second or two."""
import argparse, http.client, json, os, statistics as st, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 11993
CHAT_TOOL = {"type": "function", "function": {"name": "create_task", "description": "Start a task for a worker.",
                                              "parameters": {"type": "object", "properties": {"goal": {"type": "string"}}}}}
SHELL_TOOL = {"type": "function", "function": {"name": "shell", "description": "Run a shell command on the user's device.",
                                               "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                                                              "required": ["command"]}}}


def ask(cfg, model, kind, i):
    """-> (first_words_s or None, total_s, error or None)"""
    if kind == "chat":
        tools = [CHAT_TOOL]
        msgs = [{"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": f"(#{i} {model}) Explain in about 150 words how ocean tides work. Just answer; no tools needed."}]
    else:
        tools = [SHELL_TOOL]
        msgs = [{"role": "system", "content": "You are a worker agent. Use the tools to do the task."},
                {"role": "user", "content": f"(#{i} {model}) Print the text hello on this device with the shell tool."}]
    body = {"model": model, "stream": True, "tools": tools, "messages": msgs}
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=300)
    t0 = time.time()
    c.request("POST", "/v1/chat/completions", json.dumps(body), {"Authorization": "Bearer " + cfg["api_key"], "Content-Type": "application/json"})
    r = c.getresponse()
    first, text, called = None, "", False
    for raw in r:
        line = raw.decode().strip()
        if not line.startswith("data:") or line == "data: [DONE]":
            continue
        d = json.loads(line[5:])
        if d.get("error"):
            return None, time.time() - t0, str(d["error"].get("message", d["error"]))[:160]
        ch = (d.get("choices") or [{}])[0].get("delta") or {}
        if ch.get("content"):
            text += ch["content"]
            first = first if first is not None else time.time() - t0
        if ch.get("tool_calls"):
            called = True
    total = time.time() - t0
    if kind == "worker" and called:
        return total, total, None
    if kind == "chat" and text:
        if text.lstrip().startswith("⚠"):  # the router's own notice ("not in your plan", "usage limit")
            return None, total, text.strip()[:160]
        return first, total, None
    return None, total, (text.strip() or "no answer")[:160]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("models", nargs="+")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--effort", default="medium")
    a = ap.parse_args()
    home = tempfile.mkdtemp(prefix="os3-speed-")
    os.environ["CODEX_OS3_HOME"] = home
    sys.path.insert(0, os.path.dirname(HERE))
    from codex_os3 import codex_runner, config, server, store
    cfg = config.ensure_key()
    config.save({"role_routing": False, "effort": a.effort, "stream_chat": True, "engine": "appserver", "port": PORT})
    srv = server.Server(("127.0.0.1", PORT))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.4)
    cfg = config.load()
    print(f"{a.runs} run(s) per model, effort {a.effort}, streamed like OS3 asks for it\n")
    print(f"{'model':16} {'chat: first words':>18} {'chat: complete':>15} {'worker: tool call':>18}")
    for model in a.models:
        res = {}
        for kind in ("chat", "worker"):
            rows = []
            for i in range(a.runs):
                config.save({"model": model})
                first, total, err = ask(cfg, model, kind, i)
                # the router turns a model the plan doesn't have into one it does (that is right for OS3, wrong for a
                # benchmark): the model that really answered is what the request row says
                used = (store.q("SELECT model FROM requests ORDER BY id DESC LIMIT 1") or [{}])[0].get("model")
                if not err and used and codex_runner.split_model(used, None)[0] != model:
                    err = f"the account doesn't have it: the router answered with {codex_runner.split_model(used, None)[0]} instead"
                if err:
                    rows = err
                    break
                rows.append((first, total))
            res[kind] = rows
        bad = next((v for v in res.values() if isinstance(v, str)), None)
        if bad:
            print(f"{model:16} not available on this account: {bad}")
            continue
        med = lambda k, j: st.median(r[j] for r in res[k])
        print(f"{model:16} {med('chat', 0):17.1f}s {med('chat', 1):14.1f}s {med('worker', 1):17.1f}s")
    print("\nmedians. Different accounts and times of day differ: compare models in the same run.")


if __name__ == "__main__":
    main()
