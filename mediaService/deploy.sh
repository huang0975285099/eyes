#!/bin/bash

# MediaService - 离线部署脚本
# 使用方法: ./deploy.sh
#
# 在目标服务器上执行（无需网络），加载 build.sh 上传的镜像并启动服务。

set -e

# 镜像配置
IMAGE_NAME="media-service"
AI_IMAGE_NAME="eyes-ai-service"
TAG="latest"
SRS_IMAGE="ossrs/srs:6"
MYSQL_IMAGE="mysql:8.1.0"
SRS_TAR="srs-6.tar"
MYSQL_TAR="mysql-8.1.0.tar"
# 录像存储目录（4T 硬盘挂载点，按需修改）
# docker-compose.yml 中 volumes.device 指向此目录
# 注意：非 root 用户需要在有权限的目录下创建
RECORDING_DIR="${RECORDING_DIR:-/home/test/recordings}"
export RECORDING_DIR

if [ "${1:-}" = "-t" ] || [ "${1:-}" = "--tag" ]; then
    if [ $# -lt 2 ] || [[ ! "$2" =~ ^[A-Za-z0-9_.-]+$ ]]; then
        echo "✗ 错误: 镜像标签无效"
        exit 1
    fi
    TAG="$2"
elif [ $# -gt 0 ]; then
    echo "用法: $0 [-t tag]"
    exit 1
fi

TAR_FILE="${IMAGE_NAME}-${TAG}.tar"
AI_TAR_FILE="${AI_IMAGE_NAME}-${TAG}.tar"
export MEDIA_IMAGE="${IMAGE_NAME}:${TAG}"
export AI_IMAGE="${AI_IMAGE_NAME}:${TAG}"

# docker / docker compose 命令前缀（自动检测是否需要 sudo）
if docker info > /dev/null 2>&1; then
    DOCKER="docker"
    COMPOSE="docker compose"
else
    DOCKER="sudo docker"
    COMPOSE="sudo docker compose"
fi

echo "=========================================="
echo "  MediaService - 离线部署脚本"
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

# build.sh 只上传示例文件，不覆盖服务器上的 .env。
if [ ! -f ".env" ]; then
    echo "✗ 错误: 当前目录缺少 .env"
    echo "  请执行: cp .env.example .env，然后填写运行配置"
    exit 1
fi
echo "  ✓ .env 配置文件已加载"

if ! ${COMPOSE} config -q; then
    echo "✗ 错误: docker-compose.yml 或 .env 配置无效"
    exit 1
fi
echo "  ✓ Compose 配置校验通过"

# 创建录像存储目录
echo ""
echo "[2/6] 创建录像存储目录..."
mkdir -p "${RECORDING_DIR}"
echo "  ✓ 录像目录已就绪: ${RECORDING_DIR}"
echo "  ℹ️  请确认 4T 硬盘已挂载到该目录：df -h ${RECORDING_DIR}"
echo "     若挂载点不同，请修改 docker-compose.yml 中 volumes.device 后重新执行"

# 已有录像卷绝不能由Compose交互式重建。若部署参数与现有卷绑定目录不同，
# 在启动服务前明确失败，避免操作者误选确认后丢失录像卷元数据。
EXISTING_RECORDING_DIR=$(${DOCKER} volume inspect eyes_recordings \
    --format '{{index .Options "device"}}' 2>/dev/null || true)
if [ -n "${EXISTING_RECORDING_DIR}" ] && [ "${EXISTING_RECORDING_DIR}" != "<no value>" ]; then
    EXISTING_RECORDING_DIR=$(readlink -f "${EXISTING_RECORDING_DIR}")
    REQUESTED_RECORDING_DIR=$(readlink -f "${RECORDING_DIR}")
    if [ "${EXISTING_RECORDING_DIR}" != "${REQUESTED_RECORDING_DIR}" ]; then
        echo "✗ 错误: 现有eyes_recordings卷与本次录像目录不一致"
        echo "  现有目录: ${EXISTING_RECORDING_DIR}"
        echo "  本次目录: ${REQUESTED_RECORDING_DIR}"
        echo "  为保护录像数据，部署已停止；请修正RECORDING_DIR后重试。"
        exit 1
    fi
    echo "  ✓ 现有录像卷目录匹配: ${EXISTING_RECORDING_DIR}"
fi

# 加载依赖镜像
echo ""
echo "[3/6] 加载 SRS、MySQL 镜像..."
if ! ${DOCKER} image inspect "${SRS_IMAGE}" > /dev/null 2>&1; then
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

if ! ${DOCKER} image inspect "${MYSQL_IMAGE}" > /dev/null 2>&1; then
    if [ -f "${MYSQL_TAR}" ]; then
        ${DOCKER} load -i "${MYSQL_TAR}"
        echo "  ✓ MySQL 镜像加载完成"
    else
        echo "  ✗ 未找到 ${MYSQL_TAR}，无法离线部署"
        exit 1
    fi
else
    echo "  MySQL 镜像已存在，跳过加载"
fi

# 加载应用镜像
echo ""
echo "[4/6] 加载 media-service 镜像..."
if [ -f "${TAR_FILE}" ]; then
    ${DOCKER} load -i "${TAR_FILE}"
    echo "  ✓ 应用镜像加载完成"
else
    echo "  ✗ 未找到 ${TAR_FILE}，无法离线部署"
    exit 1
fi

if [ -f "${AI_TAR_FILE}" ]; then
    ${DOCKER} load -i "${AI_TAR_FILE}"
    echo "  ✓ AIService 镜像加载完成"
else
    echo "  ✗ 未找到 ${AI_TAR_FILE}，无法离线部署"
    exit 1
fi

# 验证镜像
echo ""
echo "[5/6] 验证镜像..."
APP_SIZE=$(${DOCKER} images "${IMAGE_NAME}:${TAG}" --format "{{.Size}}" 2>/dev/null)
AI_SIZE=$(${DOCKER} images "${AI_IMAGE_NAME}:${TAG}" --format "{{.Size}}" 2>/dev/null)
SRS_SIZE=$(${DOCKER} images "${SRS_IMAGE}" --format "{{.Size}}" 2>/dev/null)
MYSQL_SIZE=$(${DOCKER} images "${MYSQL_IMAGE}" --format "{{.Size}}" 2>/dev/null)
echo "  ✓ media-service:${TAG} → ${APP_SIZE}"
echo "  ✓ ${AI_IMAGE_NAME}:${TAG} → ${AI_SIZE}"
echo "  ✓ ${SRS_IMAGE} → ${SRS_SIZE}"
echo "  ✓ ${MYSQL_IMAGE} → ${MYSQL_SIZE}"

# 启动服务
echo ""
echo "[6/6] 启动服务（media-service + AIService + SRS + MySQL）..."
${COMPOSE} up -d --no-build --remove-orphans
# srs.conf中的回调服务名和端口已变更，必须重启SRS进程加载新配置。
${COMPOSE} restart srs

# 等待服务启动
echo ""
echo "  等待服务启动..."
MAX_WAIT=120
WAIT_INTERVAL=3
ELAPSED=0

while [ $ELAPSED -lt $MAX_WAIT ]; do
    sleep $WAIT_INTERVAL
    ELAPSED=$((ELAPSED + WAIT_INTERVAL))

    ALL_HEALTHY=true
    for SERVICE in mysql srs media-service ai-service; do
        CONTAINER_ID=$(${COMPOSE} ps -a -q "${SERVICE}")
        if [ -z "${CONTAINER_ID}" ]; then
            ALL_HEALTHY=false
            continue
        fi
        STATE=$(${DOCKER} inspect --format='{{.State.Status}}' "${CONTAINER_ID}")
        HEALTH=$(${DOCKER} inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${CONTAINER_ID}")
        if [ "${STATE}" = "exited" ] || [ "${STATE}" = "dead" ]; then
            echo "✗ ${SERVICE} 已退出（状态: ${STATE}）"
            ${COMPOSE} logs --tail=100 "${SERVICE}"
            exit 1
        fi
        if [ "${STATE}" != "running" ] || [ "${HEALTH}" != "healthy" ]; then
            ALL_HEALTHY=false
        fi
    done

    if [ "${ALL_HEALTHY}" = true ]; then
        echo ""
        echo "=========================================="
        echo "  ✓ 所有服务启动成功！"
        echo "=========================================="
        break
    fi

    if [ $ELAPSED -ge $MAX_WAIT ]; then
        echo ""
        echo "✗ 服务启动超时"
        ${COMPOSE} ps
        ${COMPOSE} logs --tail=100
        exit 1
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
echo "    - MediaService:  http://${REMOTE_IP}:22222"
echo "    - AI管理后台:     http://${REMOTE_IP}:11111/"
echo "    - AI健康检查:     http://${REMOTE_IP}:11111/health"
echo "    - RTMP 推流:     rtmp://${REMOTE_IP}:1935/live/{流名}"
echo "    - HTTP-FLV:      http://${REMOTE_IP}:8080/live/{流名}.flv"
echo "    - SRS HTTP API:  仅 Docker 内部 http://srs:1985"
echo "    - MySQL:         仅 Docker 内部 mysql:3306（数据库 eyes）"
echo "    - 录像存储目录:   ${RECORDING_DIR}"
echo "=========================================="
echo ""
echo "  常用命令:"
echo "    查看日志: ${COMPOSE} logs -f"
echo "    停止服务: ${COMPOSE} down"
echo "    重启服务: ${COMPOSE} restart"
echo "=========================================="
