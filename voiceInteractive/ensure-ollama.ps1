param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'config.json'),
    [int]$StartupTimeoutSeconds = 30
)

$ErrorActionPreference = 'Stop'

function Test-OllamaApi {
    param([string]$BaseUrl)
    try {
        $null = Invoke-RestMethod -Uri "$BaseUrl/api/tags" -Method Get -TimeoutSec 2
        return $true
    } catch {
        return $false
    }
}

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "找不到配置文件：$ConfigPath"
}

$Config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($Config.ollama_enabled -eq $false) {
    Write-Host 'Ollama：配置中已关闭，跳过启动检查。'
    return
}

$BaseUrl = ([string]$Config.ollama_url).TrimEnd('/')
if ([string]::IsNullOrWhiteSpace($BaseUrl)) {
    $BaseUrl = 'http://127.0.0.1:11434'
}
$ModelName = [string]$Config.ollama_model

if (-not (Test-OllamaApi -BaseUrl $BaseUrl)) {
    $OllamaUri = [Uri]$BaseUrl
    if (-not $OllamaUri.IsLoopback) {
        throw "远程 Ollama 服务不可用，无法在本机自动启动：$BaseUrl"
    }

    $OllamaCommand = Get-Command 'ollama.exe' -ErrorAction SilentlyContinue
    $OllamaExe = if ($OllamaCommand) {
        $OllamaCommand.Source
    } else {
        Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    }
    if (-not (Test-Path -LiteralPath $OllamaExe)) {
        throw '未找到 Ollama。请先安装 Ollama for Windows。'
    }

    Write-Host 'Ollama：服务未运行，正在后台启动……' -ForegroundColor Yellow
    Start-Process -FilePath $OllamaExe -ArgumentList @('serve') -WindowStyle Hidden | Out-Null

    $Deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    while ([DateTime]::UtcNow -lt $Deadline) {
        Start-Sleep -Milliseconds 500
        if (Test-OllamaApi -BaseUrl $BaseUrl) {
            break
        }
    }
    if (-not (Test-OllamaApi -BaseUrl $BaseUrl)) {
        throw "Ollama 在 $StartupTimeoutSeconds 秒内未能启动。请尝试手动运行：ollama serve"
    }
    Write-Host 'Ollama：后台服务已启动。' -ForegroundColor Green
} else {
    Write-Host 'Ollama：服务已运行。' -ForegroundColor Green
}

$Tags = Invoke-RestMethod -Uri "$BaseUrl/api/tags" -Method Get -TimeoutSec 5
$InstalledModels = @($Tags.models | ForEach-Object { [string]$_.name })
if (-not [string]::IsNullOrWhiteSpace($ModelName) -and $ModelName -notin $InstalledModels) {
    throw "尚未安装模型 $ModelName。请先运行：ollama pull $ModelName"
}
if (-not [string]::IsNullOrWhiteSpace($ModelName)) {
    Write-Host "Ollama：模型 $ModelName 已就绪。" -ForegroundColor Green
}
