#!/usr/bin/env python3
"""Case-insensitive contributor email mapping lookup used by CI and local audit."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EMAILS_DIR = REPO_ROOT / "contributors" / "emails"


class AmbiguousEmailMappingError(RuntimeError):
    """Raised when multiple filenames casefold to the requested email."""


def find_email_mapping(email: str, emails_dir: Path = EMAILS_DIR) -> Path | None:
    """Return the unique mapping whose filename casefolds to ``email``.

    Matching uses Unicode-aware ``casefold``. Multiple matches fail closed because
    selecting either mapping would make attribution filesystem-dependent.
    """
    folded = email.casefold()
    try:
        matches = sorted(
            (
                entry
                for entry in emails_dir.iterdir()
                if entry.is_file() and entry.name.casefold() == folded
            ),
            key=lambda entry: entry.name,
        )
    except OSError:
        return None

    if len(matches) > 1:
        names = ", ".join(path.name for path in matches)
        raise AmbiguousEmailMappingError(
            f"ambiguous contributor mappings for {email!r}: {names}"
        )
    return matches[0] if matches else None


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(sys.argv[0]).name} <email>", file=sys.stderr)
        return 2
    try:
        return 0 if find_email_mapping(sys.argv[1]) is not None else 1
    except AmbiguousEmailMappingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
