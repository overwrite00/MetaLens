# 📋 System Requirements — MetaLens

Requirements for running the pre-built MetaLens application and, separately, for building it from source.

> [!TIP]
> 🚀 **Just want to use the app?** You don't need Python or Node.js — download a pre-built binary and skip straight to the [Installation Guide](./INSTALLATION.md).

---

## 📊 Quick Reference

| 📦 Use case | ✅ Requirement |
|---|---|
| **Run the compiled app** | Windows 10/11 (64-bit) or a Linux distro matching one of the [packages](#-linux) (Ubuntu 24.04+, Fedora, RHEL 9/10) — nothing else |
| **Build from source** | Python 3.13, Node.js 22.13+ (24 LTS recommended, used by CI), npm 10+ |

---

## 🖥️ Operating System Support

### 🪟 Windows
- **Minimum:** Windows 10 (64-bit)
- **Tested on:** Windows 10, Windows 11
- **Installer:** `MetaLens-<version>-win-x64.exe` (Squirrel.Windows)
- **Status:** ✅ Fully supported

### 🐧 Linux
- **Packages:** one set per distribution family, because the bundled sidecar needs a glibc at least as recent as the one it was built on:

  | Package | Built on | For |
  |---|---|---|
  | `…-ubuntu24.deb` / `…-ubuntu24.tar.gz` | Ubuntu 24.04 LTS (glibc 2.39) | Ubuntu 24.04+, and distros at least as recent (e.g. Debian 13) |
  | `…-ubuntu26.deb` / `…-ubuntu26.tar.gz` | Ubuntu 26.04 LTS (glibc 2.43) | Ubuntu 26.04+ |
  | `metalens-<version>-1.x86_64.rpm` | RHEL 9-compatible container (glibc 2.34) | Fedora (current releases), RHEL 9 / 10, Rocky, AlmaLinux |

- **Not packaged:** RHEL 8 and older (glibc older than 2.34)
- **Status:** ✅ Supported on the distributions above; not every distribution is tested

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
| **Internet connection** | MetaLens works fully offline — no cloud dependency, no telemetry |
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
> 🛡️ **Your files never leave your machine.** MetaLens has no network calls of any kind.

- ✅ **No cloud transmission** — all metadata reads/writes happen locally
- ✅ **No telemetry** — zero data collection or external tracking
- ✅ **Localhost-only sidecar** — the Python API binds to `127.0.0.1` only, never `0.0.0.0`
- ✅ **Open source** — full source code available for audit (MIT license)
- ✅ **Offline capable** — no internet connection required at any point

---

## 🚀 Next Steps

1. **🆕 New to MetaLens?** → [Installation Guide](./INSTALLATION.md)
2. **📖 Ready to browse files?** → [Usage Guide](./USAGE.md)
3. **💻 Developer?** → [API Reference](./API.md)

---

*Last updated: 2026-10-29*
*← [README](../README.md) | [Installation →](./INSTALLATION.md)*
