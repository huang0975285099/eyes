param(
    [string]$AssistantArgumentsBase64 = ''
)

$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ProjectDir '.venv\Scripts\python.exe'
$OutputLog = Join-Path $ProjectDir 'logs\assistant-output.log'
$ErrorLog = Join-Path $ProjectDir 'logs\assistant-error.log'

Set-Location -LiteralPath $ProjectDir
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8:replace'
$env:LAOYE_NO_BROWSER = '1'
# PowerShell 重定向子进程输出时会按 [Console]::OutputEncoding 解码字节；
# 计划任务的后台会话默认是 GBK，会把 Python 的 UTF-8 输出解码成乱码写进日志。
try {
    [Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
} catch {
    # 极少数无控制台会话设置失败时退回默认编码，仅影响日志可读性。
}

$AssistantArguments = @()
if ($AssistantArgumentsBase64) {
    $ArgumentJson = [System.Text.Encoding]::UTF8.GetString(
        [System.Convert]::FromBase64String($AssistantArgumentsBase64)
    )
    $DecodedArguments = ConvertFrom-Json -InputObject $ArgumentJson
    $AssistantArguments = @($DecodedArguments | ForEach-Object { [string]$_ })
}

try {
    while ($true) {
        & $VenvPython -u (Join-Path $ProjectDir 'voice_assistant.py') @AssistantArguments 1> $OutputLog 2> $ErrorLog
        $AssistantExitCode = $LASTEXITCODE
        if ($AssistantExitCode -eq 75) {
            Start-Sleep -Seconds 1
            continue
        }
        if ($AssistantExitCode -ne 0) {
            Add-Content -LiteralPath $ErrorLog -Value "语音助手退出代码：$AssistantExitCode" -Encoding UTF8
        }
        break
    }
} catch {
    $_ | Out-String | Set-Content -LiteralPath $ErrorLog -Encoding UTF8
    exit 1
}
