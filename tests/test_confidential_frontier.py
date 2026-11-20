"""CHORUS_CONFIDENTIAL_FRONTIER: frontier voices the operator approves for confidential runs.

A listed voice reviews confidential content outside any enclave. The plan, the STOP line and
the report name it, and the TEE voices in the same run keep every check."""
import json
import os

from harness import ChorusCase, chorus

KEY = "test-anthropic-key"
MARK = "[not a TEE: approved for confidential in CHORUS_CONFIDENTIAL_FRONTIER]"


class FrontierCase(ChorusCase):
    def setUp(self):
        super().setUp()
        os.environ["ANTHROPIC_API_KEY"] = KEY
        os.environ["CHORUS_CONFIDENTIAL_FRONTIER"] = "claude"

    def confidential(self, *argv):
        code, out, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                      *argv)
        return code, (json.loads(out) if out.strip().startswith("{") else out), err


class ParseTests(FrontierCase):
    def test_none_is_approved_by_default(self):
        self.assertEqual(chorus.frontier_allowlist(None), ())
        self.assertEqual(chorus.frontier_allowlist(""), ())
        os.environ.pop("CHORUS_CONFIDENTIAL_FRONTIER")
        code, res, err = self.confidential("--yes", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(res["results"]), ["glm", "kimi"])  # Claude is not asked
        self.assertEqual(self.reqs(suffix="/claude/"), [])
        self.assertNotIn("confidential_frontier", res["plan"])

    def test_names_split_on_commas_semicolons_and_spaces(self):
        self.assertEqual(chorus.frontier_allowlist(" claude, openai;claude  gemini "),
                         ("claude", "openai", "gemini"))

    def test_a_tee_voice_or_an_unknown_name_exits_1_and_sends_nothing(self):
        for spec, words in (("kimi", "'kimi' is a TEE voice; approve its upstreams in "
                                     "CHORUS_CONFIDENTIAL_UPSTREAMS instead"),
                            ("claude,glm", "'glm' is a TEE voice"),
                            ("gpt", "unknown voice 'gpt' (frontier voices: openai, gemini, "
                                    "claude)")):
            os.environ["CHORUS_CONFIDENTIAL_FRONTIER"] = spec
            for cls in ("public", "confidential"):  # read on every run, like the upstream list
                code, _, err = self.run_cli("--sensitivity", cls, "--file", self.draft(),
                                            "--dry-run")
                self.assertEqual(code, 1, (spec, cls))
                self.assertIn("CHORUS_CONFIDENTIAL_FRONTIER: " + words, err)
        self.assertEqual(self.server.state["requests"], [])


class RoutingTests(FrontierCase):
    def test_a_listed_voice_joins_a_confidential_run_outside_any_enclave(self):
        code, res, err = self.confidential("--yes", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(res["results"]), ["claude", "glm", "kimi"])  # OpenAI is not listed
        self.assertEqual(res["plan"]["confidential_frontier"], ["claude"])
        self.assertIn("\n  claude  claude-opus-5-5 @ ", err)
        self.assertIn(MARK, err)
        self.assertEqual(err.count(MARK), 1)  # the TEE rows keep their own mark
        self.assertIn("except claude, approved in CHORUS_CONFIDENTIAL_FRONTIER, outside any "
                      "enclave", err)
        self.assertIn("outside any enclave: **claude** (approved for confidential in "
                      "CHORUS_CONFIDENTIAL_FRONTIER: no TEE, no receipt)", res["report"])
        self.assertTrue(self.round1("/claude/v1/messages"))
        self.assertNotIn("receipt", res["results"]["claude"])
        self.assertEqual(self.reqs(suffix="/openai/"), [])

    def test_the_tee_voices_in_the_same_run_keep_every_check(self):
        code, res, err = self.confidential("--yes", "--json")
        self.assertEqual(code, 0, err)
        tee = [b for _, _, b, _ in self.round1("/chat/completions")]
        self.assertEqual(sorted(b["model"].split("/")[0] for b in tee), ["moonshotai", "z-ai"])
        for body in tee:
            self.assertIs(body["provider"]["aci_verified"], True)
            self.assertIs(body["provider"]["zdr"], True)
            self.assertTrue(body["provider"]["aci_session_ids"])  # pinned to approved upstreams
        for n in ("kimi", "glm"):
            self.assertEqual(res["results"][n]["receipt"], "verified", n)
        self.server.state["requests"] = []
        self.server.state["receipt_status"] = 404  # a receipt that cannot be found
        code, _, err = self.confidential("--yes", "--json")
        self.assertEqual(code, 5, err)  # still a stop at confidential, with Claude in the run

    def test_round_2_examines_with_the_listed_voice_too(self):
        code, res, err = self.confidential("--yes", "--json")
        self.assertEqual(code, 0, err)
        exams = [b for _, _, b, _ in self.posts("/claude/v1/messages")
                 if "FINDINGS TO EXAMINE" in json.dumps(b)]
        self.assertTrue(exams)  # Claude cross-examines the TEE voices' single-family findings

    def test_an_unlisted_frontier_voice_is_still_refused(self):
        os.environ["GEMINI_API_KEY"] = "test-gemini-key"
        for voices in ("openai", "openai,kimi", "gemini"):
            code, _, err = self.confidential("--voices", voices, "--yes")
            self.assertEqual(code, 2, voices)
            self.assertIn("sensitivity 'confidential' allows attested-TEE voices only, and the "
                          "frontier voices in CHORUS_CONFIDENTIAL_FRONTIER (claude)", err)
        self.assertEqual(self.server.state["requests"], [])

    def test_a_listed_opt_in_voice_still_runs_only_when_named(self):
        os.environ["GEMINI_API_KEY"] = "test-gemini-key"
        os.environ["CHORUS_CONFIDENTIAL_FRONTIER"] = "gemini,claude"
        code, _, err = self.confidential("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertNotIn("\n  gemini ", err)  # Gemini stays opt-in
        code, _, err = self.confidential("--voices", "gemini,kimi", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertRegex(err, r"\n  gemini  \S+ @ \S+  \[not a TEE: approved for confidential")
        self.assertIn("except gemini, approved in CHORUS_CONFIDENTIAL_FRONTIER", err)
        self.assertEqual(self.server.state["requests"], [])

    def test_the_stop_line_names_it_before_anything_is_sent(self):
        code, _, err = self.confidential()
        self.assertEqual(code, 2)
        stop = err[err.index("STOP: this sends"):]
        self.assertIn("the content's sensitivity is 'confidential' (must stay inside an attested "
                      "enclave: TEE voices only, on approved upstreams; except claude, approved "
                      "in CHORUS_CONFIDENTIAL_FRONTIER, outside any enclave)", " ".join(
                          stop.split()))
        self.assertEqual(self.server.state["requests"], [])

    def test_public_and_internal_runs_are_unchanged(self):
        for cls in ("public", "internal"):
            self.server.state["requests"] = []
            code, out, err = self.run_cli("--sensitivity", cls, "--file", self.draft(), "--yes",
                                          "--json")
            self.assertEqual(code, 0, err)
            res = json.loads(out)
            self.assertEqual(sorted(res["results"]), ["claude", "glm", "kimi", "openai"], cls)
            self.assertNotIn(MARK, err)
            self.assertNotIn("confidential_frontier", res["plan"])
            self.assertNotIn("outside any enclave", res["report"])

    def test_a_use_case_that_implies_confidential_says_so(self):
        code, _, err = self.run_cli("--use-case", "contracts", "--redacted", "--file",
                                    self.draft(), "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("note: --use-case contracts implies --sensitivity confidential (TEE voices, "
                      "plus claude outside any enclave (CHORUS_CONFIDENTIAL_FRONTIER))", err)
        self.assertIn(MARK, err)
        self.assertRegex(err, r"hats: [^\n]*claude")  # the hats are dealt over Claude too
        os.environ.pop("CHORUS_CONFIDENTIAL_FRONTIER")
        code, _, err = self.run_cli("--use-case", "contracts", "--redacted", "--file",
                                    self.draft(), "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("implies --sensitivity confidential (TEE voices only)", err)
        self.assertNotIn(MARK, err)
