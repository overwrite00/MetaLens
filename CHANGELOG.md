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

## [0.1.5] — 2026-06-26

### Security
- **pypdf**: Updated 6.13.3 → 6.14.2 — **critical security fixes**
  - Prevent infinite loops for incomplete ASCII85 and ASCIIHex inline images (CVE-2026-53655)
  - Detect end of stream during inline image end marker detection
  - Limit requested image size to prevent memory exhaustion
  - Speed up recovery when reading broken cross-reference tables

### Dependencies
- **Build tools**:
  - electron: 42.4.1 → 42.5.0 (Chromium 148.0.7778.271, Node.js v24.17.0, Wayland window maximize fix)
  - vite: 8.0.16 → 8.1.0 (caseSensitive glob option, chunk importmap, Rolldown 1.1.2)
  - @vitejs/plugin-react: 6.0.2 → 6.0.3 (non-root base path fix)
  - autoprefixer: 10.5.0 → 10.5.2 (webkit-fill-available priority fix)
- **Backend**:
  - fastapi: 0.137.2 → 0.138.1 (app.frontend() SPA serving support)
  - mutagen: 1.47.0 → 1.48.1 (ID3 improvements, Python 3.7-3.9 drop)
- **Testing**:
  - pytest: 9.1.0 → 9.1.1 (regression fixes for parametrize and conftest loading)

### Quality
- All dependency updates verified with CI and runtime testing
- Frontend build size stable (231.65 kB JS, 6.83 kB CSS)
- FastAPI sidecar running on Python 3.11–3.13 without deprecation warnings
- Electron 42.5.0 binary verified functional with Python sidecar integration

### Notes
- Dismissed Dependabot alert #46 (node-tar): transitive build-time dependency from @electron-forge, no runtime exposure; fix unavailable upstream
- No breaking changes to application API or UI

---

## [0.1.4] — 2026-06-22

### Security
- **Critical**: Updated undici from 7.27.2 to 7.28.0 — **7 security vulnerabilities resolved**
  - **3× HIGH**: WebSocket DoS (CVE-2026-12151), TLS bypass in SOCKS5 (CVE-2026-9697), Cross-origin routing (CVE-2026-6734)
  - **2× MEDIUM**: Cache bypass (CVE-2026-9678), HTTP header injection (CVE-2026-9679)
  - **2× LOW**: SameSite downgrade (CVE-2026-11525), HTTP response poisoning (CVE-2026-6733)
  - Transitive dependency in @electron-forge
  - Fixes WebSocket fragment exhaustion, SOCKS5 proxy pool reuse, TLS certificate validation

### Quality
- All tests passing: 47 passed, 1 skipped
- Electron build: verified with undici 7.28.0
- Frontend build: confirmed compatible
- No regressions

---

## [0.1.3] — 2026-06-22

### Fixed
- **Tailwind CSS v4 Breaking Change**: Updated PostCSS configuration to use `@tailwindcss/postcss` package
  - Installed `@tailwindcss/postcss` as separate dev dependency
  - Updated postcss.config.js plugin configuration
  - CSS bundle size improved (6.83 kB vs 15.33 kB)

### Dependencies
- electron: 42.4.0 → 42.4.1 (DevTools fixes, safeStorage async fix)
- fastapi: 0.136.3 → 0.137.2 (bug fixes)
- lucide-react: 1.18.0 → 1.21.0 (icon updates)
- pypdf: 6.13.2 → 6.13.3 (security: MAX_DECLARED_STREAM_LENGTH fix, performance improvements)
- react-dom: 19.2.5 → 19.2.7
- tailwindcss: 3.4.19 → 4.3.1
- pytest: 9.0.3 → 9.1.0
- pywin32: 310 → 312
- xattr: 1.1.0 → 1.3.0

### Quality
- All tests passing: 47 passed, 1 skipped
- GitHub Actions workflow checks: all passing
- No regressions

---

## [0.1.2] — 2026-06-13

### Fixed
- **Code Quality**: Resolved all 30 CodeQL static analysis alerts
  - 13× unused imports removed from handlers, API routes, and tests
  - 7× empty except blocks properly handled with pass statements
  - 3× ineffectual statements removed from abstract method definitions
  - 2× unused variables removed (PathT global, captured_temp_path local)
  - 1× overly permissive file permissions fixed (CWE-732) in test suite
  - 3× JavaScript unused local variables cleaned (useCallback imports, SortIcon component)

### Testing
- All 47 tests passing, 1 skipped
- Zero regressions from code cleanup
- CodeQL analysis: 0 remaining security/quality alerts

### Quality Assurance
- Verified with Opus agent: comprehensive code review
- No syntax errors, type hint completeness verified
- Pydantic models functional, all imports validated

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
