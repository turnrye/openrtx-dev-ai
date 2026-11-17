"""Tests for the heuristic scorer in evals/review/score.py."""

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


class Matching(unittest.TestCase):
    """Which findings catch which comments."""

    def test_targets_are_round_one_inline(self):
        self.assertEqual([f["line"] for f in sc.targets(CASE)], [100, 300])

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


class ScoreCase(unittest.TestCase):
    """Counts for a whole case; git lookups fail harmlessly on a missing repo."""

    def test_counts(self):
        findings = [{"path": "a/radio.c", "line": 101, "severity": "P2"},
                    {"path": "a/radio.c", "line": 500, "severity": "P3"}]
        v = sc.score_case("/nonexistent.git", CASE, findings)
        self.assertEqual((v["targets"], v["target_hits"], v["acted"], v["acted_hits"]), (2, 1, 1, 1))
        self.assertEqual((v["findings"], v["serious"], v["flagged_case"]), (2, 1, 1))

    def test_leak(self):
        quoted = "the report said: I'd cut it short and put the ifdef on the call. as asked"
        self.assertEqual(sc.score_case("/nonexistent.git", CASE, [], quoted)["leaked"], 0)
        long_case = dict(CASE, feedback=[dict(CASE["feedback"][0], body="x " * 40)])
        self.assertEqual(sc.score_case("/nonexistent.git", long_case, [], "x " * 50)["leaked"], 1)


if __name__ == "__main__":
    unittest.main()
