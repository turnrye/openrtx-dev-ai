"""Tests for the pure parts of evals/review/build_corpus.py."""

import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "build_corpus", os.path.join(ROOT, "evals", "review", "build_corpus.py"))
bc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bc)


class HeadAt(unittest.TestCase):
    """Reconstructing the PR head at a moment from the timeline."""

    TIMELINE = [
        {"event": "committed", "sha": "a1", "committer": {"date": "2026-01-01T00:00:00Z"}},
        {"event": "commented", "created_at": "2026-01-02T00:00:00Z"},
        {"event": "head_ref_force_pushed", "commit_id": "b2", "created_at": "2026-01-03T00:00:00Z"},
    ]

    def test_pushes_are_ordered_and_filtered(self):
        self.assertEqual(bc.head_pushes(self.TIMELINE),
                         [("2026-01-01T00:00:00Z", "a1"), ("2026-01-03T00:00:00Z", "b2")])

    def test_head_at(self):
        pushes = bc.head_pushes(self.TIMELINE)
        self.assertIsNone(bc.head_at(pushes, "2025-12-31T00:00:00Z"))
        self.assertEqual(bc.head_at(pushes, "2026-01-02T00:00:00Z"), "a1")
        self.assertEqual(bc.head_at(pushes, "2026-01-03T00:00:00Z"), "b2")


class Trivial(unittest.TestCase):
    """Telling thanks and LGTM apart from feedback."""

    def test_trivial(self):
        for body in ("Thanks!", "Merged, thank you", "LGTM", "ok"):
            self.assertTrue(bc.TRIVIAL.match(body), body)

    def test_not_trivial(self):
        for body in ("LGTM! Just two questions: do we really need to gate part of the UI code?",
                     "I'd cut it short:", "Same here"):
            self.assertFalse(bc.TRIVIAL.match(body), body)


if __name__ == "__main__":
    unittest.main()
