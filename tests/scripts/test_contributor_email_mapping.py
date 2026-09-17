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
import release  # noqa: E402


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


def test_release_resolves_historical_author_case_insensitively():
    assert (
        release.resolve_author("Historical Agent", "agent@Agents-Mac-mini.local")
        == "@momomojo"
    )


def test_release_directory_resolution_precedence_and_fail_closed(tmp_path, monkeypatch):
    exact_email = "agent@example.com"
    (tmp_path / exact_email).write_text("directory-user\n", encoding="utf-8")
    monkeypatch.setattr(release, "CONTRIBUTORS_EMAILS_DIR", tmp_path)
    monkeypatch.setitem(release.LEGACY_AUTHOR_MAP, exact_email, "legacy-user")

    assert release.resolve_author("Exact Agent", exact_email) == "@directory-user"
    assert release.resolve_author("Missing Agent", "missing@example.com") == "Missing Agent"

    legacy_email = "Legacy@Example.com"
    monkeypatch.setitem(release.LEGACY_AUTHOR_MAP, legacy_email, "legacy-user")
    assert release.resolve_author("Legacy Agent", legacy_email) == "@legacy-user"
    assert release.resolve_author("Legacy Agent", legacy_email.lower()) == "Legacy Agent"

    (tmp_path / exact_email).write_text("# no login\n", encoding="utf-8")
    assert release.resolve_author("Malformed Agent", exact_email) == "Malformed Agent"

    class Entry:
        def __init__(self, name):
            self.name = name

        def is_file(self):
            return True

    entries = [Entry("collision@Example.com"), Entry("collision@example.com")]
    monkeypatch.setattr(Path, "iterdir", lambda _path: iter(entries))
    monkeypatch.setitem(
        release.LEGACY_AUTHOR_MAP, "COLLISION@example.com", "legacy-collision"
    )
    assert (
        release.resolve_author("Ambiguous Agent", "COLLISION@example.com")
        == "Ambiguous Agent"
    )