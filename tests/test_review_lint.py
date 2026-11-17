"""Tests for the deterministic house-rule checks in skills/review/lint.py."""

import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("lint", os.path.join(ROOT, "skills", "review", "lint.py"))
lint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lint)


def rules(found):
    return [(f["rule"], f["line"]) for f in found]


class TrailingComma(unittest.TestCase):
    """The last element of a multi-line enum or initializer needs a comma."""

    ENUM = "enum mode {\n    MODE_A,\n    MODE_B\n};\n"

    def test_enum(self):
        self.assertEqual(rules(lint.check_trailing_comma("a.h", self.ENUM, {3})), [("trailing-comma", 3)])

    def test_untouched_block_is_legacy(self):
        self.assertEqual(lint.check_trailing_comma("a.h", self.ENUM, {9}), [])

    def test_with_comma_and_comment(self):
        text = "enum mode {\n    MODE_A,\n    MODE_B,   // last\n};\n"
        self.assertEqual(lint.check_trailing_comma("a.h", text, {3}), [])

    def test_initializer_and_single_line(self):
        text = "static const int t[] = {\n    1,\n    2\n};\nint u[] = { 1, 2 };\n"
        self.assertEqual(rules(lint.check_trailing_comma("a.c", text, {3, 5})), [("trailing-comma", 3)])

    def test_nested_initializer(self):
        text = "struct p ps[] = {\n    { 1, 2 },\n    { 3, 4 }\n};\n"
        self.assertEqual(rules(lint.check_trailing_comma("a.c", text, {3})), [("trailing-comma", 3)])

    def test_enum_packed_on_one_line(self):
        text = "x;\nenum RtxStatus { RTX_OFF = 0, RTX_RX, RTX_TX };\nenum One { A };\nenum Ok { A, B, };\n"
        self.assertEqual(rules(lint.check_trailing_comma("a.h", text, {2, 3, 4})), [("trailing-comma", 2)])

    def test_new_content_ignores_moved_and_reindented_lines(self):
        base = "typedef struct\n{\nint a;\n"
        self.assertEqual(lint.new_content(["  typedef struct {", "int b;"], {1, 2}, base), {2})

    def test_function_bodies_are_not_lists(self):
        text = "int f(void)\n{\n    return 1\n}\n"
        self.assertEqual(lint.check_trailing_comma("a.c", text, {3}), [])


class Lines(unittest.TestCase):
    """Single-line rules."""

    def check(self, path, *lines):
        return rules(lint.check_lines(path, list(lines), set(range(1, len(lines) + 1))))

    def test_typedef(self):
        self.assertEqual(self.check("a.h", "typedef struct {", "typedef int foo_t;"), [("typedef", 1)])

    def test_array_size(self):
        found = lint.check_lines("a.c", ["for(i = 0; i < sizeof(tbl) / sizeof(tbl[0]); i++)"], {1})
        self.assertEqual(found[0]["suggestion"], "for(i = 0; i < ARRAY_SIZE(tbl); i++)")

    def test_std_string_only_in_cpp(self):
        self.assertEqual(self.check("a.cpp", "std::string s;"), [("std-string", 1)])
        self.assertEqual(self.check("a.c", "std::string s;"), [])

    def test_static_constexpr(self):
        self.assertEqual(self.check("a.hpp", "    static const uint8_t TABLE[] = {"), [("static-constexpr", 1)])
        self.assertEqual(self.check("a.hpp", "    static constexpr uint8_t N = 4;"), [])

    def test_module_prefix(self):
        self.assertEqual(self.check("input.c", "bool handleKnobEvents(uint8_t k)"), [("module-prefix", 1)])
        self.assertEqual(self.check("input.c", "bool input_handleKnob(uint8_t k)", "static bool helper(void)",
                                    "int main(void)", "    foo(bar);", "void DMA2_IRQHandler(void)"), [])


class AppendOnly(unittest.TestCase):
    """Positional lists only grow at the end."""

    LINES = ["struct t {", "    const char *a;", "    const char *new;", "    const char *b;", "};"]

    def test_inserted_in_the_middle(self):
        self.assertEqual(rules(lint.check_append_only("x.h", self.LINES, {3})), [("append-only", 3)])

    def test_appended_at_the_end(self):
        lines = ["struct t {", "    const char *a;", "    const char *b;", "    const char *new;", "};"]
        self.assertEqual(lint.check_append_only("x.h", lines, {4}), [])

    def test_path_filter(self):
        self.assertTrue(lint.APPEND_ONLY.match("openrtx/include/ui/SpanishStrings.h"))
        self.assertTrue(lint.APPEND_ONLY.match("openrtx/include/core/voicePrompts.h"))
        self.assertFalse(lint.APPEND_ONLY.match("openrtx/include/core/state.h"))


if __name__ == "__main__":
    unittest.main()
