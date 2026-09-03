"""Unit tests for the envelope, the verdict parser and the decision rule.

Run from the repo root: python3 -m unittest discover -s pr-triage/tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import triage  # noqa: E402
from triage import Envelope, Facts, Verdict, changed_paths, decide, parse_verdict  # noqa: E402


def facts(**overrides) -> Facts:
    base = dict(
        author_login="ionite34",
        author_association="MEMBER",
        author_permission="admin",
        paths=["docs/architecture.md"],
        diff_truncated=False,
        too_many_files=False,
    )
    base.update(overrides)
    return Facts(**base)


APPROVE = Verdict("approve", "reads clean :3")
HUMAN = Verdict("human", "not sure about that line owo")


class EnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.envelope = Envelope.parse(None)  # docs/** and **/*.md

    def test_default_covers_a_nested_docs_path(self):
        self.assertTrue(self.envelope.covers("docs/adr/0001-thing.md"))
        self.assertTrue(self.envelope.covers("docs/images/diagram.png"))

    def test_default_covers_a_markdown_file_at_root(self):
        self.assertTrue(self.envelope.covers("README.md"))

    def test_default_covers_markdown_anywhere(self):
        self.assertTrue(self.envelope.covers("tests/Some/Deep/NOTES.md"))

    def test_a_source_file_is_outside(self):
        self.assertFalse(self.envelope.covers("Services/ChatBrain.cs"))
        self.assertFalse(self.envelope.covers("Lykos.Chat.Core.csproj"))

    def test_a_workflow_file_is_never_inside_even_when_a_glob_matches(self):
        wide = Envelope.parse("**/*.yml\n.github/**\n**")
        self.assertFalse(wide.covers(".github/workflows/ci.yml"))
        self.assertFalse(wide.covers(".github/CODEOWNERS"))
        self.assertFalse(wide.covers(".github"))
        self.assertTrue(wide.covers("docker-compose.yml"))

    def test_a_markdown_file_under_dot_github_is_outside(self):
        self.assertFalse(self.envelope.covers(".github/PULL_REQUEST_TEMPLATE.md"))

    def test_single_star_stays_within_a_segment(self):
        env = Envelope.parse("docs/*.md")
        self.assertTrue(env.covers("docs/readme.md"))
        self.assertFalse(env.covers("docs/sub/readme.md"))

    def test_malformed_paths_are_outside(self):
        for path in ("", "/docs/x.md", "docs/../Program.cs", "docs\\x.md", "docs//x.md", "./docs/x.md"):
            with self.subTest(path=path):
                self.assertFalse(self.envelope.covers(path))

    def test_covers_all_requires_every_path_and_at_least_one(self):
        self.assertTrue(self.envelope.covers_all(["docs/a.md", "README.md"]))
        self.assertFalse(self.envelope.covers_all(["docs/a.md", "src/a.cs"]))
        self.assertFalse(self.envelope.covers_all([]))

    def test_comment_and_blank_lines_are_ignored(self):
        env = Envelope.parse("# only docs\n\ndocs/**\n")
        self.assertTrue(env.covers("docs/a.txt"))
        self.assertFalse(env.covers("README.md"))


class ChangedPathsTests(unittest.TestCase):
    def test_rename_contributes_both_names(self):
        files = [{"filename": "docs/new.md", "previous_filename": "src/old.cs", "status": "renamed"}]
        self.assertEqual(changed_paths(files), ["docs/new.md", "src/old.cs"])

    def test_plain_change_contributes_one(self):
        self.assertEqual(changed_paths([{"filename": "docs/a.md"}]), ["docs/a.md"])


class VerdictParsingTests(unittest.TestCase):
    def test_valid_json(self):
        v = parse_verdict('{"verdict": "approve", "reason": "tidy docs :3", "notes": "nothing rattling"}')
        self.assertEqual(v, Verdict("approve", "tidy docs :3", "nothing rattling"))

    def test_json_inside_prose_and_fences(self):
        text = 'Sure! Here is my verdict:\n```json\n{"verdict": "human", "reason": "hm..."}\n```\nhope that helps'
        self.assertEqual(parse_verdict(text), Verdict("human", "hm..."))

    def test_think_block_is_ignored(self):
        text = '<think>{"verdict": "approve", "reason": "draft"}</think>{"verdict": "human", "reason": "final"}'
        self.assertEqual(parse_verdict(text), Verdict("human", "final"))

    def test_garbage_is_none(self):
        for text in (None, "", "APPROVED!!!", "{not json", '{"reason": "no verdict key"}', '{"verdict": "maybe"}', "[]"):
            with self.subTest(text=text):
                self.assertIsNone(parse_verdict(text))

    def test_verdict_case_and_whitespace_are_forgiven(self):
        self.assertEqual(parse_verdict('{"verdict": " Approve "}').kind, "approve")

    def test_missing_reason_gets_a_placeholder(self):
        self.assertEqual(parse_verdict('{"verdict": "human"}').reason, "(no reason given)")

    def test_braces_inside_strings_do_not_confuse_the_scanner(self):
        text = 'text {"verdict": "human", "reason": "the diff has a { in it"} trailing'
        self.assertEqual(parse_verdict(text).reason, "the diff has a { in it")


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.envelope = Envelope.parse(None)

    def test_member_inside_envelope_with_approve_verdict_approves(self):
        outcome = decide(facts(), self.envelope, APPROVE)
        self.assertTrue(outcome.approve)
        self.assertEqual(outcome.status, "approve")

    def test_outside_envelope_must_not_approve_even_when_the_model_says_approve(self):
        outcome = decide(facts(paths=["docs/a.md", "Services/ChatBrain.cs"]), self.envelope, APPROVE)
        self.assertFalse(outcome.approve)
        self.assertEqual(outcome.status, "outside")
        self.assertTrue(any("Services/ChatBrain.cs" in b for b in outcome.blockers))

    def test_workflow_change_must_not_approve_even_when_the_model_says_approve(self):
        outcome = decide(facts(paths=[".github/workflows/pr-triage.yml"]), self.envelope, APPROVE)
        self.assertFalse(outcome.approve)
        self.assertEqual(outcome.status, "outside")

    def test_rename_out_of_envelope_must_not_approve(self):
        outcome = decide(facts(paths=["docs/moved.md", "src/old.cs"]), self.envelope, APPROVE)
        self.assertFalse(outcome.approve)

    def test_non_member_must_not_approve(self):
        for assoc, perm in (("CONTRIBUTOR", "write"), ("MEMBER", "read"), ("MEMBER", None), ("NONE", None)):
            with self.subTest(assoc=assoc, perm=perm):
                outcome = decide(facts(author_association=assoc, author_permission=perm), self.envelope, APPROVE)
                self.assertFalse(outcome.approve)
                self.assertEqual(outcome.status, "human")

    def test_human_verdict_inside_envelope_does_not_approve(self):
        outcome = decide(facts(), self.envelope, HUMAN)
        self.assertFalse(outcome.approve)
        self.assertEqual(outcome.status, "human")
        self.assertEqual(outcome.blockers, [])

    def test_no_verdict_is_the_offline_path(self):
        outcome = decide(facts(), self.envelope, None)
        self.assertFalse(outcome.approve)
        self.assertEqual(outcome.status, "offline")

    def test_truncated_diff_must_not_approve(self):
        outcome = decide(facts(diff_truncated=True), self.envelope, APPROVE)
        self.assertFalse(outcome.approve)

    def test_too_many_files_must_not_approve(self):
        outcome = decide(facts(too_many_files=True), self.envelope, APPROVE)
        self.assertFalse(outcome.approve)

    def test_no_files_must_not_approve(self):
        outcome = decide(facts(paths=[]), self.envelope, APPROVE)
        self.assertFalse(outcome.approve)


class RenderingTests(unittest.TestCase):
    def test_every_body_carries_marker_and_signature(self):
        sig = "🐾 Lykos Pup (fable-fusion on Freya)"
        review = triage.render_review_body(APPROVE, "abcdef1234", sig)
        self.assertIn(triage.MARKER, review)
        self.assertTrue(review.endswith(sig))
        for status in ("offline", "human", "outside", "approve"):
            with self.subTest(status=status):
                outcome = triage.Outcome(approve=status == "approve", status=status, blockers=["x"] if status == "outside" else [])
                body = triage.render_comment_body(outcome, None if status == "offline" else HUMAN, "abcdef1234", sig)
                self.assertIn(triage.MARKER, body)
                self.assertTrue(body.endswith(sig))

    def test_offline_comment_says_offline_and_human(self):
        body = triage.render_comment_body(triage.Outcome(False, "offline"), None, "abcdef1", "sig")
        self.assertIn("could not reach my model", body)
        self.assertIn("human should look", body)


if __name__ == "__main__":
    unittest.main()
