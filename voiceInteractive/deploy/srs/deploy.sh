#!/bin/bash
# SRS 6 服务器端部署脚本（在 Linux 服务器上执行）
# Windows 端 deploy-srs.ps1 会自动上传本目录并调用本脚本；也可手动执行：
#   CANDIDATE=10.0.20.219 bash deploy.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ---- candidate：浏览器可达的宿主机 IP（手动指定优先，否则自动探测首个非 docker 内网 IPv4） ----
CANDIDATE="${CANDIDATE:-}"
if [ -z "$CANDIDATE" ]; then
  CANDIDATE="$(ip -4 -o addr show scope global 2>/dev/null \
    | awk '{print $4}' | cut -d/ -f1 \
    | grep -vE '^(127\.|172\.(1[6-9]|2[0-9]|3[01])\.)' \
    | head -n1)"
  CANDIDATE="${CANDIDATE:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
fi
if [ -z "$CANDIDATE" ]; then
  echo "✗ 无法自动确定 candidate IP，请手动执行：CANDIDATE=<宿主机IP> bash deploy.sh"
  exit 1
fi

# ---- docker / docker compose（自动检测是否需要 sudo） ----
if docker info >/dev/null 2>&1; then
  DOCKER="docker"
else
  DOCKER="sudo docker"
fi
if ! $DOCKER info >/dev/null 2>&1; then
  echo "✗ Docker 未运行，请先安装并启动 Docker"
  exit 1
fi
if $DOCKER compose version >/dev/null 2>&1; then
  COMPOSE="$DOCKER compose"
else
  echo "✗ 未找到 docker compose v2，请安装 docker-compose-plugin"
  exit 1
fi

echo "=========================================="
echo "  SRS 6 部署（candidate=$CANDIDATE）"
echo "  目录: $SCRIPT_DIR"
echo "=========================================="

# ---- 1/3 渲染运行时配置（每次执行都重新渲染，candidate 可随时改） ----
echo "[1/3] 渲染 srs.runtime.conf ..."
if [ ! -f srs.conf ]; then
  echo "✗ 缺少模板文件 srs.conf"
  exit 1
fi
sed "s/__CANDIDATE__/${CANDIDATE}/g" srs.conf > srs.runtime.conf
grep -q "__CANDIDATE__" srs.runtime.conf && { echo "✗ candidate 渲染失败"; exit 1; }
echo "  ✓ candidate 已替换为 $CANDIDATE"

# ---- 2/3 启动 ----
echo "[2/3] 拉取镜像并启动 SRS 6 ..."
$COMPOSE pull srs
$COMPOSE up -d
$COMPOSE restart srs   # 确保新配置被加载（与项目 mediaService 部署习惯一致）

# ---- 3/3 等待 API 就绪并验证 ----
echo "[3/3] 等待 HTTP API 就绪 ..."
READY=""
for _ in $(seq 1 15); do
  if curl -sf http://127.0.0.1:1985/api/v1/versions >/dev/null 2>&1; then
    READY="1"
    break
  fi
  sleep 1
done
if [ -z "$READY" ]; then
  echo "✗ SRS API 15s 内未就绪，查看日志：$COMPOSE logs srs"
  $COMPOSE ps
  exit 1
fi
echo "  ✓ $(curl -s http://127.0.0.1:1985/api/v1/versions)"
$COMPOSE ps

echo ""
echo "=========================================="
echo "  ✓ SRS 6 部署成功"
echo "=========================================="
echo "  WHIP 推流（浏览器 USB 摄像头）:"
echo "    http://${CANDIDATE}:1985/rtc/v1/whip/?app=live&stream=usb1"
echo "  WHEP/SRS 播放信令:"
echo "    http://${CANDIDATE}:1985/rtc/v1/whep/?app=live&stream=usb1"
echo "  RTMP（飞桨 PP-Human 拉流）:"
echo "    rtmp://${CANDIDATE}:1935/live/usb1"
echo "  HTTP-FLV（备选拉流）:"
echo "    http://${CANDIDATE}:8080/live/usb1.flv"
echo "  HLS:"
echo "    http://${CANDIDATE}:8080/live/usb1.m3u8"
echo "------------------------------------------"
echo "  防火墙需放通: 1935/tcp 1985/tcp 8080/tcp 8000/udp"
if command -v ufw >/dev/null 2>&1 && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
  echo "  检测到 ufw 已启用，执行放通 ..."
  sudo ufw allow 1935/tcp >/dev/null
  sudo ufw allow 1985/tcp >/dev/null
  sudo ufw allow 8080/tcp >/dev/null
  sudo ufw allow 8000/udp >/dev/null
  echo "  ✓ ufw 规则已添加"
else
  echo "  （未检测到活动的 ufw，如用 firewalld/云安全组请自行放通上述端口）"
fi
echo "  查看日志: cd $SCRIPT_DIR && $COMPOSE logs -f srs"
echo "=========================================="
