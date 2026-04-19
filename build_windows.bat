@echo off
setlocal enabledelayedexpansion

echo ============================================
echo  MetaLens — Windows Build Script
echo ============================================

:: 1. Build Python sidecar
echo.
echo [1/3] Building Python sidecar...
cd python
python -m pip install -r requirements.txt -q
python -m PyInstaller main.py --onefile --name metalens-sidecar --distpath dist --clean --noconfirm ^
  --hidden-import piexif ^
  --hidden-import mutagen ^
  --hidden-import hachoir ^
  --hidden-import olefile ^
  --hidden-import openpyxl ^
  --hidden-import docx ^
  --hidden-import pptx ^
  --hidden-import pypdf
if errorlevel 1 ( echo ERROR: PyInstaller failed & exit /b 1 )
echo    Sidecar: python\dist\metalens-sidecar.exe

:: 2. Build React frontend
echo.
echo [2/3] Building React frontend...
cd ..\frontend
call npm install --silent
call npm run build
if errorlevel 1 ( echo ERROR: npm build failed & exit /b 1 )
echo    Frontend: frontend\dist\

:: 3. Package with Electron Forge
echo.
echo [3/3] Packaging with Electron Forge...
cd ..\electron
call npm install --silent
call npx electron-forge make --platform=win32
if errorlevel 1 ( echo ERROR: Electron Forge failed & exit /b 1 )

echo.
echo ============================================
echo  Build complete! Output in electron\out\
echo ============================================
