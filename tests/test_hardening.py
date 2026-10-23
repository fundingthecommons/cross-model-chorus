"""Edge cases of the guards, the env file, the use-case loader and the receipt checks: each
test pins one behaviour a mutant or a probe showed was not pinned elsewhere."""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from harness import ChorusCase, chorus


def made(*parts):
    """A credential-shaped value, assembled at runtime so no secret scanner flags this file."""
    return "".join(parts)


HAT = '''
[[hats]]
name = "steward"
title = "steward"
briefs = ["refute"]
looks_for = "You are the steward. You read for what the report leaves out."
'''


class NeverSendEdgeTest(ChorusCase):
    def test_a_proc_real_path_is_refused(self):
        path = self.draft("x", "env-view.md")
        real = os.path.realpath
        with mock.patch.object(chorus.os.path, "realpath",
                               lambda p: "/proc/4242/environ" if p == path else real(p)):
            self.assertIn("not a regular file", chorus.never_send_reason(path))

    def test_a_real_path_that_does_not_exist_is_refused(self):
        path = self.draft("x", "memfd.md")
        gone = os.path.join(self.tmp.name, "gone.md (deleted)")
        real = os.path.realpath
        with mock.patch.object(chorus.os.path, "realpath",
                               lambda p: gone if p == path else real(p)):
            self.assertIn("does not lead back", chorus.never_send_reason(path))


class EnvFileEdgeTest(ChorusCase):
    def test_lines_of_a_multiline_value_are_stripped_before_screening(self):
        chunk = made("Mq3", "Zr8vT2", "xKp9Lw4")
        sv = chorus.screened_values({"DEPLOY_KEY": "header-line\n    " + chunk + "\n    tail"}, [])
        self.assertIn(("DEPLOY_KEY", chunk), sv)

    def test_a_structural_line_of_a_json_credential_does_not_block_a_draft(self):
        cred = '{\n  "type": "service_account",\n  "private_key_id": "' + made("a1b2", "c3d4e5f6g7") + '"\n}'
        sv = chorus.screened_values({"GCP_SA_CREDENTIALS": cred}, [])
        values = {v for _, v in sv}
        self.assertNotIn('"type": "service_account",', values)
        self.assertIn(cred, values)  # the whole value is still screened

    def test_an_entry_swallowed_by_a_stray_quote_is_still_screened(self):
        secret = made("Vq7", "Lm2xR9", "pT4wK8z")
        with open(self.env_file, "w") as f:  # the apostrophe in the comment closes the quote
            f.write("SIGNOFF='Tallowmere team\nDB_PASSWORD=" + secret +
                    "\n# don't commit this file\n")
        draft = self.draft("The password is " + secret + " for now.\n", "leak.md")
        code, out, err = self.run_cli("--sensitivity", "public", "--voices", "openai",
                                      "--file", draft, "--dry-run")
        self.assertEqual(code, 2, err)
        self.assertIn("credential material", err)
        self.assertNotIn(secret, out + err)
        self.assertEqual(self.server.state["requests"], [])


class UseCaseLoaderEdgeTest(ChorusCase):
    def folder(self, name, files):
        d = os.path.join(self.tmp.name, name)
        os.makedirs(d, exist_ok=True)
        for fn, text in files.items():
            with open(os.path.join(d, fn), "w", encoding="utf-8") as f:
                f.write(text)
        return d

    def test_a_shipped_use_case_linked_under_another_name_loads_as_that_name(self):
        d = os.path.join(self.tmp.name, "alias")
        os.makedirs(d)
        os.symlink(os.path.join(chorus.BUILTIN_USE_CASES, "contracts.toml"),
                   os.path.join(d, "legal.toml"))
        cases = chorus.load_use_cases([d])
        self.assertIn("legal", cases)
        self.assertIn("contracts", cases)

    def test_a_broken_link_exits_1_cleanly(self):
        d = os.path.join(self.tmp.name, "broken")
        os.makedirs(d)
        os.symlink(os.path.join(self.tmp.name, "gone.toml"), os.path.join(d, "dangling.toml"))
        code, out, err = self.run_cli("--use-case-dir", d, "--list-use-cases")
        self.assertEqual(code, 1)
        self.assertIn("dangling.toml: cannot read it (FileNotFoundError)", err)
        self.assertNotIn("Traceback", err)

    def test_a_non_string_hat_name_exits_1_cleanly(self):
        for name in ('["a"]', "{ x = 1 }", "7"):
            with self.subTest(name=name):
                d = self.folder("hatname-%d" % abs(hash(name)), {"probe.toml": (
                    'purpose = "a field report"\nsensitivity = ["public"]\n'
                    + HAT.replace('name = "steward"', "name = " + name))})
                code, out, err = self.run_cli("--use-case-dir", d, "--list-use-cases")
                self.assertEqual(code, 1, err)
                self.assertIn("hat 1: name must be lowercase letters, digits and hyphens", err)


class PreflightAndReceiptEdgeTest(ChorusCase):
    def test_every_catalog_entry_for_the_model_needs_its_own_is_tee(self):
        v = dict(chorus.VOICES["glm"], name="glm")
        cat = {"data": [{"id": v["model"]}, {"id": v["model"], "is_tee": True}]}
        ok, detail, _ = chorus.tee_preflight(v, "k", {v["base"]: cat})
        self.assertFalse(ok)
        self.assertIn("no is_tee field at its top level", detail)

    def test_the_runs_missing_receipt_advice_works_as_written(self):
        self.server.state["missing_ids"] = {"rcpt-moonshotai-kimi-k3", "req-rcpt-moonshotai-kimi-k3"}
        code, out, err = self.run_cli("--sensitivity", "internal", "--voices", "kimi",
                                      "--file", self.draft(), "--yes")
        self.assertEqual(code, 6, err)
        self.assertIn("--check-receipt <id> --voices <voice> --sensitivity internal", err)
        code, out, err = self.run_cli("--check-receipt", "rcpt-moonshotai-kimi-k3",
                                      "--voices", "kimi", "--sensitivity", "internal")
        self.assertNotIn("STOP", err)
        self.assertEqual(code, 6)  # still missing, and below confidential: a warning


class UsageErrorTest(ChorusCase):
    def test_argparse_usage_errors_exit_1_as_documented(self):
        for argv in (["--sensitivity", "secret"], ["--dryrun"], ["--max-tokens", "lots"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(*argv, "--file", self.draft())
                self.assertEqual(code, 1, err)
                self.assertIn("error:", err)
        self.assertEqual(self.server.state["requests"], [])


if __name__ == "__main__":
    unittest.main()
