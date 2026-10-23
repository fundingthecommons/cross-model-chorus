"""Stream clocks and stats, effort mapping, upstream pinning and the time-and-cost report."""
import contextlib
import io
import json
import time
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from harness import CONTRACT, GLM_FINDINGS, SESSIONS, ChorusCase, chorus


class StreamAndPinningTests(ChorusCase):
    def glm_only(self, *extra, cls="public"):
        """One GLM call, no round 2; returns (code, the glm result, stderr, the whole JSON)."""
        code, out, err = self.run_cli("--sensitivity", cls, "--file", self.draft(), "--voices",
                                      "glm", "--no-cross-examine", "--yes", "--json", *extra)
        res = json.loads(out)
        return code, res["results"]["glm"], err, res

    def test_effort_is_mapped_per_exact_model_id(self):
        e = chorus.chat_effort
        for level, want in (("none", "low"), ("low", "low"), ("medium", "low"),
                            ("high", "high"), ("xhigh", "max"), ("max", "max")):
            self.assertEqual(e("z-ai/glm-5.3", level), want, level)
            self.assertEqual(e("moonshotai/kimi-k3", level), want, level)
            self.assertEqual(e("z-ai/glm-5.2", level), "max" if want == "max" else "high", level)
            self.assertIsNone(e("z-ai/glm-5.3-flash", level))  # exact ids: lists no effort
            self.assertIsNone(e("gemini-4-argon", level))
        for model in ("z-ai/glm-5.3", "z-ai/glm-5.2", "moonshotai/kimi-k3"):
            self.assertIsNone(e(model, "unset"))
        sent = {m: set(chorus.GLM_EFFORT[m].values()) for m in chorus.GLM_EFFORT}
        self.assertEqual(sent, {"z-ai/glm-5.3": {"low", "high", "max"},
                                "z-ai/glm-5.2": {"high", "max"}})  # never medium/none/minimal

    def test_effort_flags_per_voice_unset_and_the_catalog_gate(self):
        code, out, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(), "--yes",
                                      "--json", "--effort", "max", "--effort", "glm=unset",
                                      "--effort", "openai=low")
        self.assertEqual(code, 0, err)
        chat = {b["model"]: b for _, _, b, _ in self.round1("/chat/completions")}
        self.assertNotIn("reasoning_effort", chat["z-ai/glm-5.3"])  # unset: the model's default
        self.assertEqual(chat["moonshotai/kimi-k3"]["reasoning_effort"], "max")
        (_, _, oai, _), = self.round1("/responses")
        self.assertEqual(oai["reasoning"], {"effort": "low"})
        res = json.loads(out)
        self.assertIsNone(res["results"]["glm"]["effort"])
        self.assertEqual(res["results"]["kimi"]["effort"], "max")
        self.assertIn("effort none sent", res["report"])
        code, _, _ = self.run_cli("--sensitivity", "internal", "--file", self.draft(), "--voices",
                                  "openai", "--effort", "openai=unset", "--yes",
                                  "--no-cross-examine")
        self.assertEqual(code, 0)
        self.assertNotIn("reasoning", self.round1("/responses")[-1][2])
        self.server.state["catalog"][1]["supported_parameters"] = ["max_tokens"]
        code, glm, _, _ = self.glm_only("--effort", "high")
        self.assertEqual(code, 0)
        self.assertNotIn("reasoning_effort", self.round1("/chat/completions")[-1][2])
        self.assertIsNone(glm["effort"])  # what was sent, not what was asked
        self.server.state["catalog"].append({"id": "z-ai/glm-5.2", "is_tee": True,
                                             "supported_parameters": ["reasoning_effort"],
                                             "supported_features": ["structured_outputs"]})
        code, glm, _, _ = self.glm_only("--model", "glm=5.2", "--effort", "glm=low")
        self.assertEqual(code, 0)
        self.assertEqual(self.round1("/chat/completions")[-1][2]["reasoning_effort"], "high")
        n = len(self.server.state["requests"])
        for bad in (["--effort", "unset"], ["--effort", "glm=bogus"], ["--effort", "nobody=high"],
                    ["--timeout", "glm=0"], ["--timeout", "glm=soon"], ["--timeout", "x=5"],
                    ["--first-data", "0"]):
            code, _, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                        "--yes", *bad)
            self.assertEqual(code, 1, bad)
        self.assertEqual(len(self.server.state["requests"]), n)

    def test_reasoning_deltas_count_as_progress_and_no_text_is_kept(self):
        secret = ["WEIGHING-THE-ASK", "SECOND-THOUGHT", "DETAIL-TEXT"]
        self.server.state["script"] = {"z-ai/glm-5.3": [
            ("role",), ("hb",), ("reason", "reasoning_content", secret[0]),
            ("reason", "reasoning", secret[1]),
            ("reason", "reasoning_details", [{"type": "reasoning.text", "text": secret[2]}]),
            ("wait", 0.3), ("hb",), ("content",), ("done",)]}
        code, glm, err, res = self.glm_only()
        self.assertEqual(code, 0, err)
        st = glm["stream"]
        self.assertEqual(st["lines"]["role"], 1)
        self.assertEqual(st["lines"]["reasoning"], 3)
        self.assertEqual(st["lines"]["heartbeat"], 2)
        self.assertGreater(st["lines"]["content"], 1)
        self.assertEqual(st["chars"]["reasoning"], sum(len(s) for s in secret))
        self.assertEqual(st["chars"]["content"], len(json.dumps(GLM_FINDINGS)))
        self.assertEqual(st["delta_keys"], ["content", "reasoning", "reasoning_content",
                                            "reasoning_details", "role"])
        for k in ("headers", "first_data", "first_token", "last_data", "last_content"):
            self.assertIsNotNone(st[k], k)
        self.assertLessEqual(st["first_data"], st["first_token"])
        self.assertGreaterEqual(st["max_gap"], 0.3)
        self.assertEqual(st["idle_over"], 0)
        blob = json.dumps(res) + err
        for s in secret:  # counts only: reasoning text is never kept or printed
            self.assertNotIn(s, blob)
        self.assertEqual(glm["usage"]["reasoning"], 120)
        self.assertIn("_stream: headers", res["report"])

    def test_the_idle_clock_is_log_only(self):
        self.server.state["script"] = {"z-ai/glm-5.3": [
            ("role",), ("reason", "reasoning_content", "a"), ("hb",), ("wait", 0.25), ("hb",),
            ("wait", 0.25), ("hb",), ("wait", 0.25), ("reason", "reasoning_content", "b"),
            ("content",), ("done",)]}
        with mock.patch.object(chorus, "IDLE_SECONDS", 0.4):
            code, glm, err, _ = self.glm_only()
        self.assertEqual(code, 0, err)  # logged, never enforced
        self.assertEqual(glm["stream"]["idle_over"], 1)  # once per gap, not per heartbeat
        self.assertEqual(err.count("the idle clock is log-only"), 1)
        self.assertNotIn("error", glm)

    def test_never_started_covers_the_headers_and_heartbeats(self):
        self.server.state["script"] = {"z-ai/glm-5.3": [("hb",)] + [("wait", 0.3), ("hb",)] * 10}
        code, glm, _, _ = self.glm_only("--first-data", "1", "--timeout", "glm=9")
        self.assertEqual(code, 4)
        self.assertRegex(glm["error"], r"^never started after 1s \(headers at [\d.]+s, \d+ "
                                       r"heartbeat\(s\), no data\)$")
        self.assertTrue(glm["sent"])
        self.assertEqual(glm["receipt"], "verified")  # checked even so
        self.assertGreater(glm["stream"]["lines"]["heartbeat"], 1)
        self.server.state.pop("script")
        self.server.state["delay"] = {"z-ai/glm-5.3": 2}  # no headers at all: the clock covers open
        t0 = time.monotonic()
        code, glm, _, _ = self.glm_only("--first-data", "1", "--timeout", "glm=9")
        self.assertLess(time.monotonic() - t0, 2.5)
        self.assertEqual(glm["error"], "never started after 1s (no response headers)")
        self.assertIsNone(glm["stream"]["headers"])

    def test_abort_classes_stalled_still_reasoning_answering_and_errors(self):
        st = self.server.state
        st["script"] = {"z-ai/glm-5.3": [("role",)] + [("reason", "reasoning_content", "x")] * 3
                        + [("wait", 0.2), ("hb",)] * 15 + [("content",), ("done",)]}
        with mock.patch.object(chorus, "IDLE_SECONDS", 0.5):
            code, glm, err, _ = self.glm_only("--timeout", "glm=2")
        self.assertRegex(glm["error"], r"^stalled: no data after [\d.]+s, [12]s without any "
                                       r"\(reasoning 3, content 0 data lines")
        self.assertIn("idle clock is log-only", err)
        st["script"] = {"z-ai/glm-5.3": [("role",)] + [("reason", "reasoning", "y"),
                                                         ("wait", 0.05)] * 60}
        code, glm, _, _ = self.glm_only("--timeout", "1")
        self.assertRegex(glm["error"], r"^still reasoning at the 1s deadline, about \d+ tok/s "
                                       r"counting a delta as a token, (about \d+|under 1) "
                                       r"counting 4 characters as one \(reasoning \d+, content 0")
        self.assertGreater(glm["stream"]["lines"]["reasoning"], 5)  # partial stats kept
        st.pop("script")
        st["stall"] = {"z-ai/glm-5.3": 2}
        code, glm, _, _ = self.glm_only("--timeout", "1")
        self.assertRegex(glm["error"], r"^still answering at the 1s deadline \(reasoning 0, "
                                       r"content 1 data lines")
        st.pop("stall")
        st["script"] = {"z-ai/glm-5.3": [("role",), ("reason", "reasoning_content", "z"),
                                         ("error",)]}
        code, glm, _, _ = self.glm_only()
        self.assertEqual(code, 4)
        self.assertRegex(glm["error"], r"^the stream sent an error after [\d.]+s: upstream went away")
        self.assertEqual(glm["stream"]["lines"]["reasoning"], 1)
        for e in (glm["error"],):  # the old message is gone
            self.assertNotIn("no data before the", e)

    def test_per_voice_wall(self):
        self.server.state["stall"] = {"z-ai/glm-5.3": 1.5, "moonshotai/kimi-k3": 1.5}
        code, out, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                      "--yes", "--json", "--no-cross-examine", "--timeout", "1",
                                      "--timeout", "glm=6")
        res = json.loads(out)
        self.assertNotIn("error", res["results"]["glm"])  # its own wall held it
        self.assertIn("still answering at the 1s deadline", res["results"]["kimi"]["error"])
        self.assertEqual({r["voice"]: r["timeout"] for r in res["plan"]["voices"]},
                         {"glm": 6, "kimi": 1})
        self.assertIn("wall 6s", err)
        self.assertEqual(code, 6)

    def test_confidential_glm_is_pinned_to_approved_upstreams_and_its_upstream_recorded(self):
        code, out, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                      "--yes", "--json")
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        glm = res["results"]["glm"]
        self.assertEqual(glm["upstream"], "upstream-b")  # named via its pin
        self.assertEqual(sorted(u for _, u in glm["pinned"]), ["upstream-a", "upstream-b"])
        self.assertEqual(res["results"]["kimi"]["upstream"], "upstream-c")  # named via its pin
        self.assertEqual(sorted({u for _, u in res["results"]["kimi"]["pinned"]}), ["upstream-c"])
        self.assertEqual(res["cross_examination"]["calls"]["glm"]["upstream"], "upstream-b")
        # fresh ids before every call: the preflight, round 1 and round 2, per voice
        gets = [parse_qs(urlsplit(p).query)["model"][0] for _, p, _, _ in
                self.reqs("GET", "/aci/sessions?")]
        self.assertEqual(sorted(gets), ["moonshotai/kimi-k3"] * 3 + ["z-ai/glm-5.3"] * 3)
        self.assertIn("pinned to upstream-a or upstream-b", err)
        self.assertIn("served by upstream-b, pinned to upstream-a or upstream-b", res["report"])
        self.assertEqual({c["voice"]: c["upstream"] for c in res["cost_time"]["calls"]
                          if c["round"] == "1"}, {"glm": "upstream-b", "kimi": "upstream-c"})
        code, out, _ = self.run_cli("--check-receipt", "rcpt-z-ai-glm-5.3", "--voices", "glm",
                                    "--sensitivity", "confidential")
        self.assertEqual(code, 0, out)  # a confidential re-check: upstream-b is approved
        self.assertIn("served by upstream-b", out)

    def test_confidential_glm_is_refused_without_an_approved_upstream_session(self):
        st = self.server.state
        st["sessions"] = [s for s in st["sessions"] if s["upstream_name"] == "upstream-z"]
        path = self.draft(CONTRACT, "c.md")
        code, out, err = self.run_cli("--use-case", "contracts", "--file", path, "--redacted",
                                      "--yes", "--json")
        self.assertEqual(code, 6, err)  # a partial chorus: kimi ran and wore every hat
        res = json.loads(out)
        self.assertFalse(res["results"]["glm"]["sent"])
        self.assertIn("no live session for z-ai/glm-5.3 on upstream-a or upstream-b",
                      res["results"]["glm"]["error"])
        self.assertEqual(sorted(res["plan"]["hats"]["kimi"]),
                         ["commercial", "compliance", "counsel"])
        self.assertEqual([b["model"] for _, _, b, _ in self.posts("/chat/completions")
                          if "glm" in b["model"]], [])
        st["sessions_status"] = 500  # an unreadable list refuses every confidential TEE voice
        n = len(self.posts("/chat/completions"))
        code, _, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                    "--yes")
        self.assertEqual(code, 2)
        self.assertIn("REFUSED glm: cannot prove TEE serving (the gateway's session list could "
                      "not be read", err)
        self.assertIn("a confidential run keeps glm on its approved upstreams (upstream-a or "
                      "upstream-b)", err)
        self.assertEqual(len(self.posts("/chat/completions")), n)  # fail closed: nothing sent
        n = len(self.server.state["requests"])
        for up, why in (
                ("glm=upstream-z", "keeps glm on its approved upstreams (upstream-a or "
                                   "upstream-b), not upstream-z"),
                ("glm=upstream-a,upstream-z", "not upstream-z"),
                ("kimi=upstream-z", "keeps kimi on its approved upstreams (upstream-c), not "
                                    "upstream-z"),
                ("glm=somewhere", "keeps glm on its approved upstreams (upstream-a or "
                                  "upstream-b), not somewhere"),
                ("glm=upstream-a-relay", "keeps glm on its approved upstreams (upstream-a or "
                                         "upstream-b), not upstream-a-relay")):  # exact names
            code, _, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                        "--upstream", up, "--yes")
            self.assertEqual(code, 2, up)
            self.assertIn(why, err)
        self.assertEqual(len(self.server.state["requests"]), n)  # refused before any request

    def test_a_pinned_call_served_outside_its_pin(self):
        self.server.state["serve_outside_pin"] = (SESSIONS[0]["session_id"], "upstream-z")
        code, out, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                      "--voices", "glm", "--yes", "--json", "--no-cross-examine")
        self.assertEqual(code, 5)  # confidential: a hop off the approved upstreams is not proven
        glm = json.loads(out)["results"]["glm"]
        self.assertEqual(glm["receipt"], "failed")
        self.assertIn("served by upstream-z outside the pinned session(s)", glm["receipt_detail"])
        self.assertIn("CONFIDENTIALITY NOT PROVEN for glm", err)
        code, glm, err, _ = self.glm_only("--upstream", "glm=upstream-a")
        self.assertEqual(code, 0, err)  # public: a note, not a stop
        self.assertEqual(glm["receipt"], "verified")
        self.assertIn("outside the pinned session(s) on upstream-a", glm["receipt_detail"])

    def test_upstream_pins_in_public_sensitivity_and_bad_upstream_flags(self):
        code, glm, err, _ = self.glm_only("--upstream", "glm=upstream-z")
        self.assertEqual(code, 0, err)  # public and internal may pin an unapproved upstream
        self.assertEqual(glm["upstream"], "upstream-z")
        (_, _, b, _), = self.round1("/chat/completions")
        self.assertEqual(b["provider"], {"aci_verified": True,
                                         "aci_session_ids": [SESSIONS[0]["session_id"]]})
        code, glm, err, _ = self.glm_only("--upstream", "glm=upstream-z", cls="internal")
        self.assertEqual(code, 0, err)
        self.assertEqual(glm["upstream"], "upstream-z")
        code, glm, _, _ = self.glm_only()
        self.assertNotIn("aci_session_ids", self.round1("/chat/completions")[-1][2]["provider"])
        self.server.state["sessions"] = []
        n = len(self.posts("/chat/completions"))
        code, _, err = self.run_cli("--sensitivity", "public", "--file", self.draft(), "--voices",
                                    "glm", "--upstream", "glm=upstream-a", "--yes")
        self.assertEqual(code, 2)  # the only voice refused before sending
        self.assertIn("REFUSED glm: cannot prove TEE serving (no live session for z-ai/glm-5.3 "
                      "on upstream-a", err)
        self.assertEqual(len(self.posts("/chat/completions")), n)
        for bad in ("openai=upstream-a", "glm", "glm=", "nobody=upstream-a"):
            code, _, _ = self.run_cli("--sensitivity", "public", "--file", self.draft(),
                                      "--upstream", bad, "--yes")
            self.assertEqual(code, 1, bad)

    def test_a_pin_that_cannot_be_resolved_at_send_sends_nothing(self):
        v = dict(chorus.VOICES["glm"], name="glm", upstreams=("upstream-a",))
        self.server.state["sessions"] = []
        args = chorus.parse_args(["--sensitivity", "public"])
        r, out = chorus.send("glm", v, "test-gateway-key", "sys", "user", "public", {}, args,
                             {"type": "object"}, "x")
        self.assertIsNone(out)
        self.assertFalse(r["sent"])
        self.assertIn("nothing sent", r["error"])
        self.assertEqual(self.posts("/chat/completions"), [])
        with self.assertRaises(chorus.PinUnavailable):
            chorus.pin_sessions(v, "test-gateway-key", ["upstream-b"])
        self.server.state["sessions"] = [{"session_id": "as_" + "d" * 64,
                                          "upstream_name": "upstream-a"},
                                         {"upstream_name": "x"}, 5]
        self.assertEqual(chorus.pin_sessions(v, "k", ["upstream-a"]),
                         [("as_" + "d" * 64, "upstream-a")])
        self.assertEqual(chorus._sid("AS_" + "D" * 64), "d" * 64)  # the docs' prefix compares equal

    def test_cost_and_time_totals(self):
        self.assertEqual(chorus.openai_cost("gpt-6.1-sol", {"input_tokens": 4021,
                                                            "output_tokens": 3902}), 0.047062)
        self.assertEqual(chorus.openai_cost("gpt-6.1-sol", {
            "input_tokens": 1000000, "output_tokens": 0,
            "input_tokens_details": {"cached_tokens": 1000000}}), None)  # over 272K: unpriced
        self.assertEqual(chorus.openai_cost("gpt-6.1-sol", {
            "input_tokens": 200000, "output_tokens": 0,
            "input_tokens_details": {"cached_tokens": 100000}}), 0.21)
        self.assertIsNone(chorus.openai_cost("gpt-7-unknown", {"input_tokens": 1,
                                                                "output_tokens": 1}))
        self.assertIsNone(chorus.openai_cost("gpt-6.1-sol", {}))
        code, out, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(), "--yes",
                                      "--json")
        self.assertEqual(code, 0, err)
        ct = json.loads(out)["cost_time"]
        calls = {(c["round"], c["voice"]): c for c in ct["calls"]}
        self.assertEqual(sorted(calls), [("1", "glm"), ("1", "kimi"), ("1", "openai"),
                                         ("2", "glm"), ("2", "kimi"), ("2", "openai")])
        self.assertEqual(calls["1", "openai"]["cost"], 0.0088)  # 900 in, 700 out at $2 / $10
        self.assertEqual(calls["1", "openai"]["cost_from"], "price table")
        self.assertEqual(calls["1", "kimi"]["cost"], 0.0081)
        self.assertEqual(calls["1", "kimi"]["cost_from"], "provider")
        self.assertEqual(ct["unpriced"], ["openai (round 2)"])  # the mock gives no usage there
        self.assertEqual(ct["cost_usd"], round(0.0088 + 4 * 0.0081, 6))
        self.assertEqual(sorted(ct["rounds"]), ["1", "2"])
        self.assertIsInstance(ct["seconds"], int)
        self.assertIsNotNone(calls["1", "glm"]["first_token"])
        self.assertIsNone(calls["1", "openai"]["first_token"])  # not streamed
        report = json.loads(out)["report"]
        self.assertIn("## Time and cost", report)
        self.assertIn("known cost **$%.4f**" % ct["cost_usd"], report)
        self.assertIn("Not in the total: openai (round 2)", report)
        self.assertIn("The gateway reports no reasoning-token count for Kimi", report)
        self.assertIn("priced from its tokens (OPENAI_PRICES", report)
        self.assertIn("known cost $%.4f; not counted: openai (round 2)" % ct["cost_usd"], err)

    def test_json_carries_the_report(self):
        code, out, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(), "--yes",
                                      "--json")
        self.assertEqual(code, 0, err)
        report = json.loads(out)["report"]
        self.assertTrue(report.startswith("# Chorus review — draft.md"))
        for part in ("## Same passage flagged by several families", "## By voice",
                     "## Time and cost", "## How to use this"):
            self.assertIn(part, report)
        code, md, _ = self.run_cli("--sensitivity", "internal", "--file", self.draft(), "--yes")
        self.assertEqual(md.split("## Time and cost")[0].split("## By voice")[0],
                         report.split("## Time and cost")[0].split("## By voice")[0])

    def test_dry_run_plan_shows_effort_wall_and_pins_and_sends_nothing(self):
        code, _, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                    "--dry-run", "--timeout", "glm=900")
        self.assertEqual(code, 0)
        self.assertRegex(err, r"glm +z-ai/glm-5\.3 @ \S+  \[TEE: checked before send\] · effort "
                              r"high if the catalog lists it · wall 900s · pinned to upstream-a "
                              r"or upstream-b")
        self.assertRegex(err, r"kimi +moonshotai/kimi-k3 @ \S+  \[TEE: checked before send\] · "
                              r"effort high if the catalog lists it · wall 540s · pinned to "
                              r"upstream-c\n")
        self.assertEqual(self.server.state["requests"], [])

    def test_help_documents_every_flag(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            chorus.parse_args(["--help"])
        text = " ".join(out.getvalue().split())
        for s in ("--first-data", "--upstream", "voice=unset", "--timeout glm=900",
                  "CHORUS_CONFIDENTIAL_UPSTREAMS",
                  "never started, stalled, still reasoning at the deadline, or still answering",
                  "OPENAI_PRICES", "'report'"):
            self.assertIn(s, text)
