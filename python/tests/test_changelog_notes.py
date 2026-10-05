"""Tests for scripts/changelog_notes.py (release notes extracted from CHANGELOG.md)."""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "changelog_notes.py"

spec = importlib.util.spec_from_file_location("changelog_notes", SCRIPT)
changelog_notes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(changelog_notes)

SAMPLE = """# Changelog

---

## [Unreleased] — Next Release

### Roadmap

- [ ] **Future thing**

### Fixed
- Unreleased fix

---

## [0.3.4] — 2026-10-05

### Security
- Security note

### Dependencies
- dep: 1 → 2

---

## [0.3.3] — 2026-09-19

### CI/CD
- Older note

---

## [0.3.2] — 2026-09-19
"""


def test_extracts_only_the_requested_version():
    notes = changelog_notes.extract_notes(SAMPLE, "0.3.4")
    assert notes.startswith("### Security")
    assert "Security note" in notes and "dep: 1 → 2" in notes
    assert "Older note" not in notes
    assert "Unreleased fix" not in notes


def test_notes_have_no_separator_or_surrounding_blank_lines():
    notes = changelog_notes.extract_notes(SAMPLE, "0.3.3")
    assert notes == "### CI/CD\n- Older note"


def test_roadmap_subsection_is_excluded():
    notes = changelog_notes.extract_notes(SAMPLE, "0.9.9", allow_unreleased=True)
    assert "Roadmap" not in notes and "Future thing" not in notes
    assert notes == "### Fixed\n- Unreleased fix"


def test_missing_version_is_empty_without_unreleased_fallback():
    assert changelog_notes.extract_notes(SAMPLE, "0.9.9") == ""


def test_empty_version_section_is_treated_as_missing():
    assert changelog_notes.extract_notes(SAMPLE, "0.3.2") == ""


def test_existing_version_wins_over_unreleased():
    notes = changelog_notes.extract_notes(SAMPLE, "0.3.4", allow_unreleased=True)
    assert "Security note" in notes and "Unreleased fix" not in notes


def test_crlf_changelog_is_handled():
    notes = changelog_notes.extract_notes(SAMPLE.replace("\n", "\r\n"), "0.3.3")
    assert notes == "### CI/CD\n- Older note"


def _write(tmp_path, text=SAMPLE):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_cli_writes_notes_to_output_file(tmp_path):
    out = tmp_path / "notes.md"
    code = changelog_notes.main(["0.3.4", "--changelog", str(_write(tmp_path)), "--output", str(out)])
    assert code == 0
    assert out.read_text(encoding="utf-8").startswith("### Security")


def test_cli_fails_for_a_version_without_notes(tmp_path, capsys):
    code = changelog_notes.main(["0.9.9", "--changelog", str(_write(tmp_path))])
    assert code == 1
    assert "0.9.9" in capsys.readouterr().err


def test_cli_uses_fallback_text_when_given(tmp_path, capsys):
    code = changelog_notes.main(
        ["0.9.9", "--changelog", str(_write(tmp_path)), "--fallback", "Beta build."]
    )
    assert code == 0
    assert capsys.readouterr().out.strip() == "Beta build."


def test_real_changelog_is_parseable_and_dated_sections_are_not_empty():
    sections = changelog_notes.parse_sections((REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    assert "Unreleased" in sections
    dated = [name for name in sections if name != "Unreleased"]
    assert dated, "CHANGELOG.md has no released version section"
    for name in dated:
        assert changelog_notes.clean_section(sections[name]), f"[{name}] section is empty"
