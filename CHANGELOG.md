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

## [0.1.1] — 2026-06-13

### Security
- **High Priority**: Comprehensive path validation layer (`core.path_security` module)
  - Prevents symlink traversal attacks during atomic metadata write (TOCTOU vulnerability)
  - Validates all file paths before filesystem access across all 6 API routes
  - Supports full filesystem access (home directory + external drives + all disks)
  - Resolves symlinks, normalizes paths, checks existence and permissions

- **Medium Priority**: GitHub Actions workflow hardening
  - Added explicit `permissions` declarations to all jobs (principle of least privilege)
  - `get-version`, `build-windows`, `build-linux` jobs: `contents: read`
  - `release` job: `contents: write`

### Fixed
- Resolved 13 Code Scanning security alerts (100% of identified issues)
  - 9× path injection vulnerabilities (HIGH) → Explicit path validation
  - 3× missing workflow permissions (MEDIUM) → Explicit permission declarations
  - 1× insecure temporary file (HIGH) → `NamedTemporaryFile` usage

### Testing
- Added 30 comprehensive security tests (`test_path_security.py`)
  - Path normalization and validation
  - Atomic write with error cleanup
  - Cross-platform edge cases (unicode, special chars, deeply nested paths)
  - API flow validation (read, list, write operations)
  - **Result**: 47 passed, 1 skipped (symlink on Windows), 0 failed

### Documentation
- Added comprehensive security documentation (2000+ lines)
  - `SECURITY_ANALYSIS_Q_AND_A.md`: Deep threat model analysis
  - `python/SECURITY.md`: Security design and implementation guide
  - `python/core/PATH_SECURITY_USAGE.md`: API reference for developers

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
