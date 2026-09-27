#!/usr/bin/env python3
"""Stand-in for the Codex CLI in tests: same flags, JSON events and rollout files, no network.
Prompt triggers: FAKE_LIMIT (usage limit error), FAKE_HANG (sleep forever), FAKE_FINAL (final
answer instead of a tool call), FAKE_BADJSON (invalid tool-call arguments), FAKE_LIMIT_SOL (usage
limit only for *sol* models), FAKE_NOTFOUND (gives up with "couldn't find" until nudged)."""
import json, os, sys, time, uuid

a = sys.argv[1:]
if "--version" in a:
    print("codex-cli 0.0.0-fake")
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
if not a or a[0] != "exec":
    sys.exit(1)

prompt = sys.stdin.read() if a[-1] == "-" else a[-1]
resume = a[1] == "resume"
thread = a[a.index("--") - 1] if resume else str(uuid.uuid4())
ev = lambda **e: print(json.dumps(e), flush=True)

ev(type="thread.started", thread_id=thread)
ev(type="turn.started")
model = a[a.index("-m") + 1] if "-m" in a else ""
if "FAKE_LIMIT" in prompt and ("FAKE_LIMIT_SOL" not in prompt or "sol" in model):
    ev(type="error", message="You've hit your usage limit. Upgrade to Pro or try again at 9:11 PM.")
    ev(type="turn.failed", error={"message": "usage limit"})
    sys.exit(1)
if "FAKE_HANG" in prompt:
    time.sleep(3600)

if "FAKE_NOTFOUND" in prompt and "BEFORE YOU GIVE UP" not in prompt and "--output-schema" in a:
    text = json.dumps({"kind": "final", "calls": [], "content": "I couldn't find Extreme weather on the page."})
elif "--output-schema" in a and "FAKE_FINAL" not in prompt:
    args = '{"location": "Oslo"' if "FAKE_BADJSON" in prompt else '{"location":"Oslo"}'
    text = json.dumps({"kind": "tool_call", "content": "", "calls": [{"tool": "get_weather", "arguments_json": args}]})
elif "--output-schema" in a:
    text = json.dumps({"kind": "final", "calls": [], "content": f"fake answer ({'resumed' if resume else 'fresh'})"})
else:
    text = f"hello from fake codex ({len(prompt)} chars{', resumed' if resume else ''})"
ev(type="item.completed", item={"type": "agent_message", "text": text})
usage = {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 10, "reasoning_output_tokens": 0}
ev(type="turn.completed", usage=usage)

# like codex, persist a rollout with rate limits unless --ephemeral
if "--ephemeral" not in a:
    d = os.path.join(os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex")), "sessions", "2026", "01", "01")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"rollout-2026-01-01T00-00-00-{thread}.jsonl"), "a") as f:
        f.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": usage},
                "rate_limits": {"primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": time.time() + 3600},
                                "secondary": {"used_percent": 34.0, "window_minutes": 10080, "resets_at": time.time() + 86400}}}}) + "\n")
