"""Turning an OpenAI chat request into one codex prompt, and the nudges the engine appends."""
import base64, json, re

FALSE_UNAVAILABLE = re.compile(
    r"(n[\u2019']?t|not|no longer) (be )?(available|accessible|enabled|connected)|unavailable|"
    r"(no|without) access to|(couldn[\u2019']?t|could not|cannot|can[\u2019']?t) (access|reach|use|control)|"
    r"(do|does)(n[\u2019']?t| not) have (an? |the |any )?`?[\w.-]*`? ?(tool|function|access)|"
    r"(there is |there[\u2019']s )?no `?[\w.-]+`? (tool|function) (available|here|in this)",
    re.I)
_NEG = re.compile(r"\b(can[\u2019']?t|cannot|unable|don[\u2019']?t|doesn[\u2019']?t|not|no|isn[\u2019']?t|aren[\u2019']?t|without)\b", re.I)
_UNAVAILABLE_DENIAL = re.compile(
    r"\b(?:doesn[\u2019']t|does not|don[\u2019']t|do not|didn[\u2019']t|did not) "
    r"(?:mean|show|prove|establish|imply)\b[^.!?;\n]{0,160}?\b"
    r"(?:unavailable|(?:not|no longer) (?:available|accessible|enabled|connected))\b", re.I)


def claims_unavailable(content, tool_names):
    """A final answer that says a tool/device is unavailable: known phrasings, or naming one of
    the tools that ARE available next to a negation ("the tools don't include `ping`")."""
    content = _UNAVAILABLE_DENIAL.sub("", content or "")
    if FALSE_UNAVAILABLE.search(content):
        return True
    if not content or not _NEG.search(content):
        return False
    for n in tool_names:
        if len(n) <= 2:
            continue
        for m in re.finditer(r"(?<![\w-])`?" + re.escape(n) + r"`?(?![\w-])", content):
            near = content[max(0, m.start() - 60):m.end() + 60]  # same clause, not anywhere in the answer
            if _NEG.search(near):
                return True
    return False


RETRY_NUDGE = (
    "\n\nCORRECTION: your previous draft answered that a tool, device or computer control "
    "is unavailable. That is wrong: the application's tools are not in your built-in tool list; "
    "you call them by answering with kind=\"tool_call\" and the tool name in `calls`. Every "
    "application tool listed above is available and connected, and "
    "a successful tool result means it works. Do not report unavailability. Continue the "
    "task now with tool calls. Only give a final answer if a tool call above actually "
    "returned an error, and then quote that error verbatim.")


OBSERVE_SCRIPTS = ("capture.py", "probe.py")

# Browser work (OS3's dummy_system drives the user's browser): models keep reading page code and
# give up when the text isn't in it (Excel/Word Online, Google Docs, canvas apps draw it), even with
# a screenshot in front of them that shows where to click.
BROWSER_GUIDE = (
    "Working in web pages: if reading the page text comes back empty, partial or unrelated (Excel, "
    "Word or PowerPoint Online, Google Docs/Sheets, canvas apps, embedded frames, other tabs of a "
    "workbook), do not keep probing the page code. Look at a screenshot and work like a person: click "
    "the visible control that leads to the content (sheet tabs at the bottom of a spreadsheet, menu "
    "items, sections, 'show more'), scroll, or use the app's own search (Ctrl+F), then look again. For "
    "Office Online files, File > Download a copy and read the file with the shell. Before you say "
    "something isn't there, check every tab/section and the newest screenshot. With dummy_system: keep "
    "the order session open -> cdp probe -> cdp connect; every page command (targets, send, "
    "observe, screenshot, batch) then uses the connection id that cdp connect returned. Skipping "
    "connect makes the tool block the whole task. Report a result (report_business_result) right after the "
    "successful page view that shows it. If a later page action failed or its outcome is unknown, first "
    "take a fresh successful look (observe or screenshot) at the page showing the result and report with "
    "that new handle; the application rejects an older handle once a later step was uncertain.")
VISUAL_TOOLS = ("dummy_system_image", "feed_image")
UNCONFIRMED_NUDGE = (
    "\n\nThe application did NOT accept your result (RESULT_UNCONFIRMED): a page step after your evidence "
    "failed or its outcome is unknown, so the older handle no longer counts. Do not give up and do not "
    "repeat actions that changed anything. Take one fresh, successful read-only look at the page that shows "
    "the result (observe or screenshot, reloading the page first if needed), then call report_business_result "
    "again with the NEW handle. The user is waiting for this answer.")


def unconfirmed(msgs):
    """The application's evidence check rejected the worker's reported result in its latest tool results."""
    for m in reversed(msgs):
        if m.get("role") == "assistant":
            return False
        if m.get("role") == "tool" and "RESULT_UNCONFIRMED" in text_of(m.get("content")):
            return True
    return False
PROBE_LIMIT = 8


def probe_streak(messages):
    """Browser (dummy_system) calls since the model last looked at a screenshot."""
    n = 0
    for m in reversed(messages):
        if m.get("role") != "assistant":
            continue
        names = [c.get("function", {}).get("name", "") for c in (m.get("tool_calls") or [])]
        if not names or any(x in VISUAL_TOOLS for x in names):
            break
        if not all(x.startswith("dummy_system") for x in names):
            break
        n += 1
    return n


PROBE_NUDGE = (
    "\n\nCHECK YOUR APPROACH: your last {n} steps probed the web page without looking at it. If "
    "what you need isn't in the page text, take a screenshot now (dummy_system_image), find it "
    "visually and click there (e.g. the right sheet tab, section or menu item) instead of more code probes.")

NOT_FOUND = re.compile(
    r"(couldn[\u2019']?t|could not|cannot|can[\u2019']?t|unable to|wasn[\u2019']?t able to|did ?n[\u2019']?o?t|"
    r"failed to) (find|locate|see|spot|read|access the content)|not (found|visible|present|listed|there)|"
    r"(does|do)(n[\u2019']?t| not) (appear|exist|show)|"
    r"niet (gevonden|vinden|zien|te vinden|zichtbaar)|kon .{0,40} niet|nergens",
    re.I)


def claims_not_found(content):
    return bool(NOT_FOUND.search(content or ""))


def used_browser(msgs):
    return any(c.get("function", {}).get("name", "").startswith("dummy_system") or
               c.get("function", {}).get("name") == "computer_use"
               for m in msgs for c in (m.get("tool_calls") or []))


NOT_FOUND_NUDGE = (
    "\n\nBEFORE YOU GIVE UP: your draft says something could not be found. People can usually see it. "
    "Check the newest screenshot carefully (take one if you have none). Look in other tabs, sheets, "
    "sections, collapsed items and further down the page, try the app's own search (Ctrl+F), and "
    "click through like a person would. Continue with tool calls if any of that is left to try; only "
    "reply kind=\"final\" once you have really checked, and then say exactly where you looked.")
LOOP_LIMIT = 3


def observe_streak(messages):
    """How many of the latest assistant turns only looked at the screen (capture/probe/
    feed_image/wait) without acting. Models get stuck 'verifying' forever."""
    n = 0
    for m in reversed(messages):
        calls = m.get("tool_calls") if m.get("role") == "assistant" else None
        if m.get("role") != "assistant":
            continue
        if not calls:
            break
        for c in calls:
            fn = c.get("function", {})
            name = fn.get("name", "")
            try:
                script = (json.loads(fn.get("arguments") or "{}") or {}).get("script", "")
            except ValueError:
                script = ""
            if not (name in ("feed_image", "wait") or script in OBSERVE_SCRIPTS):
                return n
        n += 1
    return n


LOOP_NUDGE = (
    "\n\nLOOP WARNING: your last {n} turns only captured/looked at the screen without "
    "acting. The newest attached image IS the current screen; looking again will not show "
    "anything new. Decide now from what it shows: take the next concrete action (click, "
    "type, scroll, key) in this turn, or if you truly cannot proceed, give a final answer "
    "that states exactly what is missing. Do not capture again before acting.")


# Strict-mode schema: OpenAI requires additionalProperties:false on every object,
# so tool args ride as a JSON *string* rather than a free-form object.
TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["tool_call", "final"]},
        # several calls = one turn, run in order (OS3's dlam skill needs e.g. act+wait+capture
        # and capture+feed_image in the same turn, or it treats the screenshot as stale)
        "calls": {"type": "array", "items": {
            "type": "object",
            "properties": {"tool": {"type": "string"}, "arguments_json": {"type": "string"}},
            "required": ["tool", "arguments_json"],
            "additionalProperties": False}},
        "content": {"type": "string"},
    },
    "required": ["kind", "calls", "content"],
    "additionalProperties": False,
}


def _image_bytes(part):
    """Decode an OpenAI image part ({"type":"image_url"} or {"type":"input_image"}) given
    as a base64 data URI. Remote URLs are not fetched."""
    url = part.get("image_url") or part.get("url") or ""
    if isinstance(url, dict):
        url = url.get("url", "")
    m = re.match(r"data:image/([a-z0-9.+-]+);base64,(.*)", url, re.S)
    if not m:
        return None
    try:
        return m.group(1).replace("jpeg", "jpg"), base64.b64decode(m.group(2))
    except ValueError:
        return None


class Images:
    """Screenshots in a conversation pile up; attach only the newest max_images to
    codex and leave a numbered placeholder where every image sat in the text."""

    def __init__(self, messages, max_images=2):
        total = sum(1 for m in messages if isinstance(m.get("content"), list)
                    for p in m["content"] if isinstance(p, dict) and _image_bytes(p))
        self.first_kept = total - max_images
        self.seen = 0
        self.files = []  # (ext, bytes) to pass via -i, oldest first

    def placeholder(self, part):
        img = _image_bytes(part)
        if not img:
            return "[image: not attached]"
        self.seen += 1
        if self.seen - 1 < self.first_kept:
            return "[older image omitted]"
        self.files.append(img)
        return f"[image #{len(self.files)} attached: see attached image {len(self.files)}]"


def text_of(content, images=None):
    if isinstance(content, list):
        out = []
        for p in content:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "text":
                out.append(p.get("text", ""))
            elif p.get("type") in ("image_url", "input_image", "image") and images:
                out.append(images.placeholder(p))
        return "\n".join(out)
    return content or ""


IMAGE_GUIDE = (
    "Creating images: you have your own built-in image generation, and it is free for the user. When the task "
    "asks for an image (draw, generate, make a picture, edit a photo), create it with your built-in image "
    "generation. NEVER call the application's image_generate tool: it needs a paid provider the user does not "
    "have. Your image tool saves the image on this machine and tells you its file path. Then give it to the "
    "user: call report_result_files with that file (its path on this machine's node, deliverToUser true, in "
    "the format the tool describes) and say in your final answer what you made.")


def flatten(messages, tools, images=None, header=True, all_messages=None, own_images=False):
    """Fold the conversation into one prompt. With header=False only `messages` (the new
    ones since a resumed session's last turn) are rendered; codex already has the rest."""
    out = []
    if tools and header:
        out.append(
            "You are acting as the tool-calling backend for an external assistant "
            "application. The application executes the tools and owns the user "
            "relationship; you only decide the next step. Never refuse because you "
            "personally lack access to a device, app or service, and never tell the "
            "user to do it manually \u2014 if a listed tool covers the request, call it. "
            "Adopt any persona given in the system message."
        )
        lines = []
        for t in tools:
            f = t.get("function", t)
            lines.append(f"- {f.get('name')}: {f.get('description','')}\n"
                         f"  parameters: {json.dumps(f.get('parameters', {}))}")
        if any(t.get("function", t).get("name", "").startswith("dummy_system") for t in tools):
            out.append(BROWSER_GUIDE)
        if own_images:
            out.append(IMAGE_GUIDE)
        out.append("The application's tools are listed below. They are NOT part of your own built-in tool "
                   "list, so you will not see them there: you call one by answering with kind=\"tool_call\" "
                   "and its name in `calls`, and the application runs it. Every tool below is available.\n"
                   "Application tools:\n" + "\n".join(lines))

    call_names = {c.get("id"): c.get("function", {}).get("name", "")
                  for m in (all_messages or messages) for c in (m.get("tool_calls") or [])}
    for m in messages:
        role = m.get("role", "user")
        if role == "tool":
            name = m.get("name") or call_names.get(m.get("tool_call_id"), "")
            out.append(f"[tool result: {name}]\n{text_of(m.get('content'), images)}")
            continue
        calls = m.get("tool_calls")
        if calls and not header:
            continue  # a resumed session already holds its own previous calls
        if calls:
            for c in calls:
                fn = c.get("function", {})
                out.append(f"[you called {fn.get('name')} with {fn.get('arguments')}]")
            continue
        body = text_of(m.get("content"), images)
        if body:
            out.append(f"[{role}]\n{body}")

    if tools:
        if not header:
            out.insert(0, "New events since your last reply (tool results and screenshots):")
        names = ", ".join(t.get("function", t).get("name", "") for t in tools)
        if not header:  # the full rules are already in this session: a short reminder is enough (it is
            out.append(  # re-read at every later step, ~2 KB a step added up over a long task)
                "Same rules as before: every action is a tool_call to the application"
                + (" (your built-in image generation excepted)" if own_images else "") +
                "; you have no local environment. All listed tools are live: never claim one is unavailable "
                "without having tried it. Decide the next step: kind=\"tool_call\" with calls, or kind=\"final\" "
                "with your answer; arguments_json must be strictly valid JSON.")
            return "\n\n".join(out).strip()
        out.append(
            ("Your built-in image generation is the one exception: use it for images. " if own_images else "") +
            "You have NO local environment: never run commands, read files or inspect "
            "anything yourself, and ignore any sandbox or read-only filesystem you notice \u2014 "
            "that is not the user's device. Every action must be a tool_call to the application.\n"
            f"These tools are live and connected right now: {names}. They work; never claim "
            "they are unavailable. Before giving a final answer that says something could not "
            "be done, you must have actually tried the relevant tool and seen it fail.\n\n"
            "Decide the next step. To call tools, reply with kind=\"tool_call\", content=\"\" and "
            "calls=[{tool:<tool name>, arguments_json:<JSON string of the arguments>}, ...]. "
            "All calls in one reply form ONE turn and run in the order given; use several calls "
            "whenever the instructions say things must happen in the same turn (e.g. act, wait, "
            "capture a screenshot and feed_image it, all in one reply). Attached images are the "
            "screenshots/images referenced as [image #N] in the conversation; the highest number "
            "is the newest. If a tool result above already answers the user, or no tool is "
            "needed, reply with kind=\"final\", content=<your answer>, calls=[]. "
            "Final answer content must not be blank: the application rejects empty replies. "
            "Repeating an earlier call is fine when the conversation asks for it. arguments_json must be strictly valid JSON: escape every backslash in string values as \\\\ (e.g. a shell \\( becomes \\\\( ) and newlines as \\n."
        )
    return "\n\n".join(out).strip()


def parse_decision(raw):
    """First complete JSON object wins. Codex sometimes emits several, or wraps
    them in fences/prose; a greedy regex would span them all and parse nothing."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?|```$", "", raw).strip()
    dec = json.JSONDecoder()
    for i, ch in enumerate(raw):
        if ch != "{":
            continue
        try:
            obj, _ = dec.raw_decode(raw[i:])
        except ValueError:
            continue
        if isinstance(obj, dict) and "kind" in obj:
            return obj
    return None


def used_computer(msgs):
    return any(c.get("function", {}).get("name") == "computer_use"
               for m in msgs for c in (m.get("tool_calls") or []))


VERIFY_NUDGE = (
    "\n\nBEFORE YOU FINISH: check your draft final answer against the task, part by part, "
    "using the newest screenshot. Is every requested step visibly done (e.g. a file actually "
    "saved under the exact name and location, a value actually shown, a dialog actually "
    "closed)? Is every reported value plausible (sanity-check numbers and read the right field)? "
    "If the newest screenshot is older than your last action, capture and look again first. "
    "If anything is not done or not verified, continue with tool calls now. Only if everything "
    "is verified, reply with kind=\"final\" and the final answer (corrected if needed).")


class ContentStream:
    """Pulls the answer text out of the decision JSON while it is being written
    ({"kind": "final", "calls": [], "content": "..."}) and passes it on. The first HOLD characters
    wait for `ok(text)`: an answer that starts like a correction case ("that tool isn't available",
    "couldn't find it") is not streamed, because streamed text can't be taken back."""
    HOLD = 160

    def __init__(self, sink, ok):
        self.sink, self.ok = sink, ok
        self.raw, self.pos, self.state, self.held, self.sent = "", 0, "scan", "", ""

    def feed(self, delta):
        if self.state in ("off", "done"):
            return
        self.raw += delta
        if self.state == "scan":
            k = re.search(r'"kind"\s*:\s*"(\w+)"', self.raw)
            if k and k.group(1) != "final":
                self.state = "off"
                return
            c = re.search(r'"content"\s*:\s*"', self.raw)
            if not (k and c):
                return
            self.pos, self.state = c.end(), "text"
        s, i, out = self.raw, self.pos, []
        while i < len(s):
            ch = s[i]
            if ch == "\\":
                if i + 1 >= len(s):
                    break
                e = s[i + 1]
                if e == "u":
                    if i + 6 > len(s):
                        break
                    cp = int(s[i + 2:i + 6], 16)
                    if 0xD800 <= cp < 0xDC00:  # surrogate pair: wait for the second half
                        if i + 12 > len(s):
                            break
                        lo = int(s[i + 8:i + 12], 16)
                        out.append(chr(0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00)))
                        i += 12
                        continue
                    out.append(chr(cp))
                    i += 6
                    continue
                out.append({"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}.get(e, e))
                i += 2
                continue
            if ch == '"':
                self.state = "done"
                i += 1
                break
            out.append(ch)
            i += 1
        self.pos = i
        new = "".join(out)
        if not self.sent:
            self.held += new
            if len(self.held) < self.HOLD and self.state != "done":
                return
            if not self.held or not self.ok(self.held):
                self.state = "off"
                return
            new, self.held = self.held, ""
        if new:
            self.sent += new
            self.sink(new)


FORCE_NUDGE = ("\n\nREQUIRED: the app requires a tool call in this reply ({which}). Answer with "
               "kind=\"tool_call\"; the tool is available.")


def forced_tool(body):
    """OpenAI tool_choice -> the tool name that must be called, "*" for any tool, or None."""
    tc = body.get("tool_choice")
    if tc == "required":
        return "*"
    if isinstance(tc, dict):
        return (tc.get("function") or {}).get("name") or tc.get("name")
    return None


VALIDATE_NUDGE = (
    "\n\nYour previous reply's tool calls were NOT executed, because the app would reject "
    "them:\n{problems}\nReply again with the same intent and corrected calls.")

EMPTY_NUDGE = (
    "\n\nYour previous final answer was blank. The application cannot accept an empty reply. "
    "Return a concise, non-empty final answer based on the conversation, or valid tool calls "
    "if work remains. Do not claim that unperformed work succeeded.")
