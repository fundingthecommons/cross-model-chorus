"""Odd bodies, receipts, clocks, the guard corpus, quote matching and round 2 (the
cross-examination)."""
import json
import os
import threading
import time
import types
from unittest import mock

import harness
from harness import GLM_FINDINGS, KIMI_PINS, ChorusCase, Handler, QuietServer, chorus


class ReceiptRun:
    """A Kimi-only run whose receipt (and any other mock state) the test sets."""

    def receipt_case(self, receipt=None, cls="confidential", **state):
        if receipt is not None:
            self.server.state["receipt"] = receipt
        self.server.state.update(state)
        return self.run_cli("--sensitivity", cls, "--file", self.draft(), "--voices", "kimi",
                            "--yes")


class SendParseAndGuardTests(ReceiptRun, ChorusCase):
    """Duplicate voices, odd bodies, slow streams, parsing, quote matching, a moved home and
    the guard corpus."""

    def test_duplicate_voices_send_once(self):
        code, out, _ = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                    "--voices", "kimi,kimi,glm", "--yes", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.round1("/chat/completions")), 2)

    def test_tee_voice_refused_at_preflight_makes_a_partial_chorus(self):
        self.server.state["catalog"][0]["is_tee"] = False
        code, out, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(), "--yes",
                                      "--json")
        self.assertEqual(code, 6)
        kimi = json.loads(out)["results"]["kimi"]
        self.assertFalse(kimi["sent"])
        self.assertIn("refused before sending", kimi["error"])
        self.assertNotIn("CONFIDENTIALITY", err)

    def test_gemini_is_opt_in(self):
        os.environ["GEMINI_API_KEY"] = "test-gemini-key"
        code, out, _ = self.run_cli("--sensitivity", "public", "--file", self.draft(), "--yes",
                                    "--json")
        self.assertEqual(code, 0)
        self.assertNotIn("gemini", json.loads(out)["results"])

    def test_odd_bodies_after_a_send_never_crash(self):
        self.server.state["raw_body"] = {"moonshotai/kimi-k3": "<html>gateway hiccup</html>"}
        code, out, _ = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                    "--yes", "--json")
        self.assertEqual(code, 6)
        kimi = json.loads(out)["results"]["kimi"]
        self.assertTrue(kimi["sent"])
        self.assertEqual(kimi["receipt"], "verified")  # proven from the header, despite the body
        self.server.state.pop("raw_body")
        self.server.state["chat"] = lambda body: json.dumps(
            {"findings": [], "pass": True, "overall": "clean"})
        code, out, _ = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                    "--yes", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["results"]["glm"]["pass"], [])

    def test_truncated_5xx_body_is_handled(self):
        class Truncated(Handler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(502)
                self.send_header("Content-Length", "500")
                self.send_header("x-receipt-id", "rcpt-zzz")
                self.end_headers()
                self.wfile.write(b'{"error": "partial')

        srv = QuietServer(("127.0.0.1", 0), Truncated)
        srv.state = self.server.state
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            chorus.VOICES["glm"]["base"] = "http://127.0.0.1:%d/tee/v1" % srv.server_address[1]
            self.server.state["receipt"] = {"event_log": []}
            code, out, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                          "--voices", "glm", "--yes", "--json")
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(code, 5)
        self.assertIn("HTTP 502", json.loads(out)["results"]["glm"]["error"])

    def test_receipt_failover_that_ends_verified_passes(self):
        good = {"type": "upstream.verified", "result": "verified", "required": True,
                "session_id": KIMI_PINS[0]}  # a confidential Kimi call is pinned to upstream-c
        code, _, _ = self.receipt_case({"event_log": [dict(good, result="failed"), good]})
        self.assertEqual(code, 0)

    def test_slow_stream_is_still_receipt_checked(self):
        self.server.state["stall"] = {"moonshotai/kimi-k3": 2}
        code, out, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                      "--yes", "--timeout", "1", "--json")
        kimi = json.loads(out)["results"]["kimi"]
        self.assertTrue(kimi["sent"])
        self.assertIn("still answering at the 1s deadline", kimi["error"])
        self.assertEqual(kimi["receipt"], "verified")
        self.assertEqual(code, 6)  # slow, but proven confidential: partial, not exit 5

    def test_parse_findings_prefers_the_answer_over_a_sketch(self):
        final = json.dumps(GLM_FINDINGS)
        scratch = '{"findings": [], "pass": ["refute"], "overall": "SCRATCH"}'
        self.assertEqual(chorus.parse_findings("sketch %s\n...</think>\n%s" % (scratch, final))
                         ["overall"], "Mostly fine.")
        self.assertEqual(chorus.parse_findings("sketch %s\n</think>\n```json\n%s\n```"
                                               % (scratch, final))["overall"], "Mostly fine.")
        self.assertIsNone(chorus.parse_findings("<think>draft: %s" % scratch))

    def test_quote_matching_hard_wraps_underscores_and_ellipses(self):
        cases = [("We are **deeply\ncommitted** to open science.",
                  "We are deeply committed to open science"),
                 ("The lab was founded in\n2019. Since then it has grown.",
                  "founded in 2019. Since then it has grown"),
                 ("We expect growth of\n>50% in year two.", "growth of >50% in year two"),
                 ("Use the per_capita field and the total_count field.", "the total_count field"),
                 ("research---not marketing", "research — not marketing"),
                 ("AI and ML are tools.", "AI ... ML")]
        for draft, quote in cases:
            self.assertTrue(chorus.quote_spans(quote, chorus.norm(draft)), quote)
        nd = chorus.norm("This grant funds our grantees, who are granted a stipend.")
        self.assertEqual(len(chorus.quote_spans("grant", nd)), 1)
        nd = chorus.norm("Intro: the residency program starts. " + "x " * 220 +
                         "Later the residency program ends in June.")
        self.assertEqual(len(chorus.quote_spans("the residency program ... ends in June", nd)), 1)

    def test_never_send_survives_a_moved_home(self):
        """A default never-send entry (~/.ssh) is read against the passwd home too, so a moved
        $HOME cannot move it."""
        real_home = os.path.join(self.tmp.name, "realhome")
        path = self.draft("x", "n.md", sub="realhome/.ssh")
        with mock.patch.object(chorus.pwd, "getpwuid",
                               lambda uid: types.SimpleNamespace(pw_dir=real_home)):
            self.assertIn("~/.ssh", chorus.never_send_reason(path))

    def test_never_send_entry_survives_a_moved_home(self):
        """A CHORUS_NEVER_SEND entry written with ~ (as an env file keeps it) is the generic home
        of a personal never-send root, and must survive a moved $HOME the same way."""
        real_home = os.path.join(self.tmp.name, "realhome")
        path = self.draft("x", "n.md", sub="realhome/vault/notes")
        os.environ["CHORUS_NEVER_SEND"] = "~/vault"
        with mock.patch.object(chorus.pwd, "getpwuid",
                               lambda uid: types.SimpleNamespace(pw_dir=real_home)):
            never = chorus.load_config(types.SimpleNamespace())["never_send"]
            self.assertIn("CHORUS_NEVER_SEND entry", chorus.never_send_reason(path, never))

    def test_guard_corpus(self):
        flagged = ["Letters of support: Jane Doe, +44 (0)20 7946 0958.",
                   "Call 1-415-555-0134 to confirm.", "Office (415)555-0199.",
                   "London 020 7946 0958.", "UK mobile 07700 900123.",
                   "Jane Roe,https://janeroe.example,jane.roe@example.net,+14155550134",
                   "Scheduling: https://scheduler.example/janeroe?email=jane.roe@example.net",
                   "WhatsApp https://wa.me/447700900123",
                   "https://api.whatsapp.com/send?phone=447700900123",
                   "Callback: https://callback.example/dial?to=tel:+447700900123",
                   "Office: 42 W 99th St, Larkhaven, ZZ 00000.",
                   "Branch office: 1234 Larkspur Avenue NW, Larkhaven, ZZ 00000.",
                   "Ship to 77 Quillfeather Parkway, Larkhaven.", "Card 3782 822463 " + "10005.",
                   "Payout wallet: 9qVxFhT3kWmRz8bNpYc4LdJe6uHsKgAo2TfBnZxQrE",
                   "Contractor TIN: 123456789 for the 1099."]
        for line in flagged:
            self.assertTrue(chorus.screen("d", line, [])[1], line)
        clean = ["EIN: 871234567 on the determination letter.",
                 "Fernquill Fund Grant Agreement No. 104201337 (2023-2025).",
                 "Citation: Doe, A Field Guide to the Commons, ISBN 9781234567897.",
                 "Token budget: a fixed supply of 1000000000 governance tokens.",
                 "Meetup: events.example/tallowmere-larkhaven/events/301234567/",
                 "Hackathon outcomes: 12 First Place, 9 Second Place and 7 Third Place prizes.",
                 "Feedback: Season 5 Quillfeather Fund Drive, matched 3:1.",
                 "FY25 plan will need more cash.", "QF24 will cost more.",
                 "Headcount +2 2025-2026 (now 9 FTE).", "YoY attendance +15 (2024-2025).",
                 "Quarterly attendance: 5700 6200 4200 4900",
                 "Program questions to rfp@fernquill.example; reporting to "
                 "grantmaking@fernquill.example.", "![c](data:image/png;base64,iVBORw0KGgo"
                 "AAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==)",
                 "Researcher ID 0000-0001-2345-6789", "Zip 01234-5678"]
        for line in clean:
            self.assertEqual(chorus.screen("d", line, [])[1], [], line)


class CrossExaminationTests(ReceiptRun, ChorusCase):
    def test_cross_examination_marks_single_family_findings(self):
        code, out, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(), "--yes",
                                      "--json")
        self.assertEqual(code, 0, err)
        res = json.loads(out)["results"]
        unsourced = [f for f in res["openai"]["findings"] if f["quote"] == "400 projects"][0]
        self.assertEqual(unsourced["exam_status"], "held")
        self.assertEqual(sorted(unsourced["exam"]), ["glm", "kimi"])
        agreed = [f for f in res["kimi"]["findings"]][0]  # in an agreement group: not examined
        self.assertNotIn("exam_status", agreed)
        self.assertIn("round 2:", err)

    def test_examiners_never_judge_their_own_findings_and_refutations_show(self):
        self.server.state["exam"] = lambda model, fid: "refuted"
        code, out, _ = self.run_cli("--sensitivity", "internal", "--file", self.draft(), "--yes")
        self.assertEqual(code, 0)
        self.assertIn("## Cross-examination of findings only one family raised", out)
        self.assertIn("**refuted**", out)
        glm_round2 = [b for _, _, b, _ in self.posts("/chat/completions")
                      if b["model"] == "z-ai/glm-5.3" and "FINDINGS TO EXAMINE" in json.dumps(b)]
        self.assertEqual(len(glm_round2), 1)
        findings_block = glm_round2[0]["messages"][1]["content"].split("<<<FINDINGS", 1)[1]
        self.assertNotIn("10,000 members", findings_block)  # glm's own finding
        self.assertIn("400 projects", findings_block)
        self.assertNotIn("openai", findings_block.lower())  # anonymized

    def test_no_cross_examine_skips_round_two(self):
        code, _, _ = self.run_cli("--sensitivity", "internal", "--file", self.draft(), "--yes",
                                  "--no-cross-examine")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.round1("")), len(self.posts("")))

    def test_failed_cross_examination_is_a_partial_chorus(self):
        self.server.state["exam_openai_status"] = 500
        code, out, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(), "--yes")
        self.assertEqual(code, 6)
        self.assertIn("cross-examination incomplete: openai", err)
        self.assertIn("openai could not cross-examine", out)

    def test_receipt_falls_back_to_the_chat_id(self):
        self.server.state["missing_ids"] = {"rcpt-moonshotai-kimi-k3"}
        code, out, _ = self.receipt_case()
        self.assertEqual(code, 0)

    def test_check_receipt_flag(self):
        self.run_cli("--sensitivity", "internal", "--file", self.draft(), "--voices", "kimi",
                     "--yes")
        rid = "rcpt-moonshotai-kimi-k3"
        sent = len(self.server.state["requests"])
        # a re-check declares the sensitivity it holds the receipt to, or it fetches nothing
        for argv, why in (([], "STOP: declare the run's sensitivity"),
                          (["--use-case", "nope"], "unknown use case 'nope'"),
                          (["--use-case", "copy"],  # several sensitivities: none is implied
                           "STOP: declare the run's sensitivity"),
                          (["--use-case", "contracts", "--sensitivity", "internal"],
                           "takes sensitivity confidential, not internal")):
            code, out, err = self.run_cli("--check-receipt", rid, "--voices", "kimi", *argv)
            self.assertEqual(code, 1, argv)
            self.assertIn(why, err)
            self.assertEqual(out, "", argv)
        self.assertEqual(len(self.server.state["requests"]), sent)  # no receipt was fetched
        for argv in (["--sensitivity", "internal"], ["--sensitivity", "confidential"],
                     ["--use-case", "contracts"]):  # allowed only at confidential: implies it
            code, out, _ = self.run_cli("--check-receipt", rid, "--voices", "kimi", *argv)
            self.assertEqual(code, 0, argv)
            self.assertTrue(out.startswith("verified: "), out)
            self.assertEqual("re-check with --sensitivity confidential" in out,
                             argv == ["--sensitivity", "internal"], argv)
        self.server.state["missing_ids"] = {"rcpt-nope"}
        code, out, _ = self.run_cli("--check-receipt", "rcpt-nope", "--voices", "kimi",
                                    "--sensitivity", "internal")
        self.assertEqual(code, 6)  # still missing: a warning below confidential, as in the run
        code, out, _ = self.run_cli("--check-receipt", "rcpt-nope", "--voices", "kimi",
                                    "--sensitivity", "confidential")
        self.assertEqual(code, 5)  # at confidential a missing receipt is a failure

    def test_same_passage_different_brief_is_still_cross_examined(self):
        code, out, _ = self.run_cli("--sensitivity", "internal", "--file", self.draft(), "--yes",
                                    "--json")
        res = json.loads(out)["results"]
        # openai's tells finding shares a passage AND a brief with kimi's: corroborated
        triad = [f for f in res["openai"]["findings"] if f["brief"] == "tells"][0]
        self.assertNotIn("exam_status", triad)


class ReceiptRetryAndClockTests(ReceiptRun, ChorusCase):
    """Receipt retries, errors after forwarding, the clocks, and parse and quote edge cases."""

    def test_transient_receipt_errors_are_retried(self):
        self.server.state["receipt_flaky"] = 1
        code, _, _ = self.receipt_case()
        self.assertEqual(code, 0)

    def test_tee_error_after_forwarding_is_receipt_checked(self):
        self.server.state.update(chat_status={"moonshotai/kimi-k3": 503}, error_receipt=True,
                                 receipt={"event_log": [
                                     {"type": "route.selected"}, {"type": "request.forwarded"},
                                     {"type": "upstream.verified", "result": "failed",
                                      "required": True, "session_id": "s"}]})
        code, out, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                      "--voices", "kimi", "--yes", "--json")
        self.assertEqual(code, 5)
        kimi = json.loads(out)["results"]["kimi"]
        self.assertTrue(kimi["sent"])
        self.assertEqual(kimi["receipt"], "failed")
        code, _, _ = self.run_cli("--sensitivity", "internal", "--file", self.draft(), "--voices",
                                  "kimi", "--yes", "--no-cross-examine")
        self.assertEqual(code, 5)  # a receipt that says not-confidential stops every sensitivity

    def test_nonpositive_timeout_is_rejected_before_sending(self):
        for bad in ("-1", "0"):
            code, _, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                        "--yes", "--timeout", bad)
            self.assertEqual(code, 1)
        self.assertEqual(self.server.state["requests"], [])

    def test_stream_deadline_is_a_wall_clock(self):
        self.server.state["stall"] = {"z-ai/glm-5.3": 5}
        t0 = time.monotonic()
        self.run_cli("--sensitivity", "confidential", "--file", self.draft(), "--voices", "glm",
                     "--yes", "--timeout", "1", "--no-cross-examine")
        self.assertLess(time.monotonic() - t0, 4)

    def test_missing_receipt_at_internal_warns(self):
        self.server.state["receipt_status"] = 404
        code, _, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                    "--voices", "kimi", "--yes", "--no-cross-examine")
        self.assertEqual(code, 6)
        self.assertIn("TEE RECEIPT MISSING for kimi (sensitivity internal)", err)

    def test_parse_and_quote_edge_cases(self):
        ans = {"findings": [{"brief": "tells", "severity": "low", "quote": "a leaked </think> tag",
                             "issue": "artifact", "direction": "remove"}], "pass": [], "overall": "x"}
        self.assertEqual(chorus.parse_findings(json.dumps(ans))["findings"][0]["quote"],
                         "a leaked </think> tag")
        nd = chorus.norm("We fund open science. " + "filler words here. " * 20 +
                         "Results are public.")
        self.assertEqual(chorus.quote_spans(
            "We fund open science ... Results are public ... and we hired 50 staff", nd), [])
        nd = chorus.norm("We will hire two staff in 2026. We will launch the public ledger by 2027.")
        self.assertEqual(len(chorus.quote_spans("We will ... by 2027", nd)), 1)
        nd = chorus.norm("Goals:\n- Grow the network to 500 builders\n- Fund 40 projects a year\n")
        self.assertTrue(chorus.quote_spans("Grow the network to 500 builders - Fund 40 projects", nd))
        nd = chorus.norm("We are **the only organization doing this work** in Southeast Asia.")
        self.assertTrue(chorus.quote_spans("We are **the only organization", nd))
        self.assertTrue(chorus.quote_spans("open‑source", chorus.norm("open-source tools")))


class AttestationAndQuoteTests(ReceiptRun, ChorusCase):
    """Unattested hops, round 2 and receipts, the re-check's model, quotes and answer parsing."""

    def test_forwarded_hop_without_attestation_is_failed(self):
        receipt = {"event_log": [{"type": "route.selected"}, {"type": "request.forwarded"},
                                 {"type": "response.received"}]}
        code, _, err = self.receipt_case(receipt, cls="internal")
        self.assertEqual(code, 5)
        self.assertIn("CONFIDENTIALITY NOT PROVEN", err)

    def test_round_two_skips_a_tee_voice_whose_receipt_failed(self):
        self.server.state["receipt"] = {"event_log": [{"type": "upstream.verified",
                                                       "result": "failed", "required": True,
                                                       "session_id": "s"}]}
        self.run_cli("--sensitivity", "internal", "--file", self.draft(), "--yes")
        round2 = [b["model"] for _, _, b, _ in self.posts("/chat/completions")
                  if "FINDINGS TO EXAMINE" in json.dumps(b)]
        self.assertEqual(round2, [])

    def test_check_receipt_uses_the_runs_model(self):
        self.run_cli("--sensitivity", "internal", "--file", self.draft(), "--voices", "glm",
                     "--model", "glm=5.2", "--yes", "--no-cross-examine")
        self.server.state["catalog"].append({"id": "z-ai/glm-5.2", "is_tee": True})
        self.run_cli("--sensitivity", "internal", "--file", self.draft(), "--voices", "glm",
                     "--model", "glm=5.2", "--yes", "--no-cross-examine")
        code, out, _ = self.run_cli("--check-receipt", "rcpt-z-ai-glm-5.2", "--voices", "glm",
                                    "--model", "glm=5.2", "--sensitivity", "internal")
        self.assertEqual(code, 0, out)

    def test_punctuation_quotes_footnotes_escapes_and_lists(self):
        f = {"quote": "—", "spans": [], "severity": "low", "brief": "tells", "issue": "i",
             "direction": ""}
        self.assertIn("punctuation: not one passage", chorus.fmt_finding(f))
        nd = chorus.norm("Funders want evidence[^1] and the 3\\.5% figure.\n\n[^1]: note")
        self.assertTrue(chorus.quote_spans("Funders want evidence and the 3.5% figure", nd))
        self.assertTrue(chorus.quote_spans("Funders want [...] the 3.5% figure", nd))
        nd = chorus.norm("Goals:\n\n1. Grow the network to 500\n   builders this year\n2. Fund 40 projects\n")
        self.assertTrue(chorus.quote_spans("Grow the network to 500 builders this year Fund 40 projects", nd))

    def test_round_two_ids_are_normalized(self):
        self.server.state["exam"] = lambda model, fid: "holds"
        orig = harness.exam_reply

        def lower_ids(text, state, model):
            return orig(text, state, model).replace('"F', '"f')

        with mock.patch.object(harness, "exam_reply", lower_ids):
            code, out, _ = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                        "--yes", "--json")
        self.assertEqual(code, 0)
        res = json.loads(out)["results"]
        self.assertEqual([f for f in res["openai"]["findings"] if f["quote"] == "400 projects"][0]
                         ["exam_status"], "held")

    def test_parse_json_keeps_answers_that_quote_think_tags_inside_prose(self):
        ans = json.dumps({"findings": [{"brief": "tells", "severity": "low",
                                        "quote": "a leaked </think> tag", "issue": "artifact",
                                        "direction": "remove"}], "pass": [], "overall": "x"})
        self.assertIsNotNone(chorus.parse_findings("Here is my review:\n" + ans))
        self.assertIsNotNone(chorus.parse_findings("```json\n" + ans + "\n```\nDone."))
        self.assertIsNone(chorus.parse_findings("<think>draft: " + ans))


# Kimi's round-1 receipt ids as the mock serves them: the x-receipt-id header and the chat id.
KIMI_RECEIPT_IDS = {"rcpt-moonshotai-kimi-k3", "req-rcpt-moonshotai-kimi-k3"}
FAILED_RECEIPT = {"event_log": [{"type": "upstream.verified", "result": "failed",
                                 "required": True, "session_id": "s"}]}


class CrossExaminationReceiptTests(ReceiptRun, ChorusCase):
    """Which TEE voices round 2 may use, and the receipts of its own calls."""

    def exam_models(self, since=0):
        return [b["model"] for _, _, b, _ in self.posts("/chat/completions")[since:]
                if "FINDINGS TO EXAMINE" in json.dumps(b)]

    def test_a_missing_round_one_receipt_bars_round_two_only_when_confidential(self):
        self.server.state["missing_ids"] = set(KIMI_RECEIPT_IDS)  # 404 on both of Kimi's ids
        code, out, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                      "--yes", "--json")
        self.assertEqual(code, 5, err)
        res = json.loads(out)
        self.assertEqual(res["results"]["kimi"]["receipt"], "unverified")
        self.assertIn("could not be fetched", res["results"]["kimi"]["receipt_detail"])
        self.assertEqual(res["results"]["glm"]["receipt"], "verified")
        self.assertEqual(self.exam_models(), ["z-ai/glm-5.3"])  # Kimi gets no round-2 request
        self.assertEqual(sorted(res["cross_examination"]["calls"]), ["glm"])
        self.assertEqual(res["cross_examination"]["calls"]["glm"]["receipt"], "verified")
        self.assertRegex(err, r"round 2: \d+ single-family finding\(s\) to glm\n")
        self.assertIn("CONFIDENTIALITY NOT PROVEN for kimi", err)
        # outside confidential, a missing receipt is a warning: Kimi still cross-examines
        since = len(self.posts("/chat/completions"))
        code, out, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                      "--yes", "--json")
        self.assertEqual(code, 6, err)
        res = json.loads(out)
        self.assertEqual(res["results"]["kimi"]["receipt"], "unverified")
        self.assertIn("moonshotai/kimi-k3", self.exam_models(since))
        self.assertIn("kimi", res["cross_examination"]["calls"])
        self.assertIn("TEE RECEIPT MISSING for kimi (sensitivity internal)", err)
        self.assertNotIn("CONFIDENTIALITY NOT PROVEN", err)

    def test_a_receipt_that_fails_only_in_round_two_exits_5(self):
        st = self.server.state
        for cls, flip, want in (("internal", {"receipt": FAILED_RECEIPT}, "failed"),
                                ("confidential", {"receipt_status": 404}, "unverified")):
            st.pop("receipt", None)
            st.pop("receipt_status", None)

            def exam(model, fid, flip=flip):
                st.update(flip)  # round 2 has started: every receipt from here on fails
                return "holds"

            st["exam"] = exam
            since = len(self.posts("/chat/completions"))
            code, out, err = self.run_cli("--sensitivity", cls, "--file", self.draft(), "--yes",
                                          "--json")
            self.assertEqual(code, 5, cls)
            res = json.loads(out)
            self.assertEqual(sorted(self.exam_models(since)),
                             ["moonshotai/kimi-k3", "z-ai/glm-5.3"], cls)
            for n in ("kimi", "glm"):
                self.assertEqual(res["results"][n]["receipt"], "verified", (cls, n))  # round 1
                self.assertEqual(res["cross_examination"]["calls"][n]["receipt"], want, (cls, n))
            line = [ln for ln in err.splitlines() if "CONFIDENTIALITY NOT PROVEN" in ln]
            self.assertEqual(len(line), 1, cls)
            self.assertIn("glm", line[0])
            self.assertIn("kimi", line[0])
