#!/usr/bin/env python3
"""Stand-in for the Codex CLI in tests: same flags, JSON events and rollout files, no network.
Prompt triggers: FAKE_LIMIT (usage limit error), FAKE_HANG (sleep forever), FAKE_FINAL (final
answer instead of a tool call), FAKE_BADJSON (invalid tool-call arguments), FAKE_LIMIT_SOL (usage
limit only for *sol* models), FAKE_PLAN (the plan has no *sol* models), FAKE_SIGNEDOUT, FAKE_NOTFOUND (gives up with "couldn't find" until nudged)."""
import json, os, sys, time, uuid

a = sys.argv[1:]
if "--version" in a:
    print("codex-cli 9.9.9")  # newer than any minimum: the installer keeps it
    sys.exit(0)
if a[:2] == ["features", "list"]:
    for f in ("computer_use", "multi_agent", "plugins", "shell_tool", "unified_exec"):  # sleep_tool unknown, like 0.151
        print(f"{f:40} stable             true")
    sys.exit(0)
if any(x in ("sleep_tool", "hooks", "apps") for x in a):
    print("Error: Unknown feature flag: sleep_tool", file=sys.stderr)
    sys.exit(2)
if a[:2] == ["login", "status"]:
    print("Logged in using ChatGPT (fake)")
    sys.exit(0)


def answer(prompt, model, schema, resumed):
    """-> (text, error). The same prompt triggers for `exec` and `app-server`."""
    if "FAKE_LIMIT" in prompt and ("FAKE_LIMIT_SOL" not in prompt or "sol" in model):
        return None, "You've hit your usage limit. Upgrade to Pro or try again at 9:11 PM."
    if "FAKE_SIGNEDOUT" in prompt:
        return None, "401 Unauthorized: Your authentication token has been invalidated. Please try signing in again."
    if "FAKE_PLAN" in prompt and "sol" in model:  # a plan without sol (e.g. Free)
        return None, "The 'gpt-6-sol' model is not supported when using Codex with a ChatGPT account."
    if "FAKE_NOTFOUND" in prompt and "BEFORE YOU GIVE UP" not in prompt and schema:
        return json.dumps({"kind": "final", "calls": [], "content": "I couldn't find Extreme weather on the page."}), None
    if schema and "FAKE_FINAL" not in prompt:
        args = '{"location": "Oslo"' if "FAKE_BADJSON" in prompt else '{"location":"Oslo"}'
        return json.dumps({"kind": "tool_call", "content": "", "calls": [{"tool": "get_weather", "arguments_json": args}]}), None
    if schema:
        return json.dumps({"kind": "final", "calls": [], "content": f"fake answer ({'resumed' if resumed else 'fresh'})"}), None
    return f"hello from fake codex ({len(prompt)} chars{', resumed' if resumed else ''})", None


def rollout(thread, usage):
    d = os.path.join(os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex")), "sessions", "2026", "01", "01")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"rollout-2026-01-01T00-00-00-{thread}.jsonl"), "a") as f:
        f.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": usage},
                "rate_limits": {"primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": time.time() + 3600},
                                "secondary": {"used_percent": 34.0, "window_minutes": 10080, "resets_at": time.time() + 86400}}}}) + "\n")


if a[:1] == ["app-server"]:  # JSON-RPC over stdio, like `codex app-server`
    out = lambda m: print(json.dumps(m), flush=True)
    threads = {}  # id -> {"turns": n, "ephemeral": bool, "total": {...}}
    LIM = {"primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": int(time.time()) + 3600},
           "secondary": {"usedPercent": 34, "windowDurationMins": 10080, "resetsAt": int(time.time()) + 86400}}
    for line in sys.stdin:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if "id" not in m:
            continue  # client notifications (initialized)
        meth, p, rid = m.get("method"), m.get("params") or {}, m["id"]
        if meth == "thread/start":
            tid = str(uuid.uuid4()); threads[tid] = {"turns": 0, "ephemeral": p.get("ephemeral", True), "total": {}}
            out({"id": rid, "result": {"thread": {"id": tid}}})
        elif meth == "thread/resume":
            threads.setdefault(p["threadId"], {"turns": 1, "ephemeral": False, "total": {}})
            out({"id": rid, "result": {"thread": {"id": p["threadId"]}}})
        elif meth == "turn/start":
            tid = p["threadId"]; th = threads.setdefault(tid, {"turns": 1, "ephemeral": False, "total": {}})
            prompt = "\n".join(x.get("text", "") for x in p.get("input") or [] if x.get("type") == "text")
            out({"id": rid, "result": {"turn": {"id": "turn-" + str(th["turns"])}}})
            if "FAKE_HANG" in prompt:
                continue  # never finishes on its own: the router must interrupt it
            text, err = answer(prompt, p.get("model") or "", p.get("outputSchema") is not None, th["turns"] > 0)
            th["turns"] += 1
            note = lambda method, **kw: out({"method": method, "params": dict(kw, threadId=tid)})
            if err:
                note("error", error={"message": err}, willRetry=False)
                note("turn/completed", turn={"status": "failed", "error": {"message": err}})
                continue
            for i in range(0, len(text), 7):
                note("item/agentMessage/delta", delta=text[i:i + 7])
            note("item/completed", item={"type": "agentMessage", "text": text})
            last = {"inputTokens": 1000, "cachedInputTokens": 400, "outputTokens": 10, "reasoningOutputTokens": 0}
            th["total"] = {k: th["total"].get(k, 0) + v for k, v in last.items()}
            note("thread/tokenUsage/updated", tokenUsage={"last": last, "total": th["total"]})
            note("turn/completed", turn={"status": "completed"})
            if not th["ephemeral"]:
                rollout(tid, {"input_tokens": 1000})
        elif meth == "turn/interrupt":
            out({"id": rid, "result": {}})
            out({"method": "turn/completed", "params": {"threadId": p.get("threadId"), "turn": {"status": "interrupted"}}})
        elif meth == "account/rateLimits/read":
            out({"id": rid, "result": {"rateLimits": LIM}})
        elif meth == "account/read":
            out({"id": rid, "result": {"account": {"type": "chatgpt", "email": "fake@example.com", "planType": "plus"}}})
        else:  # initialize, thread/unsubscribe, ...
            out({"id": rid, "result": {}})
    sys.exit(0)

if not a or a[0] != "exec":
    sys.exit(1)

prompt = sys.stdin.read() if a[-1] == "-" else a[-1]
resume = a[1] == "resume"
thread = a[a.index("--") - 1] if resume else str(uuid.uuid4())
ev = lambda **e: print(json.dumps(e), flush=True)

ev(type="thread.started", thread_id=thread)
ev(type="turn.started")
model = a[a.index("-m") + 1] if "-m" in a else ""
if "FAKE_HANG" in prompt:
    time.sleep(3600)
text, err = answer(prompt, model, "--output-schema" in a, resume)
if err:
    ev(type="error", message=err)
    ev(type="turn.failed", error={"message": err})
    sys.exit(1)
ev(type="item.completed", item={"type": "agent_message", "text": text})
usage = {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 10, "reasoning_output_tokens": 0}
ev(type="turn.completed", usage=usage)

if "--ephemeral" not in a:  # like codex, persist a rollout with rate limits
    rollout(thread, usage)
