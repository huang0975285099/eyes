$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $ProjectDir 'logs'
$StopLog = Join-Path $LogDir 'last-stop.log'
$PidFile = Join-Path $LogDir 'assistant.pid'
$TaskName = 'XiaobuVoiceAssistant'

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

function Get-DashboardListeners {
    @(
        Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
    )
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
        $Connect = $Client.BeginConnect('127.0.0.1', 8765, $null, $null)
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
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $InitialListeners = Get-DashboardListeners
    $WasRunning = ($InitialListeners.Count -gt 0) -or ($Task -and $Task.State -eq 'Running')

    # 正常路径：让 Python 后台自己退出。这样会先关麦克风、HTTP 服务并写完日志。
    if ($InitialListeners.Count -gt 0) {
        $null = Request-AssistantShutdown
    }

    $StoppedGracefully = Wait-ForDashboardStop 12

    if (-not $StoppedGracefully) {
        # 兜底只处理本项目的计划任务和 Python 进程，不会误杀其他 8765 服务。
        if ($Task) {
            Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            Start-Sleep -Milliseconds 750
        }

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
                throw "端口 8765 被其他程序占用（进程 $ListenerPid），为安全起见未结束它。"
            }
            if ($ListenerPid -notin $ProcessIds) {
                $Processes += $ListenerProcess
                $ProcessIds += $ListenerPid
            }
        }

        # 先结束子 Python，再结束启动器 Python。
        foreach ($Process in ($Processes | Sort-Object {
            if ($_.ParentProcessId -in $ProcessIds) { 0 } else { 1 }
        })) {
            if (Get-Process -Id $Process.ProcessId -ErrorAction SilentlyContinue) {
                Stop-Process -Id $Process.ProcessId -Force -ErrorAction SilentlyContinue
            }
        }

        if (-not (Wait-ForDashboardStop 5)) {
            $RemainingPids = ((Get-DashboardListeners).OwningProcess | Select-Object -Unique) -join ', '
            throw "停止失败，端口 8765 仍由进程 $RemainingPids 监听。"
        }
    }

    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    if (Test-Path -LiteralPath $PidFile) {
        Remove-Item -LiteralPath $PidFile -Force
    }

    if ($WasRunning) {
        $Message = '老叶语音助手已停止，端口 8765 已释放。'
        Write-Host $Message -ForegroundColor Green
    } else {
        $Message = '老叶语音助手当前没有运行，端口 8765 已释放。'
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
