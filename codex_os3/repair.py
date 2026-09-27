"""Checks and repairs of the model's tool calls before they reach the app.
Generic JSON/schema checks plus OS3-specific fixes (node ids, dlam scripts)."""
import json, os, re, uuid

def load_args(a):
    """Parse tool args. Models write shell escapes (backslash-paren) unescaped, which is
    invalid JSON: double any backslash that doesn't start a valid JSON escape, then retry."""
    try:
        return json.loads(a)
    except ValueError:
        # consume escape pairs left-to-right so an already-valid "\\\\" pair is never split
        return json.loads(re.sub(r'\\(["\\/bfnrtu])|\\',
                                 lambda m: m.group(0) if m.group(1) else "\\\\", a))


def _norm(v):
    return re.sub(r"[^a-z0-9]", "", str(v).lower())


def fix_node_id(args, tool, prompt):
    """Models often pass a node's name ("Studio Mini") instead of its id. Map it back
    using the <node id=.. name=..> list rabbit puts in the prompt."""
    if "node_id" not in ((tool or {}).get("parameters") or {}).get("properties", {}):
        return args  # only a top-level node_id param; nested ones (e.g. files[].node_id) are left alone
    nodes = {}
    for m in re.finditer(r'<node id="([^"]+)" name="([^"]*)"([^>]*)>(.*?)</node>', prompt, re.S):
        host = re.search(r"<hostname>([^<]+)</hostname>", m.group(4))
        nodes[m.group(1)] = [m.group(2), host.group(1) if host else ""]
    v = args.get("node_id")
    if not nodes or v in nodes:
        return args
    if v:
        key = _norm(v)
        hits = [i for i, keys in nodes.items()
                if key and any(key == _norm(k) or key == _norm(k).split("local")[0] for k in keys + [i])]
        if len(hits) == 1:
            args["node_id"] = hits[0]
            return args
        # a mistyped id (models garble a character or two of a UUID): take the one known
        # id within 3 differing characters, never a guess between several
        near = [i for i in nodes if len(i) == len(v) and sum(a != b for a, b in zip(i, v)) <= 3]
        if len(near) == 1:
            args["node_id"] = near[0]
            return args
    if not v and len(nodes) == 1:
        args["node_id"] = next(iter(nodes))
    return args


def local_node(prompt):
    """The node id of this machine (where Codex saves generated images), from rabbit's node list:
    matched by hostname, else the default node, else the only one. None when unknown."""
    import socket
    me = socket.gethostname().split(".")[0].lower()
    nodes = [(m.group(1), m.group(2), (re.search(r"<hostname>([^<]+)</hostname>", m.group(3)) or [None, ""])[1])
             for m in re.finditer(r'<node id="([^"]+)"([^>]*)>(.*?)</node>', prompt or "", re.S)]
    for nid, _, host in nodes:
        if host and host.split(".")[0].lower() == me:
            return nid
    for nid, attrs, _ in nodes:
        if 'default="true"' in attrs:
            return nid
    return nodes[0][0] if len(nodes) == 1 else None


DLAM_SCRIPTS = ("probe.py", "capture.py", "act.py")


def fix_computer_use(name, args):
    """computer_use only runs probe.py/capture.py/act.py. Models write an act.py action
    as the script ("wait", "left_click") or drop the .py; rabbit then rejects the call
    with 'Bounded desktop script and literal arguments required'."""
    if name != "computer_use" or not isinstance(args, dict):
        return args
    script = str(args.get("script", ""))
    if not script or script in DLAM_SCRIPTS:
        return args
    base = script.rsplit("/", 1)[-1]
    if base in DLAM_SCRIPTS:
        args["script"] = base
    elif base + ".py" in DLAM_SCRIPTS:
        args["script"] = base + ".py"
    else:  # an action name: it belongs to act.py
        args["script"] = "act.py"
        args["args"] = [base, *(args.get("args") or [])]
    return args


_ACT = None


def _act_module():
    """act.py's own argparse, loaded once: an act.py call is valid iff it parses."""
    global _ACT
    if _ACT is None:
        import importlib.util
        path = os.path.expanduser("~/.os3/computer-use/act.py")
        try:
            spec = importlib.util.spec_from_file_location("os3_act", path)
            _ACT = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(_ACT)
        except Exception:
            _ACT = False
    return _ACT


def _act_problem(argv):
    import contextlib, io
    act = _act_module()
    if not act or not hasattr(act, "_parse_action"):
        return None
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            act._parse_action([str(a) for a in argv])
        return None
    except SystemExit:
        try:
            return json.loads(out.getvalue().strip().splitlines()[-1]).get("error") or "invalid act.py arguments"
        except (ValueError, IndexError):
            return "invalid act.py arguments"


_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
          "array": list, "object": dict}


def _schema_problems(val, schema, path):
    if not isinstance(schema, dict):
        return []
    t = schema.get("type")
    if isinstance(t, str) and t in _TYPES and not (isinstance(val, _TYPES[t]) and
                                                  not (t in ("integer", "number") and isinstance(val, bool))):
        return [f"{path} must be {t}"]
    if "enum" in schema and val not in schema["enum"]:
        return [f"{path} must be one of {schema['enum']}"]
    out = []
    if isinstance(val, dict):
        props = schema.get("properties", {})
        out += [f"{path}.{k} is required" for k in schema.get("required", []) if k not in val]
        for k, v in val.items():
            out += _schema_problems(v, props.get(k), f"{path}.{k}")
    elif isinstance(val, list) and isinstance(schema.get("items"), dict):
        for i, v in enumerate(val):
            out += _schema_problems(v, schema["items"], f"{path}[{i}]")
    return out


def call_problems(name, args, tool):
    """Everything wrong with one tool call that would make the app reject it."""
    if tool is None:
        return [f"there is no tool named {name!r}"]
    if not isinstance(args, dict):
        return [f"{name}: arguments must be a JSON object"]
    probs = [p.replace("$", name, 1) for p in _schema_problems(args, tool.get("parameters") or {}, "$")]
    if name == "computer_use":
        script, argv = args.get("script"), args.get("args") or []
        if script == "act.py" and isinstance(argv, list):
            err = _act_problem(argv)
            if err:
                probs.append(f"computer_use act.py {' '.join(map(str, argv[:1]))}: {err}")
        elif script == "capture.py" and not (isinstance(argv, list) and "--out" in argv and
                                              str((argv + [""])[argv.index("--out") + 1]).startswith("/")):
            probs.append("computer_use capture.py needs args [\"--out\", <absolute screenshotPath>]")
        elif script == "probe.py" and argv:
            probs.append("computer_use probe.py takes no args")
    return probs


def decision_problems(d, tools, node_src, own_images=False):
    if d.get("kind") != "tool_call":
        return []
    calls = [c for c in (d.get("calls") or ([d] if d.get("tool") else [])) if isinstance(c, dict)]
    if not calls:
        return ["kind is tool_call but calls is empty"]
    by_name = {t.get("function", t).get("name"): t.get("function", t) for t in tools}
    out = []
    for c in calls:
        name, tool = c.get("tool"), by_name.get(c.get("tool"))
        try:
            args = load_args(c.get("arguments_json") or "{}")
        except ValueError as e:
            out.append(f"{name}: arguments_json is not valid JSON ({e})")
            continue
        if isinstance(args, dict):  # judge the call as it will be sent, after the auto-fixes
            args = fix_computer_use(name, fix_node_id(args, tool, node_src))
        out += call_problems(name, args, tool)
        if own_images and name == "image_generate":
            out.append("image_generate: don't call it (it needs a paid image provider). Create the image with your "
                       "own built-in image generation instead, then call report_result_files with its saved path")
    return out


def add_missing_feed(out_calls, tools):
    """A screenshot the model never looks at is useless: models sometimes capture and then
    act blind. If a turn's last capture.py isn't followed by feed_image, append one."""
    if not any(t.get("function", t).get("name") == "feed_image" for t in tools):
        return out_calls
    last_cap = None
    for i, c in enumerate(out_calls):
        try:
            a = json.loads(c["function"]["arguments"])
        except ValueError:
            continue
        if c["function"]["name"] == "computer_use" and a.get("script") == "capture.py":
            argv = a.get("args") or []
            if "--out" in argv and argv.index("--out") + 1 < len(argv):
                last_cap = (i, argv[argv.index("--out") + 1], a.get("node_id"))
    if not last_cap:
        return out_calls
    i, path, node = last_cap
    if any(c["function"]["name"] == "feed_image" for c in out_calls[i + 1:]):
        return out_calls
    return out_calls + [{"id": f"call_{uuid.uuid4().hex[:24]}", "type": "function",
                         "function": {"name": "feed_image",
                                      "arguments": json.dumps({"reference": path, "node_id": node})}}]


