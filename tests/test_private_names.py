"""The private-names check (scripts/private_names_check.py). Its terms live in
a git-ignored .private/denylist.txt, so the matching is tested with made-up
terms, and the whole-repo scan runs only where that file exists."""
from __future__ import annotations

import importlib.util
import pathlib
import shutil

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("private_names_check", ROOT / "scripts" / "private_names_check.py")
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


def test_plain_terms_match_whole_identifiers_case_insensitively():
    terms = ["acme", "runkey"]
    assert check.hits("the ACME feed", terms) == ["acme"]
    assert check.hits("acme-fetcher", terms) == ["acme"]          # a dash ends a word
    assert check.hits("acmecorp and myrunkeys", terms) == []      # inside a longer word: no
    assert check.hits("SELECT runKey FROM t", terms) == ["runkey"]


def test_terms_with_punctuation_match_as_text():
    assert check.hits("mail me at x@corp.example", ["corp.example"]) == ["corp.example"]
    assert check.hits("the fetch-svc job", ["fetch-svc"]) == ["fetch-svc"]


def test_the_denylist_skips_comments_and_blanks(tmp_path):
    f = tmp_path / "d.txt"
    f.write_text("# comment\nAcme\n\n  run-key  # trailing\n")
    assert check.load_terms(f) == ["acme", "run-key"]
    assert check.load_terms(tmp_path / "missing.txt") == []


@pytest.mark.skipif(not check.DENYLIST.is_file() or shutil.which("git") is None,
                    reason="no .private/denylist.txt here (CI, a fresh clone)")
def test_no_tracked_file_contains_a_private_name():
    assert check.tree_findings(check.load_terms()) == []
