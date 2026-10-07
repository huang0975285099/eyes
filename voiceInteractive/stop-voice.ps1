$ErrorActionPreference = 'Continue'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path

chcp.com 65001 | Out-Null
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

# 停止 ASR 服务（assistant.asr_server，端口 8770）
$AsrProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Name -match '^python' -and
        $_.CommandLine -like "*$ProjectDir*" -and
        $_.CommandLine -like '*assistant.asr_server*'
    }
foreach ($Proc in $AsrProcesses) {
    try {
        Stop-Process -Id $Proc.ProcessId -Force -ErrorAction Stop
        Write-Host "[停止] ASR 服务 (PID $($Proc.ProcessId))" -ForegroundColor Green
    } catch {
        Write-Host "[跳过] ASR 进程 $($Proc.ProcessId) 已退出" -ForegroundColor DarkGray
    }
}

# 停止页面服务（http.server 8899 --directory web）
$WebProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Name -match '^python' -and
        $_.CommandLine -like '*http.server*' -and
        $_.CommandLine -like '*8899*'
    }
foreach ($Proc in $WebProcesses) {
    try {
        Stop-Process -Id $Proc.ProcessId -Force -ErrorAction Stop
        Write-Host "[停止] 页面服务 (PID $($Proc.ProcessId))" -ForegroundColor Green
    } catch {
        Write-Host "[跳过] 页面进程 $($Proc.ProcessId) 已退出" -ForegroundColor DarkGray
    }
}

if (-not $AsrProcesses -and -not $WebProcesses) {
    Write-Host '没有发现运行中的语音预览服务。' -ForegroundColor Yellow
}

# 端口确认
Start-Sleep -Milliseconds 600
$Remaining = @(8770, 8899) |
    Where-Object { Get-NetTCPConnection -LocalPort $_ -State Listen -ErrorAction SilentlyContinue }
if ($Remaining) {
    Write-Host "[警告] 端口 $($Remaining -join ', ') 仍被占用，可能被其他程序使用。" -ForegroundColor Yellow
} else {
    Write-Host '语音预览服务已全部停止（8770 / 8899）。' -ForegroundColor Green
}
Write-Host '注：Ollama 如需关闭，请右键托盘图标退出（避免影响其他应用）。'
