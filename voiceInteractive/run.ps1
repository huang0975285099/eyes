$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ProjectDir '.venv\Scripts\python.exe'
$LogDir = Join-Path $ProjectDir 'logs'
$OutputLog = Join-Path $LogDir 'assistant-output.log'
$ErrorLog = Join-Path $LogDir 'assistant-error.log'
$LauncherErrorLog = Join-Path $LogDir 'launcher-error.log'
$StartLog = Join-Path $LogDir 'last-start.log'
$PidFile = Join-Path $LogDir 'assistant.pid'
$TaskName = 'XiaobuVoiceAssistant'
$RunFailed = $false

Set-Location -LiteralPath $ProjectDir
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8:replace'
chcp.com 65001 | Out-Null
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Get-RunningAssistant {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -match '^python' -and
            $_.CommandLine -like "*$ProjectDir*voice_assistant.py*"
        } |
        Select-Object -First 1
}

function Test-Dashboard {
    $Client = New-Object System.Net.Sockets.TcpClient
    try {
        $Connect = $Client.BeginConnect('127.0.0.1', 8765, $null, $null)
        if (-not $Connect.AsyncWaitHandle.WaitOne(2000)) {
            return $false
        }
        $Client.EndConnect($Connect)
        $Client.ReceiveTimeout = 2000
        $Client.SendTimeout = 2000
        $Stream = $Client.GetStream()
        $RequestBytes = [System.Text.Encoding]::ASCII.GetBytes(
            "GET /api/status HTTP/1.0`r`nHost: 127.0.0.1`r`nConnection: close`r`n`r`n"
        )
        $Stream.Write($RequestBytes, 0, $RequestBytes.Length)
        $Buffer = New-Object byte[] 256
        $Count = $Stream.Read($Buffer, 0, $Buffer.Length)
        if ($Count -le 0) {
            return $false
        }
        $ResponseHead = [System.Text.Encoding]::ASCII.GetString($Buffer, 0, $Count)
        return $ResponseHead -match '^HTTP/1\.[01] 200 '
    } catch {
        return $false
    } finally {
        $Client.Close()
    }
}

function Save-StartResult([string]$Message) {
    $Record = "{0}`r`n{1}" -f ([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss')), $Message
    Set-Content -LiteralPath $StartLog -Value $Record -Encoding UTF8
}

function Wait-AndActivateDashboard([string[]]$ProcessNames) {
    $Deadline = [DateTime]::UtcNow.AddSeconds(5)
    do {
        $WindowProcess = Get-Process -Name $ProcessNames -ErrorAction SilentlyContinue |
            Where-Object { $_.MainWindowTitle -like '*小布视觉助手*' } |
            Select-Object -First 1
        if ($WindowProcess) {
            return $true
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $Deadline)
    return $false
}

function Open-CameraDashboard {
    $Url = 'http://localhost:8765/'
    $ChromeCandidates = @(
        (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),
        (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe')
    )
    $ChromeExe = $ChromeCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if ($ChromeExe) {
        try {
            Start-Process -FilePath $ChromeExe -ArgumentList @("--app=$Url", '--start-maximized')
        } catch {
            Start-Process -FilePath (Join-Path $env:WINDIR 'explorer.exe') -ArgumentList $Url
            return
        }
        if (Wait-AndActivateDashboard @('chrome')) {
            return
        }
        Start-Process -FilePath (Join-Path $env:WINDIR 'explorer.exe') -ArgumentList $Url
        return
    }
    $EdgeCandidates = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe')
    )
    $EdgeExe = $EdgeCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if ($EdgeExe) {
        try {
            Start-Process -FilePath $EdgeExe -ArgumentList @("--app=$Url", '--start-maximized')
        } catch {
            Start-Process -FilePath (Join-Path $env:WINDIR 'explorer.exe') -ArgumentList $Url
            return
        }
        if (-not (Wait-AndActivateDashboard @('msedge'))) {
            Start-Process -FilePath (Join-Path $env:WINDIR 'explorer.exe') -ArgumentList $Url
        }
        return
    }
    Start-Process -FilePath (Join-Path $env:WINDIR 'explorer.exe') -ArgumentList $Url
}

try {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    Remove-Item -LiteralPath $LauncherErrorLog -Force -ErrorAction SilentlyContinue

    if (-not (Test-Path -LiteralPath $VenvPython)) {
        Write-Host '首次运行：正在自动安装 Python 环境……' -ForegroundColor Yellow
        & (Join-Path $ProjectDir 'setup.ps1')
        if ($LASTEXITCODE -ne 0) {
            throw "Python 环境安装失败，退出代码：$LASTEXITCODE"
        }
    }

    $DiagnosticArguments = @('--list-devices', '--download-model', '--test-speaker')
    $IsDiagnosticRun = @($args | Where-Object { $_ -in $DiagnosticArguments }).Count -gt 0

    if ($IsDiagnosticRun) {
        & $VenvPython voice_assistant.py @args
        if ($LASTEXITCODE -ne 0) {
            throw "诊断命令执行失败，退出代码：$LASTEXITCODE"
        }
        return
    }

    $ExistingAssistant = Get-RunningAssistant
    if ($ExistingAssistant) {
        $SuccessMessage = "小布语音助手已经在后台运行（进程 $($ExistingAssistant.ProcessId)）。"
        Write-Host $SuccessMessage -ForegroundColor Green
        Write-Host '正在打开摄像头页面：http://localhost:8765/'
        Save-StartResult $SuccessMessage
        Open-CameraDashboard
        return
    }

    & (Join-Path $ProjectDir 'ensure-ollama.ps1')

    Write-Host '小布语音助手：正在后台启动……' -ForegroundColor Yellow
    $PwshCommand = Get-Command 'pwsh.exe' -ErrorAction SilentlyContinue
    $PowerShellExe = if ($PwshCommand) {
        $PwshCommand.Source
    } else {
        (Get-Process -Id $PID).Path
    }
    $HostScript = Join-Path $ProjectDir 'assistant-host.ps1'
    $TaskArguments = "-WindowStyle Hidden -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$HostScript`""
    $TaskAction = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $TaskArguments -WorkingDirectory $ProjectDir
    $TaskPrincipal = New-ScheduledTaskPrincipal `
        -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
        -LogonType Interactive `
        -RunLevel Highest
    $TaskSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $TaskAction `
        -Principal $TaskPrincipal `
        -Settings $TaskSettings `
        -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName

    $Deadline = [DateTime]::UtcNow.AddSeconds(40)
    while ([DateTime]::UtcNow -lt $Deadline) {
        Start-Sleep -Milliseconds 500
        $TaskState = (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue).State
        if ($TaskState -eq 'Ready') {
            $ErrorDetail = if (Test-Path -LiteralPath $ErrorLog) {
                (Get-Content -LiteralPath $ErrorLog -Tail 30 -Encoding UTF8) -join [Environment]::NewLine
            } else {
                '没有生成错误日志。'
            }
            throw "后台进程提前退出。$([Environment]::NewLine)$ErrorDetail"
        }
        if (Test-Dashboard) {
            break
        }
    }

    if (-not (Test-Dashboard)) {
        & (Join-Path $ProjectDir 'stop.ps1')
        throw '语音助手在 40 秒内未能启动摄像头页面。'
    }

    Write-Host '小布语音助手已由 Windows 后台任务托管。' -ForegroundColor Green
    Write-Host '摄像头页面：http://localhost:8765/'
    Write-Host '关闭此 PowerShell 窗口不会停止语音助手。'
    Write-Host '需要停止时运行：.\stop.ps1'
    Save-StartResult '启动成功：http://localhost:8765/ 可以访问。'
    Open-CameraDashboard
} catch {
    $RunFailed = $true
    $ErrorMessage = $_.Exception.Message
    Set-Content -LiteralPath $LauncherErrorLog -Value $ErrorMessage -Encoding UTF8
    Save-StartResult "启动失败：$ErrorMessage"
    Write-Host ''
    Write-Host '小布语音助手启动失败：' -ForegroundColor Red
    Write-Host $ErrorMessage -ForegroundColor Red
    Write-Host "诊断日志：$LauncherErrorLog" -ForegroundColor Yellow
}

if ($RunFailed) {
    Write-Host ''
    Read-Host '按回车键关闭窗口'
    exit 1
}
