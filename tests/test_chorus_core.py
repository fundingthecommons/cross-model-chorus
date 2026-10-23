"""The chorus core: round-1 runs, parsing, failures, prompts, keys, dry run, quotes and agreement, and transport."""
import copy
import json
import os
import re
import shlex
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from harness import DRAFT, GLM_FINDINGS, ChorusCase, chorus, default_chat

# The voices as the runner ships them, read at import, before any test points them at the mock.
REAL_VOICES = copy.deepcopy(chorus.VOICES)


class ChorusRunTest(ChorusCase):
    """A run through main(): every voice, the report, partial and failed choruses."""

    def test_internal_chorus_runs_every_voice_and_reports_agreement(self):
        code, out, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(), "--yes",
                                      "--json")
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertEqual(sorted(res["results"]), ["glm", "kimi", "openai"])
        self.assertEqual(res["plan"]["briefs"], ["refute", "tells", "redteam"])  # no criteria, no fit
        groups = [sorted({f["voice"] for f in g}) for g in res["agreements"]]
        self.assertIn(["kimi", "openai"], groups)
        (_, _, body, _), = self.round1("/responses")
        self.assertEqual(body["model"], "gpt-6.1-sol")
        self.assertIs(body["store"], False)
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertEqual(body["reasoning"]["effort"], "high")
        self.assertNotIn("max_tokens", body)
        chat = {b["model"]: b for _, _, b, _ in self.round1("/chat/completions")}
        for model, b in chat.items():  # attestation demanded at every sensitivity
            self.assertEqual(b["provider"], {"aci_verified": True}, model)
            self.assertEqual(b["response_format"]["type"], "json_schema")
            self.assertIn("max_tokens", b)
        self.assertEqual(chat["moonshotai/kimi-k3"]["reasoning_effort"], "high")
        self.assertEqual(chat["z-ai/glm-5.3"]["reasoning_effort"], "high")  # never at max by default
        for b in chat.values():  # a top-level effort only
            self.assertNotIn("chat_template_kwargs", b)
            self.assertNotIn("thinking", b)
        self.assertEqual(len(self.reqs("GET", "/models")), 1)  # one cached catalog fetch

    def test_markdown_report(self):
        code, out, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(), "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("## Same passage flagged by several families", out)
        self.assertIn("kimi + openai", out)
        self.assertIn('"we have 10,000 members" ⚠ quote not found in draft', out)
        self.assertIn("TEE receipt verified", out)
        self.assertIn("→ cite the ledger", out)

    def test_think_block_and_code_fence_are_stripped(self):
        code, out, _ = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                    "--voices", "kimi", "--yes", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["results"]["kimi"]["findings"][0]["brief"], "tells")

    def test_parse_findings_handles_unopened_think_and_stray_braces(self):
        good = json.dumps(GLM_FINDINGS)
        for text in ["Plan: output {findings, pass}.\n</think>\n" + good,
                     good + "\n\nNote: I skipped the {budget} table.",
                     "Use {curly} style.\n" + good,
                     "```json\n" + good + "\n```\nSee {x}."]:
            self.assertIsNotNone(chorus.parse_findings(text), text[:40])
        self.assertIsNone(chorus.parse_findings("no json here {at all"))

    def test_unparseable_response_is_shown_raw(self):
        self.server.state["chat"] = lambda body: "I think the draft is fine overall."
        code, out, _ = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                    "--voices", "glm", "--yes", "--json")
        self.assertEqual(code, 4)
        self.assertIn("fine overall", json.loads(out)["results"]["glm"]["raw"])
        self.server.state["chat"] = lambda body: (
            "I think the draft is fine overall." if "glm" in body["model"] else default_chat(body))
        code, _, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                    "--voices", "glm,kimi", "--yes")
        self.assertEqual(code, 6)
        self.assertIn("came back unstructured", err)

    def test_one_voice_failing_is_a_partial_chorus(self):
        self.server.state["openai_status"] = 400
        code, out, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(), "--yes",
                                      "--json")
        self.assertEqual(code, 6)
        res = json.loads(out)["results"]
        self.assertIn("HTTP 400: bad model", res["openai"]["error"])
        self.assertIn("check --list-models", res["openai"]["error"])
        self.assertIn("findings", res["kimi"])
        self.assertIn("partial chorus: openai", err)

    def test_every_voice_failing_exits_4(self):
        self.server.state["openai_status"] = 401
        code, out, _ = self.run_cli("--sensitivity", "public", "--file", self.draft(),
                                    "--voices", "openai", "--yes")
        self.assertEqual(code, 4)
        self.assertIn("the key was rejected", out)

    def test_empty_responses_exit_4(self):
        self.server.state["chat"] = lambda body: ""
        code, _, _ = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                  "--voices", "kimi,glm", "--yes")
        self.assertEqual(code, 4)

    def test_incomplete_response_is_reported_even_when_partial_text(self):
        self.server.state.update(openai_state="incomplete",
                                 openai_incomplete={"reason": "max_output_tokens"},
                                 openai_body={"id": "r", "status": "incomplete",
                                              "incomplete_details": {"reason": "max_output_tokens"},
                                              "output": [{"type": "message", "content": [
                                                  {"type": "output_text",
                                                   "text": '{"findings": [{"brief": "te'}]}]})
        code, out, _ = self.run_cli("--sensitivity", "public", "--file", self.draft(),
                                    "--voices", "openai", "--yes")
        self.assertEqual(code, 4)  # a cut-off fragment is shown but is not a review
        self.assertIn("response incomplete (max_output_tokens)", out)

    def test_openai_refusal_and_failed_status_are_surfaced(self):
        self.server.state["openai_body"] = {"id": "r", "status": "completed", "output": [
            {"type": "message", "content": [{"type": "refusal", "refusal": "I can't help"}]}]}
        code, out, _ = self.run_cli("--sensitivity", "public", "--file", self.draft(),
                                    "--voices", "openai", "--yes")
        self.assertEqual(code, 4)
        self.assertIn("refused: I can't help", out)
        self.server.state["openai_body"] = {"id": "r", "status": "failed", "output": [],
                                            "error": {"code": "server_error", "message": "boom"}}
        code, out, _ = self.run_cli("--sensitivity", "public", "--file", self.draft(),
                                    "--voices", "openai", "--yes")
        self.assertEqual(code, 4)
        self.assertIn("response failed: server_error boom", out)


class PromptTest(ChorusCase):
    """What reviewers are sent: the draft and the criteria only, and the verifier seat."""

    def test_criteria_is_sent_as_delimited_public_context_and_named_in_the_plan(self):
        rfp = self.draft("Criteria: measurable outcomes; budget justification.", "rfp.md")
        code, _, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                    "--criteria", rfp, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("criteria: %s" % rfp, err)
        code, _, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                    "--criteria", rfp)
        self.assertIn("the draft and the criteria", err)
        code, _, _ = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                  "--criteria", rfp, "--voices", "glm", "--yes")
        self.assertEqual(code, 0)
        (_, _, body, _), = self.posts("/chat/completions")
        self.assertIn("<<<CRITERIA", body["messages"][1]["content"])

    def test_reviewers_get_the_draft_and_criteria_only(self):
        d = os.path.join(self.tmp.name, "Funder-X-confidential")
        self.draft("SENTINEL-PACKET source notes", "packet.md", sub="Funder-X-confidential")
        path = self.draft(sub="Funder-X-confidential")
        crit_text = "Criteria: outcomes."
        rfp = self.draft(crit_text, "rfp.md")
        before = (sorted(os.listdir(self.tmp.name)), sorted(os.listdir(d)),
                  sorted(os.listdir(os.getcwd())))
        code, _, _ = self.run_cli("--sensitivity", "internal", "--file", path, "--criteria", rfp,
                                  "--yes")
        self.assertEqual(code, 0)
        # with no --brief, --criteria adds 'fit' to the internal default briefs
        system, user = chorus.build_prompt(chorus.DEFAULT_BRIEFS["internal"] + ["fit"], DRAFT,
                                           crit_text)
        (_, _, body, _), = self.round1("/responses")
        self.assertEqual((body["instructions"], body["input"]), (system, user))
        for _, _, b, _ in self.round1("/chat/completions"):
            self.assertEqual(b["messages"], [{"role": "system", "content": system},
                                             {"role": "user", "content": user}])
        for _, _, b, _ in self.posts(""):  # round 2 carries the same draft plus the findings
            text = b.get("input") or b["messages"][1]["content"]
            self.assertTrue(text.startswith(user), text[:80])
        blob = json.dumps([r[2] for r in self.server.state["requests"]])
        self.assertNotIn("SENTINEL-PACKET", blob)
        self.assertNotIn("Funder-X", blob)
        after = (sorted(os.listdir(self.tmp.name)), sorted(os.listdir(d)),
                 sorted(os.listdir(os.getcwd())))
        self.assertEqual(before, after)  # writes no files

    def test_reviewers_are_told_never_to_write_replacement_text(self):
        system, _ = chorus.build_prompt(["tells"], DRAFT, None)
        self.assertIn("NEVER write replacement sentences", system)
        self.assertNotIn("suggestion", json.dumps(chorus.findings_schema(["tells"])))


class VoiceAndKeyTest(ChorusCase):
    """Per-voice models, keys per host, missing keys, the dry run and the catalog."""

    def test_per_voice_model_alias_and_family_lock(self):
        code, _, _ = self.run_cli("--sensitivity", "public", "--file", self.draft(),
                                  "--voices", "openai", "--model", "openai=astra", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(self.posts("/responses")[0][2]["model"], "gpt-6-astra")
        code, _, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                    "--voices", "glm", "--model", "glm=openai/gpt-oss-120b", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("runs only z-ai/ models", err)

    def test_each_host_gets_only_its_own_key(self):
        code, _, _ = self.run_cli("--sensitivity", "internal", "--file", self.draft(), "--yes")
        self.assertEqual(code, 0)
        for method, path, _, hdrs in self.server.state["requests"]:
            want = "test-openai-key" if path.startswith("/openai/") else "test-phala-key"
            self.assertEqual(hdrs.get("Authorization"), "Bearer " + want, path)
        self.server.state["requests"] = []
        code, _, _ = self.run_cli("--sensitivity", "confidential", "--file", self.draft(), "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(self.reqs(suffix="/openai/"), [])
        for _, path, _, hdrs in self.server.state["requests"]:
            self.assertEqual(hdrs.get("Authorization"), "Bearer test-phala-key", path)

    def test_default_chorus_skips_voices_without_keys(self):
        os.environ.pop("OPENAI_API_KEY")
        code, out, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(), "--yes",
                                      "--json")
        self.assertEqual(code, 0)
        self.assertIn("skip openai: OPENAI_API_KEY not set", err)
        self.assertEqual(sorted(json.loads(out)["results"]), ["glm", "kimi"])

    def test_no_keys_at_all_exits_3(self):
        os.environ.pop("OPENAI_API_KEY")
        os.environ.pop("PHALA_API_KEY")
        code, _, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(), "--yes")
        self.assertEqual(code, 3)
        self.assertIn("OPENAI_API_KEY", err)
        self.assertIn("PHALA_API_KEY", err)
        self.assertEqual(self.server.state["requests"], [])

    def test_explicit_voice_without_key_exits_3(self):
        code, _, err = self.run_cli("--sensitivity", "public", "--file", self.draft(),
                                    "--voices", "gemini", "--yes")
        self.assertEqual(code, 3)
        self.assertIn("GEMINI_API_KEY", err)

    def test_dry_run_sends_nothing(self):
        code, _, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                    "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("nothing sent", err)
        self.assertEqual(self.server.state["requests"], [])

    def test_unknown_tee_model_is_refused_by_the_catalog_check(self):
        code, _, err = self.run_cli("--sensitivity", "internal", "--voices", "glm", "--model",
                                    "glm=z-ai/glm-9", "--file", self.draft(), "--yes")
        self.assertEqual(code, 2)  # outside confidential the catalog check still refuses it
        self.assertIn("not in the live catalog", err)
        self.assertEqual(self.posts("/chat/completions"), [])
        self.server.state["requests"] = []
        code, _, err = self.run_cli("--sensitivity", "confidential", "--voices", "glm", "--model",
                                    "glm=z-ai/glm-9", "--file", self.draft(), "--yes")
        self.assertEqual(code, 2)  # confidential: refused before any content is sent
        self.assertIn("not in the live catalog", err)
        self.assertEqual(self.posts(""), [])

    def test_confidential_single_voice_and_brief_flags(self):
        code, out, err = self.run_cli("--sensitivity", "confidential", "--voices", "kimi",
                                      "--brief", "redteam", "--file", self.draft(), "--yes",
                                      "--json")
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertEqual(res["plan"]["sensitivity"], "confidential")
        self.assertEqual(list(res["results"]), ["kimi"])
        self.assertEqual(res["plan"]["briefs"], ["redteam"])
        code, _, err = self.run_cli("--sensitivity", "confidential", "--brief", "all", "--file",
                                    self.draft(), "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("briefs=refute,tells,redteam,fit ", err)

    def test_brief_with_only_separators_exits_1_naming_the_known_briefs(self):
        known = "(known: %s, or 'all')" % ", ".join(chorus.BRIEFS)
        for spec in (",", ", ,", ",,", " , "):
            for go in ("--dry-run", "--yes"):
                code, out, err = self.run_cli("--sensitivity", "internal", "--file",
                                              self.draft(), "--brief", spec, go)
                self.assertEqual(code, 1, (spec, go, err))
                self.assertIn("--brief names no brief %s" % known, err)
                for b in chorus.BRIEFS:
                    self.assertIn(b, err)
                self.assertEqual(out, "")
        self.assertEqual(self.server.state["requests"], [])

    def test_list_models_accepts_and_ignores_file(self):
        code, out, _ = self.run_cli("--list-models", "--file", self.draft("", "e.md"))
        self.assertEqual(code, 0)
        self.assertIn('moonshotai/kimi-k3   is_tee=true providers=["upstream-c"]', out)


class QuoteAndAgreementTest(ChorusCase):
    """Locating quotes in a markdown draft, and grouping the passages several families flagged."""

    def test_quote_matching_survives_markdown_quotes_and_dashes(self):
        md = ("# Public Goods Residency\n\nWe request **$250,000** over 12 months.\n"
              "The so-called ‘public goods’ problem is not just a coordination failure "
              "— it is a *measurement* failure.\n"
              "- Our ledger is a [public impact ledger, published on the Fernquill Protocol]"
              "(https://example.org).\n"
              "TmC runs it.\n")
        nd = chorus.norm(md)
        for q in ["We request $250,000 over 12 months", "The so-called 'public goods' problem",
                  "not just a coordination failure - it is a measurement failure",
                  "public impact ledger, published on the Fernquill Protocol", "TmC",
                  "the so-called ... problem"]:
            self.assertTrue(chorus.quote_spans(q, nd), q)
        self.assertEqual(chorus.quote_spans("an absent sentence", nd), [])
        self.assertEqual(len(chorus.quote_spans("public goods", nd)), 2)

    def test_agreement_is_transitive_and_skips_ambiguous_quotes(self):
        f = lambda spans, brief="tells": {"spans": spans, "severity": "low", "brief": brief,
                                          "quote": "q", "issue": "i"}
        results = {"openai": {"findings": [f([(172, 194)])]},
                   "kimi": {"findings": [f([(213, 241)])]},
                   "glm": {"findings": [f([(191, 217)])]}}
        groups = chorus.agreements(results)
        self.assertEqual(len(groups), 1)
        self.assertEqual(sorted(n for n, _ in groups[0]), ["glm", "kimi", "openai"])
        results = {"openai": {"findings": [f([(2, 14), (40, 52)])]},
                   "kimi": {"findings": [f([(2, 14), (40, 52)])]}}
        self.assertEqual(chorus.agreements(results), [])


class ReadmeExampleTest(ChorusCase):
    """The README's example dry run prints exactly the plan the README shows."""

    def readme_example(self):
        """From the README's 'Example run' section: the dry-run command's variables and argv,
        and the text block that follows it."""
        root = os.path.dirname(os.path.abspath(chorus.__file__))
        with open(os.path.join(root, "README.md"), encoding="utf-8") as f:
            section = f.read().split("\n## Example run\n", 1)[1].split("\n## ", 1)[0]
        blocks = re.findall(r"(?ms)^```(\w*)\n(.*?)^```$", section)
        at = [i for i, (lang, body) in enumerate(blocks)
              if lang == "bash" and body.strip().endswith("--dry-run")]
        self.assertEqual(len(at), 1, "one dry-run command in the Example run section")
        words = shlex.split(blocks[at[0]][1])
        env = {}
        while words and re.match(r"[A-Z_][A-Z0-9_]*=", words[0]):
            k, _, v = words.pop(0).partition("=")
            env[k] = v
        self.assertEqual(words[:2], ["python3", "chorus.py"])
        shown = [body for lang, body in blocks[at[0] + 1:] if lang == "text"]
        self.assertTrue(shown, "a text block after the dry-run command")
        self.assertTrue(shown[0].endswith("dry run: guards passed; nothing sent.\n"), shown[0])
        return root, env, words[2:], shown[0]

    def test_readme_example_dry_run_matches_the_readme(self):
        root, env, argv, shown = self.readme_example()
        self.assertEqual(env, {"OPENAI_API_KEY": "x", "PHALA_API_KEY": "x"})
        opened = []

        class NoNetwork:  # the real voices' hosts are never dialled from a test
            def open(self, req, timeout=None):
                opened.append(req.full_url)
                raise urllib.error.URLError("no network in tests")

        # a reader's shell: the command's two variables, a scratch HOME, nothing else set
        shell = dict(env, HOME=self.home, PATH=os.environ.get("PATH", ""), no_proxy="*")
        here = os.getcwd()
        with mock.patch.object(chorus, "VOICES", copy.deepcopy(REAL_VOICES)), \
                mock.patch.object(chorus, "_OPENER", NoNetwork()), \
                mock.patch.dict(os.environ, shell, clear=True):
            os.chdir(root)  # the example's paths are relative to the repo root
            try:
                code, out, err = self.run_cli(*argv)
            finally:
                os.chdir(here)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "")
        self.assertEqual(err, shown)
        self.assertEqual(opened, [])
        self.assertEqual(self.server.state["requests"], [])


class TransportTest(ChorusCase):
    """HTTP transport: a redirect is refused, so a bearer key never follows one."""

    def test_redirects_are_refused_and_never_carry_the_key(self):
        seen = []

        class Sink(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                seen.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()

        sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
        target = "http://localhost:%d/x" % sink.server_address[1]

        class Source(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", target)
                self.end_headers()

        src = ThreadingHTTPServer(("127.0.0.1", 0), Source)
        for s in (sink, src):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                chorus.http("GET", "http://127.0.0.1:%d/r" % src.server_address[1], "SECRET-KEY")
            cm.exception.close()
            self.assertEqual(seen, [])
        finally:
            for s in (sink, src):
                s.shutdown()
                s.server_close()
