# Changelog

All notable changes to MetaLens are documented in this file.

Format: [Semantic Versioning](https://semver.org/) — `MAJOR.MINOR.PATCH`

---

## [Unreleased]

### Planned
- Export metadata as CSV/JSON
- Batch edit on multiple selected files
- Search/filter bar in file list

---

## [0.1.0] — 2026-04-19

### Added
- Initial release
- Cross-platform desktop app: Windows 11 + Linux (Electron + Python sidecar)
- File-manager UI: folder tree (left), file list (center), metadata detail (right)
- Cyber dark theme: `#0a0e17` base, cyan `#00d4ff` accent
- Metadata handlers:
  - Images: JPEG, PNG, TIFF, BMP, GIF, WebP (Pillow + piexif) — EXIF read/write
  - Audio: MP3, FLAC, OGG, M4A, WAV, AIFF, WMA, APE, Opus (mutagen) — full tag read/write
  - Video: MP4/MOV read/write (mutagen); MKV/AVI read-only (hachoir)
  - PDF: metadata read/write (pypdf)
  - Office: DOCX, XLSX, PPTX metadata read/write (python-docx, openpyxl, python-pptx)
  - Legacy Office: DOC, XLS, PPT read-only (olefile)
  - Filesystem: timestamps, permissions, extended attributes (xattr/NTFS)
  - Fallback: hachoir for unrecognized formats (read-only)
- Atomic write-back: temp file + `os.replace()` — no partial corruption
- Undo stack: 50 operations per session
- Metadata diff: compare two files side by side
- About dialog with version from `/health`
- GitHub Actions CI: automated Windows (.exe) + Linux (.deb/.rpm) builds on push to `main`
- MIT License — © 2026 Graziano Mariella
