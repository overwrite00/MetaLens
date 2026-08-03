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

### 📦 Debian / Ubuntu (`.deb`)

```bash
wget https://github.com/overwrite00/MetaLens/releases/download/vX.Y.Z/MetaLens-vX.Y.Z-Linux.deb
sudo apt install ./MetaLens-vX.Y.Z-Linux.deb
```

### 📦 Fedora / RHEL (`.rpm`)

```bash
wget https://github.com/overwrite00/MetaLens/releases/download/vX.Y.Z/MetaLens-vX.Y.Z-Linux.rpm
sudo rpm -i MetaLens-vX.Y.Z-Linux.rpm
```

### 📦 Any distro (`.tar.gz`)

```bash
wget https://github.com/overwrite00/MetaLens/releases/download/vX.Y.Z/MetaLens-vX.Y.Z-Linux.tar.gz
tar -xzf MetaLens-vX.Y.Z-Linux.tar.gz
cd MetaLens-vX.Y.Z-Linux
./metalens
```

> [!NOTE]
> 📖 Replace `vX.Y.Z` with the version shown on the [Releases](../../../releases) page.

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

**Cause:** The Python sidecar failed to start (rare — usually a port conflict or antivirus blocking the bundled executable).

**Solution:** Restart the app. If it persists, check that no other process is holding the port MetaLens tries to bind on `127.0.0.1`, and confirm your antivirus isn't quarantining `metalens-sidecar.exe`.

---

### ❌ `pyinstaller` or `npm run make` fails while building from source

**Cause:** Wrong Python/Node version, or dependencies not installed in the right subfolder.

**Solution:** Confirm Python 3.13 and Node.js 20+ per [REQUIREMENTS.md](./REQUIREMENTS.md), and re-run `pip install -r requirements.txt` / `npm install` in the exact folders shown above.

---

## ✅ What's Next?

- **First time?** → Learn the interface in [USAGE.md](./USAGE.md)
- **Developer?** → [API.md](./API.md)

---

*Last updated: 2026-08-03*
*← [Requirements](./REQUIREMENTS.md) | [Usage →](./USAGE.md)*
