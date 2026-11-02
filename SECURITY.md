# 🔒 Security Policy

## Security Model

MetaLens is designed with security-first principles:

- **Offline by default** — all metadata reading and writing happens on your machine. The only network access is the update check (see below), which runs only when you click *Check for updates* or have enabled the opt-in startup check
- **Localhost-only sidecar** — the Python (FastAPI) backend binds to `127.0.0.1` only, on a dynamically chosen port, never `0.0.0.0`
- **Electron hardening** — `nodeIntegration: false`, `contextIsolation: true`, `sandbox: true`; IPC accepts only the application's main frame. Navigation and popups are blocked and a Content Security Policy limits renderer resources
- **Authenticated sidecar** — a fresh random token is generated for each session, passed to Python through the environment and sent in an Authorization header. Requests without valid credentials are rejected; localhost binding alone is not treated as authentication
- **Atomic, verified writes** — every save (edits and deletions together) is prepared on a copy in a private temporary directory beside the target, read back and checked against the request, and checked for concurrent modification before the copy replaces the original (`ReplaceFileW` on Windows, preserving the security descriptor, alternate data streams and file attributes such as *hidden*; `os.replace` elsewhere). Any failure leaves the original content intact. The replacement is a new file, so hard links to the original are detached. A read-only file is refused (HTTP 403), not modified
- **Path validation** — a dedicated `core.path_security` module normalizes every path (resolves `~`, `..` and symbolic links) and checks that it is an existing, readable regular file or directory before any filesystem access, across all API routes. This is normalization, not a sandbox: an authenticated caller may name any file the user account can access, which is why every request must carry the session token
- **Hardened update check** — requests are made by the Electron main process, never by the renderer (whose CSP still only allows the local sidecar). It queries the GitHub Releases API of `overwrite00/MetaLens` only, and the renderer cannot supply URLs. Release and asset URLs must point to that repository, and the installer is downloaded only when you click *Download & install* (Windows installed builds), checked against its size and the SHA-256 digest published by GitHub, and discarded on any mismatch. Release notes are shown as plain text
- **Transparent code** — all handler and analysis logic is open-source and auditable

---

## 🛡️ Privacy Principles

### What We Don't Do

- ❌ No user accounts or authentication
- ❌ No telemetry or usage tracking
- ❌ No data collection or analytics
- ❌ No cloud processing or storage
- ❌ No automatic phone-home: the update check is manual, or opt-in (off by default) at startup

### Data Control

- ✅ Your files and their metadata never leave your machine
- ✅ Nothing is written anywhere except the file you explicitly edit (and its short-lived private temporary copy beside it, removed after the save)
- ✅ The undo stack and the sidecar's Undo journal (original values of the last 50 saves, at most 64 MiB) live in memory only — they are cleared when you close the app, never written to disk
- ✅ Full source code transparency

---

## 🔐 Technical Security

### Metadata Handlers

- Uses established libraries per format: Pillow (image properties), piexif tag tables plus MetaLens's own structure-preserving EXIF/XMP/IPTC codecs and JPEG segment writer (images), mutagen (audio/MP4), pypdf (PDF), Python's `zipfile`/`xml.etree` (Office Open XML packages), olefile (legacy Office), hachoir (read-only fallback for other formats). XMP packets (JPEG, PDF) containing DOCTYPE or entity declarations are refused
- Field capabilities are enforced by the API; client-provided editability flags cannot override them. Writable: JPEG EXIF/XMP/IPTC, audio tags (except AAC), MP4/MOV atoms, PDF document info, Office core properties, `fs:mtime` and Linux `user.` extended attributes. Read-only: other image formats and RAW, AAC, MKV/AVI and other non-MP4 video, encrypted or digitally signed PDFs, digitally signed Office packages (so a signature is never silently invalidated), Office `app.xml`/`custom.xml`, legacy Office, everything read through hachoir
- Every value is validated before the file is touched; library errors caused by a value are reported as validation errors (HTTP 422), never as crashes
- Filesystem access always goes through `validate_file_path`/`validate_directory_path` before touching disk

### IPC Boundary

- The Electron renderer talks to the OS only through the `preload.js` `contextBridge` surface (dialogs, path helpers, drive listing) — no direct filesystem or process access
- The renderer talks to the Python sidecar only over HTTP to `127.0.0.1:{dynamic_port}` (never a hardcoded port), with the port and session token obtained from the main process through the `get-sidecar-connection` IPC channel; the Content Security Policy only allows connections to `127.0.0.1`
- The sidecar rejects foreign browser origins and requires its session token, including for `/health`. The normal development path is Vite plus Electron; opening the frontend in a standalone browser does not grant sidecar access

---

## ⚠️ Known Limitations

### What MetaLens Does NOT Do

- **Code-signed installers** — releases are currently unsigned (no purchased certificate). Windows SmartScreen will warn on first run; this is expected, not a compromise indicator. See [Installation Guide](./docs/INSTALLATION.md) for how to proceed safely
- **Malware/content scanning** — MetaLens edits metadata; it does not scan file contents for malicious payloads
- **Encryption** — files and their metadata are stored and edited in plain form on disk, as with any local file manager
- **Guarantee against all data loss** — while writes are atomic, always keep backups of files you can't afford to lose

### Defense-in-Depth Recommended

For files handled outside MetaLens, also rely on your OS's built-in protections (Windows Defender / your Linux distro's package signing) and keep regular backups.

---

## 📢 Responsible Disclosure

If you discover a security vulnerability in MetaLens:

### Reporting Process

1. **Do not open a public GitHub issue** — this exposes the vulnerability before a fix ships
2. **Use GitHub Security Advisory**:
   - Navigate to: https://github.com/overwrite00/MetaLens/security/advisories
   - Click "Report a vulnerability"
   - Describe the vulnerability in detail, including reproduction steps

### Timeline

- **48 hours**: acknowledgment of your report
- **2 weeks** (critical): target fix timeline for critical vulnerabilities
- **30 days** (high): target fix timeline for high-severity vulnerabilities
- **Coordinated disclosure**: we'll work with you on a disclosure timeline

### Recognition

We appreciate responsible disclosure and will credit you in the security advisory (unless you prefer anonymity).

---

## 🔄 Security Updates

- **Subscribe to releases**: Watch the [Releases](https://github.com/overwrite00/MetaLens/releases) page
- **Check security advisories**: [GitHub Security Advisory](https://github.com/overwrite00/MetaLens/security/advisories)
- **Update regularly**: dependency security fixes are tracked in [CHANGELOG.md](./CHANGELOG.md)

---

## 📚 Additional Resources

- [Code of Conduct](./CODE_OF_CONDUCT.md) — community guidelines
- [Contributing Guide](./CONTRIBUTING.md) — how to contribute securely
- [REQUIREMENTS.md](./docs/REQUIREMENTS.md) — dependency information
- [CHANGELOG.md](./CHANGELOG.md) — version history and security fixes

---

*Last updated: 2026-11-02*
*← [Contributing](./CONTRIBUTING.md) | [Back to README →](./README.md)*
