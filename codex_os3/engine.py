"""The request pipeline: OpenAI chat request in, assistant message out.

main codex call (resumed session when possible, one fresh retry on failure/hang)
  -> optional self-corrections, each of which may only improve the answer and never
     fails the request: false "unavailable" claims, invalid tool calls, final answers
     after computer use that were not verified
  -> tool-call repair (JSON, node ids, dlam scripts, missing feed_image)."""
import json, os, time, uuid

from . import claude_runner, codex_runner, config, notify, prompt as P, repair, roles, sessions, store
from .codex_runner import ClientGone, CodexHung, UsageLimit


class EngineError(RuntimeError):
    pass


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


class Turn:
    """One /v1/chat/completions request."""

    def __init__(self, cfg, body, alive, source="?"):
        self.cfg, self.body, self.alive, self.source = cfg, body, alive, source
        self.role = roles.classify(body)
        self.requested = body.get("model") or cfg["model"]
        self.os3_effort = roles.requested_effort(body)
        # e.g. gpt-6-sol-medium, claude-sonnet-5-medium
        self.model = roles.pick(cfg, self.role, self.requested, self.os3_effort)
        self.backend = roles.backend(self.model)
        self.fell_back = False
        if (store.kv_get("limited:" + self.backend) or 0) > time.time():  # this subscription just hit its limit
            fb = self.fallback_model()
            if fb:
                self.model, self.backend, self.fell_back = fb, roles.backend(fb), True
        self.msgs = body.get("messages") or []
        tools = body.get("tools") or []
        if body.get("functions"):  # legacy shape
            tools = [{"function": f} for f in body["functions"]]
        self.tools = tools
        self.task = sessions.key(self.msgs, tools) if self.msgs else None
        self.schema = P.TOOL_SCHEMA if tools else None
        self.forced = P.forced_tool(body) if tools else None  # OS3's tool_choice (e.g. emit_facts)
        # device list lives in the system prompt; a resumed turn's prompt is only the delta
        self.node_src = "\n".join(P.text_of(m.get("content")) for m in self.msgs
                                  if m.get("role") == "system")
        self.rid = store.request_start(self.task, source, self.model, bool(body.get("stream")),
                                       len(tools), len(self.msgs), len(json.dumps(body)), self.role)
        self.tid = None
        self._note_params(body)

    @staticmethod
    def _note_params(body):
        """Record which request options OS3 sends (e.g. its reasoning sliders), once per new set.
        Only short scalar values; never messages or tools."""
        extra = {k: v for k, v in body.items() if k not in ("messages", "tools", "functions")}
        keys = sorted(extra)
        if store.kv_get("os3_params") != keys:
            store.kv_set("os3_params", keys)
            store.event("os3_params", json.dumps({k: v if isinstance(v, (str, int, float, bool, dict)) and len(json.dumps(v)) < 200
                                                  else "…" for k, v in extra.items()})[:500])

    def fallback_model(self, err=None):
        if err is not None and err.plan:
            return None
        fb = roles.pick_fallback(self.cfg, self.role, self.os3_effort)
        if not fb or fb == self.model:
            return None
        if roles.backend(fb) != self.backend and (store.kv_get("limited:" + roles.backend(fb)) or 0) > time.time():
            return None  # the fallback's subscription is out too
        return fb

    def ev(self, kind, msg, level="info", data=None):
        log(f"[{self.task}] {kind}: {msg}")
        store.event(kind, msg, task=self.task, level=level, data=data)

    def build(self, full, delta=None):
        src = self.msgs if full else delta
        imgs = P.Images(src, self.cfg["max_images"])
        p = (P.flatten(self.msgs, self.tools, imgs) if full else
             P.flatten(delta, self.tools, imgs, header=False, all_messages=self.msgs))
        if self.forced:
            p += P.FORCE_NUDGE.format(which="any tool" if self.forced == "*" else f"call `{self.forced}`")
        streak = P.observe_streak(self.msgs) if self.tools else 0
        if streak >= P.LOOP_LIMIT:
            p += P.LOOP_NUDGE.format(n=streak)
            self.ev("loop_nudge", f"{streak} observe-only turns, nudging to act")
        probes = P.probe_streak(self.msgs) if self.tools else 0
        if probes >= P.PROBE_LIMIT and probes % 4 == 0:  # not every turn: once, then every 4 more probes
            p += P.PROBE_NUDGE.format(n=probes)
            self.ev("probe_nudge", f"{probes} browser probes without a screenshot, nudging to look")
        return imgs, p

    def codex(self, prompt, images=(), resume=None, keep=False):
        runner = claude_runner if self.backend == "claude" else codex_runner
        text, usage, thread, limits = runner.run(
            self.cfg, prompt, self.model, self.schema, self.alive, images, resume, keep, role=self.role)
        store.add_tokens(self.rid, usage)
        store.add_limits(limits, self.backend)
        return text, thread

    def extra(self, name, prompt_resume, prompt_fresh, images=()):
        """A corrective extra codex call. Never fails the request: any error keeps the
        original answer."""
        try:
            if self.tid and self.tracked:
                return self.codex(prompt_resume, resume=self.tid)
            return self.codex(prompt_fresh, images, keep=self.tracked)
        except ClientGone:
            raise
        except Exception as e:  # hang, usage limit, codex error
            self.ev(name + "_failed", f"{type(e).__name__}: {str(e)[:160]}; keeping original answer", "warn")
            return None, None

    def run(self):
        """-> (message dict, finish_reason)."""
        tools = self.tools
        self.tracked, thread, delta = (sessions.plan(self.task, self.msgs)
                                       if tools and self.msgs else (False, None, None))
        images, prompt = self.build(full=not thread, delta=delta)
        if not prompt:
            if self.tracked:
                sessions.done(self.task, None, self.msgs, False)
            raise EngineError("no messages")
        mode = "resume" if thread else "fresh"
        ok, status = False, "error"
        try:
            try:
                raw, self.tid = self.codex(prompt, images.files, resume=thread, keep=self.tracked)
            except ClientGone:
                raise
            except UsageLimit as e:
                fb = self.fallback_model(e)
                if not fb:
                    raise
                # retry this same request on the fallback; later requests go there directly for 15 min
                name = "Claude" if self.backend == "claude" else "Codex"
                store.kv_set("limited:" + self.backend, time.time() + 900)
                msg = f"{name} usage limit reached: {roles.LABEL.get(self.role, self.role)} switched to {fb}"
                self.ev("fallback", msg + (f" (resets at {e.resets})" if e.resets else ""), "warn")
                notify.desktop(msg, key="fallback:" + self.backend)
                self.model, self.backend, self.fell_back = fb, roles.backend(fb), True
                images, prompt = self.build(full=True)
                mode = "fallback"
                raw, self.tid = self.codex(prompt, images.files, keep=self.tracked)
            except Exception as e:
                if not thread and not isinstance(e, CodexHung):
                    raise
                self.ev("retry", f"{type(e).__name__}: {str(e)[:120]}; retrying fresh", "warn")
                images, prompt = self.build(full=True)
                mode = "fresh(retry)"
                raw, self.tid = self.codex(prompt, images.files, keep=self.tracked)

            self._raw = raw
            if tools:
                raw = self._raw = self.corrections(raw, prompt, images)
            msg, finish = self.to_message(raw)
            ok, status = True, "ok"
            return msg, finish
        except UsageLimit as e:
            status = "limit"
            when = f" — resets at {e.resets}" if e.resets else ""
            self.ev("usage_limit", str(e)[:200], "error")
            name = "Claude" if self.backend == "claude" else "Codex"
            if e.plan:
                return {"role": "assistant", "content": f"⚠️ {self.model.rsplit('-', 1)[0]} is not included in your "
                        f"{name} plan: {str(e)[:200]} Pick another model in the router dashboard "
                        "(Settings → Models). Nothing was done."}, "stop"
            store.kv_set("usage_limit", {"ts": time.time(), "resets": e.resets, "backend": self.backend})
            return {"role": "assistant", "content": f"⚠️ {name} usage limit reached{when}. "
                    "Nothing was done; try again after the reset."}, "stop"
        except ClientGone:
            status = "gone"
            self.ev("client_gone", "client hung up, codex cancelled", "warn")
            raise
        except Exception as e:
            self.ev("error", f"{type(e).__name__}: {str(e)[:300]}", "error")
            store.request_end(self.rid, status="error", error=str(e)[:500], mode=mode, imgs=len(images.files))
            raise EngineError(str(e)) from e
        finally:
            if self.tracked:  # success stores the thread; failure drops it (next turn starts fresh)
                sessions.done(self.task, self.tid, self.msgs, ok)
            if status != "error":
                store.request_end(self.rid, status=status, mode=mode + ("·fallback" if self.fell_back and mode != "fallback" else ""),
                                  imgs=len(images.files), model=self.model, **self._result_fields())
            self.capture(prompt, getattr(self, "_raw", None))

    def corrections(self, raw, prompt, images):
        tools, node_src = self.tools, self.node_src
        d = P.parse_decision(raw) or {}
        names = [t.get("function", t).get("name", "") for t in tools]
        if d.get("kind") == "final" and P.claims_unavailable(d.get("content", ""), names):
            # models sometimes invent "tool not available" right after a successful call;
            # one correction. A real blocker survives the second pass.
            self.ev("false_unavailable", d["content"][:160])
            r, t = self.extra("false_unavailable", P.RETRY_NUDGE.strip(), prompt + P.RETRY_NUDGE, images.files)
            if r:
                raw, self.tid = r, t or self.tid

        if self.forced and (P.parse_decision(raw) or {}).get("kind") != "tool_call":
            self.ev("forced_tool", f"{self.forced} required but got a text answer; retrying once", "warn")
            nudge = P.FORCE_NUDGE.format(which="any tool" if self.forced == "*" else f"call `{self.forced}`")
            r, t = self.extra("forced_tool", nudge.strip(), prompt + nudge, images.files)
            if r and (P.parse_decision(r) or {}).get("kind") == "tool_call":
                raw, self.tid = r, t or self.tid

        problems = repair.decision_problems(P.parse_decision(raw) or {}, tools, node_src)
        for attempt in range(2):  # a broken call that reaches OS3 fails the step, so try twice
            if not problems:
                break
            note = P.VALIDATE_NUDGE.format(problems="\n".join("- " + p for p in problems[:8]))
            self.ev("invalid_calls", "; ".join(problems)[:300], "warn", {"problems": problems})
            r, t = self.extra("fix", note.strip(), prompt + "\n\nYour reply was: " + raw[:4000] + note, images.files)
            if not r:
                break
            left = repair.decision_problems(P.parse_decision(r) or {}, tools, node_src)
            self.ev("fix_result", f"{len(problems)} -> {len(left)} problem(s)")
            if len(left) < len(problems):
                raw, self.tid, problems = r, t or self.tid, left

        d = P.parse_decision(raw) or {}
        if d.get("kind") == "final" and P.used_browser(self.msgs) and P.claims_not_found(d.get("content", "")):
            # models give up on things people can see (other tab, canvas app, further down): one more look
            self.ev("not_found_check", d.get("content", "")[:160])
            r, t = self.extra("not_found", P.NOT_FOUND_NUDGE.strip(), prompt + "\n\nYour draft final answer was: " +
                              d.get("content", "")[:2000] + P.NOT_FOUND_NUDGE, images.files)
            d2 = (P.parse_decision(r) or {}) if r else {}
            if d2.get("kind") in ("final", "tool_call") and not repair.decision_problems(d2, tools, node_src):
                self.ev("not_found_check", f"-> {d2.get('kind')}: {(d2.get('content') or str(d2.get('calls')))[:120]}")
                raw, self.tid = r, t or self.tid
                d = d2
        if d.get("kind") == "final" and P.used_computer(self.msgs):
            # models declare "done" without checking (saved? right value?); one self-check
            r, t = self.extra("verify", P.VERIFY_NUDGE.strip(), prompt + "\n\nYour draft final answer was: " +
                              d.get("content", "")[:2000] + P.VERIFY_NUDGE, images.files)
            d2 = (P.parse_decision(r) or {}) if r else {}
            if d2.get("kind") in ("final", "tool_call") and not repair.decision_problems(d2, tools, node_src):
                self.ev("verify", f"-> {d2.get('kind')}: {(d2.get('content') or str(d2.get('calls')))[:120]}")
                raw, self.tid = r, t or self.tid
        return raw

    def to_message(self, raw):
        self._result = ("text", [])
        if not self.tools:
            return {"role": "assistant", "content": raw}, "stop"
        d = P.parse_decision(raw) or {}
        calls = d.get("calls") or ([d] if d.get("tool") else [])  # old single-call shape
        calls = [c for c in calls if isinstance(c, dict) and c.get("tool")]
        if d.get("kind") != "tool_call" or not calls:
            self._result = ("final", [])
            return {"role": "assistant", "content": d.get("content", raw)}, "stop"
        by_name = {t.get("function", t).get("name"): t.get("function", t) for t in self.tools}
        out = []
        for c in calls:
            args = c.get("arguments_json") or "{}"
            if not isinstance(args, str):
                args = json.dumps(args)
            try:
                parsed = repair.load_args(args)
                fixed = parsed
                if isinstance(parsed, dict):
                    before = json.dumps(parsed)
                    fixed = repair.fix_computer_use(c["tool"], repair.fix_node_id(dict(parsed), by_name.get(c["tool"]), self.node_src))
                    if json.dumps(fixed) != before:
                        self.ev("call_fixed", f"{c['tool']}: {before[:100]} -> {json.dumps(fixed)[:100]}")
                args = json.dumps(fixed)
            except ValueError as e:
                self.ev("unparseable_args", f"{c['tool']}: {e}", "warn")
            out.append({"id": f"call_{uuid.uuid4().hex[:24]}", "type": "function",
                        "function": {"name": c["tool"], "arguments": args}})
        n = len(out)
        out = repair.add_missing_feed(out, self.tools)
        if len(out) > n:
            self.ev("feed_added", "capture without feed_image, added feed_image")
        self._result = ("tool_call", [tc["function"]["name"] for tc in out])
        return {"role": "assistant", "content": None, "tool_calls": out}, "tool_calls"

    def _result_fields(self):
        kind, names = getattr(self, "_result", ("", []))
        return {"result": kind, "calls": names}

    def capture(self, prompt, raw):
        if not self.cfg["captures"]:
            return
        try:
            d = os.path.join(config.HOME, "captures", self.task or "none")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, f"{time.strftime('%Y%m%d-%H%M%S')}-{self.rid}.json"), "w") as f:
                json.dump({"request": self.body, "prompt": prompt, "raw": raw}, f)
        except OSError:
            pass
