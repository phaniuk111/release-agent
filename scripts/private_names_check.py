#!/usr/bin/env python3
"""Refuse a commit that contains a private name.

The terms live in .private/denylist.txt — one per line, '#' for comments —
which is git-ignored: the list of names to keep out of the repo cannot itself
be in the repo. A term made only of letters, digits and underscores matches a
whole identifier (case-insensitive), so 'abc' does not fire inside 'abcd'; any
other term (with a dot, a dash, a space) matches as text.

    private_names_check.py --staged   the lines this commit adds (the hook)
    private_names_check.py --tree     every tracked file (the test)

No denylist file: nothing to check, exit 0.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DENYLIST = ROOT / ".private" / "denylist.txt"
_WORD = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


def load_terms(path: pathlib.Path = DENYLIST) -> list[str]:
    if not path.is_file():
        return []
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        t = line.split("#", 1)[0].strip()
        if t:
            terms.append(t.lower())
    return terms


def _words(text: str) -> set[str]:
    out, cur = set(), []
    for ch in text.lower():
        if ch in _WORD:
            cur.append(ch)
        elif cur:
            out.add("".join(cur))
            cur = []
    if cur:
        out.add("".join(cur))
    return out


def hits(text: str, terms: list[str]) -> list[str]:
    """The terms found in ``text``."""
    low = text.lower()
    words = _words(text)
    return [t for t in terms if (t in words if all(c in _WORD for c in t) else t in low)]


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def staged_findings(terms: list[str]) -> list[str]:
    found, path = [], "?"
    for line in _git("diff", "--cached", "-U0", "--no-color").splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("+") and not line.startswith("+++"):
            for t in hits(line[1:], terms):
                found.append(f"{path}: adds {t!r}")
    names = _git("diff", "--cached", "--name-only").splitlines()
    for name in names:
        for t in hits(name, terms):
            found.append(f"{name}: the file name contains {t!r}")
    return found


def tree_findings(terms: list[str]) -> list[str]:
    found = []
    for name in _git("ls-files").splitlines():
        p = ROOT / name
        for t in hits(name, terms):
            found.append(f"{name}: the file name contains {t!r}")
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue   # binary or gone
        for n, line in enumerate(text.splitlines(), 1):
            for t in hits(line, terms):
                found.append(f"{name}:{n}: contains {t!r}")
    return found


def main(argv: list[str]) -> int:
    terms = load_terms()
    if not terms:
        return 0
    found = tree_findings(terms) if "--tree" in argv else staged_findings(terms)
    if found:
        print("Private names found — not committing (terms from .private/denylist.txt):", file=sys.stderr)
        for f in found[:50]:
            print(f"  {f}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
