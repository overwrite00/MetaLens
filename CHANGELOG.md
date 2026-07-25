# Changelog

All notable changes to MetaLens are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) — `MAJOR.MINOR.PATCH`.

---

## [Unreleased] — Next Release

### Roadmap

- [ ] **CSV/JSON Export** — Export metadata for the current file or folder
- [ ] **Batch Edit** — Apply a field change to multiple selected files at once
- [ ] **Search/Filter Bar** — Filter the file list by name or extension

---

## [0.2.7-beta.1] — 2026-07-25

### Security
- Resolved Electron Forge toolchain audit findings by overriding vulnerable transitive build dependencies:
  `@electron/rebuild` 3.7.2 → 4.2.0, `tar` 6.2.1 → 7.5.22,
  `tmp` 0.0.33 → 0.2.7, and `brace-expansion` 1.1.14/2.1.0 → 5.0.8.

### Quality
- Electron dependency audit verified with 0 vulnerabilities.
- Electron Forge packaging verified on Windows.
- All Python tests passing: 47 passed, 1 skipped.
- Frontend production build verified.

---

## [0.2.6-beta.1] — 2026-07-25

### Dependencies
- fastapi: 0.139.2 → 0.140.0

### Quality
- All Python tests passing: 47 passed, 1 skipped
- Frontend production build verified
- Electron 43.2.0 installation verified

---

## [0.2.5-beta.1] — 2026-07-25

### Security
- Updated transitive Electron tooling dependency `fast-uri` from 3.1.2 to 3.1.4 to include
  upstream security fixes.

### Dependencies
- electron: 43.1.1 → 43.2.0 (Chromium/Electron fixes)
- react: 19.2.7 → 19.2.8
- react-dom: 19.2.7 → 19.2.8
- lucide-react: 1.24.0 → 1.26.0
- @vitejs/plugin-react: 6.0.3 → 6.0.4
- @fontsource/inter: 5.2.8 → 5.3.0
- @fontsource/jetbrains-mono: 5.2.8 → 5.3.0

### Quality
- All Python tests passing: 47 passed, 1 skipped
- Frontend production build verified
- Electron 43.2.0 installation verified

---

## [0.2.4] — 2026-07-21

### Fixed
- **Squirrel install/update relaunch flash (Windows)** — the app now exits immediately when
  launched by Squirrel for install/update/uninstall shortcut events, instead of booting the
  full GUI+splash and then being killed and relaunched by Squirrel once it finishes — this was
  visible as the window and splash briefly appearing, closing, and reopening a few seconds
  later. Implemented inline rather than via the `electron-squirrel-startup` package: the
  packager's `ignore: [/node_modules/]` rule strips `node_modules` from the packaged app
  entirely, so the external module was missing at runtime and crashed the app on launch
  (`Cannot find module 'electron-squirrel-startup'`) — caught and fixed before the 0.2.4 stable
  promotion.

---

## [0.2.3] — 2026-07-20

### Dependencies
- fastapi: 0.139.0 → 0.139.2 (bug fixes)
- tailwindcss: 4.3.2 → 4.3.3 (patch fixes)
- @tailwindcss/vite: 4.3.2 → 4.3.3 (patch fixes)
- electron: 43.1.0 → 43.1.1 (patch fixes, Chromium/Node.js security updates)
- vite: 8.1.4 → 8.1.5 (patch fixes)

### Quality
- All tests passing: 47 passed, 1 skipped
- Frontend build verified with updated dependencies
- Electron 43.1.1 installation verified
- No regressions

---

## [0.2.2] — 2026-07-14

### Fixed
- **Self-hosted fonts** — Inter and JetBrains Mono are now bundled locally via `@fontsource`
  instead of being loaded from `fonts.googleapis.com`/`fonts.gstatic.com` at startup, removing
  the unsolicited outbound connection to Google's servers when the app launches

---

## [0.2.1] — 2026-07-14

### Dependencies
- electron: 43.0.0 → 43.1.0 (fixed crash on replacing an open application menu, Chromium 150.0.7871.47, Node.js 24.18.0)
- vite: 8.1.3 → 8.1.4 (oxc minifier preference for legacy builds, StackBlitz build workaround, SSR stacktrace alignment)
- uvicorn: 0.49.0 → 0.51.0 (near-zero-downtime worker reload on SIGHUP, removed colorama from standard extra)
- lucide-react: 1.23.0 → 1.24.0 (new icons, several upstream icon fixes)

### CI/CD
- **Beta/stable release pipeline**: `develop` pushes now build and publish a prerelease
  (`vX.Y.Z-beta.N`); `main` pushes promote the matching beta's assets to a stable release
  without rebuilding
- Beta version counter is now scoped per release version instead of the workflow's global
  run number, so the first beta for a new version starts at `beta.1`
- Windows installer renamed from the misleading `MetaLens Setup.exe` (implies a traditional
  installer) to `MetaLens-<version>-win-x64.exe`
- Fixed beta-suffix stripping on stable promotion to handle all 4 package naming conventions
  (`.exe`/`.tar.gz` use `-beta.N`, `.rpm` uses `betaN` with no separator, `.deb` uses `.beta.N`)
- Added `workflow_dispatch` trigger for manual pipeline runs

### Quality
- All tests passing: 47 passed, 1 skipped
- Frontend build and Electron startup verified with updated dependencies
- Beta/stable pipeline verified end-to-end: v0.2.1-beta.1, v0.2.1-beta.2, and the resulting
  v0.2.1 stable release all published with correct asset naming
- No regressions

---

## [0.2.0] — 2026-07-04

### Major Changes
- **Tailwind CSS v4 Upgrade**: Migrated from Tailwind v3 to v4 with new Vite plugin integration
  - Replaced @tailwindcss/postcss with @tailwindcss/vite for improved performance
  - Implemented CSS-first configuration using @theme directive
  - Custom color definitions now directly in globals.css
  - Removed tailwind.config.ts and postcss.config.js (no longer needed)
  - CSS build optimized with better tree-shaking and minification

### Dependencies
- **Build Tools**:
  - electron: 42.5.0 → 43.0.0 (Chromium 150, Node.js 24.17.0, new WebAuthn APIs, frameless window improvements)
  - tailwindcss: 3.4.19 → 4.3.2 (v4 architecture, improved CSS output)
  - @tailwindcss/vite: ⬆️ (new plugin for Vite integration, replaces @tailwindcss/postcss)
  - vite: 8.1.0 → 8.1.3 (patch fixes for build stability)
  - postcss: 8.5.15 → 8.5.16 (Input#origin positioning fixes)
- **Frontend**:
  - lucide-react: 1.21.0 → 1.23.0 (new icons, Astro v7 compatibility)
  - @tailwindcss/postcss: 4.3.1 → 4.3.2 (auto-rows/cols fixes, Windows CLI improvements)
  - @vitejs/plugin-react: 6.0.2 → 6.0.3 (React refresh improvements)
- **Python Backend**:
  - fastapi: 0.138.1 → 0.139.0 (new routing features)
  - pillow: 12.2.0 → 12.3.0 (image handling improvements)

### Performance
- Reduced configuration complexity: CSS-first approach eliminates need for TypeScript config
- Improved build times: Vite plugin integration optimized for faster rebuilds
- Custom color utility classes now correctly generated (56 classes: bg-cyber-*, text-cyber-*, border-cyber-*)
- CSS bundle optimized: fully included Tailwind utilities now properly minified

### Breaking Changes
- None for end users
- Internal: Configuration approach changed but UI/API remain identical
- Dev environment: Must use `npm run dev` instead of separate dev servers; Vite plugin handles CSS compilation

### Testing
- All Python tests passing: 47 passed, 1 skipped
- Electron 43 compatibility verified
- UI color palette fully preserved and validated
- CSS output tested: all custom colors generate correctly

### Notes
- First official release with Tailwind v4 stable build
- No changes to application API, features, or user-facing behavior
- Recommended upgrade for improved build performance and future Tailwind compatibility

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
  - `python/SECURITY.md`: Security design and implementation guide
  - `python/core/PATH_SECURITY_USAGE.md`: API reference for developers
  - *(internal threat-model notes were later removed from the repo as temporary development files — see [0.1.2])*

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

---

*← [Back to README](./README.md)*
