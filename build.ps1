# 配布用の exe を dist\SubCounter\ に作る（配布するときはこのフォルダごと zip にする）
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .venv-build)) { python -m venv .venv-build }
$py = ".venv-build\Scripts\python.exe"
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -r requirements.txt pyinstaller==6.22.3
if ($LASTEXITCODE) { exit $LASTEXITCODE }

# pystray は LGPL v3 なので、exe の中にまとめず、利用者が差し替えられるようにソースのまま _internal\pystray に置く
$pystray = & $py -c "import os, pystray; print(os.path.dirname(pystray.__file__))"
& $py -m PyInstaller --noconfirm --clean --onedir --noconsole --name SubCounter --add-data "web;web" `
    --exclude-module pystray --add-data "$pystray;pystray" `
    --hidden-import six --hidden-import queue --hidden-import ctypes.wintypes --hidden-import inspect `
    --hidden-import tempfile --collect-all tzdata server.py
if ($LASTEXITCODE) { exit $LASTEXITCODE }
Get-ChildItem dist\SubCounter\_internal\pystray -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force

Copy-Item docs\*.txt, docs\*.pdf dist\SubCounter\
& $py build_licenses.py dist\SubCounter
if ($LASTEXITCODE) { exit $LASTEXITCODE }

# build\ にも起動できない同名の exe ができるので、間違えて開かないように消す
Remove-Item build -Recurse -Force
