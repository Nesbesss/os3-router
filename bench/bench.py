#!/usr/bin/env python3
"""Plays rabbit's worker loop against the router: sends the request, executes the
returned computer_use / feed_image calls for real on this Mac, feeds results back.
Must run inside Terminal.app (it holds Screen Recording + Accessibility; sshd doesn't).
usage: bench.py <task,...> <runs> [model]"""
import base64, glob, json, os, subprocess, sys, time, urllib.request, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from codex_os3 import config  # noqa: E402

_cfg = config.ensure_key()
ROUTER = f"http://127.0.0.1:{_cfg['port']}/v1/chat/completions"
KEY = _cfg["api_key"]
NODE = "11111111-2222-4333-8444-555555555555"  # fake node id; must match template.json
CU = os.path.expanduser("~/.os3/computer-use")
MAX_STEPS = 30
NODE_ERR = "Error: This operation requires node_id to name one of your registered devices. No device operation was executed."
TPL = json.load(open(os.path.join(HERE, "template.json")))
LOG = open(os.path.join(HERE, "log.txt"), "a", buffering=1)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=LOG)


def desk(n):
    return glob.glob(os.path.expanduser(f"~/Desktop/bench-{n}*")) + \
        glob.glob(os.path.expanduser(f"~/Library/Mobile Documents/com~apple~TextEdit/Documents/bench-{n}*"))


CODES = {}


def make_page(n):
    import random
    code = CODES[n] = str(random.randint(100000, 999999))
    os.makedirs(os.path.expanduser("~/bench-pages"), exist_ok=True)
    open(os.path.expanduser(f"~/bench-pages/bench-{n}.html"), "w").write(
        "<html><body style='font:28px sans-serif;padding:60px'><h1>Bench page</h1>"
        "<button style='font-size:28px;padding:16px 32px' onclick=\"document.getElementById('c')"
        f".textContent='{code}'\">Reveal code</button><p id='c' style='font-size:64px'></p></body></html>")


PLANNER = os.environ.get("BENCH_PLANNER_URL", "")  # a school planner in Excel Online, one sheet tab per subject
SUBJECTS = ("aardrijkskunde", "geography", "ak", "biologie", "biology", "frans", "french", "duits", "german",
            "wiskunde", "math", "engels", "english", "geschiedenis", "history", "economie", "nederlands", "dutch")

TASKS = {
    "textedit": dict(
        text="Open the TextEdit app on Bench-Mac, create a new document, type exactly "
             "`bench {n} ok`, save it to the Desktop with the file name bench-{n}, then close "
             "the document.",
        pre=lambda n: [subprocess.run(["pkill", "-x", "TextEdit"]), [os.remove(f) for f in desk(n)]],
        check=lambda n, final: any("bench %s ok" % n in open(f, errors="ignore").read().lower()
                                   for f in desk(n) if os.path.isfile(f) and "/Desktop/" in f),
        post=lambda n: [subprocess.run(["pkill", "-x", "TextEdit"]), [os.remove(f) for f in desk(n) if os.path.isfile(f)]]),
    "calculator": dict(
        text="Open the Calculator app on Bench-Mac and use it (click its buttons or type "
             "into it) to compute 1234 × 56. Report the number the Calculator shows.",
        pre=lambda n: subprocess.run(["pkill", "-x", "Calculator"]),
        check=lambda n, final: any(s in final for s in ("69104", "69,104", "69.104")),  # + must have looked (see run)
        post=lambda n: subprocess.run(["pkill", "-x", "Calculator"])),
    "reveal": dict(
        text="In Google Chrome on Bench-Mac, open a new tab, go to "
             "file://{home}/bench-pages/bench-{n}.html, click the 'Reveal code' button on "
             "that page, report the 6-digit code that appears, then close that tab again.",
        pre=lambda n: make_page(n),
        check=lambda n, final: CODES.get(n, "x") in final,
        post=lambda n: [os.remove(f) for f in glob.glob(os.path.expanduser(f"~/bench-pages/bench-{n}.html"))]),
    "planner": dict(  # the real-life failure: content drawn by Excel Online, subjects on separate sheet tabs
        text="In Google Chrome on Bench-Mac, the homework planner (A3 Planner 2026_2027, Excel Online) is open. "
             "It has one sheet tab per subject in the bar at the bottom; it opens on the Dutch tab (A3-Ne). "
             "Go through the subject tabs one by one and list every test (toets, SO, PW, leestoets, "
             "proefwerk, exam) you find: subject, week/date and what it is. Report the full list.",
        pre=lambda n: [subprocess.run(["open", "-a", "Google Chrome", PLANNER]), time.sleep(10)],
        check=lambda n, final: ("toets" in final.lower() or "test" in final.lower())
                               and sum(s in final.lower() for s in SUBJECTS) >= 3,
        post=lambda n: None, steps=70),
    "planner2": dict(  # short version for A/B runs: two tabs, same screen as the full task
        text="In Google Chrome on Bench-Mac, the homework planner (A3 Planner 2026_2027, Excel Online) is open. "
             "It has one sheet tab per subject in the bar at the bottom. Open only these two tabs: A3-Ak "
             "(aardrijkskunde) and A3-Na. On each, list every test (toets, SO, PW, leestoets, proefwerk, exam) "
             "you find: subject, week/date and what it is. Report the list.",
        pre=lambda n: [subprocess.run(["open", "-a", "Google Chrome", PLANNER]), time.sleep(10)],
        check=lambda n, final: any(w in final.lower() for w in ("toets", "so ", "pw", "test"))
                               and "ak" in final.lower() and "na" in final.lower(),
        post=lambda n: None, steps=30),
    "chrome": dict(
        text="In Google Chrome on Bench-Mac, open a new tab, go to https://example.com, "
             "report the main heading shown on that page, then close that tab again.",
        pre=lambda n: None,
        check=lambda n, final: "example domain" in final.lower(),
        post=lambda n: None),
}


def post(body):
    req = urllib.request.Request(ROUTER, json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Authorization": "Bearer " + KEY})
    return json.load(urllib.request.urlopen(req, timeout=600))["choices"][0]["message"]


def run_cu(args):
    script = args.get("script")
    if script not in ("probe.py", "capture.py", "act.py") or not isinstance(args.get("args", []), list):
        return "Error: Bounded desktop script and literal arguments required"
    t = time.time()
    try:
        p = subprocess.run(["python3", os.path.join(CU, script), *map(str, args.get("args", []))],
                           capture_output=True, text=True, timeout=120)
        code, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        code, out, err = 143, "", "timeout"
    return json.dumps({"commandId": str(uuid.uuid4()), "exitCode": code, "stdout": out, "stderr": err,
                       "stdoutTruncated": False, "stderrTruncated": False,
                       "durationMs": int((time.time() - t) * 1000), "canceled": False})


def execute(call, stats):
    """Returns (tool_result_text, image_bytes_or_None) like rabbit's executor."""
    name = call["function"]["name"]
    try:
        a = json.loads(call["function"]["arguments"] or "{}")
    except ValueError:
        stats["errors"].append("bad json")
        return "Error: arguments are not valid JSON", None
    stats["calls"].append(name + (":" + " ".join(map(str, a.get("args", [])[:1])) if name == "computer_use" and a.get("script") == "act.py" else
                                  (":" + str(a.get("script")) if name == "computer_use" else "")))
    if name == "skill_view":
        return TPL["skill"], None
    if name == "computer_use_prepare":
        return TPL["prepare"], None
    if name == "notify_before_act":
        return "The line went out to the user. Do the work now.", None
    if name == "wait":
        time.sleep(1)
        return "ok", None
    if name == "ask_user":
        stats["asked"] += 1
        return "User response: Yes, you have my permission to operate Mac.localdomain for this task. Go ahead.", None
    if name in ("computer_use", "feed_image") and a.get("node_id") != NODE:
        stats["errors"].append(f"{name}: bad node_id {a.get('node_id')!r}")
        return NODE_ERR, None
    if name == "computer_use":
        r = run_cu(a)
        if r.startswith("Error") or json.loads(r)["exitCode"] != 0:
            stats["errors"].append(f"cu {a.get('script')} {a.get('args', [])[:2]}: {r[:160]}")
        return r, None
    if name == "feed_image":
        ref = a.get("reference", "")
        if not os.path.isfile(ref):
            stats["errors"].append("feed_image missing file")
            return f"Error: file not found: {ref}", None
        data = open(ref, "rb").read()
        return json.dumps({"tool": "feed_image", "kind": "local-file", "source": ref,
                           "mimeType": "image/png", "bytes": len(data)}), data
    stats["errors"].append("unsupported tool " + name)
    return f"Error: {name} is not available in this environment.", None


def run(task, n, model):
    T = TASKS[task]
    T["pre"](n)
    text = T["text"].format(n=n, home=os.path.expanduser("~"))
    msgs = [TPL["system"], {"role": "user", "content": [{"type": "text", "text": f"<task>{text}</task>"}]}]
    stats = {"calls": [], "errors": [], "asked": 0}
    t0, final, steps = time.time(), "", 0
    for steps in range(1, T.get("steps", MAX_STEPS) + 1):
        try:
            m = post({"model": model, "tools": TPL["tools"], "messages": msgs})
        except Exception as e:
            stats["errors"].append("router: " + str(e)[:160])
            break
        m = {k: v for k, v in m.items() if v is not None or k == "content"}
        msgs.append(m)
        if not m.get("tool_calls"):
            final = m.get("content") or ""
            break
        images = []
        for c in m["tool_calls"]:
            res, img = execute(c, stats)
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": [{"type": "text", "text": res}]})
            if img:
                images.append((c["id"], img))
                d = os.path.join(HERE, "shots", f"{task}-{n}"); os.makedirs(d, exist_ok=True)
                open(os.path.join(d, f"{steps:02d}.png"), "wb").write(img)
        for cid, img in images:
            msgs.append({"role": "user", "content": [
                {"type": "text", "text": f"Image output of tool call {cid}:"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(img).decode()}}]})
    looked = stats["calls"].count("feed_image")
    ok = bool(T["check"](n, final)) and looked > 0
    os.makedirs(os.path.join(HERE, "transcripts"), exist_ok=True)
    slim = [{**m, "content": [p if p.get("type") != "image_url" else {"type": "image_url", "image_url": "<img>"}
                              for p in m["content"]] if isinstance(m.get("content"), list) else m.get("content")}
            for m in msgs[1:]]
    json.dump(slim, open(os.path.join(HERE, "transcripts", f"{task}-{n}.json"), "w"), indent=1)
    rec = dict(task=task, n=n, model=model, ok=ok, steps=steps, secs=round(time.time() - t0),
               acts=sum(c.startswith("computer_use:") and not c.endswith((".py",)) for c in stats["calls"]),
               asked=stats["asked"], looked=looked, errors=stats["errors"], calls=stats["calls"], final=final[:300])
    with open(os.path.join(HERE, "results.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")
    log(f"{task} #{n}: {'OK ' if ok else 'FAIL'} steps={steps} {rec['secs']}s errors={len(stats['errors'])} looked={looked} asked={stats['asked']} | {final[:120]!r}")
    T["post"](n)
    return rec


if __name__ == "__main__":
    tasks, runs = sys.argv[1].split(","), int(sys.argv[2])
    model = sys.argv[3] if len(sys.argv) > 3 else "gpt-6-luna"
    # get this Terminal window out of the screenshots
    subprocess.run(["python3", os.path.join(CU, "act.py"), "key", "--keys", "cmd", "m", "--view-width", "1365",
                    "--view-height", "768", "--original-width", "1920", "--original-height", "1080"])
    log(f"=== start {tasks} x{runs} model={model}")
    for i in range(runs):
        for task in tasks:
            try:
                run(task, f"{int(time.time()) % 100000}", model)
            except Exception as e:
                log(f"{task}: bench crash {e!r}")
    log("=== done")
