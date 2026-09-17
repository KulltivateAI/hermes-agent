"""Regression tests for case-insensitive contributor mapping lookup."""

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import audit_pr_attribution  # noqa: E402
import contributor_email_mapping  # noqa: E402
from contributor_email_mapping import (  # noqa: E402
    AmbiguousEmailMappingError,
    InvalidEmailMappingError,
    find_email_mapping,
    resolve_email_mapping,
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


def test_resolves_exact_mapping_contents(tmp_path):
    (tmp_path / "agent@example.com").write_text("@agent\n", encoding="utf-8")

    assert resolve_email_mapping("agent@example.com", tmp_path) == "agent"


def test_resolves_ascii_case_variant_contents(tmp_path):
    (tmp_path / "agent@agents-Mac-mini.local").write_text(
        "momomojo\n", encoding="utf-8"
    )

    assert (
        resolve_email_mapping("agent@Agents-Mac-mini.local", tmp_path) == "momomojo"
    )


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


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        (b"", "empty contributor mapping"),
        (b"# comment only\n", "empty contributor mapping"),
        (b"not_a_github_login\n", "invalid contributor login"),
        (b"\xff\xfe\n", "cannot read contributor mapping"),
    ],
)
def test_resolve_rejects_invalid_mapping_contents(tmp_path, contents, message):
    (tmp_path / "agent@example.com").write_bytes(contents)

    with pytest.raises(InvalidEmailMappingError, match=message):
        resolve_email_mapping("agent@example.com", tmp_path)


@pytest.mark.parametrize(
    "contents",
    [b"", b"# comment only\n", b"not_a_github_login\n", b"\xff\xfe\n"],
)
def test_cli_returns_2_for_invalid_mapping(tmp_path, monkeypatch, contents):
    (tmp_path / "agent@example.com").write_bytes(contents)
    monkeypatch.setattr(contributor_email_mapping, "EMAILS_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["contributor_email_mapping.py", "agent@example.com"])

    assert contributor_email_mapping.main() == 2


def test_cli_returns_2_for_unreadable_mapping(tmp_path, monkeypatch):
    mapping = tmp_path / "agent@example.com"
    mapping.write_text("agent\n", encoding="utf-8")
    original_read_text = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == mapping:
            raise PermissionError("denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    monkeypatch.setattr(contributor_email_mapping, "EMAILS_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["contributor_email_mapping.py", "agent@example.com"])

    assert contributor_email_mapping.main() == 2


def test_cli_preserves_valid_and_absent_statuses(tmp_path, monkeypatch):
    (tmp_path / "agent@example.com").write_text("agent\n", encoding="utf-8")
    monkeypatch.setattr(contributor_email_mapping, "EMAILS_DIR", tmp_path)

    monkeypatch.setattr(sys, "argv", ["contributor_email_mapping.py", "AGENT@example.com"])
    assert contributor_email_mapping.main() == 0

    monkeypatch.setattr(sys, "argv", ["contributor_email_mapping.py", "missing@example.com"])
    assert contributor_email_mapping.main() == 1


def test_cli_returns_2_for_ambiguous_mapping(tmp_path, monkeypatch):
    class Entry:
        def __init__(self, name):
            self.name = name

        def is_file(self):
            return True

    entries = [Entry("agent@Example.com"), Entry("agent@example.com")]
    monkeypatch.setattr(Path, "iterdir", lambda _path: iter(entries))
    monkeypatch.setattr(contributor_email_mapping, "EMAILS_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["contributor_email_mapping.py", "AGENT@example.com"])

    assert contributor_email_mapping.main() == 2


@pytest.mark.parametrize(
    "contents",
    [b"", b"# comment only\n", b"not_a_github_login\n", b"\xff\xfe\n"],
)
def test_audit_rejects_invalid_mapping_without_legacy_fallback(
    tmp_path, monkeypatch, contents
):
    email = "agent@example.com"
    emails_dir = tmp_path / "contributors" / "emails"
    emails_dir.mkdir(parents=True)
    (emails_dir / email).write_bytes(contents)
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "release.py").write_text(
        f'LEGACY_AUTHOR_MAP = {{"{email}": "legacy-user"}}\n', encoding="utf-8"
    )
    monkeypatch.setattr(audit_pr_attribution, "REPO_ROOT", tmp_path)

    assert audit_pr_attribution.is_mapped(email) is False


def test_audit_rejects_unreadable_mapping(tmp_path, monkeypatch):
    email = "agent@example.com"
    emails_dir = tmp_path / "contributors" / "emails"
    emails_dir.mkdir(parents=True)
    mapping = emails_dir / email
    mapping.write_text("agent\n", encoding="utf-8")
    monkeypatch.setattr(audit_pr_attribution, "REPO_ROOT", tmp_path)
    original_read_text = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == mapping:
            raise PermissionError("denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)

    assert audit_pr_attribution.is_mapped(email) is False


def test_audit_accepts_valid_exact_and_casefold_mappings(tmp_path, monkeypatch):
    emails_dir = tmp_path / "contributors" / "emails"
    emails_dir.mkdir(parents=True)
    (emails_dir / "agent@agents-Mac-mini.local").write_text(
        "momomojo\n", encoding="utf-8"
    )
    monkeypatch.setattr(audit_pr_attribution, "REPO_ROOT", tmp_path)

    assert audit_pr_attribution.is_mapped("agent@agents-Mac-mini.local") is True
    assert audit_pr_attribution.is_mapped("agent@Agents-Mac-mini.local") is True


def test_audit_rejects_ambiguous_mapping(tmp_path, monkeypatch):
    class Entry:
        def __init__(self, name):
            self.name = name

        def is_file(self):
            return True

    entries = [Entry("agent@Example.com"), Entry("agent@example.com")]
    monkeypatch.setattr(Path, "iterdir", lambda _path: iter(entries))
    monkeypatch.setattr(audit_pr_attribution, "REPO_ROOT", tmp_path)

    assert audit_pr_attribution.is_mapped("AGENT@example.com") is False


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