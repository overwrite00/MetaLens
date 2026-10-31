# 📋 System Requirements — MetaLens

Requirements for running the pre-built MetaLens application and, separately, for building it from source.

> [!TIP]
> 🚀 **Just want to use the app?** You don't need Python or Node.js — download a pre-built binary and skip straight to the [Installation Guide](./INSTALLATION.md).

---

## 📊 Quick Reference

| 📦 Use case | ✅ Requirement |
|---|---|
| **Run the compiled app** | Windows 10/11 (64-bit) or a Linux distro with glibc 2.34+ (Ubuntu 22.04+, Debian 12+, Fedora, RHEL 9/10) — nothing else |
| **Build from source** | Python 3.13, Node.js 22.13+ (24 LTS recommended, used by CI), npm 10+ |

---

## 🖥️ Operating System Support

### 🪟 Windows
- **Minimum:** Windows 10 (64-bit)
- **Tested on:** Windows 10, Windows 11
- **Installer:** `MetaLens-<version>-win-x64.exe` (Squirrel.Windows)
- **Status:** ✅ Fully supported

### 🐧 Linux
- **Packages:** `.deb` (Debian/Ubuntu), `.rpm` (Fedora/RHEL), `.tar.gz` (any distro) — one set for all distributions
- **Minimum C library:** glibc 2.34 — the bundled sidecar is built on RHEL 9. This covers Ubuntu 22.04+, Debian 12+, RHEL 9 / 10 (Rocky, AlmaLinux) and current Fedora
- **Not supported:** RHEL 8 and older (glibc older than 2.34)
- **Tested:** installing each package and starting the sidecar (not the full desktop window) was verified in containers on Ubuntu 22.04 / 24.04 / 26.04, Debian 12 / 13, Fedora 43 / 44, Rocky Linux 9 and AlmaLinux 10 (`.tar.gz` on Debian 12, Ubuntu 22.04 and Fedora 44)

### 🍎 macOS
- **Status:** ❌ Not currently packaged — no macOS build is produced by CI. Building from source with `npm run make` is possible but unverified.

---

## 💻 Hardware Requirements

| 🔧 Resource | 📊 Requirement |
|---|---|
| **Processor** | Any modern 64-bit CPU |
| **RAM** | 512 MB free (Electron + Python sidecar) |
| **Storage** | ~300 MB for the installed application |

---

## ❌ What You DON'T Need

| ❌ | ℹ️ Why you don't need it |
|---|---|
| **Python** | The sidecar ships as a compiled binary (`metalens-sidecar[.exe]`) inside the installer |
| **Node.js** | The frontend is pre-built and bundled into the Electron app |
| **Internet connection** | MetaLens works fully offline — no cloud dependency, no telemetry. A connection is only used for the optional update check |
| **Admin/root rights** | Windows installer runs per-user (Squirrel); Linux packages install standard desktop entries |

---

## 🛠️ Building From Source

Only needed if you want to modify MetaLens or package it yourself.

| 📦 Tool | ✅ Version | 📝 Notes |
|---|---|---|
| **Python** | 3.13 | Used to run/package the FastAPI sidecar |
| **Node.js** | 20+ | Runs Electron and the Vite build |
| **npm** | 10+ | Installs both `electron/` and `frontend/` dependencies |

See [Installation Guide → Build from Source](./INSTALLATION.md#-build-from-source) for exact steps.

---

## 🔒 Data Privacy & Security

> [!IMPORTANT]
> 🛡️ **Your files never leave your machine.** The only network call MetaLens can make is the update check against GitHub Releases, and only when you ask for it or opt in.

- ✅ **No cloud transmission** — all metadata reads/writes happen locally
- ✅ **No telemetry** — zero data collection or external tracking
- ✅ **Localhost-only sidecar** — the Python API binds to `127.0.0.1` only, never `0.0.0.0`
- ✅ **Open source** — full source code available for audit (MIT license)
- ✅ **Offline capable** — no internet connection required, except to check for or download a new version

---

## 🚀 Next Steps

1. **🆕 New to MetaLens?** → [Installation Guide](./INSTALLATION.md)
2. **📖 Ready to browse files?** → [Usage Guide](./USAGE.md)
3. **💻 Developer?** → [API Reference](./API.md)

---

*Last updated: 2026-10-31*
*← [README](../README.md) | [Installation →](./INSTALLATION.md)*
