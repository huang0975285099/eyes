# 语音助手启动脚本
# 用法：
#   ./run.ps1                  启动语音助手（前台，Ctrl+C 退出）
#   ./run.ps1 --list-cameras   探测摄像头
#   ./run.ps1 --no-tray        不显示托盘
# 其余参数透传给 run.py（见 run.py / assistant.main.build_parser）

$ErrorActionPreference = 'Stop'

# 定位项目目录（脚本所在目录）
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

# 虚拟环境 Python 路径
$VenvPython = Join-Path $ProjectDir '.venv\Scripts\python.exe'

if (-not (Test-Path $VenvPython)) {
    Write-Host "未找到虚拟环境：$(Join-Path $ProjectDir '.venv')" -ForegroundColor Red
    Write-Host "请先创建并安装依赖：" -ForegroundColor Yellow
    Write-Host "  python -m venv .venv" -ForegroundColor Yellow
    Write-Host "  .\.venv\Scripts\Activate.ps1" -ForegroundColor Yellow
    Write-Host "  pip install -r requirements.txt" -ForegroundColor Yellow
    exit 1
}

Write-Host "正在启动语音助手……（Ctrl+C 退出）" -ForegroundColor Green
& $VenvPython (Join-Path $ProjectDir 'run.py') @args
