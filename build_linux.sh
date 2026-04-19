#!/usr/bin/env bash
set -euo pipefail

echo "============================================"
echo " MetaLens — Linux Build Script"
echo "============================================"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 1. Python sidecar
echo ""
echo "[1/3] Building Python sidecar..."
cd python
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -q
pyinstaller main.py --onefile --name metalens-sidecar --distpath dist --clean --noconfirm \
  --hidden-import piexif \
  --hidden-import mutagen \
  --hidden-import hachoir \
  --hidden-import olefile \
  --hidden-import openpyxl \
  --hidden-import docx \
  --hidden-import pptx \
  --hidden-import pypdf
deactivate
echo "   Sidecar: python/dist/metalens-sidecar"
cd "$SCRIPT_DIR"

# 2. React frontend
echo ""
echo "[2/3] Building React frontend..."
cd frontend
npm install --silent
npm run build
echo "   Frontend: frontend/dist/"
cd "$SCRIPT_DIR"

# 3. Electron Forge
echo ""
echo "[3/3] Packaging with Electron Forge..."
cd electron
npm install --silent
npx electron-forge make --platform=linux
echo "   Output: electron/out/"
cd "$SCRIPT_DIR"

echo ""
echo "============================================"
echo " Build complete! Output in electron/out/"
echo "============================================"
