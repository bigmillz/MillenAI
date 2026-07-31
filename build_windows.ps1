# Builds MillenAI-Windows.zip — a self-bootstrapping Windows package.
#
#   powershell -ExecutionPolicy Bypass -File build_windows.ps1
#
# Mirrors the macOS design: the zip is tiny (just the script and a launcher),
# and on first run the launcher creates a private venv, installs the engine,
# and the app downloads Ollama + models itself. On an NVIDIA box Ollama uses
# CUDA automatically.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$ver = (Select-String -Path millenai.py -Pattern 'APP_VERSION = "([^"]+)"').Matches[0].Groups[1].Value
Write-Host "version: $ver"

$stage = "build-win\MillenAI"
if (Test-Path "build-win") { Remove-Item -Recurse -Force "build-win" }
New-Item -ItemType Directory -Path $stage -Force | Out-Null
Copy-Item millenai.py $stage

# --- launcher: creates the venv on first run, then starts the app windowless
@'
@echo off
setlocal
set "SUPPORT=%LOCALAPPDATA%\MillenAI"
set "VENV=%SUPPORT%\venv"
set "PY=%VENV%\Scripts\pythonw.exe"
set "PIP=%VENV%\Scripts\pip.exe"
if not exist "%SUPPORT%" mkdir "%SUPPORT%"

where python >nul 2>&1
if errorlevel 1 (
  echo MillenAI needs Python 3.10 or newer.
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
'@ | Set-Content -Encoding ASCII "$stage\MillenAI.bat"

@"
MillenAI $ver — local AI for Windows
=====================================

REQUIREMENTS
  * Windows 10/11, 64-bit
  * Python 3.10+ from python.org (tick "Add python.exe to PATH")
  * NVIDIA GPU strongly recommended — Ollama uses CUDA automatically.
    It runs on CPU without one, just slowly.

INSTALL
  1. Unzip anywhere (e.g. Documents\MillenAI)
  2. Double-click MillenAI.bat
  3. First run installs the engine, then offers the models (~46 GB).
     SmartScreen may warn about an unknown publisher — choose
     "More info" then "Run anyway".

Everything runs locally. No accounts, no cloud.
Models and settings live in %LOCALAPPDATA%\MillenAI
"@ | Set-Content -Encoding UTF8 "$stage\README.txt"

$zip = "MillenAI-$ver-Windows.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path "build-win\MillenAI" -DestinationPath $zip
Remove-Item -Recurse -Force "build-win"

Write-Host ""
Write-Host "built $zip"
