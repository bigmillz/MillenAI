# Builds a real Windows app: MillenAI.exe, and an installer if Inno Setup is
# present. Run this ON Windows - PyInstaller cannot cross-compile, which is
# why this is the one build that can't be done from the Mac.
#
#   powershell -ExecutionPolicy Bypass -File build_windows_exe.ps1
#
# Produces:
#   dist\MillenAI\MillenAI.exe              the app (no Python needed to run)
#   MillenAI-<ver>-Setup.exe                installer, if ISCC.exe is found
#   MillenAI-<ver>-Windows-exe.zip          otherwise, a zip of the folder
#
# ON A WINDOWS-ON-ARM VM: run this under an *x64* Python. PyInstaller bakes
# in whatever architecture built it, so an ARM64 Python yields an exe that
# only runs on ARM machines - and those have no CUDA. Building under emulated
# x64 gives the x64 exe an NVIDIA machine actually needs. The script checks
# and stops if you get this wrong.
#
# KEEP THIS FILE PURE ASCII. Windows PowerShell 5.1 reads a .ps1 with no BOM
# as the ANSI codepage, not UTF-8. A UTF-8 em-dash arrives as three CP1252
# characters, the last of which is U+201D - a curly double quote, which
# PowerShell honours as a string delimiter. One dash in a comment silently
# desyncs the quoting for the rest of the file.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# ---------------------------------------------------------------- preflight
# Windows ships a stub python.exe under WindowsApps that only prints an advert
# for the Microsoft Store. It is on PATH even when Python is not installed, so
# resolve a real interpreter rather than trusting the name.
function Get-PyArch([string]$exe) {
  try {
    $a = & $exe -c "import platform;print(platform.machine())" 2>$null
    if ($LASTEXITCODE -eq 0) { return ("" + $a).Trim() }
  } catch { }
  return $null
}

# Collect every interpreter on the machine, then pick an x64 one. Testing
# only the first thing found is what went wrong before: on an ARM box the
# py.exe launcher is itself ARM64, so it answered for the whole machine and
# hid a perfectly good x64 install sitting next to it.
function Find-Python {
  $cands = New-Object System.Collections.Generic.List[string]

  foreach ($name in @("python", "python3")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -and $cmd.Source -notlike "*\WindowsApps\*") {
      $cands.Add($cmd.Source)
    }
  }
  # the launcher can enumerate every registered interpreter for us
  $launcher = Get-Command py -ErrorAction SilentlyContinue
  if ($launcher) {
    foreach ($line in (& $launcher.Source -0p 2>$null)) {
      if ("$line" -match '([A-Za-z]:\\[^\r\n]*python\.exe)') { $cands.Add($matches[1]) }
    }
  }
  # and the standard install locations, since a shell opened before the
  # installer ran captured the old PATH and will never see the new entry
  foreach ($g in @(
      "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
      "$env:ProgramFiles\Python3*\python.exe",
      "${env:ProgramFiles(x86)}\Python3*\python.exe",
      "C:\Python3*\python.exe")) {
    foreach ($f in (Get-ChildItem -Path $g -ErrorAction SilentlyContinue)) {
      $cands.Add($f.FullName)
    }
  }

  $seen = @{}
  $script:PyReport = @()
  foreach ($exe in $cands) {
    if (-not (Test-Path $exe)) { continue }
    $key = $exe.ToLower()
    if ($seen.ContainsKey($key)) { continue }
    $seen[$key] = $true
    $arch = Get-PyArch $exe
    $script:PyReport += ("    {0,-8} {1}" -f ($arch, $exe))
    if ($arch -match 'AMD64|x86_64') { return $exe }
  }
  return $null
}

$py = Find-Python
if (-not $py) {
  Write-Host ""
  Write-Host "  Python is not installed." -ForegroundColor Yellow
  Write-Host ""
  Write-Host "  Get it from  https://www.python.org/downloads/windows/"
  Write-Host "  Choose 'Windows installer (64-bit)' - the x64 one."
  Write-Host "  Do NOT choose ARM64: PyInstaller would then build an exe that"
  Write-Host "  only runs on ARM machines, and those have no CUDA."
  Write-Host ""
  Write-Host "  Tick 'Add python.exe to PATH' in the installer."
  Write-Host ""
  Write-Host "  ALREADY INSTALLED IT? Close this window and open a new one."
  Write-Host "  A shell captures PATH when it starts, so one opened before the"
  Write-Host "  installer ran will never see Python no matter how often you retry."
  Write-Host ""
  Write-Host "  If it still is not found afterwards, turn off the Store stubs:"
  Write-Host "  Settings > Apps > Advanced app settings > App execution aliases"
  Write-Host "  -> switch off python.exe and python3.exe."
  throw "no Python interpreter found"
}
Write-Host "python: $py"

$arch = & $py -c "import platform;print(platform.machine())"
Write-Host "python architecture: $arch"
if ($arch -notmatch 'AMD64|x86_64') {
  Write-Host ""
  Write-Host "  STOP - this Python is $arch, not x64." -ForegroundColor Yellow
  Write-Host "  The exe it builds would run only on ARM machines, which have no CUDA."
  Write-Host "  Install the x64 python.org build and run this again with that one."
  Write-Host "  (Windows 11 runs it under emulation; nothing else to set up.)"
  throw "need an x64 Python"
}

$ver = (Select-String -Path millenai.py -Pattern 'APP_VERSION = "([^"]+)"').Matches[0].Groups[1].Value
Write-Host "version: $ver"

# ------------------------------------------------------------------- venv
$bv = ".build-venv"
if (-not (Test-Path "$bv\Scripts\python.exe")) {
  Write-Host "-> creating build venv"
  & $py -m venv $bv
}
$bpy = "$bv\Scripts\python.exe"
Write-Host "-> installing build dependencies (a few minutes the first time)"
& $bpy -m pip install --upgrade pip | Out-Null
& $bpy -m pip install pyinstaller pywebview ddgs psutil huggingface_hub faster-whisper | Out-Null
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# ------------------------------------------------------------------- build
if (Test-Path "build") { Remove-Item -Recurse -Force "build" }
if (Test-Path "dist")  { Remove-Item -Recurse -Force "dist"  }

# onedir, not onefile: pywebview loads native WebView2 pieces at runtime and
# is markedly more reliable unpacked. The installer hides the folder anyway.
# NB: not named $args - that is an automatic variable in PowerShell.
$pyiArgs = @(
  "--noconfirm", "--clean", "--windowed", "--onedir",
  "--name", "MillenAI",
  "--icon", "MillenAI.ico",
  # pywebview resolves its backend dynamically, so PyInstaller cannot see it
  "--hidden-import", "webview.platforms.edgechromium",
  "--hidden-import", "clr",
  "--collect-all", "webview",
  "millenai.py"
)
Write-Host "-> running PyInstaller"
& $bpy -m PyInstaller @pyiArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
if (-not (Test-Path "dist\MillenAI\MillenAI.exe")) { throw "no exe was produced" }
Write-Host "-> built dist\MillenAI\MillenAI.exe"

# --------------------------------------------------------------- installer
$iscc = @(
  "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
  "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($iscc) {
  Write-Host "-> found Inno Setup, building the installer"
  $iss = @"
[Setup]
AppName=MillenAI
AppVersion=$ver
DefaultDirName={autopf}\MillenAI
DefaultGroupName=MillenAI
OutputBaseFilename=MillenAI-$ver-Setup
OutputDir=.
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=MillenAI.ico
UninstallDisplayIcon={app}\MillenAI.exe
PrivilegesRequired=lowest

[Files]
Source: "dist\MillenAI\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\MillenAI"; Filename: "{app}\MillenAI.exe"
Name: "{autodesktop}\MillenAI"; Filename: "{app}\MillenAI.exe"

[Run]
Filename: "{app}\MillenAI.exe"; Description: "Launch MillenAI"; Flags: nowait postinstall skipifsilent
"@
  Set-Content -Encoding UTF8 "MillenAI.iss" $iss
  & $iscc "MillenAI.iss"
  if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
  Write-Host ""
  Write-Host "built MillenAI-$ver-Setup.exe" -ForegroundColor Green
} else {
  $zip = "MillenAI-$ver-Windows-exe.zip"
  if (Test-Path $zip) { Remove-Item $zip }
  Compress-Archive -Path "dist\MillenAI" -DestinationPath $zip
  Write-Host ""
  Write-Host "built $zip" -ForegroundColor Green
  Write-Host "(install Inno Setup 6 for a single-file installer instead:"
  Write-Host "  winget install JRSoftware.InnoSetup )"
}

Write-Host ""
Write-Host "Models are NOT bundled - the app downloads them on first run,"
Write-Host "and pulls Ollama's CUDA build automatically on an NVIDIA machine."
