# 编译 screen-helper.exe（嵌入管理员权限 manifest）
# 用法：在此目录下执行  .\build.ps1

Set-StrictMode -Off
$ErrorActionPreference = 'Stop'

Write-Host "[1/3] 安装 rsrc 工具（用于将 manifest 打包进 .syso）..."
go install github.com/akavel/rsrc@latest

Write-Host "[2/3] 生成资源文件 rsrc.syso ..."
& rsrc -manifest manifest.xml -o rsrc.syso
if ($LASTEXITCODE -ne 0) { throw "rsrc 失败" }

Write-Host "[3/3] 编译 screen-helper.exe ..."
$env:GOOS   = 'windows'
$env:GOARCH = 'amd64'
$env:CGO_ENABLED = '0'
go build -ldflags "-s -w" -o screen-helper.exe .
if ($LASTEXITCODE -ne 0) { throw "go build 失败" }

Write-Host "完成：screen-helper.exe 已生成（已嵌入 requireAdministrator manifest）"
Write-Host "将新 exe 复制到 resources\ 目录后重新打包即可生效。"
