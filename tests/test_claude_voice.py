"""The Claude voice (Anthropic's Messages API) and the --drafted-by marker."""
import contextlib
import io
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from harness import (CLAUDE_FINDINGS, DRAFT, GRANT, THINKING_MARKER, ChorusCase, chorus,
                     grant_chat, grant_claude, grant_openai)

KEY = "test-anthropic-key"


def lower(hdrs):
    return {k.lower(): v for k, v in hdrs.items()}


class ClaudeCase(ChorusCase):
    def setUp(self):
        super().setUp()
        os.environ["ANTHROPIC_API_KEY"] = KEY

    def run_json(self, *argv, cls="internal"):
        code, out, err = self.run_cli("--sensitivity", cls, "--file", self.draft(), "--yes",
                                      "--json", *argv)
        return code, (json.loads(out) if out.strip() else None), err

    def claude_only(self, *argv, cls="internal"):
        """One Claude call, no round 2: (code, the claude result, stderr, the request body,
        its headers in lower case)."""
        code, res, err = self.run_json("--voices", "claude", "--no-cross-examine", *argv, cls=cls)
        sent = self.round1("/claude/v1/messages")
        self.assertEqual(len(sent), 1, err)
        return code, res["results"]["claude"], err, sent[0][2], lower(sent[0][3])


class RoutingTests(ClaudeCase):
    def test_runs_by_default_at_public_and_internal(self):
        for cls in ("public", "internal"):
            self.server.state["requests"] = []
            code, res, err = self.run_json(cls=cls)
            self.assertEqual(code, 0, err)
            self.assertEqual(sorted(res["results"]), ["claude", "glm", "kimi", "openai"], cls)
            self.assertEqual(len(res["results"]["claude"]["findings"]), 2, cls)
            self.assertEqual(len(self.round1("/claude/v1/messages")), 1, cls)

    def test_skipped_cleanly_without_its_key(self):
        os.environ.pop("ANTHROPIC_API_KEY")
        code, res, err = self.run_json()
        self.assertEqual(code, 0, err)
        self.assertIn("skip claude: ANTHROPIC_API_KEY not set", err)
        self.assertNotIn("claude", res["results"])
        self.assertEqual(self.posts("/messages"), [])

    def test_never_sent_at_confidential(self):
        code, res, err = self.run_json(cls="confidential")
        self.assertEqual(code, 0, err)  # not a candidate: the TEE voices run, Claude is not asked
        self.assertEqual(sorted(res["results"]), ["glm", "kimi"])
        self.assertEqual(self.reqs(suffix="/claude/"), [])
        for voices in ("claude", "claude,kimi"):
            self.server.state["requests"] = []
            code, _, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                        "--voices", voices, "--yes")
            self.assertEqual(code, 2, voices)
            self.assertIn("claude: sensitivity 'confidential' allows attested-TEE voices only", err)
            self.assertEqual(self.server.state["requests"], [], voices)  # nothing to anyone

    def test_its_key_goes_only_to_anthropic_and_never_as_a_bearer_token(self):
        code, _, err = self.run_json()
        self.assertEqual(code, 0, err)
        for _, path, _, hdrs in self.server.state["requests"]:
            h = lower(hdrs)
            if path.startswith("/claude/"):
                self.assertEqual(h.get("x-api-key"), KEY, path)
                self.assertNotIn("authorization", h, path)
            else:
                self.assertNotIn("x-api-key", h, path)
                self.assertNotIn(KEY, json.dumps(hdrs), path)

    def test_a_redirect_never_carries_the_key(self):
        """302 and 303 on a POST, and any redirect on a GET, are ones Python's own opener
        follows: only the runner's refusal keeps the x-api-key from going along."""
        seen, codes = [], []

        class Sink(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                seen.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()

            do_POST = do_GET

        class Source(Sink):
            def do_GET(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(codes[-1])
                self.send_header("Location", target)
                self.end_headers()

            do_POST = do_GET

        sink, src = (ThreadingHTTPServer(("127.0.0.1", 0), h) for h in (Sink, Source))
        target = "http://localhost:%d/v1/messages" % sink.server_address[1]
        url = "http://127.0.0.1:%d/v1/messages" % src.server_address[1]
        for s in (sink, src):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        try:
            for code in (302, 303, 307):
                codes.append(code)
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    chorus.messages_stream(url, chorus.anthropic_headers("SECRET-KEY"),
                                           {"x": 1}, 10)
                cm.exception.close()
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    chorus.http("GET", url, None, auth=chorus.anthropic_headers("SECRET-KEY"))
                cm.exception.close()
            self.assertEqual(seen, [])
        finally:
            for s in (sink, src):
                s.shutdown()
                s.server_close()

    def test_only_claude_runs_one_round(self):
        code, res, err = self.run_json("--voices", "claude")
        self.assertEqual(code, 0, err)
        self.assertEqual(list(res["results"]), ["claude"])
        self.assertEqual(len(self.posts("/messages")), 1)  # one family: nobody to cross-examine

    def test_list_models_uses_anthropic_headers(self):
        code, out, _ = self.run_cli("--list-models", "--voices", "claude")
        self.assertEqual(code, 0)
        gets = self.reqs("GET", "/claude/v1/models")
        self.assertEqual(len(gets), 1)
        h = lower(gets[0][3])
        self.assertEqual((h.get("x-api-key"), h.get("anthropic-version")),
                         (KEY, chorus.ANTHROPIC_VERSION))
        self.assertNotIn("authorization", h)
        os.environ.pop("ANTHROPIC_API_KEY")
        self.server.state["requests"] = []
        code, out, _ = self.run_cli("--list-models", "--voices", "claude")
        self.assertIn("ANTHROPIC_API_KEY not set; trying unauthenticated", out)
        h = lower(self.reqs("GET", "/claude/v1/models")[0][3])
        self.assertFalse({"x-api-key", "authorization"} & set(h))  # no empty key header either
        self.assertTrue(self.reqs("GET", "/claude/v1/models")[0][1].endswith("?limit=1000"))
        self.assertNotIn("more models than this page shows", out)
        self.server.state["models_more"] = True
        code, out, _ = self.run_cli("--list-models", "--voices", "claude")
        self.assertIn("(the host lists more models than this page shows)", out)


class RequestTests(ClaudeCase):
    def test_request_shape(self):
        code, r, err, body, h = self.claude_only()
        self.assertEqual(code, 0, err)
        self.assertEqual((h.get("x-api-key"), h.get("anthropic-version"), h.get("anthropic-beta")),
                         (KEY, chorus.ANTHROPIC_VERSION, chorus.CLAUDE_FALLBACK_BETA))
        self.assertNotIn("authorization", h)
        briefs = chorus.DEFAULT_BRIEFS["internal"]
        system, user = chorus.build_prompt(briefs, DRAFT, None)
        self.assertEqual(body, {
            "model": "claude-opus-5-5", "max_tokens": 32000, "stream": True, "system": system,
            "messages": [{"role": "user", "content": user}],
            "thinking": {"type": "adaptive"}, "fallbacks": "default",
            "output_config": {"format": {"type": "json_schema",
                                         "schema": chorus.findings_schema(briefs)},
                              "effort": "high"}})
        self.assertEqual(r["model"], "claude-opus-5-5")
        self.assertEqual(r["host"], "127.0.0.1:%d" % self.server.server_address[1])
        self.assertFalse(r["tee"])

    def test_effort_maps_to_output_config(self):
        e = chorus.claude_effort
        for level, want in (("none", "low"), ("low", "low"), ("medium", "medium"),
                            ("high", "high"), ("xhigh", "xhigh"), ("max", "max")):
            for model in chorus.CLAUDE_EFFORT:
                self.assertEqual(e(model, level), want, (model, level))
            self.assertIsNone(e("claude-opus-9-9", level))  # exact ids only
        self.assertIsNone(e("claude-opus-5-5", "unset"))
        _, r, _, body, _ = self.claude_only("--effort", "claude=none")
        self.assertEqual(body["output_config"]["effort"], "low")
        self.assertEqual(body["thinking"], {"type": "adaptive"})  # never turned off
        self.assertEqual(r["effort"], "low")
        self.server.state["requests"] = []
        _, r, _, body, _ = self.claude_only("--effort", "claude=unset")
        self.assertNotIn("effort", body["output_config"])
        self.assertEqual(body["thinking"], {"type": "adaptive"})
        self.assertIsNone(r["effort"])
        code, _, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                    "--dry-run", "--effort", "claude=xhigh")
        self.assertEqual(code, 0)
        self.assertRegex(err, r"claude +claude-opus-5-5 @ \S+ · effort xhigh · wall 540s\n")

    def test_an_unlisted_id_gets_no_effort_thinking_or_fallback(self):
        _, r, _, body, h = self.claude_only("--model", "claude=claude-opus-9-9")
        self.assertEqual(body["model"], "claude-opus-9-9")
        for k in ("thinking", "fallbacks"):
            self.assertNotIn(k, body)
        self.assertNotIn("effort", body["output_config"])
        self.assertNotIn("anthropic-beta", h)
        self.assertIsNone(r["usage"]["cost"])  # no price either
        code, _, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                    "--dry-run", "--model", "claude=claude-opus-9-9")
        self.assertRegex(err, r"claude +claude-opus-9-9 @ \S+ · effort none sent")

    def test_aliases_the_family_lock_and_the_fallback_models(self):
        for alias, model, fallback in (("opus", "claude-opus-5-5", True),
                                       ("sonnet", "claude-sonnet-5-5", True),
                                       ("fable", "claude-fable-5-1", True),
                                       ("haiku", "claude-haiku-5-5", False)):
            self.server.state["requests"] = []
            _, _, _, body, h = self.claude_only("--model", "claude=" + alias)
            self.assertEqual(body["model"], model)
            self.assertEqual(body.get("fallbacks"), "default" if fallback else None, alias)
            self.assertEqual(h.get("anthropic-beta"),
                             chorus.CLAUDE_FALLBACK_BETA if fallback else None, alias)
        code, _, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                    "--voices", "claude", "--model", "claude=gpt-6-astra", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("runs only claude- models", err)

    def test_the_mock_is_strict(self):
        """The strict mock gives the request tests their teeth: it speaks only the version the
        runner pins, and refuses a bearer key, sampling parameters, a fallback without its beta
        and an open schema."""
        url = self.root + "/claude/v1/messages"
        good = {"model": "claude-opus-5-5", "max_tokens": 10,
                "messages": [{"role": "user", "content": "x"}],
                "output_config": {"format": {"type": "json_schema",
                                             "schema": chorus.exam_schema()}}}

        def status(body, headers):
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                         headers=dict(headers, **{"Content-Type":
                                                                  "application/json"}))
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                e.close()
                return e.code

        ok = chorus.anthropic_headers(KEY)
        self.assertEqual(status(good, ok), 200)
        self.assertEqual(status(good, {"Authorization": "Bearer " + KEY}), 401)
        self.assertEqual(status(good, dict(ok, **{"anthropic-version": "2023-06-01"})), 400)
        self.assertEqual(status(dict(good, temperature=1), ok), 400)
        self.assertEqual(status(dict(good, fallbacks="default"), ok), 400)
        loose = json.loads(json.dumps(good))
        del loose["output_config"]["format"]["schema"]["properties"]["verdicts"]["items"][
            "additionalProperties"]
        self.assertEqual(status(loose, ok), 400)


class ResponseTests(ClaudeCase):
    def test_structured_findings_parse_and_thinking_is_never_kept(self):
        code, r, err, _, _ = self.claude_only()
        self.assertEqual(code, 0, err)
        self.assertEqual([f["quote"] for f in r["findings"]],
                         [f["quote"] for f in CLAUDE_FINDINGS["findings"]])
        self.assertTrue(all(len(f["spans"]) == 1 for f in r["findings"]))  # quotes located
        self.assertEqual((r["pass"], r["overall"]), (["tells"], "The ask outruns the evidence."))
        self.assertEqual((r["usage"]["input"], r["usage"]["output"], r["usage"]["reasoning"]),
                         (800, 600, 400))
        self.assertEqual(r["incomplete"], "")
        self.assertNotIn("c2ln", json.dumps(r))  # the thinking block's signature
        self.assertEqual(r["stream"]["chars"]["reasoning"], len(THINKING_MARKER))  # counted
        self.assertEqual(r["stream"]["lines"]["heartbeat"], 1)  # the ping
        self.assertIsNotNone(r["stream"]["first_token"])

    def test_thinking_text_never_reaches_the_report_or_another_voice(self):
        code, out, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                      "--yes", "--json")
        self.assertEqual(code, 0, err)
        self.assertNotIn(THINKING_MARKER, out + err)
        for _, path, body, _ in self.server.state["requests"]:
            self.assertNotIn(THINKING_MARKER, json.dumps(body), path)
        self.server.state["claude_fn"] = lambda body: "not json at all"  # an unstructured answer
        code, out, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                      "--voices", "claude", "--yes", "--json")
        self.assertEqual(json.loads(out)["results"]["claude"]["raw"], "not json at all")
        self.assertNotIn(THINKING_MARKER, out + err)

    def test_input_counts_cache_reads_and_writes(self):
        self.server.state["claude_usage"] = {"cache_read_input_tokens": 100,
                                             "cache_creation_input_tokens": 50}
        _, r, _, _, _ = self.claude_only()
        self.assertEqual(r["usage"]["input"], 950)  # Anthropic's input_tokens exclude both
        self.assertIsNone(r["usage"]["cost"])  # a cache write's price depends on its duration

    def test_refusal_is_a_failed_voice(self):
        self.server.state.update(claude_stop="refusal",
                                 claude_stop_details={"type": "refusal", "category": "cyber"})
        code, res, err = self.run_json()
        self.assertEqual(code, 6, err)  # a partial chorus: the others printed
        claude = res["results"]["claude"]
        self.assertTrue(claude["sent"])
        self.assertIn("stop_reason refusal, category cyber", claude["error"])
        self.assertNotIn("findings", claude)
        self.assertIn("FAILED: refused by the model's safeguards", res["report"])
        self.assertIsNone(claude["usage"]["cost"])  # billed or not by category: unpriced
        self.assertIn("claude (round 1)", res["cost_time"]["unpriced"])
        self.assertNotIn("answered_by", claude)
        self.server.state["claude_stop_details"] = None
        code, res, _ = self.run_json()
        self.assertIn("category none given", res["results"]["claude"]["error"])
        self.server.state["claude_fallback"] = "claude-opus-4-8"  # the fallback declined too
        code, res, _ = self.run_json()
        claude = res["results"]["claude"]
        self.assertIn("; the fallback (claude-opus-4-8) declined too", claude["error"])
        self.assertNotIn("answered_by", claude)
        self.assertNotIn("answered by", res["report"])

    def test_max_tokens_is_reported_incomplete(self):
        self.server.state.update(claude_stop="max_tokens", claude_fn=lambda b: '{"findings": [')
        code, r, _, _, _ = self.claude_only()
        self.assertEqual(code, 4)  # the only voice came back unstructured
        self.assertEqual(r["incomplete"], "max_tokens")
        self.assertEqual(r["raw"], '{"findings": [')  # shown to the operator, never relayed
        for stop, incomplete in ((None, "no stop reason"), ("stop_sequence", ""),
                                 ("end_turn", ""), ("model_context_window_exceeded",
                                                    "model_context_window_exceeded")):
            self.server.state.update(claude_stop=stop, claude_fn=None)
            self.server.state["requests"] = []
            _, r, _, _, _ = self.claude_only()
            self.assertEqual(r["incomplete"], incomplete, stop)

    def test_http_errors_are_named(self):
        for status, words in ((529, "HTTP 529: Overloaded (provider error; re-run just this "
                                    "voice with --voices)"),
                              (401, "HTTP 401: Overloaded (the key was rejected")):
            self.server.state["claude_status"] = status
            code, res, err = self.run_json()
            self.assertEqual(code, 6, err)
            self.assertIn(words, res["results"]["claude"]["error"])
            self.assertEqual(res["results"]["claude"]["sent"], status >= 500)


class RoundTwoTests(ClaudeCase):
    def test_claude_examines_and_is_examined(self):
        self.server.state["exam"] = lambda model, fid: (
            "refuted" if model.startswith("claude-") else "holds")
        code, res, err = self.run_json()
        self.assertEqual(code, 0, err)
        exam_posts = [r for r in self.posts("/claude/v1/messages")
                      if "FINDINGS TO EXAMINE" in r[2]["messages"][0]["content"]]
        self.assertEqual(len(exam_posts), 1)
        self.assertEqual(exam_posts[0][2]["output_config"]["format"]["schema"],
                         chorus.exam_schema())
        examined = exam_posts[0][2]["messages"][0]["content"].split("FINDINGS TO EXAMINE")[1]
        self.assertIn("we have 10,000 members", examined)
        self.assertNotIn("We request $250,000", examined)  # its own findings are not put to it
        findings = {(n, f["quote"]): f for n, r in res["results"].items()
                    for f in r.get("findings") or []}
        own = findings["claude", "We request $250,000 over 12 months"]
        self.assertEqual(sorted(own["exam"]), ["glm", "kimi", "openai"])  # examined by the rest
        self.assertEqual(own["exam_status"], "held")
        glm = findings["glm", "we have 10,000 members"]
        self.assertEqual(sorted(glm["exam"]), ["claude", "kimi", "openai"])
        self.assertEqual(glm["exam"]["claude"]["verdict"], "refuted")  # Claude examined it
        self.assertEqual(glm["exam_status"], "disputed")  # 1 refuted of 3 is no majority
        corroborated = findings["openai", "400 projects"]
        self.assertNotIn("exam_id", corroborated)  # Claude flagged the passage, same brief
        self.assertIn("**claude + openai** — \"funded 400 projects since 2019\"", res["report"])


class CostTests(ClaudeCase):
    def test_price_table(self):
        c = chorus.claude_cost
        u = {"input_tokens": 800, "output_tokens": 600}
        self.assertEqual(c("claude-opus-5-5", u), 0.0152)  # $4 in, $20 out
        self.assertEqual(c("claude-sonnet-5-5", u), 0.0076)
        self.assertEqual(c("claude-fable-5-1", u), 0.038)
        self.assertEqual(c("claude-haiku-5-5", u), 0.00038)
        self.assertEqual(c("claude-opus-5-5", dict(u, cache_read_input_tokens=1000)), 0.0154)
        self.assertEqual(c("claude-sonnet-5-5", dict(u, cache_read_input_tokens=1000)), 0.0077)
        self.assertIsNone(c("claude-opus-5-5", dict(u, cache_creation_input_tokens=1)))
        self.assertIsNone(c("claude-opus-9-9", u))
        self.assertIsNone(c("claude-opus-5-5", {"input_tokens": 800}))
        self.assertIsNone(c("claude-opus-5-5", {"input_tokens": "800", "output_tokens": 600}))
        self.assertIsNone(c("claude-opus-5-5", {"input_tokens": -1, "output_tokens": 600}))
        haiku = {"input_tokens": 99000, "output_tokens": 0}
        self.assertEqual(c("claude-haiku-5-5", dict(haiku, cache_read_input_tokens=1000)), 0.00991)
        self.assertIsNone(c("claude-haiku-5-5", dict(haiku, cache_read_input_tokens=1001)))
        self.assertEqual(c("claude-opus-5-5", {"input_tokens": 1000000, "output_tokens": 0}), 4.0)

    def test_cost_table_and_note(self):
        code, res, err = self.run_json()
        self.assertEqual(code, 0, err)
        ct = res["cost_time"]
        calls = {(c["round"], c["voice"]): c for c in ct["calls"]}
        for rnd in ("1", "2"):
            self.assertEqual(calls[rnd, "claude"]["cost"], 0.0152)
            self.assertEqual(calls[rnd, "claude"]["cost_from"], "price table")
            self.assertEqual(calls[rnd, "claude"]["reasoning"], 400)
        self.assertNotIn("claude (round 1)", ct["unpriced"])
        self.assertIn("OpenAI's cost is priced from its tokens (OPENAI_PRICES, read 2026-10-22); "
                      "Claude's cost is priced from its tokens (CLAUDE_PRICES, read 2026-10-25); "
                      "the TEE gateway's is as billed.", res["report"])
        self.assertIn("| 1 | claude | — | high |", res["report"])

    def test_a_fallback_answer_is_named_and_unpriced(self):
        self.server.state["claude_fallback"] = "claude-opus-4-8"
        code, res, err = self.run_json()
        self.assertEqual(code, 0, err)  # still a review, from the Claude family
        claude = res["results"]["claude"]
        self.assertEqual(claude["answered_by"], "claude-opus-4-8")
        self.assertEqual(claude["model"], "claude-opus-5-5")  # what the plan named
        self.assertEqual(len(claude["findings"]), 2)
        self.assertIsNone(claude["usage"]["cost"])
        self.assertIn("claude (round 1)", res["cost_time"]["unpriced"])
        self.assertIn("answered by claude-opus-4-8 (a fallback after the safeguards declined)",
                      res["report"])
        self.assertIn("Answered by a fallback model after the requested model's safeguards "
                      "declined, so not priced (the declined attempt may be billed too): claude "
                      "(round 1) by claude-opus-4-8, claude (round 2) by claude-opus-4-8.",
                      res["report"])
        self.assertNotIn("Claude's cost is priced", res["report"])
        self.server.state.pop("claude_fallback")
        self.server.state["requests"] = []
        self.server.state["claude_body"] = {  # another model string, no fallback signal
            "id": "msg_01", "model": "claude-opus-5-5-20261001", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": json.dumps(CLAUDE_FINDINGS)}],
            "usage": {"input_tokens": 800, "output_tokens": 600}}
        _, r, _, _, _ = self.claude_only()
        self.assertNotIn("answered_by", r)  # a resolved id is not a fallback
        self.assertEqual(r["usage"]["cost"], 0.0152)
        self.server.state["requests"] = []
        self.server.state["claude_body"] = dict(  # a fallback the model field does not show
            self.server.state["claude_body"], model="claude-opus-5-5", usage={
                "input_tokens": 800, "output_tokens": 600,
                "iterations": [{"type": "message"}, {"type": "fallback_message"}]})
        _, r, _, _, _ = self.claude_only()
        self.assertEqual(r["answered_by"], "an unnamed fallback model")
        self.assertIsNone(r["usage"]["cost"])
        self.server.state["requests"] = []
        self.server.state["claude_body"] = dict(  # only the content block marks the handoff
            self.server.state["claude_body"], usage={"input_tokens": 800, "output_tokens": 600},
            content=[{"type": "fallback", "from": {"model": "claude-opus-5-5"},
                      "to": {"model": "claude-opus-4-8"}},
                     {"type": "text", "text": json.dumps(CLAUDE_FINDINGS)}])
        _, r, _, _, _ = self.claude_only()
        self.assertEqual(r["answered_by"], "claude-opus-4-8")
        self.assertIsNone(r["usage"]["cost"])
        self.assertEqual(len(r["findings"]), 2)
        self.server.state["requests"] = []
        _, r, _, _, _ = self.claude_only("--model", "claude=haiku")  # no fallback was asked for
        self.assertNotIn("answered_by", r)
        self.assertEqual(r["usage"]["cost"], 0.00038)


class KeyScreeningTests(ClaudeCase):
    def test_its_key_value_is_screened_out_of_the_draft(self):
        os.environ["ANTHROPIC_API_KEY"] = "anthropic-test-value-" + "0123456789"
        self.assertIn(("ANTHROPIC_API_KEY", os.environ["ANTHROPIC_API_KEY"]),
                      chorus.screened_values(os.environ))
        path = self.draft(DRAFT + "Config: %s\n" % os.environ["ANTHROPIC_API_KEY"])
        code, _, err = self.run_cli("--sensitivity", "internal", "--file", path, "--yes")
        self.assertEqual(code, 2)
        self.assertIn("draft line 4: the value of ANTHROPIC_API_KEY", err)
        self.assertEqual(self.server.state["requests"], [])
        self.assertNotIn(os.environ["ANTHROPIC_API_KEY"], err)

    def test_an_anthropic_shaped_key_is_blocked_whoever_owns_it(self):
        shaped = "sk-" + "ant-" + "api03-" + "Zq7" * 14  # built here: no scanner trips on it
        path = self.draft(DRAFT + "export ANTHROPIC_API_KEY=%s\n" % shaped)
        code, _, err = self.run_cli("--sensitivity", "public", "--file", path, "--yes",
                                    "--allow-flagged")
        self.assertEqual(code, 2)
        self.assertIn("API secret key (sk-…)", err)
        self.assertEqual(self.server.state["requests"], [])
        self.assertNotIn(shaped, err)


class DraftedByTests(ClaudeCase):
    def test_the_report_labels_the_drafting_family(self):
        code, res, err = self.run_json("--drafted-by", "claude")
        self.assertEqual(code, 0, err)
        self.assertEqual(res["plan"]["drafted_by"], ["claude"])
        self.assertIn("drafting family: claude (Anthropic Claude, in the plan) helped write the "
                      "draft", err)
        self.assertTrue(res["results"]["claude"]["drafting_family"])
        self.assertNotIn("drafting_family", res["results"]["openai"])
        report = res["report"]
        self.assertIn("drafting family: **claude** (helped write the draft", report)
        self.assertIn("claude-opus-5-5 @ 127.0.0.1:%d, effort high, drafting family"
                      % self.server.server_address[1], report)
        self.assertIn("### claude (drafting family) — claude-opus-5-5", report)
        self.assertIn("PASS: tells (tells: weak evidence, since this family helped write the "
                      "draft)", report)
        self.assertIn("The drafting family (claude) helped write this draft and may favour it. "
                      "Its findings count in full; its approval counts for less: its pass on "
                      "tells is weak evidence, and its \"refuted\" in round 2 counts only as "
                      "\"disputed\".", report)
        self.assertIn("  - claude (drafting family): [MEDIUM][refute] no source", report)
        self.assertEqual(len(res["results"]["claude"]["findings"]), 2)  # it still reviews
        self.assertTrue(any("FINDINGS TO EXAMINE" in r[2]["messages"][0]["content"]
                            for r in self.posts("/claude/v1/messages")))  # and examines

    def test_without_the_marker_nothing_is_labelled(self):
        code, res, err = self.run_json()
        self.assertEqual(code, 0, err)
        self.assertNotIn("drafted_by", res["plan"])
        self.assertNotIn("drafting family", res["report"] + err)
        self.assertIn("PASS: tells\n", res["report"])

    def test_a_drafting_family_corroborates_like_any_other(self):
        code, res, err = self.run_json("--drafted-by", "claude")
        self.assertEqual(code, 0, err)
        findings = {(n, f["quote"]): f for n, r in res["results"].items()
                    for f in r.get("findings") or []}
        for key in (("openai", "400 projects"), ("claude", "funded 400 projects since 2019")):
            self.assertNotIn("exam_id", findings[key], key)  # one pair, read the same both ways
        report = res["report"]
        self.assertIn("**claude (drafting family) + openai** — \"funded 400 projects", report)
        self.assertNotIn("no corroboration", report)

    def test_two_drafting_families_do_not_corroborate_each_other(self):
        code, res, err = self.run_json("--drafted-by", "claude,openai")
        self.assertEqual(code, 0, err)
        findings = {(n, f["quote"]): f for n, r in res["results"].items()
                    for f in r.get("findings") or []}
        for key in (("openai", "400 projects"), ("claude", "funded 400 projects since 2019")):
            self.assertIn("exam_id", findings[key], key)  # both go to round 2
        self.assertEqual(sorted(findings["openai", "400 projects"]["exam"]),
                         ["claude", "glm", "kimi"])
        report = res["report"]
        self.assertIn("**claude (drafting family) + openai (drafting family)** · drafting "
                      "families only: no corroboration", report)
        self.assertIn("Two drafting families agreeing is no corroboration, so their shared "
                      "findings are here too.", report)
        self.assertIn("; two drafting families agreeing is no corroboration.", report)

    def test_a_drafting_family_that_does_not_review_is_only_noted(self):
        self.server.state["catalog"][0]["is_tee"] = False  # kimi is refused at the TEE check
        code, res, err = self.run_json("--drafted-by", "kimi")
        self.assertEqual(code, 6, err)
        self.assertIn("drafting family: kimi (Moonshot Kimi (open weights, via Phala), in the "
                      "plan)", err)
        report = res["report"]
        self.assertIn("drafted with help from **kimi**, which did not review in this run: "
                      "nothing of it to discount", report)
        self.assertNotIn("drafting family: **", report)
        self.assertNotIn("The drafting family (", report)

    def test_without_round_2_the_report_does_not_promise_round_2_rules(self):
        code, res, err = self.run_json("--drafted-by", "claude", "--no-cross-examine")
        self.assertEqual(code, 0, err)
        self.assertIn("its pass on tells is weak evidence.", res["report"])
        self.assertNotIn("round 2 counts only", res["report"])

    def test_another_drafting_family_counts_as_usual_for_claude(self):
        code, res, err = self.run_json("--drafted-by", "gemini")
        self.assertEqual(code, 0, err)
        self.assertIn("drafting family: gemini (Google Gemini, not in this run)", err)
        self.assertIn("drafted with help from **gemini**, which did not review in this run",
                      res["report"])
        findings = {(n, f["quote"]): f for n, r in res["results"].items()
                    for f in r.get("findings") or []}
        self.assertNotIn("exam_id", findings["openai", "400 projects"])  # Claude corroborates
        self.assertIn("PASS: tells\n", res["report"])  # Claude did not draft it

    def test_its_refuted_counts_only_as_disputed(self):
        def case(votes, drafters=()):
            f = {"exam_id": "F1"}
            exams = {n: {"verdicts": {"F1": {"verdict": v, "reason": "r"}}}
                     for n, v in votes.items()}
            chorus.apply_exam([("openai", f)], exams, drafters)
            return f["exam_status"]

        self.assertEqual(case({"claude": "refuted"}), "refuted")
        self.assertEqual(case({"claude": "refuted"}, ["claude"]), "disputed")
        three = {"kimi": "holds", "glm": "refuted", "claude": "refuted"}
        self.assertEqual(case(three), "refuted")
        self.assertEqual(case(three, ["claude"]), "disputed")
        self.assertEqual(case({"claude": "holds"}, ["claude"]), "held")
        self.assertEqual(case({"claude": "disputed"}, ["claude"]), "disputed")
        self.assertEqual(case({"kimi": "refuted", "glm": "refuted"}, ["claude"]), "refuted")
        self.server.state["exam"] = lambda model, fid: (
            "refuted" if model.startswith("claude-") else "holds")
        code, res, err = self.run_json("--drafted-by", "claude", "--voices", "openai,claude")
        self.assertEqual(code, 0, err)
        theirs = {f["quote"]: f for f in res["results"]["openai"]["findings"]}
        tells = theirs["not just innovative — it is transformative, scalable, and sustainable"]
        self.assertEqual(tells["exam"]["claude"], {"verdict": "refuted", "counts_as": "disputed",
                                                   "reason": "checked against the draft"})
        self.assertEqual(tells["exam_status"], "disputed")
        theirs = {f["quote"]: f for f in res["results"]["claude"]["findings"]}
        own = theirs["We request $250,000 over 12 months"]
        self.assertNotIn("counts_as", own["exam"]["openai"])  # only the drafter is softened
        self.assertIn("  - claude (drafting family): refuted (counts as disputed) — checked "
                      "against the draft", res["report"])

    def test_tells_pass_is_weak_only_for_the_drafting_family(self):
        self.server.state["claude_fn"] = lambda body: json.dumps(
            dict(CLAUDE_FINDINGS, **{"pass": ["refute"]}))
        code, res, _ = self.run_json("--drafted-by", "claude")
        self.assertIn("PASS: refute\n", res["report"])  # no tells pass: no note
        self.server.state.pop("claude_fn")
        code, res, _ = self.run_json("--drafted-by", "openai")
        self.assertIn("PASS: tells\n", res["report"])  # Claude's tells pass, not the drafter's
        self.assertIn("PASS: redteam\n", res["report"])  # OpenAI's pass carries no tells
        self.server.state["claude_fn"] = lambda body: json.dumps(
            dict(CLAUDE_FINDINGS, **{"pass": [" Tells "]}))
        code, res, _ = self.run_json("--drafted-by", "claude")
        self.assertIn("PASS:  Tells  (tells: weak evidence", res["report"])

    def test_bad_names_exit_1_and_repeats_collapse(self):
        code, _, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                    "--drafted-by", "claude,llama", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("--drafted-by: unknown voice 'llama'", err)
        code, _, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                    "--drafted-by", " claude, ,claude ", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(err.count("drafting family: claude"), 1)
        self.assertEqual(self.server.state["requests"], [])


class HelpTests(ClaudeCase):
    def test_help_documents_the_voice_and_the_marker(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            chorus.parse_args(["--help"])
        text = " ".join(out.getvalue().split())
        for s in ("--drafted-by", "ANTHROPIC_API_KEY", "CLAUDE_PRICES", "CLAUDE_EFFORT",
                  "api.anthropic.com", "drafting family",
                  "to Claude for the ids in CLAUDE_EFFORT (none → low",
                  "kimi, glm and claude stay within their own family",
                  "two voices share a hat when voices outnumber hats"):
            self.assertIn(s, text)


class StreamTests(ClaudeCase):
    """The streamed call's clocks and breaks: the wall bounds the whole answer."""

    def claude_fails(self, *argv, **state):
        self.server.state.update(state)
        code, res, err = self.run_json("--timeout", "claude=1", *argv)
        self.assertEqual(code, 6, err)  # the others printed: a partial chorus
        claude = res["results"]["claude"]
        self.assertTrue(claude["sent"])
        self.assertNotIn("findings", claude)
        return claude, res

    def test_no_headers_within_the_wall(self):
        claude, res = self.claude_fails(claude_delay=2)
        self.assertIn("never started after 1s (no response headers)", claude["error"])
        self.assertIn("claude (round 1)", res["cost_time"]["unpriced"])

    def test_a_stall_after_the_first_text(self):
        claude, _ = self.claude_fails(claude_stall=2)
        self.assertIn("still answering at the 1s deadline", claude["error"])
        self.assertGreaterEqual(claude["stream"]["lines"]["content"], 1)

    def test_a_stream_that_ends_without_message_stop(self):
        claude, _ = self.claude_fails(claude_cut=True)
        self.assertIn("the stream ended without finishing", claude["error"])

    def test_an_error_event(self):
        claude, _ = self.claude_fails(claude_stream_error=True)
        self.assertIn("the stream sent an error after", claude["error"])
        self.assertIn("Overloaded", claude["error"])

    def test_a_slow_answer_inside_the_wall_is_kept(self):
        self.server.state["claude_delay"] = 1
        code, r, err, _, _ = self.claude_only("--timeout", "claude=5")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(r["findings"]), 2)
        self.assertGreaterEqual(r["stream"]["headers"], 1)

    def test_odd_events_never_crash(self):
        for bad in ({"type": "message_delta", "delta": ["x"]}, {"type": "content_block_delta",
                                                              "index": 9, "delta": {}}, [1, 2]):
            msg, _, _ = self.parse_events([bad])
            self.assertEqual(msg["content"][0]["text"], "ok", bad)
            self.assertEqual(msg["stop_reason"], "end_turn", bad)

    def parse_events(self, extra):
        """Run messages_stream against a one-off server that sends `extra` events mid-stream."""
        events = [{"type": "message_start", "message": {"id": "m", "model": "claude-opus-5-5"}},
                  {"type": "content_block_start", "index": 0,
                   "content_block": {"type": "text", "text": ""}}] + extra + [
                  {"type": "content_block_delta", "index": 0,
                   "delta": {"type": "text_delta", "text": "ok"}},
                  {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {}},
                  {"type": "message_stop"}]

        class Events(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for e in events:
                    self.wfile.write(("data: %s\n\n" % json.dumps(e)).encode())

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Events)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            return chorus.messages_stream("http://127.0.0.1:%d/v1/messages" % srv.server_address[1],
                                          chorus.anthropic_headers(KEY), {}, 10)
        finally:
            srv.shutdown()
            srv.server_close()


class UseCaseTests(ClaudeCase):
    def test_claude_wears_its_dealt_hats_in_a_use_case(self):
        self.server.state.update(chat=grant_chat, openai_fn=grant_openai, claude_fn=grant_claude)
        code, out, err = self.run_cli("--use-case", "grant", "--sensitivity", "internal",
                                      "--file", self.draft(GRANT), "--seed", "demo", "--yes",
                                      "--json")
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        dealt = res["plan"]["hats"]["claude"]
        self.assertTrue(dealt)
        body = self.round1("/claude/v1/messages")[0][2]
        item = body["output_config"]["format"]["schema"]["properties"]["findings"]["items"]
        self.assertEqual(item["properties"]["hat"]["enum"], dealt)
        for h in dealt:
            self.assertIn("- %s (" % h, body["system"])  # its role text is in its prompt
        hats = {f["hat"] for f in res["results"]["claude"]["findings"]}
        self.assertEqual(hats, set(dealt))  # tagged with its own hats, never "?"
        self.assertTrue(any(n == "claude" for g in res["agreements"] for n in
                            {f["voice"] for f in g}))  # the shared passage, with the others
