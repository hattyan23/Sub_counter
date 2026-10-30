# 配布用の exe を dist\YouTubeSubCounter\ に作る（配布するときはこのフォルダごと zip にする）
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .venv-build)) { python -m venv .venv-build }
$py = ".venv-build\Scripts\python.exe"
& $py -m pip install --quiet -r requirements.txt pyinstaller
if ($LASTEXITCODE) { exit $LASTEXITCODE }

& $py -m PyInstaller --noconfirm --clean --onedir --noconsole --name YouTubeSubCounter --add-data "web;web" --hidden-import pystray._win32 server.py
if ($LASTEXITCODE) { exit $LASTEXITCODE }

Copy-Item docs\*.txt dist\YouTubeSubCounter\

# build\ にも起動できない同名の exe ができるので、間違えて開かないように消す
Remove-Item build -Recurse -Force
