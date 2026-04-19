# MetaLens

![Version](https://img.shields.io/badge/version-0.1.0-cyan)
![License](https://img.shields.io/badge/license-MIT-blue)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey)

**Universal File Metadata Manager** — read, edit, delete and compare metadata across all major file formats.

---

## Features

- **File-manager UI**: folder tree + file list + metadata detail panel
- **All major formats**: images (JPEG/PNG/TIFF/WebP/RAW), audio (MP3/FLAC/OGG/M4A/WAV), video (MP4/MKV/MOV), documents (PDF/DOCX/XLSX/PPTX/DOC/XLS), any file (filesystem metadata)
- **Full operations**: read, edit, delete individual fields, compare/diff two files
- **Atomic writes**: temp-file + rename — no file corruption on failure
- **Undo stack**: 50 operations per session
- **Cyber dark theme**: professional dark UI with cyan/blue accents
- **Compiled binaries**: no Python or Node.js required to run

---

## Download

See [Releases](../../releases) for pre-built binaries:

| Platform | Download |
|---|---|
| Windows 11 | `MetaLens-vX.Y.Z-Setup-Windows.exe` |
| Linux (deb) | `MetaLens-vX.Y.Z-Linux.deb` |
| Linux (rpm) | `MetaLens-vX.Y.Z-Linux.rpm` |
| Linux (tar) | `MetaLens-vX.Y.Z-Linux.tar.gz` |

---

## Build from Source

### Prerequisites
- Python 3.11–3.13
- Node.js 20+
- npm 10+

### Steps

```bash
# 1. Clone the repository
git clone https://github.com/YOUR_USERNAME/MetaLens.git
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
cd python && pyinstaller main.py --onefile --name metalens-sidecar
cd ../frontend && npm run build
cd ../electron && npm run make
```

---

## Supported Formats

| Category | Formats | Read | Write |
|---|---|---|---|
| Images | JPEG, PNG, TIFF, BMP, GIF, WebP | ✅ | ✅ |
| Images (RAW) | CR2, NEF, ARW, DNG | ✅ | — |
| Audio | MP3, FLAC, OGG, M4A, WAV, AIFF, WMA, APE, Opus | ✅ | ✅ |
| Video | MP4, MOV | ✅ | ✅ |
| Video | MKV, AVI | ✅ | — |
| Documents | PDF | ✅ | ✅ |
| Office | DOCX, XLSX, PPTX | ✅ | ✅ |
| Office (legacy) | DOC, XLS, PPT | ✅ | — |
| Any file | Filesystem timestamps, permissions, xattr | ✅ | ✅ |
| Other | Hundreds of formats via hachoir | ✅ | — |

---

## Architecture

```
Electron Main (Node.js)
    ├── Spawns Python sidecar (FastAPI, localhost only)
    ├── Manages window, native menus, file dialogs
    └── IPC bridge (contextBridge) ↔ React renderer

Python Sidecar (FastAPI)
    ├── /list   — directory listing with handler detection
    ├── /read   — full metadata extraction
    ├── /write  — atomic metadata write-back
    ├── /delete — field removal
    └── /diff   — metadata comparison

React Frontend (Vite + TailwindCSS)
    ├── FolderPanel  — folder tree navigation
    ├── FilePanel    — file list with metadata summary
    └── DetailPanel  — View / Edit / Diff tabs
```

---

## License

MIT License — © 2026 Graziano Mariella

See [LICENSE](LICENSE) for full text.

---

## Credits

Developed by **Graziano Mariella**.
