"""Extract the release notes of one version from CHANGELOG.md.

Used by the release jobs in .github/workflows/build.yml so that the GitHub Release body is the
CHANGELOG section of that exact version (not GitHub's auto-generated list of PR titles).

    python scripts/changelog_notes.py 0.3.4 --output release-notes.md

Lookup rules:
  * the section "## [VERSION]" is used;
  * with --allow-unreleased, "## [Unreleased]" is the fallback (beta builds, whose notes live
    under Unreleased until the version is promoted to stable);
  * the "Roadmap" subsection is never part of the notes;
  * if nothing usable is found the script fails (stable), or prints --fallback when it is given.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SECTION_HEADING = re.compile(r"^## \[(?P<name>[^\]]+)\]")
SUBSECTION_HEADING = re.compile(r"^### (?P<title>.+?)\s*$")
SEPARATOR = re.compile(r"^---+\s*$")
EXCLUDED_SUBSECTIONS = {"Roadmap"}


def parse_sections(text: str) -> dict[str, list[str]]:
    """Map each "## [name]" heading to the lines under it, up to the next "## [" heading."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        heading = SECTION_HEADING.match(line)
        if heading:
            current = sections.setdefault(heading.group("name"), [])
        elif current is not None:
            current.append(line)
    return sections


def clean_section(lines: list[str]) -> str:
    """Drop excluded subsections and the "---" separators, trim surrounding blank lines."""
    kept: list[str] = []
    skipping = False
    for line in lines:
        if SEPARATOR.match(line):
            continue
        subsection = SUBSECTION_HEADING.match(line)
        if subsection:
            skipping = subsection.group("title") in EXCLUDED_SUBSECTIONS
        if not skipping:
            kept.append(line)
    return "\n".join(kept).strip()


def extract_notes(text: str, version: str, allow_unreleased: bool = False) -> str:
    """Return the notes for `version`, or "" when no usable section exists."""
    sections = parse_sections(text)
    candidates = [version] + (["Unreleased"] if allow_unreleased else [])
    for name in candidates:
        if name in sections:
            notes = clean_section(sections[name])
            if notes:
                return notes
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="version section to extract, e.g. 0.3.4")
    parser.add_argument("--changelog", default="CHANGELOG.md", type=Path)
    parser.add_argument("--output", type=Path, help="write the notes here instead of stdout")
    parser.add_argument(
        "--allow-unreleased",
        action="store_true",
        help="fall back to the [Unreleased] section when the version has no section yet",
    )
    parser.add_argument("--fallback", help="text to use when no notes are found (default: fail)")
    args = parser.parse_args(argv)

    notes = extract_notes(args.changelog.read_text(encoding="utf-8"), args.version, args.allow_unreleased)
    if not notes:
        if args.fallback is None:
            print(
                f"::error::{args.changelog} has no non-empty section for [{args.version}]. "
                "Write the release notes before releasing.",
                file=sys.stderr,
            )
            return 1
        notes = args.fallback

    if args.output:
        args.output.write_text(notes + "\n", encoding="utf-8")
    else:
        # The notes contain non-ASCII characters (arrows, ≤); don't depend on the console codepage.
        sys.stdout.reconfigure(encoding="utf-8")
        print(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
