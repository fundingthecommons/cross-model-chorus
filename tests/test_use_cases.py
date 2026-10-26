"""Use cases and hats: dealing, pinning and seeding hats, redaction, routing by sensitivity, rewrites."""
import contextlib
import io
import json
import os
import re
import shutil
import unittest
from unittest import mock

from harness import (CONTRACT, DRAFT, SOLO_QUOTES, ChorusCase, chorus, hat_chat, hat_openai,
                     provider_for)

# The use cases this repo ships. A contributor may add more TOML files beside them: the tests
# that read the library hold these five to their rules and every loaded one to the loader's.
SHIPPED = ("code", "contracts", "copy", "grant", "startup")

# Where the shipped use cases take --criteria, in the order a refusal names them.
SHIPPED_CRITERIA_TAKES = ["--use-case copy at internal", "--use-case grant at internal",
                          "--use-case grant at confidential"]

# A sixth use case, as a contributor might add it beside the shipped ones: two hats, a brief of
# its own that reads the criteria, two sensitivities.
RELEASE_NOTES = '''
purpose = "a set of release notes, read by the people who install the release"
sensitivity = ["public", "internal"]

[briefs.upgrade]
reads_criteria = true
text = """
Read the notes as someone about to upgrade. For each gap: the line (quoted verbatim), what an
upgrader would get wrong, and what the notes need to say.
"""

[[hats]]
name = "maintainer"
title = "downstream maintainer"
briefs = ["upgrade", "refute"]
looks_for = """
You maintain a project that depends on this release. You read for breaking changes the notes
play down and upgrade steps they leave out.
"""

[[hats]]
name = "newcomer"
title = "first-time user"
briefs = ["tells"]
looks_for = """
You are installing this for the first time. You read for jargon, missing context and claims
you cannot check.
"""
'''


def brief_pairs(cases):
    """Every (brief, text) the runner knows: the core briefs plus each use case's own."""
    pairs = list(chorus.BRIEFS.items())
    for uc in cases.values():
        pairs += [(b, t) for b, t in uc["briefs"].items() if (b, t) not in pairs]
    return pairs


def docstring_rows():
    """The module docstring's USE CASES AND HATS table, as {use case: its row text}."""
    table = chorus.__doc__.split("USE CASES AND HATS", 1)[1].split("Every hat is worn", 1)[0]
    return dict(re.findall(r"(?ms)^  (\w[\w-]*) +(.*?)(?=^  \w|\Z)", table))


def check_docstring_table(test, cases):
    """The docstring lists every shipped use case (a contributor's own need not be listed), and
    only use cases that load; each row names every hat of its use case as a whole word."""
    rows = docstring_rows()
    test.assertLessEqual(set(SHIPPED), set(rows))
    test.assertLessEqual(set(rows), set(cases))
    for name in rows:  # each hat in its own row, as a whole word
        for k in cases[name]["hats"]:
            test.assertRegex(rows[name], r"(?<![\w-])%s(?![\w-])" % re.escape(k), (name, k))


def check_criteria_takes(test, err, cases):
    """A --criteria refusal names where --criteria is taken: every loaded use case at each
    level where one of its hats runs a brief that reads criteria, in load order. The shipped
    ones are exactly SHIPPED_CRITERIA_TAKES."""
    m = re.search(r"It is taken with (.*?)\. Drop --criteria\.", err)
    test.assertIsNotNone(m, err)
    takes = m.group(1).split(" or ")
    test.assertEqual([t for t in takes if t.split()[1] in SHIPPED], SHIPPED_CRITERIA_TAKES)
    want = ["--use-case %s at %s" % (n, c) for n, uc in cases.items() for c in uc["classes"]
            if set(uc["criteria_briefs"]) & {b for h in chorus.use_case_hats(uc, c).values()
                                             for b in h["briefs"]}]
    test.assertEqual(takes, want)


class UseCaseHatTests(ChorusCase):
    def hatted(self):
        self.server.state["chat"] = hat_chat
        self.server.state["openai_fn"] = hat_openai

    def assert_only_these_briefs(self, system, briefs, uc, cases):
        """The prompt carries exactly `briefs`, each with this use case's text, and no other
        brief's text from any use case."""
        texts = dict(chorus.BRIEFS, **uc["briefs"])
        for b, text in brief_pairs(cases):
            line = "- %s: %s" % (b, text)
            (self.assertIn if b in briefs and texts.get(b) == text else self.assertNotIn)(
                line, system)

    def test_deal_hats_wears_every_hat_and_is_deterministic(self):
        self.check_deals(self.use_cases())

    def check_deals(self, cases):
        """Every hat worn, nobody hatless, no hat twice on one voice, balanced, and the same
        deal for the same seed: for every use case in `cases`, over several voice sets."""
        for name, uc in cases.items():
            keys = list(uc["hats"])
            for voices in (["kimi", "glm"], ["openai", "kimi", "glm"],
                           ["openai", "kimi", "glm", "gemini"], ["kimi"],
                           ["openai", "kimi", "glm", "claude"]):
                for seed in range(30):
                    dealt, unworn = chorus.deal_hats(keys, voices, seed)
                    label = "%s %s %s" % (name, voices, seed)
                    self.assertEqual(unworn, [], label)
                    self.assertEqual({h for hs in dealt.values() for h in hs}, set(keys), label)
                    self.assertTrue(all(dealt[v] for v in voices), label)  # nobody hatless
                    for hs in dealt.values():
                        self.assertEqual(len(hs), len(set(hs)), label)
                    sizes = [len(hs) for hs in dealt.values()]
                    self.assertLessEqual(max(sizes) - min(sizes), 1, label)  # balanced
                    self.assertEqual(dealt, chorus.deal_hats(keys, voices, seed)[0], label)

    def test_deal_hats_rotates_across_seeds(self):
        keys = list(self.use_cases()["contracts"]["hats"])
        pairs, doubles, deals = set(), set(), set()
        for seed in range(60):
            dealt, _ = chorus.deal_hats(keys, ["kimi", "glm"], "s%d" % seed)
            pairs |= {(v, h) for v, hs in dealt.items() for h in hs}
            doubles |= {v for v, hs in dealt.items() if len(hs) == 2}
            deals.add(json.dumps(dealt, sort_keys=True))
        self.assertEqual(pairs, {(v, h) for v in ("kimi", "glm") for h in keys})
        self.assertEqual(doubles, {"kimi", "glm"})  # who wears two rotates too
        self.assertEqual(len(deals), 6)  # every way to deal 3 hats over 2 voices

    def test_hat_pins_are_kept_and_every_hat_is_still_dealt(self):
        keys = list(self.use_cases()["copy"]["hats"])
        dealt, unworn = chorus.deal_hats(keys, ["openai", "kimi", "glm"], 1, [("kimi", "vc")])
        self.assertEqual(dealt["kimi"], ["vc"])
        self.assertEqual(sorted(dealt["openai"] + dealt["glm"]), ["copywriter", "fundraiser", "lp"])
        self.assertEqual(unworn, [])
        dealt, unworn = chorus.deal_hats(keys, ["kimi", "glm"], 1, [
            ("kimi", "lp"), ("kimi", "vc"), ("glm", "copywriter")])
        # every voice pinned: the unpinned hat goes to the voice wearing fewest
        self.assertEqual(dealt, {"kimi": ["vc", "lp"], "glm": ["copywriter", "fundraiser"]})
        self.assertEqual(unworn, [])
        path = self.draft()
        for spec, msg in (("kimi=judge", "unknown hat"), ("openai=vc", "not in this run"),
                          ("kimi", "takes voice=hat")):
            code, _, err = self.run_cli("--use-case", "copy", "--sensitivity", "confidential",
                                        "--file", path, "--hats", spec, "--dry-run")
            self.assertEqual(code, 1, spec)
            self.assertIn(msg, err)
        code, _, err = self.run_cli("--use-case", "copy", "--sensitivity", "confidential",
                                    "--file", path, "--hats", "kimi=vc,kimi=LP,glm=copywriter",
                                    "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("hats: kimi = vc + lp; glm = copywriter + fundraiser", err)
        self.assertIn("pinned kimi=vc, kimi=lp, glm=copywriter", err)
        self.assertNotIn("not worn", err)
        self.assertEqual(self.server.state["requests"], [])

    def test_seed_is_recorded_and_the_yes_run_deals_as_its_dry_run(self):
        path = self.draft(CONTRACT)
        argv = ("--use-case", "contracts", "--file", path, "--redact", "Acme Corp=SPONSOR")
        code, _, dry = self.run_cli(*argv, "--dry-run")
        self.assertEqual(code, 0, dry)
        line = [ln for ln in dry.splitlines() if ln.strip().startswith("hats:")][0]
        self.hatted()
        code, out, err = self.run_cli(*argv, "--yes", "--json")
        self.assertEqual(code, 0, err)
        self.assertIn(line, err)
        plan = json.loads(out)["plan"]
        self.assertEqual(plan["seed_source"], "from the draft + date")
        self.assertEqual(plan["seed"], chorus.default_seed(
            "contracts", CONTRACT.replace("Acme Corp", "[SPONSOR]")))
        today = chorus.default_seed("contracts", CONTRACT)
        with mock.patch.object(chorus.time, "strftime", lambda fmt: "2031-01-01"):
            self.assertNotEqual(chorus.default_seed("contracts", CONTRACT), today)
        self.assertNotEqual(chorus.default_seed("contracts", CONTRACT + "x"), today)
        lines = set()
        for _ in range(2):
            code, _, err = self.run_cli(*argv, "--seed", "7", "--dry-run")
            self.assertEqual(code, 0, err)
            lines.add([ln for ln in err.splitlines() if ln.strip().startswith("hats:")][0])
        self.assertEqual(len(lines), 1)
        self.assertIn("seed 7, --seed", lines.pop())

    def test_contracts_use_case_end_to_end(self):
        self.hatted()
        cases = self.use_cases()
        uc = cases["contracts"]
        path = self.draft(CONTRACT)
        before = sorted(os.listdir(self.tmp.name))
        code, out, err = self.run_cli("--use-case", "contracts", "--file", path,
                                      "--redact", "Acme Corp=SPONSOR", "--yes", "--json")
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertEqual(res["plan"]["sensitivity"], "confidential")
        self.assertEqual(sorted(res["results"]), ["glm", "kimi"])  # TEE voices only
        self.assertEqual(self.posts("/responses"), [])
        blob = json.dumps([r[2] for r in self.server.state["requests"]])
        self.assertNotIn("Acme", blob)
        self.assertIn("[SPONSOR]", blob)
        self.assertEqual(res["plan"]["redactions"], [["[SPONSOR]", 2]])
        hats = res["plan"]["hats"]
        self.assertEqual(sorted(h for hs in hats.values() for h in hs),
                         ["commercial", "compliance", "counsel"])
        for _, _, b, _ in self.round1("/chat/completions"):
            voice = "kimi" if "kimi" in b["model"] else "glm"
            system = b["messages"][0]["content"]
            item = b["response_format"]["json_schema"]["schema"]["properties"]["findings"]["items"]
            self.assertEqual(item["properties"]["hat"]["enum"], hats[voice])
            self.assertIn("hat", item["required"])
            self.assertNotIn("rewrite", item["properties"])
            mine = list(dict.fromkeys(br for h in hats[voice] for br in uc["hats"][h]["briefs"]))
            self.assertEqual(item["properties"]["brief"]["enum"], mine)
            self.assert_only_these_briefs(system, mine, uc, cases)  # exactly its own hats' briefs
            for h, hat in uc["hats"].items():
                (self.assertIn if h in hats[voice] else self.assertNotIn)(hat["role"], system)
            self.assertIn("NEVER write replacement sentences", system)
            self.assertEqual(b["provider"], provider_for(b["model"], confidential=True))
        for n, r in res["results"].items():
            self.assertEqual(r["hats"], hats[n])
            self.assertTrue(r["findings"])
            self.assertTrue(all(f["hat"] in hats[n] for f in r["findings"]))
            self.assertEqual(r["receipt"], "verified")
        self.assertEqual(before, sorted(os.listdir(self.tmp.name)))  # writes no files

    def test_report_shows_hats_seed_and_cross_hat_agreement(self):
        self.hatted()
        code, out, err = self.run_cli("--use-case", "contracts", "--file", self.draft(CONTRACT),
                                      "--redacted", "--seed", "abc", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("use case: contracts", out)
        self.assertIn("(seed `abc`, --seed)", out)
        self.assertRegex(out, r"\*\*glm \(\w+(, \w+)?\) \+ kimi \(\w+(, \w+)?\)\*\* · \d different hats")
        by_voice = out.split("## By voice", 1)[1]
        self.assertRegex(by_voice, r"(?m)^- \[HIGH\]\[terms\]\[(counsel|commercial|compliance)\]")
        self.assertRegex(out, r"\((kimi|glm) as (counsel|commercial|compliance)\)")
        self.assertIn(", wearing ", out)
        self.assertIn("Each finding carries the hat it was raised under", out)
        self.assertNotIn("suggested wording", out)

    def test_round_two_keeps_the_hats(self):
        self.hatted()
        uc = self.use_cases()["contracts"]
        code, out, err = self.run_cli("--use-case", "contracts", "--file", self.draft(CONTRACT),
                                      "--redacted", "--yes", "--json")
        self.assertEqual(code, 0, err)
        hats = json.loads(out)["plan"]["hats"]
        exams = [b for _, _, b, _ in self.posts("/chat/completions")
                 if "FINDINGS TO EXAMINE" in json.dumps(b)]
        self.assertEqual(len(exams), 2)
        for b in exams:
            voice = "kimi" if "kimi" in b["model"] else "glm"
            other = "glm" if voice == "kimi" else "kimi"
            system = b["messages"][0]["content"]
            self.assertIn("judge every finding as", system)
            for h in hats[voice]:
                self.assertIn(uc["hats"][h]["role"], system)
            self.assertIn("Never write replacement text", system)
            block = b["messages"][1]["content"].split("<<<FINDINGS", 1)[1]
            self.assertRegex(block, r"\[raised as: (%s)\]" % "|".join(hats[other]))
            self.assertNotIn(SOLO_QUOTES[b["model"]], block)  # never its own finding
        res = json.loads(out)["results"]
        solo = [f for f in res["kimi"]["findings"] if f["quote"] == SOLO_QUOTES["moonshotai/kimi-k3"]]
        self.assertEqual(solo[0]["exam_status"], "held")

    def test_redaction_is_in_memory_one_pass_and_never_echoes_a_term(self):
        text = "Acme Corp signs. ACME corp\npays. Acme alone. Acmeville. The party of the first part.\n"
        out, counts, _ = chorus.redact(text, ["Acme Corp=sponsor!", "acme", "party"])
        self.assertNotIn("acme ", out.lower())
        self.assertIn("Acmeville", out)  # whole words only
        self.assertEqual(counts, [("[SPONSOR]", 2), ("[PARTY-2]", 1), ("[PARTY-3]", 1)])
        self.assertIn("The [PARTY-3] of the first part", out)  # placeholders never re-matched
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            chorus.redact("Acmeville", ["Acme"])
        self.assertEqual(cm.exception.code, 2)
        code, out, err = self.run_cli("--use-case", "contracts", "--file", self.draft(CONTRACT),
                                      "--redact", "Acme Corp", "--redact", "Zyxwv Holdings", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("term 2 matched nothing", err)
        self.assertNotIn("Zyxwv", out + err)
        self.assertEqual(self.server.state["requests"], [])
        email = "jane.roe" + "@" + "acme-sponsor.example"
        path = self.draft(DRAFT + "Signatory: %s\n" % email)
        code, _, err = self.run_cli("--sensitivity", "confidential", "--file", path, "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("email address", err)
        code, _, err = self.run_cli("--sensitivity", "confidential", "--file", path, "--dry-run",
                                    "--redact", email + "=SIGNATORY EMAIL")
        self.assertEqual(code, 0, err)
        self.assertIn("[SIGNATORY EMAIL] ×1", err)
        self.assertNotIn(email, err)

    def test_contracts_and_startup_need_a_redacted_copy_and_the_tee(self):
        for uc in ("contracts", "startup"):
            code, _, err = self.run_cli("--use-case", uc, "--file", self.draft(), "--yes")
            self.assertEqual(code, 2, uc)
            self.assertIn("runs on a redacted copy", err)
            code, _, err = self.run_cli("--use-case", uc, "--sensitivity", "public", "--file",
                                        self.draft(), "--redacted", "--yes")
            self.assertEqual(code, 1, uc)
            self.assertIn("takes sensitivity confidential", err)
            code, _, err = self.run_cli("--use-case", uc, "--file", self.draft(), "--redacted",
                                        "--voices", "openai", "--yes")
            self.assertEqual(code, 2, uc)
            self.assertIn("attested-TEE voices only", err)
        self.assertEqual(self.server.state["requests"], [])
        code, out, err = self.run_cli("--use-case", "startup", "--file", self.draft(),
                                      "--redacted", "--yes", "--json")
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertEqual(sorted(res["results"]), ["glm", "kimi"])
        self.assertTrue(res["plan"]["redacted_by_operator"])
        self.assertEqual(sorted(h for hs in res["plan"]["hats"].values() for h in hs),
                         ["lp", "operator", "techdd", "vc"])

    def test_code_use_case_routes_by_sensitivity_and_takes_a_file_set(self):
        a = self.draft("def f(x):\n    return eval(x)\n", "a.py", sub="repo")
        b = self.draft("import os\nos.system(cmd)", "b.py", sub="repo")
        code, out, err = self.run_cli("--use-case", "code", "--sensitivity", "confidential",
                                      "--file", a, "--file", b, "--yes", "--json")
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertEqual(sorted(res["results"]), ["glm", "kimi"])
        self.assertEqual(res["plan"]["files"], [a, b])
        self.assertIn("FILE 1 = %s (draft lines 2-3); FILE 2 = %s (draft lines 7-8)" % (a, b), err)
        user = self.round1("/chat/completions")[0][2]["messages"][1]["content"]
        self.assertIn("=== FILE 1: a.py ===\ndef f(x):", user)
        self.assertLess(user.index("=== FILE 1: a.py ==="), user.index("=== FILE 2: b.py ==="))
        self.assertNotIn(self.tmp.name, user)  # an absolute path is named by its basename
        body = user.split("<<<DRAFT\n", 1)[1].split("\n")
        self.assertEqual(body[1:3] + body[6:8], ["def f(x):", "    return eval(x)",
                                                 "import os", "os.system(cmd)"])
        code, out, err = self.run_cli("--use-case", "code", "--sensitivity", "public", "--file", a,
                                      "--yes", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(json.loads(out)["results"]), ["glm", "kimi", "openai"])
        code, _, err = self.run_cli("--use-case", "code", "--sensitivity", "internal", "--file", a,
                                    "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("takes sensitivity public or confidential", err)
        leak = self.draft("+API_KEY = '" + "sk-proj-" + "Q" * 30 + "'\n", "leak.diff")
        n = len(self.server.state["requests"])
        code, _, err = self.run_cli("--use-case", "code", "--sensitivity", "confidential",
                                    "--file", a, "--file", leak, "--yes")
        self.assertEqual(code, 2)
        self.assertIn("credential material", err)
        self.assertNotIn("sk-proj", err)
        self.assertEqual(len(self.server.state["requests"]), n)
        code, _, err = self.run_cli("--use-case", "code", "--sensitivity", "confidential",
                                    "--file", a, "--file", self.draft("", "empty.py"), "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("empty.py is empty", err)

    def test_copy_rewrites_are_gated_to_low_stakes_public_copy(self):
        path = self.draft()
        for argv, msg in ((["--sensitivity", "public", "--rewrites"], "need --use-case"),
                          (["--use-case", "copy", "--sensitivity", "internal", "--rewrites"],
                           "--rewrites is for --use-case copy at public"),
                          (["--use-case", "copy", "--sensitivity", "confidential", "--rewrites"],
                           "--rewrites is for --use-case copy at public"),
                          (["--use-case", "code", "--sensitivity", "public", "--rewrites"],
                           "--use-case code allows it at no sensitivity")):
            code, _, err = self.run_cli(*argv, "--file", path, "--yes")
            self.assertEqual(code, 1, argv)
            self.assertIn(msg, err)
        self.assertEqual(self.server.state["requests"], [])
        self.hatted()
        code, out, err = self.run_cli("--use-case", "copy", "--sensitivity", "public", "--file",
                                      self.draft(CONTRACT), "--rewrites", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("suggested wording (the author picks and edits): \"Either party", out)
        self.assertIn("Suggested wording is offered only for low-stakes copy", out)
        self.assertIn("suggested wording ON", err)
        for _, _, b, _ in self.round1(""):
            system = b.get("instructions") or b["messages"][0]["content"]
            self.assertNotIn("NEVER write replacement sentences", system)
            self.assertIn("never add a claim, figure or name", system)
        exams = [b for _, _, b, _ in self.posts("") if "FINDINGS TO EXAMINE" in json.dumps(b)]
        self.assertTrue(exams)
        for b in exams:  # round 2 stays verifier-only
            system = b.get("instructions") or b["messages"][0]["content"]
            self.assertIn("Never write replacement text", system)
            self.assertNotIn("rewrite", json.dumps(b.get("text") or b.get("response_format")))

    def test_without_rewrites_a_models_wording_is_never_relayed(self):
        def leaky(body):
            obj = json.loads(hat_chat(body))
            for f in obj["findings"]:
                f["rewrite"] = "PASTE-ME wording"
            return json.dumps(obj)
        self.server.state["chat"] = leaky
        code, out, err = self.run_cli("--use-case", "copy", "--sensitivity", "confidential",
                                      "--file", self.draft(CONTRACT), "--yes", "--json")
        self.assertEqual(code, 0, err)
        self.assertNotIn("PASTE-ME", out)
        code, out, _ = self.run_cli("--use-case", "copy", "--sensitivity", "confidential",
                                    "--file", self.draft(CONTRACT), "--yes")
        self.assertNotIn("PASTE-ME", out)

    def test_use_case_flag_combinations(self):
        path = self.draft()
        for argv, msg in ((["--sensitivity", "public", "--hats", "openai=vc"], "need --use-case"),
                          (["--sensitivity", "public", "--seed", "1"], "need --use-case"),
                          (["--use-case", "copy", "--sensitivity", "public", "--brief", "tells"],
                           "does not combine with --use-case"),
                          (["--use-case", "code"],
                           "declare the content's sensitivity with --sensitivity"),
                          (["--sensitivity", "public", "--brief", "terms"], "unknown brief")):
            code, _, err = self.run_cli(*argv, "--file", path, "--dry-run")
            self.assertEqual(code, 1, argv)
            self.assertIn(msg, err)

    def test_per_use_case_briefs(self):
        self.check_per_use_case_briefs(self.use_cases())

    def check_per_use_case_briefs(self, cases):
        """Each loaded use case's briefs, hats and levels hold together, its prompt carries
        exactly its hats' briefs, and its dry run plans every brief; the shipped ones wear at
        least three hats (a contributor's own may wear fewer)."""
        self.assertLessEqual(set(SHIPPED), set(cases))
        for name, uc in cases.items():
            self.assertEqual(set(uc["briefs"]) & set(chorus.BRIEFS), set(), name)
            self.assertGreaterEqual(len(uc["hats"]), 3 if name in SHIPPED else 1, name)
            self.assertTrue(set(uc["classes"]) <= set(chorus.SENSITIVITY), name)
            texts = dict(chorus.BRIEFS, **uc["briefs"])
            for h in uc["hats"].values():
                self.assertTrue(h["briefs"] and set(h["briefs"]) <= set(texts), name)
            two = {k: uc["hats"][k] for k in list(uc["hats"])[:2]}
            briefs = list(dict.fromkeys(b for h in two.values() for b in h["briefs"]))
            system, _ = chorus.build_prompt(briefs, DRAFT, None, two, brief_texts=texts)
            self.assertIn("You wear 2 hats", system)
            self.assert_only_these_briefs(system, briefs, uc, cases)
            code, _, err = self.run_cli("--use-case", name, "--sensitivity", uc["classes"][0],
                                        "--file", self.draft(), "--redacted", "--dry-run")
            self.assertEqual(code, 0, err)
            union = list(dict.fromkeys(b for h in uc["hats"].values() for b in h["briefs"]))
            self.assertIn("briefs=%s " % ",".join(union), err)
        self.assertEqual(cases["copy"]["classes"], ("public", "internal", "confidential"))
        self.assertTrue(all("next-step" in h["briefs"] for h in cases["copy"]["hats"].values()))

    def test_a_sixth_use_case_beside_the_shipped_ones_breaks_nothing(self):
        """A contributor's new TOML in the built-in folder: the checks that read the library
        still pass with it loaded, and it runs."""
        shipped = self.use_cases()
        d = os.path.join(self.tmp.name, "use_cases")
        shutil.copytree(chorus.BUILTIN_USE_CASES, d)
        with open(os.path.join(d, "release-notes.toml"), "w", encoding="utf-8") as f:
            f.write(RELEASE_NOTES)
        rfp = self.draft("The Fernquill Fund backs open civic tools.\n", "rfp.md")
        with mock.patch.object(chorus, "BUILTIN_USE_CASES", d):
            cases = self.use_cases()
            self.assertEqual(set(cases), set(shipped) | {"release-notes"})
            self.assertEqual(list(cases["release-notes"]["hats"]), ["maintainer", "newcomer"])
            # every use case loads from the built-in folder, under its own file name
            self.assertLessEqual(set(SHIPPED), set(cases))
            for name, uc in cases.items():
                self.assertEqual(uc["name"], name)
                self.assertEqual(uc["path"], os.path.join(d, name + ".toml"))
            check_docstring_table(self, cases)
            self.check_per_use_case_briefs(cases)
            self.check_deals({"release-notes": cases["release-notes"]})  # 2 hats, up to 4 voices
            code, _, err = self.run_cli("--use-case", "nowhere", "--sensitivity", "public",
                                        "--file", self.draft(), "--dry-run")
            self.assertEqual(code, 1)
            self.assertIn("unknown use case 'nowhere' (known: %s; --list-use-cases shows them)"
                          % ", ".join(sorted(cases)), err)
            self.assertIn("grant, release-notes, startup", err)
            code, _, err = self.run_cli("--use-case", "copy", "--sensitivity", "public",
                                        "--file", self.draft(), "--criteria", rfp, "--dry-run")
            self.assertEqual(code, 1)
            check_criteria_takes(self, err, cases)
            self.assertIn("--use-case release-notes at public or --use-case release-notes at "
                          "internal", err)
            code, out, err = self.run_cli("--list-use-cases")
            self.assertEqual(code, 0, err)
            for name in cases:
                self.assertIn("## %s — " % name, out)
            self.assertIn("- maintainer (downstream maintainer) · briefs: upgrade, refute", out)
            self.assertIn("- newcomer (first-time user) · briefs: tells", out)
            self.assertNotIn("from %s" % d, out)  # listed as built in, like the shipped ones
            code, _, err = self.run_cli("--use-case", "release-notes", "--sensitivity", "public",
                                        "--file", self.draft(), "--criteria", rfp, "--dry-run")
            self.assertEqual(code, 0, err)
            self.assertIn("use case: release-notes — a set of release notes, read by the people "
                          "who install the release", err)
            self.assertIn("briefs=upgrade,refute,tells ", err)
            line = [ln for ln in err.splitlines() if ln.strip().startswith("hats:")][0]
            for v in ("openai", "kimi", "glm"):  # more voices than hats: two share one
                self.assertRegex(line, r"\b%s = (maintainer|newcomer)\b" % v)
            for h in ("maintainer", "newcomer"):
                self.assertIn(h, line)
            self.assertNotIn("not worn", err)
            self.assertIn("dry run: guards passed; nothing sent.", err)
        self.assertEqual(self.server.state["requests"], [])
        self.assertEqual(set(self.use_cases()), set(shipped))  # the patch is gone with the test

    def test_tee_refusal_redeals_the_hats(self):
        self.server.state["catalog"][1]["is_tee"] = False  # glm
        self.hatted()
        code, out, err = self.run_cli("--use-case", "contracts", "--file", self.draft(CONTRACT),
                                      "--redacted", "--yes", "--json")
        self.assertEqual(code, 6, err)
        res = json.loads(out)
        self.assertEqual(res["plan"]["hats"], {"kimi": ["counsel", "commercial", "compliance"]})
        self.assertEqual(res["plan"]["hats_redealt"], ["glm"])
        self.assertIn("hats re-dealt over kimi", err)
        self.assertFalse(res["results"]["glm"]["sent"])
        system = self.round1("/chat/completions")[0][2]["messages"][0]["content"]
        self.assertIn("You wear 3 hats", system)

    def test_use_case_keeps_the_receipt_rules(self):
        self.server.state["receipt_status"] = 404
        code, _, err = self.run_cli("--use-case", "contracts", "--file", self.draft(CONTRACT),
                                    "--redacted", "--voices", "kimi", "--yes")
        self.assertEqual(code, 5)
        self.assertIn("CONFIDENTIALITY NOT PROVEN", err)

    def test_hat_tags_are_normalized_and_wording_needs_rewrites(self):
        f = {"brief": "terms", "severity": "high", "quote": "", "issue": "x", "direction": "y",
             "hat": "Judge", "rewrite": "paste me"}
        norm = lambda *a: chorus.normalize_findings({"findings": [dict(f)]}, "", *a)[0]
        self.assertEqual(norm(["counsel"])["hat"], "counsel")
        self.assertEqual(norm(["counsel", "compliance"])["hat"], "?")
        f["hat"] = " Compliance"
        self.assertEqual(norm(["counsel", "compliance"])["hat"], "compliance")
        self.assertNotIn("hat", norm())
        self.assertNotIn("rewrite", norm(["counsel"]))
        self.assertEqual(norm(["counsel"], True)["rewrite"], "")  # no located passage, no wording
        f.update(quote="may cancel at any time", rewrite="End it on 30 days' notice.")
        nd = chorus.norm(CONTRACT)
        kept = chorus.normalize_findings({"findings": [dict(f)]}, nd, ["counsel"], True)[0]
        self.assertEqual(kept["rewrite"], "End it on 30 days' notice.")
        self.assertTrue(kept["rewrite_new_figures"])  # "30" is not in the draft
        self.assertIn("adds a figure the draft does not contain", chorus.fmt_finding(kept))
        f["rewrite"] = "Sponsor pays within 90 days."
        kept = chorus.normalize_findings({"findings": [dict(f)]}, nd, ["counsel"], True)[0]
        self.assertFalse(kept["rewrite_new_figures"])
        self.assertNotIn("suggestion", json.dumps(chorus.findings_schema(["terms"], ["counsel"])))
        self.assertIn("rewrite", json.dumps(chorus.findings_schema(["terms"], ["counsel"], True)))

    def test_list_use_cases_needs_no_keys(self):
        for k in ("OPENAI_API_KEY", "PHALA_API_KEY"):  # no keys at all
            os.environ.pop(k, None)
        with mock.patch.object(chorus, "parse_env_file", wraps=chorus.parse_env_file) as read_env:
            code, out, err = self.run_cli("--list-use-cases")
        self.assertEqual(code, 0, err)
        for name, uc in self.use_cases().items():
            self.assertIn("## %s — " % name, out)
            for k, h in uc["hats"].items():
                self.assertIn("- %s (%s)" % (k, h["title"]), out)
        self.assertEqual(self.server.state["requests"], [])
        # listing the use cases prints before any env file of keys is read
        self.assertFalse(read_env.called, "--list-use-cases read the env file")
        self.assertNotIn("SOME_API_TOKEN", os.environ)


if __name__ == "__main__":
    unittest.main()
