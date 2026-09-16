$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $ProjectDir 'logs'
$StopLog = Join-Path $LogDir 'last-stop.log'
$RuntimeStateFile = Join-Path $LogDir 'runtime.json'
$LegacyPidFile = Join-Path $LogDir 'assistant.pid'
$TaskNames = @('LaoyeVoiceAssistant', 'XiaobuVoiceAssistant')
$DashboardPort = 8765

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

if (Test-Path -LiteralPath $RuntimeStateFile) {
    try {
        $RuntimeState = Get-Content -LiteralPath $RuntimeStateFile -Raw | ConvertFrom-Json
        if ($RuntimeState.port) {
            $DashboardPort = [int]$RuntimeState.port
        }
    } catch {
        $DashboardPort = 8765
    }
} else {
    try {
        $DefaultConfig = Get-Content -LiteralPath (Join-Path $ProjectDir 'config.json') -Raw | ConvertFrom-Json
        if ($DefaultConfig.web_port) {
            $DashboardPort = [int]$DefaultConfig.web_port
        }
    } catch {
        $DashboardPort = 8765
    }
}

function Get-DashboardListeners {
    @(Get-NetTCPConnection -LocalPort $DashboardPort -State Listen -ErrorAction SilentlyContinue)
}

function Get-AssistantPythonProcesses {
    @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Name -match '^python(?:w)?(?:\.exe)?$' -and
                $_.CommandLine -like "*$ProjectDir*voice_assistant.py*"
            }
    )
}

function Wait-ForDashboardStop([int]$Seconds) {
    $Deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    do {
        if ((Get-DashboardListeners).Count -eq 0) {
            return $true
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $Deadline)
    return (Get-DashboardListeners).Count -eq 0
}

function Request-AssistantShutdown {
    $Client = New-Object System.Net.Sockets.TcpClient
    try {
        $Connect = $Client.BeginConnect('127.0.0.1', $DashboardPort, $null, $null)
        if (-not $Connect.AsyncWaitHandle.WaitOne(3000)) {
            return $false
        }
        $Client.EndConnect($Connect)
        $Client.ReceiveTimeout = 3000
        $Client.SendTimeout = 3000
        $Stream = $Client.GetStream()
        $RequestBytes = [System.Text.Encoding]::ASCII.GetBytes(
            "POST /api/shutdown HTTP/1.0`r`nHost: 127.0.0.1`r`nContent-Length: 0`r`nConnection: close`r`n`r`n"
        )
        $Stream.Write($RequestBytes, 0, $RequestBytes.Length)
        $Buffer = New-Object byte[] 256
        $Count = $Stream.Read($Buffer, 0, $Buffer.Length)
        if ($Count -le 0) {
            return $false
        }
        $ResponseHead = [System.Text.Encoding]::ASCII.GetString($Buffer, 0, $Count)
        return $ResponseHead -match '^HTTP/1\.[01] 202 '
    } catch {
        return $false
    } finally {
        $Client.Close()
    }
}

function Save-StopResult([string]$Message) {
    $Record = "{0}`r`n{1}" -f ([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss')), $Message
    Set-Content -LiteralPath $StopLog -Value $Record -Encoding UTF8
}

try {
    $Tasks = @(
        foreach ($TaskName in $TaskNames) {
            Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        }
    )
    $InitialListeners = Get-DashboardListeners
    $WasRunning = ($InitialListeners.Count -gt 0) -or @(
        $Tasks | Where-Object { $_.State -eq 'Running' }
    ).Count -gt 0

    # 正常路径：让 Python 后台自己退出，先释放麦克风、摄像头和 HTTP 服务。
    if ($InitialListeners.Count -gt 0) {
        $null = Request-AssistantShutdown
    }
    $StoppedGracefully = Wait-ForDashboardStop 12

    if (-not $StoppedGracefully) {
        foreach ($Task in $Tasks) {
            Stop-ScheduledTask -TaskName $Task.TaskName -ErrorAction SilentlyContinue
        }
        Start-Sleep -Milliseconds 750

        $Processes = Get-AssistantPythonProcesses
        $ProcessIds = @($Processes.ProcessId)
        foreach ($Listener in Get-DashboardListeners) {
            $ListenerPid = $Listener.OwningProcess
            $ListenerProcess = Get-CimInstance Win32_Process `
                -Filter "ProcessId = $ListenerPid" `
                -ErrorAction SilentlyContinue
            if (-not $ListenerProcess) {
                continue
            }
            if ($ListenerProcess.CommandLine -notlike "*$ProjectDir*voice_assistant.py*") {
                throw "端口 $DashboardPort 被其他程序占用（进程 $ListenerPid），为安全起见未结束它。"
            }
            if ($ListenerPid -notin $ProcessIds) {
                $Processes += $ListenerProcess
                $ProcessIds += $ListenerPid
            }
        }

        foreach ($Process in ($Processes | Sort-Object {
            if ($_.ParentProcessId -in $ProcessIds) { 0 } else { 1 }
        })) {
            if (Get-Process -Id $Process.ProcessId -ErrorAction SilentlyContinue) {
                Stop-Process -Id $Process.ProcessId -Force -ErrorAction SilentlyContinue
            }
        }

        if (-not (Wait-ForDashboardStop 5)) {
            $RemainingPids = ((Get-DashboardListeners).OwningProcess | Select-Object -Unique) -join ', '
            throw "停止失败，端口 $DashboardPort 仍由进程 $RemainingPids 监听。"
        }
    }

    foreach ($TaskName in $TaskNames) {
        if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
    }
    Remove-Item -LiteralPath $RuntimeStateFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $LegacyPidFile -Force -ErrorAction SilentlyContinue

    if ($WasRunning) {
        $Message = "老叶语音助手已停止，端口 $DashboardPort 已释放。"
        Write-Host $Message -ForegroundColor Green
    } else {
        $Message = "老叶语音助手当前没有运行，端口 $DashboardPort 已释放。"
        Write-Host $Message -ForegroundColor Yellow
    }
    Save-StopResult $Message
} catch {
    $Message = "停止失败：$($_.Exception.Message)"
    Save-StopResult $Message
    Write-Host $Message -ForegroundColor Red
    Write-Host "诊断记录：$StopLog" -ForegroundColor Yellow
    exit 1
}
