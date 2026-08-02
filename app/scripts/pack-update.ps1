$ErrorActionPreference = 'Stop'

$projectDir = Split-Path -Parent $PSScriptRoot
$distDir = Join-Path $projectDir 'dist'
$packageJson = [System.IO.File]::ReadAllText((Join-Path $projectDir 'package.json'), [System.Text.Encoding]::UTF8)
$package = $packageJson | ConvertFrom-Json
$version = [string]$package.version
$latestPath = Join-Path $distDir 'latest.yml'
$installerName = "all-seeing-eyes-$version-setup.exe"
$installerPath = Join-Path $distDir $installerName
$builderConfigPath = Join-Path $distDir 'builder-effective-config.yaml'
$blockmapPath = "$installerPath.blockmap"
$zipPath = Join-Path $distDir "$version.zip"

foreach ($requiredFile in @($installerPath)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Missing update file: $requiredFile. Run pnpm build:win first."
    }
}

$installerStream = [System.IO.File]::OpenRead($installerPath)
$sha = [System.Security.Cryptography.SHA512]::Create()
try {
    $actualHash = [Convert]::ToBase64String($sha.ComputeHash($installerStream))
} finally {
    $sha.Dispose()
    $installerStream.Dispose()
}

$installerSize = (Get-Item -LiteralPath $installerPath).Length
$releaseDate = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
$latest = @"
version: $version
files:
  - url: $installerName
    sha512: $actualHash
    size: $installerSize
path: $installerName
sha512: $actualHash
releaseDate: '$releaseDate'
"@
[System.IO.File]::WriteAllText($latestPath, $latest, [System.Text.UTF8Encoding]::new($false))

$files = @($installerPath, $latestPath)
if (Test-Path -LiteralPath $builderConfigPath -PathType Leaf) { $files += $builderConfigPath }
if (Test-Path -LiteralPath $blockmapPath -PathType Leaf) { $files += $blockmapPath }

Compress-Archive -LiteralPath $files -DestinationPath $zipPath -CompressionLevel Optimal -Force
Write-Host "Update package created: $zipPath"
