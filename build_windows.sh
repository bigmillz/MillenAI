#!/bin/zsh
# Builds Concorde-<version>-Windows.zip — the self-bootstrapping Windows
# package, including CUDA support on an NVIDIA machine.
#
#   ./build_windows.sh
#
# This runs on macOS on purpose. Nothing here is compiled: the package is
# millenai.py plus a .bat launcher and a README, so the output is identical
# whatever machine builds it. CUDA is not built either — Ollama's Windows
# amd64 build bundles the CUDA runtime, and the app downloads it on the
# user's PC and offloads to the GPU automatically.
#
# Replaces build_windows.ps1, which could only run on Windows. Keeping two
# copies of the launcher and README text would have guaranteed they drifted.
set -e
cd "$(dirname "$0")"

VER=$(python3 -c "import re;print(re.search(r'APP_VERSION = \"([^\"]+)\"',open('millenai.py').read()).group(1))")
[[ -n "$VER" ]] || { echo "could not read APP_VERSION from millenai.py"; exit 1; }
echo "version: $VER"

STAGE="build-win/MillenAI"
rm -rf build-win
mkdir -p "$STAGE"
cp millenai.py "$STAGE/"

# cmd.exe is unforgiving about bare LF in a .bat, so every line the launcher
# and readme emit is converted to CRLF on the way out.
crlf() { sed $'s/$/\r/' ; }

cat <<'BAT' | crlf > "$STAGE/Concorde.bat"
@echo off
setlocal
set "SUPPORT=%LOCALAPPDATA%\MillenAI"
set "VENV=%SUPPORT%\venv"
set "PY=%VENV%\Scripts\pythonw.exe"
set "PIP=%VENV%\Scripts\pip.exe"
if not exist "%SUPPORT%" mkdir "%SUPPORT%"

where python >nul 2>&1
if errorlevel 1 (
  echo Concorde needs Python 3.10 or newer.
  echo Install it from https://www.python.org/downloads/ ^(tick "Add to PATH"^)
  pause
  exit /b 1
)

if not exist "%PY%" (
  echo First run: setting up the AI engine. This takes a few minutes...
  python -m venv "%VENV%"
  "%PIP%" install --upgrade pip
  "%PIP%" install pywebview ddgs psutil faster-whisper
)

start "" "%PY%" "%~dp0millenai.py"
BAT

cat <<README | crlf > "$STAGE/README.txt"
Concorde $VER — local AI for Windows
=====================================

REQUIREMENTS
  * Windows 10/11, 64-bit
  * Python 3.10+ from python.org (tick "Add python.exe to PATH")
  * NVIDIA GPU recommended — see below.

INSTALL
  1. Unzip anywhere (e.g. Documents\Concorde)
  2. Double-click Concorde.bat
  3. First run installs the engine, then offers the models (~46 GB).
     SmartScreen may warn about an unknown publisher — choose
     "More info" then "Run anyway".

NVIDIA / CUDA
  Nothing to install and nothing to configure. Concorde downloads Ollama's
  Windows build, which bundles the CUDA runtime; Ollama detects the GPU and
  offloads to it on its own. Without an NVIDIA card everything still runs,
  just on the CPU and much slower.

WINDOWS ON ARM (Snapdragon / Surface / ARM VMs)
  Install the "Windows installer (64-bit)" — the x64 one, NOT ARM64.
  Two dependencies (pythonnet, which draws the window, and ctranslate2,
  which does voice input) ship x64 wheels only, so an ARM64 Python cannot
  install them. Windows 11 emulates the x64 build with no setup on your part.

  Only the app runs emulated — Concorde still fetches the native ARM64
  Ollama, so the models run at full speed. There is no CUDA on
  Windows-on-ARM, so inference is CPU-only on those machines.

Everything runs locally. No accounts, no cloud.
Models and settings live in %LOCALAPPDATA%\MillenAI
README

ZIP="Concorde-$VER-Windows.zip"
rm -f "$ZIP"
(cd build-win && zip -qr "../$ZIP" MillenAI)
rm -rf build-win

echo ""
echo "built $ZIP"
