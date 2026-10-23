"""TEE receipt hops and shapes, the catalog preflight, --check-receipt's declared sensitivity,
upstream pinning and per-voice approved upstreams, stream clocks and cost accounting."""
import contextlib
import io
import itertools
import json
import os
import threading
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from harness import (APPROVED, GLM_FINDINGS, KIMI_PINS, KIMI_SESSIONS, SESSIONS, ChorusCase,
                     Handler, QuietServer, chorus)

# The fixtures' upstreams by role: GLM's approved pair, GLM's unapproved one, Kimi's approved one.
Z_HOP = (SESSIONS[0]["session_id"], "upstream-z")  # listed first, so an unpinned call lands here
B_HOP = (SESSIONS[1]["session_id"], "upstream-b")
A_HOP = (SESSIONS[2]["session_id"], "upstream-a")
GLM_RULE = "a confidential run keeps z-ai/glm-5.3 on upstream-a or upstream-b"
KIMI_RULE = "a confidential run keeps moonshotai/kimi-k3 on upstream-c"


class TeeCase(ChorusCase):
    """Helpers shared by the receipt and pinning tests."""

    def glm_only(self, *extra, sensitivity="public"):
        """One GLM call, no round 2; returns (code, the glm result, stderr, the whole JSON)."""
        code, out, err = self.run_cli("--sensitivity", sensitivity, "--file", self.draft(),
                                      "--voices", "glm", "--no-cross-examine", "--yes", "--json",
                                      *extra)
        res = json.loads(out)
        return code, res["results"]["glm"], err, res

    def two_hop(self, first, last):
        """A receipt whose request failed over: two forwarded hops, each attested."""
        hop = lambda sid, up: [{"type": "route.selected"},
                               {"type": "upstream.verified", "result": "verified", "required": True,
                                "session_id": sid, "upstream_name": up},
                               {"type": "request.forwarded"}]
        return {"event_log": hop(*first) + hop(*last)}

    def other_server(self, handler):
        """Serve the GLM voice from a second mock with its own handler, sharing the state."""
        srv = QuietServer(("127.0.0.1", 0), handler)
        srv.state = self.server.state
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        chorus.VOICES["glm"]["base"] = "http://127.0.0.1:%d/tee/v1" % srv.server_address[1]

    def kimi_posts(self):
        return len([b for _, _, b, _ in self.posts("/chat/completions") if "kimi" in b["model"]])


class ReceiptHopsStreamsAndCostTest(TeeCase):
    """Failover hops, the pin on every error path, the stream's clocks and counts, and cost."""

    def test_every_hop_of_a_receipt_is_held_to_the_approved_upstreams(self):
        self.server.state["receipt"] = self.two_hop(Z_HOP, A_HOP)
        code, out, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                      "--voices", "glm", "--yes", "--json", "--no-cross-examine")
        self.assertEqual(code, 5, err)  # the last hop is pinned, but the first was unapproved
        glm = json.loads(out)["results"]["glm"]
        self.assertEqual(glm["receipt"], "failed")
        self.assertEqual(glm["upstreams"], ["upstream-z", "upstream-a"])
        self.assertEqual(glm["upstream"], "upstream-a")  # the hop that answered
        self.assertIn("served by upstream-z outside the pinned session(s)", glm["receipt_detail"])
        self.assertIn("served by upstream-z: " + GLM_RULE, glm["receipt_detail"])
        self.assertIn("served by upstream-z then upstream-a", json.loads(out)["report"])
        code, glm, err, _ = self.glm_only("--upstream", "glm=upstream-a")
        self.assertEqual(code, 0, err)  # public: the failover is a note, not a stop
        self.assertIn("outside the pinned session(s) on upstream-a", glm["receipt_detail"])
        self.assertNotIn("a confidential run keeps", glm["receipt_detail"])
        self.server.state["receipt"] = self.two_hop(A_HOP, Z_HOP)
        code, out, _ = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                    "--voices", "kimi", "--yes", "--no-cross-examine", "--json")
        self.assertEqual(code, 5)  # every TEE voice is held to its own list, Kimi included
        detail = json.loads(out)["results"]["kimi"]["receipt_detail"]
        self.assertIn("served by upstream-a, upstream-z: " + KIMI_RULE, detail)
        self.server.state["receipt"] = {"event_log": [  # a pinned id, but a hop off the list
            {"type": "upstream.verified", "result": "verified", "required": True,
             "session_id": A_HOP[0], "upstream_name": "upstream-c"}]}
        code, out, _ = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                    "--voices", "glm", "--yes", "--no-cross-examine", "--json")
        self.assertEqual(code, 5)  # the receipt and the gateway's list disagree about the session
        self.assertIn("the receipt names upstream-c for a session the gateway lists as upstream-a",
                      json.loads(out)["results"]["glm"]["receipt_detail"])

    def test_check_receipt_applies_the_approved_upstreams_with_sensitivity_confidential(self):
        self.server.state["receipt"] = {"event_log": [
            {"type": "upstream.verified", "result": "verified", "required": True,
             "session_id": Z_HOP[0], "upstream_name": "upstream-z"}]}
        code, out, _ = self.run_cli("--check-receipt", "rcpt-x", "--voices", "glm",
                                    "--sensitivity", "confidential")
        self.assertEqual(code, 5)
        self.assertIn("failed:", out)
        self.assertIn("served by upstream-z: " + GLM_RULE, out)
        code, out, _ = self.run_cli("--check-receipt", "rcpt-x", "--voices", "glm",
                                    "--sensitivity", "internal")
        self.assertEqual(code, 0)  # attested; the sensitivity decides which upstreams may serve
        self.assertIn("served by upstream-z", out)
        self.assertIn("re-check with --sensitivity confidential", out)
        self.server.state["receipt"] = self.two_hop(B_HOP, A_HOP)
        code, out, _ = self.run_cli("--check-receipt", "rcpt-x", "--voices", "glm",
                                    "--sensitivity", "confidential")
        self.assertEqual(code, 0)
        self.assertIn("served by upstream-b then upstream-a", out)
        self.assertNotIn("a confidential run keeps", out)
        for up, why in (("upstream-c", GLM_RULE),
                        (None, "its upstream cannot be shown to be upstream-a or upstream-b")):
            ev = {"type": "upstream.verified", "result": "verified", "required": True,
                  "session_id": "s1", "upstream_name": up}  # off the list, or unnamed
            self.server.state["receipt"] = {"event_log": [ev]}
            code, out, _ = self.run_cli("--check-receipt", "rcpt-x", "--voices", "glm",
                                        "--sensitivity", "confidential")
            self.assertEqual(code, 5, up)
            self.assertIn(why, out)
            code, out, _ = self.run_cli("--check-receipt", "rcpt-x", "--voices", "glm",
                                        "--sensitivity", "internal")
            self.assertEqual(code, 0, up)
            self.assertIn("served by %s" % (up or "unnamed"), out)
            self.assertNotIn("None", out)

    def test_a_string_or_junk_cost_never_crashes_the_report(self):
        for cost, counted in (("0.0081", True), ("n/a", False), (float("nan"), False)):
            self.server.state["cost"] = cost
            code, out, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                          "--yes", "--json")
            self.assertEqual(code, 0, err)
            ct = json.loads(out)["cost_time"]
            kimi1 = [c for c in ct["calls"] if c["voice"] == "kimi" and c["round"] == "1"][0]
            self.assertEqual(kimi1["cost"], 0.0081 if counted else None, cost)
            self.assertEqual("kimi (round 1)" in ct["unpriced"], not counted, cost)
            self.assertIn("## Time and cost", json.loads(out)["report"])
        self.assertIsNone(chorus.usd(float("inf")))
        self.assertIsNone(chorus.usd(-1))
        with mock.patch.object(chorus, "render", side_effect=RuntimeError("boom")):
            self.server.state["serve_outside_pin"] = Z_HOP
            code, out, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                          "--yes", "--json", "--no-cross-examine")
        self.assertEqual(code, 5)  # a report that cannot be drawn still reaches the exit checks
        self.assertIn("could not be drawn (RuntimeError: boom)", json.loads(out)["report"])

    def test_a_pin_gone_stale_before_the_send_says_so(self):
        self.server.state["rotate_sessions"] = True
        code, out, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                      "--yes", "--json", "--no-cross-examine")
        self.assertEqual(code, 6, err)  # kimi ran; glm sent nothing
        res = json.loads(out)
        glm = res["results"]["glm"]
        self.assertFalse(glm["sent"])
        self.assertIn("no longer live when the call went (the ids rotate)", glm["error"])
        self.assertNotIn("effort", res["report"].split("**glm**")[1].split("\n")[0])

    def test_pin_enforcement_on_the_abort_5xx_and_forwarded_error_paths(self):
        st = self.server.state
        cases = {"stall": ({"stall": {"z-ai/glm-5.3": 2}}, ["--timeout", "glm=1"]),
                 "5xx": ({"chat_status": {"z-ai/glm-5.3": 500}, "error_receipt": True}, []),
                 "503 forwarded": ({"chat_status": {"z-ai/glm-5.3": 503}, "error_receipt": True},
                                   [])}
        outsiders = (Z_HOP, ("9" * 64, "upstream-a"))  # unapproved, and a session nobody pinned
        for (label, (state, extra)), outside in itertools.product(cases.items(), outsiders):
            for k in ("stall", "chat_status", "error_receipt"):
                st.pop(k, None)
            st.update(state, serve_outside_pin=outside)
            code, out, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                          "--voices", "glm", "--yes", "--json",
                                          "--no-cross-examine", *extra)
            self.assertEqual(code, 5, (label, outside[1]))
            glm = json.loads(out)["results"]["glm"]
            self.assertTrue(glm["sent"], label)
            self.assertEqual(glm["receipt"], "failed", label)
            listed = outside[0] in [x["session_id"] for x in SESSIONS]
            self.assertEqual(glm["upstream"], outside[1] if listed else "unnamed", label)
            self.assertIn("outside the pinned session(s)", glm["receipt_detail"], label)

    def test_either_end_marker_finishes_a_stream(self):
        for tail in ([("finish",)], [("done_marker",)]):
            self.server.state["script"] = {"z-ai/glm-5.3": [
                ("role",), ("rawline", "data: not json"), ("rawline", "data: [1, 2]"),
                ("content",)] + tail}
            code, glm, err, _ = self.glm_only()
            self.assertEqual(code, 0, (tail, err))
            self.assertEqual(glm["stream"]["lines"]["other"], 2 + (tail[0][0] == "finish"))

    def test_a_stream_that_ends_unfinished_is_an_abort(self):
        self.server.state["script"] = {"z-ai/glm-5.3": [("role",), ("reason", "reasoning", "r"),
                                                         ("content",)]}  # no finish, no [DONE]
        code, glm, _, _ = self.glm_only()
        self.assertEqual(code, 4)
        self.assertRegex(glm["error"], r"^the stream ended without finishing after [\d.]+s \(no "
                                       r"\[DONE\] or finish reason; reasoning 1, content \d+")
        self.assertNotIn("raw", glm)
        self.assertEqual(glm["receipt"], "verified")

    def test_a_disconnect_before_the_headers_never_started(self):
        class Hangup(Handler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.close_connection = True  # no status line at all

        self.other_server(Hangup)
        code, glm, _, _ = self.glm_only(sensitivity="internal")
        self.assertEqual(code, 4)
        self.assertRegex(glm["error"], r"^never started after 0s: the connection closed before "
                                       r"any response \((RemoteDisconnected|BadStatusLine|"
                                       r"ConnectionResetError)\)$")
        self.assertTrue(glm["sent"])
        self.assertIsNone(glm["stream"]["headers"])

    def test_a_chunked_stream_cut_mid_chunk_broke_off(self):
        class Cut(Handler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("x-receipt-id", "rcpt-z-ai-glm-5.3")
                self.end_headers()
                line = b'data: {"choices": [{"delta": {"reasoning_details": [{"summary": "abcd"}]}}]}\n\n'
                self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
                self.wfile.write(b"200\r\ndata: {\"cho")  # promises 512 bytes, sends 15
                self.wfile.flush()
                self.close_connection = True

        self.server.state["models"]["rcpt-z-ai-glm-5.3"] = "z-ai/glm-5.3"
        self.server.state["aci"]["rcpt-z-ai-glm-5.3"] = True
        self.other_server(Cut)
        code, glm, _, _ = self.glm_only(sensitivity="internal")
        self.assertEqual(code, 4)
        self.assertRegex(glm["error"], r"^the stream broke off after [\d.]+s \(IncompleteRead; "
                                       r"reasoning 1, content 0 data lines\)$")
        self.assertEqual(glm["stream"]["chars"]["reasoning"], 4)  # a details summary counts
        self.assertEqual(glm["receipt"], "verified")  # checked even so

    def test_a_provider_field_is_not_an_upstream_name(self):
        for sid, want in (("s1", "unnamed"), (KIMI_PINS[0], "upstream-c")):  # named by session only
            self.server.state["receipt"] = {"event_log": [
                {"type": "upstream.verified", "result": "verified", "required": True,
                 "session_id": sid, "provider": "upstream-z"}]}
            code, out, _ = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                        "--voices", "kimi", "--yes", "--json",
                                        "--no-cross-examine")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["results"]["kimi"]["upstream"], want, sid)

    def test_the_wall_takes_over_from_the_first_data_clock(self):
        self.server.state["script"] = {"z-ai/glm-5.3": [("role",), ("reason", "reasoning", "a")]
                                       + [("wait", 0.5), ("reason", "reasoning", "b")] * 4
                                       + [("wait", 1.5), ("content",), ("done",)]}
        code, glm, err, _ = self.glm_only("--first-data", "1", "--timeout", "glm=6")
        self.assertEqual(code, 0, err)  # data came in time, so the 6 s wall governs
        self.assertGreater(glm["stream"]["last_data"], 1.5)

    def test_silent_gaps_are_logged_and_counted_without_heartbeats(self):
        self.server.state["script"] = {"z-ai/glm-5.3": [
            ("role",), ("reason", "reasoning", "a"), ("wait", 0.6), ("reason", "reasoning", "b"),
            ("wait", 0.6), ("reason", "reasoning", "c"), ("content",), ("done",)]}
        with mock.patch.object(chorus, "IDLE_SECONDS", 0.3):
            code, glm, err, res = self.glm_only("--timeout", "glm=5")
        self.assertEqual(code, 0, err)  # log-only: two silent gaps, no abort
        self.assertEqual(glm["stream"]["idle_over"], 2)
        self.assertEqual(err.count("the idle clock is log-only"), 2)
        self.assertIn(" · 2 gap(s) over", res["report"])
        self.server.state["script"] = {"z-ai/glm-5.3": [("role",), ("reason", "reasoning", "a"),
                                                         ("wait", 3)]}  # a dead socket
        with mock.patch.object(chorus, "IDLE_SECONDS", 0.5):
            code, glm, err, _ = self.glm_only("--timeout", "glm=1")
        self.assertTrue(glm["error"].startswith("stalled: no data after"))
        self.assertEqual(glm["stream"]["idle_over"], 1)  # the silence before the stop counts
        self.assertGreaterEqual(glm["stream"]["max_gap"], 0.9)
        self.assertIn("when the call stopped; the idle clock is log-only", err)
        self.assertGreaterEqual(glm["seconds"], 1)

    def test_time_to_first_token_and_last_content(self):
        self.server.state["script"] = {"z-ai/glm-5.3": [
            ("role",), ("wait", 0.4), ("reason", "reasoning", "a"), ("wait", 0.4), ("content",),
            ("wait", 0.4), ("done",)]}
        code, glm, err, res = self.glm_only()
        self.assertEqual(code, 0, err)
        st = glm["stream"]
        self.assertGreaterEqual(st["first_token"] - st["first_data"], 0.3)
        self.assertGreaterEqual(st["last_content"] - st["first_token"], 0.3)
        self.assertGreaterEqual(st["last_data"] - st["last_content"], 0.3)
        self.assertLess(st["last_reasoning"], st["last_content"])
        self.assertEqual(res["cost_time"]["calls"][0]["first_token"], st["first_token"])

    def test_failed_but_billed_calls_count_and_unknown_costs_are_listed(self):
        self.server.state.update(openai_state="failed", stall={"z-ai/glm-5.3": 2})
        code, out, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(), "--yes",
                                      "--json", "--no-cross-examine", "--timeout", "glm=1")
        ct = json.loads(out)["cost_time"]
        calls = {c["voice"]: c for c in ct["calls"]}
        self.assertTrue(calls["openai"]["failed"])
        self.assertEqual(calls["openai"]["cost"], 0.0088)  # billed, so counted
        self.assertTrue(calls["glm"]["failed"])
        self.assertIn("glm (round 1)", ct["unpriced"])
        self.assertEqual(ct["cost_usd"], round(0.0088 + 0.0081, 6))
        self.assertIn("glm (failed)", json.loads(out)["report"])
        self.server.state.pop("stall")
        self.server.state["openai_body"] = {
            "id": "resp_r", "status": "completed", "usage": {"input_tokens": 900,
                                                             "output_tokens": 700},
            "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]}
        code, out, _ = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                    "--voices", "openai", "--yes", "--json")
        ct = json.loads(out)["cost_time"]
        self.assertTrue(ct["calls"][0]["failed"])
        self.assertEqual(ct["cost_usd"], 0.0088)

    def test_abort_class_boundaries(self):
        self.server.state["script"] = {"z-ai/glm-5.3": [("role",)] + [("hb",), ("wait", 0.2)] * 10}
        code, glm, _, _ = self.glm_only("--timeout", "1")
        self.assertRegex(glm["error"], r"^no reasoning or content after 1s: 1 role or empty data "
                                       r"line\(s\), the last at [\d.]+s, and \d+ heartbeat\(s\)$")
        self.server.state["script"] = {"z-ai/glm-5.3": [
            ("role",), ("text", "<think>"), ("text", "weighing"), ("wait", 0.1)] +
            [("text", "x"), ("wait", 0.1)] * 30}
        code, glm, _, _ = self.glm_only("--timeout", "1")
        self.assertRegex(glm["error"], r"^still reasoning in the answer's <think> block at the 1s "
                                       r"deadline, about \d+ tok/s")
        self.server.state["script"] = {"z-ai/glm-5.3": [("role",), ("reason", "reasoning", "a"),
                                                         ("wait", 3)]}
        with mock.patch.object(chorus, "IDLE_SECONDS", 0.6):  # quiet about 1.7x the idle clock
            code, glm, _, _ = self.glm_only("--timeout", "1")
        self.assertTrue(glm["error"].startswith("stalled:"), glm["error"])
        self.server.state["script"] = {"z-ai/glm-5.3": [("role",), ("text", " ")] + [
            ("reason", "reasoning", "y"), ("wait", 0.05)] * 40}  # one early blank, then thinking
        code, glm, _, _ = self.glm_only("--timeout", "1")
        self.assertTrue(glm["error"].startswith("still reasoning at the 1s deadline"),
                        glm["error"])
        st = chorus.new_stream_stats()
        st.update(first_data=0.0, first_token=10.0, last_data=30.0)
        st["lines"]["reasoning"] = 5
        self.assertIn("under 1 tok/s", chorus.abort_message(st, 31, 31))
        self.assertIsNone(chorus.openai_cost("gpt-6.1-sol", {"input_tokens": 272001,
                                                             "output_tokens": 0}))
        self.assertEqual(chorus.openai_cost("gpt-6.1-sol", {"input_tokens": 272000,
                                                            "output_tokens": 0}), 0.544)

    def test_another_glm_model_is_pinned_by_its_voice_on_confidential(self):
        code, _, err = self.run_cli("--sensitivity", "confidential", "--model", "glm=5.2",
                                    "--file", self.draft(), "--dry-run")
        self.assertEqual(code, 0, err)  # approvals belong to the voice, whatever model it runs
        self.assertRegex(err, r"glm +z-ai/glm-5\.2 @ \S+  \[TEE: checked before send\] .* · "
                              r"pinned to upstream-a or upstream-b")
        self.assertEqual(self.server.state["requests"], [])

    def test_session_ids_are_checked_and_upstream_names_match_exactly(self):
        v = dict(chorus.VOICES["glm"], name="glm", approved=("upstream-a", "upstream-b"))
        good = "a" * 64
        self.server.state["sessions"] = [
            {"session_id": "not-hex", "upstream_name": "upstream-a"},
            {"session_id": " ", "upstream_name": "upstream-a"},
            {"session_id": "b" * 64, "upstream_name": "upstream-a-relay"},
            {"session_id": "c" * 64, "upstream_name": "upstream-a", "model": "z-ai/glm-5.2"},
            {"session_id": good, "upstream_name": "Upstream-A"},
            {"session_id": "d" * 64, "upstream_name": "upstream-z"}]
        self.assertEqual(chorus.pin_sessions(v, "k", ["upstream-a"]), [(good, "Upstream-A")])
        self.assertEqual(chorus.pin_sessions(v, "k", ["upstream-z"]), [("d" * 64, "upstream-z")])
        with self.assertRaises(chorus.PinUnavailable):  # a prefix of a name is not the name
            chorus.pin_sessions(v, "k", ["upstream"])
        self.assertIsNone(chorus._sid(""))
        self.assertIsNone(chorus._sid("as_"))
        r = {"pinned": [(good, "upstream-a")]}
        self.server.state["receipt"] = {"event_log": [
            {"type": "upstream.verified", "result": "verified", "required": True,
             "session_id": "as_", "upstream_name": "upstream-a"}]}
        chorus.check_receipt(r, v, "k", "confidential", None, "rcpt-x")
        self.assertEqual(r["receipt"], "failed")  # a malformed session is never a pinned one
        self.assertIn("outside the pinned session(s)", r["receipt_detail"])
        self.assertFalse(chorus.upstream_matches("upstream-a-relay", "upstream-a"))
        self.assertTrue(chorus.upstream_matches(" Upstream-A ", "upstream-a"))
        for name in ("", "  ", None):
            self.assertFalse(chorus.upstream_matches(name, name), name)
        self.server.state["sessions"] = [
            {"session_id": "e" * 64, "upstream_name": "upstream-a", "expires_at": time.time() - 5},
            {"session_id": "f" * 64, "upstream_name": "upstream-a", "expires_at": "soon"}]
        self.assertEqual(chorus.pin_sessions(v, "k", ["upstream-a"]), [("f" * 64, "upstream-a")])
        self.assertIsNone(chorus.usd(10 ** 400))

    def test_raw_answers_never_carry_reasoning_text(self):
        for text in ("<think>SECRET-REASONING</think> plain prose, not JSON",
                     "preface <think>SECRET-REASONING never closed",
                     "SECRET-REASONING from a template's open tag</think> plain prose"):
            self.server.state["chat"] = lambda body, t=text: t
            code, glm, _, res = self.glm_only()
            self.assertEqual(code, 4)
            self.assertIn("[reasoning removed:", glm["raw"])
            self.assertNotIn("SECRET-REASONING", json.dumps(res))

    def test_mirrored_reasoning_is_counted_once(self):
        self.server.state["script"] = {"z-ai/glm-5.3": [
            ("delta", {"role": "assistant", "content": "", "reasoning": "abc",
                       "reasoning_details": [{"type": "reasoning.text", "text": "abc"}]}),
            ("delta", {"content": ""}), ("delta", {"reasoning_content": "abcd",
                                                     "reasoning": "abcd"}),
            ("content",), ("done",)]}
        code, glm, err, _ = self.glm_only()
        self.assertEqual(code, 0, err)
        st = glm["stream"]
        self.assertEqual(st["chars"]["reasoning"], 3 + 4)
        self.assertEqual(st["lines"]["reasoning"], 2)
        self.assertEqual(st["lines"]["role"], 1)
        self.assertEqual(st["lines"]["other"], 3)  # the empty content delta, finish and usage
        self.assertEqual(st["chars"]["content"], len(json.dumps(GLM_FINDINGS)))

    def test_sessions_are_read_for_the_voices_own_model(self):
        self.server.state["catalog"].append({"id": "z-ai/glm-5.2", "is_tee": True,
                                             "supported_features": ["structured_outputs"]})
        code, out, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                      "--yes", "--json", "--model", "glm=5.2",
                                      "--no-cross-examine")
        self.assertEqual(code, 6, err)  # no approved-upstream session serves GLM 5.2
        self.assertIn("no live session for z-ai/glm-5.2", json.loads(out)["results"]["glm"]
                      ["error"])
        models = {parse_qs(urlsplit(p).query)["model"][0]
                  for _, p, _, _ in self.reqs("GET", "/aci/sessions?")}
        self.assertEqual(models, {"z-ai/glm-5.2", "moonshotai/kimi-k3"})

    def test_flag_details(self):
        args = chorus.parse_args(["--upstream", "glm=upstream-a", "--upstream",
                                  "glm=upstream-b, Upstream-Z", "--upstream", "glm=upstream-a"])
        self.assertEqual(args.upstreams, {"glm": ["upstream-a", "upstream-b", "upstream-z"]})
        args = chorus.parse_args(["--sensitivity", "public", "--timeout", "300"])
        voices = {n: dict(v) for n, v in chorus.VOICES.items()}
        chorus.voice_settings(args, voices, {})
        self.assertEqual(voices["gemini"]["first_data"], 300)  # Gemini keeps its wall
        self.assertEqual(voices["glm"]["first_data"], chorus.FIRST_DATA_SECONDS)
        for bad in (["--first-data", "soon"], ["--first-data", "-3"]):
            code, _, _ = self.run_cli("--sensitivity", "public", "--file", self.draft(), *bad)
            self.assertEqual(code, 1, bad)

    def test_a_broken_stderr_never_breaks_a_call(self):
        class Broken(io.StringIO):
            def __init__(self, exc):
                super().__init__()
                self.exc = exc

            def write(self, s):
                if "idle clock" in s:
                    raise self.exc
                return super().write(s)

        self.server.state["script"] = {"z-ai/glm-5.3": [
            ("role",), ("reason", "reasoning", "a"), ("wait", 0.6), ("reason", "reasoning", "b"),
            ("content",), ("done",)]}
        for exc in (BrokenPipeError(), ValueError("I/O operation on closed file")):
            out = io.StringIO()
            with mock.patch.object(chorus, "IDLE_SECONDS", 0.3), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(Broken(exc)):
                code = chorus.main(["--sensitivity", "public", "--file", self.draft(), "--voices",
                                    "glm", "--no-cross-examine", "--yes", "--json"])
            glm = json.loads(out.getvalue())["results"]["glm"]
            self.assertEqual(code, 0, type(exc).__name__)
            self.assertNotIn("error", glm)
            self.assertEqual(glm["stream"]["idle_over"], 1)

    def test_a_crashed_call_shows_no_effort(self):
        ct = chorus.cost_time({"glm": {"voice": "glm", "model": "z-ai/glm-5.3", "sent": True,
                                       "error": "crashed after sending"}}, {}, {"1": 1}, 1)
        self.assertEqual(ct["calls"][0]["effort"], "?")
        self.assertIn("| glm (failed) | — | — |", "\n".join(chorus.cost_lines(ct)))

    def test_notes_say_only_what_is_true(self):
        ct = {"seconds": 1, "rounds": {"1": 1}, "cost_usd": 0.0, "unpriced": ["kimi (round 1)"],
              "calls": [{"round": "1", "voice": "kimi", "model": "moonshotai/kimi-k3",
                         "upstream": None, "effort": "?", "seconds": 1, "first_token": None,
                         "input": None, "output": None, "reasoning": None, "cost": None,
                         "cost_from": None, "failed": True}]}
        text = "\n".join(chorus.cost_lines(ct))
        self.assertNotIn("reasoning-token count for Kimi", text)  # no output count to speak of
        self.assertIn("| kimi (failed) | — | — |", text)  # effort unknown, not "none sent"
        self.server.state["catalog"][1]["supported_parameters"] = ["max_tokens"]
        code, _, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                    "--voices", "glm", "--yes", "--no-cross-examine")
        self.assertEqual(code, 0)
        self.assertIn("lists no reasoning_effort for z-ai/glm-5.3, so glm gets none", err)


class HopsNamedBySessionTest(TeeCase):
    """Live receipts carry the serving session id but no upstream name: a hop is named by its
    session, and a confidential run pins each TEE voice to its approved upstreams."""

    def test_a_hop_is_named_by_its_session_id(self):
        code, out, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                      "--yes", "--json", "--no-cross-examine")
        self.assertEqual(code, 0, err)
        res = json.loads(out)["results"]
        self.assertEqual(res["glm"]["upstream"], "upstream-z")  # unpinned: via the list
        self.assertEqual(res["kimi"]["upstream"], "upstream-c")
        self.assertNotIn("pinned", res["glm"])  # internal stays unpinned
        self.server.state["sessions_status"] = 500  # no list: unnamed, never a failure here
        code, out, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                      "--yes", "--json", "--no-cross-examine")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["results"]["glm"]["upstream"], "unnamed")
        self.server.state.pop("sessions_status")
        self.server.state["receipt"] = {"event_log": [
            {"type": "upstream.verified", "result": "verified", "required": True,
             "session_id": Z_HOP[0]}]}  # no name: a session listed on the unapproved upstream
        code, out, _ = self.run_cli("--check-receipt", "rcpt-x", "--voices", "glm",
                                    "--sensitivity", "confidential")
        self.assertEqual(code, 5)
        self.assertIn("served by upstream-z", out)

    def test_confidential_kimi_is_pinned_off_an_unapproved_upstream_or_refused(self):
        st = self.server.state
        st["kimi_sessions"] = [{"session_id": "4" * 64, "upstream_name": "upstream-z"},
                               {"session_id": "5" * 64, "upstream_name": "upstream-c"}]
        code, out, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                      "--yes", "--json", "--no-cross-examine")
        self.assertEqual(code, 0, err)
        kimi = json.loads(out)["results"]["kimi"]
        self.assertEqual(kimi["pinned"], [["5" * 64, "upstream-c"]])  # never the unapproved one
        body = [b for _, _, b, _ in self.posts("/chat/completions") if "kimi" in b["model"]][-1]
        self.assertEqual(body["provider"]["aci_session_ids"], ["5" * 64])
        st["kimi_sessions"] = [{"session_id": "4" * 64, "upstream_name": "upstream-z"}]
        n = self.kimi_posts()
        code, out, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                      "--yes", "--json", "--no-cross-examine")
        self.assertEqual(code, 6, err)  # GLM ran; Kimi was refused before sending
        self.assertIn("no live session for moonshotai/kimi-k3 on upstream-c",
                      json.loads(out)["results"]["kimi"]["error"])
        self.assertEqual(self.kimi_posts(), n)
        code, _, _ = self.run_cli("--sensitivity", "public", "--file", self.draft(), "--voices",
                                  "kimi", "--yes", "--no-cross-examine")
        self.assertEqual(code, 0)  # public Kimi stays unpinned
        self.assertNotIn("aci_session_ids", self.posts("/chat/completions")[-1][2]["provider"])


class PerVoiceApprovalTest(TeeCase):
    """Per-voice approved upstreams on confidential runs, with every hop named by its session."""

    def test_a_confidential_recheck_fails_closed_when_a_hop_cannot_be_named(self):
        st = self.server.state
        st["kimi_sessions"] = [{"session_id": "9" * 64, "upstream_name": "upstream-z"}]
        st["receipt"] = {"event_log": [{"type": "upstream.verified", "result": "verified",
                                        "required": True, "session_id": "9" * 64}]}
        code, out, _ = self.run_cli("--check-receipt", "rcpt-x", "--voices", "kimi",
                                    "--sensitivity", "confidential")
        self.assertEqual(code, 5)  # listed as unapproved: the hop is named by its session
        self.assertIn("served by upstream-z: " + KIMI_RULE, out)
        for state in ({"kimi_sessions": [dict(KIMI_SESSIONS[0])]}, {"sessions_status": 500}):
            st.update(state)  # rotated off the list, or the list unreadable
            code, out, _ = self.run_cli("--check-receipt", "rcpt-x", "--voices", "kimi",
                                        "--sensitivity", "confidential")
            self.assertEqual(code, 5, state)
            self.assertIn("is in neither the call's pins nor the gateway's session list now", out)
            st.pop("sessions_status", None)

    def test_a_pin_that_goes_away_before_the_send_sends_nothing(self):
        for later in (500, [{"session_id": "4" * 64, "upstream_name": "upstream-z"}]):
            self.server.state["sessions_after_first"] = {"moonshotai/kimi-k3": later}
            self.server.state["session_reads"] = {}
            n = self.kimi_posts()
            code, out, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                          "--yes", "--json", "--no-cross-examine")
            kimi = json.loads(out)["results"]["kimi"]
            self.assertFalse(kimi["sent"], later)
            self.assertTrue(kimi["error"].endswith("nothing sent"), kimi["error"])
            self.assertNotEqual(code, 0)
            self.assertEqual(self.kimi_posts(), n)  # never unpinned, never unapproved

    def test_confidential_kimi_is_held_to_its_approved_upstream_whatever_the_labels(self):
        for label in ("upstream-c-relay", "managed-upstream-c", "upstream-cc", "upstream_c",
                      "upstream c", "upstreamc", "upstream-z", "", "  ", None):
            self.server.state["kimi_sessions"] = [{"session_id": "6" * 64, "upstream_name": label}]
            n = self.kimi_posts()
            code, out, _ = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                        "--yes", "--json", "--no-cross-examine")
            self.assertIn("no live session for moonshotai/kimi-k3 on upstream-c",
                          json.loads(out)["results"]["kimi"]["error"], label)
            self.assertEqual(self.kimi_posts(), n, label)

    def test_a_receipt_name_never_outranks_the_session(self):
        st = self.server.state
        st["kimi_sessions"] = [{"session_id": "4" * 64, "upstream_name": "upstream-z"}]
        st["receipt"] = {"event_log": [{"type": "upstream.verified", "result": "verified",
                                        "required": True, "session_id": "4" * 64,
                                        "upstream_name": "upstream-c"}]}
        code, out, _ = self.run_cli("--check-receipt", "rcpt-x", "--voices", "kimi",
                                    "--sensitivity", "confidential")
        self.assertEqual(code, 5)
        self.assertIn("served by upstream-z", out)  # the session's name, not the receipt's
        self.assertIn("the receipt names upstream-c for a session the gateway lists as upstream-z",
                      out)
        code, out, _ = self.run_cli("--check-receipt", "rcpt-x", "--voices", "kimi",
                                    "--sensitivity", "internal")
        self.assertEqual(code, 0)  # outside a confidential run the list does not apply
        self.assertIn("re-check with --sensitivity confidential", out)
        st["kimi_sessions"] = [dict(KIMI_SESSIONS[0])]  # the receipt names upstream-z for an
        st["receipt"]["event_log"][0].update(session_id=KIMI_PINS[0],  # upstream-c session
                                             upstream_name="upstream-z")
        code, out, _ = self.run_cli("--check-receipt", "rcpt-x", "--voices", "kimi",
                                    "--sensitivity", "confidential")
        self.assertEqual(code, 5)
        self.assertIn("the receipt names upstream-z for a session the gateway lists as upstream-c",
                      out)

    def test_confidential_needs_an_approved_upstream_per_voice(self):
        allow = chorus.upstream_allowlist(APPROVED)
        self.assertEqual(allow, {"glm": ("upstream-a", "upstream-b"), "kimi": ("upstream-c",)})
        self.assertIsNone(chorus.upstream_allowlist("glm=upstream-a").get("kimi"))
        voices = {n: dict(v) for n, v in chorus.VOICES.items()}
        chorus.voice_settings(chorus.parse_args(["--sensitivity", "confidential"]), voices, allow)
        self.assertEqual(voices["glm"]["approved"], ("upstream-a", "upstream-b"))
        self.assertEqual(voices["kimi"]["approved"], ("upstream-c",))
        self.assertNotIn("approved", voices["openai"])
        n = len(self.server.state["requests"])
        for argv in (["--upstream", "kimi=upstream-a"], ["--upstream", "glm=upstream-c"]):
            code, _, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                        "--yes", *argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("a confidential run keeps", err)
        with mock.patch.dict(os.environ, {"CHORUS_CONFIDENTIAL_UPSTREAMS": "glm=upstream-a"}):
            code, _, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                        "--voices", "kimi", "--yes")
        self.assertEqual(code, 2)  # a named voice with no approved upstream is refused
        self.assertIn("voice kimi has no approved upstream for confidential runs", err)
        self.assertEqual(len(self.server.state["requests"]), n)


def verified_event(**k):
    """One attested upstream.verified event on Kimi's first approved session; k overrides."""
    return dict({"type": "upstream.verified", "result": "verified", "required": True,
                 "session_id": KIMI_PINS[0], "upstream_name": "upstream-c"}, **k)


def hop(event):
    """One forwarded hop of a receipt's event log, attested by `event`."""
    return [{"type": "route.selected"}, event, {"type": "request.forwarded"}]


class CheckReceiptSensitivityTest(TeeCase):
    """--check-receipt needs a declared sensitivity: --sensitivity, or a use case that runs only
    at confidential and so implies it."""

    def setUp(self):
        super().setUp()
        self.server.state["receipt"] = {"event_log": [  # attested, on GLM's unapproved upstream
            {"type": "upstream.verified", "result": "verified", "required": True,
             "session_id": Z_HOP[0], "upstream_name": "upstream-z"}]}

    def recheck(self, *argv):
        return self.run_cli("--check-receipt", "rcpt-x", *argv)

    def test_no_sensitivity_and_no_use_case_stops_before_any_request(self):
        stop = "STOP: declare the run's sensitivity with --sensitivity (%s)" % ", ".join(
            chorus.SENSITIVITY)
        for argv in (["--voices", "glm"], ["--voices", "kimi"], []):  # [] is the default, kimi
            code, out, err = self.recheck(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn(stop, err)
            self.assertEqual(out, "", argv)
        self.assertEqual(self.server.state["requests"], [])  # no receipt was even fetched

    def test_an_unknown_use_case_exits_1_naming_the_known_ones(self):
        known = ", ".join(sorted(self.use_cases()))
        for extra in ([], ["--sensitivity", "confidential"]):
            code, out, err = self.recheck("--voices", "glm", "--use-case", "contract", *extra)
            self.assertEqual(code, 1, extra)
            self.assertIn("unknown use case 'contract' (known: %s;" % known, err)
            self.assertIn("contracts", known)
            self.assertNotIn("STOP: declare", err)
            self.assertEqual(out, "", extra)
        self.assertEqual(self.server.state["requests"], [])

    def test_a_use_case_given_a_sensitivity_it_does_not_take_exits_1(self):
        grant = " or ".join(self.use_cases()["grant"]["classes"])
        self.assertNotIn("public", grant)
        for uc, cls, takes in (("contracts", "internal", "confidential"),
                               ("contracts", "public", "confidential"),
                               ("startup", "internal", "confidential"),
                               ("grant", "public", grant)):
            code, out, err = self.recheck("--voices", "glm", "--use-case", uc, "--sensitivity", cls)
            self.assertEqual(code, 1, (uc, cls))
            self.assertIn("--use-case %s takes sensitivity %s, not %s" % (uc, takes, cls), err)
            self.assertEqual(out, "", (uc, cls))
        code, out, err = self.recheck("--voices", "glm", "--use-case", "grant")
        self.assertEqual(code, 1)  # a use case open to several levels implies none of them
        self.assertIn("STOP: declare the run's sensitivity", err)
        self.assertEqual(self.server.state["requests"], [])

    def test_a_confidential_only_use_case_implies_confidential(self):
        for uc in ("contracts", "startup"):
            code, out, err = self.recheck("--voices", "glm", "--use-case", uc)
            self.assertEqual(code, 5, (uc, out, err))  # held to the approved upstreams
            self.assertTrue(out.startswith("failed:"), out)
            self.assertIn("served by upstream-z: " + GLM_RULE, out)
            self.assertNotIn("re-check with --sensitivity confidential", out)
            declared = self.recheck("--voices", "glm", "--use-case", uc,
                                    "--sensitivity", "confidential")
            self.assertEqual(declared[:2], (code, out), uc)  # implied reads as declared
        code, out, _ = self.recheck("--voices", "glm", "--sensitivity", "internal")
        self.assertEqual(code, 0)  # the same receipt, outside a confidential run
        self.assertIn("re-check with --sensitivity confidential", out)
        self.server.state["receipt"] = {"event_log": [  # the default voice, on an unlisted session
            verified_event(session_id="4" * 64, upstream_name="upstream-z")]}
        code, out, _ = self.recheck("--use-case", "contracts")
        self.assertEqual(code, 5, out)
        self.assertIn("served by upstream-z: " + KIMI_RULE, out)
        self.assertEqual(self.reqs("POST"), [])  # a re-check sends no content

    def test_a_confidential_recheck_of_a_voice_with_no_approved_list_fails(self):
        self.server.state["receipt"] = {"event_log": [
            {"type": "upstream.verified", "result": "verified", "required": True,
             "session_id": A_HOP[0], "upstream_name": "upstream-a"}]}
        code, out, _ = self.recheck("--voices", "glm", "--sensitivity", "confidential")
        self.assertEqual(code, 0, out)  # upstream-a is approved for glm in the default list
        none_set = ("served by upstream-a: a confidential run keeps z-ai/glm-5.3 on an approved "
                    "upstream, and none is set (CHORUS_CONFIDENTIAL_UPSTREAMS)")
        with mock.patch.dict(os.environ, {"CHORUS_CONFIDENTIAL_UPSTREAMS": "kimi=upstream-c"}):
            for argv in (["--sensitivity", "confidential"], ["--use-case", "contracts"]):
                code, out, _ = self.recheck("--voices", "glm", *argv)
                self.assertEqual(code, 5, argv)
                self.assertTrue(out.startswith("failed:"), out)
                self.assertIn(none_set, out)
            code, out, _ = self.recheck("--voices", "glm", "--sensitivity", "internal")
            self.assertEqual(code, 0, out)  # no list applies outside confidential
            self.assertNotIn("none is set", out)
            self.server.state["receipt"] = {"event_log": [verified_event()]}
            code, out, _ = self.recheck("--voices", "kimi", "--sensitivity", "confidential")
            self.assertEqual(code, 0, out)  # kimi keeps its own approved list
        self.assertEqual(self.reqs("POST"), [])


class ReceiptShapeTest(TeeCase):
    """Receipt event shapes that must never pass as attested."""

    def kimi_run(self, cls):
        code, out, err = self.run_cli("--sensitivity", cls, "--file", self.draft(), "--voices",
                                      "kimi", "--yes", "--json", "--no-cross-examine")
        return code, json.loads(out)["results"]["kimi"], err

    def test_required_must_be_exactly_true(self):
        v = dict(chorus.VOICES["kimi"], name="kimi")
        missing = verified_event()
        del missing["required"]
        for label, ev in (("required missing", missing),
                          ("required=None", verified_event(required=None)),
                          ("required='true'", verified_event(required="true")),
                          ("required=1", verified_event(required=1))):
            why = "upstream.verified required=%s" % (ev.get("required"),)
            for log in ([ev], hop(ev) + hop(verified_event())):  # alone, or before a good hop
                status, detail = chorus.verify_receipt(v, "k", None, "rcpt-x",
                                                       rec={"event_log": log})
                self.assertEqual((status, detail), ("failed", "receipt rcpt-x: " + why), label)
            self.server.state["receipt"] = {"event_log": [ev]}
            for cls in ("confidential", "internal"):  # "not confidential" is 5 at every level
                code, kimi, err = self.kimi_run(cls)
                self.assertEqual(code, 5, (label, cls))
                self.assertTrue(kimi["sent"], label)
                self.assertEqual(kimi["receipt"], "failed", (label, cls))
                self.assertIn(why, kimi["receipt_detail"], label)
                self.assertIn("CONFIDENTIALITY NOT PROVEN for kimi", err)
            code, out, _ = self.run_cli("--check-receipt", "rcpt-x", "--voices", "kimi",
                                        "--sensitivity", "confidential")
            self.assertEqual(code, 5, label)
            self.assertTrue(out.startswith("failed: receipt rcpt-x: " + why), out)

    def test_a_forwarded_hop_attested_without_a_session_fails_before_a_good_hop(self):
        v = dict(chorus.VOICES["kimi"], name="kimi")
        no_key = verified_event()
        del no_key["session_id"]
        why = ("the request was forwarded to hop 1, which has no verified upstream.verified "
               "event")
        for label, bad in (("empty", verified_event(session_id="")),
                           ("None", verified_event(session_id=None)), ("missing", no_key)):
            receipt = {"event_log": hop(bad) + hop(verified_event())}
            n = len(self.server.state["requests"])
            self.assertEqual(chorus.verify_receipt(v, "k", None, "rcpt-x", rec=receipt),
                             ("failed", "receipt rcpt-x: " + why), label)
            self.assertEqual(len(self.server.state["requests"]), n)  # a given receipt: no fetch
            self.server.state["receipt"] = receipt
            code, kimi, err = self.kimi_run("internal")
            self.assertEqual(code, 5, label)  # unpinned, and still held to every hop
            self.assertEqual(kimi["receipt"], "failed", label)
            self.assertIn(why, kimi["receipt_detail"], label)
            self.assertIn("CONFIDENTIALITY NOT PROVEN for kimi", err)
        good = {"event_log": hop(verified_event(session_id=KIMI_PINS[1])) + hop(verified_event())}
        self.assertEqual(chorus.verify_receipt(v, "k", None, "rcpt-x", rec=good)[0], "verified")
        self.server.state["receipt"] = good
        code, kimi, err = self.kimi_run("internal")
        self.assertEqual(code, 0, err)  # the same failover, each hop with a session, passes
        self.assertEqual(kimi["upstreams"], ["upstream-c", "upstream-c"])

    def test_a_blank_session_id_is_no_session(self):
        v = dict(chorus.VOICES["kimi"], name="kimi")
        for sid in (" ", "\t", " \n "):
            with self.subTest(session_id=sid):
                alone = {"event_log": [verified_event(session_id=sid)]}
                first = {"event_log": hop(verified_event(session_id=sid)) + hop(verified_event())}
                for receipt in (alone, first):  # as an empty id is: never a session
                    status, detail = chorus.verify_receipt(v, "k", None, "rcpt-x", rec=receipt)
                    self.assertEqual(status, "failed", detail)
                self.server.state["receipt"] = first
                code, kimi, _ = self.kimi_run("internal")
                self.assertEqual(code, 5, kimi.get("receipt_detail"))
                self.assertEqual(kimi["receipt"], "failed")


class TeePreflightTest(TeeCase):
    """The catalog preflight accepts is_tee as True or the string "true" (any case), and
    refuses every other value before anything is sent."""

    ACCEPTED = (True, "true", "True", "TRUE", "tRuE")
    REFUSED = (False, "false", "False", 0, "0", None, "no", "", 1, "1", "yes", "TRUE ", " true",
               "true\n", 1.0, [True], ["true"], {"tee": True})

    def test_the_preflight_contract(self):
        v = dict(chorus.VOICES["glm"], name="glm")
        for i, flag in enumerate(self.ACCEPTED + self.REFUSED):
            entry = {"id": v["model"], "is_tee": flag}
            cache = {v["base"]: {"data": [entry]}}  # the catalog, already read
            ok, detail, got = chorus.tee_preflight(v, "k", cache)
            self.assertEqual(ok, i < len(self.ACCEPTED), repr(flag))  # 1 == True, yet refused
            if ok:
                self.assertEqual((detail, got), ("is_tee=true", entry), repr(flag))
            else:
                self.assertEqual(detail, "%s is listed with is_tee=%s" % (v["model"], flag))
                self.assertIsNone(got)
        self.assertEqual(self.server.state["requests"], [])

    def test_a_flag_that_is_not_true_refuses_before_any_send(self):
        cat = self.server.state["catalog"]
        for flag in ("false", "False", 0, "0", None, "no", "", 1, "1", "yes", "TRUE "):
            cat[1]["is_tee"] = flag  # glm
            code, _, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                        "--voices", "glm", "--yes")
            self.assertEqual(code, 2, repr(flag))
            self.assertIn("REFUSED glm: cannot prove TEE serving (z-ai/glm-5.3 is listed with "
                          "is_tee=%s); nothing sent to it" % (flag,), err)
            self.assertIn("no voice proved TEE serving; nothing sent", err)
        cat[1]["is_tee"] = True
        cat[1]["providers"] = [{"is_tee": True}, {"is_tee": "1"}]  # every listed flag counts
        code, _, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                    "--voices", "glm", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("is listed with is_tee=1)", err)
        self.assertEqual(self.posts("/chat/completions"), [])
        self.assertEqual(self.reqs("GET", "/aci/sessions?"), [])  # refused before its pins
        cat[1].pop("providers")
        for flag in ("true", "True", "TRUE"):
            cat[1]["is_tee"] = flag
            code, _, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                        "--voices", "glm", "--yes", "--no-cross-examine")
            self.assertEqual(code, 0, (flag, err))
        self.assertEqual(len(self.posts("/chat/completions")), 3)


if __name__ == "__main__":
    unittest.main(verbosity=1)


class NestedIsTeeTest(ChorusCase):
    """The preflight reads the model's own top-level is_tee: a flag nested under some other key
    (a price table, one provider row) is not the model being listed as TEE-served."""

    def test_a_nested_is_tee_alone_is_refused(self):
        v = dict(chorus.VOICES["glm"], name="glm")
        for entry in ({"id": v["model"], "pricing": {"is_tee": True}},
                      {"id": v["model"], "providers": [{"name": "upstream-a", "is_tee": True}]}):
            with self.subTest(entry=entry):
                ok, detail, _ = chorus.tee_preflight(v, "k", {v["base"]: {"data": [entry]}})
                self.assertFalse(ok)
                self.assertIn("no is_tee field at its top level", detail)

    def test_a_top_level_flag_with_a_false_nested_one_is_still_refused(self):
        v = dict(chorus.VOICES["glm"], name="glm")
        entry = {"id": v["model"], "is_tee": True,
                 "providers": [{"name": "upstream-a", "is_tee": True},
                               {"name": "upstream-z", "is_tee": False}]}
        ok, detail, _ = chorus.tee_preflight(v, "k", {v["base"]: {"data": [entry]}})
        self.assertFalse(ok)
        self.assertIn("is_tee=False", detail)

    def test_nested_only_flag_end_to_end_sends_nothing(self):
        cat = self.server.state["catalog"]
        flag = cat[1].pop("is_tee")
        cat[1]["pricing"] = {"is_tee": flag}
        code, _, err = self.run_cli("--sensitivity", "confidential", "--voices", "glm",
                                    "--file", self.draft(), "--yes")
        self.assertEqual(code, 2, err)
        self.assertEqual(self.posts("/chat/completions"), [])
