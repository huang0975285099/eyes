#!/bin/bash

# RecordingService - 离线部署脚本
# 使用方法: ./deploy.sh
#
# 在目标服务器上执行（无需网络），加载 build.sh 上传的镜像并启动服务。

set -e

# 镜像配置
IMAGE_NAME="recording-service"
TAG="latest"
SRS_IMAGE="ossrs/srs:5"
TAR_FILE="${IMAGE_NAME}-${TAG}.tar"
SRS_TAR="srs-5.tar"
# 录像存储目录（4T 硬盘挂载点，按需修改）
# docker-compose.yml 中 volumes.device 指向此目录
# 注意：非 root 用户需要在有权限的目录下创建
RECORDING_DIR="/home/administrator/recordings"

# docker / docker compose 命令前缀（自动检测是否需要 sudo）
if docker info > /dev/null 2>&1; then
    DOCKER="docker"
    COMPOSE="docker compose"
else
    DOCKER="sudo docker"
    COMPOSE="sudo docker compose"
fi

echo "=========================================="
echo "  RecordingService - 离线部署脚本"
echo "=========================================="
echo "  部署时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "  录像目录: ${RECORDING_DIR}"
echo "=========================================="

# 检查 Docker 环境
echo ""
echo "[1/6] 检查 Docker 环境..."
if ! ${DOCKER} info > /dev/null 2>&1; then
    echo "✗ 错误: Docker 未运行，请先安装并启动 Docker"
    exit 1
fi
echo "  ✓ Docker 正在运行"

if ! ${COMPOSE} version > /dev/null 2>&1; then
    echo "✗ 错误: docker compose 不可用，请安装 docker-compose-plugin"
    exit 1
fi
echo "  ✓ docker compose 可用"

# 创建录像存储目录
echo ""
echo "[2/6] 创建录像存储目录..."
mkdir -p "${RECORDING_DIR}"
echo "  ✓ 录像目录已就绪: ${RECORDING_DIR}"
echo "  ℹ️  请确认 4T 硬盘已挂载到该目录：df -h ${RECORDING_DIR}"
echo "     若挂载点不同，请修改 docker-compose.yml 中 volumes.device 后重新执行"

# 加载 SRS 镜像
echo ""
echo "[3/6] 加载 SRS 镜像..."
if ! ${DOCKER} images "${SRS_IMAGE}" | grep -q "srs"; then
    if [ -f "${SRS_TAR}" ]; then
        ${DOCKER} load -i "${SRS_TAR}"
        echo "  ✓ SRS 镜像加载完成"
    else
        echo "  ✗ 未找到 ${SRS_TAR}，无法离线部署"
        exit 1
    fi
else
    echo "  SRS 镜像已存在，跳过加载"
fi

# 加载应用镜像（始终加载，确保覆盖旧版本）
echo ""
echo "[4/6] 加载 recording-service 镜像..."
if [ -f "${TAR_FILE}" ]; then
    ${DOCKER} load -i "${TAR_FILE}"
    echo "  ✓ 应用镜像加载完成"
else
    echo "  ✗ 未找到 ${TAR_FILE}，无法离线部署"
    exit 1
fi

# 验证镜像
echo ""
echo "[5/6] 验证镜像..."
APP_SIZE=$(${DOCKER} images "${IMAGE_NAME}:${TAG}" --format "{{.Size}}" 2>/dev/null)
SRS_SIZE=$(${DOCKER} images "${SRS_IMAGE}" --format "{{.Size}}" 2>/dev/null)
echo "  ✓ recording-service:${TAG} → ${APP_SIZE}"
echo "  ✓ ${SRS_IMAGE} → ${SRS_SIZE}"

# 启动服务
echo ""
echo "[6/6] 启动服务（recording-service + SRS）..."
${COMPOSE} down 2>/dev/null || true
${COMPOSE} up -d

# 等待服务启动
echo ""
echo "  等待服务启动..."
MAX_WAIT=60
WAIT_INTERVAL=3
ELAPSED=0

while [ $ELAPSED -lt $MAX_WAIT ]; do
    sleep $WAIT_INTERVAL
    ELAPSED=$((ELAPSED + WAIT_INTERVAL))

    RUNNING=$(${COMPOSE} ps | grep -c "Up" || echo "0")
    TOTAL=$(${COMPOSE} ps -a | grep -cE "recording|srs" || echo "0")

    if [ "$RUNNING" -ge "$TOTAL" ] && [ "$TOTAL" -gt 0 ]; then
        echo ""
        echo "=========================================="
        echo "  ✓ 所有服务启动成功！"
        echo "=========================================="
        break
    fi

    if [ $ELAPSED -ge $MAX_WAIT ]; then
        echo ""
        echo "  ⚠️ 服务启动超时，请检查日志：${COMPOSE} logs -f"
    fi
done

# 显示最终状态
echo ""
${COMPOSE} ps

echo ""
echo "  服务状态:"
for container in $(${COMPOSE} ps -q); do
    NAME=$(${DOCKER} inspect --format='{{.Name}}' ${container} | sed 's/\///')
    STATUS=$(${DOCKER} inspect --format='{{.State.Status}}' ${container})
    echo "    - ${NAME}: ${STATUS}"
done

# 输出访问地址
echo ""
echo "=========================================="
echo "  访问地址:"
REMOTE_IP=$(hostname -I | awk '{print $1}')
echo "    - 录像回放 Web: http://${REMOTE_IP}:8089"
echo "    - RTMP 推流:     rtmp://${REMOTE_IP}:21935/live/{流名}"
echo "    - HTTP-FLV:      http://${REMOTE_IP}:28080/live/{流名}.flv"
echo "    - SRS HTTP API:  http://${REMOTE_IP}:21985/api/v1/streams"
echo "    - 录像存储目录:   ${RECORDING_DIR}"
echo "=========================================="
echo ""
echo "  常用命令:"
echo "    查看日志: ${COMPOSE} logs -f"
echo "    停止服务: ${COMPOSE} down"
echo "    重启服务: ${COMPOSE} restart"
echo "=========================================="
