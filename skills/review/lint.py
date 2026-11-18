#!/usr/bin/env python3
"""Check an OpenRTX change for the house rules a tool can check, on the lines it adds.

    lint.py [--repo PATH] BASE [HEAD] [--clang-tidy [--tree DIR]] [--json]

BASE..HEAD is the change (HEAD defaults to HEAD). --repo is a checkout or a bare clone;
files are read from git, so nothing needs to be checked out. --clang-tidy also runs a
curated set of clang-tidy checks on the changed C/C++ files; it needs the files on disk
(--tree, default --repo) and LLVM's clang-tidy (on the PATH or $CLANG_TIDY).

Findings come out as the review skill writes them: path, line, end_line, severity,
title, body, suggestion, plus `rule`. Only lines the change adds are reported, so
legacy code that breaks a rule is left alone, as AGENTS.md asks.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

C_EXT = (".c", ".h")
CPP_EXT = (".cpp", ".hpp", ".cc", ".hh")
SOURCE_EXT = C_EXT + CPP_EXT
SKIP_DIRS = ("lib/", "subprojects/", "tests/", "scripts/")

# Lists that must only grow at the end: users' saved settings and the string and
# voice-prompt tables are indexed by position (AGENTS.md § Persistent Data).
APPEND_ONLY = re.compile(r"^openrtx/include/(ui/\w*Strings\.h|ui/ui_strings\.h|core/voicePrompts\.h|core/settings\.h)$")

MULTILINE_LISTS = False  # also flag multi-line lists without a final comma (noisy: see packed_enums)

TIDY_PATHS = ("openrtx/", "platform/targets/linux/")  # what the Linux build compiles
# Scored against maintainer comments on 63 merged PRs (evals/review/lint_eval.py), the
# generic checks (simplify-boolean-expr, switch-missing-default-case, use-internal-linkage)
# raised 240 findings for 2 matches; what maintainers ask for is mostly OpenRTX-specific,
# so the default keeps only checks that flag outright redundancy.
TIDY_CHECKS = ",".join([
    "-*",
    "misc-redundant-expression",
    "readability-redundant-control-flow",
    "readability-redundant-declaration",
    "readability-redundant-casting",          # "is the static_cast actually needed?"
    "readability-make-member-function-const", # getters are const (AGENTS.md § C++ Class Conventions)
])


def git(repo, *args):
    """Run git on the repository and return stdout ('' on failure)."""
    p = subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")  # old files aren't all UTF-8
    return p.stdout if p.returncode == 0 else ""


def flat(text):
    """Text without any whitespace: what's left when only formatting changed."""
    return re.sub(r"\s+", "", text)


def changes(repo, base, head):
    """What BASE..HEAD does to the sources.

    Returns ({path: set of new-side line numbers added}, {path: its path at BASE when renamed},
    the text of every line the change removes anywhere, so code moved between files is
    recognised as moved).
    """
    added, renamed, removed, path = {}, {}, [], None
    specs = ["*" + e for e in SOURCE_EXT]
    # deleted files too: code moved out of a file the change deletes is moved, not new
    for line in git(repo, "diff", "-U0", "-M", "--no-color", "--diff-filter=AMRD", base, head,
                    "--", *specs).splitlines():
        if line.startswith("rename from "):
            old = line[len("rename from "):]
        elif line.startswith("rename to "):
            renamed[line[len("rename to "):]] = old
        elif line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("---"):
            continue
        elif line.startswith("@@") and path:
            m = re.match(r"@@ -\S+ \+(\d+)(?:,(\d+))? @@", line)
            start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            added.setdefault(path, set()).update(range(start, start + count))
        elif line.startswith("-"):
            removed.append(line[1:])
    return added, renamed, "\n".join(removed)


def added_lines(repo, base, head):
    """{path: set of new-side line numbers the change adds}, for files that exist at head."""
    return changes(repo, base, head)[0]


def new_content(lines, added, base_text, removed_text=""):
    """The added lines whose text isn't already in the file's base version or removed elsewhere.

    Whitespace is ignored on both sides and the match can span lines, so re-indenting,
    moving a brace or re-wrapping a statement doesn't make old code look new.
    """
    seen = flat(base_text) + "\0" + flat(removed_text)
    return {n for n in added if n <= len(lines) and flat(lines[n - 1]) not in seen}


def strip_comments(text):
    """Text with // and /* */ comments blanked out, keeping line breaks."""
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    return re.sub(r"//[^\n]*|/\*.*?\*/", blank, text, flags=re.S)


def finding(rule, path, line, severity, title, body, suggestion=None):
    """One finding in the review skill's format."""
    return {"rule": rule, "path": path, "line": line, "end_line": line, "severity": severity,
            "title": title, "body": body, "suggestion": suggestion}


def matching_brace(text, i):
    """Index of the } that closes the { at text[i], or -1."""
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return j
    return -1


def check_trailing_comma(path, text, added):
    """Multi-line enums and initializer lists need a comma after the last element."""
    code = strip_comments(text)
    out = []
    if not MULTILINE_LISTS:
        return out + packed_enums(path, code, added)
    for m in re.finditer(r"(\benum\b[^;{}()]*|=\s*)\{", code):
        open_i = m.end() - 1
        close_i = matching_brace(code, open_i)
        if close_i < 0:
            continue
        first, last = code.count("\n", 0, open_i) + 1, code.count("\n", 0, close_i) + 1
        if first == last or not any(first <= n <= last for n in added):
            continue
        inner = code[open_i + 1:close_i]
        rows = [r.rstrip().rstrip("\\").rstrip() for r in inner.split("\n")]
        idx = max((k for k, r in enumerate(rows) if r.strip()), default=None)
        if idx is None or rows[idx].endswith((",", "{")):
            continue
        line = first + idx
        kind = "enum" if m.group(1).lstrip().startswith("enum") else "initializer"
        out.append(finding("trailing-comma", path, line, "P3", "Missing trailing comma on the last %s element" % kind,
                           "Add a comma after the last element, so clang-format keeps one element per line and "
                           "the next addition is a one-line diff (AGENTS.md § Readability).",
                           rows[idx].rstrip() + ","))
    return out + packed_enums(path, code, added)


def packed_enums(path, code, added):
    """Enums clang-format packed onto one line because the last enumerator has no comma.

    This is the case maintainers raise (a reformat collapses the enum); a missing comma
    in a list that stays one element per line rarely gets a comment.
    """
    out = []
    for n in sorted(added):
        line = code.split("\n")[n - 1] if n <= code.count("\n") + 1 else ""
        m = re.search(r"\benum\b[^{;]*\{([^{}]*)\}", line)
        if m and m.group(1).count(",") >= 1 and not m.group(1).rstrip().endswith(","):
            out.append(finding("trailing-comma", path, n, "P3", "Enum packed onto one line for lack of a trailing comma",
                               "Add a comma after the last enumerator and re-run clang-format: it then keeps one "
                               "enumerator per line (AGENTS.md § Readability)."))
    return out


def check_lines(path, lines, added, existing=frozenset()):
    """Single-line rules on the added lines. `existing` holds the names the base's headers
    already declare: implementing an existing interface (`delayUs`) isn't new API."""
    out = []
    cpp = path.endswith(CPP_EXT)
    for n in sorted(added):
        if n > len(lines):
            continue
        raw = lines[n - 1]
        line = strip_comments(raw)
        if re.match(r"\s*typedef\s+(struct|enum|union)\b", line):
            out.append(finding("typedef", path, n, "P2", "typedef of a struct or enum",
                               "Use the plain `struct name`/`enum name` (AGENTS.md § Types and Data Structures)."))
        m = re.search(r"sizeof\s*\(?\s*(\w+)\s*\)?\s*/\s*sizeof\s*\(?\s*\1\s*\[\s*0\s*\]\s*\)?", line)
        if m:
            out.append(finding("array-size", path, n, "P3", "Use ARRAY_SIZE()",
                               "`ARRAY_SIZE()` from `core/utils.h` does this (AGENTS.md § Readability).",
                               raw.replace(m.group(0), "ARRAY_SIZE(%s)" % m.group(1))))
        if cpp and re.search(r"\bstd::string\b", line):
            out.append(finding("std-string", path, n, "P2", "std::string in firmware code",
                               "No `std::string` in the codebase: it allocates on the heap "
                               "(AGENTS.md § C++ Class Conventions)."))
        # only constant definitions (with an initializer): `static const T *p;` is a pointer to const
        if cpp and re.search(r"\bstatic\s+const\b(?!expr)", line) and "=" in line and "(" not in line.split("=")[0]:
            out.append(finding("static-constexpr", path, n, "P3", "static const instead of static constexpr",
                               "Constants and lookup tables are `static constexpr` (AGENTS.md § C++ Class Conventions).",
                               raw.replace("static const", "static constexpr", 1)))
        if path.endswith(C_EXT):
            m = re.match(r"(?!static\b|typedef\b|return\b|else\b|extern\s+\"C\")(?:extern\s+|inline\s+)*"
                         r"[A-Za-z_][\w\s\*]*?[\s\*]([a-z][A-Za-z0-9]*)\s*\([^;{]*\)?\s*[;{]?\s*$", line)
            prototype_in_c = path.endswith(".c") and line.rstrip().endswith(";")  # reported at the definition
            if m and m.group(1) not in ("main",) and m.group(1) not in existing and not re.match(r"\s", raw) \
                    and not prototype_in_c:
                out.append(finding("module-prefix", path, n, "P2", "Public function without a module prefix",
                                   "`%s` is visible outside this file but has no `module_` prefix. Make it `static` "
                                   "if only this file uses it; otherwise prefix it with its module "
                                   "(AGENTS.md § Naming)." % m.group(1)))
    return out


def declared_in_headers(repo, head):
    """Every identifier in the tree's headers at HEAD: what other files can name."""
    out = git(repo, "grep", "-h", "-o", "-w", "-E", r"[A-Za-z_][A-Za-z0-9_]*", head, "--", "*.h", "*.hpp")
    return set(out.split("\n")) - {""}


def check_file_local(path, lines, added, declared, used_elsewhere=lambda name: False):
    """A new C function or file-scope variable nothing else mentions is used only in its file: make it static.

    `declared` is every identifier in the headers; `used_elsewhere(name)` says whether another
    source file names it, which catches the `extern` prototypes some modules keep in .c files.
    """
    out = []
    if not path.endswith(".c"):
        return out
    not_decl = r"(?!static\b|typedef\b|extern\b|return\b|else\b|const\s+struct\b|#)"
    for n in sorted(added):
        if n > len(lines):
            continue
        line = strip_comments(lines[n - 1])
        fn = re.match(not_decl + r"(?:inline\s+)?[A-Za-z_][\w\s\*]*?[\s\*]([a-z_]\w*)\s*\([^;]*$", line)
        var = re.match(not_decl + r"(?:volatile\s+|const\s+)*[A-Za-z_]\w*[\s\*]+([a-z_]\w*)\s*(\[[^\]]*\])?\s*(=[^;]*)?;\s*$",
                       line)
        m, kind = (fn, "Function") if fn else (var, "File-scope variable")
        if m and m.group(1) not in declared and m.group(1) != "main" and "IRQ" not in m.group(1) \
                and not used_elsewhere(m.group(1)):
            out.append(finding("file-local", path, n, "P2", "%s used only in this file is not static" % kind,
                               "Nothing outside this file mentions `%s`, so it can't be used elsewhere: make it "
                               "`static` (AGENTS.md § Naming)." % m.group(1),
                               "static " + lines[n - 1].lstrip()))
    return out


def inside_block(code, n):
    """True when line n sits inside a { } block (a struct, enum or initializer), not at file level.

    `extern "C" {` and `namespace x {` wrap whole headers, so they count as file level.
    """
    depth = 0
    for j in range(n - 2, -1, -1):
        for ch in reversed(code[j]):
            if ch == "}":
                depth += 1
            elif ch == "{":
                if depth > 0:
                    depth -= 1
                    continue
                opener = code[j] if code[j].strip() != "{" else (code[j - 1] if j else "")
                if re.search(r'extern\s+"C"|\bnamespace\b', opener):
                    continue
                return True
    return False


def check_append_only(path, lines, added):
    """New entries in a positional list go at its end, in every language."""
    out = []
    entry = re.compile(r"^\s*[^\s{}#/][^{}]*$")  # an element line, not a block's opening or closing line
    code = strip_comments("\n".join(lines)).split("\n")  # across lines, so block comments are blanked too
    for n in sorted(added):
        if n > len(code) or not entry.match(code[n - 1]) or (n - 1) in added or not inside_block(code, n):
            continue
        # walk forward through the run of added lines, then look for an old entry before the block ends
        k = n
        while k + 1 in added:
            k += 1
        depth = 0
        for j in range(k, len(code)):
            text = code[j]
            depth += text.count("{") - text.count("}")
            if depth < 0 or text.strip().startswith("}"):
                break
            if entry.match(text):
                out.append(finding("append-only", path, n, "P1", "Entry inserted in the middle of a positional list",
                                   "This list is indexed by position (saved settings, string table, voice "
                                   "prompts): add new entries at the end, in every language, or existing "
                                   "radios and voice prompts read the wrong entry (AGENTS.md § Persistent Data)."))
                break
    return out


# Calls a layer may not make (AGENTS.md § Layering and Ownership, § Concurrency and Real-Time):
# (pattern, test on the path that makes the call a violation, title, body)
CALL_RULES = [
    (r"\bradio_[a-zA-Z]+\s*\(", lambda p: p.startswith("openrtx/") and not p.startswith(("openrtx/src/rtx/", "openrtx/include/")),
     "Radio API called outside the rtx thread",
     "Only the rtx thread drives the radio: change the configuration and set `sync_rtx = true` instead "
     "(AGENTS.md § Layering and Ownership)."),
    (r"\bpthread_mutex_(?:lock|trylock)\s*\(\s*&\s*state_mutex\b", lambda p: p.startswith("openrtx/src/rtx/"),
     "rtx thread takes the state mutex",
     "Waiting on the state mutex can delay the rtx thread and break real-time work such as M17 modulation; "
     "the rtx thread must not take it (AGENTS.md § Concurrency and Real-Time)."),
    (r"\bui_updateFSM\s*\(", lambda p: p.startswith("openrtx/") and p != "openrtx/src/core/threads.c",
     "ui_updateFSM() called outside the UI thread",
     "The UI thread already calls `ui_updateFSM()` (threads.c); calling it from elsewhere races with it."),
    (r"\bdisplay_(?:init|terminate)\s*\(", lambda p: p.startswith("openrtx/src/ui/"),
     "Display driver initialised from the UI",
     "`display_init()` runs once at startup; the UI shouldn't re-initialise the driver."),
]


def check_calls(path, lines, added):
    """Calls the layer this file belongs to may not make."""
    out = []
    for n in sorted(added):
        if n > len(lines) or not re.match(r"\s", lines[n - 1]):  # calls sit inside function bodies
            continue
        line = strip_comments(lines[n - 1])
        for pattern, bad, title, body in CALL_RULES:
            if bad(path) and re.search(pattern, line):
                out.append(finding("layering", path, n, "P1", title, body))
    return out


def block_opener(code, n):
    """The text that opens the innermost { } block around line n ('' at file level)."""
    depth = 0
    for j in range(n - 2, -1, -1):
        for ch in reversed(code[j]):
            if ch == "}":
                depth += 1
            elif ch == "{":
                if depth > 0:
                    depth -= 1
                    continue
                return " ".join(code[max(0, j - 1):j + 1])
    return ""


def check_unused(path, lines, added, refs):
    """New struct members and macros that nothing references (`refs(name)` counts lines naming it).

    Only in openrtx/: drivers and MCU support legitimately add register maps whose
    definitions mostly go unused.
    """
    out = []
    if not path.startswith("openrtx/"):
        return out
    code = strip_comments("\n".join(lines)).split("\n")
    for n in sorted(added):
        if n > len(code):
            continue
        line = code[n - 1]
        m = re.match(r"\s*#\s*define\s+([A-Za-z_]\w*)", line)
        kind = "Macro"
        if not m and path.endswith((".h", ".hpp")) and re.match(r"\s", line) \
                and re.search(r"\b(struct|union)\b", block_opener(code, n)) and "=" not in block_opener(code, n):
            m = re.match(r"\s+(?:const\s+|volatile\s+|unsigned\s+|struct\s+|enum\s+)*[A-Za-z_][\w:<>]*[\s\*]+"
                         r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*(?::\s*\w+)?\s*;\s*$", line)
            kind = "Struct member"
        if not m or re.search(r"_H_?$|_HPP_?$", m.group(1)):
            continue
        if refs(m.group(1)) <= 1:
            out.append(finding("unused", path, n, "P2", "%s `%s` is not used" % (kind, m.group(1)),
                               "Nothing else in the tree refers to `%s`. Remove it, or add it with the change "
                               "that uses it." % m.group(1)))
    return out


def check_doxygen(path, lines, added):
    """New public function declarations in headers need a Doxygen comment (AGENTS.md § Documentation)."""
    out = []
    if not path.endswith((".h", ".hpp")) or not path.startswith(("openrtx/include/", "platform/")):
        return out
    for n in sorted(added):
        if n > len(lines):
            continue
        line = strip_comments(lines[n - 1])
        c_decl = re.match(r"(?!typedef\b|#|return\b|struct\s+\w+\s*\{)(?:extern\s+|static\s+inline\s+|inline\s+)?"
                          r"[A-Za-z_][\w\s\*]*?[\s\*]([A-Za-z_]\w*)\s*\(", line)
        # a method declaration has a return type before its name; `reset();` is a call
        cpp_decl = path.endswith(".hpp") and re.match(r"\s+(?:virtual\s+|static\s+|inline\s+)*(?:const\s+)?"
                                                      r"[A-Za-z_][\w:]*(?:<[^;()]*>)?[\s\*&]+"
                                                      r"([A-Za-z_]\w*)\s*\([^;{]*\)\s*(?:const\s*)?"
                                                      r"(?:override\s*)?;\s*$", line)
        m = c_decl or cpp_decl
        if not m or "return" in line or m.group(1) in ("if", "while", "for", "switch", "sizeof"):
            continue
        prev = next((lines[j].strip() for j in range(n - 2, -1, -1) if lines[j].strip()), "")
        if not (prev.endswith("*/") or prev.startswith("//")):
            out.append(finding("doxygen", path, n, "P2", "No Doxygen comment on `%s`" % m.group(1),
                               "Every public function is documented in its header with a Doxygen comment "
                               "(AGENTS.md § Documentation)."))
    return out


def check_static_locals(path, lines, added):
    """Static variables inside function bodies: keep state at file scope, at the top of the file."""
    out = []
    if not path.endswith((".c", ".cpp", ".cc")):
        return out
    code = strip_comments("\n".join(lines)).split("\n")
    for n in sorted(added):
        if n > len(code) or not re.match(r"\s+static\s+(?!const\b|constexpr\b|inline\b|void\b)[A-Za-z_]", code[n - 1]):
            continue
        opener = block_opener(code, n)
        if ")" in opener and not re.search(r"\b(struct|class|enum|namespace)\b", opener):
            out.append(finding("static-local", path, n, "P3", "Static variable inside a function",
                               "Move function-local static state to the beginning of the file, where the "
                               "module's state lives."))
    return out


def check_switch_chain(path, lines, added):
    """An if / else if chain comparing one variable with constants reads better as a switch."""
    out = []
    if not path.endswith(SOURCE_EXT):
        return out
    test = r"\(\s*([\w.\->\[\]]+)\s*==\s*[\w:]+\s*\)"
    for n in sorted(added):
        if n > len(lines):
            continue
        m = re.match(r"\s*if\s*" + test, strip_comments(lines[n - 1]))
        if not m:
            continue
        var, branches = m.group(1), 0
        for j in range(n, min(n + 60, len(lines))):
            e = re.search(r"\belse\s+if\s*" + test, strip_comments(lines[j]))
            if e and e.group(1) == var:
                branches += 1
        if branches >= 2:
            out.append(finding("switch", path, n, "P3", "if / else if chain on `%s`" % var,
                               "Comparing one value with several constants reads better as a `switch`; put "
                               "the error case in `default` (`return -ENOTSUP`) (AGENTS.md § Readability)."))
    return out


def check_define_location(path, lines, added):
    """#defines in a source file belong at its top, not between functions."""
    out = []
    if not path.endswith((".c", ".cpp", ".cc")):
        return out
    code = strip_comments("\n".join(lines)).split("\n")
    first_fn = next((i + 1 for i, l in enumerate(code)
                     if re.match(r"[A-Za-z_][\w\s\*:<>]*\b\w+\s*\([^;]*$", l) and not l.startswith(("#", "typedef"))
                     and any(code[k].strip().startswith("{") or code[k].rstrip().endswith("{")
                             for k in range(i, min(i + 3, len(code))))), None)
    for n in sorted(added):
        if first_fn and n > first_fn and n <= len(code) and re.match(r"\s*#\s*define\b", code[n - 1]):
            out.append(finding("define-location", path, n, "P3", "#define between functions",
                               "Put macros at the beginning of the source file, with the others."))
    return out


def top_level_split(expr):
    """Split an expression at depth-0 && and ||, stopping at the parenthesis that closes it."""
    parts, depth, start, i = [], 0, 0, 0
    while i < len(expr):
        ch = expr[i]
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
            if depth < 0:  # the `)` that closes the if / while condition
                expr = expr[:i]
                break
        elif depth == 0 and expr[i:i + 2] in ("&&", "||"):
            parts.append(expr[start:i])
            start, i = i + 2, i + 2
            continue
        i += 1
    return parts + [expr[start:]]


def unwrapped_comparison(operand):
    """True when an operand has a comparison outside parentheses."""
    s = operand.strip()
    depth = 0
    for i, ch in enumerate(s):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif depth == 0 and (s[i:i + 2] in ("==", "!=", "<=", ">=") or
                             (ch in "<>" and s[i - 1:i] not in "<>-" and s[i + 1:i + 2] not in "<>=")):
            return True
    return False


def check_parentheses(path, lines, added):
    """Each comparison in a compound condition sits in its own parentheses (AGENTS.md § Readability)."""
    out = []
    if not path.endswith(SOURCE_EXT):
        return out
    for n in sorted(added):
        if n > len(lines):
            continue
        line = strip_comments(lines[n - 1])
        if "&&" not in line and "||" not in line or "template" in line:
            continue
        # the condition may continue on the next line: take everything after `if (`
        m = re.search(r"\b(?:if|while)\s*\((.*)", line)
        expr = m.group(1) if m else re.split(r"\breturn\b|=(?!=)", line, maxsplit=1)[-1]
        parts = top_level_split(expr)
        if len(parts) > 1 and any(unwrapped_comparison(p) for p in parts):
            out.append(finding("parentheses", path, n, "P3", "Comparison not in its own parentheses",
                               "Put each comparison of a compound condition in parentheses: "
                               "`(a == b) && (c < d)` (AGENTS.md § Readability)."))
    return out


def check_std_array(path, lines, added):
    """C++ class members that are raw arrays: use std::array (AGENTS.md § C++ Class Conventions)."""
    out = []
    if not path.endswith(CPP_EXT):
        return out
    code = strip_comments("\n".join(lines)).split("\n")
    for n in sorted(added):
        if n > len(code):
            continue
        m = re.match(r"\s+(?:static\s+)?(?:constexpr\s+|const\s+)*(?!char\b)([A-Za-z_][\w:<>]*)\s+(\w+)\s*\[[^\]]+\]\s*(?:=.*)?;",
                     code[n - 1])
        if m and re.search(r"\b(class|struct)\b", block_opener(code, n)):
            out.append(finding("std-array", path, n, "P3", "Raw array member `%s`" % m.group(2),
                               "Use `std::array<%s, N>` for class members: it carries its size and has "
                               "`fill()` (AGENTS.md § C++ Class Conventions)." % m.group(1)))
    return out


def class_members(body):
    """Split a class body into (access-or-None, member text, offset) at depth 0."""
    out, depth, start = [], 0, 0
    i = 0
    while i < len(body):
        ch = body[i]
        if ch in "{(":
            depth += 1
        elif ch in "})":
            depth -= 1
            if ch == "}" and depth == 0:  # an inline body or nested type ends the member
                j = i + 1
                while j < len(body) and body[j] in " \t;":
                    j += 1
                out.append((body[start:j], start))
                start = i = j
                continue
        elif ch == ";" and depth == 0:
            out.append((body[start:i + 1], start))
            start = i + 1
        elif ch == ":" and depth == 0 and re.search(r"\b(public|private|protected)\s*$", body[start:i]) \
                and body[i + 1:i + 2] != ":":
            out.append((body[start:i + 1], start))
            start = i + 1
        i += 1
    return out


# Member order (AGENTS.md § C++ Class Conventions): public methods, private methods,
# private constants and enums, private variables.
RANK_NAMES = {0: "public methods", 1: "private methods", 2: "private constants and enums", 3: "private variables"}


def check_cpp_classes(path, text, added):
    """Class layout: member order, explicit constructor and destructor, private data, access specifiers at column 0."""
    out = []
    if not path.endswith(CPP_EXT):
        return out
    code = first_branches(strip_comments(text))  # one branch of each #if; directives blanked, lines kept
    line_of = lambda off: code.count("\n", 0, off) + 1  # noqa: E731
    for m in re.finditer(r"(?<!enum\s)\b(class|struct)\s+(\w+)\s*(?:final\s*)?(?::[^{;()]*)?\{", code):
        if re.search(r"\benum\s*$", code[max(0, m.start() - 8):m.start()]):
            continue
        open_i = m.end() - 1
        close_i = matching_brace(code, open_i)
        if close_i < 0:
            continue
        first, last = line_of(m.start()), line_of(close_i)
        touched = {n for n in added if first <= n <= last}
        if not touched:
            continue
        new_class = first in added
        kind, name = m.group(1), m.group(2)
        access = "private" if kind == "class" else "public"
        highest, highest_desc, reported = -1, "", False
        methods, ctor, dtor, public_data = 0, False, False, []
        starts, stops = [], []  # init()-style and terminate()-style methods: where a constructor/destructor belongs
        for member, off in class_members(code[open_i + 1:close_i]):
            s = member.strip()
            line = line_of(open_i + 1 + off + (len(member) - len(member.lstrip())))
            spec = re.match(r"(public|private|protected)\s*:$", s)
            if spec:
                access = spec.group(1)
                if line in added and re.match(r"[ \t]+(public|private|protected)\s*:", code.split("\n")[line - 1]):
                    out.append(finding("cpp-class", path, line, "P3", "Access specifier indented in `%s`" % name,
                                       "Access specifiers sit at column 0 (AGENTS.md § C++ Class Conventions, #166)."))
                continue
            if not s or s.startswith(("friend", "template", "class", "struct", "union", "static_assert")):
                continue
            decl = s.split("=")[0]
            # a method: a name (or `operator…`) followed by `(`; `(*name)` is a function-pointer variable
            is_method = (bool(re.search(r"\b~?\w+\s*\(", decl)) or bool(re.search(r"\boperator\b", decl))) \
                and not re.search(r"\(\s*\*", decl) and not s.startswith(("using", "typedef", "enum"))
            if is_method:
                methods += 1
                ctor |= bool(re.search(r"(^|[\s:])%s\s*\(" % name, s.split("{")[0])) and "~" not in s.split("(")[0]
                dtor |= bool(re.search(r"~\s*%s\s*\(" % name, s))
                if re.search(r"\b(init|initialize|setup|begin|start)\s*\(", decl, re.I):
                    starts.append(line)
                if re.search(r"\b(terminate|deinit|cleanup|shutdown|destroy|end)\s*\(", decl, re.I):
                    stops.append(line)
                rank, desc = (0 if access == "public" else 1), "method"
            elif re.match(r"(enum\b|using\b|typedef\b|static\s+constexpr\b|static\s+const\b)", s):
                rank, desc = (0 if access == "public" else 2), "constant or type"
            else:
                rank, desc = 3, "variable"
                if access == "public" and kind == "class":
                    public_data.append(line)
            if rank < highest and not reported and (line in added or new_class):
                out.append(finding("cpp-class", path, line, "P3", "Member out of order in `%s`" % name,
                                   "This %s comes after the class's %s. The order is public methods, private "
                                   "methods, private constants and enums, then private variables "
                                   "(AGENTS.md § C++ Class Conventions; `Demodulator.hpp` is the reference)."
                                   % (desc, RANK_NAMES[highest])))
                reported = True
            if rank > highest:
                highest = rank
        for line in public_data[:1]:
            if line in added or new_class:
                out.append(finding("cpp-class", path, line, "P2", "Public data member in `%s`" % name,
                                   "Default to private, and expose state through methods only when callers "
                                   "need it (AGENTS.md § C++ Class Conventions)."))
        if kind == "class" and new_class and methods and not (ctor and dtor) and path.endswith((".hpp", ".hh")):
            why = ("Declare an explicit constructor and destructor, even when empty, and set the object up in the "
                   "constructor and tear it down in the destructor rather than in `init()`/`terminate()` "
                   "methods (AGENTS.md § C++ Class Conventions, #481).")
            # anchor each at the method doing its job, where a reviewer would comment
            if not ctor and starts:
                out.append(finding("cpp-class", path, starts[0], "P3", "`%s` uses an init method, not a constructor" % name, why))
            if not dtor and stops:
                out.append(finding("cpp-class", path, stops[0], "P3", "`%s` uses a terminate method, not a destructor" % name, why))
            if (not ctor and not starts) or (not dtor and not stops):
                missing = [w for w, has in (("constructor", ctor or starts), ("destructor", dtor or stops)) if not has]
                out.append(finding("cpp-class", path, first, "P3",
                                   "`%s` has no explicit %s" % (name, " or ".join(missing)), why))
    return out


def check_redundant_guards(path, lines, added):
    """A NULL check before free() or delete: both already do nothing on NULL (AGENTS.md § Reuse)."""
    out = []
    if not path.endswith(SOURCE_EXT):
        return out
    code = strip_comments("\n".join(lines)).split("\n")
    guard = re.compile(r"\bif\s*\(\s*(?:(\w[\w.\->]*)\s*(?:!=\s*(?:NULL|nullptr|0))?|(?:NULL|nullptr)\s*!=\s*(\w[\w.\->]*))\s*\)")
    for n in sorted(added):
        if n > len(code):
            continue
        m = guard.search(code[n - 1])
        if not m:
            continue
        ptr = m.group(1) or m.group(2)
        rest = code[n - 1][m.end():] + " " + " ".join(code[n:n + 2])
        call = r"(free\s*\(\s*%s\s*\)|delete(?:\s*\[\])?\s+%s)\s*;" % (re.escape(ptr), re.escape(ptr))
        # the guarded body must be the free alone: a block that also does more needs the check
        body = re.match(r"\s*\{\s*%s\s*\}" % call, rest) or (not rest.lstrip().startswith("{") and re.match(r"\s*" + call, rest))
        if body:
            out.append(finding("redundant-guard", path, n, "P3", "NULL check before %s" % body.group(1).split("(")[0].split()[0],
                               "`free(NULL)` and `delete nullptr` do nothing, so the check only adds code "
                               "(AGENTS.md § Reuse Before Reimplementation).",
                               "%s;" % body.group(1)))
    return out


def check_hw_literals(path, lines, added):
    """Frequencies hard-coded in core code break radios on other bands."""
    out = []
    if not path.startswith(("openrtx/src/core/", "openrtx/src/ui/", "openrtx/src/rtx/")):
        return out
    for n in sorted(added):
        if n <= len(lines) and re.search(r"(?<![\w.])(?:[1-9]\d{8,9})(?![\w.])", strip_comments(lines[n - 1])):
            out.append(finding("hw-literal", path, n, "P2", "Hard-coded frequency",
                               "Take defaults and limits from the hardware info (band limits), so the code "
                               "works on VHF and UHF radios alike (AGENTS.md, defaults from the hardware config)."))
    return out


def check_new_files(repo, base, head):
    """Scripts outside scripts/, where the repository keeps them all."""
    out = []
    for path in git(repo, "diff", "--name-only", "--diff-filter=A", base, head).split("\n"):
        if path.endswith((".py", ".sh", ".pl")) and not path.startswith(
                ("scripts/", "tests/", ".github/", "lib/", "subprojects/", "docs/", ".devcontainer/")):
            out.append(finding("script-location", path, 1, "P3", "Script outside scripts/",
                               "Keep scripts and generators together in `scripts/` at the repository root."))
    return out


def check_changelog(repo, base, head):
    """Changelog entries are one short line."""
    out, n = [], None
    for line in git(repo, "diff", "-U0", base, head, "--", "CHANGELOG.rst", "CHANGELOG.md").splitlines():
        if line.startswith("@@"):
            n = int(re.match(r"@@ -\S+ \+(\d+)", line).group(1))
        elif line.startswith("+") and not line.startswith("+++") and n is not None:
            if re.match(r"\s*[-*] ", line[1:]) and len(line[1:].strip()) > 80:
                out.append(finding("changelog", "CHANGELOG.rst", n, "P3", "Long changelog entry",
                                   "Keep each changelog entry to one short line; maintainers trim long ones."))
            n += 1
    return out


# ---------------------------------------------------------------- PR-level checks
# These look at the change as a whole: its commits, the PR text, persistent formats and
# the build. Their findings have no line (or the line of the offending declaration).

PREFIX = re.compile(r"^[\w.+/-]+(?:\s*[,/]\s*[\w.+/-]+)*(?:\s*:\s*[\w.+/-]+)*:\s+\S")
AUTOSQUASH = re.compile(r"^(fixup|squash|amend)!\s")
# subjects of commits that answer a review or patch up the branch rather than add a change
REVIEW_RESPONSE = re.compile(r"\b(review|address(ed|ing)?|comments?|feedback|suggestions?|typos?|nits?|cleanup|"
                             r"clean up|wip|oops|forgot|minor|small fix(es)?|fix(es|ed)? (build|ci|format|style|warnings?))\b",
                             re.I)


def commits(repo, base, head):
    """The change's commits, oldest first: dicts of sha, parents, subject, body."""
    out = []
    for rec in git(repo, "log", "--reverse", "--format=%H%x00%P%x00%s%x00%b%x1e", "%s..%s" % (base, head)).split("\x1e"):
        parts = rec.strip("\n").split("\x00")
        if len(parts) == 4:
            out.append({"sha": parts[0], "parents": parts[1].split(), "subject": parts[2], "body": parts[3]})
    return out


def hunks(repo, sha):
    """[(path, removed text, added text, old start, old count)] for one commit's source changes."""
    out, path, cur = [], None, None
    specs = ["*" + e for e in SOURCE_EXT]
    for line in git(repo, "show", "-U0", "--format=", "--no-color", sha, "--", *specs).splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("--- "):
            continue
        elif line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+", line)
            cur = [path, [], [], int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)]
            out.append(cur)
        elif cur is not None and line[:1] in "+-":
            cur[2 if line[0] == "+" else 1].append(line[1:])
    return [(p, "\n".join(r), "\n".join(a), s, n) for p, r, a, s, n in out if p]


def short(sha):
    return sha[:9]


def check_commits(repo, base, head):
    """Commit hygiene and history shape (CONTRIBUTING.md § Making Changes, AGENTS.md § Commits and PRs)."""
    out = []
    cs = commits(repo, base, head)
    if not cs:
        return out
    shas = {c["sha"] for c in cs}
    # DCO sign-off is left to CI, not the review
    for c in cs:
        s = c["subject"]
        if len(c["parents"]) > 1:
            out.append(finding("commit-merge", None, None, "P2", "Merge commit %s in the branch" % short(c["sha"]),
                               "Rebase on the base branch instead of merging it in; the history should be "
                               "a straight line of complete changes."))
            continue
        if AUTOSQUASH.match(s):
            out.append(finding("commit-autosquash", None, None, "P3", "%s still to autosquash" % short(c["sha"]),
                               "`%s` is fine during review; squash it with `git rebase --autosquash` before "
                               "the merge." % s))
            continue
        if not PREFIX.match(s) and not s.startswith(("Revert ", "Merge ")):
            out.append(finding("commit-prefix", None, None, "P3", "Commit subject without a module prefix",
                               "`%s` (%s): start the subject with the module it changes, e.g. "
                               "`drivers: …`, `ui: …`, `m17: …`." % (s, short(c["sha"]))))
    for i, c in enumerate(cs):
        if i == 0 or len(c["parents"]) != 1 or AUTOSQUASH.match(c["subject"]):
            continue
        hs = hunks(repo, c["sha"])
        # a review response that rewrites lines an earlier commit of this change added; later
        # commits building on earlier ones in the same series are normal and not flagged
        targets = set()
        for path, removed, added, start, count in (hs if REVIEW_RESPONSE.search(c["subject"]) else []):
            if count == 0 or not removed.strip():
                continue
            blame = git(repo, "blame", "--porcelain", "-L", "%d,%d" % (start, start + count - 1),
                        c["parents"][0], "--", path)
            for m in re.finditer(r"^([0-9a-f]{40}) \d+ \d+", blame, re.M):
                if m.group(1) in shas and m.group(1) != c["sha"]:
                    targets.add(m.group(1))
        for t in sorted(targets, key=lambda sha: [x["sha"] for x in cs].index(sha)):
            target = next(x for x in cs if x["sha"] == t)
            out.append(finding("commit-fixup", None, None, "P2",
                               "%s rewrites code that %s added" % (short(c["sha"]), short(t)),
                               "`%s` changes lines `%s` introduced in this same branch. Make it a "
                               "`fixup!` of that commit (or fold it in), so each commit is final."
                               % (c["subject"], target["subject"])))
        # reformatting mixed into a commit that also changes behaviour
        ws = [h for h in hs if h[1].strip() and flat(h[1]) == flat(h[2])]
        real = [h for h in hs if flat(h[1]) != flat(h[2])]
        formatting_commit = re.search(r"clang-format|\bformat|\blint\b|whitespace|\bstyle\b|indent", c["subject"], re.I)
        if len(ws) >= 3 and real and not formatting_commit:  # a stray whitespace fix isn't worth a comment
            out.append(finding("commit-mixed-format", ws[0][0], None, "P3",
                               "%s mixes reformatting with changes" % short(c["sha"]),
                               "%d hunk(s) of `%s` only change whitespace. Reformatting goes in its own commit, "
                               "separate from behavioural changes (AGENTS.md § Commits and PRs)." % (len(ws), c["subject"])))
    return out


def section(body, heading):
    """The text under a Markdown heading of the PR body, or None if the heading isn't there."""
    m = re.search(r"^#+\s*%s\s*$(.*?)(?=^#+\s|\Z)" % heading, body or "", re.M | re.S | re.I)
    return m.group(1) if m else None


def check_pr_text(body):
    """The PR template's required sections, when the PR uses the template."""
    out = []
    testing = section(body, r"Testing performed")
    if testing is not None:
        prose = re.sub(r"<!--.*?-->|^\s*[-*]\s*\[[ xX]\].*$|\*\*Tested on:?\*\*", "", testing, flags=re.S | re.M).strip()
        ticked = re.search(r"^\s*[-*]\s*\[[xX]\]", testing, re.M)
        if not prose and not ticked:  # an offline-only change explained in prose is fine
            out.append(finding("pr-testing", None, None, "P2", "Testing performed is incomplete",
                               "Say what was run and on which radios, and tick the targets it was tested on "
                               "(\"Not tested on hardware — help wanted\" is a fine answer; silence isn't)."))
    log = section(body, r"Changelog entry")
    if log is not None and not re.sub(r"<!--.*?-->", "", log, flags=re.S).strip():
        out.append(finding("pr-changelog", None, None, "P3", "No changelog entry",
                           "Give the one-line changelog entry the template asks for, or `n/a` for changes "
                           "users don't see."))
    return out


def struct_members(text, name):
    """Declarators in `typedef struct { ... } name;` (bitfield lists count each field)."""
    m = re.search(r"typedef\s+struct\s*(?:\w+\s*)?\{(.*?)\}\s*(?:__attribute__\(\(\w+\)\)\s*)?%s\s*;" % name,
                  first_branches(strip_comments(text)), re.S)  # one branch of each #if, as for the defaults
    if not m:
        return None
    n = 0
    for decl in m.group(1).split(";"):
        if decl.strip():
            n += len(top_level_split(decl.replace(",", "&&")))  # count comma-separated declarators
    return n


def first_branches(text):
    """Text with only the first branch of each #if / #else kept, as one build would see it."""
    out, live = [], []
    for line in text.split("\n"):
        s = line.strip()
        if re.match(r"#\s*if", s):
            live.append(True)
        elif re.match(r"#\s*(else|elif)", s) and live:
            live[-1] = False
        elif re.match(r"#\s*endif", s) and live:
            live.pop()
        elif all(live):
            out.append(line)
            continue
        out.append("")
    return "\n".join(out)


def initializer_entries(text, name):
    """Top-level entries of `... name = { ... };`, counting one branch of each #if."""
    code = first_branches(strip_comments(text))
    m = re.search(r"\b%s\s*=\s*\{" % name, code)
    if not m:
        return None
    close = matching_brace(code, m.end() - 1)
    inner, depth, n, seen = code[m.end():close], 0, 0, False
    for ch in inner:
        if ch in "{(":
            depth += 1
        elif ch in "})":
            depth -= 1
        elif ch == "," and depth == 0:
            n, seen = n + 1, False
            continue
        if not ch.isspace():
            seen = True
    return n + (1 if seen else 0)


def check_persistence(repo, base, head):
    """Saved settings and the codeplug format must stay readable by radios in the field."""
    out = []
    path = "openrtx/include/core/settings.h"
    diff = git(repo, "diff", "-U0", base, head, "--", path)
    if diff:
        removed = [l[1:] for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]
        added = flat("\n".join(l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")))
        gone = [l for l in removed if re.match(r"\s+[A-Za-z_][\w\s\*]*\b\w+\s*(\[[^\]]*\])?\s*(:\s*\d+)?\s*[,;]", l)
                and flat(l) not in added]
        if gone:
            out.append(finding("persistence", path, None, "P0", "A saved setting was removed or changed",
                               "Radios load `settings_t` as stored; removing or changing `%s` moves every field "
                               "after it (AGENTS.md § Persistent Data)." % gone[0].strip()))
        text = git(repo, "show", "%s:%s" % (head, path))
        members, defaults = struct_members(text, "settings_t"), initializer_entries(text, "default_settings")
        if members and defaults and members != defaults:
            out.append(finding("persistence", path, None, "P1", "default_settings doesn't match settings_t",
                               "`settings_t` has %d fields but `default_settings` gives %d values; the "
                               "initializer is positional, so every value after the gap lands in the wrong "
                               "field." % (members, defaults)))
    cps = "openrtx/include/core/cps.h"
    diff = git(repo, "diff", "-U0", base, head, "--", cps)
    if diff and re.search(r"^[-+]\s+[A-Za-z_].*;", diff, re.M) and "CPS_VERSION" not in diff:
        out.append(finding("persistence", cps, None, "P1", "Codeplug layout changed without a version bump",
                           "A codeplug structure changed but `CPS_VERSION_MAJOR/MINOR` didn't: older "
                           "codeplugs would be read with the new layout (AGENTS.md § Persistent Data)."))
    return out


def config_defined(repo, head, name):
    """True when some header, build file or Kconfig at HEAD defines a CONFIG_ name."""
    for hit in git(repo, "grep", "-n", "-w", "-F", name, head).split("\n"):
        parts = hit.split(":", 3)  # sha:path:line:text
        if len(parts) < 4:
            continue
        path, text = parts[1], parts[3]
        if re.search(r"#\s*define\s+%s\b" % name, text) or "-D" + name in text:
            return True
        if re.search(r"(^|/)(meson\.build|meson_options\.txt|CMakeLists\.txt|Kconfig[^/]*|[^/]*\.(cmake|conf|ya?ml|ini))$",
                     path) and not re.search(r"#\s*if", text):
            return True
    kconfig = name[len("CONFIG_"):]  # Zephyr: `config FOO` in a Kconfig file defines CONFIG_FOO
    return bool(git(repo, "grep", "-l", "-w", "-F", "config " + kconfig, head, "--", "*Kconfig*").strip())


def check_build(repo, base, head):
    """New sources the build never compiles, and CONFIG_ macros nothing defines."""
    out = []
    build_files = [p for p in git(repo, "ls-tree", "-r", "--name-only", head).split("\n")
                   if re.search(r"(^|/)(meson\.build|CMakeLists\.txt|[^/]*\.cmake)$", p)]
    build_text = "\n".join(git(repo, "show", "%s:%s" % (head, p)) for p in build_files)
    for path in git(repo, "diff", "--name-only", "--diff-filter=A", base, head).split("\n"):
        if path.endswith((".c", ".cpp", ".cc")) and not path.startswith(SKIP_DIRS) \
                and os.path.basename(path) not in build_text:
            out.append(finding("build-unlisted", path, None, "P1", "New source isn't in the build",
                               "No meson.build or CMakeLists.txt names `%s`, so it is never compiled."
                               % os.path.basename(path)))
    used = set()
    for path, added in added_lines(repo, base, head).items():
        text = git(repo, "show", "%s:%s" % (head, path)).split("\n")
        for n in added:
            if n <= len(text) and re.match(r"\s*#\s*(if|ifdef|ifndef|elif)\b", text[n - 1]):
                used |= set(re.findall(r"\bCONFIG_[A-Z0-9_]+\b", text[n - 1]))
    for name in sorted(used):
        if not config_defined(repo, head, name):
            out.append(finding("config-undefined", None, None, "P2", "`%s` is never defined" % name,
                               "New code tests `%s`, but no header, build file or Kconfig defines it: a typo, "
                               "or a missing default (AGENTS.md § Conditional Compilation)." % name))
    return out


def pr_checks(repo, base, head="HEAD", body=None):
    """All PR-level findings for BASE..HEAD (body: the PR description, if there is one)."""
    out = check_commits(repo, base, head) + check_persistence(repo, base, head) + check_build(repo, base, head)
    if body is not None:
        out += check_pr_text(body)
    return out


def tidy_flags(tree, path):
    """Compiler flags for clang-tidy, modelled on the Linux build."""
    inc = ["platform/targets/linux", "platform/targets/linux/emulator", "platform/drivers/audio",
           "platform/drivers/tones", "openrtx/include", "platform", "lib/minmea/include",
           "lib/QRCode/include", "lib/qdec/include"]
    m = re.match(r"platform/targets/([^/]+)/", path)
    if m:
        inc.insert(0, "platform/targets/" + m.group(1))
    std = "-std=gnu++14" if path.endswith(CPP_EXT) else "-std=gnu17"
    return [std, "-DPLATFORM_LINUX", "-DCONFIG_SCREEN_WIDTH=160", "-DCONFIG_SCREEN_HEIGHT=128",
            "-DCONFIG_PIX_FMT_RGB565", "-DCONFIG_GPS", "-DCONFIG_RTC", "-Wno-everything"] + \
        ["-I" + os.path.join(tree, d) for d in inc]


def clang_tidy(tree, changed):
    """clang-tidy's curated checks on the changed files the Linux build compiles, kept to the added lines.

    Target and MCU sources need their cross build's headers and defines; without them
    clang-tidy stops at the first missing header, so they are left to the review.
    """
    exe = os.environ.get("CLANG_TIDY") or shutil.which("clang-tidy") or "/opt/homebrew/opt/llvm/bin/clang-tidy"
    out = []
    for path, added in changed.items():
        if not path.endswith((".c", ".cpp", ".cc")) or not path.startswith(TIDY_PATHS) \
                or not os.path.exists(os.path.join(tree, path)):
            continue
        p = subprocess.run([exe, "--quiet", "--checks=" + TIDY_CHECKS, os.path.join(tree, path), "--"]
                           + tidy_flags(tree, path), capture_output=True, text=True, timeout=300)
        for m in re.finditer(r"^(.+?):(\d+):\d+: warning: (.+?) \[([\w.-]+)\]$", p.stdout, re.M):
            file, line, msg, check = m.groups()
            rel = os.path.relpath(file, tree)
            if rel == path and int(line) in added:
                out.append(finding("clang-tidy:" + check, path, int(line), "P3", msg, msg))
    return out


def lint(repo, base, head="HEAD", tidy=False, tree=None):
    """All findings for BASE..HEAD."""
    added_by_path, renamed, removed = changes(repo, base, head)
    changed = {p: a for p, a in added_by_path.items()
               if p.endswith(SOURCE_EXT) and not p.startswith(SKIP_DIRS)}
    out, declared, existing = [], None, None
    for path, added in changed.items():
        text = git(repo, "show", "%s:%s" % (head, path))
        lines = text.split("\n")
        # new content: not in the file's base version (at its old path if renamed), and not
        # removed elsewhere in the change (moved between files)
        base_text = git(repo, "show", "%s:%s" % (base, renamed.get(path, path)))
        new = new_content(lines, added, base_text, removed)
        if existing is None:
            existing = declared_in_headers(repo, base)
        # a reformat touches a block, so the comma check looks at every touched line;
        # the other rules only at lines whose content is new, not moved or re-indented
        out += check_trailing_comma(path, text, added)
        out += check_lines(path, lines, new, existing)
        if path.endswith(".c"):
            if declared is None:
                declared = declared_in_headers(repo, head)

            def used_elsewhere(name, path=path):
                hits = git(repo, "grep", "-l", "-w", "-F", name, head, "--", *["*" + e for e in SOURCE_EXT])
                return any(h.split(":", 1)[-1] != path for h in hits.split("\n") if h)

            out += check_file_local(path, lines, new, declared, used_elsewhere)
        if APPEND_ONLY.match(path):
            out += check_append_only(path, lines, new)
        out += check_calls(path, lines, new)
        out += check_doxygen(path, lines, new)
        out += check_static_locals(path, lines, new)
        out += check_switch_chain(path, lines, new)
        out += check_define_location(path, lines, new)
        out += check_parentheses(path, lines, new)
        out += check_std_array(path, lines, new)
        out += check_hw_literals(path, lines, new)
        out += check_redundant_guards(path, lines, new)
        out += check_cpp_classes(path, text, new)

        def refs(name):
            hits = git(repo, "grep", "-c", "-w", "-F", name, head, "--", *["*" + e for e in SOURCE_EXT])
            return sum(int(h.rsplit(":", 1)[1]) for h in hits.split("\n") if h)

        out += check_unused(path, lines, new, refs)
    out += check_new_files(repo, base, head)
    out += check_changelog(repo, base, head)
    if tidy:
        out += clang_tidy(tree or repo, changed)
    return sorted(out, key=lambda f: (f["path"], f["line"]))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("base")
    p.add_argument("head", nargs="?", default="HEAD")
    p.add_argument("--repo", default=".")
    p.add_argument("--clang-tidy", action="store_true")
    p.add_argument("--tree", help="checkout of HEAD for clang-tidy (default: --repo)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--pr", action="store_true",
                   help="also check the change as a whole: commits, persistent formats, the build")
    p.add_argument("--pr-body", help="a file with the PR description, to check the template's sections")
    a = p.parse_args()
    found = lint(a.repo, a.base, a.head, a.clang_tidy, a.tree)
    if a.pr or a.pr_body:
        body = open(a.pr_body).read() if a.pr_body else None
        found += pr_checks(a.repo, a.base, a.head, body)
    if a.json:
        json.dump(found, sys.stdout, indent=1)
        print()
    else:
        for f in found:
            print("%s:%s [%s] %s: %s" % (f["path"] or "-", f["line"] or "-", f["severity"], f["rule"], f["title"]))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
