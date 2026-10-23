"""The grant use case: its definition, declared sensitivity, hats, criteria, verifier-only seat, receipts, guards and exits."""
import json
import os
import tomllib
from unittest import mock

from harness import (APPROVED_GLM, GRANT, GRANT_HATS, KIMI_PINS, ChorusCase, chorus,
                     grant_chat, grant_openai)
from test_use_cases import check_criteria_takes, check_docstring_table


def grant_ground():
    """The grant use case's shared_role, folded to one line the way the loader folds it: the
    ground rule appended to every grant hat's role."""
    with open(os.path.join(chorus.BUILTIN_USE_CASES, "grant.toml"), "rb") as f:
        return " ".join(tomllib.load(f)["shared_role"].split())


# A use case outside the shipped library whose hats carry a brief of its own named "budget";
# whether --criteria is taken depends only on that brief's reads_criteria mark.
ZZ_TEST = '''
purpose = "a test use case"
sensitivity = ["public"]

[briefs.budget]
reads_criteria = %s
text = "Review the budget against the work it pays for."

[[hats]]
name = "a"
title = "a"
briefs = ["budget"]
looks_for = "r"

[[hats]]
name = "b"
title = "b"
briefs = ["refute"]
looks_for = "r"

[[hats]]
name = "c"
title = "c"
briefs = ["redteam"]
looks_for = "r"
'''


class GrantUseCaseTests(ChorusCase):
    def granted(self):
        self.server.state["chat"] = grant_chat
        self.server.state["openai_fn"] = grant_openai

    def round1_requests(self):
        """{voice: (system prompt, findings item schema, body)} for each round-1 request."""
        out = {}
        for _, path, b, _ in self.round1(""):
            if path.endswith("/responses"):
                out["openai"] = (b["instructions"],
                                 b["text"]["format"]["schema"]["properties"]["findings"]["items"], b)
            else:
                out["kimi" if "kimi" in b["model"] else "glm"] = (
                    b["messages"][0]["content"],
                    b["response_format"]["json_schema"]["schema"]["properties"]["findings"]["items"],
                    b)
        return out

    def tree(self):
        """Every path under the test's temp dir, HOME and the run's cwd included."""
        return sorted(os.path.join(d, n) for d, ds, fs in os.walk(self.tmp.name) for n in ds + fs)

    def all_briefs(self):
        """Every brief text a prompt could carry: the core briefs and every use case's own."""
        out = dict(chorus.BRIEFS)
        for uc in self.use_cases().values():
            out.update(uc["briefs"])
        return out

    def test_grant_use_case_registry(self):
        uc = self.use_cases()["grant"]
        self.assertEqual(uc["classes"], ("internal", "confidential"))
        self.assertFalse(uc["redacted"])
        self.assertEqual(list(uc["hats"]), GRANT_HATS)
        self.assertEqual({k: h["briefs"] for k, h in uc["hats"].items()},
                         {"program-officer": ["fit", "redteam"],
                          "impact-evaluator": ["impact", "refute"],
                          "budget-reviewer": ["budget", "refute"]})
        used = {b for h in uc["hats"].values() for b in h["briefs"]}
        self.assertEqual(used & set(chorus.BRIEFS), {"refute", "redteam", "fit"})  # reused
        self.assertEqual(used - set(chorus.BRIEFS), {"impact", "budget"})  # the use case's own
        self.assertEqual(set(uc["briefs"]), {"impact", "budget"})
        self.assertNotIn("tells", used)  # the tells-only re-run stays on --brief tells
        for cls in uc["classes"]:  # no per-sensitivity extras
            self.assertEqual(chorus.use_case_hats(uc, cls), uc["hats"], cls)
        for b in ("impact", "budget"):  # a use case's own briefs are not selectable with --brief
            code, _, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                        "--brief", b, "--dry-run")
            self.assertEqual(code, 1, b)
            self.assertIn("unknown brief", err)
        self.assertEqual(chorus.CRITERIA_BRIEFS, ("fit",))  # the core brief that reads criteria
        self.assertEqual(set(uc["criteria_briefs"]), {"fit", "impact", "budget"})
        for b in ("impact", "budget"):
            self.assertIn("PUBLIC CRITERIA block", uc["briefs"][b])
        ground = grant_ground()
        for h in uc["hats"].values():  # every hat judges only by the draft and the call
            self.assertTrue(h["role"].endswith(ground), h["title"])
        self.assertIn("Never assert a funder rule", ground)
        for word in ("market rate", "baseline", "typical grant size", "eligibility condition"):
            self.assertIn(word, ground)
        code, out, _ = self.run_cli("--list-use-cases")
        self.assertEqual(code, 0)
        self.assertIn("## grant — a grant or RFP application", out)
        self.assertIn("sensitivity: internal or confidential", out)
        self.assertIn("- budget-reviewer (budget reviewer) · briefs: budget, refute", out)
        self.assertIn("at internal: for a grant application", out)

    def test_the_docstring_lists_every_use_case_and_hat(self):
        check_docstring_table(self, self.use_cases())  # every shipped one, each hat in its row

    def test_grant_use_case_needs_a_declared_sensitivity(self):
        path = self.draft(GRANT)
        code, _, err = self.run_cli("--use-case", "grant", "--file", path, "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("declare the content's sensitivity with --sensitivity (internal, "
                      "confidential).", err)  # its own levels only
        self.assertNotIn("implies --sensitivity", err)  # never implied: it allows two levels
        code, _, err = self.run_cli("--use-case", "grant", "--sensitivity", "public", "--file",
                                    path, "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("--use-case grant takes sensitivity internal or confidential, not public",
                      err)
        code, _, err = self.run_cli("--use-case", "grant", "--sensitivity", "internal", "--file",
                                    path, "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("briefs=fit,redteam,impact,refute,budget ", err)
        self.assertIn("check: for a grant application, that means the form text and the "
                      "budget as the funder will see them", err)
        code, _, err = self.run_cli("--use-case", "grant", "--sensitivity", "confidential",
                                    "--file", path, "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("sensitivity=confidential", err)
        self.assertIn("briefs=fit,redteam,impact,refute,budget ", err)
        code, _, err = self.run_cli("--use-case", "grant", "--sensitivity", "internal", "--file",
                                    path)
        self.assertEqual(code, 2)
        stop = err.split("STOP:", 1)[1]  # the STOP itself restates the sensitivity check
        self.assertIn("the content's sensitivity is 'internal'", stop)
        self.assertIn("never the internal cost workings", stop)
        self.assertEqual(self.server.state["requests"], [])

    def test_grant_use_case_end_to_end(self):
        self.granted()
        path = self.draft(GRANT)
        cwd = os.path.join(self.tmp.name, "cwd")
        os.makedirs(cwd)
        before, here = self.tree(), os.getcwd()
        os.chdir(cwd)
        try:
            code, out, err = self.run_cli("--use-case", "grant", "--sensitivity", "internal",
                                          "--file", path, "--yes", "--json")
        finally:
            os.chdir(here)
        self.assertEqual(code, 0, err)
        self.assertEqual(before, self.tree())  # writes no files: not in HOME, not in the cwd
        res = json.loads(out)
        self.assertEqual((res["plan"]["sensitivity"], res["plan"]["use_case"]),
                         ("internal", "grant"))
        self.assertFalse(res["plan"]["rewrites"])
        self.assertEqual(sorted(res["results"]), ["glm", "kimi", "openai"])  # every voice
        hats = res["plan"]["hats"]
        self.assertEqual(sorted(h for hs in hats.values() for h in hs), sorted(GRANT_HATS))
        self.assertTrue(all(len(hs) == 1 for hs in hats.values()))  # three hats, three voices
        registry = self.use_cases()["grant"]["hats"]
        ground = grant_ground()
        sent = self.round1_requests()
        self.assertEqual(sorted(sent), ["glm", "kimi", "openai"])
        for voice, (system, item, body) in sent.items():
            mine = list(dict.fromkeys(b for h in hats[voice] for b in registry[h]["briefs"]))
            self.assertEqual(item["properties"]["brief"]["enum"], mine, voice)
            self.assertEqual(item["properties"]["hat"]["enum"], hats[voice], voice)
            self.assertNotIn("rewrite", item["properties"])
            for b, text in self.all_briefs().items():  # exactly its own hats' briefs
                (self.assertIn if b in mine else self.assertNotIn)("- %s: %s" % (b, text), system)
            for h, hat in registry.items():
                (self.assertIn if h in hats[voice] else self.assertNotIn)(hat["role"], system)
            self.assertIn("NEVER write replacement sentences", system)
            self.assertIn(ground, system)
            if voice != "openai":
                self.assertIs(body["provider"]["aci_verified"], True)
        exams = [b for _, _, b, _ in self.posts("") if "FINDINGS TO EXAMINE" in json.dumps(b)]
        self.assertTrue(exams)
        for b in exams:  # round 2 reads the ground rule too, through the hats
            self.assertIn(ground, b.get("instructions") or b["messages"][0]["content"])
        for n, r in res["results"].items():
            self.assertEqual(r["hats"], hats[n])
            self.assertTrue(r["findings"])
            if n != "openai":
                self.assertEqual(r["receipt"], "verified")
        self.assertIn(["glm", "kimi", "openai"],
                      [sorted({f["voice"] for f in g}) for g in res["agreements"]])

    def test_grant_use_case_wears_every_hat_on_fewer_voices(self):
        self.granted()
        path = self.draft(GRANT)
        registry = self.use_cases()["grant"]["hats"]
        for voices, sizes in (("kimi,glm", [1, 2]), ("glm", [3])):
            self.server.state["requests"] = []
            code, out, err = self.run_cli("--use-case", "grant", "--sensitivity", "internal",
                                          "--file", path, "--voices", voices, "--yes", "--json")
            self.assertEqual(code, 0, err)
            hats = json.loads(out)["plan"]["hats"]
            self.assertEqual(sorted(h for hs in hats.values() for h in hs), sorted(GRANT_HATS))
            self.assertEqual(sorted(len(hs) for hs in hats.values()), sizes)
            for voice, (system, item, _) in self.round1_requests().items():
                n = len(hats[voice])
                self.assertIn("You wear %s" % ("one hat" if n == 1 else "%d hats" % n), system)
                mine = list(dict.fromkeys(b for h in hats[voice] for b in registry[h]["briefs"]))
                self.assertEqual(item["properties"]["brief"]["enum"], mine, voice)
        for seed in range(12):  # whatever the seed, the plan deals every hat
            code, _, err = self.run_cli("--use-case", "grant", "--sensitivity", "internal",
                                        "--file", path, "--voices", "kimi,glm", "--seed",
                                        str(seed), "--dry-run")
            self.assertEqual(code, 0, err)
            line = [ln for ln in err.splitlines() if ln.strip().startswith("hats:")][0]
            for h in GRANT_HATS:
                self.assertIn(h, line, seed)

    def test_grant_use_case_takes_criteria_where_its_hats_read_it(self):
        self.granted()
        rfp = self.draft("The Fernquill Fund backs open civic tools. Match: 1:1, secured at "
                         "application. Overhead: at most 10%.\n", "rfp.md")
        code, out, err = self.run_cli("--use-case", "grant", "--sensitivity", "internal",
                                      "--file", self.draft(GRANT), "--criteria", rfp,
                                      "--hats", "kimi=program-officer", "--yes", "--json")
        self.assertEqual(code, 0, err)
        hats = json.loads(out)["plan"]["hats"]
        self.assertEqual(hats["kimi"], ["program-officer"])
        self.assertIn("criteria: %s" % rfp, err)
        readers = self.use_cases()["grant"]["criteria_briefs"]
        reads = {"program-officer": ["fit"], "impact-evaluator": ["impact"],
                 "budget-reviewer": ["budget"]}
        for voice, (system, _, body) in self.round1_requests().items():
            user = body["input"] if voice == "openai" else body["messages"][1]["content"]
            self.assertIn("<<<CRITERIA\nThe Fernquill Fund backs", user)  # every voice gets it
            self.assertEqual([b for b in readers if "- %s: " % b in system],
                             reads[hats[voice][0]], voice)
        self.assertNotIn("no --criteria", err)
        code, _, err = self.run_cli("--use-case", "grant", "--sensitivity", "internal", "--file",
                                    self.draft(GRANT), "--voices", "kimi", "--dry-run")
        self.assertEqual(code, 0, err)  # a call is optional; the plan says what the hats read
        self.assertIn("note: no --criteria: budget, fit, impact judge against what the draft "
                      "itself states", err)
        code, _, err = self.run_cli("--use-case", "code", "--sensitivity", "confidential",
                                    "--file", self.draft(), "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertNotIn("no --criteria", err)
        code, _, err = self.run_cli("--use-case", "copy", "--sensitivity", "public", "--file",
                                    self.draft(), "--criteria", rfp, "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("--criteria is read only by the 'fit' briefs, and --use-case copy at "
                      "public runs none of them", err)
        check_criteria_takes(self, err, self.use_cases())  # copy at internal, grant at both
        # the gate reads the briefs a run carries, not a use case's name
        for mark, want in (("true", 0), ("false", 1)):
            d = os.path.join(self.tmp.name, "cases-" + mark)
            os.makedirs(d)
            with open(os.path.join(d, "zz-test.toml"), "w", encoding="utf-8") as f:
                f.write(ZZ_TEST % mark)
            code, _, err = self.run_cli("--use-case-dir", d, "--use-case", "zz-test",
                                        "--sensitivity", "public", "--file", self.draft(),
                                        "--criteria", rfp, "--dry-run")
            self.assertEqual(code, want, (mark, err))
            if want:
                self.assertIn("--use-case zz-test at public runs none of them", err)

    def test_grant_use_case_stays_verifier_only(self):
        path = self.draft(GRANT)
        for cls in ("internal", "confidential"):
            code, _, err = self.run_cli("--use-case", "grant", "--sensitivity", cls, "--file",
                                        path, "--rewrites", "--yes")
            self.assertEqual(code, 1, cls)
            self.assertIn("--rewrites is for a use case whose output allows it, and --use-case "
                          "grant allows it at no sensitivity: every other run stays "
                          "verifier-only", err)
        self.assertEqual(self.server.state["requests"], [])

        def leak(fn):  # a voice that offers wording anyway is never relayed
            def wrapped(body):
                obj = json.loads(fn(body))
                for f in obj["findings"]:
                    f["rewrite"] = "PASTE-ME wording"
                return json.dumps(obj)
            return wrapped
        self.server.state.update(chat=leak(grant_chat), openai_fn=leak(grant_openai))
        for fmt in (["--json"], []):
            code, out, err = self.run_cli("--use-case", "grant", "--sensitivity", "internal",
                                          "--file", path, "--yes", *fmt)
            self.assertEqual(code, 0, err)
            self.assertNotIn("PASTE-ME", out)
        self.assertNotIn("suggested wording", out)
        exams = [b for _, _, b, _ in self.posts("") if "FINDINGS TO EXAMINE" in json.dumps(b)]
        self.assertTrue(exams)
        for b in exams:  # round 2 keeps the hats and stays verifier-only
            system = b.get("instructions") or b["messages"][0]["content"]
            user = b.get("input") or b["messages"][1]["content"]
            self.assertIn("Never write replacement text", system)
            self.assertIn("judge every finding as", system)
            self.assertRegex(user.split("<<<FINDINGS", 1)[1],
                             r"\[raised as: (%s)\]" % "|".join(GRANT_HATS))

    def test_grant_use_case_keeps_the_receipt_and_guard_rules(self):
        self.granted()
        path = self.draft(GRANT)
        self.server.state["receipt_status"] = 404
        code, _, err = self.run_cli("--use-case", "grant", "--sensitivity", "internal", "--file",
                                    path, "--voices", "kimi", "--yes")
        self.assertEqual(code, 6, err)  # internal: a missing receipt warns (confidential: exit 5)
        self.assertIn("TEE RECEIPT MISSING", err)
        self.assertNotIn("CONFIDENTIALITY NOT PROVEN", err)
        del self.server.state["receipt_status"]
        self.server.state["receipt"] = {"event_log": [{
            "type": "upstream.verified", "result": "failed", "required": True,
            "session_id": "s1"}]}
        code, _, err = self.run_cli("--use-case", "grant", "--sensitivity", "internal", "--file",
                                    path, "--voices", "kimi", "--yes")
        self.assertEqual(code, 5, err)  # "not confidential" is exit 5 at every sensitivity
        self.assertIn("CONFIDENTIALITY NOT PROVEN", err)
        n = len(self.server.state["requests"])
        email = "pat.lee" + "@" + "tallowmere.example"
        code, _, err = self.run_cli("--use-case", "grant", "--sensitivity", "internal", "--file",
                                    self.draft(GRANT + "Contact: %s\n" % email), "--yes")
        self.assertEqual(code, 2)
        self.assertIn("email address", err)
        self.assertNotIn(email, err)
        tok = "sk-proj-" + "Q" * 30
        code, out, err = self.run_cli("--use-case", "grant", "--sensitivity", "internal",
                                      "--file", self.draft(GRANT + "key: %s\n" % tok),
                                      "--allow-flagged", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("credential material", err)
        self.assertNotIn("Q" * 10, out + err)
        self.assertEqual(len(self.server.state["requests"]), n)

    def test_grant_report_tags_findings_with_their_hats(self):
        self.granted()
        code, out, err = self.run_cli("--use-case", "grant", "--sensitivity", "internal",
                                      "--file", self.draft(GRANT), "--seed", "g1", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("use case: grant", out)
        self.assertIn("(seed `g1`, --seed)", out)
        self.assertRegex(out, r"(?m)^- \[HIGH\]\[(fit|impact|budget)\]"
                              r"\[(program-officer|impact-evaluator|budget-reviewer)\]")
        self.assertIn("3 different hats", out)
        self.assertIn("Each finding carries the hat it was raised under", out)

    def test_hyphenated_hat_tags_are_matched_and_off_hat_tags_stay_unknown(self):
        f = {"brief": "fit", "severity": "high", "quote": "", "issue": "x", "direction": "y"}
        two = ["program-officer", "budget-reviewer"]
        for tag in ("Program Officer", "program_officer", " PROGRAM-OFFICER ", "program  officer"):
            n = chorus.normalize_findings({"findings": [dict(f, hat=tag)]}, "", two)[0]
            self.assertEqual(n["hat"], "program-officer", tag)
        n = chorus.normalize_findings({"findings": [dict(f, hat="impact-evaluator")]}, "", two)[0]
        self.assertEqual(n["hat"], "?")  # a hat this voice does not wear

        def off_hat(body):  # a voice wearing every hat tags everything with one it does not wear
            obj = json.loads(grant_chat(body))
            for x in obj["findings"]:
                x["hat"] = "lp"
            return json.dumps(obj)
        self.server.state["chat"] = off_hat
        code, out, err = self.run_cli("--use-case", "grant", "--sensitivity", "internal",
                                      "--file", self.draft(GRANT), "--voices", "glm", "--yes",
                                      "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual({x["hat"] for x in json.loads(out)["results"]["glm"]["findings"]}, {"?"})

    def test_grant_use_case_keeps_the_tee_floor_and_exit_rules(self):
        self.granted()
        path = self.draft(GRANT)
        self.server.state["catalog"][1]["is_tee"] = False  # glm
        code, out, err = self.run_cli("--use-case", "grant", "--sensitivity", "internal",
                                      "--file", path, "--yes", "--json")
        self.assertEqual(code, 6, err)  # refused at the TEE check: a partial chorus
        plan = json.loads(out)["plan"]
        self.assertEqual(plan["hats_redealt"], ["glm"])
        self.assertEqual(sorted(h for hs in plan["hats"].values() for h in hs), sorted(GRANT_HATS))
        self.assertNotIn("glm", plan["hats"])
        self.assertFalse(json.loads(out)["results"]["glm"]["sent"])
        self.server.state["catalog"][1]["is_tee"] = True
        n = len(self.server.state["requests"])
        ssh = self.draft(GRANT, "fields.md", sub="home/.ssh")  # under $HOME/.ssh, a default entry
        restricted = os.path.join(self.tmp.name, "x", "restricted")
        crit = self.draft("Criteria.", "rfp.md", sub="x/restricted")  # under a configured entry
        with mock.patch.dict(os.environ, {"CHORUS_NEVER_SEND": restricted}):
            for argv, why in ((["--file", ssh], "it is under ~/.ssh"),
                              (["--file", path, "--criteria", crit],
                               "it is under CHORUS_NEVER_SEND entry")):
                code, _, err = self.run_cli("--use-case", "grant", "--sensitivity", "internal",
                                            *argv, "--yes")
                self.assertEqual(code, 2, argv)
                self.assertIn("never-send list", err)
                self.assertIn(why, err)
        self.assertEqual(len(self.server.state["requests"]), n)
        self.server.state["chat"] = lambda body: ""
        self.server.state["openai_fn"] = lambda body: ""
        code, _, _ = self.run_cli("--use-case", "grant", "--sensitivity", "internal", "--file",
                                  path, "--yes")
        self.assertEqual(code, 4)  # no voice returned structured findings
        os.environ.pop("OPENAI_API_KEY")
        os.environ.pop("PHALA_API_KEY")
        n = len(self.server.state["requests"])
        code, _, err = self.run_cli("--use-case", "grant", "--sensitivity", "internal", "--file",
                                    path, "--yes")
        self.assertEqual(code, 3)
        self.assertEqual(len(self.server.state["requests"]), n)

    def test_grant_use_case_at_confidential_stays_in_the_enclave(self):
        self.granted()
        path = self.draft(GRANT)
        code, out, err = self.run_cli("--use-case", "grant", "--sensitivity", "confidential",
                                      "--file", path, "--yes", "--json")
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertEqual(res["plan"]["sensitivity"], "confidential")
        self.assertEqual(sorted(res["results"]), ["glm", "kimi"])  # TEE voices only
        hats = res["plan"]["hats"]
        self.assertEqual(sorted(h for hs in hats.values() for h in hs), sorted(GRANT_HATS))
        pins = {"glm": set(APPROVED_GLM), "kimi": set(KIMI_PINS)}
        sent = self.round1_requests()
        self.assertEqual(sorted(sent), ["glm", "kimi"])
        for voice, (system, _, body) in sent.items():
            self.assertIs(body["provider"]["aci_verified"], True)
            self.assertIs(body["provider"]["zdr"], True)
            self.assertEqual(set(body["provider"]["aci_session_ids"]), pins[voice], voice)
            self.assertIn("NEVER write replacement sentences", system)
        approved = {"glm": {"upstream-a", "upstream-b"}, "kimi": {"upstream-c"}}
        for n, r in res["results"].items():
            self.assertEqual(r["receipt"], "verified", n)
            self.assertTrue(set(r["upstreams"]) <= approved[n], (n, r["upstreams"]))
        code, _, err = self.run_cli("--use-case", "grant", "--sensitivity", "confidential",
                                    "--file", path, "--voices", "openai,kimi", "--yes")
        self.assertEqual(code, 2)  # a frontier voice named on a confidential run is refused
        self.assertIn("allows attested-TEE voices only", err)
        self.server.state["receipt_status"] = 404
        code, _, err = self.run_cli("--use-case", "grant", "--sensitivity", "confidential",
                                    "--file", path, "--voices", "kimi", "--yes")
        self.assertEqual(code, 5, err)  # confidential: a missing receipt is not a warning
        self.assertIn("CONFIDENTIALITY NOT PROVEN", err)
        self.assertNotIn("TEE RECEIPT MISSING", err)
