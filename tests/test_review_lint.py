"""Tests for the deterministic house-rule checks in skills/review/lint.py."""

import importlib.util
import os
import shutil
import subprocess
import tempfile
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

    def setUp(self):
        self.default = lint.MULTILINE_LISTS
        lint.MULTILINE_LISTS = True  # these tests cover the optional multi-line check

    def tearDown(self):
        lint.MULTILINE_LISTS = self.default

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

    def test_new_content_ignores_lines_moved_from_another_file(self):
        moved = "static const int t[] = { 1, 2 };"
        self.assertEqual(lint.new_content(["static const int t[] = { 1, 2 };", "int c;"], {1, 2}, "", moved), {2})

    def test_new_content_ignores_rewrapped_statements(self):
        base = 'static const char *sym[] =\n{\n    " 0", ",.?1",\n    "abc2"\n};\n'
        self.assertEqual(lint.new_content(['static const char *sym[] = { " 0", ",.?1",', "int x;"], {1, 2}, base), {2})

    def test_multiline_lists_are_off_by_default(self):
        self.assertFalse(self.default)
        lint.MULTILINE_LISTS = False
        self.assertEqual(lint.check_trailing_comma("a.h", self.ENUM, {3}), [])

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

    def test_pointer_to_const_is_not_a_constant(self):
        self.assertEqual(self.check("a.cpp", "static const rtxStatus_t *config;"), [])
        self.assertEqual(self.check("a.cpp", 'static const char charMap[] = " AB";'), [("static-constexpr", 1)])

    def test_module_prefix_once_per_function(self):
        self.assertEqual(self.check("ui_menu.c", "color_t getColorFromLevel(uint16_t Level);"), [])
        self.assertEqual(self.check("ui.h", "color_t getColorFromLevel(uint16_t Level);"), [("module-prefix", 1)])

    def test_module_prefix_skips_existing_interfaces(self):
        found = lint.check_lines("delays.c", ["void delayUs(unsigned int us)"], {1}, existing={"delayUs"})
        self.assertEqual(found, [])

    def test_module_prefix(self):
        self.assertEqual(self.check("input.c", "bool handleKnobEvents(uint8_t k)"), [("module-prefix", 1)])
        self.assertEqual(self.check("input.c", "bool input_handleKnob(uint8_t k)", "static bool helper(void)",
                                    "int main(void)", "    foo(bar);", "void DMA2_IRQHandler(void)"), [])


class FileLocal(unittest.TestCase):
    """A new C function no header declares should be static."""

    def test_undeclared_function(self):
        lines = ["bool scanKeys(uint8_t k)", "{", "static int helper(void)", "void input_init(void)", "int main(void)"]
        found = lint.check_file_local("input.c", lines, {1, 3, 4, 5}, {"input_init"})
        self.assertEqual(rules(found), [("file-local", 1)])
        self.assertEqual(found[0]["suggestion"], "static bool scanKeys(uint8_t k)")

    def test_file_scope_variable(self):
        lines = ["bool pressAllowed;", "static int ok;", "uint8_t buf[16] = { 0 };", "    int local;",
                 "extern int x;", "int shared;"]
        found = lint.check_file_local("input.c", lines, set(range(1, 7)), {"shared"})
        self.assertEqual(rules(found), [("file-local", 1), ("file-local", 3)])

    def test_extern_in_another_c_file_counts(self):
        lines = ["int _ui_getSpectrumValueName(char *buf)"]
        found = lint.check_file_local("ui_menu.c", lines, {1}, set(),
                                      used_elsewhere=lambda name: name == "_ui_getSpectrumValueName")
        self.assertEqual(found, [])

    def test_only_c_files(self):
        self.assertEqual(lint.check_file_local("a.cpp", ["void f(void)"], {1}, set()), [])


class AppendOnly(unittest.TestCase):
    """Positional lists only grow at the end."""

    LINES = ["struct t {", "    const char *a;", "    const char *new;", "    const char *b;", "};"]

    def test_inserted_in_the_middle(self):
        self.assertEqual(rules(lint.check_append_only("x.h", self.LINES, {3})), [("append-only", 3)])

    def test_appended_at_the_end(self):
        lines = ["struct t {", "    const char *a;", "    const char *b;", "    const char *new;", "};"]
        self.assertEqual(lint.check_append_only("x.h", lines, {4}), [])

    def test_comment_lines_are_not_entries(self):
        lines = ["/*", " * OpenRTX is distributed in the hope that it will be useful,", " */", "struct t {",
                 "    int a;", "};"]
        self.assertEqual(lint.check_append_only("x.h", lines, {2}), [])

    def test_file_level_lines_are_not_entries(self):
        lines = ["int settings_load(struct settings_storage *s);", "", "int settings_save(void);"]
        self.assertEqual(lint.check_append_only("x.h", lines, {1}), [])

    def test_extern_c_block_is_file_level(self):
        lines = ['extern "C" {', "int settings_load(struct settings_storage *s);", "", "int settings_save(void);", "}"]
        self.assertEqual(lint.check_append_only("x.h", lines, {2}), [])

    def test_path_filter(self):
        self.assertTrue(lint.APPEND_ONLY.match("openrtx/include/ui/SpanishStrings.h"))
        self.assertTrue(lint.APPEND_ONLY.match("openrtx/include/core/voicePrompts.h"))
        self.assertFalse(lint.APPEND_ONLY.match("openrtx/include/core/state.h"))


class MoreRules(unittest.TestCase):
    """Layering, unused code, docs and readability rules."""

    def all_added(self, lines):
        return set(range(1, len(lines) + 1))

    def test_layering(self):
        lines = ["void f()", "{", "    radio_setOpmode(OPMODE_FM);", "    display_init();", "}"]
        self.assertEqual(rules(lint.check_calls("openrtx/src/ui/default/ui.c", lines, {3, 4})),
                         [("layering", 3), ("layering", 4)])
        self.assertEqual(lint.check_calls("openrtx/src/rtx/rtx.cpp", lines, {3}), [])
        mutex = ["    pthread_mutex_lock(&state_mutex);"]
        self.assertEqual(rules(lint.check_calls("openrtx/src/rtx/OpMode_M17.cpp", mutex, {1})), [("layering", 1)])
        self.assertEqual(lint.check_calls("openrtx/src/ui/default/ui.c", mutex, {1}), [])

    def test_unused(self):
        lines = ["struct rtxStatus {", "    uint8_t used;", "    uint8_t spare;", "};", "#define MAX_X 4"]
        refs = {"used": 3, "spare": 1, "MAX_X": 1}.get
        found = lint.check_unused("openrtx/include/rtx/rtx.h", lines, {2, 3, 5}, refs)
        self.assertEqual(rules(found), [("unused", 3), ("unused", 5)])
        self.assertEqual(lint.check_unused("platform/mcu/regs.h", lines, {5}, refs), [])

    def test_doxygen(self):
        lines = ["/**", " * Documented.", " */", "void input_a(void);", "", "void input_b(void);"]
        self.assertEqual(rules(lint.check_doxygen("openrtx/include/core/input.h", lines, {4, 6})), [("doxygen", 6)])

    def test_doxygen_cpp_declarations_not_calls(self):
        lines = ["class S {", "public:", "    S()", "    {", "        reset();", "    }", "    void reset();", "};"]
        self.assertEqual(rules(lint.check_doxygen("openrtx/include/S.hpp", lines, {5, 7})), [("doxygen", 7)])

    def test_static_local(self):
        lines = ["static int fileState;", "void f(void)", "{", "    static int count;", "    static const int t[] = {1};", "}"]
        self.assertEqual(rules(lint.check_static_locals("a.c", lines, self.all_added(lines))), [("static-local", 4)])

    def test_define_location(self):
        lines = ["#define TOP 1", "", "void f(void)", "{", "}", "#define LATE 2"]
        self.assertEqual(rules(lint.check_define_location("a.c", lines, {1, 6})), [("define-location", 6)])

    def test_parentheses(self):
        check = lambda *l: rules(lint.check_parentheses("a.c", list(l), set(range(1, len(l) + 1))))
        self.assertEqual(check("    if (pos.x >= W || pos.y >= H ||"), [("parentheses", 1)])
        self.assertEqual(check("    if ((pos.x >= W) || (pos.y >= H))"), [])
        self.assertEqual(check("    bool b = ok && (x == y);"), [])
        self.assertEqual(check("    x = (a << 2) && p->q;"), [])

    def test_switch_chain(self):
        lines = ["if (mode == A)", "    f();", "else if (mode == B)", "    g();", "else if (mode == C)", "    h();"]
        self.assertEqual(rules(lint.check_switch_chain("a.c", lines, {1})), [("switch", 1)])
        self.assertEqual(lint.check_switch_chain("a.c", lines[:4], {1}), [])

    def test_std_array(self):
        lines = ["class Correlator {", "private:", "    int16_t samples[SIZE];", "    char name[8];", "};"]
        self.assertEqual(rules(lint.check_std_array("a.hpp", lines, {3, 4})), [("std-array", 3)])

    def test_hw_literal(self):
        lines = ["    state.channel.rx_frequency = 430000000;", "    x = 1000;"]
        self.assertEqual(rules(lint.check_hw_literals("openrtx/src/core/state.c", lines, {1, 2})), [("hw-literal", 1)])
        self.assertEqual(lint.check_hw_literals("platform/targets/MD-UV3x0/hwconfig.c", lines, {1}), [])


class PrText(unittest.TestCase):
    """The PR template's sections."""

    def test_empty_testing(self):
        body = "### Testing performed\n\n**Tested on:**\n\n- [ ] Linux emulator\n- [ ] MD-3x0\n\n### Changelog entry\n\n"
        self.assertEqual([f["rule"] for f in lint.check_pr_text(body)], ["pr-testing", "pr-changelog"])

    def test_filled_in(self):
        body = ("### Testing performed\n\nOn air on one CS7000-PLUS.\n\n- [x] CS7000 / CS7000-PLUS\n\n"
                "### Changelog entry\n\nCS7000-PLUS: fix CTCSS\n")
        self.assertEqual(lint.check_pr_text(body), [])

    def test_no_template(self):
        self.assertEqual(lint.check_pr_text("Fixes the thing."), [])


class SettingsCounts(unittest.TestCase):
    """settings_t fields against default_settings values."""

    TEXT = ("typedef struct\n{\n    uint8_t a;\n    uint8_t b : 4,\n            c : 4;\n    char d[10];\n}\n"
            "__attribute__((packed)) settings_t;\n\nstatic const settings_t default_settings =\n{\n    1,\n"
            "#ifdef X\n    X_DEFAULT,\n#else\n    2,\n#endif\n    0,\n    \"N0CALL\",\n};\n")

    def test_fields_under_ifdef(self):
        text = ("typedef struct\n{\n    uint8_t a;\n    #ifdef X\n    uint8_t b;\n    #endif\n\n}\n"
                "__attribute__((packed)) settings_t;\n\nstatic const settings_t default_settings =\n{\n    1,\n"
                "    #ifdef X\n    2\n    #endif\n};\n")
        self.assertEqual(lint.struct_members(text, "settings_t"), lint.initializer_entries(text, "default_settings"))

    def test_counts(self):
        # one value per field, counting one branch of the #ifdef
        self.assertEqual(lint.struct_members(self.TEXT, "settings_t"), 4)
        self.assertEqual(lint.initializer_entries(self.TEXT, "default_settings"), 4)


class Repo(unittest.TestCase):
    """Commit and build checks on a throwaway repository."""

    def git(self, *args):
        subprocess.run(["git", "-C", self.dir] + list(args), check=True, capture_output=True)

    def commit(self, files, message):
        for path, text in files.items():
            full = os.path.join(self.dir, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(text)
            self.git("add", path)
        self.git("commit", "-q", "-m", message)
        return subprocess.run(["git", "-C", self.dir, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.base = self.commit({"meson.build": "src = ['a.c']\n", "a.c": "int a_x;\n"}, "build: base\n\nSigned-off-by: t")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_commit_checks(self):
        self.commit({"a.c": "int a_x;\nint a_f(void)\n{\n    return 1;\n}\n"}, "core: add a_f\n\nSigned-off-by: t")
        self.commit({"a.c": "int a_x;\nint a_f(void)\n{\n    return 2;\n}\n", "b.c": "#ifdef CONFIG_NOPE\n#endif\n"},
                    "address review comments")
        rules_found = sorted(f["rule"] for f in lint.pr_checks(self.dir, self.base, "HEAD"))
        self.assertEqual(rules_found, ["build-unlisted", "commit-fixup", "commit-prefix", "commit-signoff",
                                       "config-undefined"])

    def test_building_on_an_earlier_commit_is_fine(self):
        self.commit({"a.c": "int a_x;\nint a_f(void)\n{\n    return 1;\n}\n"}, "core: add a_f\n\nSigned-off-by: t")
        self.commit({"a.c": "int a_x;\nint a_f(void)\n{\n    return 2;\n}\n"}, "core: make a_f return 2\n\nSigned-off-by: t")
        self.assertEqual([f["rule"] for f in lint.pr_checks(self.dir, self.base, "HEAD")], [])

    def test_defined_config_is_fine(self):
        self.commit({"meson.build": "src = ['a.c', 'b.c']\ndefs = {'CONFIG_YES': ''}\n",
                     "b.c": "#ifdef CONFIG_YES\n#endif\n"}, "core: add b\n\nSigned-off-by: t")
        self.assertEqual([f["rule"] for f in lint.pr_checks(self.dir, self.base, "HEAD")], [])

    def test_mixed_formatting(self):
        text = "".join("int a_%d;\n\n\n\n" % i for i in range(4))
        self.commit({"a.c": text}, "core: add vars\n\nSigned-off-by: t")
        reformatted = "".join("int  a_%d;\n\n\n\n" % i for i in range(3)) + "int a_3;\n\n\n\nint a_new;\n"
        self.commit({"a.c": reformatted}, "core: add a_new\n\nSigned-off-by: t")
        found = [f["rule"] for f in lint.pr_checks(self.dir, self.base, "HEAD")]
        self.assertIn("commit-mixed-format", found)


if __name__ == "__main__":
    unittest.main()
