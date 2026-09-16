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

$AssistantArguments = @()
if ($AssistantArgumentsBase64) {
    $ArgumentJson = [System.Text.Encoding]::UTF8.GetString(
        [System.Convert]::FromBase64String($AssistantArgumentsBase64)
    )
    $DecodedArguments = ConvertFrom-Json -InputObject $ArgumentJson
    $AssistantArguments = @($DecodedArguments | ForEach-Object { [string]$_ })
}

try {
    & $VenvPython -u (Join-Path $ProjectDir 'voice_assistant.py') @AssistantArguments 1> $OutputLog 2> $ErrorLog
    if ($LASTEXITCODE -ne 0) {
        Add-Content -LiteralPath $ErrorLog -Value "语音助手退出代码：$LASTEXITCODE" -Encoding UTF8
    }
} catch {
    $_ | Out-String | Set-Content -LiteralPath $ErrorLog -Encoding UTF8
    exit 1
}
