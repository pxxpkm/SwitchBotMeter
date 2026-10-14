# Build portable folder + zip for the desktop PC.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "==> Installing build tools..." -ForegroundColor Cyan
python -m pip install --upgrade pip | Out-Null
python -m pip install -r requirements.txt pyinstaller | Out-Null

$outName = "SwitchBotMeter"
$work = Join-Path $PSScriptRoot "build"
$dist = Join-Path $PSScriptRoot "dist"

Write-Host "==> Building with PyInstaller (onedir, windowed)..." -ForegroundColor Cyan

python -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --name $outName `
  --paths $PSScriptRoot `
  --collect-all bleak `
  --collect-all winotify `
  --collect-all pystray `
  --collect-all PIL `
  --collect-all winrt `
  --collect-submodules winrt `
  --hidden-import core `
  --hidden-import winrt `

  --distpath $dist `
  --workpath $work `
  --specpath $work `
  app.py

if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller failed"
}

$appDir = Join-Path $dist $outName
$dataDir = Join-Path $appDir "data"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

# Never ship a live history database or a machine-specific MAC.
Get-ChildItem -Path $appDir -Recurse -Filter "history.db*" -ErrorAction SilentlyContinue |
  Remove-Item -Force
$cfgDst = Join-Path $appDir "config.json"
python -c @"
from pathlib import Path
import json, sys
sys.path.insert(0, r'$PSScriptRoot')
from core import default_config
p = Path(r'$cfgDst')
p.write_text(json.dumps(default_config(), ensure_ascii=False, indent=2), encoding='utf-8')
print('wrote clean', p)
"@


$batPath = Join-Path $appDir "start.bat"
Set-Content -Path $batPath -Encoding ASCII -Value "@echo off`r`ncd /d `"%~dp0`"`r`nstart `"`" `"%~dp0$outName.exe`"`r`n"

$readmePath = Join-Path $appDir "README_PORTABLE.txt"
$readme = @(
  "SwitchBot Meter portable build"
  ""
  "How to use:"
  "1. Copy this whole folder to the desktop PC"
  "2. Double-click start.bat or SwitchBotMeter.exe"
  "3. Turn on Windows Bluetooth (BLE)"
  "4. Settings via UI write config.json next to the exe"
  ""
  "Notes:"
  "- Need real Bluetooth BLE (Intel/Realtek USB), not Xbox/Logitech Unifying/DJI"
  "- Keep config.json and data\ with the exe"
  "- If SmartScreen blocks: More info -> Run anyway"
)
$readme | Set-Content -Path $readmePath -Encoding UTF8

$zipPath = Join-Path $dist "SwitchBotMeter_Portable.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Write-Host "==> Creating zip: $zipPath" -ForegroundColor Cyan
Compress-Archive -Path $appDir -DestinationPath $zipPath -Force

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "  Folder: $appDir"
Write-Host "  Zip:    $zipPath"
