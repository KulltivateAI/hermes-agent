"""Regression tests for case-insensitive contributor mapping lookup."""

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from contributor_email_mapping import (  # noqa: E402
    AmbiguousEmailMappingError,
    find_email_mapping,
)


def test_finds_exact_mapping(tmp_path):
    mapping = tmp_path / "agent@example.com"
    mapping.write_text("agent\n", encoding="utf-8")

    assert find_email_mapping("agent@example.com", tmp_path) == mapping


def test_finds_ascii_case_variant(tmp_path):
    mapping = tmp_path / "agent@agents-Mac-mini.local"
    mapping.write_text("KulltivateAI\n", encoding="utf-8")

    assert find_email_mapping("agent@Agents-Mac-mini.local", tmp_path) == mapping


def test_returns_none_for_nonmatch(tmp_path):
    (tmp_path / "other@example.com").write_text("other\n", encoding="utf-8")

    assert find_email_mapping("missing@example.com", tmp_path) is None


def test_ambiguous_case_collision_fails_closed(tmp_path, monkeypatch):
    class Entry:
        def __init__(self, name):
            self.name = name

        def is_file(self):
            return True

    entries = [Entry("agent@Example.com"), Entry("agent@example.com")]
    monkeypatch.setattr(Path, "iterdir", lambda _path: iter(entries))

    with pytest.raises(AmbiguousEmailMappingError, match="ambiguous contributor mappings"):
        find_email_mapping("AGENT@example.com", tmp_path)