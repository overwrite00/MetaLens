# 🔒 Security Policy

## Security Model

MetaLens is designed with security-first principles:

- **Offline by default** — all metadata reading and writing happens on your machine. The only network access is the update check (see below), which runs only when you click *Check for updates* or have enabled the opt-in startup check
- **Localhost-only sidecar** — the Python (FastAPI) backend binds to `127.0.0.1` only, on a dynamically chosen port, never `0.0.0.0`
- **Electron hardening** — `nodeIntegration: false`, `contextIsolation: true`, `sandbox: true`; IPC accepts only the application's main frame. Navigation and popups are blocked and a Content Security Policy limits renderer resources
- **Authenticated sidecar** — a fresh random token is generated for each session, passed to Python through the environment and sent in an Authorization header. Requests without valid credentials are rejected; localhost binding alone is not treated as authentication
- **Atomic writes** — changes are prepared in a private temporary directory beside the target, with checks for concurrent modification before replacement. Audio and video use the same mechanism. Writer failures leave the original content intact
- **Path validation** — a dedicated `core.path_security` module validates and normalizes every path (resolves symlinks, blocks traversal) before any filesystem access, across all API routes
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
- ✅ Nothing is written anywhere except the file you explicitly edit
- ✅ The undo stack lives in memory only — it is cleared when you close the app, never written to disk
- ✅ Full source code transparency

---

## 🔐 Technical Security

### Metadata Handlers

- Uses established, audited libraries per format: Pillow/piexif (images), mutagen (audio/video), pypdf (PDF), python-docx/openpyxl/python-pptx (Office), olefile (legacy Office), hachoir (fallback)
- Field capabilities are enforced by the API; client-provided editability flags cannot override them. Image metadata outside supported JPEG EXIF, encrypted PDFs, digitally signed Office packages, legacy Office and MKV/AVI are read-only. Writable filesystem fields are handled separately
- Filesystem access always goes through `validate_file_path`/`validate_directory_path` before touching disk

### IPC Boundary

- The Electron renderer talks to the OS only through the `preload.js` `contextBridge` surface (dialogs, path helpers, drive listing) — no direct filesystem or process access
- The renderer talks to the Python sidecar only over HTTP to `127.0.0.1:{dynamic_port}`, never a hardcoded port
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

*Last updated: 2026-10-31*
*← [Contributing](./CONTRIBUTING.md) | [Back to README →](./README.md)*
