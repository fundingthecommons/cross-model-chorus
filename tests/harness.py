"""The offline test harness for chorus.py.

A local mock server stands in for OpenAI's Responses API, Anthropic's Messages API and the TEE
gateway's OpenAI-compatible API (catalog, chat completions, receipts, public session list). Its
receipts only verify when the request asked for attestation (provider.aci_verified), as the
gateway's do. Its Messages endpoint is strict: it speaks only the API version chorus.py pins
(2026-10-21), and refuses a key sent as a bearer token, the sampling parameters that version
removed, a fallback without its beta, and a JSON schema with an open object. Nothing leaves
the machine, no real key is read, and the environment is cleared per test.

Every name, organisation, address and upstream in these fixtures is fictitious. Upstream
names are neutral placeholders: upstream-a and upstream-b serve GLM and are approved for
confidential runs in the test environment, upstream-z serves GLM and is not approved, and
upstream-c serves Kimi and is approved.

Run the suite from the repo root:

  python3 -m unittest discover -s tests
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "chorus.py")
spec = importlib.util.spec_from_file_location("chorus", SCRIPT)
chorus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chorus)

DRAFT = (
    "Tallowmere Collective has funded 400 projects since 2019.\n"
    "Our approach is not just innovative — it is transformative, scalable, and sustainable.\n"
    "We request $250,000 over 12 months to run three residencies.\n")

OPENAI_FINDINGS = {"findings": [
    {"brief": "tells", "severity": "high",
     "quote": "not just innovative — it is transformative, scalable, and sustainable",
     "issue": "a negated-contrast opener into a parallel triad", "direction": "one concrete claim"},
    {"brief": "refute", "severity": "medium", "quote": "400 projects",
     "issue": "unsourced count", "direction": "cite the ledger"}],
    "pass": ["redteam"], "overall": "Reads as AI in the second sentence."}
KIMI_FINDINGS = {"findings": [
    {"brief": "tells", "severity": "medium", "quote": "transformative, scalable, and sustainable",
     "issue": "triad of abstractions", "direction": "name the outcome"}],
    "pass": [], "overall": "Generic."}
GLM_FINDINGS = {"findings": [
    {"brief": "refute", "severity": "low", "quote": "we have 10,000 members",
     "issue": "membership figure looks inflated", "direction": "check"}],
    "pass": [], "overall": "Mostly fine."}
# The text of the mock's thinking block: the runner must never keep, print or forward it.
THINKING_MARKER = "THINKING-" + "MARKER-never-kept"
# Claude's refute finding sits on OpenAI's passage under the same brief (a corroboration); its
# redteam finding is its own; it passes tells.
CLAUDE_FINDINGS = {"findings": [
    {"brief": "redteam", "severity": "high", "quote": "We request $250,000 over 12 months",
     "issue": "the ask is not tied to what three residencies cost", "direction": "show the costs"},
    {"brief": "refute", "severity": "medium", "quote": "funded 400 projects since 2019",
     "issue": "no source for the count", "direction": "cite the record"}],
    "pass": ["tells"], "overall": "The ask outruns the evidence."}


def open_objects(schema, path="schema"):
    """Where a JSON schema breaks Anthropic's structured-output rules: an object without
    additionalProperties false, or a keyword it does not support."""
    bad = []
    if isinstance(schema, dict):
        if schema.get("type") == "object" and schema.get("additionalProperties") is not False:
            bad.append(path)
        bad += ["%s.%s" % (path, k) for k in ("minLength", "maxLength", "minimum", "maximum",
                                               "multipleOf", "maxItems") if k in schema]
        for k, v in schema.items():
            bad += open_objects(v, "%s.%s" % (path, k))
    elif isinstance(schema, list):
        for i, v in enumerate(schema):
            bad += open_objects(v, "%s[%d]" % (path, i))
    return bad

CATALOG = [
    {"id": "moonshotai/kimi-k3", "is_tee": True, "providers": ["upstream-c"],
     "supported_parameters": ["max_tokens", "reasoning_effort", "response_format"],
     "supported_features": ["structured_outputs", "reasoning"]},
    {"id": "z-ai/glm-5.3", "is_tee": True, "metadata": {},
     "supported_parameters": ["max_tokens", "reasoning_effort", "response_format"],
     "supported_features": ["structured_outputs"]},
]

# The gateway's public session list for GLM 5.3: bare 64-hex ids, built here. The unapproved
# upstream is listed first, so an unpinned call is served by it.
SESSIONS = [{"session_id": c * 64, "upstream_name": u, "expires_at": int(time.time()) + 3600}
            for c, u in (("e", "upstream-z"), ("f", "upstream-b"), ("c", "upstream-a"))]
APPROVED_GLM = [SESSIONS[1]["session_id"], SESSIONS[2]["session_id"]]  # upstream-b, upstream-a
# Kimi K3's sessions: three on upstream-c.
KIMI_SESSIONS = [{"session_id": c * 64, "upstream_name": "upstream-c",
                  "expires_at": int(time.time()) + 3600} for c in "123"]
KIMI_PINS = [x["session_id"] for x in KIMI_SESSIONS]

# The approved-upstream list the test environment sets, as CHORUS_CONFIDENTIAL_UPSTREAMS.
APPROVED = "glm=upstream-a,upstream-b;kimi=upstream-c"


def provider_for(model, confidential):
    """The provider block a TEE call carries: attestation always; on a confidential run, zero
    retention, and GLM pinned to its approved sessions and Kimi to its own."""
    p = {"aci_verified": True}
    if confidential:
        p["zdr"] = True
        p["aci_session_ids"] = APPROVED_GLM if model.startswith("z-ai/") else KIMI_PINS
    return p


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        st = self.server.state
        st["requests"].append(("GET", self.path, None, dict(self.headers)))
        if urlsplit(self.path).path.endswith("/models"):
            if st.get("catalog_status"):
                return self._send(st["catalog_status"], {"error": "down"})
            return self._send(200, dict({"data": st["catalog"]},
                                        **({"has_more": True} if st.get("models_more") else {})))
        if "/aci/sessions?" in self.path:  # the gateway's public session list, per model
            if st.get("sessions_status"):
                return self._send(st["sessions_status"], {"error": "down"})
            model = parse_qs(urlsplit(self.path).query).get("model", [""])[0]
            rows = {"z-ai/glm-5.3": st["sessions"],
                    "moonshotai/kimi-k3": st["kimi_sessions"]}.get(model, [])
            reads = st.setdefault("session_reads", {})
            reads[model] = reads.get(model, 0) + 1
            later = st.get("sessions_after_first", {}).get(model)
            if later is not None and reads[model] > 1:  # the list changes after its first read
                if later == 500:
                    return self._send(500, {"error": "down"})
                rows = later
            body = {"api_version": "aci/1",
                    "sessions": [dict(x) if isinstance(x, dict) else x for x in rows]}
            if st.get("rotate_sessions"):  # the ids go stale before the call is sent
                st["rotations"] = st.get("rotations", 0) + 1
                for x in st["sessions"]:
                    x["session_id"] = "%x" % (st["rotations"] % 16) + x["session_id"][1:]
            return self._send(200, body)
        if "/aci/receipts/" in self.path:
            if st.get("receipt_status"):
                return self._send(st["receipt_status"], {"error": "nope"})
            if st.get("receipt_flaky"):
                st["receipt_flaky"] -= 1
                return self._send(502, {"error": "busy"})
            rid = self.path.rsplit("/", 1)[1]
            if rid in st.get("missing_ids", ()):
                return self._send(404, {"error": "not found"})
            rid = st.get("chat_to_receipt", {}).get(rid, rid)
            if "receipt" in st:
                return self._send(200, st["receipt"])
            session, upstream = st["served"].get(rid, ("sess-" + rid, None))
            event = {"type": "upstream.verified", "result": "verified",
                     "required": st["aci"].get(rid, False), "session_id": session,
                     "model_id": "Upstream-Name-TEE"}
            if st.get("receipt_names"):  # the gateway's docs show the name; live receipts do not
                event["upstream_name"] = upstream
            return self._send(200, {"receipt_id": rid, "chat_id": "req-" + rid,
                                    "model": st["models"].get(rid), "event_log": [
                {"type": "route.selected", "target_route_id": "tee-host:x"}, event,
                {"type": "request.forwarded"}]})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        st = self.server.state
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        st["requests"].append(("POST", self.path, body, dict(self.headers)))
        if self.path.endswith("/messages"):
            return self._messages(body, st)
        if self.path.endswith("/responses") and "FINDINGS TO EXAMINE" in body.get("input", ""):
            if st.get("exam_openai_status"):
                return self._send(st["exam_openai_status"], {"error": {"message": "busy"}})
            return self._send(200, {"id": "resp_2", "status": "completed", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": exam_reply(
                    body["input"], st, body["model"])}]}], "usage": {}})
        if self.path.endswith("/responses"):
            if st.get("openai_status"):
                return self._send(st["openai_status"], {"error": {"message": "bad model"}})
            if st.get("openai_fn"):  # an answer built from the request (e.g. its hats)
                return self._send(200, {"id": "resp_1", "status": "completed", "output": [
                    {"type": "message", "content": [{"type": "output_text",
                                                     "text": st["openai_fn"](body)}]}],
                    "usage": {}})
            return self._send(200, st.get("openai_body") or {
                "id": "resp_1", "status": st.get("openai_state", "completed"),
                "incomplete_details": st.get("openai_incomplete"),
                "output": [{"type": "reasoning", "summary": []},
                           {"type": "message", "content": [
                               {"type": "output_text", "text": json.dumps(OPENAI_FINDINGS)}]}],
                "usage": {"input_tokens": 900, "output_tokens": 700,
                          "output_tokens_details": {"reasoning_tokens": 500}}})
        if self.path.endswith("/chat/completions"):
            model = body["model"]
            rid = "rcpt-" + model.replace("/", "-")
            st["aci"][rid] = (body.get("provider") or {}).get("aci_verified") is True
            st["models"][rid] = model
            delay = st.get("delay", {}).get(model)
            if delay:  # not time.sleep: the tests patch it out of the runner's time module
                threading.Event().wait(delay)
            hdrs = {} if st.get("no_receipt_header") else {"x-receipt-id": rid}
            pins = (body.get("provider") or {}).get("aci_session_ids")
            listed = [x for x in {"z-ai/glm-5.3": st["sessions"],
                                  "moonshotai/kimi-k3": st["kimi_sessions"]}.get(model, [])
                      if isinstance(x, dict) and "session_id" in x]
            live = {x["session_id"]: x.get("upstream_name")
                    for x in st["sessions"] + st["kimi_sessions"]
                    if isinstance(x, dict) and "session_id" in x}
            st["served"].pop(rid, None)
            if not pins and listed:  # unpinned: the gateway picks; here, the first listed session
                st["served"][rid] = (listed[0]["session_id"], listed[0].get("upstream_name"))
            if pins:  # like the gateway: a pin that matches no live session is a 503, nothing sent
                hit = [p for p in pins if p in live]
                if not hit:
                    return self._send(503, {"error": {
                        "message": "upstream verification failed: none of the requested "
                                   "attested sessions is available for model " + model,
                        "type": "service_unavailable"}})
                st["served"][rid] = st.get("serve_outside_pin") or (hit[0], live[hit[0]])
            status = st.get("chat_status", {}).get(model)
            if status:
                return self._send(status, {"error": {"message": "upstream trouble"}},
                                  hdrs if st.get("error_receipt") else {})
            if st.get("raw_body", {}).get(model):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                for k, v in hdrs.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(st["raw_body"][model].encode())
                return
            assert body.get("stream") is True, "chat voices must stream"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            for k, v in hdrs.items():
                self.send_header(k, v)
            self.end_headers()
            user_text = body["messages"][1]["content"]
            if "FINDINGS TO EXAMINE" in user_text:
                content = exam_reply(user_text, st, model)
            else:
                content = st["chat"](body)
            cid = None if st.get("omit_id") else "req-" + rid
            st.setdefault("chat_to_receipt", {})[cid] = rid
            script = st.get("script", {}).get(model)
            if script:
                return self._script(script, content, cid)
            stall = st.get("stall", {}).get(model)
            pieces = [content[i:i + 40] for i in range(0, len(content), 40)] or [""]
            for i, piece in enumerate(pieces):
                if stall and i == 1:
                    threading.Event().wait(stall)
                chunk = {"choices": [{"index": 0, "delta": {"content": piece}}]}
                if cid:
                    chunk["id"] = cid
                self.wfile.write(("data: %s\n\n" % json.dumps(chunk)).encode())
                self.wfile.flush()
            self.wfile.write(("data: %s\n\n" % json.dumps(
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})).encode())
            self.wfile.write(("data: %s\n\n" % json.dumps(
                {"choices": [], "usage": {"prompt_tokens": 950, "completion_tokens": 300,
                                          "cost": st.get("cost", 0.0081)}})).encode())
            self.wfile.write(b"data: [DONE]\n\n")
        return self._send(404, {"error": "not found"})

    def _messages(self, body, st):
        """Anthropic's Messages API: strict about the request, then a structured answer after a
        thinking block whose text (THINKING_MARKER) the runner must never keep; streamed as
        server-sent events when the request asks. State: claude_status, claude_fn (an answer
        built from the request; a request with hats gets one per hat), claude_stop,
        claude_stop_details, claude_usage, claude_fallback (a fallback model serves it),
        claude_body (the whole message), and for the stream claude_delay (before the headers),
        claude_stall (after the first text), claude_cut (no message_stop) and
        claude_stream_error (an error event)."""
        def invalid(msg, code=400):
            return self._send(code, {"type": "error", "error": {
                "type": "invalid_request_error", "message": msg}})

        h = {k.lower(): v for k, v in self.headers.items()}
        if not h.get("x-api-key") or "authorization" in h:
            return self._send(401, {"type": "error", "error": {
                "type": "authentication_error", "message": "x-api-key header is required"}})
        if h.get("anthropic-version") != "2026-10-21":  # the version chorus.py pins
            return invalid("anthropic-version: this mock speaks 2026-10-21 only")
        sampling = [k for k in ("temperature", "top_p", "top_k") if k in body]
        if sampling:
            return invalid("%s: not supported on this API version" % sampling[0])
        if "fallbacks" in body and h.get("anthropic-beta") != "server-side-fallback-2026-07-01":
            return invalid("fallbacks: requires the server-side-fallback beta")
        fmt = (body.get("output_config") or {}).get("format") or {}
        if fmt.get("type") != "json_schema" or open_objects(fmt.get("schema")):
            return invalid("output_config.format: %s" % (open_objects(fmt.get("schema"))
                                                         or "not a json_schema"))
        if st.get("claude_delay"):  # not time.sleep: the tests patch it out of the runner
            threading.Event().wait(st["claude_delay"])
        if st.get("claude_status"):
            return self._send(st["claude_status"], {"type": "error", "error": {
                "type": "overloaded_error", "message": "Overloaded"}})
        user = body["messages"][0]["content"]
        item = fmt["schema"]["properties"].get("findings", {}).get("items", {})
        if "FINDINGS TO EXAMINE" in user:
            text = exam_reply(user, st, body["model"])
        elif st.get("claude_fn"):
            text = st["claude_fn"](body)
        elif "hat" in item.get("properties", {}):
            text = hat_reply(fmt["schema"], body["model"])
        else:
            text = json.dumps(CLAUDE_FINDINGS)
        served = st.get("claude_fallback")
        usage = {"input_tokens": 800, "output_tokens": 600, "cache_read_input_tokens": 0,
                 "cache_creation_input_tokens": 0,
                 "output_tokens_details": {"thinking_tokens": 400}}
        usage.update(st.get("claude_usage") or {})
        content = [{"type": "thinking", "thinking": THINKING_MARKER, "signature": "c2ln"},
                   {"type": "text", "text": text}]
        if served:  # the requested model declined; Anthropic's recommended model answered
            content.insert(0, {"type": "fallback", "from": {"model": body["model"]},
                               "to": {"model": served}})
            usage["iterations"] = [{"type": "message", "input_tokens": 800, "output_tokens": 0},
                                   {"type": "fallback_message", "input_tokens": 800,
                                    "output_tokens": 600}]
        msg = st.get("claude_body") or {
            "id": "msg_01", "type": "message", "role": "assistant",
            "model": served or body["model"], "content": content,
            "stop_reason": st.get("claude_stop", "end_turn"),
            "stop_details": st.get("claude_stop_details"), "usage": usage}
        if body.get("stream") is not True:
            return self._send(200, msg)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def event(obj):
            self.wfile.write(("event: %s\ndata: %s\n\n" % (obj["type"], json.dumps(obj))).encode())
            self.wfile.flush()

        rest = dict(msg.get("usage") or {})
        late = {k: rest.pop(k) for k in ("output_tokens", "output_tokens_details", "iterations")
                if k in rest}
        start = {k: x for k, x in msg.items() if k not in ("content", "usage")}
        start.update(content=[], stop_reason=None, stop_details=None,
                     usage=dict(rest, output_tokens=1))
        try:
            event({"type": "message_start", "message": start})
            event({"type": "ping"})
            if st.get("claude_stream_error"):
                return event({"type": "error", "error": {"type": "overloaded_error",
                                                         "message": "Overloaded"}})
            for i, b in enumerate(msg.get("content") or []):
                if b.get("type") == "text":
                    event({"type": "content_block_start", "index": i,
                           "content_block": {"type": "text", "text": ""}})
                    for n, k in enumerate(range(0, len(b["text"]), 40)):
                        event({"type": "content_block_delta", "index": i, "delta": {
                            "type": "text_delta", "text": b["text"][k:k + 40]}})
                        if n == 0 and st.get("claude_stall"):
                            threading.Event().wait(st["claude_stall"])
                elif b.get("type") == "thinking":
                    event({"type": "content_block_start", "index": i,
                           "content_block": {"type": "thinking", "thinking": ""}})
                    event({"type": "content_block_delta", "index": i, "delta": {
                        "type": "thinking_delta", "thinking": b.get("thinking", "")}})
                    event({"type": "content_block_delta", "index": i, "delta": {
                        "type": "signature_delta", "signature": b.get("signature", "")}})
                else:
                    event({"type": "content_block_start", "index": i, "content_block": b})
                event({"type": "content_block_stop", "index": i})
            if st.get("claude_cut"):
                return
            event({"type": "message_delta", "delta": {"stop_reason": msg.get("stop_reason"),
                                                      "stop_details": msg.get("stop_details")},
                   "usage": late})
            event({"type": "message_stop"})
        except OSError:
            pass  # the client gave up first: expected when its clock trips

    def _script(self, script, content, cid):
        """Stream a scripted SSE body: ("role",), ("reason", key, text), ("hb",), ("wait", s),
        ("content",) for the answer, ("error",), ("done",) for the finish, usage and [DONE]."""
        def data(obj):
            if cid:
                obj["id"] = cid
            self.wfile.write(("data: %s\n\n" % json.dumps(obj)).encode())

        try:
            for ev in script:
                if ev[0] == "wait":
                    threading.Event().wait(ev[1])
                elif ev[0] == "hb":
                    self.wfile.write(b": PROCESSING\n\n")
                elif ev[0] == "role":
                    data({"choices": [{"index": 0, "delta": {"role": "assistant"}}]})
                elif ev[0] == "reason":
                    data({"choices": [{"index": 0, "delta": {ev[1]: ev[2]}}]})
                elif ev[0] == "delta":  # a raw delta, e.g. one text mirrored under two keys
                    data({"choices": [{"index": 0, "delta": ev[1]}]})
                elif ev[0] == "text":  # a content piece of the test's choosing
                    data({"choices": [{"index": 0, "delta": {"content": ev[1]}}]})
                elif ev[0] == "content":
                    for i in range(0, len(content), 40):
                        data({"choices": [{"index": 0, "delta": {"content": content[i:i + 40]}}]})
                elif ev[0] == "error":
                    data({"error": {"message": "upstream went away"}})
                elif ev[0] == "rawline":
                    self.wfile.write(("%s\n\n" % ev[1]).encode())
                elif ev[0] == "finish":
                    data({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                elif ev[0] == "done_marker":
                    self.wfile.write(b"data: [DONE]\n\n")
                elif ev[0] == "done":
                    data({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                    data({"choices": [], "usage": {
                        "prompt_tokens": 950, "completion_tokens": 300, "cost": 0.0081,
                        "completion_tokens_details": {"reasoning_tokens": 120}}})
                    self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
        except OSError:
            pass  # the client gave up first: expected when a clock trips


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass  # a client that timed out closes the socket; that is expected


def exam_reply(text, state, model):
    """Round-2 verdicts for every finding id in the prompt (default: holds)."""
    ids = re.findall(r"(?m)^(F\d+)\. ", text)
    decide = state.get("exam", lambda model, fid: "holds")
    return json.dumps({"verdicts": [{"id": i, "verdict": decide(model, i),
                                     "reason": "checked against the draft"} for i in ids]})


def default_chat(body):
    if "kimi" in body["model"]:
        return "<think>weighing the triad</think>\n```json\n%s\n```" % json.dumps(KIMI_FINDINGS)
    return json.dumps(GLM_FINDINGS)


# A synthetic agreement: [CLIENT] is the reviewing side once redacted, Acme Corp the other.
CONTRACT = (
    "SPONSORSHIP AGREEMENT between Tallowmere Collective and Acme Corp (\"Sponsor\").\n"
    "1. Sponsor pays USD 50,000 within 90 days of the event; Acme Corp may cancel at any time.\n"
    "2. Tallowmere Collective assigns all IP in event recordings to the Sponsor.\n"
    "3. This agreement is governed by the laws of the Cayman Islands.\n")
SOLO_QUOTES = {"moonshotai/kimi-k3": "governed by the laws of the Cayman Islands",
               "z-ai/glm-5.3": "assigns all IP in event recordings",
               "gpt-6.1-sol": "within 90 days of the event"}


def hat_reply(schema, model, shared="may cancel at any time", solos=SOLO_QUOTES):
    """A round-1 answer that wears the voice's own hats: it flags the shared passage once per
    hat, plus one passage only its family raises."""
    item = schema["properties"]["findings"]["items"]["properties"]
    hats = item["hat"]["enum"] if "hat" in item else [None]
    brief = item["brief"]["enum"][0]
    fs = [{"brief": brief, "hat": h, "severity": "high", "quote": shared,
           "issue": "one-sided termination (%s)" % h, "direction": "mutual notice"} for h in hats]
    fs.append({"brief": brief, "hat": hats[-1], "severity": "medium",
               "quote": solos.get(model, ""), "issue": "only this family raised it",
               "direction": "check"})
    for f in fs:
        if "hat" not in item:
            f.pop("hat")
        if "rewrite" in item:
            f["rewrite"] = "Either party may end it on 30 days' notice."
    return json.dumps({"findings": fs, "pass": [], "overall": "Risky for the signer."})


def hat_chat(body):
    return hat_reply(body["response_format"]["json_schema"]["schema"], body["model"])


def hat_openai(body):
    return hat_reply(body["text"]["format"]["schema"], body["model"])


# A synthetic grant application; every organisation in it is fictitious. Its lines that do not
# add up, reach presented as an outcome and a match only pledged are for realism: the mock answers
# from the request schema, so offline tests pin routing and briefs, never what a model would catch.
GRANT = (
    "Tallowmere Collective asks the Fernquill Fund for USD 120,000 over 12 months.\n"
    "Activities: four regional workshops and an open toolkit.\n"
    "Outcome: 2,000 people reached across four workshops.\n"
    "Budget: staff USD 80,000; workshops USD 30,000; overhead USD 25,000 (total USD 120,000).\n"
    "Match: a 1:1 match is pledged by a local partner.\n")
GRANT_SHARED = "2,000 people reached across four workshops"
GRANT_SOLOS = {"moonshotai/kimi-k3": "overhead USD 25,000",
               "z-ai/glm-5.3": "a 1:1 match is pledged by a local partner",
               "gpt-6.1-sol": "four regional workshops and an open toolkit"}
GRANT_HATS = ["program-officer", "impact-evaluator", "budget-reviewer"]


def grant_chat(body):
    return hat_reply(body["response_format"]["json_schema"]["schema"], body["model"],
                     GRANT_SHARED, GRANT_SOLOS)


def grant_openai(body):
    return hat_reply(body["text"]["format"]["schema"], body["model"], GRANT_SHARED, GRANT_SOLOS)


def grant_claude(body):
    return hat_reply(body["output_config"]["format"]["schema"], body["model"], GRANT_SHARED,
                     GRANT_SOLOS)


# The env file every test loads (via CHORUS_ENV_FILE): one credential-shaped value, which blocks
# a draft that contains it, and two identifiers, which only flag one.
ENV_FILE = ("SOME_API_TOKEN=" + "tok_live_" + "ABCDEFGHIJKLMNOP\n"
            "TREASURY_ADDRESS=0x1111111111111111111111111111111111111111\n"
            "ORG_ID=org_ABCDEFGH12345\n")


class ChorusCase(unittest.TestCase):
    """The base case: a mock server per test class, a scratch HOME, a cleared environment with
    fake keys, the env file above, and the approved-upstream list APPROVED."""

    @classmethod
    def setUpClass(cls):
        cls.server = QuietServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.root = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home)
        self.server.state = {"requests": [], "chat": default_chat, "aci": {}, "models": {},
                             "catalog": json.loads(json.dumps(CATALOG)), "served": {},
                             "sessions": [dict(s) for s in SESSIONS],
                             "kimi_sessions": [dict(s) for s in KIMI_SESSIONS]}
        voices = {n: dict(v) for n, v in chorus.VOICES.items()}
        for n in voices:  # every voice, present and future, points at the mock
            voices[n]["base"] = self.root + ("/phala/v1" if voices[n]["tee"] else "/%s/v1" % n)
        self.env_file = os.path.join(self.tmp.name, "chorus.env")
        with open(self.env_file, "w") as f:
            f.write(ENV_FILE)
        self.patches = [
            mock.patch.object(chorus, "VOICES", voices),
            mock.patch.object(chorus.time, "sleep", lambda s: None),
            mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-openai-key",
                                         "PHALA_API_KEY": "test-phala-key",
                                         "CHORUS_ENV_FILE": self.env_file,
                                         "CHORUS_CONFIDENTIAL_UPSTREAMS": APPROVED,
                                         "HOME": self.home, "no_proxy": "*",
                                         "PATH": os.environ.get("PATH", "")}, clear=True),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    # helpers
    def draft(self, text=DRAFT, name="draft.md", sub=""):
        d = os.path.join(self.tmp.name, sub) if sub else self.tmp.name
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = chorus.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def reqs(self, method=None, suffix=""):
        return [r for r in self.server.state["requests"]
                if (method is None or r[0] == method) and suffix in r[1]]

    def posts(self, suffix):
        return self.reqs("POST", suffix)

    def round1(self, suffix):
        return [r for r in self.posts(suffix) if "FINDINGS TO EXAMINE" not in json.dumps(r[2])]

    def use_cases(self):
        """The shipped use cases, loaded the way main() loads them."""
        return chorus.load_use_cases()
