"""--redact folding, joins, defined short names and notes; file sets; hat dealing and spread."""
import contextlib
import io
import json
import os
import re
import tomllib
from unittest import mock

from harness import CONTRACT, DRAFT, ChorusCase, chorus, hat_chat, hat_openai


def shared_role(use_case):
    """A shipped use case's shared_role, folded to one line as the loader folds it."""
    with open(os.path.join(chorus.BUILTIN_USE_CASES, use_case + ".toml"), "rb") as f:
        return " ".join(tomllib.load(f)["shared_role"].split())


class Helpers:
    def hatted(self):
        self.server.state["chat"] = hat_chat
        self.server.state["openai_fn"] = hat_openai

    def refused(self, text, terms):
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()) as e:
            chorus.redact(text, terms)
        self.assertEqual(cm.exception.code, 2, text)
        return e.getvalue()


class RedactionAndHatDealingTests(Helpers, ChorusCase):
    """Redaction joins and short names, file sets, criteria fit, and dealing hats."""

    def test_redaction_never_unblocks_a_credential(self):
        tok = "sk-proj-" + "Q" * 30
        path = self.draft(DRAFT + "debug: %s\n" % tok)
        code, out, err = self.run_cli("--sensitivity", "confidential", "--file", path,
                                      "--redact", "sk-proj", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("credential material", err)
        self.assertNotIn("Q" * 10, out + err)
        self.assertEqual(self.server.state["requests"], [])

    def test_redacted_file_sets_are_named_by_number_only(self):
        a = self.draft("This [COUNTERPARTY] master services agreement.\n", "acme_msa.md", sub="acme")
        b = self.draft("Statement of work for [COUNTERPARTY].\n", "acme_sow.md", sub="acme")
        code, out, err = self.run_cli("--use-case", "contracts", "--redacted", "--file", a,
                                      "--file", b, "--yes", "--json")
        self.assertEqual(code, 0, err)
        user = self.round1("/chat/completions")[0][2]["messages"][1]["content"]
        self.assertIn("=== FILE 1 ===\nThis [COUNTERPARTY]", user)
        self.assertIn("=== FILE 2 ===\nStatement", user)
        self.assertNotIn("acme", json.dumps([r[2] for r in self.server.state["requests"]]).lower())
        self.assertIn("FILE 1 = %s" % a, err)  # the mapping stays in the local plan

    def test_redaction_folds_variants_wraps_and_scripts(self):
        cases = [("O’Tallow Holdings signs.", "O'Tallow Holdings"),
                 ("Ac­me Corp signs.", "Acme Corp"),
                 ("José Núñez signs.", "José Núñez"),
                 ("José Núñez signs.", "Jose Nunez"),
                 ("ＡＣＭＥ Corp signs.", "ACME Corp"),
                 ("Acme\n  Corp signs.", "Acme Corp"),
                 ("甲株式会社乙", "株式会社")]
        for text, term in cases:
            out, counts, _ = chorus.redact(text, [term + "=X"])
            self.assertEqual(counts, [("[X]", 1)], (text, term))
            self.assertIn("[X]", out)
            self.assertEqual(out.count("\n"), text.count("\n"), (text, term))  # line numbers hold
        out, _, _ = chorus.redact("Acme\nCorp pays.", ["Acme Corp=X"])
        self.assertEqual(out, "[X]\n pays.")

    def test_redaction_is_longest_first_whatever_the_order_given(self):
        out, counts, _ = chorus.redact("Jane Roe met Jane.", ["Jane", "Jane Roe"])
        self.assertEqual(out, "[PARTY-2] met [PARTY-1].")
        self.assertEqual(counts, [("[PARTY-1]", 1), ("[PARTY-2]", 1)])

    def test_redaction_joins_refuse_and_ordinary_words_are_only_counted(self):
        # a multi-word name joined any way is redacted, not refused
        out, counts, _ = chorus.redact(
            "Acme Labs, AcmeLabs, acme-labs, acme_labs; mail legal@acme-labs.example or "
            "@acme_labs.", ["Acme Labs=X"])
        self.assertNotIn("acme", out.lower())
        self.assertEqual(counts, [("[X]", 6)])
        # a single-word name joined to other text is the name itself: refused, never named
        out, _, _ = chorus.redact("Zyxwv pays; see zyxwv.example.", ["Zyxwv"])  # "." bounds a word
        self.assertEqual(out, "[PARTY-1] pays; see [PARTY-1].example.")
        for text in ("Zyxwv pays. ZyxwvTech builds.", "Zyxwv pays; MyZyxwv app.",
                     "Zyxwv pays; zyxwv42 wallet."):
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()) as e:
                chorus.redact(text, ["Zyxwv"])
            self.assertEqual(cm.exception.code, 2, text)
            self.assertIn("glued to other text", e.getvalue())
            self.assertNotIn("Zyxwv", e.getvalue())
        # inside an ordinary word: left as is, counted for the plan
        for text, term in (("Chase will approve the purchase order.", "Chase"),
                           ("Mr. Stone signs at each milestone.", "Stone"),
                           ("Grant signs; Tallowmere granted the licence.", "Grant"),
                           ("Quarte pays on a quarterly schedule.", "Quarte"),
                           ("Acme and Acmeville.", "Acme")):
            out, _, notes = chorus.redact(text, [term])
            self.assertEqual(notes, [(1, "inside", 1)], text)
            self.assertTrue(out.startswith("[PARTY-1]") or "[PARTY-1]" in out, text)
        code, _, err = self.run_cli("--sensitivity", "confidential", "--file",
                                    self.draft("Chase signs the purchase order."), "--redact",
                                    "Chase", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("note: --redact term 1 also appears inside 1 longer word(s), domain(s)", err)
        self.assertNotIn("purchase", err)

    def test_tee_refusal_with_every_survivor_pinned_still_wears_every_hat(self):
        self.server.state["catalog"][1]["is_tee"] = False  # glm
        self.hatted()
        code, out, err = self.run_cli("--use-case", "contracts", "--file", self.draft(CONTRACT),
                                      "--redacted", "--hats", "kimi=counsel", "--yes", "--json")
        self.assertEqual(code, 6, err)
        plan = json.loads(out)["plan"]
        self.assertEqual(plan["hats"], {"kimi": ["counsel", "commercial", "compliance"]})
        self.assertEqual(plan["hats_unworn"], [])
        self.assertEqual(plan["hats_pinned"], ["kimi=counsel"])

    def test_a_seed_deals_the_same_whatever_the_voices_order(self):
        keys = list(self.use_cases()["copy"]["hats"])
        for seed in range(20):
            self.assertEqual(chorus.deal_hats(keys, ["glm", "kimi", "openai"], seed),
                             chorus.deal_hats(keys, ["openai", "kimi", "glm"], seed))
        lines = set()
        for order in ("glm,kimi", "kimi,glm"):
            code, _, err = self.run_cli("--use-case", "copy", "--sensitivity", "confidential",
                                        "--file", self.draft(), "--voices", order, "--seed", "5",
                                        "--dry-run")
            self.assertEqual(code, 0, err)
            lines.add([ln for ln in err.splitlines() if ln.strip().startswith("hats:")][0])
        self.assertEqual(len(lines), 1)

    def test_stop_restates_every_operator_word_flag(self):
        code, _, err = self.run_cli("--use-case", "copy", "--sensitivity", "public", "--file",
                                    self.draft(), "--rewrites")
        self.assertEqual(code, 2)
        self.assertIn("this copy is low-stakes and not going to a high-stakes reader (--rewrites)",
                      err)
        code, _, err = self.run_cli("--use-case", "startup", "--file", self.draft(), "--redacted")
        self.assertEqual(code, 2)
        self.assertIn("already a redacted copy (--redacted)", err)
        self.assertEqual(self.server.state["requests"], [])

    def test_hat_spread_counts_families_not_one_voices_extra_hats(self):
        f = lambda h: {"hat": h}
        self.assertEqual(chorus.hat_spread([("kimi", f("counsel")), ("kimi", f("compliance")),
                                            ("glm", f("?"))]), 1)
        self.assertEqual(chorus.hat_spread([("kimi", f("counsel")), ("glm", f("commercial"))]), 2)
        self.assertEqual(chorus.hat_spread([("kimi", f("counsel")), ("glm", f("counsel"))]), 1)
        self.assertEqual(chorus.hat_spread([("kimi", {}), ("glm", {})]), 0)

    def test_redaction_refuses_an_unredacted_defined_short_name(self):
        text = 'Between Tallowmere and Acme Corp ("Acme"). Acme pays.'
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()) as e:
            chorus.redact(text, ["Acme Corp=SPONSOR"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("defines a short name", e.getvalue())
        out, _, _ = chorus.redact(text, ["Acme Corp=SPONSOR", "Acme=SPONSOR"])
        self.assertNotIn("Acme", out)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            chorus.redact("Quorra Labs (“QL”) pays.", ["Quorra Labs"])  # initials
        out, _, _ = chorus.redact("Quorra Labs (“the Sponsor”) pays.", ["Quorra Labs=X"])
        self.assertEqual(out, "[X] (“the Sponsor”) pays.")  # a role, not a name

    def test_redaction_sorts_on_term_length_not_regex_length(self):
        out, counts, _ = chorus.redact("Signed by Jane R. and Jane.", ["Jane", "Jane R."])
        self.assertEqual(out, "Signed by [PARTY-2] and [PARTY-1].")
        self.assertEqual(counts, [("[PARTY-1]", 1), ("[PARTY-2]", 1)])
        out, _, _ = chorus.redact("Jean-P. signs; Jean agrees.", ["Jean-P.", "Jean"])
        self.assertEqual(out, "[PARTY-1] signs; [PARTY-2] agrees.")

    def test_hat_spread_takes_the_best_pick_per_family(self):
        f = lambda h: {"hat": h}
        group = [("kimi", f("a")), ("glm", f("a")), ("openai", f("b")), ("openai", f("c"))]
        self.assertEqual(chorus.hat_spread(group), 2)

    def test_criteria_runs_only_where_a_hat_reads_it(self):
        rfp = self.draft("Criteria: outcomes.", "rfp.md")
        for argv, extra in ((["--use-case", "contracts", "--redacted"], "unredacted"),
                            (["--use-case", "startup", "--redacted"], "unredacted"),
                            (["--use-case", "code", "--sensitivity", "confidential"], ""),
                            (["--use-case", "copy", "--sensitivity", "public"], "")):
            code, _, err = self.run_cli(*argv, "--file", self.draft(), "--criteria", rfp,
                                        "--dry-run")
            self.assertEqual(code, 1, argv)
            self.assertIn("Drop --criteria", err)
            self.assertIn(extra, err)
        self.hatted()
        cases = self.use_cases()  # the registry the run loads, held so it can be checked after
        with mock.patch.object(chorus, "load_use_cases", lambda *a, **k: cases):
            code, out, err = self.run_cli("--use-case", "copy", "--sensitivity", "internal",
                                          "--file", self.draft(), "--criteria", rfp, "--hats",
                                          "kimi=fundraiser", "--yes", "--json")
            self.assertEqual(code, 0, err)
            self.assertIn("fit", json.loads(out)["plan"]["briefs"])
            kimi = [b for _, _, b, _ in self.round1("/chat/completions") if "kimi" in b["model"]][0]
            self.assertIn("- fit: ", kimi["messages"][0]["content"])
            self.assertIn("Briefs: next-step, refute, fit.", kimi["messages"][0]["content"])
            code, out, _ = self.run_cli("--list-use-cases")
        self.assertIn("at internal: fundraiser also runs fit", out)
        self.assertEqual(cases["copy"]["hats"]["fundraiser"]["briefs"],
                         ["next-step", "refute"])  # the registry itself is never changed

    def test_file_sets_with_the_same_basename_stay_apart(self):
        a = self.draft("x = 1\n", "__init__.py", sub="pkg/a")
        b = self.draft("y = 2\n", "__init__.py", sub="pkg/b")
        code, out, err = self.run_cli("--use-case", "code", "--sensitivity", "confidential",
                                      "--file", a, "--file", b, "--yes")
        self.assertEqual(code, 0, err)
        user = self.round1("/chat/completions")[0][2]["messages"][1]["content"]
        self.assertIn("=== FILE 1: __init__.py ===\nx = 1", user)
        self.assertIn("=== FILE 2: __init__.py ===\ny = 2", user)
        self.assertNotIn(self.tmp.name, user)
        self.assertIn("# Chorus review — __init__.py (+1 more)", out)

    def test_file_label(self):
        self.assertEqual(chorus.file_label("src/a.py"), "src/a.py")
        self.assertEqual(chorus.file_label("./src/../a.py"), "a.py")
        self.assertEqual(chorus.file_label("../other/a.py"), "a.py")
        self.assertEqual(chorus.file_label("/abs/path/a.py"), "a.py")

    def test_contracts_hats_know_whose_side_they_are_on(self):
        self.hatted()
        uc = self.use_cases()["contracts"]
        side = shared_role("contracts")
        self.assertIn("[CLIENT]", side)
        for h in uc["hats"].values():
            self.assertTrue(h["role"].endswith(side), h["title"])
        code, out, err = self.run_cli("--use-case", "contracts", "--file", self.draft(CONTRACT),
                                      "--redacted", "--seed", "1", "--yes")
        self.assertEqual(code, 0, err)
        posts = self.posts("/chat/completions")
        self.assertTrue([b for _, _, b, _ in posts if "FINDINGS TO EXAMINE" in json.dumps(b)])
        for _, _, b, _ in posts:  # both rounds
            self.assertIn(side, b["messages"][0]["content"])
        self.assertIn("valuation or cap", uc["briefs"]["terms"])
        self.assertIn("never assume nonprofit or tax-exempt status",
                      uc["hats"]["compliance"]["role"])
        self.assertIn("never assume it is a nonprofit", side)  # every hat

    def test_code_on_public_restates_the_security_fix_rule(self):
        path = self.draft("def f():\n    pass\n", "f.py")
        code, _, err = self.run_cli("--use-case", "code", "--sensitivity", "public", "--file",
                                    path, "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("check: for code, public means open-source code. An unmerged security fix "
                      "stays confidential", err)
        code, _, err = self.run_cli("--use-case", "code", "--sensitivity", "public", "--file", path)
        self.assertEqual(code, 2)
        self.assertIn("An unmerged security fix stays confidential", err.split("STOP:", 1)[1])
        code, _, err = self.run_cli("--use-case", "code", "--sensitivity", "confidential",
                                    "--file", path, "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertNotIn("  check: ", err)
        self.assertNotIn("open-source code", err)

    def test_deal_hats_when_every_hat_is_pinned_to_one_voice(self):
        keys = list(self.use_cases()["contracts"]["hats"])
        for seed in range(10):
            dealt, unworn = chorus.deal_hats(keys, ["kimi", "glm"], seed,
                                             [("kimi", h) for h in keys])
            self.assertEqual(dealt["kimi"], keys)
            self.assertEqual(len(dealt["glm"]), 1)  # the free voice still wears a hat
            self.assertIn(dealt["glm"][0], keys)
            self.assertEqual(unworn, [])

    def test_report_records_redaction_pins_and_redeal(self):
        self.server.state["catalog"][1]["is_tee"] = False  # glm
        self.hatted()
        code, out, err = self.run_cli("--use-case", "contracts", "--file", self.draft(CONTRACT),
                                      "--redact", "Acme Corp=SPONSOR", "--hats", "kimi=counsel",
                                      "--yes")
        self.assertEqual(code, 6, err)
        self.assertIn("redacted in memory before sending: [SPONSOR] ×2", out)
        self.assertIn("_hats re-dealt after glm was refused at the TEE check_", out)
        self.assertIn("pinned kimi=counsel", out)
        self.assertIn("The same seed, voices and pins re-deal the same hats", out)

    def test_dry_run_says_how_to_hold_the_deal_past_midnight(self):
        path = self.draft()
        code, _, err = self.run_cli("--use-case", "copy", "--sensitivity", "confidential",
                                    "--file", path, "--dry-run")
        self.assertEqual(code, 0, err)
        seed = re.search(r"seed (\w+), from the draft \+ date", err).group(1)
        self.assertIn("to hold them past midnight, dry-run again with --seed %s" % seed, err)
        code, _, err = self.run_cli("--use-case", "copy", "--sensitivity", "confidential",
                                    "--file", path, "--seed", seed, "--dry-run")
        self.assertNotIn("past midnight", err)


class RedactFoldingTests(Helpers, ChorusCase):
    """Folding invisibles, case and scripts; defined-term layouts; figures a rewrite adds; the
    STOP wording; file numbering; the hat redeal."""

    def test_an_invisible_character_still_bounds_a_short_term(self):
        for ch in ("­", "​", "﻿", "️", "‍", "‌", "⁠", "͏"):
            text = "QL lends.\nThe QL%sfund and Acme%sLabs.\n" % (ch, ch)
            out, counts, _ = chorus.redact(text, ["QL=LENDER", "Acme=CP"])
            self.assertNotIn("QL", out, repr(ch))
            self.assertNotIn("Acme", out, repr(ch))
            self.assertEqual(counts, [("[LENDER]", 2), ("[CP]", 1)], repr(ch))
            self.assertEqual(out.count("\n"), 2)
        out, _, _ = chorus.redact("A‍cme and Ac⁠me sign.", ["Acme=X"])
        self.assertEqual(out.count("[X]"), 2)

    def test_redaction_folds_case_letters_symbols_and_apostrophes(self):
        cases = [("Weißfern GmbH signs. WEISSFERN GMBH, by its director.",
                  "Weißfern GmbH", 2),
                 ("Łukasz Nowak signs; lukasz nowak, director.", "Łukasz Nowak", 2),
                 ("Søren Holm signs; Soren Holm, director.", "Søren Holm", 2),
                 ("O´Tallow Holdings and OʼTallow Holdings.", "O'Tallow Holdings", 2),
                 ("Hollis Packard and HOLLIS-PACKARD.", "Hollis-Packard", 2),
                 ("Acme™ sells; Acme pays.", "Acme", 2)]
        for text, term, n in cases:
            out, counts, _ = chorus.redact(text, [term + "=X"])
            self.assertEqual(counts, [("[X]", n)], text)

    def test_redaction_across_scripts(self):
        out, counts, _ = chorus.redact("Acme株式会社 and Acme Corp sign.",
                                       ["Acme=X"])
        self.assertEqual(counts, [("[X]", 2)])
        out, _, _ = chorus.redact("本协议由Quorra Labs与 Tallowmere "
                                  "签署", ["Quorra Labs=X"])
        self.assertIn("由[X]与", out)
        out, counts, _ = chorus.redact("김하 대표와 김한 이사",
                                       ["김하=X"])
        self.assertEqual(out, "[X] 대표와 김한 이사")  # never a different syllable
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            chorus.redact("이사가 있다.", ["이=X"])  # one syllable: too short
        self.assertEqual(cm.exception.code, 1)

    def test_defined_short_name_layouts_are_caught(self):
        layouts = ['between Tallowmere and Acme\nRobotics, Inc. (“Acme”). Acme pays.',
                   'between Tallowmere and Acme Robotics,\nInc. ("Acme"). Acme pays.',
                   "Acme Robotics Ltd (‘Acme’) pays.",
                   "Acme Robotics Ltd ('Acme') pays.",
                   'Acme Robotics Limited (company number 01234567) ("Acme") pays.',
                   'Acme Robotics Corporation, a corporation organized and existing under the laws '
                   'of the State of Delaware, with its principal office in Wilmington ("Acme") pays.',
                   "Acme Robotics GmbH („Acme“) zahlt.",
                   "Acme Robotics SA («Acme») paie."]
        for text in layouts:
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()) as e:
                chorus.redact(text, ["Acme Robotics=CP"])
            self.assertEqual(cm.exception.code, 2, text)
            self.assertIn("defines a short name", e.getvalue())
            self.assertNotIn("Acme", e.getvalue())
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            chorus.redact('The Ministry of Data Archives ("MODA") pays.', ["Ministry of Data Archives"])

    def test_generic_defined_terms_are_not_refused(self):
        for text, term in (('Orrery Foundation reviews Schedule 1 (the "Statement of Work").',
                            "Orrery Foundation"),
                           ('Orrery Foundation follows (the "Code of Conduct").', "Orrery Foundation"),
                           ('Tallow Vale AV Services Inc provides AV (the "Services").',
                            "Tallow Vale AV Services Inc"),
                           ('Acme Software Inc licenses it (the "Software").', "Acme Software Inc"),
                           ('Gitwell Grants funds it (the "Grants").', "Gitwell Grants"),
                           ('Fernvale Catering Co serves (the "Catering Services").',
                            "Fernvale Catering Co")):
            out, _, _ = chorus.redact(text, [term + "=X"])
            self.assertIn("[X]", out, text)

    def test_rewrite_figures_compare_as_whole_numbers(self):
        f = {"brief": "next-step", "severity": "low", "quote": "may cancel at any time",
             "issue": "x", "direction": "y", "hat": "copywriter"}
        nd = chorus.norm(CONTRACT)  # carries 50,000 and 90
        for rewrite, new in (("Pay within 9 days.", True), ("Pay 5,000 now.", True),
                             ("Pay 50,000 within 90 days.", False), ("Pay 50000 now.", False),
                             ("Pay within 900 days.", True)):
            n = chorus.normalize_findings({"findings": [dict(f, rewrite=rewrite)]}, nd,
                                          ["copywriter"], True)[0]
            self.assertEqual(n["rewrite_new_figures"], new, rewrite)

    def test_stop_restates_allow_flagged_too(self):
        email = "jane.roe" + "@" + "acme-sponsor.example"
        code, _, err = self.run_cli("--sensitivity", "confidential", "--file",
                                    self.draft(DRAFT + "Signatory: %s\n" % email), "--allow-flagged")
        self.assertEqual(code, 2)
        self.assertIn("they checked each flagged line (--allow-flagged)", err)

    def test_never_send_criteria_is_refused_before_the_fit_check(self):
        held = os.path.join(self.tmp.name, "data", "held")
        crit = self.draft("Criteria.", "crit.md", sub=os.path.join("data", "held"))
        with mock.patch.dict(os.environ, {"CHORUS_NEVER_SEND": held}):
            code, _, err = self.run_cli("--use-case", "contracts", "--file", self.draft(),
                                        "--redacted", "--criteria", crit, "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("REFUSED: --criteria %s is on the never-send list" % crit, err)
        self.assertNotIn("Drop --criteria", err)  # the fit check never ran

    def test_a_redact_run_names_files_by_number_too(self):
        a = self.draft("Acme Corp signs the order.\n", "acme_msa.md", sub="acme")
        b = self.draft("Acme Corp pays.\n", "acme_sow.md", sub="acme")
        code, out, err = self.run_cli("--use-case", "contracts", "--redact", "Acme Corp=CP",
                                      "--file", a, "--file", b, "--yes")
        self.assertEqual(code, 0, err)
        user = self.round1("/chat/completions")[0][2]["messages"][1]["content"]
        self.assertIn("=== FILE 1 ===\n[CP] signs", user)
        self.assertNotIn("acme", json.dumps([r[2] for r in self.server.state["requests"]]).lower())

    def test_hat_spread_drives_the_report_order_and_label(self):
        def f(hat, sev, quote="may cancel at any time"):
            return {"brief": "terms", "severity": sev, "quote": quote, "issue": hat,
                    "direction": "", "hat": hat, "spans": chorus.quote_spans(quote, nd)}
        nd = chorus.norm(CONTRACT)
        results = {
            "kimi": {"voice": "kimi", "family": "k", "model": "m", "host": "h",
                     "findings": [f("counsel", "high"), f("commercial", "high"),
                                  f("compliance", "low", "governed by the laws of the Cayman Islands")]},
            "glm": {"voice": "glm", "family": "g", "model": "m", "host": "h",
                    "findings": [f("counsel", "high"),
                                 f("commercial", "low", "governed by the laws of the Cayman Islands")]}}
        groups = chorus.agreements(results)
        self.assertEqual([chorus.hat_spread(g) for g in groups], [2, 2])
        self.assertEqual(groups[0][0][1]["severity"], "high")  # equal spread: severity decides
        results["glm"]["findings"][0]["hat"] = "?"
        results["kimi"]["findings"] = results["kimi"]["findings"][:2]
        g, = chorus.agreements(results)
        self.assertEqual(chorus.hat_spread(g), 1)  # kimi's two hats alone are not two families
        plan = {"file": "c.md", "sensitivity": "confidential", "briefs": ["terms"],
                "criteria": None}
        self.assertNotIn("different hats", chorus.render(plan, results, [g]))

    def test_redeal_refreshes_the_pins_of_a_refused_voice(self):
        self.server.state["catalog"][1]["is_tee"] = False  # glm
        self.hatted()
        code, out, err = self.run_cli("--use-case", "contracts", "--file", self.draft(CONTRACT),
                                      "--redacted", "--hats", "kimi=counsel,glm=commercial",
                                      "--yes", "--json")
        self.assertEqual(code, 6, err)
        plan = json.loads(out)["plan"]
        self.assertEqual(plan["hats_pinned"], ["kimi=counsel"])
        self.assertEqual(plan["hats"], {"kimi": ["counsel", "commercial", "compliance"]})


class RedactPatternTests(Helpers, ChorusCase):
    """The --redact pattern and its notes."""

    def test_a_term_with_its_own_punctuation_matches_its_own_text(self):
        safe = ('This SAFE is made by Acme Robotics, Inc., a Delaware corporation.\n'
                'ACME ROBOTICS INC.\nBy: ______\n')
        for term in ("Acme Robotics, Inc.", "Acme Robotics Inc", "Acme Robotics, Inc"):
            out, counts, _ = chorus.redact(safe, [term + "=COMPANY"])
            self.assertEqual(counts, [("[COMPANY]", 2)], term)
            self.assertNotIn("acme", out.lower(), term)
        out, counts, _ = chorus.redact("Smith & Jones LLP signs. Smith&Jones agrees.",
                                       ["Smith & Jones=CP"])
        self.assertEqual(counts, [("[CP]", 2)])
        out, _, _ = chorus.redact("Witnesses: Smith, Jones and Brown. Smith Jones signs.",
                                  ["Smith Jones=CP"])
        self.assertIn("Smith, Jones and Brown", out)  # a list of names is not the name
        out, counts, _ = chorus.redact("Zyxwv Labs, Inc. pays; Zyxwv Labs, Inc. signs.",
                                       ["Zyxwv Labs, Inc.=X"])
        self.assertEqual(counts, [("[X]", 2)])

    def test_a_terms_leading_punctuation_is_not_part_of_the_match(self):
        text = "Contact @janesmith or janesmith; mail ops@acme.example; price $5; $ZYXW trades."
        out, counts, _ = chorus.redact(text, ["@janesmith=HANDLE", "$ZYXW=TICKER"])
        self.assertEqual(counts, [("[HANDLE]", 2), ("[TICKER]", 1)])
        self.assertIn("ops@acme.example", out)
        self.assertIn("$5", out)

    def test_links_escapes_and_hyphenated_line_breaks_are_redacted(self):
        text = ("Deck: [pitch](Acme%20Robotics%20Pitch%20Deck.pdf). Handle jane\\_roe.\n"
                "Acme Robo-\ntics will pay. Jane Roe signs.\n")
        out, counts, _ = chorus.redact(text, ["Acme Robotics=CO", "Jane Roe=FOUNDER"])
        self.assertEqual(counts, [("[CO]", 2), ("[FOUNDER]", 2)])
        self.assertNotIn("Robo", out)
        self.assertEqual(out.count("\n"), text.count("\n"))

    def test_a_first_name_left_standing_is_noted(self):
        memo = "Jane Roe founded Acme. Jane built the prototype; Roe raised the seed.\n"
        out, _, notes = chorus.redact(memo, ["Jane Roe=FOUNDER"])
        self.assertEqual(notes, [(1, "alone", 2)])
        out, _, notes = chorus.redact(memo, ["Jane Roe=FOUNDER", "Jane=FOUNDER", "Roe=FOUNDER"])
        self.assertEqual(notes, [])
        code, _, err = self.run_cli("--use-case", "startup", "--file", self.draft(memo),
                                    "--redact", "Jane Roe=FOUNDER", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("has 2 capitalised word(s) or acronym(s) of it standing alone", err)
        self.assertNotIn("Jane", err)

    def test_ordinary_words_in_urls_and_slashes_do_not_refuse(self):
        cases = [("Wei Lin built it. Lin is CTO. [Linkfolio](https://www.linkfolio.example/in/"
                  "wei-lin); onchain/offchain, online/offline.", ["Wei Lin=F", "Lin=F"]),
                 ("Ron Patel built it. Ron met us at https://tallowmere.example/frontier.",
                  ["Ron Patel=F", "Ron=F"]),
                 ("Ana Lima signs. Ana reviews https://example.com/management.pdf.",
                  ["Ana Lima=F", "Ana=F"]),
                 ("**8. Confidentiality.** Ali Khan signs; Ali agrees.", ["Ali Khan=C", "Ali=C"])]
        for text, terms in cases:
            out, _, notes = chorus.redact(text, terms)
            self.assertTrue(all(kind == "inside" for _, kind, _ in notes), (text, notes))

    def test_defined_short_names_more_layouts(self):
        for text in ("甲方：Zyxwv Labs Ltd.（以下简称“Zyxwv"
                     "”）\nZyxwv应支付。",
                     "between Tallowmere and Acme Robotics\n\n(“Acme”), a Delaware "
                     "corporation."):
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
                chorus.redact(text, ["Zyxwv Labs Ltd.=SPONSOR"] if "Zyxwv" in text
                              else ["Acme Robotics=CP"])
            self.assertEqual(cm.exception.code, 2, text)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            chorus.redact('Larch Foundation for the Open Web ("LFOW") pays.',
                          ["Larch Foundation for the Open Web=SPONSOR"])

    def test_defined_terms_that_merely_share_a_word_are_not_refused(self):
        cases = [('Larch Foundation for the Open Web ("LFOW" or "Sponsor").\n\n'
                  '2. Benefits. LFOW will receive logo placement on the Event web page (the "Web Page").',
                  ["Larch Foundation for the Open Web=SPONSOR", "LFOW=SPONSOR"]),
                 ('Open Signal Collective ("OSC") hosts it under a permissive license '
                  '(the "Open Source License").', ["Open Signal Collective=HOST", "OSC=HOST"]),
                 ('Jordan Price shall pay $0.0001 per share (the "Purchase Price").',
                  ["Jordan Price=INVESTOR"]),
                 ('Lumen Stack ("Partner") will co-host a track at the conference (the '
                  '"Lumen Berlin Conference").', ["Lumen Stack=PARTNER"]),
                 ("The Company's counsel reviews Acme Robotics' terms (the 'Review').",
                  ["Acme Robotics=CP"])]
        for text, terms in cases:
            out, _, _ = chorus.redact(text, terms)
            self.assertIn("[", out, text)


class ShortNameAndRankingTests(Helpers, ChorusCase):
    """Short names after a legal suffix, acronyms, rare invisibles, header figures, ranking."""

    def test_a_suffixed_legal_name_still_catches_its_short_name(self):
        self.refused('Quorra Labs, Inc. ("QL") hosts the venue.\n\nQL may invoice.',
                     ["Quorra Labs, Inc.=VENDOR"])
        self.refused('Quorra Labs, Inc. ("Quorra Labs") hosts the venue.', ["Quorra Labs, Inc.=VENDOR"])
        self.refused('Alliance Network LLC ("Alliance Network") invests.',
                     ["Alliance Network LLC=INVESTOR"])  # every word generic
        self.refused('Cobalt Lab, Inc. ("CL") incubates it.', ["Cobalt Lab, Inc.=STUDIO"])
        self.refused('Bank of Avalon ("BofA") lends.', ["Bank of Avalon=BANK"])

    def test_an_unquoted_acronym_definition_or_acronym_left_standing(self):
        err = self.refused("Open Shale Network (OSN) is raising a seed. OSN has three founders.",
                           ["Open Shale Network=STARTUP"])
        self.assertIn("(line 1)", err)
        self.refused("The studio books Quorra Labs (QL) for catering. QL confirmed the menu.",
                     ["Quorra Labs=VENDOR"])
        _, _, notes = chorus.redact("Open Shale Network is raising. OSN has three founders.",
                                    ["Open Shale Network=STARTUP"])
        self.assertEqual(notes, [(1, "alone", 1)])
        _, _, notes = chorus.redact("Open Shale Network signs. The osn files are here.",
                                    ["Open Shale Network=STARTUP"])
        self.assertEqual(notes, [])  # lowercase letters are not the acronym

    def test_quote_pairs_after_a_redacted_short_name(self):
        self.refused('Acme Robotics ("ARI" or "Acme") pays.', ["Acme Robotics=X", "ARI=X"])
        out, _, _ = chorus.redact('Capital Partners ("CP") lends. CP commits more (the "Capital").',
                                  ["Capital Partners=LENDER", "CP=LENDER"])
        self.assertIn('(the "Capital")', out)  # the money, defined after the short name

    def test_a_short_name_inside_an_ordinary_word_with_digits_is_a_note(self):
        _, _, notes = chorus.redact("Ken Ito founded it. Ken met us at Tokenville2049 and "
                                    "TokenTroupe.", ["Ken Ito=FOUNDER", "Ken=FOUNDER"])
        self.assertEqual(notes, [(2, "inside", 2)])
        err = self.refused("Zyxwv pays.\nzyxwv42 is its wallet.", ["Zyxwv"])
        self.assertIn("term 1 (line 2)", err)

    def test_an_email_at_a_redacted_domain_stays_flagged(self):
        email = "jane.doe" + "@" + "acme-labs.example"
        path = self.draft("Acme Labs pays. Write to %s.\n" % email)
        code, _, err = self.run_cli("--use-case", "contracts", "--file", path, "--redact",
                                    "Acme Labs=SPONSOR", "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("email address", err)
        code, _, err = self.run_cli("--use-case", "contracts", "--file", path, "--redact",
                                    "Acme Labs=SPONSOR", "--redact", email + "=EMAIL", "--dry-run")
        self.assertEqual(code, 0, err)

    def test_rare_invisibles_and_html_spaces_fold(self):
        for ch in ("ㅤ", "ᅟ", "឴", "᠋", "\U000e0100"):
            out, counts, _ = chorus.redact("Ac%sme Robotics signs; Acme Robotics pays." % ch,
                                           ["Acme Robotics=X"])
            self.assertEqual(counts, [("[X]", 2)], hex(ord(ch)))
        out, counts, _ = chorus.redact("Acme&nbsp;Robotics signs; Acme Robotics pays.",
                                       ["Acme Robotics=X"])
        self.assertEqual(counts, [("[X]", 2)])

    def test_file_headers_carry_no_figures(self):
        nd = chorus.norm("=== FILE 1: 2030-01-22-post.md ===\nWe ran events.\n\n=== FILE 2 ===\n"
                         "More.\n")
        f = {"brief": "next-step", "severity": "low", "quote": "we ran events", "issue": "x",
             "direction": "y", "hat": "copywriter", "rewrite": "We ran 2 events on the 22nd."}
        n = chorus.normalize_findings({"findings": [f]}, nd, ["copywriter"], True)[0]
        self.assertTrue(n["rewrite_new_figures"])

    def test_agreements_rank_by_hat_spread_not_raw_hats(self):
        nd = chorus.norm(CONTRACT)

        def f(hat, quote):
            return {"brief": "terms", "severity": "low", "quote": quote, "issue": hat,
                    "direction": "", "hat": hat, "spans": chorus.quote_spans(quote, nd)}
        q1, q2 = "may cancel at any time", "governed by the laws of the Cayman Islands"
        results = {"kimi": {"findings": [f("counsel", q1), f("commercial", q1), f("counsel", q2)]},
                   "glm": {"findings": [f("?", q1), f("compliance", q2)]}}
        groups = chorus.agreements(results)
        self.assertEqual(groups[0][0][1]["quote"], q2)  # two families, two hats: first
