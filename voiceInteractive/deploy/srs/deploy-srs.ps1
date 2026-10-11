# SRS 6 一键部署脚本（在 Windows 本机执行，远程部署到 Linux 服务器）
# 依赖：Windows 自带 OpenSSH 客户端（ssh / scp，Win10 1809+ 默认已装）
#
# 用法：
#   .\deploy-srs.ps1                                        # 默认部署到 root@10.0.20.219:/opt/srs-eyes
#   .\deploy-srs.ps1 -User test -RemoteDir /home/test/srs   # 自定义账号与目录
#   .\deploy-srs.ps1 -Candidate 10.0.20.219                 # 多网卡时手动指定对外 IP
#   .\deploy-srs.ps1 -Logs                                  # 部署后跟踪容器日志
param(
    [string]$ServerIp = '10.0.20.219',
    [string]$User = 'root',
    [string]$RemoteDir = '/opt/srs-eyes',
    [string]$Candidate = '',
    [switch]$Logs
)

$ErrorActionPreference = 'Stop'
if (-not $Candidate) { $Candidate = $ServerIp }

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = "${User}@${ServerIp}"

foreach ($cmd in 'ssh', 'scp') {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        throw "未找到 $cmd 命令，请先安装 Windows OpenSSH 客户端（设置 → 应用 → 可选功能 → OpenSSH 客户端）"
    }
}

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  SRS 6 部署: $target" -ForegroundColor Cyan
Write-Host "  远程目录: $RemoteDir / candidate: $Candidate" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

# 1/3 创建远程目录（首次连接自动接受主机指纹，避免交互卡住）
Write-Host "[1/3] 准备远程目录 ..." -ForegroundColor Green
ssh -o StrictHostKeyChecking=accept-new $target "mkdir -p '$RemoteDir'"

# 2/3 上传 compose、配置模板与服务端脚本
Write-Host "[2/3] 上传部署文件 ..." -ForegroundColor Green
scp `
    (Join-Path $here 'docker-compose.yml') `
    (Join-Path $here 'srs.conf') `
    (Join-Path $here 'deploy.sh') `
    "${target}:$RemoteDir/"

# 3/3 远程执行：统一转 LF（防 Windows CRLF 导致 bash 报 \r 错误）→ 渲染配置 → 启动
Write-Host "[3/3] 远程启动 SRS 6 ..." -ForegroundColor Green
ssh $target "cd '$RemoteDir' && sed -i 's/\r$//' deploy.sh srs.conf docker-compose.yml && CANDIDATE='$Candidate' bash deploy.sh"

# 本机侧验证 API 可达（不通通常是防火墙 / 跨网段问题）
Write-Host ""
try {
    $resp = Invoke-RestMethod "http://${ServerIp}:1985/api/v1/versions" -TimeoutSec 5
    Write-Host "✓ 本机访问 SRS API 正常，服务版本信息：$($resp.data | ConvertTo-Json -Compress)" -ForegroundColor Green
} catch {
    Write-Host "⚠ 服务器内已启动，但本机无法访问 http://${ServerIp}:1985，请检查防火墙/安全组与跨网段路由" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "推流地址: http://${ServerIp}:1985/rtc/v1/whip/?app=live&stream=usb1" -ForegroundColor Cyan
Write-Host "拉流地址: rtmp://${ServerIp}:1935/live/usb1" -ForegroundColor Cyan

if ($Logs) {
    Write-Host "跟踪日志（Ctrl+C 退出）..." -ForegroundColor DarkGray
    ssh $target "cd '$RemoteDir' && docker compose logs -f srs"
}
