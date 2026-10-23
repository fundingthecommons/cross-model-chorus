"""The sensitivity ladder and TEE receipts, the never-send list, and the content guards."""
import json
import os
import threading
from types import SimpleNamespace
from unittest import mock

from harness import DRAFT, KIMI_PINS, ChorusCase, chorus, provider_for

ORG_DOMAIN = "tallowmere.example"


class LadderAndReceiptTests(ChorusCase):
    """The routing ladder and the receipt checks on every TEE call."""

    def test_confidential_refuses_a_frontier_voice_without_sending(self):
        code, _, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                    "--voices", "openai", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("attested-TEE voices only", err)
        self.assertEqual(self.server.state["requests"], [])

    def test_confidential_default_is_tee_only_with_zdr_aci_and_verified_receipts(self):
        code, out, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                      "--yes", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(json.loads(out)["results"]), ["glm", "kimi"])
        self.assertEqual(self.posts("/responses"), [])
        for _, _, b, _ in self.posts("/chat/completions"):  # both rounds
            self.assertEqual(b["provider"], provider_for(b["model"], confidential=True))
        self.assertEqual(len(self.reqs("GET", "/aci/receipts/")),
                         len(self.posts("/chat/completions")))  # every send is receipt-checked

    def test_internal_kimi_not_tee_in_catalog_is_never_sent(self):
        self.server.state["catalog"][0]["is_tee"] = False
        code, _, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                    "--voices", "kimi", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("REFUSED kimi", err)
        self.assertEqual(self.posts("/chat/completions"), [])

    def test_mixed_or_missing_is_tee_refuses_and_string_true_passes(self):
        cat = self.server.state["catalog"]
        cat[1]["providers"] = [{"is_tee": True}, {"is_tee": False}]
        code, _, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                    "--voices", "glm", "--yes")
        self.assertEqual(code, 2)
        self.assertEqual(self.posts("/chat/completions"), [])
        cat[1]["providers"] = []
        del cat[1]["is_tee"]
        code, _, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                    "--voices", "glm", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("no is_tee field", err)
        cat[1]["is_tee"] = "true"
        code, _, _ = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                  "--voices", "glm", "--yes")
        self.assertEqual(code, 0)

    def test_catalog_fetch_error_refuses(self):
        self.server.state["catalog_status"] = 500
        code, _, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                    "--yes")
        self.assertEqual(code, 2)
        self.assertIn("catalog fetch failed", err)
        self.assertEqual(self.posts("/chat/completions"), [])

    def receipt_case(self, receipt=None, cls="confidential", **state):
        if receipt is not None:
            self.server.state["receipt"] = receipt
        self.server.state.update(state)
        return self.run_cli("--sensitivity", cls, "--file", self.draft(), "--voices", "kimi",
                            "--yes")

    def test_receipt_cases_that_must_exit_5(self):
        ev = lambda **k: {"event_log": [dict({"type": "upstream.verified", "result": "verified",
                                              "required": True, "session_id": KIMI_PINS[0]},
                                             **k)]}
        cases = {"verified but not required": ev(required=False),
                 "required but failed": ev(result="failed"),
                 "no session": ev(session_id=""),
                 "wrong model": dict(ev(), model="moonshotai/kimi-k2.6"),
                 "another request": dict(ev(), receipt_id="rcpt-someone-else"),
                 "ends on a failed hop": {"event_log": [
                     ev()["event_log"][0], dict(ev()["event_log"][0], result="failed")]},

                 "forwarded to a failed hop": {"event_log": [
                     {"type": "route.selected"}, {"type": "request.forwarded"},
                     dict(ev()["event_log"][0], result="failed"),
                     {"type": "route.selected"}, {"type": "request.forwarded"},
                     ev()["event_log"][0]]},
                 "forwarded hop never attested": {"event_log": [
                     {"type": "route.selected"}, {"type": "request.forwarded"},
                     ev()["event_log"][0],
                     {"type": "route.selected"}, {"type": "request.forwarded"},
                     {"type": "response.received"}]}}
        for label, receipt in cases.items():  # the receipt says "not confidential": always 5
            for cls in ("confidential", "internal"):
                code, _, err = self.receipt_case(receipt, cls=cls)
                self.assertEqual(code, 5, "%s / %s" % (label, cls))
                self.assertIn("CONFIDENTIALITY NOT PROVEN", err)

    def test_unreadable_receipt_stops_confidential_but_warns_otherwise(self):
        cases = {"no upstream event": {"event_log": [{"type": "other"}]},
                 "list-shaped": [1, 2], "bad event_log": {"event_log": 5}}
        for label, receipt in cases.items():
            code, _, err = self.receipt_case(receipt, cls="confidential")
            self.assertEqual(code, 5, label)
            code, _, err = self.receipt_case(receipt, cls="internal")
            self.assertEqual(code, 6, label)  # outside confidential, a missing receipt warns
            self.assertIn("TEE RECEIPT MISSING", err)
            self.assertNotIn("CONFIDENTIALITY NOT PROVEN", err)

    def test_receipt_fetch_404_exits_5_after_three_tries(self):
        code, out, _ = self.receipt_case(receipt_status=404, cls="confidential")
        self.assertEqual(code, 5)
        self.assertIn("could not be fetched", out)
        # every backoff step tries the header id and the chat id
        self.assertEqual(len(self.reqs("GET", "/aci/receipts/")),
                         2 * len(chorus.RECEIPT_BACKOFF))

    def test_receipt_found_by_response_id_without_header(self):
        code, _, _ = self.receipt_case(no_receipt_header=True)
        self.assertEqual(code, 0)
        code, out, _ = self.receipt_case(no_receipt_header=True, omit_id=True)
        self.assertEqual(code, 5)  # receipt_case defaults to sensitivity confidential
        self.assertIn("no receipt id", out)

    def test_503_under_attestation_is_not_a_send(self):
        self.server.state["chat_status"] = {"z-ai/glm-5.3": 503}
        code, out, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                      "--yes", "--json")
        self.assertEqual(code, 6)
        glm = json.loads(out)["results"]["glm"]
        self.assertFalse(glm["sent"])
        # pinned, but the gateway did not say the pinned sessions were stale
        self.assertIn("no attested upstream", glm["error"])
        self.assertNotIn("no longer live", glm["error"])
        self.assertNotIn("CONFIDENTIALITY", err)
        code, out, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                      "--yes", "--json")  # unpinned: no attested upstream found
        glm = json.loads(out)["results"]["glm"]
        self.assertFalse(glm["sent"])
        self.assertIn("no attested upstream", glm["error"])

    def test_5xx_after_send_without_receipt_exits_5(self):
        self.server.state["chat_status"] = {"z-ai/glm-5.3": 500}
        code, _, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                    "--yes")
        self.assertEqual(code, 5)
        self.assertIn("CONFIDENTIALITY NOT PROVEN for glm", err)
        self.server.state["error_receipt"] = True  # a verifiable receipt on the error: proven
        code, _, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                    "--yes")
        self.assertEqual(code, 6)

    def test_timeout_after_send_exits_5(self):
        self.server.state["delay"] = {"moonshotai/kimi-k3": 2}
        code, out, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                      "--yes", "--timeout", "1", "--json")
        self.assertEqual(code, 5)
        kimi = json.loads(out)["results"]["kimi"]
        self.assertTrue(kimi["sent"])
        self.assertIn("never started after 1s (no response headers)", kimi["error"])

    def after_send(self):
        """A confidential GLM + Kimi run, round 1 only: (exit code, results, stderr)."""
        code, out, err = self.run_cli("--sensitivity", "confidential", "--file", self.draft(),
                                      "--voices", "glm,kimi", "--yes", "--no-cross-examine",
                                      "--json")
        return code, json.loads(out)["results"], err

    def test_http_400_whose_receipt_never_resolves_is_sent_and_unproven(self):
        self.server.state.update(chat_status={"z-ai/glm-5.3": 400}, error_receipt=True,
                                 missing_ids={"rcpt-z-ai-glm-5.3"})
        code, res, err = self.after_send()
        self.assertEqual(code, 5, err)
        glm = res["glm"]
        self.assertTrue(glm["sent"])  # the receipt cannot show it was not forwarded
        self.assertEqual(glm["receipt"], "unverified")
        self.assertIn("HTTP 400", glm["error"])
        self.assertIn("could not be fetched", glm["receipt_detail"])
        self.assertEqual(len(self.reqs("GET", "/aci/receipts/rcpt-z-ai-glm-5.3")),
                         len(chorus.RECEIPT_BACKOFF))  # every backoff step asked again
        self.assertEqual(res["kimi"]["receipt"], "verified")
        self.assertIn("CONFIDENTIALITY NOT PROVEN for glm", err)
        self.assertNotIn("kimi", [ln for ln in err.splitlines()
                                  if "CONFIDENTIALITY NOT PROVEN" in ln][0])

    def test_a_receipt_check_that_raises_leaves_the_receipt_unverified(self):
        with mock.patch.object(chorus, "verify_receipt",
                               side_effect=RuntimeError("the check broke")):
            code, res, err = self.after_send()
        self.assertEqual(code, 5, err)
        for n in ("glm", "kimi"):
            self.assertTrue(res[n]["sent"], n)
            self.assertEqual(res[n]["receipt"], "unverified", n)
            self.assertIn("the receipt check itself failed (RuntimeError)",
                          res[n]["receipt_detail"])
        self.assertIn("CONFIDENTIALITY NOT PROVEN", err)

    def test_a_response_that_cannot_be_read_after_sending_is_unproven(self):
        real = chorus.call_voice
        for garbled in (None, ["not", "a", "response"], {"text": "{}"}):
            def call(v, *a, garbled=garbled, **k):
                out = real(v, *a, **k)  # the request really goes out
                return garbled if v["model"].startswith("z-ai/") else out

            sent = len(self.posts("/chat/completions"))
            with mock.patch.object(chorus, "call_voice", call):
                code, res, err = self.after_send()
            self.assertEqual(code, 5, garbled)
            self.assertEqual([b["model"] for _, _, b, _ in self.posts("/chat/completions")[sent:]
                              if b["model"].startswith("z-ai/")], ["z-ai/glm-5.3"])
            glm = res["glm"]
            self.assertTrue(glm["sent"], garbled)
            self.assertIn("could not read the response", glm["error"])
            self.assertEqual(glm["receipt"], "unverified", garbled)
            self.assertIn("the response could not be checked", glm["receipt_detail"])
            self.assertEqual(res["kimi"]["receipt"], "verified", garbled)
            self.assertIn("CONFIDENTIALITY NOT PROVEN for glm", err)


class NeverSendTests(ChorusCase):
    """The never-send list, and the stops before anything is sent."""

    def test_never_send_case_variants_symlinks_and_pipes(self):
        # ~/.ssh is on the default list; ~/vault and ~/Ledger (a case variant of the ledger
        # folder) are named by CHORUS_NEVER_SEND.
        os.environ["CHORUS_NEVER_SEND"] = os.pathsep.join(
            ["~/vault", os.path.join(self.home, "Ledger")])
        never = chorus.load_config(SimpleNamespace())["never_send"]
        for root in (".ssh", "vault", "ledger"):
            os.makedirs(os.path.join(self.home, root, "notes"))
            with open(os.path.join(self.home, root, "notes", "x.md"), "w") as f:
                f.write("x")
            for variant in (root, root.upper(), root.capitalize()):
                path = os.path.join(self.home, variant, "notes", "x.md")
                if not os.path.exists(path):
                    continue  # a case-sensitive filesystem: a different, absent path
                self.assertTrue(chorus.never_send_reason(path, never), path)
        for sibling in ("vaultstorm", ".sshkeys"):  # a near-miss name is not on the list
            self.draft("x", "x.md", sub="home/" + sibling)
            self.assertEqual(chorus.never_send_reason(
                os.path.join(self.home, sibling, "x.md"), never), "", sibling)
        os.symlink(os.path.join(self.home, "vault"), os.path.join(self.tmp.name, "pub"))
        code, _, err = self.run_cli("--sensitivity", "internal", "--file",
                                    os.path.join(self.tmp.name, "pub", "notes", "x.md"), "--yes")
        self.assertEqual(code, 2)
        self.assertIn("never-send list", err)
        r, w = os.pipe()
        try:
            self.assertIn("not a regular file", chorus.never_send_reason("/dev/fd/%d" % r, never))
        finally:
            os.close(r)
            os.close(w)
        self.assertEqual(self.server.state["requests"], [])

    def test_a_fifo_is_refused_without_being_opened(self):
        fifo = os.path.join(self.tmp.name, "inbox.md")
        os.mkfifo(fifo)
        self.assertFalse(os.path.realpath(fifo).startswith("/dev/"))  # refused by its type
        self.assertIn("not a regular file", chorus.never_send_reason(fifo))
        for argv in (["--file", fifo], ["--file", self.draft(), "--criteria", fifo]):
            box = {}
            t = threading.Thread(target=lambda argv=argv: box.update(res=self.run_cli(
                "--sensitivity", "internal", *argv, "--yes")), daemon=True)
            t.start()
            t.join(10)
            if t.is_alive():  # the run opened the pipe and is waiting on a writer: release it
                try:
                    os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
                except OSError:
                    pass
                t.join(10)
                self.fail("the run blocked opening the FIFO (%s)" % argv[-2])
            code, _, err = box["res"]
            self.assertEqual(code, 2, argv)
            self.assertIn("REFUSED: %s %s is on the never-send list" % (argv[-2], fifo), err)
            self.assertIn("not a regular file", err)
        self.assertEqual(self.server.state["requests"], [])

    def test_a_path_whose_real_path_leads_elsewhere_is_refused(self):
        path = self.draft("x", "shown.md")
        other = self.draft("y", "other.md")  # an existing regular file, another inode
        real = os.path.realpath

        def realpath(p, **k):
            return real(other, **k) if p == path else real(p, **k)

        with mock.patch.object(chorus.os.path, "realpath", realpath):
            self.assertIn("does not lead back", chorus.never_send_reason(path))
            code, _, err = self.run_cli("--sensitivity", "internal", "--file", path, "--yes")
        self.assertEqual(code, 2)
        self.assertIn("its real path does not lead back to the file", err)
        self.assertEqual(self.server.state["requests"], [])
        self.assertEqual(chorus.never_send_reason(path), "")  # unpatched, the same file passes

    def test_never_send_hard_link_to_a_listed_file_is_refused(self):
        listed = os.path.join(self.home, ".netrc")  # a file on the default list
        with open(listed, "w") as f:
            f.write("x")
        link = os.path.join(self.tmp.name, "elsewhere.md")
        os.link(listed, link)  # another name for the same inode, outside every listed folder
        self.assertIn("~/.netrc", chorus.never_send_reason(link))
        code, _, err = self.run_cli("--sensitivity", "internal", "--file", link, "--yes")
        self.assertEqual(code, 2)
        self.assertIn("never-send list", err)
        self.assertEqual(self.server.state["requests"], [])

    def test_criteria_never_send_path_refused(self):
        crit = self.draft("Criteria.", "rfp.md", sub="home/.ssh")
        code, _, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                    "--criteria", crit, "--yes")
        self.assertEqual(code, 2)
        self.assertIn("--criteria", err)
        self.assertEqual(self.server.state["requests"], [])

    def test_no_yes_stops_before_sending(self):
        code, _, err = self.run_cli("--sensitivity", "internal", "--file", self.draft())
        self.assertEqual(code, 2)
        self.assertIn("STOP", err)
        self.assertEqual(self.server.state["requests"], [])

    def test_sensitivity_is_required(self):
        code, _, err = self.run_cli("--file", self.draft(), "--yes")
        self.assertEqual(code, 1)
        self.assertIn("--sensitivity", err)


class ContentGuardTests(ChorusCase):
    """The credential guard (no override) and the contact/financial flags."""

    def test_credential_corpus_blocks(self):
        corpus = ["sk-proj-" + "A" * 30, "sk-ant-api03-" + "B" * 30, "sk_live_" + "C" * 24,
                  "rk_live_" + "D" * 24, "AKIA" + "ABCDEFGHIJKLMNOP",
                  "-----BEGIN OPENSSH " + "PRIVATE KEY-----", "ghp_" + "E" * 36,
                  "github_pat_" + "F" * 70, "xoxb-" + "1234567890-abcdef", "AIza" + "G" * 35,
                  ".".join(["eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiIxMjM0NTY3ODkwIn0", "abcdefghijklmnop"]),
                  "password: " + "Zq9xL2mV7p" + "R4tY8wK3nB", "PHALA_API_KEY=" + "phk_0123456789" + "abcdefghijkl",
                  "DB_PASSWORD=" + "Zq9xL2mV7p" + "R4tY8wK3nB", "export GITHUB_PAT=" + "abcdefghij" + "0123456789",
                  "Authorization: Basic " + "dXNlcm5hbWU6" + "cGFzc3dvcmQ=",
                  "Authorization: Bearer " + "abcdef0123" + "456789abcdef",
                  '"api_key": "' + 'abcdefghij' + 'klmnop1234"']
        for tok in corpus:
            blocks, _ = chorus.screen("d", "x %s y" % tok, [])
            self.assertTrue(blocks, tok)
            self.assertFalse(any(tok in b for b in blocks), tok)

    def test_prose_does_not_trip_the_credential_guard(self):
        for line in ["Data is kept in a password-protected/encrypted vault.",
                     "We built an api-key-management-service for grantees.",
                     "Strong password requirements/policies apply.",
                     "MONKEY: bananas-and-more-bananas", "We use API_KEY rotation every 90 days.",
                     "access-token-based authentication", DRAFT]:
            self.assertEqual(chorus.screen("d", line, [])[0], [], line)

    def test_credential_in_draft_blocks_with_no_override(self):
        path = self.draft(DRAFT + "debug: " + "sk-proj-" + "A" * 28 + "\n")
        code, _, err = self.run_cli("--sensitivity", "confidential", "--file", path, "--yes",
                                    "--allow-flagged")
        self.assertEqual(code, 2)
        self.assertIn("line 4: API secret key", err)
        self.assertNotIn("sk-proj", err)
        self.assertEqual(self.server.state["requests"], [])

    def test_secret_value_blocks_and_identifier_value_flags(self):
        token = "tok_live_" + "ABCDEFGHIJKLMNOP"  # SOME_API_TOKEN in the harness's env file
        code, _, err = self.run_cli("--sensitivity", "confidential", "--file",
                                    self.draft(DRAFT + token + "\n"), "--yes")
        self.assertEqual(code, 2)
        self.assertIn("the value of SOME_API_TOKEN", err)
        self.assertNotIn("tok_live", err)
        path = self.draft(DRAFT + "Org: org_ABCDEFGH12345\n")  # ORG_ID in the env file
        code, _, err = self.run_cli("--sensitivity", "internal", "--file", path, "--voices",
                                    "openai", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("the value of ORG_ID", err)
        self.assertNotIn("org_ABCDEFGH12345", err)
        code, _, _ = self.run_cli("--sensitivity", "internal", "--file", path, "--voices",
                                  "openai", "--yes", "--allow-flagged")
        self.assertEqual(code, 0)

    def test_env_only_provider_key_in_draft_blocks(self):
        key = "gwkey_live_" + "QWERTYUIOPASDFGH"
        os.environ["PHALA_API_KEY"] = key
        path = self.draft(DRAFT + key + "\n")
        code, _, err = self.run_cli("--sensitivity", "internal", "--file", path, "--voices",
                                    "openai", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("the value of PHALA_API_KEY", err)
        self.assertNotIn("QWERTY", err)
        self.assertEqual(self.server.state["requests"], [])

    def test_env_var_named_like_a_credential_in_draft_blocks(self):
        value = "wdgt" + "Q7RANDOMVALUE88"
        os.environ["WIDGET_SECRET"] = value  # in no env file and no voice's key
        path = self.draft(DRAFT + "note " + value + "\n")
        code, _, err = self.run_cli("--sensitivity", "internal", "--file", path, "--voices",
                                    "openai", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("the value of WIDGET_SECRET", err)
        self.assertNotIn(value, err)
        self.assertEqual(self.server.state["requests"], [])

    def test_env_file_parsing(self):
        with open(self.env_file, "w") as f:
            f.write("A_KEY=valuevalue1\nexport B_KEY=valuevalue2\nC_KEY=\"valuevalue3\"\n"
                    "D_KEY='valuevalue4'\nE_KEY=\"valuevalue5\"  # rotated\n"
                    "F_KEY=valuevalue6 # note\nG_KEY=ab#cdefghij\n# comment\n")
        self.assertEqual(dict(chorus.parse_env_file(self.env_file))["G_KEY"], "ab#cdefghij")
        pairs = dict(chorus.load_config(SimpleNamespace())["secret_values"])
        for i, k in enumerate("ABCDEF", 1):
            self.assertEqual(pairs["%s_KEY" % k], "valuevalue%d" % i)
            self.assertEqual(os.environ["%s_KEY" % k], "valuevalue%d" % i)
        self.assertEqual(pairs["G_KEY"], "ab#cdefghij")

    def test_env_file_never_overrides_a_set_variable(self):
        os.environ["A_KEY"] = "already-set-value"
        with open(self.env_file, "w") as f:
            f.write("A_KEY=from-the-env-file\n")
        values = [v for _, v in chorus.load_config(SimpleNamespace())["secret_values"]]
        self.assertEqual(os.environ["A_KEY"], "already-set-value")
        self.assertIn("already-set-value", values)  # both are screened out of a draft
        self.assertIn("from-the-env-file", values)

    def test_flag_corpus(self):
        for line in ["IBAN DE89370400440532013000", "DE89 3704 0044 0532 0130 00",
                     "iban de89 3704 0044 0532 0130 00",
                     "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
                     "send to 1ExampLeWaLLetAddressXyz9876543qrs", "+44 20 7946 0958",
                     "+61 491 570 156", "+44 7700 900123", "(415) 555-0134",
                     "1600 Fernquill Avenue, Springfield", "221B Larkspur Street, Avalon",
                     "card 4111 1111 1111 1111",
                     "Wire: Example Bank, acct 000123456789, ABA 123456789.",
                     "sort code 12-34-56, account 12345678", "jane@not" + ORG_DOMAIN,
                     "jane.doe@example.net", "0x" + "a" * 40]:
            # a lookalike of the organisation's own domain is still flagged
            self.assertTrue(chorus.screen("d", line, [], (ORG_DOMAIN,))[1], line)

    def test_flag_false_positive_corpus(self):
        for line in ["https://archive.example.org/web/20220321052004/https://tallowmere.example/nov21",
                     "https://tickets.example.com/e/tallowmere-2025-tickets-1279440162969",
                     "https://notes.example.com/7f3a91c2b4d5e6f708192a3b4c5d6e7f",
                     "Write to grants@fernquill.example or grants@example.org.",
                     "images/hero@2x.png", "git@git.example.com:org/repo.git",
                     "@tallowmere@social.example",
                     "jane.roe@tallowmere.example and jo@lab.tallowmere.example",
                     "3 First Place awards", "2023 Supreme Court amicus brief",
                     "4 Open Source Way workshops", "In 2019 Fernquill Labs Dr. Jane Roe spoke.",
                     "EIN 12-3456789", "FY2026-2027", "Budget: $1,250,000", "+20% growth",
                     "years 2023 2024 2025 2026", "Token supply: governance-token-v2.",
                     "account for FY2025-2026", DRAFT]:
            self.assertEqual(chorus.screen("d", line, [], (ORG_DOMAIN,))[1], [], line)
        # the organisation's own addresses pass because its domain is configured, not by luck
        self.assertTrue(chorus.screen(
            "d", "jane.roe@tallowmere.example and jo@lab.tallowmere.example", [])[1])

    def test_flags_block_every_voice_unless_allowed(self):
        os.environ["CHORUS_ORG_EMAIL_DOMAINS"] = ORG_DOMAIN
        path = self.draft(DRAFT + "Contact jane.doe@example.net or (415) 555-0134.\n"
                                  "Team: jane.roe@" + ORG_DOMAIN + "\n")
        for voices in ("openai", "kimi,glm"):
            code, _, err = self.run_cli("--sensitivity", "internal", "--file", path, "--voices",
                                        voices, "--yes")
            self.assertEqual(code, 2, voices)
            self.assertIn("line 4: email address", err)
            self.assertIn("line 4: phone number", err)
            self.assertNotIn("line 5", err)
            self.assertNotIn("jane.doe", err)
        self.assertEqual(self.server.state["requests"], [])
        code, _, err = self.run_cli("--sensitivity", "internal", "--file", path, "--yes",
                                    "--allow-flagged")
        self.assertEqual(code, 0)
        self.assertIn("checked by the operator", err)

    def test_criteria_flags_are_notes_and_credentials_still_block(self):
        crit = self.draft("Questions: grants@funder.example, Jane Roe jane@example.net, "
                          "(212) 555-0199, 350 Larkspur Avenue, Springfield.\n", "rfp.md")
        code, _, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                    "--criteria", crit, "--voices", "glm", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("public criteria carry", err)
        crit2 = self.draft("Criteria. AKIA" + "ABCDEFGHIJKLMNOP\n", "rfp2.md")
        code, _, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                    "--criteria", crit2, "--voices", "glm", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("criteria line 1: AWS access key id", err)

    def test_non_utf8_input_exits_1(self):
        path = os.path.join(self.tmp.name, "cp1252.txt")
        with open(path, "wb") as f:
            f.write(b"He said \x93hello\x94.\n")
        code, _, err = self.run_cli("--sensitivity", "internal", "--file", path, "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("not UTF-8", err)
