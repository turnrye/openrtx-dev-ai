"""Tests for the scorer in evals/review/score.py."""

import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("score", os.path.join(ROOT, "evals", "review", "score.py"))
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

CASE = {
    "start": {"sha": "s1"}, "end": {"sha": "e1"},
    "feedback": [
        {"kind": "inline", "trivial": False, "commit": "s1", "path": "a/radio.c", "line": 100,
         "start_line": None, "outcome": "changed", "body": "I'd cut it short and put the ifdef on the call."},
        {"kind": "inline", "trivial": False, "commit": "s1", "path": "a/radio.c", "line": 300,
         "start_line": 290, "outcome": "unchanged", "body": "Note for the future."},
        {"kind": "inline", "trivial": False, "commit": "s2", "path": "a/other.c", "line": 5,
         "outcome": "changed", "body": "Later round."},
        {"kind": "conversation", "trivial": False, "commit": "s1", "body": "General remark."},
    ],
}
FINDINGS = [{"path": "a/radio.c", "line": 101, "severity": "P2", "title": "Move the #ifdef onto the call"},
            {"path": "a/radio.c", "line": 500, "severity": "P3", "title": "Unrelated"},
            {"path": None, "line": None, "severity": "P2", "title": "No sign-off"}]


class Matching(unittest.TestCase):
    """Which findings are at which comment's spot."""

    def test_targets_are_round_one_inline(self):
        self.assertEqual([f["line"] for f in sc.targets(CASE)], [100, 300])

    def test_replies_are_not_targets(self):
        reply = dict(CASE["feedback"][0], in_reply_to=123, body="Ow, sorry!")
        self.assertEqual(sc.targets(dict(CASE, feedback=[reply])), [])

    def test_window(self):
        f = CASE["feedback"][0]
        self.assertTrue(sc.hits(f, {"path": "./a/radio.c", "line": 104}))
        self.assertFalse(sc.hits(f, {"path": "a/radio.c", "line": 110}))
        self.assertFalse(sc.hits(f, {"path": "b/radio.c", "line": 100}))
        self.assertTrue(sc.hits(f, {"path": "a/radio.c", "line": 80, "end_line": 96}))

    def test_comment_range(self):
        self.assertTrue(sc.hits(CASE["feedback"][1], {"path": "a/radio.c", "line": 287}))

    def test_norm_path_keeps_dot_dirs(self):
        self.assertEqual(sc.norm_path("./.github/workflows/x.yml"), ".github/workflows/x.yml")

    def test_meta(self):
        self.assertTrue(sc.is_meta({"path": "a.c", "line": 1, "title": "PR 'Testing performed' is only a ticked box"}))
        self.assertTrue(sc.is_meta({"path": None, "title": "anything"}))
        self.assertFalse(sc.is_meta({"path": "a.c", "line": 1, "title": "CTCSS register overflows"}))


class Judge(unittest.TestCase):
    """The judge sees only comments with findings at their spot, and its answer maps back."""

    def setUp(self):
        self.code = [x for x in FINDINGS if not sc.is_meta(x)]
        self.pairs = sc.spot_pairs(CASE, self.code)

    def test_prompt_lists_only_comments_with_findings_at_their_spot(self):
        prompt = sc.judge_prompt(self.pairs, self.code)
        self.assertIn("Comment 1 on a/radio.c:100", prompt)
        self.assertIn("a) a/radio.c:101 Move the #ifdef onto the call", prompt)
        self.assertNotIn("Comment 2", prompt)

    def test_no_prompt_without_pairs(self):
        self.assertIsNone(sc.judge_prompt([(CASE["feedback"][0], [])], self.code))

    def test_parse(self):
        self.assertEqual(sc.parse_judgement('Sure:\n[{"comment": 1, "finding": "a"}]', self.pairs), {1: 0})
        self.assertEqual(sc.parse_judgement('[{"comment": 1, "finding": null}]', self.pairs), {})
        self.assertEqual(sc.parse_judgement('[{"comment": 1, "finding": "z"}]', self.pairs), {})
        self.assertEqual(sc.parse_judgement("not json", self.pairs), {})


class ScoreCase(unittest.TestCase):
    """Counts for a whole case; git lookups fail harmlessly on a missing repo."""

    def test_counts_without_and_with_the_judge(self):
        v = sc.score_case("/nonexistent.git", CASE, FINDINGS)
        self.assertEqual((v["targets"], v["target_spot_hits"], v["acted"], v["acted_spot_hits"]), (2, 1, 1, 1))
        self.assertEqual(v["acted_point_hits"], 0)
        self.assertEqual(sc.score_case("/nonexistent.git", CASE, FINDINGS, judged={1: 0})["acted_point_hits"], 1)
        self.assertEqual((v["findings"], v["code_findings"], v["meta_findings"], v["flagged_case"]), (3, 2, 1, 1))

    def test_rates(self):
        r = sc.rates(sc.score_case("/nonexistent.git", CASE, FINDINGS, judged={1: 0}))
        self.assertEqual((r["catch_rate"], r["catch_rate_spot"], r["flagged"]), (1.0, 1.0, 1))
        self.assertIsNone(sc.rates(sc.score_case("/nonexistent.git", dict(CASE, feedback=[]), []))["catch_rate"])

    def test_prior_comments_are_not_targets_or_leaks(self):
        prior = dict(CASE["feedback"][0], prior=True, body="y " * 40)
        v = sc.score_case("/nonexistent.git", dict(CASE, feedback=[prior]), [], "y " * 50)
        self.assertEqual((v["targets"], v["leaked"]), (0, 0))

    def test_leak(self):
        quoted = "the report said: I'd cut it short and put the ifdef on the call. as asked"
        self.assertEqual(sc.score_case("/nonexistent.git", CASE, [], quoted)["leaked"], 0)
        long_case = dict(CASE, feedback=[dict(CASE["feedback"][0], body="x " * 40)])
        self.assertEqual(sc.score_case("/nonexistent.git", long_case, [], "x " * 50)["leaked"], 1)


if __name__ == "__main__":
    unittest.main()
