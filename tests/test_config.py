"""The configuration the runner reads: the env file and the values the guards screen, the
approved-upstream list, the never-send list, the organisation's email domains, the use-case
loader, the runner's independence from any personal path, the interpreter guards, and where the
script finds its use cases when it is run through a symlink or copied alone.

Every name, value, domain and path here is fictitious. Credential-shaped values are assembled
at runtime, never written whole.
"""
import argparse
import ast
import builtins
import contextlib
import fnmatch
import importlib.util
import io
import itertools
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from harness import (APPROVED_GLM, DRAFT, ENV_FILE, KIMI_PINS, SESSIONS, ChorusCase, chorus,
                     hat_chat, hat_openai, provider_for)

UPSTREAMS = "CHORUS_CONFIDENTIAL_UPSTREAMS"


def made(*parts):
    """A credential-shaped value, assembled at runtime so no secret scanner flags this file."""
    return "".join(parts)


def ns(**kw):
    """The two flags load_config reads, as parse_args would give them."""
    base = {"env_file": None, "use_case_dir": []}
    base.update(kw)
    return argparse.Namespace(**base)


def captured(fn, *a, **k):
    """(result, exit code or None, stderr) for a call that may exit. Any other exception is
    returned as the code "crash: <type>", so a missing validation shows as a failed check."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            return fn(*a, **k), None, err.getvalue()
        except SystemExit as e:
            return None, e.code, err.getvalue()
        except Exception as e:  # a validation the runner should have made
            return None, "crash: %s" % type(e).__name__, err.getvalue()


def write(path, text, mode="w"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        f.write(text)
    return path


def clean_draft(value):
    """A draft whose only notable content is `value`, on line 2."""
    return "The Fernquill Fund reviewed the season plan.\nIt mentions %s once.\n" % value


# ---------------------------------------------------------------------------------------------
# parse_env_file


class ParseEnvFileTest(ChorusCase):
    def parse(self, text):
        return chorus.parse_env_file(write(os.path.join(self.tmp.name, "probe.env"), text))

    def test_parse_env_file_name_value_pairs(self):
        self.assertEqual(self.parse("ALPHA_NAME=one-value\nBETA=two\nGAMMA = three\n"),
                         [("ALPHA_NAME", "one-value"), ("BETA", "two"), ("GAMMA", "three")])

    def test_parse_env_file_export_prefix(self):
        self.assertEqual(self.parse("export DELTA=four\nexport   EPSILON=five\n"),
                         [("DELTA", "four"), ("EPSILON", "five")])

    def test_parse_env_file_single_and_double_quotes(self):
        self.assertEqual(self.parse("A='x y z'\nB=\"p q\"\nC=\"it's\"\nD='say \"hi\"'\n"),
                         [("A", "x y z"), ("B", "p q"), ("C", "it's"), ("D", 'say "hi"')])

    def test_parse_env_file_unclosed_quote_raises_naming_the_line(self):
        # A quote that is never closed is an error naming the line it opened on; the message
        # carries the line number only, never the value.
        for text, line in (("A=\"abc def\nB='xyz\n", 1),
                           ("A=1\nB=2\nC='kept quiet\nD=4\n", 3),
                           ("A=\"one\"\nB=\"spans\ntwo lines\"\nC='open\n", 4)):
            with self.subTest(line=line):
                with self.assertRaises(ValueError) as cm:
                    self.parse(text)
                self.assertEqual(str(cm.exception),
                                 "line %d: a quoted value is never closed" % line)

    def test_parse_env_file_quoted_value_may_span_lines(self):
        self.assertEqual(self.parse('PEM="first line\nsecond line\n  third"\nNEXT=1\n'),
                         [("PEM", "first line\nsecond line\n  third"), ("NEXT", "1")])
        self.assertEqual(self.parse("NOTE='one\ntwo'\r\nNEXT=2\r\n"),
                         [("NOTE", "one\ntwo"), ("NEXT", "2")])

    def test_parse_env_file_trailing_comment_on_unquoted_value(self):
        self.assertEqual(self.parse("A=value # a note\nB=value\t# a note\nC=val#ue\n"),
                         [("A", "value"), ("B", "value"), ("C", "val#ue")])

    def test_parse_env_file_comment_after_an_empty_value_leaves_it_empty(self):
        self.assertEqual(self.parse("K=   # note\nL=\t# note\nM= #\n"),
                         [("K", ""), ("L", ""), ("M", "")])

    def test_parse_env_file_hash_with_no_space_before_it_is_kept(self):
        self.assertEqual(self.parse("K=#Secret123abc\nL=#\n"),
                         [("K", "#Secret123abc"), ("L", "#")])

    def test_parse_env_file_hash_inside_quotes_is_kept(self):
        self.assertEqual(self.parse("A=\"a # b\"\nB='c # d' # a note\n"),
                         [("A", "a # b"), ("B", "c # d")])

    def test_parse_env_file_skips_blank_and_comment_lines(self):
        self.assertEqual(self.parse("\n   \n# a comment\n   # an indented comment\nA=1\n\n"),
                         [("A", "1")])

    def test_parse_env_file_skips_lines_without_equals_or_name(self):
        self.assertEqual(self.parse("JUSTANAME\nexport ALONE\n=orphan\nA=1\n"), [("A", "1")])

    def test_parse_env_file_value_keeps_later_equals_and_may_be_empty(self):
        self.assertEqual(self.parse("A=b=c==\nB=\n"), [("A", "b=c=="), ("B", "")])

    def test_parse_env_file_crlf_line_endings(self):
        self.assertEqual(self.parse("A=1\r\nB='two'\r\n"), [("A", "1"), ("B", "two")])


# ---------------------------------------------------------------------------------------------
# load_config: the env file and the screened values


class LoadConfigTest(ChorusCase):
    def env(self, text, name="other.env", sub=""):
        return write(os.path.join(self.tmp.name, sub, name), text)

    def config(self, args=None, environ=None):
        cfg, code, err = captured(chorus.load_config, args or ns(), environ)
        self.assertIsNone(code, err)
        return cfg

    def assertQuiet(self, values, *outputs):
        for v in values:
            for o in outputs:
                self.assertNotIn(v, o)

    def test_env_file_from_variable_loads_into_environ(self):
        cfg = self.config()
        self.assertEqual(cfg["env_file"], os.path.abspath(self.env_file))
        self.assertEqual(os.environ["ORG_ID"], "org_ABCDEFGH12345")
        self.assertIn("SOME_API_TOKEN", os.environ)
        self.assertIn(("the env file", os.path.abspath(self.env_file)), cfg["never_send"])

    def test_env_file_flag_wins_over_variable(self):
        flag_file = self.env("FLAG_ONLY_NAME=" + made("fl", "9" * 8) + "\n")
        cfg = self.config(ns(env_file=flag_file))
        self.assertEqual(cfg["env_file"], os.path.abspath(flag_file))
        self.assertIn("FLAG_ONLY_NAME", os.environ)
        self.assertNotIn("ORG_ID", os.environ)  # the CHORUS_ENV_FILE file was not loaded
        files = [p for label, p in cfg["never_send"] if label == "the env file"]
        self.assertEqual(files, [os.path.abspath(flag_file)])

    def test_env_file_flag_wins_end_to_end(self):
        flag_file = self.env("FLAG_ONLY_TOKEN=" + made("fl", "9" * 8) + "\n")
        # ORG_ID's value is screened only when the CHORUS_ENV_FILE file loads, and it does not.
        code, out, err = self.run_cli("--sensitivity", "internal", "--env-file", flag_file,
                                      "--file", self.draft(clean_draft("org_ABCDEFGH12345")),
                                      "--dry-run")
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("--sensitivity", "internal", "--env-file", flag_file,
                                      "--file", self.draft(clean_draft(made("fl", "9" * 8))),
                                      "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("the value of FLAG_ONLY_TOKEN", err)

    def test_env_file_path_expands_tilde(self):
        write(os.path.join(self.home, "chorus.env"), "HOME_FILE_NAME=from-home\n")
        cfg = self.config(ns(env_file="~/chorus.env"))
        self.assertEqual(cfg["env_file"], os.path.join(self.home, "chorus.env"))
        self.assertEqual(os.environ["HOME_FILE_NAME"], "from-home")

    def test_env_file_never_overrides_a_set_variable(self):
        other = made("from-file-", "k" * 8)
        path = self.env("OPENAI_API_KEY=%s\nNEW_SETTING=from-the-file\n" % other)
        cfg = self.config(ns(env_file=path))
        self.assertEqual(os.environ["OPENAI_API_KEY"], "test-openai-key")
        self.assertEqual(os.environ["NEW_SETTING"], "from-the-file")
        # the file's value did not win, but it is still screened out of every draft
        self.assertIn(("OPENAI_API_KEY", other), cfg["secret_values"])
        self.assertIn(("OPENAI_API_KEY", "test-openai-key"), cfg["secret_values"])

    def test_load_config_loads_into_the_mapping_it_is_given(self):
        environ = {"CHORUS_ENV_FILE": self.env_file, "HOME": self.home}
        cfg = self.config(environ=environ)
        self.assertEqual(environ["ORG_ID"], "org_ABCDEFGH12345")
        self.assertNotIn("ORG_ID", os.environ)
        self.assertEqual(cfg["env_file"], os.path.abspath(self.env_file))

    def test_env_file_supplies_a_missing_voice_key(self):
        key = made("ok", "-" * 2, "z" * 12)
        path = self.env("OPENAI_API_KEY=%s\n" % key)
        os.environ.pop("OPENAI_API_KEY")
        draft = self.draft()
        code, out, err = self.run_cli("--sensitivity", "public", "--voices", "openai", "--file",
                                      draft, "--dry-run")
        self.assertEqual(code, 3, err)
        self.assertIn("OPENAI_API_KEY not set", err)
        code, out, err = self.run_cli("--sensitivity", "public", "--voices", "openai", "--file",
                                      draft, "--env-file", path, "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(os.environ["OPENAI_API_KEY"], key)
        self.assertQuiet([key], out, err)
        self.assertEqual(self.server.state["requests"], [])

    def test_missing_env_file_exits_1(self):
        missing = os.path.join(self.tmp.name, "nowhere.env")
        for argv, env in ((["--env-file", missing], {}), ([], {"CHORUS_ENV_FILE": missing})):
            with self.subTest(argv=argv), mock.patch.dict(os.environ, env):
                code, out, err = self.run_cli("--sensitivity", "public", "--file", self.draft(),
                                              "--dry-run", *argv)
                self.assertEqual(code, 1)
                self.assertIn("env file %s does not exist or is not a regular file" % missing,
                              err)
        self.assertEqual(self.server.state["requests"], [])

    def test_non_regular_env_file_exits_1(self):
        folder = os.path.join(self.tmp.name, "folder.env")
        os.makedirs(folder)
        for path in (folder, os.devnull):
            with self.subTest(path=path):
                cfg, code, err = captured(chorus.load_config, ns(env_file=path))
                self.assertEqual(code, 1)
                self.assertIn("is not a regular file", err)

    def test_undecodable_env_file_exits_1(self):
        path = write(os.path.join(self.tmp.name, "latin.env"), b"NAME=caf\xe9\xff\n", "wb")
        cfg, code, err = captured(chorus.load_config, ns(env_file=path))
        self.assertEqual(code, 1)
        self.assertIn("cannot read env file %s (UnicodeDecodeError)" % path, err)

    def test_env_file_is_on_the_never_send_list(self):
        path = self.env("A_SETTING=some-setting\n")
        cfg = self.config(ns(env_file=path))
        self.assertIn(("the env file", os.path.abspath(path)), cfg["never_send"])
        os.environ.pop("CHORUS_ENV_FILE")
        cfg = self.config(ns())
        self.assertIsNone(cfg["env_file"])
        self.assertNotIn("the env file", [label for label, _ in cfg["never_send"]])

    def test_values_of_eight_chars_or_more_are_screened(self):
        seven, eight = made("qz7", "-", "k4m"), made("qz7", "-", "k4mw")
        path = self.env("SHORT_TOKEN=%s\nEXACT_TOKEN=%s\nPLAIN_NOTE=%s\n" % (
            seven, eight, made("pn", "8" * 6)))
        cfg = self.config(ns(env_file=path))
        names = {n for n, _ in cfg["secret_values"]}
        self.assertIn("EXACT_TOKEN", names)
        self.assertIn("PLAIN_NOTE", names)  # every env-file entry, whatever its name
        self.assertNotIn("SHORT_TOKEN", names)
        self.assertTrue(all(len(v) >= 8 for _, v in cfg["secret_values"]))
        code, out, err = self.run_cli("--sensitivity", "internal", "--env-file", path, "--file",
                                      self.draft(clean_draft(seven), "seven.md"), "--dry-run")
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("--sensitivity", "internal", "--env-file", path, "--file",
                                      self.draft(clean_draft(eight), "eight.md"), "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("REFUSED: credential material", err)
        self.assertIn("draft line 2: the value of EXACT_TOKEN", err)
        self.assertQuiet([seven, eight], out, err)

    def test_any_env_file_entry_blocks_unless_it_is_an_identifier(self):
        note = made("pn", "8" * 6)
        path = self.env("PLAIN_NOTE=%s\n" % note)
        code, out, err = self.run_cli("--sensitivity", "internal", "--env-file", path, "--file",
                                      self.draft(clean_draft(note)), "--allow-flagged",
                                      "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("REFUSED: credential material", err)
        self.assertIn("the value of PLAIN_NOTE", err)
        self.assertQuiet([note], out, err)

    def test_identifier_entries_flag_rather_than_block(self):
        path = self.env("PICKUP_ADDRESS=depot-north-annex-7\n"
                        "MEMBER_IDENTIFIER=member-ref-77731\n")
        cases = [("ORG_ID", "org_ABCDEFGH12345", []),  # from the harness's env file
                 ("PICKUP_ADDRESS", "depot-north-annex-7", ["--env-file", path]),
                 ("MEMBER_IDENTIFIER", "member-ref-77731", ["--env-file", path])]
        for name, value, env_flag in cases:
            with self.subTest(name=name):
                argv = ["--sensitivity", "internal", "--file",
                        self.draft(clean_draft(value), name.lower() + ".md")] + env_flag
                code, out, err = self.run_cli(*(argv + ["--dry-run"]))
                self.assertEqual(code, 2)
                self.assertIn("REFUSED: contact/financial identifiers", err)
                self.assertIn("draft line 2: the value of %s" % name, err)
                self.assertNotIn("credential material", err)
                self.assertQuiet([value], out, err)
                code, out, err = self.run_cli(*(argv + ["--allow-flagged", "--dry-run"]))
                self.assertEqual(code, 0, err)
                self.assertIn("note: sending with 1 flagged line(s)", err)
                self.assertQuiet([value], out, err)
        self.assertEqual(self.server.state["requests"], [])

    def test_credential_named_environment_variables_block_without_an_env_file(self):
        os.environ.pop("CHORUS_ENV_FILE")
        names = ["DEPLOY_TOKEN", "X_SECRET", "Y_PASSWORD", "DB_PASSWD", "SERVICE_PAT",
                 "SIGNING_KEY", "CLOUD_CREDENTIALS", "webhook_token"]
        for i, name in enumerate(names):
            value = made("cv", str(i), "-", "q" * 9)
            with self.subTest(name=name), mock.patch.dict(os.environ, {name: value}):
                self.assertIsNone(self.config()["env_file"])
                code, out, err = self.run_cli(
                    "--sensitivity", "internal", "--file",
                    self.draft(clean_draft(value), "cred%d.md" % i), "--allow-flagged",
                    "--dry-run")
                self.assertEqual(code, 2)
                self.assertIn("REFUSED: credential material", err)
                self.assertIn("draft line 2: the value of %s" % name, err)
                self.assertQuiet([value], out, err)
        self.assertEqual(self.server.state["requests"], [])

    def test_variable_not_named_like_a_credential_is_not_screened(self):
        os.environ.pop("CHORUS_ENV_FILE")
        label = made("build", "-label-", "2" * 6)
        with mock.patch.dict(os.environ, {"BUILD_LABEL": label}):
            self.assertNotIn("BUILD_LABEL", {n for n, _ in self.config()["secret_values"]})
            code, out, err = self.run_cli("--sensitivity", "internal", "--file",
                                          self.draft(clean_draft(label)), "--dry-run")
        self.assertEqual(code, 0, err)

    def test_voice_key_value_in_the_draft_blocks(self):
        for name, value in (("PHALA_API_KEY", "test-phala-key"),
                            ("OPENAI_API_KEY", "test-openai-key")):
            with self.subTest(name=name):
                code, out, err = self.run_cli(
                    "--sensitivity", "public", "--file",
                    self.draft(clean_draft(value), name.lower() + ".md"), "--allow-flagged",
                    "--dry-run")
                self.assertEqual(code, 2)
                self.assertIn("REFUSED: credential material", err)
                self.assertIn("the value of %s" % name, err)
                self.assertQuiet([value], out, err)

    def test_no_screened_value_is_ever_printed(self):
        extra = {"DEPLOY_TOKEN": made("dt", "-", "w" * 10)}
        with mock.patch.dict(os.environ, extra):
            cfg = self.config()
            values = [v for _, v in cfg["secret_values"]]
            self.assertTrue({"org_ABCDEFGH12345", "test-phala-key",
                             extra["DEPLOY_TOKEN"]} <= set(values))
            text = "".join("Line %d carries %s here.\n" % (i, v) for i, v in enumerate(values))
            for flags in ([], ["--allow-flagged"], ["--allow-flagged", "--json"]):
                with self.subTest(flags=flags):
                    code, out, err = self.run_cli("--sensitivity", "internal", "--file",
                                                  self.draft(text), "--dry-run", *flags)
                    self.assertEqual(code, 2)
                    self.assertQuiet(values, out, err)
            # the plan and the never-send refusals print paths, never values
            code, out, err = self.run_cli("--sensitivity", "internal", "--file", self.env_file,
                                          "--yes")
            self.assertEqual(code, 2)
            self.assertQuiet(values, out, err)
        self.assertEqual(self.server.state["requests"], [])

    def test_screened_values_dedupes_and_keeps_only_credential_names(self):
        shared, key = made("sh", "-", "a" * 9), made("pk", "-", "b" * 9)
        environ = {"A_TOKEN": shared, "B_TOKEN": shared, "PHALA_API_KEY": key,
                   "HOME": "/nowhere/at/all", "TOKEN_COUNT": "123456789"}
        out = chorus.screened_values(environ, [("ENTRY", made("en", "-", "c" * 9)),
                                               ("TINY", "abc")])
        self.assertEqual(out, [("ENTRY", made("en", "-", "c" * 9)), ("A_TOKEN", shared),
                               ("PHALA_API_KEY", key)])

    def test_unclosed_quote_in_the_env_file_exits_1_without_printing_the_value(self):
        value = made("uq", "-", "m" * 10)
        path = self.env("A_SETTING=some-setting\nDEPLOY_TOKEN=\"%s\nB_SETTING=other\n" % value)
        want = "env file %s, line 2: a quoted value is never closed" % path
        cfg, code, err = captured(chorus.load_config, ns(env_file=path))
        self.assertEqual(code, 1)
        self.assertIn(want, err)
        self.assertQuiet([value], err)
        os.environ.pop("CHORUS_ENV_FILE")
        for argv, env in ((["--env-file", path], {}), ([], {"CHORUS_ENV_FILE": path})):
            with self.subTest(argv=argv), mock.patch.dict(os.environ, env):
                code, out, err = self.run_cli("--sensitivity", "internal", "--file",
                                              self.draft(), "--yes", *argv)
                self.assertEqual(code, 1)
                self.assertIn(want, err)
                self.assertNotIn("Traceback", err)
                self.assertQuiet([value], out, err)
        for name in ("A_SETTING", "DEPLOY_TOKEN", "B_SETTING"):  # nothing from the file loads
            self.assertNotIn(name, os.environ)
        self.assertEqual(self.server.state["requests"], [])

    def test_multiline_value_is_screened_whole_and_line_by_line(self):
        begin, end = "-----BEGIN " + "EXAMPLE BLOCK-----", "-----END " + "EXAMPLE BLOCK-----"
        body = [made("MIIBexample", "Qq" * 6, "x1"), made("MIIBexample", "Rr" * 6, "x2"),
                made("MIIBexample", "Ss" * 6, "x3")]
        value = "\n".join([begin] + body + [end])
        path = self.env('SERVICE_CERT="%s"\nAFTER_SETTING=after-the-block\n' % value)
        cfg = self.config(ns(env_file=path))
        self.assertEqual(os.environ["SERVICE_CERT"], value)
        self.assertEqual(os.environ["AFTER_SETTING"], "after-the-block")
        self.assertIn(("SERVICE_CERT", value), cfg["secret_values"])
        for ln in body:
            self.assertIn(("SERVICE_CERT", ln), cfg["secret_values"])
        # a draft that carries just one line of the block is blocked, with no override
        code, out, err = self.run_cli("--sensitivity", "internal", "--env-file", path, "--file",
                                      self.draft(clean_draft(body[1])), "--allow-flagged",
                                      "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("REFUSED: credential material", err)
        self.assertIn("draft line 2: the value of SERVICE_CERT", err)
        self.assertQuiet(body + [value], out, err)
        self.assertEqual(self.server.state["requests"], [])

    def test_a_value_under_an_identifier_and_a_credential_name_blocks(self):
        value = made("dual", "-", "v" * 10)
        for order in (("ORG_ID", "X_SECRET"), ("X_SECRET", "ORG_ID")):
            with self.subTest(order=order):
                self.assertEqual(chorus.screened_values({}, [(n, value) for n in order]),
                                 [("X_SECRET", value)])
                path = self.env("".join("%s=%s\n" % (n, value) for n in order),
                                name="%s.env" % "-".join(order).lower())
                for flags in ([], ["--allow-flagged"]):
                    code, out, err = self.run_cli(
                        "--sensitivity", "internal", "--env-file", path, "--file",
                        self.draft(clean_draft(value)), "--dry-run", *flags)
                    self.assertEqual(code, 2)
                    self.assertIn("REFUSED: credential material", err)
                    self.assertIn("draft line 2: the value of X_SECRET", err)
                    self.assertNotIn("the value of ORG_ID", err)
                    self.assertQuiet([value], out, err)
        # the identifier in the env file, the credential in the environment
        os.environ.pop("ORG_ID", None)
        os.environ.pop("X_SECRET", None)
        with mock.patch.dict(os.environ, {"X_SECRET": value}):
            cfg = self.config(ns(env_file=self.env("ORG_ID=%s\n" % value, name="id-only.env")))
        self.assertIn(("X_SECRET", value), cfg["secret_values"])
        self.assertNotIn(("ORG_ID", value), cfg["secret_values"])
        self.assertEqual(self.server.state["requests"], [])


# ---------------------------------------------------------------------------------------------
# CHORUS_CONFIDENTIAL_UPSTREAMS


class ChorusSettingsAreNotSecretsTest(ChorusCase):
    """The tool's own CHORUS_* settings may live in the env file, but they are settings, not
    secrets: a draft that mentions the org's domain or a never-send folder is not blocked."""

    def test_chorus_settings_in_the_env_file_do_not_block_a_draft(self):
        with open(self.env_file, "a") as f:
            f.write("CHORUS_ORG_EMAIL_DOMAINS=tallowmere.example\n"
                    "CHORUS_NEVER_SEND=~/private-notes\n"
                    "CHORUS_USE_CASE_DIR=~/chorus-use-cases-elsewhere\n")
        os.makedirs(os.path.join(self.home, "chorus-use-cases-elsewhere"))
        draft = self.draft("Write to ops@tallowmere.example; never paste ~/private-notes or "
                           "~/chorus-use-cases-elsewhere into a draft.\n", "settings.md")
        code, out, err = self.run_cli("--sensitivity", "public", "--file", draft, "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertNotIn("REFUSED", err)
        names = {k for k, _ in chorus.load_config(ns())["secret_values"]}
        self.assertFalse({n for n in names if n.startswith("CHORUS_")}, names)
        self.assertIn("SOME_API_TOKEN", names)  # the env file's real secret is still screened


class UpstreamAllowlistTest(ChorusCase):
    def allow(self, spec):
        out, code, err = captured(chorus.upstream_allowlist, spec)
        return out, code, err

    def confidential(self, *extra):
        return self.run_cli("--sensitivity", "confidential", "--file", self.draft(), *extra)

    def chat_posts(self, model=None):
        return [r for r in self.posts("/chat/completions")
                if model is None or r[2]["model"] == model]

    def test_semicolon_and_whitespace_separators(self):
        want = {"glm": ("upstream-a",), "kimi": ("upstream-c",)}
        for spec in ("glm=upstream-a;kimi=upstream-c", "glm=upstream-a kimi=upstream-c",
                     "  glm=upstream-a\n\tkimi=upstream-c ;; ", "glm=upstream-a;\nkimi=upstream-c"):
            with self.subTest(spec=spec):
                self.assertEqual(self.allow(spec)[0], want)
        self.assertEqual(self.allow("glm=upstream-a,upstream-b")[0],
                         {"glm": ("upstream-a", "upstream-b")})

    def test_unset_or_empty_approves_nothing(self):
        for spec in (None, "", "  ;; \n"):
            with self.subTest(spec=spec):
                self.assertEqual(self.allow(spec)[0], {})

    def test_voice_named_twice_merges(self):
        self.assertEqual(self.allow("glm=upstream-a;kimi=upstream-c;glm=upstream-b,upstream-a")[0],
                         {"glm": ("upstream-a", "upstream-b"), "kimi": ("upstream-c",)})

    def test_upstream_names_are_case_folded(self):
        self.assertEqual(self.allow("glm=Upstream-A,UPSTREAM-B;kimi=upstream-C")[0],
                         {"glm": ("upstream-a", "upstream-b"), "kimi": ("upstream-c",)})
        with mock.patch.dict(os.environ, {UPSTREAMS: "glm=UPSTREAM-A;kimi=Upstream-C"}):
            code, out, err = self.confidential("--yes", "--no-cross-examine")
        self.assertEqual(code, 0, err)
        self.assertEqual([r[2]["provider"]["aci_session_ids"] for r in self.chat_posts(
            "z-ai/glm-5.3")], [[SESSIONS[2]["session_id"]]])

    def test_malformed_entries_exit_1(self):
        cases = {"glm": "takes voice=name", "glm=": "takes voice=name",
                 "=upstream-a": "takes voice=name", "glm=,": "takes voice=name",
                 "glm=upstream a": "takes voice=name",  # whitespace separates entries
                 "nobody=upstream-a": "'nobody' is not a TEE voice",
                 "openai=upstream-a": "'openai' is not a TEE voice",
                 "gemini=upstream-a": "'gemini' is not a TEE voice",
                 "glm=upstream/a": "'upstream/a' is not an upstream name",
                 "glm=up+stream": "'up+stream' is not an upstream name",
                 "glm=-upstream": "'-upstream' is not an upstream name",
                 "glm=" + "u" * 65: "is not an upstream name",
                 "kimi=upstream-c;glm=upstream-a,bad\\name": "is not an upstream name"}
        for spec, why in cases.items():
            with self.subTest(spec=spec):
                out, code, err = self.allow(spec)
                self.assertEqual(code, 1)
                self.assertIn("CHORUS_CONFIDENTIAL_UPSTREAMS", err)
                self.assertIn(why, err)

    def test_malformed_allowlist_stops_every_run(self):
        with mock.patch.dict(os.environ, {UPSTREAMS: "openai=upstream-a"}):
            code, out, err = self.run_cli("--sensitivity", "public", "--file", self.draft(),
                                          "--yes")
        self.assertEqual(code, 1)
        self.assertIn("'openai' is not a TEE voice", err)
        self.assertEqual(self.server.state["requests"], [])

    def test_no_allowlist_confidential_run_skips_both_tee_voices_and_sends_nothing(self):
        os.environ.pop(UPSTREAMS)
        code, out, err = self.confidential("--yes")
        self.assertEqual(code, 2)
        self.assertIn("skip kimi: no approved upstream for confidential runs", err)
        self.assertIn("skip glm: no approved upstream for confidential runs", err)
        self.assertIn("REFUSED: no TEE voice has an approved upstream", err)
        self.assertEqual(self.server.state["requests"], [])

    def test_no_allowlist_named_voice_is_refused(self):
        os.environ.pop(UPSTREAMS)
        code, out, err = self.confidential("--voices", "glm", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("REFUSED: voice glm has no approved upstream for confidential runs", err)
        self.assertEqual(self.server.state["requests"], [])
        with mock.patch.dict(os.environ, {UPSTREAMS: "kimi=upstream-c"}):
            code, out, err = self.confidential("--voices", "kimi,glm", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("REFUSED: voice glm has no approved upstream", err)
        self.assertEqual(self.server.state["requests"], [])

    def test_partial_allowlist_runs_only_the_approved_voice(self):
        with mock.patch.dict(os.environ, {UPSTREAMS: "kimi=upstream-c"}):
            code, out, err = self.confidential("--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("skip glm: no approved upstream for confidential runs", err)
        self.assertEqual({r[2]["model"] for r in self.chat_posts()}, {"moonshotai/kimi-k3"})
        self.assertTrue(self.reqs("GET", "/aci/sessions?model=moonshotai"))
        self.assertEqual(self.reqs("GET", "/aci/sessions?model=z-ai"), [])  # glm: not even read

    def test_hand_pin_outside_the_allowlist_is_refused(self):
        code, out, err = self.confidential("--upstream", "glm=upstream-z", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("REFUSED: a confidential run keeps glm on its approved upstreams "
                      "(upstream-a or upstream-b), not upstream-z", err)
        self.assertEqual(self.server.state["requests"], [])

    def test_hand_pin_for_a_voice_with_no_allowlist_is_refused(self):
        with mock.patch.dict(os.environ, {UPSTREAMS: "kimi=upstream-c"}):
            code, out, err = self.confidential("--upstream", "glm=upstream-a", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("REFUSED: voice glm has no approved upstream", err)
        self.assertEqual(self.server.state["requests"], [])

    def test_hand_pin_inside_the_allowlist_narrows_the_pins(self):
        code, out, err = self.confidential("--upstream", "glm=upstream-a", "--yes",
                                           "--no-cross-examine")
        self.assertEqual(code, 0, err)
        glm = self.chat_posts("z-ai/glm-5.3")
        self.assertTrue(glm)
        for r in glm:
            self.assertEqual(r[2]["provider"]["aci_session_ids"], [SESSIONS[2]["session_id"]])

    def test_approved_list_pins_every_confidential_call_with_zdr(self):
        code, out, err = self.confidential("--yes")
        self.assertEqual(code, 0, err)
        posts = self.chat_posts()
        self.assertEqual({r[2]["model"] for r in self.round1("/chat/completions")},
                         {"z-ai/glm-5.3", "moonshotai/kimi-k3"})
        self.assertTrue(len(posts) > 2, "round 2 ran too")
        for r in posts:  # both rounds
            with self.subTest(model=r[2]["model"]):
                self.assertEqual(r[2]["provider"], provider_for(r[2]["model"], True))
                self.assertIs(r[2]["provider"]["zdr"], True)
        glm_pins = {s for r in self.chat_posts("z-ai/glm-5.3")
                    for s in r[2]["provider"]["aci_session_ids"]}
        self.assertEqual(glm_pins, set(APPROVED_GLM))
        self.assertNotIn(SESSIONS[0]["session_id"], glm_pins)  # upstream-z, not approved
        self.assertEqual([r[2]["provider"]["aci_session_ids"] for r in self.chat_posts(
            "moonshotai/kimi-k3")][0], KIMI_PINS)
        self.assertEqual(self.posts("/responses"), [])

    def test_narrower_allowlist_pins_exactly_its_sessions(self):
        with mock.patch.dict(os.environ, {UPSTREAMS: "glm=upstream-b;kimi=upstream-c"}):
            code, out, err = self.confidential("--yes", "--no-cross-examine")
        self.assertEqual(code, 0, err)
        self.assertEqual([r[2]["provider"]["aci_session_ids"] for r in self.chat_posts(
            "z-ai/glm-5.3")], [[SESSIONS[1]["session_id"]]])

    def test_a_prefix_of_an_upstream_name_is_not_approved(self):
        with mock.patch.dict(os.environ, {UPSTREAMS: "glm=upstream;kimi=upstream-c"}):
            code, out, err = self.confidential("--yes", "--no-cross-examine")
        self.assertEqual(code, 6, err)
        self.assertIn("REFUSED glm: cannot prove TEE serving (no live session for z-ai/glm-5.3 "
                      "on upstream", err)
        self.assertEqual(self.chat_posts("z-ai/glm-5.3"), [])
        self.assertTrue(self.chat_posts("moonshotai/kimi-k3"))

    def test_allowlist_does_not_pin_an_internal_run(self):
        code, out, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                      "--yes", "--no-cross-examine")
        self.assertEqual(code, 0, err)
        tee = self.chat_posts()
        self.assertEqual({r[2]["model"] for r in tee}, {"z-ai/glm-5.3", "moonshotai/kimi-k3"})
        for r in tee:
            self.assertEqual(r[2]["provider"], provider_for(r[2]["model"], False))


# ---------------------------------------------------------------------------------------------
# CHORUS_NEVER_SEND


class NeverSendTest(ChorusCase):
    def refused(self, path, *argv, flag="--file"):
        code, out, err = self.run_cli("--sensitivity", "internal", flag, path, *argv, "--yes")
        self.assertEqual(code, 2, err)
        self.assertIn("is on the never-send list", err)
        self.assertIn("Nothing sent.", err)
        self.assertEqual(self.server.state["requests"], [])
        return err

    def test_entries_are_pathsep_separated(self):
        a, b = os.path.join(self.tmp.name, "vault-a"), os.path.join(self.tmp.name, "vault-b")
        spec = os.pathsep.join([a, "", "  ", b + " "])
        with mock.patch.dict(os.environ, {"CHORUS_NEVER_SEND": spec}):
            cfg = captured(chorus.load_config, ns())[0]
            self.assertIn(("CHORUS_NEVER_SEND entry %s" % a, a), cfg["never_send"])
            self.assertIn(("CHORUS_NEVER_SEND entry %s" % b, b), cfg["never_send"])
            self.assertEqual(len([1 for label, _ in cfg["never_send"]
                                  if label.startswith("CHORUS_NEVER_SEND")]), 2)
            for root in (a, b):
                with self.subTest(root=root):
                    err = self.refused(self.draft(sub=os.path.basename(root)))
                    self.assertIn("it is under CHORUS_NEVER_SEND entry %s" % root, err)

    def test_tilde_entry_is_read_against_home(self):
        with mock.patch.dict(os.environ, {"CHORUS_NEVER_SEND": "~/private-notes"}):
            path = write(os.path.join(self.home, "private-notes", "plan.md"), DRAFT)
            err = self.refused(path)
        self.assertIn("CHORUS_NEVER_SEND entry ~/private-notes", err)  # read against every home

    def test_entry_naming_a_file_refuses_that_file(self):
        path = self.draft(name="ledger.md")
        with mock.patch.dict(os.environ, {"CHORUS_NEVER_SEND": path}):
            err = self.refused(path)
        self.assertIn("it is CHORUS_NEVER_SEND entry %s" % path, err)

    def test_sibling_with_a_shared_prefix_is_not_refused(self):
        notes = os.path.join(self.tmp.name, "x", "notes")
        os.makedirs(notes)
        with mock.patch.dict(os.environ, {"CHORUS_NEVER_SEND": notes}):
            self.refused(self.draft(sub=os.path.join("x", "notes")))
            for sub, name in ((os.path.join("x", "notes2"), "a.md"), ("x", "notes.md")):
                with self.subTest(sub=sub, name=name):
                    code, out, err = self.run_cli("--sensitivity", "internal", "--file",
                                                  self.draft(name=name, sub=sub), "--dry-run")
                    self.assertEqual(code, 0, err)

    def test_case_variant_of_an_entry_is_refused(self):
        path = self.draft(sub="Vault")
        with mock.patch.dict(os.environ, {
                "CHORUS_NEVER_SEND": os.path.join(self.tmp.name, "VAULT")}):
            self.refused(path)

    def test_symlink_into_an_entry_is_refused(self):
        target = self.draft(sub="vault")
        link = os.path.join(self.tmp.name, "innocent.md")
        os.symlink(target, link)
        with mock.patch.dict(os.environ, {
                "CHORUS_NEVER_SEND": os.path.join(self.tmp.name, "vault")}):
            self.refused(link)

    def test_defaults_are_refused_under_the_scratch_home(self):
        for rel, why in ((os.path.join(".ssh", "notes.md"), "it is under ~/.ssh"),
                         (os.path.join(".aws", "credentials.md"), "it is under ~/.aws"),
                         (".netrc", "it is ~/.netrc")):
            with self.subTest(rel=rel):
                path = write(os.path.join(self.home, rel), DRAFT)
                self.assertIn(why, self.refused(path))

    def test_env_file_itself_is_refused_as_file(self):
        err = self.refused(self.env_file)
        self.assertIn("--file %s is on the never-send list (it is the env file)" % self.env_file,
                      err)

    def test_env_file_itself_is_refused_as_criteria(self):
        err = self.refused(self.env_file, "--file", self.draft(), flag="--criteria")
        self.assertIn("--criteria %s is on the never-send list (it is the env file)"
                      % self.env_file, err)

    def test_env_file_given_by_flag_is_refused_too(self):
        other = write(os.path.join(self.tmp.name, "keys", "flag.env"), "A_SETTING=some-setting\n")
        err = self.refused(other, "--env-file", other)
        self.assertIn("it is the env file", err)

    def stopped(self, spec):
        """A --yes run with CHORUS_NEVER_SEND set to `spec`: (exit code, stderr)."""
        with mock.patch.dict(os.environ, {"CHORUS_NEVER_SEND": spec}):
            code, out, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                          "--yes")
        self.assertEqual(self.server.state["requests"], [])
        return code, err

    def test_entry_that_looks_like_two_paths_exits_1(self):
        a, b = os.path.join(self.tmp.name, "a"), os.path.join(self.tmp.name, "b")
        for spec in ("~/a;~/b", "~/a, ~/b", "~/a,~/b", "%s;%s" % (a, b), "%s, %s" % (a, b)):
            with self.subTest(spec=spec):
                code, err = self.stopped(spec)
                self.assertEqual(code, 1, err)
                self.assertIn("CHORUS_NEVER_SEND entry %r looks like two paths: separate entries "
                              "with %r" % (spec, os.pathsep), err)

    def test_existing_path_with_a_separator_in_its_name_is_an_entry(self):
        odd = os.path.join(self.home, "a, ~", "b")  # what "~/a, ~/b" names, read literally
        os.makedirs(odd)
        with mock.patch.dict(os.environ, {"CHORUS_NEVER_SEND": "~/a, ~/b"}):
            code, out, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                          "--dry-run")
            self.assertEqual(code, 0, err)
            self.assertNotIn("looks like two paths", err)
            self.assertNotIn("names nothing", err)
            err = self.refused(write(os.path.join(odd, "plan.md"), DRAFT))
        self.assertIn("it is under CHORUS_NEVER_SEND entry ~/a, ~/b", err)

    def test_relative_entry_exits_1(self):
        for spec in ("private-notes", "notes/private", "./vault", "~nosuchuser/x",
                     os.pathsep.join([os.path.join(self.tmp.name, "vault"), "relative-too"])):
            bad = spec.split(os.pathsep)[-1]
            with self.subTest(spec=spec):
                code, err = self.stopped(spec)
                self.assertEqual(code, 1, err)
                self.assertIn("CHORUS_NEVER_SEND entry %r must be an absolute path or start with "
                              "~/" % bad, err)

    def test_entry_naming_nothing_gets_a_plan_note(self):
        ghost = os.path.join(self.tmp.name, "no-such-folder")
        real = os.path.join(self.tmp.name, "vault")
        os.makedirs(real)
        os.makedirs(os.path.join(self.home, "kept-notes"))
        spec = os.pathsep.join([ghost, real, "~/no-such-notes", "~/kept-notes"])
        with mock.patch.dict(os.environ, {"CHORUS_NEVER_SEND": spec}):
            code, out, err = self.run_cli("--sensitivity", "internal", "--file", self.draft(),
                                          "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("note: CHORUS_NEVER_SEND entry %s names nothing on this machine (a typo?)"
                      % ghost, err)
        self.assertIn("note: CHORUS_NEVER_SEND entry ~/no-such-notes names nothing on this "
                      "machine", err)
        self.assertNotIn("entry %s names nothing" % real, err)
        self.assertNotIn("entry ~/kept-notes names nothing", err)
        self.assertEqual(self.server.state["requests"], [])

    def test_chorus_config_folder_is_refused_under_the_scratch_home(self):
        self.assertIn("~/.config/chorus", chorus.NEVER_SEND_DEFAULTS)
        for name in ("notes.md", "chorus.env"):
            with self.subTest(name=name):
                path = write(os.path.join(self.home, ".config", "chorus", name), DRAFT)
                self.assertIn("it is under ~/.config/chorus", self.refused(path))


# ---------------------------------------------------------------------------------------------
# CHORUS_ORG_EMAIL_DOMAINS


class OrgEmailDomainsTest(ChorusCase):
    def domains(self, spec):
        with mock.patch.dict(os.environ, {"CHORUS_ORG_EMAIL_DOMAINS": spec}):
            return captured(chorus.load_config, ns())[0]["org_email_domains"]

    def flagged(self, address, spec):
        org = self.domains(spec) if spec is not None else ()
        return chorus.screen("draft", clean_draft(address), [], org)[1]

    def test_domains_are_comma_or_space_separated(self):
        self.assertEqual(self.domains("tallowmere.example, fernquill.example  acme.example\t"
                                      "lists.example,"),
                         ("tallowmere.example", "fernquill.example", "acme.example",
                          "lists.example"))
        self.assertEqual(self.domains("Tallowmere.Example,tallowmere.example"),
                         ("tallowmere.example",))
        self.assertEqual(self.domains(""), ())

    def test_leading_at_or_dot_is_tolerated(self):
        self.assertEqual(self.domains("@tallowmere.example .fernquill.example"),
                         ("tallowmere.example", "fernquill.example"))
        for spec in ("@tallowmere.example", ".tallowmere.example"):
            with self.subTest(spec=spec):
                self.assertEqual(self.flagged("jane.doe@tallowmere.example", spec), [])

    def test_address_on_an_org_domain_is_not_flagged(self):
        address = "jane.doe@tallowmere.example"
        self.assertEqual(self.flagged(address, None), ["draft line 2: email address"])
        self.assertEqual(self.flagged(address, "tallowmere.example"), [])
        self.assertEqual(self.flagged(address, "fernquill.example, tallowmere.example"), [])

    def test_subdomains_are_covered(self):
        for address in ("jane.doe@mail.tallowmere.example", "ops@a.b.tallowmere.example"):
            with self.subTest(address=address):
                self.assertEqual(self.flagged(address, "tallowmere.example"), [])

    def test_other_domains_are_still_flagged(self):
        for address in ("jane.doe@example.com", "jane.doe@nottallowmere.example",
                        "jane.doe@tallowmere.example.net", "jane.roe@fernquill.example"):
            with self.subTest(address=address):
                self.assertEqual(self.flagged(address, "tallowmere.example"),
                                 ["draft line 2: email address"])

    def test_org_domains_end_to_end(self):
        draft = self.draft("The Fernquill Fund reviewed the season plan.\n"
                           "Write to ops@tallowmere.example.\nOr to jane.roe@example.org.\n")
        code, out, err = self.run_cli("--sensitivity", "internal", "--file", draft, "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("draft line 2: email address", err)
        self.assertIn("draft line 3: email address", err)
        with mock.patch.dict(os.environ, {"CHORUS_ORG_EMAIL_DOMAINS": "@tallowmere.example"}):
            code, out, err = self.run_cli("--sensitivity", "internal", "--file", draft,
                                          "--dry-run")
            self.assertEqual(code, 2)
            self.assertNotIn("draft line 2: email address", err)
            self.assertIn("draft line 3: email address", err)  # the example.org address
            code, out, err = self.run_cli(
                "--sensitivity", "internal", "--dry-run", "--file",
                self.draft(clean_draft("ops@tallowmere.example"), "org-only.md"))
            self.assertEqual(code, 0, err)
        self.assertEqual(self.server.state["requests"], [])

    def test_single_label_or_malformed_domain_exits_1(self):
        # a bare 'com' would unflag every .com address
        cases = {"com": "com", "org": "org", "a..b": "a..b", "acme.": "acme.",
                 "@com": "com", "tallowmere.example, com": "com",
                 "jane@tallowmere.example": "jane@tallowmere.example",
                 "acme-.example": "acme-.example"}
        for spec, bad in cases.items():
            with self.subTest(spec=spec), mock.patch.dict(os.environ,
                                                          {"CHORUS_ORG_EMAIL_DOMAINS": spec}):
                cfg, code, err = captured(chorus.load_config, ns())
                self.assertEqual(code, 1, err)
                self.assertIn("CHORUS_ORG_EMAIL_DOMAINS: %r is not a domain like example.org"
                              % bad, err)
                code, out, err = self.run_cli(
                    "--sensitivity", "internal", "--file",
                    self.draft(clean_draft("ops@tallowmere.example")), "--yes")
                self.assertEqual(code, 1, err)
                self.assertIn("is not a domain like example.org", err)
        self.assertEqual(self.server.state["requests"], [])

    def test_a_subdomain_entry_covers_that_subdomain_only(self):
        spec = "mail.tallowmere.example"
        self.assertEqual(self.domains(spec), ("mail.tallowmere.example",))
        for address in ("jane.doe@mail.tallowmere.example", "ops@eu.mail.tallowmere.example"):
            with self.subTest(address=address):
                self.assertEqual(self.flagged(address, spec), [])
        self.assertEqual(self.flagged("jane.doe@tallowmere.example", spec),
                         ["draft line 2: email address"])
        with mock.patch.dict(os.environ, {"CHORUS_ORG_EMAIL_DOMAINS": spec}):
            code, out, err = self.run_cli(
                "--sensitivity", "internal", "--dry-run", "--file",
                self.draft(clean_draft("jane.doe@mail.tallowmere.example")))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.server.state["requests"], [])


# ---------------------------------------------------------------------------------------------
# The use-case loader


HAT = '''
[[hats]]
name = "steward"
title = "season steward"
briefs = ["refute"]
looks_for = "You are the steward who signs off the season."
'''
MINIMAL = 'purpose = "a field report"\nsensitivity = ["public"]\n' + HAT

# A valid use case outside the shipped library, with a brief and a verdict of its own.
FIELD_REPORT = '''
purpose = """a field report,
read by the people who fund the next season"""
sensitivity = ["public", "internal"]

[briefs.ledger-check]
text = \'\'\'
Check every figure in the report against the others
and against      the season's plan.
\'\'\'

[[hats]]
name = "steward"
title = "season steward"
briefs = ["ledger-check", "refute"]
looks_for = \'\'\'
You are the steward who signs off the season. You read for
what the report promises the next season.
\'\'\'

[output]
verdict = \'\'\'whether the steward would sign off
the season\'\'\'
guidance = "Relay the quoted line to the author."
'''
FIELD_BRIEF = "Check every figure in the report against the others and against the season's plan."
FIELD_VERDICT = "whether the steward would sign off the season"
FIELD_ROLE = ("You are the steward who signs off the season. You read for what the report "
              "promises the next season.")


class UseCaseLoaderTest(ChorusCase):
    def bad(self, text, name="probe"):
        """Write a use case and load it: (exit code, stderr, path)."""
        path = os.path.join(self.tmp.name, "uc", name + ".toml")
        write(path, text, "wb" if isinstance(text, bytes) else "w")
        uc, code, err = captured(chorus.load_use_case, path)
        return code, err, path

    def assertRejected(self, text, why="", name="probe"):
        code, err, path = self.bad(text, name)
        self.assertEqual(code, 1, err)
        self.assertIn("use case %s: " % path, err)
        self.assertIn(why, err)
        return err

    def custom_dir(self, files):
        d = os.path.join(self.tmp.name, "my-use-cases")
        for name, text in files.items():
            write(os.path.join(d, name), text)
        return d

    # the shipped library

    def test_shipped_use_cases_load(self):
        cases = self.use_cases()
        self.assertEqual(sorted(cases), ["code", "contracts", "copy", "grant", "startup"])
        for name, uc in cases.items():
            self.assertEqual(uc["name"], name)
            self.assertEqual(uc["path"], os.path.join(chorus.BUILTIN_USE_CASES, name + ".toml"))

    def test_shipped_use_cases_match_the_documented_shape(self):
        ladder = list(chorus.SENSITIVITY)
        keys = {"name", "path", "what", "classes", "redacted", "class_note", "class_briefs",
                "briefs", "criteria_briefs", "hats", "verdict", "rewrites", "guidance"}
        one_line = lambda s: isinstance(s, str) and s.strip() and s == " ".join(s.split())
        for name, uc in self.use_cases().items():
            with self.subTest(use_case=name):
                self.assertEqual(set(uc), keys)
                self.assertTrue(one_line(uc["what"]))
                self.assertIsInstance(uc["classes"], tuple)
                self.assertTrue(uc["classes"])
                self.assertEqual(list(uc["classes"]), [c for c in ladder if c in uc["classes"]])
                self.assertIsInstance(uc["redacted"], bool)
                known = set(chorus.BRIEFS) | set(uc["briefs"])
                self.assertTrue(uc["hats"])
                for hn, h in uc["hats"].items():
                    self.assertRegex(hn, chorus.NAME_RE)
                    self.assertEqual(set(h), {"title", "briefs", "role"})
                    self.assertTrue(one_line(h["title"]))
                    self.assertTrue(one_line(h["role"]))
                    self.assertTrue(h["briefs"])
                    self.assertLessEqual(set(h["briefs"]), known)
                for b, text in uc["briefs"].items():
                    self.assertNotIn(b, chorus.BRIEFS)
                    self.assertTrue(one_line(text))
                self.assertIsInstance(uc["criteria_briefs"], tuple)
                self.assertIn("fit", uc["criteria_briefs"])
                self.assertLessEqual(set(uc["criteria_briefs"]), known)
                self.assertTrue(one_line(uc["verdict"]))
                self.assertIsInstance(uc["rewrites"], tuple)
                self.assertLessEqual(set(uc["rewrites"]), set(uc["classes"]))
                self.assertLessEqual(set(uc["class_note"]), set(uc["classes"]))
                for c, extra in uc["class_briefs"].items():
                    self.assertIn(c, uc["classes"])
                    self.assertLessEqual(set(extra), set(uc["hats"]))
                self.assertTrue(uc["guidance"] == "" or one_line(uc["guidance"]))

    def test_shipped_use_cases_match_the_documented_ladder_and_hats(self):
        cases = self.use_cases()
        want = {"contracts": (("confidential",), True, ["counsel", "commercial", "compliance"]),
                "code": (("public", "confidential"), False, ["security", "sre", "reviewer"]),
                "copy": (("public", "internal", "confidential"), False,
                         ["copywriter", "fundraiser", "vc", "lp"]),
                "startup": (("confidential",), True, ["vc", "operator", "lp", "techdd"]),
                "grant": (("internal", "confidential"), False,
                          ["program-officer", "impact-evaluator", "budget-reviewer"])}
        for name, (classes, redacted, hats) in want.items():
            with self.subTest(use_case=name):
                self.assertEqual(cases[name]["classes"], classes)
                self.assertIs(cases[name]["redacted"], redacted)
                self.assertEqual(list(cases[name]["hats"]), hats)
        self.assertEqual(cases["copy"]["rewrites"], ("public",))
        self.assertEqual([n for n, uc in cases.items() if uc["rewrites"]], ["copy"])
        self.assertEqual(cases["copy"]["class_briefs"], {"internal": {"fundraiser": ["fit"]}})
        self.assertIn("next-step", cases["copy"]["briefs"])
        self.assertEqual(cases["grant"]["criteria_briefs"], ("budget", "fit", "impact"))
        for h in cases["contracts"]["hats"].values():
            self.assertIn("[CLIENT]", h["role"])

    # every validation error exits 1, naming the file

    def test_invalid_toml_exits_1(self):
        self.assertRejected('purpose = "a field report\nsensitivity = ["public"]\n',
                            "not valid TOML")

    def test_use_case_not_utf8_exits_1(self):
        self.assertRejected(b'purpose = "caf\xe9 \xff"\nsensitivity = ["public"]\n' +
                            HAT.encode())

    def test_unknown_top_level_key_exits_1(self):
        self.assertRejected('colour = "teal"\n' + MINIMAL, "unknown key(s): colour")

    def test_top_level_key_after_a_table_header_error_is_clear(self):
        """TOML puts a key written after a table header inside that table. The use-case README
        promises the loader says so: the error names the key and the table it landed in."""
        cases = {"in-hat": (MINIMAL + 'shared_role = "Read for the steward."\n', "shared_role",
                            r"hat 1|\[\[hats\]\]"),
                 "in-output": (MINIMAL + '[output]\nverdict = "v"\nredacted = true\n',
                               "redacted", r"\[?output\]?"),
                 "sensitivity-in-hat": ('purpose = "a field report"\n' + HAT +
                                        'sensitivity = ["public"]\n', "sensitivity",
                                        r"hat 1|\[\[hats\]\]")}
        for name, (text, key, table) in cases.items():
            with self.subTest(case=name):
                code, err, path = self.bad(text, name)
                self.assertEqual(code, 1, err)
                self.assertIn(path, err)
                self.assertRegex(err, r"\b%s\b" % key, "the error does not name the key")
                self.assertRegex(err, table, "the error does not name the table")

    def test_missing_purpose_exits_1(self):
        self.assertRejected('sensitivity = ["public"]\n' + HAT, "purpose must be non-empty text")

    def test_empty_unknown_or_duplicate_sensitivity_exits_1(self):
        for i, levels in enumerate(('[]', '["secret"]', '["public", "public"]', '"public"')):
            with self.subTest(levels=levels):
                self.assertRejected('purpose = "a field report"\nsensitivity = %s\n' % levels
                                    + HAT, "sensitivity must list one or more of public, "
                                    "internal, confidential, once each", "s%d" % i)
        self.assertRejected('purpose = "a field report"\n' + HAT,
                            "sensitivity must list one or more", "s-missing")

    def test_non_bool_redacted_exits_1(self):
        self.assertRejected('redacted = "yes"\n' + MINIMAL, "redacted must be true or false")

    def test_brief_named_like_a_core_brief_exits_1(self):
        self.assertRejected(MINIMAL + '[briefs.refute]\ntext = "Refute it again."\n',
                            "brief 'refute' is a core brief")

    def test_brief_with_an_unknown_field_exits_1(self):
        self.assertRejected(MINIMAL.replace('"refute"]', '"tally"]') +
                            '[briefs.tally]\ntext = "Tally it."\nweight = 2\n',
                            "briefs.tally takes text and, optionally, reads_criteria")

    def test_non_bool_reads_criteria_exits_1(self):
        self.assertRejected(MINIMAL.replace('"refute"]', '"tally"]') +
                            '[briefs.tally]\ntext = "Tally it."\nreads_criteria = "yes"\n',
                            "briefs.tally.reads_criteria must be true or false")

    def test_brief_with_empty_text_exits_1(self):
        self.assertRejected(MINIMAL.replace('"refute"]', '"tally"]') +
                            '[briefs.tally]\ntext = "   "\n', "briefs.tally.text must be "
                            "non-empty text")

    def test_no_hats_exits_1(self):
        self.assertRejected('purpose = "a field report"\nsensitivity = ["public"]\n',
                            "it needs at least one [[hats]] table")

    def test_hat_with_an_unknown_field_exits_1(self):
        self.assertRejected(MINIMAL + 'colour = "teal"\n',
                            "hat 1 takes briefs, looks_for, name, title only")

    def test_hat_missing_title_or_role_exits_1(self):
        self.assertRejected(MINIMAL.replace('title = "season steward"\n', ''),
                            "hat steward: title must be non-empty text", "no-title")
        self.assertRejected(MINIMAL.replace(
            'looks_for = "You are the steward who signs off the season."\n', ''),
            "hat steward: looks_for must be non-empty text", "no-role")

    def test_hat_name_with_capitals_exits_1(self):
        self.assertRejected(MINIMAL.replace('name = "steward"', 'name = "Steward"'),
                            "hat 1: name must be lowercase letters, digits and hyphens")

    def test_duplicate_hat_names_exit_1(self):
        self.assertRejected(MINIMAL + HAT, "hat 'steward' is defined twice")

    def test_hat_brief_that_does_not_exist_exits_1(self):
        self.assertRejected(MINIMAL.replace('"refute"]', '"nowhere"]'),
                            "hat steward: briefs: unknown brief 'nowhere'")

    def test_extra_briefs_for_a_disallowed_sensitivity_exits_1(self):
        self.assertRejected(MINIMAL + '[extra_briefs.confidential]\nsteward = ["redteam"]\n',
                            "extra_briefs.confidential: a table for one of this use case's "
                            "sensitivities (public)")

    def test_extra_briefs_for_an_unknown_hat_exits_1(self):
        self.assertRejected(MINIMAL + '[extra_briefs.public]\nnobody = ["redteam"]\n',
                            "extra_briefs.public: unknown hat 'nobody'")

    def test_extra_briefs_not_a_table_exits_1(self):
        self.assertRejected('purpose = "a field report"\nsensitivity = ["public"]\n'
                            'extra_briefs = "redteam"\n' + HAT, "extra_briefs")

    def test_sensitivity_notes_for_a_disallowed_sensitivity_exits_1(self):
        self.assertRejected(MINIMAL + '[sensitivity_notes]\nconfidential = "check it"\n',
                            "sensitivity_notes.confidential: not one of this use case's "
                            "sensitivities (public)")

    def test_sensitivity_notes_not_a_table_exits_1(self):
        self.assertRejected('purpose = "a field report"\nsensitivity = ["public"]\n'
                            'sensitivity_notes = ["check it"]\n' + HAT, "sensitivity_notes")

    def test_output_unknown_key_exits_1(self):
        self.assertRejected(MINIMAL + '[output]\nverdict = "v"\ntone = "warm"\n',
                            "[output] takes guidance, rewrites, verdict only")

    def test_output_rewrites_outside_the_allowed_sensitivities_exits_1(self):
        self.assertRejected(MINIMAL + '[output]\nrewrites = ["internal"]\n',
                            "output.rewrites must list sensitivities this use case allows "
                            "(public)")

    def test_brief_no_hat_runs_exits_1(self):
        self.assertRejected(MINIMAL + '[briefs.idle]\ntext = "Sit idle."\n',
                            "brief 'idle' is defined but no hat runs it")

    def test_brief_run_only_through_extra_briefs_counts_as_run(self):
        code, err, path = self.bad(MINIMAL + '[briefs.tally]\ntext = "Tally it."\n'
                                   '[extra_briefs.public]\nsteward = ["tally"]\n')
        self.assertIsNone(code, err)

    def test_bad_file_name_exits_1(self):
        for name in ("FieldReport", "field_report", "-field"):
            with self.subTest(name=name):
                self.assertRejected(MINIMAL, "the file name is the --use-case name: lowercase "
                                             "letters, digits and hyphens", name)

    def test_bad_use_case_in_a_folder_stops_every_run(self):
        d = self.custom_dir({"broken.toml": MINIMAL + 'colour = "teal"\n'})
        code, out, err = self.run_cli("--sensitivity", "public", "--file", self.draft(),
                                      "--use-case-dir", d, "--yes")
        self.assertEqual(code, 1)
        self.assertIn(os.path.join(d, "broken.toml"), err)
        self.assertEqual(self.server.state["requests"], [])

    # custom use cases

    def test_multiline_prose_folds_to_single_spaces(self):
        path = write(os.path.join(self.tmp.name, "uc", "field-report.toml"), FIELD_REPORT)
        uc = captured(chorus.load_use_case, path)[0]
        self.assertEqual(uc["what"], "a field report, read by the people who fund the next season")
        self.assertEqual(uc["briefs"]["ledger-check"], FIELD_BRIEF)
        self.assertEqual(uc["hats"]["steward"]["role"], FIELD_ROLE)
        self.assertEqual(uc["verdict"], FIELD_VERDICT)

    def test_shared_role_is_appended_to_every_hat(self):
        path = write(os.path.join(self.tmp.name, "uc", "two-hats.toml"),
                     'purpose = "x"\nsensitivity = ["public"]\nshared_role = """Judge by\n'
                     'the draft only."""\n' + HAT + HAT.replace('"steward"', '"auditor"'))
        uc = captured(chorus.load_use_case, path)[0]
        for h in uc["hats"].values():
            self.assertTrue(h["role"].endswith(" Judge by the draft only."), h["role"])

    def test_custom_use_case_is_listed_with_its_path(self):
        d = self.custom_dir({"field-report.toml": FIELD_REPORT, "README.md": "not a use case"})
        for argv, env in ((["--use-case-dir", d], {}), ([], {"CHORUS_USE_CASE_DIR": d})):
            with self.subTest(argv=argv, env=env), mock.patch.dict(os.environ, env):
                code, out, err = self.run_cli("--list-use-cases", *argv)
                self.assertEqual(code, 0, err)
                self.assertIn("## field-report — a field report, read by the people who fund "
                              "the next season", out)
                self.assertIn("from %s" % os.path.join(d, "field-report.toml"), out)
                self.assertIn("verdict: %s" % FIELD_VERDICT, out)
                self.assertNotIn("README", out)
                for shipped in ("code", "contracts", "copy", "grant", "startup"):
                    self.assertIn("## %s — " % shipped, out)
                self.assertNotIn("from %s" % chorus.BUILTIN_USE_CASES, out)

    def test_use_case_dirs_from_flag_and_variable_combine(self):
        a = self.custom_dir({"field-report.toml": FIELD_REPORT})
        b = os.path.join(self.tmp.name, "more")
        write(os.path.join(b, "board-memo.toml"), MINIMAL)
        with mock.patch.dict(os.environ, {"CHORUS_USE_CASE_DIR": b}):
            code, out, err = self.run_cli("--list-use-cases", "--use-case-dir", a)
        self.assertEqual(code, 0, err)
        self.assertIn("## field-report", out)
        self.assertIn("## board-memo", out)

    def test_custom_use_case_dry_run(self):
        d = self.custom_dir({"field-report.toml": FIELD_REPORT})
        code, out, err = self.run_cli("--use-case-dir", d, "--use-case", "field-report",
                                      "--sensitivity", "public", "--file", self.draft(),
                                      "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("use case: field-report — a field report", err)
        self.assertIn("briefs=ledger-check,refute", err)
        self.assertIn("dry run: guards passed; nothing sent.", err)
        self.assertEqual(self.server.state["requests"], [])

    def test_custom_use_case_runs_end_to_end_with_its_brief_and_verdict(self):
        d = self.custom_dir({"field-report.toml": FIELD_REPORT})
        self.server.state["chat"] = hat_chat
        self.server.state["openai_fn"] = hat_openai
        code, out, err = self.run_cli("--use-case-dir", d, "--use-case", "field-report",
                                      "--sensitivity", "public", "--file", self.draft(),
                                      "--yes", "--json")
        self.assertEqual(code, 0, err)
        prompts = [r[2]["instructions"] for r in self.round1("/responses")] + [
            r[2]["messages"][0]["content"] for r in self.round1("/chat/completions")]
        self.assertEqual(len(prompts), 3)
        for p in prompts:
            self.assertIn("- ledger-check: %s" % FIELD_BRIEF, p)
            self.assertIn('"overall": "<two or three sentences: %s>"' % FIELD_VERDICT, p)
            self.assertIn("- steward (season steward): %s" % FIELD_ROLE, p)
            self.assertNotIn(chorus.DEFAULT_VERDICT, p)
        res = json.loads(out)
        self.assertEqual(res["plan"]["use_case"], "field-report")
        self.assertIn("Relay the quoted line to the author.", res["report"])
        for r in res["results"].values():
            self.assertEqual(r["hats"], ["steward"])
            self.assertTrue(r["findings"])
            self.assertTrue(all(f["hat"] == "steward" for f in r["findings"]))

    def test_custom_use_case_cannot_shadow_a_shipped_name(self):
        d = self.custom_dir({"copy.toml": MINIMAL})
        code, out, err = self.run_cli("--list-use-cases", "--use-case-dir", d)
        self.assertEqual(code, 1)
        self.assertIn("use case 'copy' is defined twice (%s and %s): rename one" % (
            os.path.join(chorus.BUILTIN_USE_CASES, "copy.toml"), os.path.join(d, "copy.toml")),
            err)

    def test_same_name_in_two_custom_folders_is_refused(self):
        a = self.custom_dir({"board-memo.toml": MINIMAL})
        b = os.path.join(self.tmp.name, "more")
        write(os.path.join(b, "board-memo.toml"), MINIMAL)
        code, out, err = self.run_cli("--list-use-cases", "--use-case-dir", a,
                                      "--use-case-dir", b)
        self.assertEqual(code, 1)
        self.assertIn("use case 'board-memo' is defined twice", err)

    def test_missing_use_case_dir_exits_1(self):
        missing = os.path.join(self.tmp.name, "no-such-folder")
        for argv, env in ((["--use-case-dir", missing], {}),
                          ([], {"CHORUS_USE_CASE_DIR": missing})):
            with self.subTest(argv=argv, env=env), mock.patch.dict(os.environ, env):
                code, out, err = self.run_cli("--list-use-cases", *argv)
                self.assertEqual(code, 1)
                self.assertIn("use-case folder %s does not exist" % missing, err)

    def test_unknown_use_case_name_exits_1_listing_the_known_ones(self):
        d = self.custom_dir({"field-report.toml": FIELD_REPORT})
        code, out, err = self.run_cli("--use-case-dir", d, "--use-case", "nowhere",
                                      "--sensitivity", "public", "--file", self.draft(),
                                      "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("unknown use case 'nowhere' (known: code, contracts, copy, field-report, "
                      "grant, startup; --list-use-cases shows them)", err)

    def test_use_case_allowed_only_at_confidential_implies_it(self):
        d = self.custom_dir({"board-memo.toml": MINIMAL.replace('["public"]',
                                                                 '["confidential"]')})
        code, out, err = self.run_cli("--use-case-dir", d, "--use-case", "board-memo",
                                      "--file", self.draft(), "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("note: --use-case board-memo implies --sensitivity confidential", err)
        self.assertIn("chorus → sensitivity=confidential", err)
        self.assertRegex(err, r"(?m)^  kimi +moonshotai/")
        self.assertRegex(err, r"(?m)^  glm +z-ai/")
        self.assertNotRegex(err, r"(?m)^  openai ")
        self.assertEqual(self.server.state["requests"], [])

    def test_use_case_allowed_at_more_than_one_level_implies_none(self):
        d = self.custom_dir({"board-memo.toml": MINIMAL.replace(
            '["public"]', '["internal", "confidential"]')})
        for argv in (["--use-case-dir", d, "--use-case", "board-memo"],
                     ["--use-case", "grant"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(*argv, "--file", self.draft(), "--yes")
                self.assertEqual(code, 1)
                self.assertIn("STOP: declare the content's sensitivity with --sensitivity "
                              "(internal, confidential).", err)
                self.assertNotIn("implies", err)
        self.assertEqual(self.server.state["requests"], [])

    # a folder or a file reached twice is read once

    def assertListedOnce(self, argv, env=None, names=("field-report",)):
        with mock.patch.dict(os.environ, env or {}):
            code, out, err = self.run_cli("--list-use-cases", *argv)
        self.assertEqual(code, 0, err)
        for name in tuple(names) + ("code", "contracts", "copy", "grant", "startup"):
            self.assertEqual(out.count("## %s — " % name), 1, name)
        return out

    def test_a_folder_named_twice_is_read_once(self):
        d = self.custom_dir({"field-report.toml": FIELD_REPORT})
        link = os.path.join(self.tmp.name, "linked-use-cases")
        os.symlink(d, link)
        cases = {"flag and variable": (["--use-case-dir", d], {"CHORUS_USE_CASE_DIR": d}),
                 "trailing slash": (["--use-case-dir", d + os.sep], {"CHORUS_USE_CASE_DIR": d}),
                 "flag twice": (["--use-case-dir", d, "--use-case-dir", d + os.sep], {}),
                 "variable twice": ([], {"CHORUS_USE_CASE_DIR": os.pathsep.join([d, d + os.sep])}),
                 "symlink to the folder": (["--use-case-dir", d, "--use-case-dir", link], {}),
                 "symlink first": (["--use-case-dir", link], {"CHORUS_USE_CASE_DIR": d})}
        for label, (argv, env) in cases.items():
            with self.subTest(case=label):
                self.assertListedOnce(argv, env)
                with mock.patch.dict(os.environ, env):
                    code, out, err = self.run_cli(*argv, "--use-case", "field-report",
                                                  "--sensitivity", "public", "--file",
                                                  self.draft(), "--dry-run")
                self.assertEqual(code, 0, err)
                self.assertIn("use case: field-report — a field report", err)
        self.assertEqual(self.server.state["requests"], [])

    def test_the_built_in_folder_named_as_a_custom_folder_is_read_once(self):
        builtin = chorus.BUILTIN_USE_CASES
        link = os.path.join(self.tmp.name, "shipped-link")
        os.symlink(builtin, link)
        for argv, env in ((["--use-case-dir", builtin], {}), ([], {"CHORUS_USE_CASE_DIR": builtin}),
                          (["--use-case-dir", builtin + os.sep], {}),
                          (["--use-case-dir", link], {})):
            with self.subTest(argv=argv, env=env):
                out = self.assertListedOnce(argv, env, names=())
                self.assertNotRegex(out, r"(?m)^from ")  # each still from the built-in folder
        code, out, err = self.run_cli("--use-case-dir", builtin, "--use-case", "copy",
                                      "--sensitivity", "public", "--file", self.draft(),
                                      "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.server.state["requests"], [])

    def test_one_file_linked_into_two_folders_is_read_once(self):
        a = self.custom_dir({"field-report.toml": FIELD_REPORT})
        real = os.path.join(a, "field-report.toml")
        for how in ("symlink", "hard link"):
            b = os.path.join(self.tmp.name, "more-" + how.replace(" ", "-"))
            os.makedirs(b)
            (os.symlink if how == "symlink" else os.link)(real, os.path.join(b, "field-report.toml"))
            for argv in (["--use-case-dir", a, "--use-case-dir", b],
                         ["--use-case-dir", b, "--use-case-dir", a]):
                with self.subTest(how=how, argv=argv):
                    self.assertListedOnce(argv)

    def test_two_different_files_with_one_name_still_exit_1(self):
        a = self.custom_dir({"field-report.toml": FIELD_REPORT})
        b = os.path.join(self.tmp.name, "more")
        write(os.path.join(b, "field-report.toml"), FIELD_REPORT)  # a copy is another file
        code, out, err = self.run_cli("--list-use-cases", "--use-case-dir", a,
                                      "--use-case-dir", b)
        self.assertEqual(code, 1)
        self.assertIn("use case 'field-report' is defined twice (%s and %s): rename one" % (
            os.path.join(a, "field-report.toml"), os.path.join(b, "field-report.toml")), err)
        with open(os.path.join(chorus.BUILTIN_USE_CASES, "code.toml"), encoding="utf-8") as f:
            shipped = f.read()
        c = os.path.join(self.tmp.name, "copies")
        write(os.path.join(c, "code.toml"), shipped)  # byte for byte, still another file
        code, out, err = self.run_cli("--list-use-cases", "--use-case-dir", c)
        self.assertEqual(code, 1)
        self.assertIn("use case 'code' is defined twice", err)

    # keys TOML puts in the wrong table, reserved names, names with a trailing newline

    def test_top_level_key_under_an_extra_briefs_table_is_reported_as_misplaced(self):
        head = 'purpose = "a field report"\n'
        levels = 'sensitivity = ["public", "internal"]\n'
        table = '[extra_briefs.internal]\nsteward = ["redteam"]\n'
        cases = {"redacted": head + levels + HAT + table + "redacted = true\n",
                 "shared_role": head + levels + HAT + table + 'shared_role = "Read closely."\n',
                 "sensitivity": head + HAT + table + levels}
        for key, text in cases.items():
            with self.subTest(key=key):
                err = self.assertRejected(text, "[extra_briefs.internal] holds the key %r, which "
                                                "belongs at the top of the file" % key,
                                          "x-" + key.replace("_", "-"))
                self.assertIn("move %s above the first [table] header" % key, err)

    def test_hat_named_like_a_top_level_key_may_take_extra_briefs(self):
        text = ('purpose = "a field report"\nsensitivity = ["public", "internal"]\n'
                + HAT.replace('"steward"', '"output"') + '[extra_briefs.internal]\n'
                'output = ["redteam"]\n')
        code, err, path = self.bad(text, "hat-output")
        self.assertIsNone(code, err)

    def test_brief_named_like_a_top_level_key_is_reserved(self):
        for b in ("output", "hats", "sensitivity", "briefs"):
            with self.subTest(brief=b):
                self.assertRejected(MINIMAL.replace('"refute"]', '"%s"]' % b)
                                    + '[briefs.%s]\ntext = "Tally it."\n' % b,
                                    "brief name %r is reserved (it is a top-level key); rename "
                                    "the brief" % b, "r-" + b)

    def test_names_with_a_trailing_newline_are_refused(self):
        self.assertIsNotNone(chorus.NAME_RE.match("chair"))
        self.assertIsNone(chorus.NAME_RE.match("chair\n"))
        self.assertIsNotNone(chorus.UPSTREAM_NAME.match("upstream-a"))
        self.assertIsNone(chorus.UPSTREAM_NAME.match("upstream-a\n"))
        self.assertRejected(MINIMAL.replace('name = "steward"', 'name = "chair\\n"'),
                            "hat 1: name must be lowercase letters, digits and hyphens", "nl-hat")
        self.assertRejected(MINIMAL.replace('"refute"]', '"tally\\n"]')
                            + '[briefs."tally\\n"]\ntext = "Tally it."\n',
                            "brief name 'tally\\n': use lowercase letters, digits and hyphens",
                            "nl-brief")

    def test_non_string_entries_exit_1_cleanly(self):
        cases = {"nested-sensitivity": ('purpose = "a field report"\nsensitivity = [["public"]]\n'
                                        + HAT, "sensitivity must list one or more of public, "
                                        "internal, confidential, once each"),
                 "number-sensitivity": ('purpose = "a field report"\nsensitivity = [1]\n' + HAT,
                                        "sensitivity must list one or more"),
                 "number-rewrites": (MINIMAL + '[output]\nrewrites = [1]\n',
                                     "output.rewrites must list sensitivities this use case "
                                     "allows (public)"),
                 "nested-rewrites": (MINIMAL + '[output]\nrewrites = [["public"]]\n',
                                     "output.rewrites must list sensitivities")}
        for name, (text, why) in cases.items():
            with self.subTest(case=name):
                err = self.assertRejected(text, why, name)
                self.assertNotIn("Traceback", err)
        d = self.custom_dir({"nested-sensitivity.toml": cases["nested-sensitivity"][0]})
        code, out, err = self.run_cli("--list-use-cases", "--use-case-dir", d)
        self.assertEqual(code, 1)
        self.assertIn("use case %s: sensitivity must list" % os.path.join(
            d, "nested-sensitivity.toml"), err)
        self.assertNotIn("Traceback", err)

    def test_builtin_folder_is_found_from_the_harness(self):
        self.assertEqual(chorus.builtin_note(), "")
        code, out, err = self.run_cli("--use-case", "nowhere", "--sensitivity", "public",
                                      "--file", self.draft(), "--dry-run")
        self.assertEqual(code, 1)
        self.assertNotIn("was not found", err)

    # the use case's own text goes to every voice, so it is screened like the draft

    def test_use_case_text_carrying_a_credential_is_refused(self):
        token = dict(ln.split("=", 1) for ln in ENV_FILE.splitlines())["SOME_API_TOKEN"]
        shaped = made("sk", "-", "Qx7" * 8)
        places = {
            "hat role": (lambda v: FIELD_REPORT.replace(
                "signs off the season. You read for", "signs off the season (%s). You read for" % v),
                "use case field-report, hat steward line 2: "),
            "hat title": (lambda v: FIELD_REPORT.replace('title = "season steward"',
                                                         'title = "season steward %s"' % v),
                          "use case field-report, hat steward line 1: "),
            "brief": (lambda v: FIELD_REPORT.replace("the season's plan.\n",
                                                     "the season's plan, filed as %s.\n" % v),
                      "use case field-report, brief ledger-check line 1: "),
            "verdict": (lambda v: FIELD_REPORT.replace("the season'''", "the season, as %s'''" % v),
                        "use case field-report, verdict line 1: ")}
        for value, kind in ((shaped, "API secret key"), (token, "the value of SOME_API_TOKEN")):
            for where, (build, label) in places.items():
                with self.subTest(where=where, kind=kind):
                    text = build(value)
                    self.assertIn(value, text)
                    d = self.custom_dir({"field-report.toml": text})
                    code, out, err = self.run_cli("--use-case-dir", d, "--use-case",
                                                  "field-report", "--sensitivity", "public",
                                                  "--file", self.draft(), "--allow-flagged",
                                                  "--yes")
                    self.assertEqual(code, 2, err)
                    self.assertIn("REFUSED: credential material in the input (never sendable, "
                                  "no override)", err)
                    self.assertIn(label + kind, err)
                    self.assertNotIn(value, out + err)
                    self.assertEqual(self.server.state["requests"], [])
        # the same use case without the value passes the guards
        d = self.custom_dir({"field-report.toml": FIELD_REPORT})
        code, out, err = self.run_cli("--use-case-dir", d, "--use-case", "field-report",
                                      "--sensitivity", "public", "--file", self.draft(),
                                      "--dry-run")
        self.assertEqual(code, 0, err)


# ---------------------------------------------------------------------------------------------
# No hidden dependency on a personal path


REPO = os.path.dirname(os.path.abspath(chorus.__file__))
SHIPPED = ("code", "contracts", "copy", "grant", "startup")


def gitignore_rules():
    """(negated, pattern) for each rule in the repo's .gitignore, in order."""
    rules = []
    try:
        with open(os.path.join(REPO, ".gitignore"), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return rules
    for ln in lines:
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            rules.append((ln.startswith("!"), ln.lstrip("!").rstrip("/")))
    return rules


def ignored(rel, rules):
    """Whether .gitignore keeps `rel` out of the repo (the last matching rule wins)."""
    out = False
    for negated, pattern in rules:
        if fnmatch.fnmatch(os.path.basename(rel), pattern) or fnmatch.fnmatch(rel, pattern):
            out = not negated
    return out


class NoPersonalPathTest(ChorusCase):
    def sources(self):
        """Every text file in the repo, by path from its root: the runner, its use cases, the
        tests, the docs, the examples. A file .gitignore keeps out of the repo (a local env
        file, a cache) is never opened."""
        rules, out = gitignore_rules(), {}
        for root, dirs, files in os.walk(REPO):
            dirs[:] = sorted(d for d in dirs if d not in (".git", "__pycache__"))
            for fn in sorted(files):
                rel = os.path.relpath(os.path.join(root, fn), REPO)
                if ignored(rel, rules):
                    continue
                try:
                    with open(os.path.join(root, fn), encoding="utf-8") as f:
                        out[rel] = f.read()
                except UnicodeDecodeError:
                    continue  # not a text file
        return out

    def test_sources_cover_the_whole_repo(self):
        srcs = self.sources()
        tomls = [os.path.join("use_cases", f) for f in os.listdir(chorus.BUILTIN_USE_CASES)
                 if f.endswith(".toml")]
        self.assertEqual(len(tomls), len(SHIPPED))
        for rel in ["chorus.py", "README.md", "LICENSE", "SECURITY.md", ".env.example",
                    ".gitignore", os.path.join("use_cases", "README.md"),
                    os.path.join("examples", "README.md"), os.path.join("tests", "harness.py"),
                    os.path.join("tests", os.path.basename(__file__))] + tomls:
            self.assertIn(rel, srcs)
        self.assertFalse([r for r in srcs if r.split(os.sep)[0] in (".git", "__pycache__")
                          or "__pycache__" in r.split(os.sep)])

    def test_no_file_names_a_personal_path(self):
        real_home = pwd.getpwuid(os.getuid()).pw_dir.rstrip(os.sep)  # "/" would match anything
        # built here, so this file's own text cannot match it
        home_folder = re.compile(r"(?<![\w.])/(?:%s)/\w" % "|".join(["Users", "home"]))
        for rel, src in self.sources().items():
            with self.subTest(path=rel):
                if real_home:
                    self.assertFalse(real_home in src, "names this machine's home folder")
                m = home_folder.search(src)  # the message gives a line number, never the text
                self.assertFalse(bool(m), "line %d names a home folder" % (
                    src.count("\n", 0, m.start()) + 1) if m else "")

    def test_runner_names_only_never_send_tilde_paths(self):
        # every ~/... path the runner or a shipped use case names is a never-send default, or
        # the documented place for an env file, under one of them
        allowed, example = set(chorus.NEVER_SEND_DEFAULTS), "~/.config/chorus/"
        tilde = re.compile("~/[\\w.-][\\w./-]*")
        seen = set()
        for rel, src in self.sources().items():
            if rel != "chorus.py" and not (rel.startswith("use_cases" + os.sep)
                                           and rel.endswith(".toml")):
                continue
            for p in tilde.findall(src):
                p = p.rstrip(".").rstrip("/")
                seen.add(p)
                with self.subTest(path=rel, names=p):
                    self.assertTrue(p in allowed or p.startswith(example))
        self.assertLessEqual({"~/.ssh", "~/.config/chorus"}, seen)  # the scan itself works

    def recorded(self, args, environ):
        """load_config, with every file-touching call it makes recorded."""
        seen = []

        def spy(name, real):
            def call(*a, **k):
                seen.append((name, a[0] if a else k.get("path")))
                return real(*a, **k)
            return call

        patches = [mock.patch("builtins.open", spy("open", builtins.open)),
                   mock.patch("os.open", spy("os.open", os.open)),
                   mock.patch("os.path.isfile", spy("isfile", os.path.isfile)),
                   mock.patch("os.path.exists", spy("exists", os.path.exists)),
                   mock.patch("os.path.isdir", spy("isdir", os.path.isdir)),
                   mock.patch("os.listdir", spy("listdir", os.listdir)),
                   mock.patch("os.scandir", spy("scandir", os.scandir)),
                   mock.patch("os.stat", spy("stat", os.stat)),
                   mock.patch("os.lstat", spy("lstat", os.lstat))]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            cfg, code, err = captured(chorus.load_config, args, environ)
        self.assertIsNone(code, err)
        return cfg, seen

    def test_load_config_opens_nothing_without_an_env_file(self):
        environ = {"HOME": self.home, "PATH": os.environ.get("PATH", ""),
                   "CHORUS_NEVER_SEND": os.path.join(self.tmp.name, "vault"),
                   "CHORUS_USE_CASE_DIR": os.path.join(self.tmp.name, "use-cases"),
                   "CHORUS_CONFIDENTIAL_UPSTREAMS": "glm=upstream-a"}
        cfg, seen = self.recorded(ns(), environ)
        self.assertEqual(seen, [])
        self.assertIsNone(cfg["env_file"])
        os.environ.pop("CHORUS_ENV_FILE")
        cfg, seen = self.recorded(ns(), None)
        self.assertEqual(seen, [])

    def test_load_config_opens_only_the_env_file_it_was_given(self):
        cfg, seen = self.recorded(ns(), None)  # CHORUS_ENV_FILE, from the harness
        self.assertTrue(seen)
        self.assertEqual({p for _, p in seen}, {os.path.abspath(self.env_file)})
        other = write(os.path.join(self.tmp.name, "flag.env"), "A_SETTING=some-setting\n")
        cfg, seen = self.recorded(ns(env_file=other), None)
        self.assertEqual({p for _, p in seen}, {os.path.abspath(other)})


@contextlib.contextmanager
def without_module(name):
    """`import name` fails inside the block, as on an interpreter that lacks the module."""
    missing = object()
    saved = sys.modules.get(name, missing)
    sys.modules[name] = None
    try:
        yield
    finally:
        if saved is missing:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved


class InterpreterGuardTest(unittest.TestCase):
    """The runner stops on an interpreter it cannot run on before it imports a module that
    interpreter lacks (tomllib before Python 3.11, pwd off POSIX)."""

    probes = itertools.count()

    def fresh_exec(self, *patches):
        """Execute a fresh copy of chorus.py, under a module name of its own, with `patches`
        applied. Returns the SystemExit code, or None when the module ran to its end."""
        spec = importlib.util.spec_from_file_location(
            "chorus_guard_probe_%d" % next(self.probes), chorus.__file__)
        module = importlib.util.module_from_spec(spec)
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            try:
                spec.loader.exec_module(module)
            except SystemExit as e:
                return e.code
        self.assertTrue(hasattr(module, "main"))
        return None

    def test_this_interpreter_passes_both_guards(self):
        self.assertIsNone(self.fresh_exec())

    def test_python_older_than_3_11_is_refused(self):
        code = self.fresh_exec(mock.patch.object(sys, "version_info", (3, 10, 14, "final", 0)),
                               without_module("tomllib"))
        self.assertIsInstance(code, str)
        self.assertIn("3.11", code)
        self.assertIn("3.10", code)

    def test_a_non_posix_system_is_refused(self):
        code = self.fresh_exec(mock.patch.object(os, "name", "nt"), without_module("pwd"))
        self.assertIsInstance(code, str)
        self.assertIn("runs on macOS and Linux", code)

    def test_old_interpreters_can_parse_it_and_reach_the_guard(self):
        with open(chorus.__file__, encoding="utf-8") as f:
            source = f.read()
        ast.parse(source, filename=chorus.__file__, feature_version=(3, 9))


class ScriptLocationTest(unittest.TestCase):
    """chorus.py run as a script, in a scratch HOME with an empty environment, no keys and a
    dead proxy: through a symlink it still finds its use_cases/ folder; a copy without the
    folder says so."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)

    def run_script(self, script, *argv):
        dead = "http://127.0.0.1:9"
        env = {"HOME": self.home,
               "PATH": os.pathsep.join([os.path.dirname(sys.executable), "/usr/bin", "/bin"]),
               "HTTPS_PROXY": dead, "HTTP_PROXY": dead, "https_proxy": dead, "http_proxy": dead}
        p = subprocess.run([sys.executable, "-B", "-I", script] + list(argv), env=env,
                           cwd=self.tmp, capture_output=True, text=True, timeout=120)
        return p.returncode, p.stdout, p.stderr

    def test_a_symlink_to_the_script_finds_its_use_cases(self):
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        link = os.path.join(bin_dir, "chorus")
        os.symlink(os.path.abspath(chorus.__file__), link)
        code, out, err = self.run_script(link, "--list-use-cases")
        self.assertEqual(code, 0, err)
        for name in SHIPPED:
            self.assertEqual(out.count("## %s — " % name), 1, name)
        self.assertNotRegex(out, r"(?m)^from ")  # each from the folder beside the real file
        self.assertNotIn("was not found", err)

    def test_a_copy_without_its_folder_says_so(self):
        bare = os.path.join(self.tmp, "bare")
        os.makedirs(bare)
        script = os.path.join(bare, "chorus.py")
        shutil.copyfile(chorus.__file__, script)
        note = ("the built-in use-case folder %s was not found (keep chorus.py next to its "
                "use_cases/ folder)" % os.path.join(bare, "use_cases"))
        code, out, err = self.run_script(script, "--list-use-cases")
        self.assertEqual(code, 0, err)
        self.assertIn("note: " + note, err)
        self.assertNotIn("## ", out)
        mine = os.path.join(self.tmp, "mine")
        write(os.path.join(mine, "field-report.toml"), FIELD_REPORT)
        code, out, err = self.run_script(script, "--list-use-cases", "--use-case-dir", mine)
        self.assertEqual(code, 0, err)
        self.assertIn("## field-report — ", out)
        self.assertIn("note: " + note, err)
        draft = write(os.path.join(self.tmp, "draft.md"), DRAFT)
        for argv, known in (([], "none"), (["--use-case-dir", mine], "field-report")):
            with self.subTest(argv=argv):
                code, out, err = self.run_script(script, "--use-case", "nowhere",
                                                 "--sensitivity", "public", "--file", draft,
                                                 "--dry-run", *argv)
                self.assertEqual(code, 1, err)
                self.assertIn("unknown use case 'nowhere' (known: %s; --list-use-cases shows "
                              "them); %s" % (known, note), err)


if __name__ == "__main__":
    unittest.main()
