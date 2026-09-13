$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ProjectDir '.venv\Scripts\python.exe'

Set-Location -LiteralPath $ProjectDir
$env:PYTHONUTF8 = '1'

if (-not (Test-Path -LiteralPath $VenvPython)) {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        uv venv --python 3.12 --seed
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        py -3.12 -m venv .venv
    } else {
        python -m venv .venv
    }
}

if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv pip install --python $VenvPython -r requirements.txt
} else {
    & $VenvPython -m pip install -r requirements.txt
}
& $VenvPython voice_assistant.py --download-model

Write-Host ''
Write-Host '安装完成。运行 .\run.ps1 启动语音助手。' -ForegroundColor Green
