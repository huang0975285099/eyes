$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ProjectDir '.venv\Scripts\python.exe'
$PreviewUrl = 'http://localhost:8899/voice-preview.html'

chcp.com 65001 | Out-Null
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Test-Port([int]$Port) {
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "未找到虚拟环境：$VenvPython （请先运行 setup.ps1）"
}

# 1. Ollama 检查（ASR/页面不依赖，只有语音对话需要）
try {
    $null = Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3
    Write-Host '[OK] Ollama 已在运行' -ForegroundColor Green
} catch {
    Write-Host '[警告] Ollama 未启动，语音对话不可用（识别/播报不受影响）' -ForegroundColor Yellow
    Write-Host '       启动命令：ollama serve（或打开 Ollama 应用后重试）' -ForegroundColor Yellow
}

# 2. ASR 服务（Qwen3-ASR + 对话 + TTS，三合一）
if (Test-Port 8770) {
    Write-Host '[OK] ASR 服务已在运行 (8770)' -ForegroundColor Green
} else {
    Start-Process -FilePath $VenvPython -ArgumentList '-m','assistant.asr_server' -WorkingDirectory $ProjectDir
    Write-Host '[启动] ASR 服务 (8770)… 模型加载约 30 秒' -ForegroundColor Cyan
}

# 3. 静态页面服务
if (Test-Port 8899) {
    Write-Host '[OK] 页面服务已在运行 (8899)' -ForegroundColor Green
} else {
    Start-Process -FilePath $VenvPython -ArgumentList '-m','http.server','8899','--directory','web' -WorkingDirectory $ProjectDir
    Write-Host '[启动] 页面服务 (8899)' -ForegroundColor Cyan
}

# 4. 等 ASR 就绪（首次加载模型较慢）
if (-not (Test-Port 8770)) {
    Write-Host '等待 ASR 模型加载…' -ForegroundColor Cyan
    $Deadline = [DateTime]::Now.AddSeconds(90)
    while ([DateTime]::Now -lt $Deadline) {
        Start-Sleep -Milliseconds 1000
        if (Test-Port 8770) { break }
    }
    if (-not (Test-Port 8770)) {
        Write-Host '[警告] ASR 服务 90 秒内未就绪，请查看 ASR 窗口的报错' -ForegroundColor Yellow
    } else {
        Write-Host '[OK] ASR 模型加载完成' -ForegroundColor Green
    }
}

# 5. 打开浏览器
Start-Process $PreviewUrl
Write-Host ''
Write-Host "完成：$PreviewUrl 已在浏览器打开，点击麦克风开始对话。" -ForegroundColor Green
Write-Host '停止服务：运行 .\stop-voice.ps1（或在两个服务窗口按 Ctrl+C）'
