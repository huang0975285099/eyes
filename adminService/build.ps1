param(
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$distDir = Join-Path $projectDir 'dist'
$source = Join-Path $projectDir 'src\main.cpp'
$ffmpegSource = Join-Path $projectDir '..\app\resources\ffmpeg.exe'
$output = Join-Path $distDir 'Viewer.exe'

if ($Clean -and (Test-Path $distDir)) {
    Remove-Item -LiteralPath $distDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $distDir | Out-Null

$compiler = Get-Command g++.exe -ErrorAction SilentlyContinue
if (-not $compiler) {
    throw '未找到 g++.exe。请安装 MSYS2 UCRT64 GCC，并将 C:\msys64\ucrt64\bin 加入 PATH。'
}
if (-not (Test-Path $ffmpegSource)) {
    throw "未找到H.264/H.265解码器: $ffmpegSource"
}

Write-Host '[1/2] 编译 Viewer.exe...'
& $compiler.Source `
    -std=c++20 `
    -O2 `
    -Wall `
    -Wextra `
    -D_WIN32_WINNT=0x0A00 `
    -DWINVER=0x0A00 `
    -municode `
    -mwindows `
    -static `
    -static-libgcc `
    -static-libstdc++ `
    $source `
    -o $output `
    -lwinhttp `
    -lcomctl32 `
    -lgdi32 `
    -lshell32 `
    -lole32
if ($LASTEXITCODE -ne 0) {
    throw "C++编译失败，退出码: $LASTEXITCODE"
}

Write-Host '[2/2] 复制FFmpeg原生解码器...'
Copy-Item -LiteralPath $ffmpegSource -Destination (Join-Path $distDir 'ffmpeg.exe') -Force

$configPath = Join-Path $distDir 'adminService.ini'
if (-not (Test-Path $configPath)) {
    @"
[server]
mediaServiceURL=http://10.0.20.219:22222
"@ | Set-Content -LiteralPath $configPath -Encoding utf8NoBOM
}

Write-Host "构建完成: $output"
