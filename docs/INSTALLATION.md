# 🚀 Installation Guide — MetaLens

Step-by-step instructions for installing MetaLens on Windows and Linux, plus building it from source.

> [!IMPORTANT]
> 💻 **Prerequisites:** Review [REQUIREMENTS.md](./REQUIREMENTS.md) before starting.

---

## 📋 Table of Contents

1. [🪟 Windows Installation](#-windows-installation)
2. [🐧 Linux Installation](#-linux-installation)
3. [✅ Verify Installation](#-verify-installation)
4. [🛠️ Build from Source](#-build-from-source)
5. [🔧 Troubleshooting](#-troubleshooting)

---

## 🪟 Windows Installation

1. Go to the [Releases](../../../releases) page
2. Download **`MetaLens-vX.Y.Z-Setup-Windows.exe`** (latest version)
3. Double-click the downloaded file

> [!WARNING]
> ⚠️ **Installers are not code-signed.** Windows SmartScreen will show "Windows protected your PC". Click **"More info"** → **"Run anyway"** to proceed. This is expected for an unsigned open-source build — see [SECURITY.md](../SECURITY.md) for details.

4. The installer (Squirrel.Windows) runs per-user — no admin rights required
5. MetaLens launches automatically after installation and adds a Start Menu shortcut

---

## 🐧 Linux Installation

One set of packages covers every supported distribution: the bundled Python sidecar is built on a
RHEL 9 baseline (glibc 2.34), so it runs on any distribution with **glibc 2.34 or newer** —
Ubuntu 22.04 and newer, Debian 12 and newer, RHEL 9 / 10 and compatible (Rocky, AlmaLinux) and
current Fedora releases.

### 📦 Debian / Ubuntu (`.deb`)

```bash
wget https://github.com/overwrite00/MetaLens/releases/download/vX.Y.Z/metalens_X.Y.Z_amd64.deb
sudo apt install ./metalens_X.Y.Z_amd64.deb
```

### 📦 Fedora / RHEL (`.rpm`)

```bash
wget https://github.com/overwrite00/MetaLens/releases/download/vX.Y.Z/metalens-X.Y.Z-1.x86_64.rpm
sudo dnf install ./metalens-X.Y.Z-1.x86_64.rpm
```

### 📦 Any distro (`.tar.gz`)

```bash
wget https://github.com/overwrite00/MetaLens/releases/download/vX.Y.Z/MetaLens-X.Y.Z-linux-x64.tar.gz
tar -xzf MetaLens-X.Y.Z-linux-x64.tar.gz
cd MetaLens-linux-x64
./metalens
```

> [!NOTE]
> 📖 Replace `X.Y.Z` with the version shown on the [Releases](../../../releases) page. Beta
> releases are named `X.Y.Z-beta.N` (the `.deb` uses `X.Y.Z.beta.N` and the `.rpm` `X.Y.Zbeta.N`);
> copy the exact file name from the release page.

---

## ✅ Verify Installation

Launch MetaLens, then open **Help → About** (or the About dialog) to confirm the version number matches the one you installed — it's read live from the sidecar's `/health` endpoint.

You should see:
- ✅ A folder tree on the left (drives and home directory)
- ✅ An empty file list until you select a folder
- ✅ No errors in the window

---

## 🛠️ Build from Source

Only needed for development or if no pre-built binary exists for your platform.

```bash
# 1. Clone the repository
git clone https://github.com/overwrite00/MetaLens.git
cd MetaLens

# 2. Install Python dependencies
cd python
python -m venv .venv
source .venv/bin/activate        # Linux
# .venv\Scripts\activate         # Windows
pip install -r requirements.txt

# 3. Install Node dependencies
cd ../electron && npm install
cd ../frontend && npm install

# 4. Run in development mode
cd ../electron && npm start

# 5. Build for production
cd ../python && pyinstaller main.py --onefile --name metalens-sidecar
cd ../frontend && npm run build
cd ../electron && npm run make
```

`npm run make` (Electron Forge) produces the Windows `.exe`, Linux `.deb`/`.rpm` under `electron/out/make/`.

> [!NOTE]
> The sidecar embeds the glibc of the machine that builds it, so a locally built package only runs
> on systems at least as recent as yours (the release pipeline builds it on RHEL 9 for that reason). To build
> the `.rpm` you need `rpmbuild` (`rpm-build`); `electron-installer-redhat` 4.0.0 fails with RPM 4.20 or newer
> (including RPM 6), so build it on a RHEL 9-compatible system or container.

---

## 🔧 Troubleshooting

### ❌ "Windows protected your PC" (SmartScreen)

**Cause:** The installer is not code-signed (no certificate purchased for this open-source project).

**Solution:** Click **"More info"** → **"Run anyway"**. Verify you downloaded from the official [Releases](../../../releases) page if in doubt.

---

### ❌ `.deb`/`.rpm` install fails with dependency errors

**Cause:** Missing system libraries used by Electron (e.g. `libgtk-3-0`, `libnotify4`).

**Solution:**
```bash
sudo apt --fix-broken install   # Debian/Ubuntu, after a failed .deb install
```

---

### ❌ App window opens but the file list never loads

**Cause:** The Python sidecar failed to start (rare — usually a port conflict or antivirus blocking the bundled executable). On Linux it can also be the wrong package for your distribution: the sidecar needs a glibc at least as recent as the one it was built on, and fails with `version 'GLIBC_2.xx' not found` otherwise.

**Solution:** Restart the app. On Linux, launch `metalens` from a terminal to see the sidecar's error message, and check that your distribution ships glibc 2.34 or newer (`ldd --version`). If it persists, check that no other process is holding the port MetaLens tries to bind on `127.0.0.1`, and confirm your antivirus isn't quarantining `metalens-sidecar.exe`.

---

### ❌ `pyinstaller` or `npm run make` fails while building from source

**Cause:** Wrong Python/Node version, or dependencies not installed in the right subfolder.

**Solution:** Confirm Python 3.13 and Node.js 22.13+ (24 LTS recommended) per [REQUIREMENTS.md](./REQUIREMENTS.md), and re-run `pip install -r requirements.txt` / `npm install` in the exact folders shown above.

---

## ✅ What's Next?

- **First time?** → Learn the interface in [USAGE.md](./USAGE.md)
- **Developer?** → [API.md](./API.md)

---

*Last updated: 2026-10-29*
*← [Requirements](./REQUIREMENTS.md) | [Usage →](./USAGE.md)*
