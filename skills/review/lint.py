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

TIDY_CHECKS = ",".join([
    "-*",
    "readability-simplify-boolean-expr",
    "misc-use-internal-linkage",
    "modernize-use-designated-initializers",
    "bugprone-switch-missing-default-case",
    "misc-redundant-expression",
    "readability-redundant-control-flow",
    "readability-redundant-declaration",
])


def git(repo, *args):
    """Run git on the repository and return stdout ('' on failure)."""
    p = subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else ""


def added_lines(repo, base, head):
    """{path: set of new-side line numbers the change adds}, for files that exist at head."""
    out, path = {}, None
    for line in git(repo, "diff", "-U0", "--no-color", "--diff-filter=AMR", base, head).splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@") and path:
            m = re.match(r"@@ -\S+ \+(\d+)(?:,(\d+))? @@", line)
            start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            out.setdefault(path, set()).update(range(start, start + count))
    return out


def new_content(lines, added, base_text):
    """The added lines whose text (ignoring whitespace) isn't anywhere in the base version."""
    def key(line):  # whitespace and brace placement don't make a line new
        return re.sub(r"\s+", "", line).lstrip("}").rstrip("{")
    seen = {key(l) for l in base_text.split("\n")}
    return {n for n in added if n <= len(lines) and key(lines[n - 1]) not in seen}


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
    # Without the comma clang-format packs a whole enum onto one line, which is how a
    # reformatted enum usually shows up in a diff.
    for n in sorted(added):
        line = code.split("\n")[n - 1] if n <= code.count("\n") + 1 else ""
        m = re.search(r"\benum\b[^{;]*\{([^{}]*)\}", line)
        if m and m.group(1).count(",") >= 1 and not m.group(1).rstrip().endswith(","):
            out.append(finding("trailing-comma", path, n, "P3", "Enum packed onto one line for lack of a trailing comma",
                               "Add a comma after the last enumerator and re-run clang-format: it then keeps one "
                               "enumerator per line (AGENTS.md § Readability)."))
    return out


def check_lines(path, lines, added):
    """Single-line rules on the added lines."""
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
        if cpp and re.search(r"\bstatic\s+const\b(?!expr)", line) and "(" not in line.split("=")[0]:
            out.append(finding("static-constexpr", path, n, "P3", "static const instead of static constexpr",
                               "Constants and lookup tables are `static constexpr` (AGENTS.md § C++ Class Conventions).",
                               raw.replace("static const", "static constexpr", 1)))
        if path.endswith(C_EXT):
            m = re.match(r"(?!static\b|typedef\b|return\b|else\b|extern\s+\"C\")(?:extern\s+|inline\s+)*"
                         r"[A-Za-z_][\w\s\*]*?[\s\*]([a-z][A-Za-z0-9]*)\s*\([^;{]*\)?\s*[;{]?\s*$", line)
            if m and m.group(1) not in ("main",) and not re.match(r"\s", raw):
                out.append(finding("module-prefix", path, n, "P2", "Public function without a module prefix",
                                   "`%s` is visible outside this file but has no `module_` prefix. Make it `static` "
                                   "if only this file uses it; otherwise prefix it with its module "
                                   "(AGENTS.md § Naming)." % m.group(1)))
    return out


def check_append_only(path, lines, added):
    """New entries in a positional list go at its end, in every language."""
    out = []
    entry = re.compile(r"^\s*[^\s{}#/][^{}]*$")  # an element line, not a block's opening or closing line
    for n in sorted(added):
        if n > len(lines) or not entry.match(strip_comments(lines[n - 1])) or (n - 1) in added:
            continue
        # walk forward through the run of added lines, then look for an old entry before the block ends
        k = n
        while k + 1 in added:
            k += 1
        depth = 0
        for j in range(k, len(lines)):
            text = strip_comments(lines[j])
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
    """clang-tidy's curated checks on the changed files, kept to the added lines."""
    exe = os.environ.get("CLANG_TIDY") or shutil.which("clang-tidy") or "/opt/homebrew/opt/llvm/bin/clang-tidy"
    out = []
    for path, added in changed.items():
        if not path.endswith((".c", ".cpp", ".cc")) or not os.path.exists(os.path.join(tree, path)):
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
    changed = {p: a for p, a in added_lines(repo, base, head).items()
               if p.endswith(SOURCE_EXT) and not p.startswith(SKIP_DIRS)}
    out = []
    for path, added in changed.items():
        text = git(repo, "show", "%s:%s" % (head, path))
        lines = text.split("\n")
        new = new_content(lines, added, git(repo, "show", "%s:%s" % (base, path)))
        # a reformat touches a block, so the comma check looks at every touched line;
        # the other rules only at lines whose content is new, not moved or re-indented
        out += check_trailing_comma(path, text, added)
        out += check_lines(path, lines, new)
        if APPEND_ONLY.match(path):
            out += check_append_only(path, lines, new)
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
    a = p.parse_args()
    found = lint(a.repo, a.base, a.head, a.clang_tidy, a.tree)
    if a.json:
        json.dump(found, sys.stdout, indent=1)
        print()
    else:
        for f in found:
            print("%s:%s [%s] %s: %s" % (f["path"], f["line"], f["severity"], f["rule"], f["title"]))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
